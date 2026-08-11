"""Encoding pipeline: classify a source clip, plan the recipe, build the
ffmpeg command, run it with cancel/stall/hard-timeout handling, and verify
the result. Pure logic — no widgets — so it is easy to reason about and to
reuse from the duplicate finder and gap sweep.
"""

from __future__ import annotations

import collections
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .config import AppConfig
from .files import NO_WINDOW
from .format import fmt_time
from .media import MediaInfo, available_encoders

FFMPEG_NOISE = (
    "ffmpeg version", "built with", "configuration:", "libav", "libsw",
    "libpost", "Press [q]", "frame=", "Stream mapping",
    "deprecated", "Past duration", "Last message repeated",
)


def clean_stderr(text: str, max_lines: int = 10) -> str:
    if not text:
        return "No error output captured"
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not any(n in ln for n in FFMPEG_NOISE)]
    return "\n".join(lines[-max_lines:]) if lines else text.strip()[:300]


@dataclass
class Task:
    """One queued conversion: everything decided before ffmpeg starts."""
    iid: str
    source: str
    target: str
    folder: str
    name: str
    pid: str = ""                # e621 post ID parsed from the filename
    state: str = "queued"
    info: MediaInfo | None = None
    error: str | None = None
    dest: str = ""
    encoder: str = ""
    seconds: float = 0.0


# ── sorting rules ───────────────────────────────────────────────────────

def snap_target(cfg: AppConfig) -> int:
    return int(round(cfg.min_fps))


def snap_applies(cfg: AppConfig, fps: float) -> bool:
    """
    True when a near-miss source (58.5 up to just under the target) should
    be resampled to exactly the target. Always on - 59.94 is just NTSC's
    way of saying 60, so it's always treated as the real target rather
    than failing the bar by a rounding hair. The upper bound is a plain
    `< target` (no epsilon fudge-factor) so nothing in that band - 59.94,
    59.97, 59.999, whatever a container happens to report - slips through
    uncaught. Never touches sources already at or above the target, so
    120 fps footage is left alone.
    """
    if not fps:
        return False
    target = snap_target(cfg)
    return (target - 1.5) <= fps < target


def classify(cfg: AppConfig, width: int, height: int, fps: float) -> tuple:
    """Return (destination_root, label) for a clip of these properties."""
    short_side = min(width, height) if width else 0
    if snap_applies(cfg, fps):
        fps = float(snap_target(cfg))
    big = short_side >= cfg.min_height
    fast = fps >= cfg.min_fps
    if big and fast:
        return cfg.premium_root, "Edit pool"
    if big:
        return cfg.upscale_root, "Needs frames"
    if fast:
        return cfg.upscale_root, "Needs resolution"
    return cfg.upscale_root, "Needs both"


GPU_ENCODERS = {"h264": "h264_nvenc", "hevc": "hevc_nvenc", "av1": "av1_nvenc"}
CPU_ENCODERS = {"h264": "libx264", "hevc": "libx265", "av1": "libsvtav1"}


def video_args(cfg: AppConfig, gpu: bool) -> list | None:
    """Encoder flags for the configured codec, or None if unavailable."""
    have = available_encoders()
    if gpu:
        name = GPU_ENCODERS.get(cfg.codec)
        if not name or (have and name not in have):
            return None
        args = ["-c:v", name, "-preset", "p7", "-rc", "vbr",
                "-cq", str(cfg.gpu_quality), "-b:v", "0"]
        if cfg.codec in ("h264", "hevc"):
            args += ["-tune", "hq"]
        if cfg.codec == "hevc":
            args += ["-tag:v", "hvc1"]
        return args

    name = CPU_ENCODERS.get(cfg.codec)
    if not name or (have and name not in have):
        return None
    if name == "libsvtav1":
        return ["-c:v", name, "-preset", "6", "-crf", str(cfg.cpu_quality)]
    args = ["-c:v", name, "-preset", cfg.cpu_preset, "-crf", str(cfg.cpu_quality)]
    if name == "libx265":
        args += ["-tag:v", "hvc1"]
    return args


@dataclass
class Recipe:
    """Everything about one encode that is decided before ffmpeg starts."""
    has_audio: bool = False
    duration: float = 0.0        # effective length, after any looping
    loops: int = 0               # extra repeats prepended via -stream_loop
    snap_to: int = 0             # resample to this exact fps (0 = leave alone)
    gop: int = 0                 # keyframe interval in frames (0 = default)
    cfr: bool = False
    audio_mode: str = "keep"
    notes: list = field(default_factory=list)


