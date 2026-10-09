#!/usr/bin/env python3
"""
PAZ Suite — Convert + Library + Vault + Beat This
===================================================

Batch-converts a video library to MP4 (GPU with CPU fallback), sorts the
output by resolution/frame rate, browses/searches the converted result
e621-style, tracks what's already used in a project, and runs the "Beat
This!" beat tracker on a song to export DaVinci Resolve markers — all in
one app with one config and one database.

Requirements:
    pip install -r requirements.txt
    ffmpeg + ffprobe on PATH (ffplay too, for audio in the Library player)

    The Beat This tab needs its own, heavier dependencies (PyTorch and
    friends) and degrades gracefully without them - see the Beat This
    section of README.md.

Run:
    python main.py
    (or double-click "PAZ Suite.pyw" on Windows for no console window)
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _console_is_ours() -> bool:
    """True when Windows opened a console just for this run - main.py
    double-clicked, so only py.exe/python.exe are attached to it - rather
    than it belonging to a cmd or PowerShell window someone typed into."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pids = (wintypes.DWORD * 16)()
    count = kernel32.GetConsoleProcessList(pids, 16)
    if not 0 < count <= 16:
        return False
    for pid in pids[:count]:
        if pid == os.getpid():
            continue
        handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return False
        finally:
            kernel32.CloseHandle(handle)
        if os.path.basename(buf.value).lower() not in ("py.exe", "python.exe", "pythonw.exe"):
            return False
    return True


def _leave_console() -> None:
    """Double-clicked, restart under pythonw via PAZ Suite.pyw so the
    console window closes instead of sitting beside the app for as long
    as it runs. Started from a terminal or a debugger, stay put."""
    if sys.platform != "win32" or sys.gettrace() is not None or os.environ.get("PAZ_CONSOLE"):
        return
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    launcher = os.path.join(HERE, "PAZ Suite.pyw")
    if not (os.path.isfile(pythonw) and os.path.isfile(launcher)):
        return
    try:
        if not _console_is_ours():
            return
        import subprocess
        subprocess.Popen([pythonw, launcher, *sys.argv[1:]], cwd=HERE, close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS
                         | subprocess.CREATE_NEW_PROCESS_GROUP)
    except (OSError, AttributeError, ValueError):
        return
    sys.exit(0)


def _check_imports() -> None:
    missing = []
    for module, package in (("customtkinter", "customtkinter"), ("PIL", "Pillow")):
        try:
            __import__(module)
        except ModuleNotFoundError:
            missing.append(package)
    if missing:
        print("\n[ERROR] Missing dependencies: " + ", ".join(missing))
        print(f"Install:  {sys.executable} -m pip install {' '.join(missing)}\n")
        sys.exit(1)


if __name__ == "__main__":
    _leave_console()
    _check_imports()
    from paz_suite.app import main
    main()
