"""Playback through libmpv, loaded into this process and drawing into one
of our own Tk widgets.

The player the app reaches for first. On this library it was measured
against the VLC backend it replaced, clip for clip: a new clip's first
frame on screen in a median 90ms against VLC's 300-550 (and over a second
off a cold drive), GPU decode on everything including AV1 4K60, a tenth to
half a percent of the CPU, and no dropped frames at 4K60. Switching clips
is a loadfile "replace": the video output stays up and only the file
changes, where VLC tore its window down and built a new one every time.

Not to be confused with mpv_player, which runs mpv.exe beside us and
talks to it over a named pipe - the arrangement that was fragile on
Windows. This is the library, in-process, as libVLC is: no pipe that can
fail to open, no process to outlive us.

Same rule as the VLC backend: the UI thread never calls libmpv. It puts a
command on a queue and returns, and one worker thread makes every call.
mpv draws into a child window of ours, and a call that waits on mpv while
mpv waits on our window's thread is a deadlock - one was seen with libVLC
from exactly that. Here it cannot happen, because the UI thread never
waits on anything.

Needs libmpv-2.dll (a Windows build of mpv's library) and the python-mpv
package. The DLL is looked for in %LOCALAPPDATA%\\PAZ Suite\\mpv, then in
an "mpv" folder beside the app, then wherever PAZ_LIBMPV_DIR points.
"""

from __future__ import annotations

import importlib.util
import os
import queue
import threading
import time

IS_WINDOWS = os.name == "nt"
DLL_NAMES = ("libmpv-2.dll", "mpv-2.dll") if IS_WINDOWS else ()


# ── is it here ──────────────────────────────────────────────────────────

def _dll_dirs() -> list:
    dirs = []
    if os.environ.get("PAZ_LIBMPV_DIR"):
        dirs.append(os.environ["PAZ_LIBMPV_DIR"])
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        dirs.append(os.path.join(local, "PAZ Suite", "mpv"))
    dirs.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mpv"))
    return dirs


def library_dir() -> str:
    """The folder holding libmpv, or "" when it isn't installed."""
    if not IS_WINDOWS:
        from ctypes.util import find_library
        return "system" if find_library("mpv") else ""
    for folder in _dll_dirs():
        if any(os.path.isfile(os.path.join(folder, n)) for n in DLL_NAMES):
            return folder
    return ""


def bindings_present() -> bool:
    try:
        return importlib.util.find_spec("mpv") is not None
    except Exception:
        return False


# Loaded once, at startup, on a thread of its own - importing the bindings
# is what loads the DLL (120MB, with all of ffmpeg in it), and that is not
# something to do on the thread drawing the window, or on a worker at the
# moment someone presses Play. See vlc_player for the same arrangement.
_lock = threading.Lock()
_module = None
_error = ""
_started = False
_loaded = False


def _load() -> None:
    global _module, _error, _loaded
    folder = library_dir()
    try:
        if IS_WINDOWS and folder:
            os.add_dll_directory(folder)
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
        import mpv
    except Exception as exc:
        with _lock:
            _error = f"{exc.__class__.__name__}: {exc}"
            _loaded = True
        return
    with _lock:
        _module = mpv
        _loaded = True


def start_load() -> None:
    """Begin loading libmpv in the background. Called once, at startup."""
    global _started
    if not (bindings_present() and library_dir()):
        return
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_load, daemon=True, name="libmpv-load").start()


def module():
    with _lock:
        return _module


def available() -> bool:
    """Installed, and not known to be broken. Still loading counts as yes:
    the player's own worker waits for the load (see _boot), so a Library
    built in the first moments after launch does not settle for VLC for
    the whole session just because the DLL was a second from ready."""
    if not (bindings_present() and library_dir()):
        return False
    with _lock:
        if _loaded:
            return _module is not None
    start_load()
    return True