def plan_recipe(cfg: AppConfig, info: MediaInfo | None) -> Recipe:
    recipe = Recipe(audio_mode=cfg.audio_mode, cfr=cfg.force_cfr)
    fps = info.fps if info else 0.0
    duration = info.duration if info else 0.0
    recipe.has_audio = bool(info and info.has_audio)

    if cfg.loop_short and 0 < duration < cfg.loop_min:
        import math as _math
        recipe.loops = int(_math.ceil(cfg.loop_min / duration)) - 1
        recipe.notes.append(f"looped x{recipe.loops + 1} to reach {cfg.loop_min:.0f}s")
    recipe.duration = duration * (recipe.loops + 1)

    if snap_applies(cfg, fps):
        recipe.snap_to = snap_target(cfg)
        recipe.notes.append(f"{fps:.2f} fps resampled to {recipe.snap_to}.00")

    if cfg.edit_gop:
        base = recipe.snap_to or fps or 60
        recipe.gop = max(24, min(int(round(base)), 240))

    if info and info.vfr and cfg.force_cfr:
        recipe.notes.append("variable frame rate locked to constant")
    return recipe


def build_command(source: str, target: str, cfg: AppConfig,
                   venc: list, gpu: bool, recipe: Recipe) -> list:
    cmd = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats",
           "-err_detect", "ignore_err", "-fflags", "+genpts",
           "-ignore_unknown", "-strict", "-2"]
    if gpu:
        cmd += ["-hwaccel", "cuda"]
    if recipe.loops:
        cmd += ["-stream_loop", str(recipe.loops)]
    cmd += ["-i", source]

    mode = recipe.audio_mode
    silent = (mode == "mute") or (mode == "keep" and not recipe.has_audio)
    if silent:
        cmd += ["-f", "lavfi", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000"]
        if mode == "mute" and recipe.has_audio:
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]

    filters = ["setparams=colorspace=bt709:color_primaries=bt709:"
               "color_trc=bt709:range=tv"]
    if recipe.snap_to:
        filters.append(f"fps={recipe.snap_to}")
    cmd += ["-vf", ",".join(filters)]

    cmd += venc
    if recipe.gop:
        cmd += ["-g", str(recipe.gop)]
    if recipe.cfr:
        cmd += ["-vsync", "cfr"]
    cmd += ["-pix_fmt", "yuv420p"]

    if mode == "none":
        cmd += ["-an"]
    else:
        cmd += ["-c:a", "aac", "-b:a", cfg.audio_bitrate]
        if silent:
            cmd += ["-shortest"]

    cmd += ["-movflags", "+faststart", "-map_metadata", "-1", target]
    return cmd


class Cancelled(Exception):
    pass


def _reader(stream, tag, out_queue):
    try:
        for line in stream:
            out_queue.put((tag, line))
    except (ValueError, OSError):
        pass
    finally:
        out_queue.put((tag, None))


_PROGRESS_RE = re.compile(r"^([a-z_]+)=(.*)$")


