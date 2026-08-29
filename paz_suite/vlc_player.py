"""Playback delegated to libVLC, loaded in-process and pointed at one of
our own Tk widgets.

Why this exists next to the mpv backend: mpv runs as a separate process and
the two of us talk over a socket - a named pipe on Windows. That handshake
is the fragile part. If the pipe never opens there is nothing to talk to,
and every command goes nowhere. libVLC is a library. We load the DLL, hand
it a window handle, and call functions. There is no channel between us that
can fail to open, no process to outlive us, and no console window.

The whole VLC side lives on one worker thread. The UI thread never calls
libVLC - it puts a command on a queue and returns. That is deliberate, not
an optimisation: it means nothing libVLC does, however slow (scanning its
plugin folder on first load, opening a 4K file off a drive that has spun
down, tearing down a hardware decoder), can stall the window. The player
can fail; the app cannot freeze.
"""

from __future__ import annotations

import importlib.util
import os
import queue
import threading
import time

IS_WINDOWS = os.name == "nt"


# ── is it here at all ───────────────────────────────────────────────────
#
# Answered without importing `vlc`, because importing it loads libvlc.dll
# and scans the plugin folder, and this question gets asked on the UI
# thread while the window is being built.

def _windows_vlc_dirs() -> list:
    dirs = []
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(var, "")
        if root:
            dirs.append(os.path.join(root, "VideoLAN", "VLC"))
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\VideoLAN\VLC") as key:
                    path, _ = winreg.QueryValueEx(key, "InstallDir")
                    if path:
                        dirs.insert(0, path)
            except OSError:
                continue
    except Exception:
        pass
    return dirs


def library_path() -> str:
    """Where libVLC lives, or "" if it isn't installed."""
    if IS_WINDOWS:
        for folder in _windows_vlc_dirs():
            dll = os.path.join(folder, "libvlc.dll")
            if os.path.isfile(dll):
                return dll
        return ""
    from ctypes.util import find_library
    return find_library("vlc") or ""


def bindings_present() -> bool:
    """The `vlc` module, without importing it."""
    try:
        return importlib.util.find_spec("vlc") is not None
    except Exception:
        return False


def available() -> bool:
    return bindings_present() and bool(library_path())


def why_not() -> str:
    """Plain-language reason VLC can't be used, for the diagnostics."""
    if not bindings_present() and not library_path():
        return "VLC isn't installed, and neither is the python-vlc package"
    if not bindings_present():
        return "VLC is installed but the python-vlc package isn't (pip install python-vlc)"
    if not library_path():
        return "python-vlc is installed but VLC itself isn't - get it from videolan.org"
    return ""


