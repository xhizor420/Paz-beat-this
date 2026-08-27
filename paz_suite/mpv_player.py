"""Playback delegated to mpv, embedded in one of our own Tk widgets.

The hand-rolled player next door decodes with ffmpeg, pushes raw RGB
through a pipe, and plays the sound with a *second*, independent ffplay
process. That last part is the problem, and it is not a tuning problem:
two processes started a moment apart, with no clock between them, cannot
be made to agree. Sound leads picture by however long ffplay took to
open, for the whole clip, and nothing in the design can notice or correct
it. Add the cost of moving uncompressed 4K frames through a pipe into
Python and into a PhotoImage, and 60fps is a fight even when it works.

mpv already solves all of it, properly: one process demuxes the file,
decodes both streams, drives video off the audio clock, uses whatever
hardware decoder the machine has, and seeks in about a tenth of a
millisecond. It will render into a window we own - pass it the Tk
widget's id - so it can sit inside the inspector exactly where our own
canvas sat.

This class deliberately mirrors ClipPlayer's interface method for method,
so InlinePlayer can hold either one and never ask which. When mpv is not
installed, `available()` is False and the old engine is used instead;
nothing here is required for the app to run.

Talking to it: mpv exposes a JSON IPC channel - a Unix socket, or a named
pipe on Windows. Commands go out as one JSON object per line, events and
property changes come back the same way on a reader thread.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid

from .files import NO_WINDOW

# Properties worth hearing about the moment they change, rather than
# polling for. time-pos drives the seek bar, so it is asked for at a
# readable rate rather than every frame.
_OBSERVED = (("time-pos", 1), ("pause", 2), ("eof-reached", 3),
             ("duration", 4))

IS_WINDOWS = os.name == "nt"


def mpv_path() -> str:
    """Where mpv is, or "".

    Beside the app first, so dropping mpv.exe next to it works without
    touching PATH, then PATH, then the handful of places Windows package
    managers put it. `winget` in particular installs to a versioned
    folder under Program Files that never reaches PATH.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    beside = (os.path.join(here, "mpv", "mpv.exe"),
              os.path.join(here, "mpv", "mpv"),
              os.path.join(here, "mpv.exe"),
              os.path.join(here, "mpv"))
    for candidate in beside:
        if os.path.isfile(candidate):
            return candidate

    found = shutil.which("mpv")
    if found:
        return found

    if IS_WINDOWS:
        roots = [os.environ.get("LOCALAPPDATA", ""),
                 os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                 os.environ.get("PROGRAMFILES(X86)", ""),
                 os.path.join(os.environ.get("USERPROFILE", ""), "scoop", "apps")]
        for root in filter(None, roots):
            for sub in ("mpv", "mpv.net", os.path.join("Microsoft", "WinGet", "Packages")):
                base = os.path.join(root, sub)
                if not os.path.isdir(base):
                    continue
                direct = os.path.join(base, "mpv.exe")
                if os.path.isfile(direct):
                    return direct
                # winget nests one versioned folder deep; don't walk the
                # whole tree, just look one level down.
                try:
                    for name in os.listdir(base):
                        nested = os.path.join(base, name, "mpv.exe")
                        if os.path.isfile(nested):
                            return nested
                except OSError:
                    pass
    return ""


def available() -> bool:
    return bool(mpv_path())