def run_ffmpeg(cmd: list, duration: float, cfg: AppConfig,
               progress_cb=None, cancel=None) -> tuple:
    """
    Run ffmpeg, streaming -progress output.

    Returns (ok, error_text). Raises Cancelled if the cancel event fires.
    stderr is drained on its own thread into a ring buffer, so an error at
    the very end of a long encode is still reported.
    """
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=NO_WINDOW,
        )
    except OSError as exc:
        return False, str(exc)

    events = queue.Queue()
    for stream, tag in ((proc.stdout, "out"), (proc.stderr, "err")):
        threading.Thread(target=_reader, args=(stream, tag, events), daemon=True).start()

    err_tail = collections.deque(maxlen=80)
    finished = {"out": False, "err": False}
    stats = {}
    started = time.time()
    last_move = time.time()
    killed_reason = None

    def stop(reason):
        nonlocal killed_reason
        killed_reason = reason
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (subprocess.SubprocessError, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    while not (finished["out"] and finished["err"]):
        tag = line = None
        try:
            tag, line = events.get(timeout=0.25)
        except queue.Empty:
            pass

        if tag and line is None:
            finished[tag] = True
        elif line is not None:
            if tag == "err":
                text = line.strip()
                if text and not any(n in text for n in FFMPEG_NOISE):
                    err_tail.append(text)
            else:
                match = _PROGRESS_RE.match(line.strip())
                if match:
                    stats[match.group(1)] = match.group(2)
                    if match.group(1) == "progress":
                        last_move = time.time()
                        if progress_cb:
                            progress_cb(_snapshot(stats, duration))

        if cancel is not None and cancel.is_set():
            stop("cancelled")
            break
        if time.time() - last_move > cfg.stall_timeout:
            stop(f"No progress for {cfg.stall_timeout}s")
            break
        if cfg.hard_timeout and time.time() - started > cfg.hard_timeout:
            stop(f"Exceeded {fmt_time(cfg.hard_timeout)} limit")
            break

    try:
        code = proc.wait(timeout=10)
    except subprocess.SubprocessError:
        code = -1

    if killed_reason == "cancelled":
        raise Cancelled()
    if killed_reason:
        return False, killed_reason
    if code != 0:
        return False, clean_stderr("\n".join(err_tail)) or f"ffmpeg exited {code}"
    return True, None


def _snapshot(stats: dict, duration: float) -> dict:
    raw = stats.get("out_time_us") or stats.get("out_time_ms") or "0"
    try:
        seconds = int(raw) / 1_000_000
    except ValueError:
        seconds = 0.0
    fraction = min(seconds / duration, 1.0) if duration else 0.0
    speed = stats.get("speed", "").strip()
    try:
        speed_val = float(speed.rstrip("x"))
    except ValueError:
        speed_val = 0.0
    eta = (duration - seconds) / speed_val if (speed_val > 0 and duration) else None
    return {
        "fraction": fraction,
        "seconds": seconds,
        "speed": speed if speed and speed != "N/A" else "",
        "fps": stats.get("fps", ""),
        "eta": eta,
        "done": stats.get("progress") == "end",
    }


def verify(path: str, timeout: int = 120) -> tuple:
    """Full decode pass to catch a truncated or corrupt result."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=NO_WINDOW,
        )
        if result.returncode == 0:
            return True, None
        return False, clean_stderr(result.stderr, 5)
    except subprocess.TimeoutExpired:
        return False, "Verification timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def convert(source: str, target: str, cfg: AppConfig,
            progress_cb=None, cancel=None, log=None) -> tuple:
    """
    Encode one file. Returns (ok, error, encoder_label).
    Tries GPU first when enabled, then falls back to CPU.
    """
    from .media import probe
    info = probe(source)
    recipe = plan_recipe(cfg, info)
    if log:
        for note in recipe.notes:
            log(note, "info")

    plan = []
    if cfg.use_gpu:
        gpu_args = video_args(cfg, gpu=True)
        if gpu_args:
            plan.append(("GPU", gpu_args, True))
    cpu_args = video_args(cfg, gpu=False)
    if cpu_args:
        plan.append(("CPU", cpu_args, False))

    if not plan:
        return False, f"No encoder available for {cfg.codec}", ""

    errors = []
    for label, venc, gpu in plan:
        if progress_cb:
            progress_cb({"fraction": 0.0, "speed": "", "eta": None,
                         "stage": label, "done": False})
        cmd = build_command(source, target, cfg, venc, gpu, recipe)
        try:
            ok, error = run_ffmpeg(cmd, recipe.duration, cfg, progress_cb, cancel)
        except Cancelled:
            _discard(target)
            raise

        if ok and cfg.verify_output:
            ok, error = verify(target)
            if not ok:
                error = f"Output failed verification: {error}"

        if ok:
            return True, None, label

        errors.append(f"{label}: {error}")
        _discard(target)
        if log and len(plan) > 1 and label == "GPU":
            log("GPU encode failed, falling back to CPU", "warn")

    return False, "\n".join(errors), ""


def _discard(path: str) -> None:
    import os
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def transfer(source: str, dest: str, mode: str) -> None:
    """copy / move / hardlink, with a copy fallback if linking is impossible."""
    import os
    if mode == "move":
        shutil.move(source, dest)
    elif mode == "hardlink":
        try:
            if os.path.exists(dest):
                os.remove(dest)
            os.link(source, dest)
        except OSError:
            shutil.copy2(source, dest)
    else:
        shutil.copy2(source, dest)