def wait_loaded(timeout: float) -> bool:
    """Block until the load has finished, either way. Worker threads only."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with _lock:
            if _loaded:
                return _module is not None
        time.sleep(0.02)
    return False


def why_not() -> str:
    if not library_dir() and not bindings_present():
        return "libmpv-2.dll and the python-mpv package are not installed"
    if not library_dir():
        return ("libmpv-2.dll was not found - put it in "
                "%LOCALAPPDATA%\\PAZ Suite\\mpv")
    if not bindings_present():
        return "the python-mpv package is not installed (pip install mpv)"
    with _lock:
        if _error:
            return f"libmpv will not load - {_error}"
        if _module is None:
            return "still loading libmpv"
    return ""


# How mpv is set up. Each line is a decision, not a default.
OPTIONS = {
    # Draw with the GPU through Direct3D 11, decode on the GPU where the
    # file allows, and fall back to the CPU quietly where it does not.
    "vo": "gpu-next",
    "gpu_api": "d3d11" if IS_WINDOWS else "auto",
    "hwdec": "auto-safe",
    # Pace frames to the display rather than to the audio clock alone -
    # the setting that makes 60fps on a 60Hz screen free of judder.
    "video_sync": "display-resample",
    # Hold the last frame at the end instead of closing the window, so a
    # clip that is not looping ends on its picture, not on black.
    "keep_open": "always",
    # The app owns the keyboard and draws its own controls.
    "osc": False,
    "input_default_bindings": False,
    "input_vo_keyboard": False,
    "input_cursor": False,
    "osd_level": 0,
    "cursor_autohide": False,
    # Exact seeks, so frame stepping and the scrub land where asked.
    "hr_seek": "yes",
    # Read well ahead of the picture. The library drive is a busy SMR
    # disk; a big buffer turns its pauses into nothing visible.
    "cache": "yes",
    "demuxer_max_bytes": "200MiB",
    "demuxer_readahead_secs": 10,
    # Nothing beside the clip to go looking for on a 3.5TB drive.
    "sub_auto": "no",
    "audio_file_auto": "no",
    "ytdl": False,
    "terminal": False,
    # None of mpv's own scripts: the app draws its controls and owns the
    # keys. Each one is a Lua state started for nothing, and LuaJIT's
    # internal error signal (0xe24c4a02, handled inside mpv) was landing
    # in the freeze log dozens of times a session as a "fatal exception".
    "load_scripts": False,
    "load_stats_overlay": False,
    "load_console": False,
    "load_auto_profiles": False,
    "load_select": False,
    "load_commands": False,
    "load_positioning": False,
    "load_context_menu": False,
    "load_osd_console": False,
}


class LibMpvPlayer:
    """Same surface as ClipPlayer and VlcPlayer, backed by libmpv."""

    START_TIMEOUT = 15.0
    STALL_AFTER = 4.0
    POLL = 0.02

    def __init__(self, widget, width: int, height: int,
                 on_tick=None, on_state=None, on_fail=None, on_eof=None,
                 post=None, background: str = "#000000"):
        self.widget = widget
        # What mpv paints around a clip narrower than its box. Set to the
        # card's colour, the box can stay one size for every clip - see
        # InlinePlayer.letterboxes - and still look clip-shaped.
        self.background = background
        self.view_w = max(int(width), 240)
        self.view_h = max(int(height), 135)
        self.on_tick = on_tick
        self.on_state = on_state
        self.on_fail = on_fail
        self.on_eof = on_eof
        # Everything that touches Tk goes through here - the callbacks
        # below are raised on mpv's and our worker's threads.
        self._post = post or (lambda fn, *a: None)
        self.on_unavailable = None

        self.path = ""
        self.duration = 0.0
        self.fps = 30.0
        self.playing = False
        self.position = 0.0
        self.loop = True
        self.volume = 80
        self.muted = False
        self.speed = 1.0

        self._q: queue.Queue = queue.Queue()
        self._mpv = None
        self._failed = False
        self._alive = False
        # mpv's clock, as last reported, and when it last moved.
        self._mpv_time = 0.0
        self._moved_at = 0.0
        self._played_at = None
        self._last_told = -1.0
        self._eof = False
        self._seek_target = None
        self._seek_at = 0.0
        # mpv opens a file asynchronously and drops a seek that arrives
        # before it has - so a seek asked for in that window waits here
        # and is made the moment the file is in. See _apply and _tick.
        self._file_loaded = False
        self._held_seek = None
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="libmpv")
        self._worker.start()

    # ── the worker owns every libmpv call ───────────────────────────────

    def _run(self) -> None:
        if not self._boot():
            return
        while True:
            try:
                command = self._q.get(timeout=self.POLL)
            except queue.Empty:
                command = None
            if command is not None:
                if command[0] == "quit":
                    break
                # Only the newest load matters: holding Down queues one per
                # clip passed, and mpv would open each before the next.
                if command[0] == "load":
                    command = self._newest_load(command)
                # Likewise a drag along a timeline: a seek per mouse move,
                # and only where the pointer is now is worth showing.
                elif command[0] == "seek":
                    command = self._newest_seek(command)
                try:
                    self._apply(command)
                except Exception:
                    # One bad command must not leave the player deaf.
                    pass
            self._tick()
        self._teardown()

    def _newest_load(self, command):
        """Skip straight to the last load waiting in the queue, keeping any
        other command that came after it."""
        rest = []
        while True:
            try:
                nxt = self._q.get_nowait()
            except queue.Empty:
                break
            if nxt[0] == "load":
                command, rest = nxt, []
            else:
                rest.append(nxt)
        for item in rest:
            self._q.put(item)
        return command

    def _newest_seek(self, command):
        """Skip a run of seeks to the last one. Stops at anything else,
        which must still happen in order."""
        while True:
            try:
                nxt = self._q.queue[0]
            except IndexError:
                return command
            if nxt[0] != "seek":
                return command
            command = self._q.get_nowait()

    def _boot(self) -> bool:
        start_load()
        wait_loaded(self.START_TIMEOUT)
        mpv = module()
        if mpv is None:
            self._failed = True
            self._report_unavailable("libmpv has not loaded. " + why_not())
            return False
        try:
            options = dict(OPTIONS)
            options["wid"] = str(self.widget.winfo_id())
            options["mute"] = bool(self.muted)
            options["volume"] = int(self.volume)
            options["loop_file"] = "inf" if self.loop else "no"
            options["background"] = "color"
            options["background_color"] = self.background
            player = mpv.MPV(**options)
            # Observers run on python-mpv's event thread and only store a
            # number; the worker reads it.
            player.observe_property("time-pos", self._seen_time)
            player.observe_property("duration", self._seen_duration)
            player.observe_property("eof-reached", self._seen_eof)
            player.event_callback("end-file")(self._seen_end)
            player.event_callback("file-loaded")(self._seen_loaded)
            self._mpv = player
        except Exception as exc:
            self._failed = True
            self._report_unavailable(
                f"libmpv wouldn't start ({exc.__class__.__name__}: {exc}).")
            return False
        self._alive = True
        return True

    def _teardown(self) -> None:
        self._alive = False
        player, self._mpv = self._mpv, None
        if player is not None:
            try:
                player.terminate()
            except Exception:
                pass

    def _report_unavailable(self, message: str) -> None:
        if self.on_unavailable:
            self._post(self.on_unavailable, message)
        elif self.on_fail:
            self._post(self.on_fail, message)

    # ── what mpv tells us (its event thread) ────────────────────────────

    def _seen_time(self, _name, value) -> None:
        if value is not None:
            if value != self._mpv_time:
                self._moved_at = time.monotonic()
            self._mpv_time = float(value)

    def _seen_duration(self, _name, value) -> None:
        if value:
            self.duration = float(value)

    def _seen_eof(self, _name, value) -> None:
        if value and not self.loop and self.playing:
            self._eof = True

    def _seen_loaded(self, _event) -> None:
        self._file_loaded = True

    def _seen_end(self, event) -> None:
        try:
            reason = event.data.reason
            error = getattr(event.data, "error", 0)
        except Exception:
            return
        # 4 is MPV_END_FILE_REASON_ERROR - a file mpv could not play.
        if int(reason) == 4 and self.on_fail:
            name = os.path.basename(self.path)
            self._post(self.on_fail, f"mpv couldn't play {name} (error {error}).")

    # ── commands, as run on the worker ──────────────────────────────────

    def _apply(self, command) -> None:
        name = command[0]
        player = self._mpv
        if player is None:
            return
        if name == "load":
            path, start, play = command[1:]
            # Paused or not is set on the player now, not passed with the
            # file. A per-file pause is applied when mpv gets round to
            # opening the file - after a Play queued straight behind this
            # load had already un-paused it - so holding Down ended on a
            # clip sitting paused on black. Set here, a later Play simply
            # wins, however the timing falls.
            player.pause = not play
            options = {}
            if start > 0.05:
                options["start"] = f"{start:.3f}"
            self._eof = False
            self._mpv_time = start
            self._moved_at = time.monotonic()
            self._file_loaded = False
            self._held_seek = None
            player.loadfile(path, "replace", **options)
            self._played_at = time.monotonic() if play else None
        elif name == "play":
            if self._eof or player.idle_active:
                # Ran off the end without looping, or nothing loaded: start
                # the clip again from where the playhead is.
                self._eof = False
                start = self.position if self.position < self.duration - 0.1 else 0.0
                self._file_loaded = False
                player.loadfile(self.path, "replace", pause="no", start=f"{start:.3f}")
            else:
                player.pause = False
            self._played_at = time.monotonic()
        elif name == "pause":
            player.pause = True
            self._played_at = None
        elif name == "stop":
            # Paused, not unloaded. A stop is nearly always followed by a
            # load, and unloading would take the video output down only to
            # build it again - the very cost this backend exists to avoid.
            player.pause = True
            self._played_at = None
        elif name == "clear":
            player.command("stop")
            self._played_at = None
        elif name == "seek":
            seconds, = command[1:]
            if not self._file_loaded:
                self._held_seek = seconds
                return
            player.command("seek", f"{max(seconds, 0.0):.3f}", "absolute+exact")
            self._played_at = time.monotonic() if self.playing else None
        elif name == "step":
            frames, = command[1:]
            if frames == 1:
                player.command("frame-step")
            elif frames == -1:
                player.command("frame-back-step")
            else:
                fps = max(float(self.fps or 30.0), 1.0)
                player.command("seek", f"{frames / fps:.4f}", "relative+exact")
        elif name == "volume":
            player.volume = int(command[1])
        elif name == "mute":
            player.mute = bool(command[1])
        elif name == "rate":
            player.speed = float(command[1])
        elif name == "loop":
            player.loop_file = "inf" if command[1] else "no"

    def _tick(self) -> None:
        if self._held_seek is not None and self._file_loaded and self._mpv is not None:
            seconds, self._held_seek = self._held_seek, None
            try:
                self._mpv.command("seek", f"{max(seconds, 0.0):.3f}", "absolute+exact")
            except Exception:
                pass
        if self._eof:
            self._eof = False
            self.playing = False
            self._played_at = None
            if self.on_state:
                self._post(self.on_state, False)
            if self.on_eof:
                self._post(self.on_eof)
        if self._seeking():
            return
        self.position = self._mpv_time
        if self.duration:
            self.position = min(self.position, self.duration)
        if self.on_tick and abs(self.position - self._last_told) >= 0.015:
            self._last_told = self.position
            self._post(self.on_tick, self.position)

    def _seeking(self) -> bool:
        """Between asking for a seek and mpv arriving there, its clock
        still reads the old place; showing that would bounce the
        playhead back for a moment after every click on the bar."""
        target = self._seek_target
        if target is None:
            return False
        if abs(self._mpv_time - target) < 0.25 or time.monotonic() - self._seek_at > 1.5:
            self._seek_target = None
            return False
        return True

    def _put(self, *command) -> None:
        if not self._failed:
            self._q.put(command)

    # ── the surface the players talk to ─────────────────────────────────

    def load(self, path: str, duration: float, fps: float, play: bool = False) -> None:
        """Open `path`. Opened paused it holds its first frame, which is
        the still - there is no thumbnail to draw over it."""
        self.path = path
        self.duration = float(duration or 0.0)
        self.fps = float(fps or 30.0)
        self.position = 0.0
        self._mpv_time = 0.0
        self._seek_target = None
        self.playing = bool(play)
        self._put("load", path, 0.0, bool(play))
        if play and self.on_state:
            self.on_state(True)

    def clear(self) -> None:
        self.path = ""
        self.duration = 0.0
        self.position = 0.0
        self.playing = False
        self._put("clear")

    def play(self) -> None:
        if not self.path:
            return
        self.playing = True
        if self.on_state:
            self.on_state(True)
        self._put("play")

    def pause(self) -> None:
        self.playing = False
        if self.on_state:
            self.on_state(False)
        self._put("pause")

    def toggle(self) -> None:
        self.pause() if self.playing else self.play()

    def stop(self) -> None:
        self.playing = False
        self._put("stop")

    def seek(self, seconds: float) -> None:
        if not self.path:
            return
        limit = max(self.duration - 0.05, 0.0) if self.duration else seconds
        self.position = max(0.0, min(seconds, limit))
        self._seek_target = self.position
        self._seek_at = time.monotonic()
        self._put("seek", self.position)

    def nudge(self, seconds: float) -> None:
        if self.path:
            self.seek(self.position + seconds)

    def step(self, frames: int = 1) -> None:
        if self.path and frames:
            self.playing = False
            self._put("step", int(frames))

    def toggle_loop(self) -> bool:
        self.loop = not self.loop
        self._put("loop", self.loop)
        return self.loop

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        self._put("mute", self.muted)
        return self.muted

    def set_volume(self, value: int) -> None:
        self.volume = max(0, min(int(value), 100))
        if self.muted and self.volume > 0:
            self.muted = False
            self._put("mute", False)
        self._put("volume", self.volume)

    def set_speed(self, value: float) -> None:
        self.speed = float(value)
        self._put("rate", self.speed)

    def set_size(self, width: int, height: int = 0) -> None:
        # mpv scales to its window; the widget resizing is the message.
        self.view_w = max(int(width), 240)
        if height:
            self.view_h = max(int(height), 135)

    def check_progress(self) -> str:
        """"" while the clock moves, or why it isn't."""
        if not self.playing or self._played_at is None:
            return ""
        now = time.monotonic()
        if now - self._played_at < self.STALL_AFTER:
            return ""
        if now - self._moved_at < self.STALL_AFTER:
            return ""
        return ("mpv has the clip but the picture isn't moving. "
                "Settings > Player can switch players.")

    def shutdown(self) -> None:
        self._failed = True
        self._q.put(("quit",))
