"""Double-click this to start PAZ Suite on Windows - no console window.

.pyw files open with pythonw.exe, which runs without a console. That
also means there is nowhere for an error message to go, so anything the
app would have printed - including the traceback of an error inside a
click handler - is written to %USERPROFILE%\\.video_tool\\paz_suite.log
instead. The log is started afresh when it passes a couple of megabytes.

Right-click → Send to → Desktop (create shortcut) for a desktop icon, or
pin the running app to the taskbar: it has its own taskbar identity, so
the pinned button starts PAZ Suite rather than Python.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(os.path.expanduser("~"), ".video_tool")
LOG_PATH = os.path.join(LOG_DIR, "paz_suite.log")
LOG_LIMIT = 2 * 1024 * 1024


def _open_log():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_LIMIT:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        return open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    except OSError:
        return None


if sys.stdout is None or sys.stderr is None:      # pythonw: no console
    _log = _open_log()
    if _log is not None:
        sys.stdout = sys.stderr = _log

sys.path.insert(0, HERE)
os.chdir(HERE)


def _missing() -> list:
    missing = []
    for module, package in (("customtkinter", "customtkinter"), ("PIL", "Pillow")):
        try:
            __import__(module)
        except ModuleNotFoundError:
            missing.append(package)
    return missing


_need = _missing()
if _need:
    # No console to print to, so say it where it will be seen.
    import tkinter
    from tkinter import messagebox
    _root = tkinter.Tk()
    _root.withdraw()
    messagebox.showerror(
        "PAZ Suite - missing packages",
        "PAZ Suite needs: " + ", ".join(_need) + "\n\nInstall them with:\n\n"
        + os.path.basename(sys.executable).replace("pythonw", "python")
        + " -m pip install " + " ".join(_need))
    sys.exit(1)

from paz_suite.app import main                        # noqa: E402

main()
