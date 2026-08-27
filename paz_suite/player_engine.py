"""Reusable clip-playback engine — the piece both tabs' players share.

Decodes raw RGB frames from ffmpeg into a caller-supplied canvas while a
second ffplay process plays the audio track alongside it, both started at
the same position (not frame-accurate lip sync, but real sound instead of
none). A bounded queue keeps memory flat: when paused, ffmpeg blocks on its
own pipe and simply waits.

Playback is paced against the wall clock, not by adding a fixed delay after
each frame. Tk's after() guarantees only a *minimum* delay, and decoding
plus blitting a frame is far from free, so "wait 16ms, draw, repeat" ends
up spending 16ms + however long the work took on every single frame - the
video falls a little further behind real time with each one, which is
exactly what made playback drift away from its own audio and look fine for
a moment before visibly breaking up. Instead every frame gets an absolute
deadline measured from when playback started, and when the display can't
keep up frames are dropped to stay on that timeline rather than played
late. Smooth and in sync beats every-frame-but-drifting, especially at
60fps where the per-frame budget is only ~16ms to begin with.

This module owns none of the surrounding UI (buttons, seek bar, clock) —
callers wire it up via small callbacks (`on_tick`, `on_state`, `on_fail`,
`on_eof`) and read `.playing` / `.position` / `.duration` as needed. That
split is what lets the Convert tab's inspector and the Library tab's
viewer both get real play/pause/seek/volume without duplicating the
ffmpeg-pipe plumbing.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time

from PIL import Image, ImageTk

from .files import NO_WINDOW
from .media import read_exact, has_ffplay

HAS_FFPLAY = has_ffplay()


def _reap(proc) -> None:
    """Stop `proc` without waiting for it here.

    terminate() returns immediately but wait() does not, and a 4K ffmpeg
    can take the better part of a second to actually go away. Every seek,
    every pause and every change of clip went through two of those waits
    (video and audio), on the UI thread, which is why clicking the seek bar
    froze the window and swallowed the click. The process still gets
    collected - just on a thread nobody is looking at.
    """
    if proc is None:
        return

    def run():
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass
        # The pipes are deliberately left alone. A reader thread may still
        # be blocked inside read() on this same file object, and closing it
        # underneath that thread aborts the interpreter outright. The
        # process dying gives the reader a clean EOF, and subprocess closes
        # the descriptors when the object is collected.

    threading.Thread(target=run, daemon=True).start()


class ClipPlayer:

    # How much decoded video to keep buffered ahead. Half a second absorbs
    # a slow disk read or a scheduler hiccup without starving the display;
    # the byte cap keeps that from turning into hundreds of megabytes when
    # the player is stretched across a 4K screen.
    QUEUE_SECONDS = 0.5
    QUEUE_BYTES_CAP = 48 * 1024 * 1024
    # Never blit more than this many frames' worth of catch-up in one tick -
    # a long stall shouldn't turn into a visible fast-forward burst.
    MAX_CATCHUP = 8
    # Give up only after a genuinely long silence from the decoder. Opening
    # a large 4K file can take a moment before the first frame lands.
    STARVE_TIMEOUT = 8.0

    def __init__(self, canvas, width: int, height: int,
                 on_tick=None, on_state=None, on_fail=None, on_eof=None):
        self.canvas = canvas
        self.view_w = max(int(width) // 2 * 2, 240)
        self.view_h = max(int(height), 135)
        self.frame_bytes = self.view_w * self.view_h * 3
        self.on_tick = on_tick          # called(position) after each frame
        self.on_state = on_state        # called(playing: bool)
        self.on_fail = on_fail          # called(message)
        self.on_eof = on_eof            # called() — clip ended, not looping

        self.path: str | None = None
        self.duration = 0.0
        self.fps = 30.0
        self.stream_fps = 30.0
        self.position = 0.0
        self.playing = False
        self.speed = 1.0
        self.loop = True
        self.volume = 80
        self.muted = not HAS_FFPLAY

        self.proc = None
        self.audio_proc = None
        self._token = 0
        self._queue: queue.Queue = queue.Queue(maxsize=8)
        self._after = None
        self._photo = None
        self._canvas_item = None
        self._decoded = 0
        # wall-clock pacing
        # None until the decoder hands over its first frame - see
        # _arm_clock. Audio is held back until then so the two start
        # together instead of sound running ahead of a black canvas.
        self._clock_t0 = None
        self._wants_audio = False
        self._waiting_since = 0.0
        # Turned off for the rest of the session the first time a hardware
        # decode produces nothing, rather than per file - a build without
        # it will fail for every file, and retrying each one costs a
        # visible stall apiece.
        self._hwaccel = True
        self.av_offset = 0.0     # seconds; see _spawn_audio
        self._clock_pos = 0.0       # source position that _clock_t0 corresponds to
        self._frames_shown = 0      # frames consumed since _clock_t0 (incl. dropped)
        self._starved_at = 0.0      # when the queue first came up empty

    # ── content ──────────────────────────────────────────────────────────

    def load(self, path: str, duration: float, fps: float) -> None:
        self.stop()
        self.path = path
        self.duration = max(duration, 0.0)
        self.fps = min(max(fps or 30.0, 1.0), 60.0)
        self.position = 0.0

    def clear(self) -> None:
        self.stop()
        self.path = None
        self.duration = 0.0
        self.position = 0.0
        self.canvas.delete("all")
        self._photo = None
        self._canvas_item = None

    def set_size(self, width: int, height: int) -> None:
        width = max(int(width) // 2 * 2, 240)
        height = max(int(height), 135)
        if width == self.view_w and height == self.view_h:
            return
        was_playing = self.playing
        position = self.position
        self.stop()
        self.view_w, self.view_h = width, height
        self.frame_bytes = width * height * 3
        self.canvas.configure(width=width, height=height)
        self.position = position
        if self.path and was_playing:
            self.play()

    # ── transport ────────────────────────────────────────────────────────

    def play(self) -> None:
        if not self.path:
            return
        if not os.path.exists(self.path):
            self._fail("That file is no longer on disk.")
            return
        # A finished decoder leaves self.proc set but with nothing left to
        # read, so play() used to arm a clock against an empty queue and
        # sit there - which is "I press play and nothing happens", or
        # "nothing happens until I seek". poll() is non-blocking.
        spent = self.proc is not None and self.proc.poll() is not None
        if self.proc is None or spent:
            if self._spawn(self.position) is False:
                return
        elif self.audio_proc is None and self._clock_t0 is not None:
            self._spawn_audio(self.position)
        self.playing = True
        if self.on_state:
            self.on_state(True)
        self._arm_clock(self.position)
        self._schedule()

    def pause(self) -> None:
        self.playing = False
        if self.on_state:
            self.on_state(False)
        self._kill_audio()
        self._cancel_tick()

    def stop(self) -> None:
        self.pause()
        self._token += 1
        self._kill()

    def toggle(self) -> None:
        self.pause() if self.playing else self.play()

    def toggle_loop(self) -> bool:
        self.loop = not self.loop
        return self.loop

    def nudge(self, seconds: float) -> None:
        if self.path:
            self.seek(self.position + seconds)

    def seek(self, seconds: float) -> None:
        if not self.path:
            return
        limit = max(self.duration - 0.1, 0.0) if self.duration else seconds
        self.position = max(0.0, min(seconds, limit))
        was_playing = self.playing
        self._cancel_tick()
        # Seeking while paused used to start the audio process anyway (and
        # never draw anything), so scrubbing a paused clip played sound at
        # a frozen picture. Decode without audio and show the frame we
        # landed on instead.
        if self._spawn(self.position, with_audio=was_playing) is False:
            return
        self._arm_clock(self.position)
        if was_playing:
            self.playing = True
            self._schedule()
        else:
            self._preview_frame(self._token)

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        if self.muted:
            self._kill_audio()
        elif self.playing:
            self._spawn_audio(self.position)
        return self.muted

    def set_volume(self, value: int) -> None:
        self.volume = max(0, min(int(value), 100))
        if self.muted and self.volume > 0:
            self.muted = False
        if self.volume <= 0:
            self._kill_audio()
        elif self.playing and not self.muted:
            self._spawn_audio(self.position)

    # ── decoding ────────────────────────────────────────────────────────

    def _queue_size(self, rate: float) -> int:
        """Frames to buffer ahead: ~QUEUE_SECONDS worth, but never more
        than QUEUE_BYTES_CAP of raw RGB."""
        by_time = int(rate * self.QUEUE_SECONDS)
        by_bytes = int(self.QUEUE_BYTES_CAP / max(self.frame_bytes, 1))
        return max(4, min(by_time, by_bytes, 90))

    def _spawn(self, position: float, with_audio: bool = True):
        # Bump first: it invalidates every previous reader's alive() check
        # before the old process is even asked to stop, so no frame from
        # the old position can reach the new queue.
        self._token += 1
        token = self._token
        self._kill()
        # Held until the first frame arrives (_begin_clock). Starting it
        # here is what let sound run ahead of the picture.
        self._wants_audio = bool(with_audio)
        # The whole point of the pool is 4K/60 - capping decode below the
        # source rate here was making 60fps footage play back at half its
        # actual smoothness. self.fps is already clamped to 60 in load().
        rate = max(min(self.fps, 60.0), 1.0)
        self.stream_fps = rate
        # Captured in the reader's closure rather than read back off self:
        # a re-spawn (any seek, or the loop restart) swaps self._queue, and
        # a reader from the previous run that was mid-put would otherwise
        # drop a stale frame into the *new* queue - frames from the old
        # position leaking into the new one, which is what made seeking
        # look like it froze or jumped.
        frame_queue: queue.Queue = queue.Queue(maxsize=self._queue_size(rate))
        self._queue = frame_queue
        vf = (f"scale={self.view_w}:{self.view_h}:"
              f"force_original_aspect_ratio=decrease,"
              f"pad={self.view_w}:{self.view_h}:(ow-iw)/2:(oh-ih)/2,"
              f"fps={rate:.3f}")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        # Decoding 4K on the CPU and pushing it through a pipe is the one
        # thing here that cannot keep up with 60fps on most machines - the
        # picture starves while the audio, which costs nothing, runs on.
        # That is the stutter and the drift. Ask for hardware decode and
        # fall back if this build or file can't do it (see _handle_eof).
        if self._hwaccel:
            cmd += ["-hwaccel", "auto"]
        if position > 0.05:
            cmd += ["-ss", f"{position:.3f}"]
        cmd += ["-i", self.path, "-an", "-sn", "-vf", vf,
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        self._decoded = 0
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0, creationflags=NO_WINDOW)
        except OSError:
            self.proc = None
            self._fail("ffmpeg not found - install it and add it to PATH")
            return False
        proc = self.proc

        def alive():
            return token == self._token

        def reader():
            frames = 0
            try:
                while alive():
                    chunk = read_exact(proc.stdout, self.frame_bytes, alive)
                    if chunk is None:
                        break
                    frames += 1
                    while alive():
                        try:
                            frame_queue.put(chunk, timeout=0.2)
                            break
                        except queue.Full:
                            continue
            except (OSError, ValueError):
                pass
            finally:
                if alive():
                    # Set before the sentinel: _tick reads _decoded the
                    # moment it sees None, so writing it after would race.
                    self._decoded = frames
                    try:
                        frame_queue.put(None, timeout=0.5)
                    except queue.Full:
                        pass

        threading.Thread(target=reader, daemon=True).start()
        return True

    def _spawn_audio(self, position: float):
        self._kill_audio()
        if not HAS_FFPLAY or self.muted or self.volume <= 0 or not self.path:
            return
        # ffplay is a separate process with its own start-up cost, and
        # there is no clock between it and the video - so it produces its
        # first sound some hundreds of milliseconds after being asked,
        # by which time the picture has moved on. Nothing here can measure
        # that gap, but it is near enough constant on a given machine, so
        # it can be dialled out: `av_offset` is how much further into the
        # file to start the audio, in seconds. Positive if sound lags the
        # picture, negative if it runs ahead.
        start = max(position + self.av_offset, 0.0)
        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error",
               "-vn", "-volume", str(self.volume)]
        if start > 0.05:
            cmd += ["-ss", f"{start:.3f}"]
        cmd += ["-i", self.path]
        try:
            self.audio_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except OSError:
            self.audio_proc = None

    def _kill_audio(self):
        _reap(self.audio_proc)
        self.audio_proc = None

    def _kill(self):
        self._kill_audio()
        _reap(self.proc)
        self.proc = None

    def _fail(self, message: str):
        self.playing = False
        if self.on_state:
            self.on_state(False)
        self.canvas.delete("all")
        self._photo = None
        self._canvas_item = None
        self.canvas.create_text(self.view_w // 2, self.view_h // 2,
                                text=message, fill="#FF5C6E", font=("Segoe UI", 10),
                                width=self.view_w - 40)
        if self.on_fail:
            self.on_fail(message)

    def _cancel_tick(self):
        if self._after is not None:
            try:
                self.canvas.after_cancel(self._after)
            except ValueError:
                pass
            self._after = None

    # ── pacing ──────────────────────────────────────────────────────────

    def _arm_clock(self, position: float) -> None:
        """Get ready to play from `position`, but don't start counting yet.

        The clock used to start the instant play() was called, while
        ffmpeg was still opening the file. On a 4K clip that is easily
        half a second before the first frame exists - and the audio was
        already running, so sound led picture by however long the decoder
        took, every time. Counting starts when there is actually something
        to show; see _begin_clock.
        """
        self._clock_t0 = None
        self._clock_pos = position
        self._frames_shown = 0
        self._starved_at = 0.0
        self._waiting_since = time.monotonic()

    def _begin_clock(self) -> None:
        """First frame is in hand: start the clock and the sound together."""
        self._clock_t0 = time.monotonic()
        self._frames_shown = 0
        if self._wants_audio:
            self._spawn_audio(self._clock_pos)
            self._wants_audio = False

    def _period(self) -> float:
        """Wall-clock seconds between frames at the current speed."""
        return 1.0 / max(self.stream_fps * self.speed, 1.0)

    def _schedule(self):
        if not self.playing:
            return
        if self._clock_t0 is None:
            # Still waiting on the decoder's first frame. Poll briskly so
            # playback begins the moment it lands.
            self._after = self.canvas.after(5, self._tick)
            return
        # Deadline for the *next* frame, measured from the start of this
        # run - not "now + one frame", which is what accumulated drift.
        target = self._clock_t0 + self._frames_shown * self._period()
        delay_ms = int((target - time.monotonic()) * 1000)
        self._after = self.canvas.after(max(delay_ms, 1), self._tick)

    def _tick(self):
        self._after = None
        if not self.playing:
            return
        now = time.monotonic()

        if self._clock_t0 is None:
            # Waiting for frame one. Nothing is late yet, so no catch-up
            # logic applies - take a frame if there is one and start.
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                if now - self._waiting_since > self.STARVE_TIMEOUT:
                    self._fail("Could not decode this file")
                else:
                    self._after = self.canvas.after(5, self._tick)
                return
            if item is None:
                self._handle_eof()
                return
            self._begin_clock()
            self._frames_shown = 1
            self._blit(item)
            if self.on_tick:
                self.on_tick(self.position)
            self._schedule()
            return

        period = self._period()
        # How many frames should already have been shown by now. If the
        # display fell behind, pull (and discard) the stale ones so the
        # frame we actually paint is the one that belongs on screen right
        # now - the video stays locked to the audio instead of sliding.
        due = int((now - self._clock_t0) / period) - self._frames_shown + 1
        due = max(1, min(due, self.MAX_CATCHUP))

        chunk = None
        hit_eof = False
        for _ in range(due):
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                hit_eof = True
                break
            chunk = item
            self._frames_shown += 1

        if hit_eof:
            self._handle_eof()
            return

        if chunk is None:
            # Starved. Come back promptly rather than burning a whole frame
            # period waiting - a full-period retry guarantees falling
            # another frame behind every time the decoder stutters.
            if not self._starved_at:
                self._starved_at = now
            elif now - self._starved_at > self.STARVE_TIMEOUT:
                self._fail("Could not decode this file")
                return
            self._after = self.canvas.after(4, self._tick)
            return

        self._starved_at = 0.0
        self._blit(chunk)
        # Position comes from frames consumed, so dropping a frame advances
        # the clock exactly as much as showing it would have.
        self.position = self._clock_pos + self._frames_shown / self.stream_fps
        if self.duration and self.position > self.duration:
            self.position = self.duration
        if self.on_tick:
            self.on_tick(self.position)
        self._schedule()

    def _handle_eof(self):
        if self._decoded == 0:
            if self._hwaccel:
                # Nothing came out at all. Far more likely to be hardware
                # decoding this build can't actually do than a broken file,
                # so drop it and try again in software before giving up.
                self._hwaccel = False
                position = self.position
                if self._spawn(position, with_audio=self._wants_audio) is not False:
                    self._arm_clock(position)
                    self._schedule()
                    return
            self._fail("No video stream could be decoded from this file")
            return
        if self.loop and self.path is not None:
            self.position = 0.0
            self._spawn(0.0)
            self._arm_clock(0.0)
            self._schedule()
            return
        self.pause()
        self.position = 0.0
        if self.on_tick:
            self.on_tick(self.position)
        if self.on_eof:
            self.on_eof()

    def _preview_frame(self, token: int, tries: int = 0) -> None:
        """Paint the first frame of a paused seek once the decoder produces
        it, so scrubbing a paused clip actually shows where you landed."""
        if self.playing or token != self._token:
            return
        try:
            item = self._queue.get_nowait()
        except queue.Empty:
            if tries < 150:
                self.canvas.after(16, lambda: self._preview_frame(token, tries + 1))
            return
        if item is not None:
            self._blit(item)

    def _blit(self, chunk: bytes):
        try:
            image = Image.frombytes("RGB", (self.view_w, self.view_h), chunk)
        except Exception:
            return
        # Recreating the PhotoImage and canvas item every frame (the old
        # delete("all") + create_image approach) is the single biggest cost
        # in this loop at 60fps - Tk has to re-register a whole new image
        # each time. Painting into one persistent PhotoImage via .paste()
        # and reusing one canvas item is dramatically cheaper. But callers
        # (the idle thumbnail, a paused scrub frame) draw on this same
        # canvas and call delete("all") directly without knowing about our
        # cached item - canvas.type() is the one way to actually ask Tk
        # "does this item still exist", rather than trusting our own flag
        # and silently painting into an image nothing on screen points to
        # anymore (which is exactly what made playback look frozen after
        # the first clip: every clip after that kept "playing" into an
        # orphaned image while the canvas still showed the old thumbnail).
        stale = (self._photo is None or self._canvas_item is None
                 or self._photo.width() != self.view_w
                 or self._photo.height() != self.view_h
                 or self.canvas.type(self._canvas_item) != "image")
        if stale:
            self._photo = ImageTk.PhotoImage(image)
            self.canvas.delete("all")
            self._canvas_item = self.canvas.create_image(
                self.view_w // 2, self.view_h // 2,
                image=self._photo, anchor="center")
        else:
            self._photo.paste(image)