class VlcPlayer:
    """Same surface as ClipPlayer and MpvPlayer, backed by libVLC."""

    # How long to give libVLC to come up before deciding it won't. Nothing
    # waits on this from the UI thread, so it is patience, not a freeze.
    START_TIMEOUT = 15.0
    # A clock that hasn't moved this long while we believe we are playing
    # means the picture is stuck, whatever libVLC thinks.
    STALL_AFTER = 4.0
    POLL = 0.05

    def __init__(self, widget, width: int, height: int,
                 on_tick=None, on_state=None, on_fail=None, on_eof=None,
                 post=None):
        self.widget = widget
        self.view_w = max(int(width), 240)
        self.view_h = max(int(height), 135)
        self.on_tick = on_tick
        self.on_state = on_state
        self.on_fail = on_fail
        self.on_eof = on_eof
        # Everything that touches Tk goes through here, because these
        # callbacks are raised on the worker thread. Tk from the wrong
        # thread is a crash, not a bug.
        self._post = post or (lambda fn, *a: None)
        # Set by whoever builds this, so a failure can go back to another
        # engine instead of leaving a dead panel.
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
        self._vlc = None            # the module
        self._instance = None
        self._player = None
        self._alive = False
        self._failed = False
        self._stopping = False
        self._played_from = 0.0
        self._played_at = None
        self._last_told = -1.0
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # ── the worker owns every libVLC call ───────────────────────────────

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
                try:
                    self._apply(command)
                except Exception:
                    # One bad command must not take the thread down with
                    # it - the player would go deaf for the rest of the
                    # session with no sign of why.
                    pass
            self._tick()
        self._teardown()

    def _boot(self) -> bool:
        try:
            import vlc
            self._vlc = vlc
            # --no-video-title-show: no filename splashed over the picture.
            # --quiet: VLC's own logging is not ours to print.
            # --no-sub-autodetect-file: don't go looking beside a 4K file
            # on a 3.5 TB drive for subtitles that were never there.
            args = ["--no-video-title-show", "--quiet",
                    "--no-sub-autodetect-file", "--no-snapshot-preview"]
            if not IS_WINDOWS:
                # On X11 this is not optional. Without it libVLC calls Xlib
                # from its own threads, and Tk is already using Xlib from
                # ours - two threads in an Xlib connection that was never
                # made thread-safe. It does not fail cleanly: the app's
                # window disappears off the display while the process
                # carries on running. --no-xlib keeps VLC on xcb, which
                # opens its own connection and leaves ours alone.
                args.append("--no-xlib")
            if os.environ.get("PAZ_VLC_ARGS"):
                args += os.environ["PAZ_VLC_ARGS"].split()
            self._instance = vlc.Instance(*args)
            if self._instance is None:
                raise RuntimeError("libVLC refused to start")
            self._player = self._instance.media_player_new()
            handle = self.widget.winfo_id()
            if IS_WINDOWS:
                self._player.set_hwnd(handle)
            elif os.uname().sysname == "Darwin":
                self._player.set_nsobject(handle)
            else:
                self._player.set_xwindow(handle)
            self._player.audio_set_volume(0 if self.muted else self.volume)
            manager = self._player.event_manager()
            # The callback runs on a VLC thread and must not call back into
            # libVLC, so it does the one safe thing: leaves a note.
            manager.event_attach(vlc.EventType.MediaPlayerEndReached,
                                 lambda _e: self._q.put(("ended",)))
        except Exception as exc:
            self._failed = True
            self._report_unavailable(
                f"VLC wouldn't start ({exc.__class__.__name__}: {exc}).")
            return False
        self._alive = True
        return True

    def _teardown(self) -> None:
        self._alive = False
        player, self._player = self._player, None
        instance, self._instance = self._instance, None
        for obj, method in ((player, "stop"), (player, "release"),
                            (instance, "release")):
            if obj is None:
                continue
            try:
                getattr(obj, method)()
            except Exception:
                pass

    def _report_unavailable(self, message: str) -> None:
        if self.on_unavailable:
            self._post(self.on_unavailable, message)
        elif self.on_fail:
            self._post(self.on_fail, message)

    # ── commands, as run on the worker ──────────────────────────────────

    def _set_media(self, path: str) -> None:
        """Point the player at `path`.

        The release has to happen after set_media, not before: set_media
        takes its own reference, and dropping ours first frees the object
        we are about to hand over. That is a segfault, not a leak."""
        media = self._instance.media_new_path(path)
        # Modest read-ahead: this is local disk, and a big cache only
        # makes the first frame later.
        media.add_option("file-caching=300")
        self._player.set_media(media)
        media.release()

    def _apply(self, command) -> None:
        name = command[0]
        player = self._player
        if player is None:
            return
        if name == "load":
            path, = command[1:]
            # Hand it the clip but do not start it. Opening a 4K file is
            # real work, and the panel already has a thumbnail to show -
            # there is nothing to gain by paying for a frame we would
            # cover up the moment Play is pressed.
            self._set_media(path)
        elif name == "play":
            spent = player.get_state() in (self._vlc.State.Ended,
                                           self._vlc.State.Stopped,
                                           self._vlc.State.NothingSpecial)
            if spent:
                # Nothing left to un-pause: hand it the clip again.
                if not self.path:
                    return
                self._set_media(self.path)
                player.play()
                if self.position > 0.05:
                    player.set_time(int(self.position * 1000))
            else:
                player.set_pause(0)
            self._played_from = self.position
            self._played_at = time.monotonic()
        elif name == "pause":
            player.set_pause(1)
            self._played_at = None
        elif name == "stop":
            player.stop()
            self._played_at = None
        elif name == "seek":
            seconds, = command[1:]
            player.set_time(int(max(seconds, 0.0) * 1000))
            self._played_from = max(seconds, 0.0)
            self._played_at = time.monotonic() if self.playing else None
        elif name == "volume":
            player.audio_set_volume(int(command[1]))
        elif name == "mute":
            player.audio_set_mute(bool(command[1]))
            if not command[1]:
                player.audio_set_volume(int(self.volume))
        elif name == "rate":
            player.set_rate(float(command[1]))
        elif name == "ended":
            self._ended()

    def _ended(self) -> None:
        player = self._player
        if player is None:
            return
        if self.loop:
            # A media player that has reached its end will not seek, so
            # looping means handing it the clip again.
            self._set_media(self.path)
            player.play()
            self.position = 0.0
            self._played_from = 0.0
            self._played_at = time.monotonic()
        else:
            self.playing = False
            self._played_at = None
            if self.on_state:
                self._post(self.on_state, False)
            if self.on_eof:
                self._post(self.on_eof)

    def _tick(self) -> None:
        player = self._player
        if player is None:
            return
        try:
            millis = player.get_time()
        except Exception:
            return
        if millis is None or millis < 0:
            return
        self.position = millis / 1000.0
        if not self.duration:
            try:
                length = player.get_length()
            except Exception:
                length = 0
            if length and length > 0:
                self.duration = length / 1000.0
        # 20 polls a second keeps the stall check honest, but the panel
        # does not need redrawing that often.
        if self.on_tick and abs(self.position - self._last_told) >= 0.08:
            self._last_told = self.position
            self._post(self.on_tick, self.position)

    def _put(self, *command) -> None:
        if not self._failed:
            self._q.put(command)

    # ── the surface the inspector talks to ──────────────────────────────

    def load(self, path: str, duration: float, fps: float) -> None:
        self.path = path
        self.duration = float(duration or 0.0)
        self.fps = float(fps or 30.0)
        self.position = 0.0
        self.playing = False
        self._played_at = None
        self._put("load", path)

    def clear(self) -> None:
        self.path = ""
        self.duration = 0.0
        self.position = 0.0
        self.playing = False
        self._played_at = None
        self._put("stop")

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
        self._played_at = None
        self._put("stop")

    def seek(self, seconds: float) -> None:
        if not self.path:
            return
        limit = max(self.duration - 0.1, 0.0) if self.duration else seconds
        self.position = max(0.0, min(seconds, limit))
        self._put("seek", self.position)

    def nudge(self, seconds: float) -> None:
        if self.path:
            self.seek(self.position + seconds)

    def toggle_loop(self) -> bool:
        self.loop = not self.loop
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
        # libVLC scales to whatever the window is, so there is nothing to
        # tell it - the widget resizing is the whole message.
        self.view_w = max(int(width), 240)
        if height:
            self.view_h = max(int(height), 135)

    def check_progress(self) -> str:
        """"" while the clock is moving, or a sentence about why it isn't."""
        if not self.playing or self._played_at is None:
            return ""
        waited = time.monotonic() - self._played_at
        if waited < self.STALL_AFTER:
            return ""
        if self.position - self._played_from > 0.20:
            return ""
        return ("VLC has the clip but the picture isn't moving. "
                "Settings > e621 & App > Playback can switch players.")

    def shutdown(self) -> None:
        self._failed = True
        self._q.put(("quit",))
