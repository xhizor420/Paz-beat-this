"""Windows: the handful of things the OS has to be asked for directly.

This app is used on Windows, and a few of the things that make it feel
right there are not reachable through Tk at all. Each is one Win32 call,
each is a no-op anywhere else, and none of them can stop the app from
starting: a call that fails is simply not made.

- The taskbar knows the app as PAZ Suite, not as python.exe, so it gets
  its own taskbar button, its own icon and its own pinning.
- Timers tick every millisecond instead of every 15.6. Tk's after() is
  how every animation, debounce and chunked redraw here is paced, and at
  Windows' default tick an after(16) could fire at 31ms - a frame late,
  every frame.
- The thread that draws the window runs a notch above normal priority,
  so while ffmpeg, Topaz and the thumbnail workers keep every core busy,
  a click is still answered first. Only that one thread: the workers stay
  at normal priority and ffmpeg's background jobs below it.
- The PC does not go to sleep in the middle of a conversion, a sync or a
  long tag fetch - a batch left running overnight used to stop the moment
  Windows decided nobody was there.
- Encodes run at full speed. Windows 11 puts processes it thinks are in
  the background into Efficiency mode (EcoQoS), and on a CPU with
  efficiency cores that moves them there. An ffmpeg encode has no window
  of its own, so it looks like background work exactly when you switch to
  Resolve or Topaz and leave it running. The app opts its encodes, and
  itself, out.
"""

from __future__ import annotations

import os
import threading

IS_WINDOWS = os.name == "nt"

APP_ID = "PAZ.Suite"

# SetThreadExecutionState
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
# SetThreadPriority
THREAD_PRIORITY_ABOVE_NORMAL = 1


def _windll():
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        return ctypes.windll
    except Exception:
        return None


def set_app_id() -> bool:
    """Give the process its own taskbar identity. Before any window."""
    windll = _windll()
    if windll is None:
        return False
    try:
        windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        return True
    except Exception:
        return False


_timer = {"on": False}


def sharpen_timers() -> bool:
    """1ms timer resolution for this process - see the module notes."""
    windll = _windll()
    if windll is None or _timer["on"]:
        return False
    try:
        if windll.winmm.timeBeginPeriod(1) == 0:      # TIMERR_NOERROR
            _timer["on"] = True
            return True
    except Exception:
        pass
    return False


def restore_timers() -> None:
    windll = _windll()
    if windll is None or not _timer["on"]:
        return
    try:
        windll.winmm.timeEndPeriod(1)
    except Exception:
        pass
    _timer["on"] = False


def raise_ui_thread() -> bool:
    """A notch above normal for the calling thread. Call it from the UI
    thread, once."""
    windll = _windll()
    if windll is None:
        return False
    try:
        kernel32 = windll.kernel32
        return bool(kernel32.SetThreadPriority(kernel32.GetCurrentThread(),
                                               THREAD_PRIORITY_ABOVE_NORMAL))
    except Exception:
        return False


# SetProcessInformation(ProcessPowerThrottling, ...)
_PROCESS_POWER_THROTTLING = 4
_THROTTLING_VERSION = 1
_THROTTLE_EXECUTION_SPEED = 0x1


def _full_speed_handle(handle) -> bool:
    windll = _windll()
    if windll is None or not handle:
        return False
    try:
        import ctypes

        class State(ctypes.Structure):
            _fields_ = [("Version", ctypes.c_ulong),
                        ("ControlMask", ctypes.c_ulong),
                        ("StateMask", ctypes.c_ulong)]

        # ControlMask says "this process decides execution speed itself";
        # StateMask 0 says "and it is never throttled".
        state = State(_THROTTLING_VERSION, _THROTTLE_EXECUTION_SPEED, 0)
        return bool(windll.kernel32.SetProcessInformation(
            ctypes.c_void_p(int(handle)), _PROCESS_POWER_THROTTLING,
            ctypes.byref(state), ctypes.sizeof(state)))
    except Exception:
        return False


def full_speed(proc=None) -> bool:
    """Keep a process out of Efficiency mode: `proc` is a Popen, or None
    for this process. False wherever it does not apply."""
    if proc is None:
        windll = _windll()
        if windll is None:
            return False
        try:
            return _full_speed_handle(windll.kernel32.GetCurrentProcess())
        except Exception:
            return False
    return _full_speed_handle(getattr(proc, "_handle", None))


class KeepAwake:
    """Keep Windows from sleeping while any long job is running.

    Jobs hold() a named reason and release() it when done. While any
    reason is held, a background thread tells Windows the system is in use
    every PING_SECONDS - the stateless form of SetThreadExecutionState,
    so it does not matter which thread holds or releases, and nothing is
    left set if the app dies. The display may still turn off; only sleep
    is held off.
    """

    PING_SECONDS = 30.0

    def __init__(self, ping=None):
        self._lock = threading.Lock()
        self._reasons: dict = {}
        self._wake = threading.Event()
        self._thread = None
        self._ping = ping if ping is not None else self._system_required

    @staticmethod
    def _system_required() -> None:
        windll = _windll()
        if windll is None:
            return
        try:
            windll.kernel32.SetThreadExecutionState(ES_SYSTEM_REQUIRED)
        except Exception:
            pass

    @property
    def held(self) -> bool:
        with self._lock:
            return bool(self._reasons)

    def hold(self, reason: str) -> None:
        with self._lock:
            self._reasons[reason] = self._reasons.get(reason, 0) + 1
            start = self._thread is None
            if start:
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="keep-awake")
        self._ping()
        if start:
            self._thread.start()

    def release(self, reason: str) -> None:
        with self._lock:
            count = self._reasons.get(reason, 0) - 1
            if count > 0:
                self._reasons[reason] = count
            else:
                self._reasons.pop(reason, None)
        self._wake.set()

    def _run(self) -> None:
        while True:
            self._wake.wait(self.PING_SECONDS)
            self._wake.clear()
            with self._lock:
                if not self._reasons:
                    self._thread = None
                    return
            self._ping()


awake = KeepAwake()
