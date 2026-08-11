#!/usr/bin/env python3
"""
PAZ Suite — Convert + Library
==============================

Batch-converts a video library to MP4 (GPU with CPU fallback), sorts the
output by resolution/frame rate, and browses/searches the converted result
e621-style — all in one app with one config, one database and one e621 tag
cache, instead of two separate tools.

Requirements:
    pip install -r requirements.txt
    ffmpeg + ffprobe on PATH (ffplay too, for audio in the Library player)

Run:
    python main.py
"""

import sys


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
    _check_imports()
    from paz_suite.app import main
    main()
