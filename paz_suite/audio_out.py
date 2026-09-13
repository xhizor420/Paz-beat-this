"""The sound half of the built-in player, and the clock the picture follows.

The built-in player used to hand the audio track to a separate ffplay
process. Two processes, no clock between them: ffplay produced its first
sound some hundreds of milliseconds after being asked, the picture had
already moved on, and the gap stayed for the life of the clip. That is not
a tuning problem, it is a missing clock - which is why the only honest fix
on offer before now was a slider to guess the gap by hand.

Here the sound is decoded by ffmpeg into this process and written to the
audio device directly, so we know exactly how much of it has reached the
speakers. That number is the master clock: `position()` reports the point
in the clip the listener is hearing *right now*, and the video side paces
itself to it, dropping or holding frames to stay level. Audio is the right
master because a gap in sound is audible and a repeated frame is not.

Needs `sounddevice` (a small wrapper over PortAudio, wheels on every
platform). Without it `available()` is False and the player falls back to
the old ffplay path, out of sync but working.
"""

from __future__ import annotations

import importlib.util
import subprocess
import threading
import time

from .files import NO_WINDOW

# One format for every clip, so the device is opened once and never has to
# be reconfigured: ffmpeg resamples whatever the file holds into this.
RATE = 48000
CHANNELS = 2
DTYPE = "float32"
BYTES_PER_FRAME = CHANNELS * 4
# Frames per write. ~21 ms - small enough that a pause or a seek stops the
# sound promptly, large enough not to spend all day in Python.
BLOCK = 1024


# Whether we can really drive a device is settled once, on a thread, and
# then remembered. Two reasons it cannot be answered inline:
#
#   * find_spec() only proves the module file is on disk. Importing it is
#     what loads PortAudio, and that is what can fail - a pip install
#     whose native library is missing or the wrong architecture imports
#     with OSError('PortAudio library not found'). Answering from
#     find_spec alone promised a clock we did not have, which showed up as
#     no sound at all plus a menu claiming there was nothing to set.
#   * importing it is also the part that can be *slow*. Loading an audio
#     DLL enumerates the machine's devices, and on Windows with an unhappy
#     driver that is seconds, or hangs. Doing that on the thread drawing
#     the window is how a player freezes an app.
#
# So: unknown until proven, proven off to the side, and "unknown" counts
# as no - better to fall back to ffplay and drift than to promise a clock
# and produce silence.
_probe_lock = threading.Lock()
_probe: tuple | None = None          # (ok, reason) once settled
_probe_started = False


# Loaded once, here, and then used. Deliberately not imported inside the
# feeder thread where they are needed: importing numpy and an audio
# library is not thread-safe in any useful sense. Python's import lock and
# the platform's native library loader are both involved, and doing it on
# a worker while the main thread is using the same modules produces either
# a "partially initialized module" error or, on Windows, a process that
# stops answering - which is what pressing Play looked like.
_np = None
_sd = None


def _run_probe() -> tuple:
    global _probe, _np, _sd
    try:
        import numpy
        import sounddevice
    except Exception as exc:
        answer = (False, f"the audio packages will not load ({exc})")
    else:
        _np, _sd = numpy, sounddevice
        answer = (True, "")
    with _probe_lock:
        _probe = answer
    return answer


def start_probe() -> None:
    """Settle it in the background. Called once as the app starts."""
    global _probe_started
    with _probe_lock:
        if _probe_started or _probe is not None:
            return
        _probe_started = True
    threading.Thread(target=_run_probe, daemon=True).start()


def bindings_present() -> bool:
    """Cheap: is the module on disk at all. Says nothing about whether it
    loads, so it is only good enough for deciding that sound is worth
    offering a volume control for."""
    try:
        return importlib.util.find_spec("sounddevice") is not None
    except Exception:
        return False


def available() -> bool:
    """True only once an import has actually succeeded."""
    with _probe_lock:
        settled = _probe
    if settled is None:
        start_probe()
        return False
    return settled[0]


def why_not() -> str:
    with _probe_lock:
        settled = _probe
    if settled is None:
        return "still checking whether this machine can drive an audio device"
    if settled[0]:
        return ""
    if not bindings_present():
        return ("the sounddevice package isn't installed, so sound has to go "
                "through a separate ffplay with no clock joining it to the "
                "picture (pip install sounddevice)")
    return settled[1]