class MpvPlayer:
    """Same surface as ClipPlayer, backed by mpv."""

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
        # Callbacks arrive on the IPC reader thread; `post` hands them to
        # the UI thread. Without one they are dropped rather than run in
        # the wrong place - Tk from a worker thread is a crash, not a bug.
        self._post = post or (lambda fn, *a: None)

        self.path = ""
        self.duration = 0.0
        self.fps = 30.0
        self.playing = False
        self.position = 0.0
        self.loop = True
        self.volume = 80
        self.muted = False
        self.speed = 1.0

        self._proc = None
        self._sock = None
        self._ipc = ""
        self._lock = threading.Lock()
        self._request = 0
        self._alive = False
        self._pending: dict = {}
        self._failed = False
        self._starting = False
        self._queued: list = []
        # Called when mpv turns out not to be usable, so the caller can
        # go back to the built-in engine. Set by whoever builds this.
        self.on_unavailable = None
        self._vo = ""
        self.vo = ""            # override; empty lets mpv choose
        self._played_from = 0.0
        self._played_at = None

    # ── process ─────────────────────────────────────────────────────────

    def _ipc_address(self) -> str:
        token = uuid.uuid4().hex[:12]
        if IS_WINDOWS:
            return r"\\.\pipe\paz-mpv-" + token
        return os.path.join(tempfile.gettempdir(), f"paz-mpv-{token}.sock")

    # How long to wait for mpv to come up before giving up on it. Nothing
    # waits on this from the UI thread, so it is a patience limit rather
    # than a freeze budget.
    START_TIMEOUT = 6.0

    def _start(self, vo: str = None) -> bool:
        """Ask for mpv, and return immediately.

        Starting a process and waiting for its IPC endpoint takes a moment
        on a good day and forever on a bad one - a named pipe that never
        appears used to freeze the whole window for ten seconds every time
        a clip was clicked. None of it happens on the calling thread now.
        Commands issued in the meantime queue up and are flushed once the
        connection is live; if it never comes, `on_unavailable` fires and
        the caller goes back to the engine that needs nothing installed.
        """
        if self._starting or self._proc is not None:
            return True
        if not mpv_path():
            return False
        self._starting = True
        threading.Thread(target=self._start_blocking, args=(vo,),
                         daemon=True).start()
        return True

    def _start_blocking(self, vo: str = None) -> None:
        ok = False
        try:
            ok = self._spawn(vo)
        except Exception:
            ok = False
        finally:
            self._starting = False
        if ok:
            self._alive = True
            threading.Thread(target=self._reader, daemon=True).start()
            for name, ident in _OBSERVED:
                self._send("observe_property", ident, name)
            self._flush_pending()
        else:
            self.shutdown()
            if self.on_unavailable:
                self._post(self.on_unavailable,
                           "mpv wouldn't start, or wouldn't accept a "
                           "connection. Falling back to the built-in player.")

    def _spawn(self, vo: str = None) -> bool:
        binary = mpv_path()
        if not binary:
            return False
        # Default to the configured override rather than "", so it applies
        # however mpv comes to be started - load() gets there before play().
        vo = self.vo if vo is None else vo
        try:
            wid = self.widget.winfo_id()
        except Exception:
            return False

        self._ipc = self._ipc_address()
        if not IS_WINDOWS and os.path.exists(self._ipc):
            try:
                os.remove(self._ipc)
            except OSError:
                pass

        cmd = [
            binary,
            f"--wid={wid}",
            f"--input-ipc-server={self._ipc}",
            "--idle=yes",                  # stay up between clips
            "--keep-open=yes",             # hold the last frame at the end
            "--no-osc", "--no-osd-bar",    # our own controls, not mpv's
            "--no-input-default-bindings", "--input-vo-keyboard=no",
            "--no-terminal",
            "--hwdec=auto-safe",           # the whole point, on 4K
            # Deliberately NOT --profile=low-latency: it is for live
            # streams and sets vd-lavc-threads=1, which is the opposite of
            # what a 4K file needs. Seeks are already sub-millisecond.
            "--cache=yes", "--demuxer-max-bytes=64MiB",
            "--video-sync=audio",          # picture follows the sound

            f"--volume={self.volume}",
            "--mute=" + ("yes" if self.muted else "no"),
            "--loop-file=" + ("inf" if self.loop else "no"),
        ]
        if vo:
            cmd.append(f"--vo={vo}")
        self._vo = vo
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except OSError:
            self._proc = None
            return False

        return self._connect(self.START_TIMEOUT)

    def _connect(self, timeout: float = 6.0) -> bool:
        """Wait for the IPC endpoint to exist, then attach to it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False
            try:
                if IS_WINDOWS:
                    self._sock = open(self._ipc, "r+b", buffering=0)
                    return True
                if os.path.exists(self._ipc):
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(self._ipc)
                    self._sock = sock
                    return True
            except OSError:
                pass
            time.sleep(0.05)
        return False

    def shutdown(self) -> None:
        self._alive = False
        self._starting = False
        with self._lock:
            self._queued = []
        try:
            self._send("quit")
        except Exception:
            pass
        sock, self._sock = self._sock, None
        try:
            if sock is not None:
                sock.close()
        except OSError:
            pass
        proc, self._proc = self._proc, None
        if proc is not None:
            def reap():
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            threading.Thread(target=reap, daemon=True).start()
        if not IS_WINDOWS and self._ipc and os.path.exists(self._ipc):
            try:
                os.remove(self._ipc)
            except OSError:
                pass

    # ── talking to it ───────────────────────────────────────────────────

    def _flush_pending(self) -> None:
        with self._lock:
            queued, self._queued = self._queued, []
        for args in queued:
            self._send(*args)

    def _send(self, *args) -> None:
        sock = self._sock
        if sock is None:
            # Still coming up. Hold the command rather than dropping it,
            # so "click a clip, press play" works even when both happen
            # before mpv has finished starting.
            if self._starting:
                with self._lock:
                    self._queued.append(args)
                    del self._queued[:-32]
            return
        with self._lock:
            self._request += 1
            payload = json.dumps({"command": list(args),
                                  "request_id": self._request}) + "\n"
            try:
                data = payload.encode("utf-8")
                if IS_WINDOWS:
                    sock.write(data)
                    sock.flush()
                else:
                    sock.sendall(data)
            except (OSError, ValueError):
                self._sock = None

    def _set(self, name: str, value) -> None:
        self._send("set_property", name, value)

    def _reader(self) -> None:
        """One JSON object per line, forever. Property updates arrive here
        unasked, which is what keeps the seek bar honest without polling."""
        buffer = b""
        sock = self._sock
        while self._alive and sock is not None:
            try:
                chunk = sock.read(65536) if IS_WINDOWS else sock.recv(65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                self._handle(message)
        if self._alive:
            self._alive = False
            self._post(self._fail, "mpv stopped unexpectedly.")

    def _handle(self, message: dict) -> None:
        event = message.get("event")
        if event == "property-change":
            name, data = message.get("name"), message.get("data")
            if name == "time-pos" and data is not None:
                self.position = float(data)
                if self.on_tick:
                    self._post(self.on_tick, self.position)
            elif name == "duration" and data:
                self.duration = float(data)
            elif name == "pause" and data is not None:
                playing = not bool(data)
                if playing != self.playing:
                    self.playing = playing
                    if self.on_state:
                        self._post(self.on_state, playing)
            elif name == "eof-reached" and data:
                if not self.loop:
                    self.playing = False
                    if self.on_state:
                        self._post(self.on_state, False)
                    if self.on_eof:
                        self._post(self.on_eof)
        elif event == "end-file" and message.get("reason") == "error":
            self._post(self._fail, "mpv could not play this file.")

    def _fail(self, message: str) -> None:
        self.playing = False
        if self.on_fail:
            self.on_fail(message)

    # ── the ClipPlayer interface ────────────────────────────────────────

    def load(self, path: str, duration: float, fps: float) -> None:
        if not self._start():
            self._fail("mpv wouldn't start.")
            return
        self.path = path or ""
        self.duration = float(duration or 0.0)
        self.fps = float(fps or 30.0)
        self.position = 0.0
        self.playing = False
        if self.path:
            # replace, not append: one clip at a time, no playlist.
            self._send("loadfile", self.path, "replace")
            self._set("pause", True)

    def clear(self) -> None:
        self.path = ""
        self.playing = False
        self.position = 0.0
        self._send("stop")

    STALL_AFTER = 3.0

    def play(self) -> None:
        if not self.path:
            return
        if not self._start():
            return
        self._set("pause", False)
        self.playing = True
        self._played_from = self.position
        self._played_at = time.monotonic()
        if self.on_state:
            self.on_state(True)

    def check_progress(self) -> str:
        """"" while playback is healthy, or a reason it is not.

        A video output that cannot draw - no OpenGL, a driver that will
        not talk to an embedded window - leaves mpv running happily with
        nothing on screen and the clock stopped. Callers poll this so that
        shows up as a message instead of a black rectangle.
        """
        if not self.playing or self._played_at is None:
            return ""
        if self.position > self._played_from + 0.2:
            self._played_at = None          # moving; stop watching
            return ""
        if time.monotonic() - self._played_at < self.STALL_AFTER:
            return ""
        self._played_at = None
        return ("mpv is running but no picture is coming out - usually a "
                "video-output problem. Settings > e621 & App > Playback "
                "has a video output to try, or switch back to the built-in "
                "player there.")

    def pause(self) -> None:
        self._set("pause", True)
        self.playing = False
        if self.on_state:
            self.on_state(False)

    def toggle(self) -> None:
        self.pause() if self.playing else self.play()

    def stop(self) -> None:
        self.playing = False
        self.position = 0.0
        self._set("pause", True)
        if self.on_state:
            self.on_state(False)

    def seek(self, seconds: float) -> None:
        limit = max(self.duration - 0.05, 0.0) if self.duration else seconds
        target = max(0.0, min(float(seconds), limit))
        self.position = target
        self._send("seek", target, "absolute+exact")

    def nudge(self, seconds: float) -> None:
        if self.path:
            self.seek(self.position + seconds)

    def toggle_loop(self) -> bool:
        self.loop = not self.loop
        self._set("loop-file", "inf" if self.loop else "no")
        return self.loop

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        self._set("mute", self.muted)
        return self.muted

    def set_volume(self, value: int) -> None:
        self.volume = max(0, min(int(value), 100))
        if self.muted and self.volume > 0:
            self.muted = False
            self._set("mute", False)
        self._set("volume", self.volume)

    def set_speed(self, value: float) -> None:
        self.speed = max(0.1, float(value))
        self._set("speed", self.speed)

    def set_size(self, width: int, height: int = 0) -> None:
        self.view_w = max(int(width), 240)
        self.view_h = max(int(height) if height else int(width * 9 / 16), 135)
        # mpv follows the window it was given; the caller resizes that.
