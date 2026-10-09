"""Launch the real app, visit every tab, screenshot each, and fail loudly.

py_compile and pyflakes pass on code that crashes at startup, and the test
suite builds widgets one at a time - neither notices a tab that throws the
first time it is shown. This starts the app the way main.py does, against
a throwaway home folder so no real config or library is touched, and
exits non-zero if any Tk callback raised, any screenshot came out blank,
or the whole run hung.

    python tools/smoke.py [out_dir]
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import traceback

for stream in (sys.stdout, sys.stderr):
    # A traceback holding a "·" or "⋯" must not be what stops the report.
    try:
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "smoke-shots")
HOME = tempfile.mkdtemp(prefix="paz-smoke-")
# Before paz_suite is imported: CONFIG_DIR is worked out at import time.
os.environ["USERPROFILE"] = os.environ["HOME"] = HOME
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.makedirs(os.path.join(HOME, ".video_tool"), exist_ok=True)
for sub in ("source", "output"):
    os.makedirs(os.path.join(HOME, sub), exist_ok=True)
with open(os.path.join(HOME, ".video_tool", "paz_config.json"), "w", encoding="utf-8") as f:
    json.dump({"source_root": os.path.join(HOME, "source"),
               "output_root": os.path.join(HOME, "output"),
               "e621_enabled": False, "ui_scale": "100%",
               "player_backend": "builtin", "last_tab": "Convert"}, f)

from PIL import ImageGrab                       # noqa: E402
from paz_suite import app as paz_app            # noqa: E402

errors: list = []
shots: list = []


def _record(where: str, exc: BaseException) -> None:
    errors.append(f"{where}: " + "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)))


threading.excepthook = lambda a: _record(f"thread {a.thread.name}", a.exc_value)


def _blank(image) -> bool:
    # getcolors() gives up (None) past its limit - a real screen does.
    return image.convert("RGB").resize((160, 90)).getcolors(12) is not None


def _dialogs(root) -> list:
    import tkinter as tk
    return [w for w in root.winfo_children()
            if isinstance(w, tk.Toplevel) and w.winfo_viewable()]


def _grab(root, name: str) -> None:
    root.update_idletasks()
    # A dialog that opened for this step is what gets photographed, and
    # then closed so the next step starts from the main window.
    dialogs = _dialogs(root)
    window = dialogs[-1] if dialogs else root
    if dialogs:
        # The root is topmost while the run drives it, so a dialog opens
        # underneath unless it is raised too.
        window.attributes("-topmost", True)
        window.lift()
        window.update()
        import time
        time.sleep(0.4)
    x, y = window.winfo_rootx(), window.winfo_rooty()
    image = ImageGrab.grab(bbox=(x, y, x + window.winfo_width(), y + window.winfo_height()),
                           all_screens=True)
    for dialog in dialogs:
        try:
            dialog.destroy()
        except Exception:
            pass
    path = os.path.join(OUT, f"{name}.png")
    image.save(path)
    shots.append(path)
    if _blank(image):
        errors.append(f"{name}: the screenshot is blank")


def _drive(app) -> None:
    root = app.root
    root.report_callback_exception = lambda kind, value, tb: _record("Tk callback", value)
    root.attributes("-topmost", True)
    steps = []
    for tab in paz_app.TAB_NAMES:
        steps.append((lambda t=tab: app._select_tab(t), 2500, tab.replace(" ", "_")))
    steps.append((app._shortcuts, 1500, "Shortcuts"))
    steps.append((app.open_settings, 2000, "Settings"))
    from paz_suite.library_windows import HelpWindow
    steps.append((lambda: HelpWindow(root), 1500, "Help"))

    def run(i: int = 0) -> None:
        if i:
            _grab(root, steps[i - 1][2])
        if i == len(steps):
            root.quit()
            return
        action, wait, _ = steps[i]
        try:
            action()
        except Exception as exc:
            _record(steps[i][2], exc)
        root.after(wait, lambda: run(i + 1))

    root.after(4000, run)


_Original = paz_app.PazApp


class _Driven(_Original):
    def __init__(self, root):
        super().__init__(root)
        _drive(self)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    paz_app.PazApp = _Driven
    hang = threading.Timer(120, lambda: (print("smoke: hung", flush=True), os._exit(3)))
    hang.daemon = True
    hang.start()
    try:
        paz_app.main()
    except Exception as exc:
        _record("startup", exc)
    hang.cancel()
    for path in shots:
        print("shot", path)
    for error in errors:
        print("ERROR", error, file=sys.stderr)
    expected = len(paz_app.TAB_NAMES) + 3
    if len(shots) != expected:
        print(f"smoke: {len(shots)} of {expected} screenshots taken", file=sys.stderr)
        return 1
    print("smoke: ok" if not errors else f"smoke: {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Worker threads (census, thumbnails, audio probe) need not be waited on.
    os._exit(code)