class AudioTrack:
    """One clip's sound, playing from `start` seconds, with a clock.

    Everything here runs on its own threads. The UI thread only ever calls
    the small methods at the bottom, and none of them wait on anything.
    """

    # If the device has produced nothing in this long, stop believing in
    # it and let the caller fall back to pacing off the wall clock, so a
    # silent machine cannot freeze the picture.
    START_TIMEOUT = 2.0

    def __init__(self, path: str, start: float, speed: float = 1.0,
                 gain: float = 0.8, on_fail=None):
        self.path = path
        self.start = max(float(start), 0.0)
        self.speed = float(speed) or 1.0
        self.gain = max(0.0, min(float(gain), 1.0))
        self.on_fail = on_fail

        self._proc = None
        self._stream = None
        self._np = None
        self._written = 0            # audio frames handed to the device
        self._latency = 0.0          # seconds of them not yet heard
        self._started_at = None      # when the first block went out
        self._failed = False
        self._ended = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._opened = time.monotonic()

        threading.Thread(target=self._run, daemon=True).start()

    # ── the worker ──────────────────────────────────────────────────────

    def _run(self) -> None:
        # Already loaded by the startup probe, or we do not play. Nothing
        # is imported from here - see the note on _run_probe.
        np, sd = _np, _sd
        if np is None or sd is None:
            return self._give_up("the audio packages are not loaded")
        self._np = np

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if self.start > 0.01:
            # Before -i, so ffmpeg seeks rather than decodes and discards.
            cmd += ["-ss", f"{self.start:.3f}"]
        cmd += ["-i", self.path, "-vn", "-sn", "-dn"]
        if abs(self.speed - 1.0) > 0.01:
            cmd += ["-af", _atempo(self.speed)]
        cmd += ["-f", "f32le", "-acodec", "pcm_f32le",
                "-ac", str(CHANNELS), "-ar", str(RATE), "pipe:1"]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, bufsize=0, creationflags=NO_WINDOW)
        except OSError:
            return self._give_up("ffmpeg not found")

        try:
            self._stream = sd.OutputStream(
                samplerate=RATE, channels=CHANNELS, dtype=DTYPE,
                blocksize=BLOCK)
            self._stream.start()
            self._latency = float(self._stream.latency or 0.0)
        except Exception as exc:
            return self._give_up(f"no audio device ({exc.__class__.__name__})")

        want = BLOCK * BYTES_PER_FRAME
        try:
            while not self._stop.is_set():
                chunk = self._proc.stdout.read(want)
                if not chunk:
                    break
                block = self._np.frombuffer(chunk, dtype=self._np.float32)
                if block.size % CHANNELS:
                    block = block[:block.size - (block.size % CHANNELS)]
                block = block.reshape(-1, CHANNELS)
                # Silence is written, never skipped: muting must not stop
                # the clock, or the picture would freeze with it. The
                # multiply also gives the device a writable array of its
                # own rather than a read-only view onto the pipe buffer.
                block = block * self.gain
                if self._stop.is_set():
                    break
                self._stream.write(block)
                with self._lock:
                    self._written += block.shape[0]
                    if self._started_at is None:
                        self._started_at = time.monotonic()
        except Exception:
            pass
        finally:
            if self._written == 0:
                # ffmpeg gave us nothing and exited. Almost always a clip
                # with no audio track - plenty of the converted library
                # has none - and occasionally a codec it cannot open.
                # Either way say so now: leaving it to the start timeout
                # would hold the first frame for two seconds on every
                # silent clip before the picture gave up waiting.
                self._failed = True
            self._ended = True
            self._shutdown()

    # Only ever called on the feeder thread - see stop().
    def _give_up(self, why: str) -> None:
        self._failed = True
        if self.on_fail:
            self.on_fail(why)
        self._shutdown()

    def _shutdown(self) -> None:
        stream, self._stream = self._stream, None
        proc, self._proc = self._proc, None
        if stream is not None:
            try:
                stream.abort()
                stream.close()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
            # Collected on a thread nobody is waiting on; the pipe is left
            # alone because a read may still be in flight on it.
            threading.Thread(target=_collect, args=(proc,), daemon=True).start()

    # ── what the player asks ────────────────────────────────────────────

    def position(self) -> float | None:
        """Where in the clip the speakers are, in source seconds, or None
        if sound is not flowing (yet, or at all) and the caller should
        pace itself some other way."""
        if self._failed:
            return None
        with self._lock:
            written, started = self._written, self._started_at
        if started is None:
            if time.monotonic() - self._opened > self.START_TIMEOUT:
                self._failed = True
            return None
        # Written minus what is still sitting in the device's buffer. In
        # steady state the buffer is full, because write() blocks until
        # there is room for the next block - so the reported latency is a
        # good measure of the audio we have handed over but not yet heard.
        heard = (written / RATE) - self._latency
        if heard < 0.0:
            heard = 0.0
        return self.start + heard * self.speed

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def ended(self) -> bool:
        """The sound ran out. The picture may still have frames to show
        (a clip whose audio is shorter than its video), so from here the
        caller should pace itself off the wall clock again."""
        return self._ended

    @property
    def running(self) -> bool:
        return not self._failed and not self._stop.is_set()

    def set_gain(self, gain: float) -> None:
        """Takes effect on the next block - about 20 ms. No restart, so
        changing the volume or muting cannot knock the clip out of sync."""
        self.gain = max(0.0, min(float(gain), 1.0))

    def stop(self) -> None:
        """Ask it to stop and return at once.

        The stream is closed by the feeder thread, never from here.
        PortAudio will not survive having a stream aborted underneath a
        write() that is still in flight on another thread - it corrupts
        the heap and takes the process with it. Killing ffmpeg gives the
        feeder a clean EOF, and it tears the stream down itself on the way
        out, inside a block time.
        """
        # Silent from this instant. The feeder needs a block or two to
        # notice and get out, and a burst of seeks would otherwise have
        # two or three dying tracks still writing to the device at once -
        # audible as a smear of overlapping sound.
        self.gain = 0.0
        self._stop.set()
        proc = self._proc
        if proc is not None:
            _quietly(proc.terminate)


def _atempo(speed: float) -> str:
    """atempo only spans 0.5-2.0, so anything outside that is a chain."""
    speed = max(0.25, min(float(speed), 4.0))
    parts = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={speed:.4f}")
    return ",".join(parts)


def _collect(proc) -> None:
    """See a terminated process all the way out. Without the kill-then-wait
    a decoder that ignores terminate stays a zombie for the life of the
    program, one per clip."""
    try:
        proc.wait(timeout=3)
        return
    except Exception:
        pass
    _quietly(proc.kill)
    _quietly(proc.wait, 3)


def _quietly(fn, *a) -> None:
    try:
        fn(*a)
    except Exception:
        pass
