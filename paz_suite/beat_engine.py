"""Beat This: no-UI logic for running the CPJKU "Beat This!" beat tracker
on an audio file and turning the result into things a video editor can
use - the .beats TSV the beat_this project already understands, a
CMX3600 EDL of LOC markers DaVinci Resolve reads via Timeline > Import >
Timeline Markers from EDL, and (best-effort, only when Resolve is running
locally with scripting enabled) markers dropped straight into the open
timeline via the Resolve scripting API.

The actual beat_this package lives vendored at ../beat_this (a checkout of
github.com/CPJKU/beat_this) rather than pip-installed, so this module adds
that folder to sys.path before importing it. Its own dependencies (torch,
torchaudio, einops, rotary-embedding-torch, soxr) are heavy and optional -
nothing here is imported at module load time, so the rest of the suite
works fine without them installed. Call import_error() first to check.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import numpy as np

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BEAT_THIS_ROOT = os.path.join(_REPO_ROOT, "beat_this")
if os.path.isdir(_BEAT_THIS_ROOT) and _BEAT_THIS_ROOT not in sys.path:
    sys.path.insert(0, _BEAT_THIS_ROOT)

CHECKPOINTS = ("final0", "final1", "final2", "small0", "small1", "small2")
DEVICE_CHOICES = ("Auto", "CPU", "GPU")
MARKER_COLORS = ("Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple")
FRAME_RATES = (23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0)


def import_error() -> str | None:
    """None if the beat tracker and its dependencies are importable, else a
    short human-readable reason. Call before touching anything else here."""
    try:
        import torch  # noqa: F401
        import beat_this.inference  # noqa: F401
    except Exception as exc:
        return str(exc)
    return None


def resolve_device(choice: str) -> str:
    """UI choice ('Auto'/'CPU'/'GPU') -> a torch device string."""
    if choice == "CPU":
        return "cpu"
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    if choice == "GPU":
        raise RuntimeError("No CUDA GPU available - install a CUDA build of "
                            "PyTorch, or use CPU.")
    return "cpu"


@dataclass
class BeatResult:
    audio_path: str
    beats: np.ndarray          # seconds, includes downbeats
    downbeats: np.ndarray      # seconds, subset of beats
    beat_numbers: np.ndarray   # 1 = downbeat, counts up within each measure

    @property
    def duration(self) -> float:
        return float(self.beats[-1]) if len(self.beats) else 0.0

    @property
    def bpm(self) -> float:
        return estimate_bpm(self.beats)

    @property
    def is_downbeat(self) -> np.ndarray:
        return np.isin(self.beats, self.downbeats)


def estimate_bpm(beats: np.ndarray) -> float:
    if len(beats) < 2:
        return 0.0
    intervals = np.diff(beats)
    intervals = intervals[intervals > 0]
    if len(intervals) == 0:
        return 0.0
    return float(60.0 / np.median(intervals))


# One loaded model per (checkpoint, device, dbn, float16) combination, kept
# for the life of the process - reloading a 78 MB checkpoint before every
# analysis would make "try another song" painfully slow.
_MODEL_CACHE: dict = {}


def analyze(audio_path: str, checkpoint: str = "final0", device: str = "cpu",
            dbn: bool = False, float16: bool = False, progress_cb=None) -> BeatResult:
    """Run the model on one audio file. Blocking - call off the UI thread.
    `progress_cb(str)`, if given, is called with short stage descriptions."""
    from beat_this.inference import File2Beats
    from beat_this.utils import infer_beat_numbers

    def note(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    key = (checkpoint, device, dbn, float16)
    file2beats = _MODEL_CACHE.get(key)
    if file2beats is None:
        note(f"Loading model '{checkpoint}' on {device}…")
        file2beats = File2Beats(checkpoint_path=checkpoint, device=device,
                                 dbn=dbn, float16=float16)
        _MODEL_CACHE[key] = file2beats

    note("Analyzing audio…")
    beats, downbeats = file2beats(audio_path)
    beats = np.asarray(beats, dtype=float)
    downbeats = np.asarray(downbeats, dtype=float)
    numbers = (infer_beat_numbers(beats, downbeats) if len(beats)
               else np.array([], dtype=int))
    return BeatResult(audio_path=audio_path, beats=beats, downbeats=downbeats,
                       beat_numbers=numbers)


def save_beats_tsv(result: BeatResult, outpath: str) -> None:
    """The native beat_this format: one `time<TAB>beat_number` line per
    beat (number 1 = downbeat) - importable into Sonic Visualiser."""
    from beat_this.utils import save_beat_tsv
    save_beat_tsv(result.beats, result.downbeats, outpath)


# ── DaVinci Resolve export ──────────────────────────────────────────────
#
# Resolve has no native audio-analysis or click-track tool, but it can
# import timeline markers from a CMX3600 EDL (Timeline > Import > Timeline
# Markers from EDL) - EDLs carry markers as `* LOC:` locator comment lines
# tied to an absolute record timecode. That's the reliable, offline path;
# AddMarker() over the live scripting API (below) is a nicer bonus when
# Resolve happens to be running right here, but needs local setup.

def _timecode(seconds: float, fps: float) -> str:
    """HH:MM:SS:FF, non-drop-frame, using the nominal (rounded) frame rate -
    e.g. 29.97 fps is counted as 30 frames/sec, matching how Resolve labels
    a non-drop timeline at that rate. Drop-frame timecode isn't produced;
    use a non-drop timeline, or shift markers afterwards if yours is drop-frame."""
    nominal = max(int(round(fps)), 1)
    total_frames = max(int(round(seconds * nominal)), 0)
    frames = total_frames % nominal
    total_seconds, _ = divmod(total_frames, nominal)
    secs = total_seconds % 60
    total_minutes = total_seconds // 60
    mins = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{mins:02d}:{secs:02d}:{frames:02d}"


def build_edl(result: BeatResult, fps: float = 30.0, title: str | None = None,
              beat_color: str = "Blue", downbeat_color: str = "Red",
              downbeats_only: bool = False) -> str:
    """A CMX3600 EDL with one dummy event spanning the song plus a `LOC`
    locator per beat. Markers land at record timecode = time into the
    song, counting from 00:00:00:00 - so line the audio clip up at the
    start of a timeline (or a fresh one) before importing, since EDL
    marker import always uses absolute record position."""
    name = title or os.path.splitext(os.path.basename(result.audio_path))[0]
    end_tc = _timecode(result.duration, fps)
    lines = [f"TITLE: {name} - Beat This markers", "FCM: NON-DROP FRAME", ""]
    lines.append("001  AX       A     C        "
                  f"00:00:00:00 {end_tc} 00:00:00:00 {end_tc}")
    lines.append(f"* FROM CLIP NAME: {name}")
    for time, number, is_down in zip(result.beats, result.beat_numbers, result.is_downbeat):
        if downbeats_only and not is_down:
            continue
        color = downbeat_color if is_down else beat_color
        label = "Downbeat" if is_down else f"Beat {int(number)}"
        lines.append(f"* LOC: {_timecode(float(time), fps)} {color.upper():<7} {label}")
    return "\n".join(lines) + "\n"


def save_edl(result: BeatResult, outpath: str, **kw) -> None:
    text = build_edl(result, **kw)
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(outpath, "w", encoding="utf-8") as fh:
        fh.write(text)


def send_to_resolve(result: BeatResult, beat_color: str = "Blue",
                     downbeat_color: str = "Red") -> tuple[bool, str]:
    """Best-effort live handoff: only works run on the same machine as a
    running copy of Resolve with Preferences > General > External
    scripting using set to Local/Network, and RESOLVE_SCRIPT_API /
    RESOLVE_SCRIPT_LIB / PYTHONPATH set per Resolve's own scripting
    README. Drops one marker per beat onto the currently open timeline, at
    its own frame rate, from its own start frame - so unlike the EDL, the
    audio clip doesn't need to sit at timeline zero first."""
    try:
        import DaVinciResolveScript as dvr  # type: ignore
    except Exception:
        return False, ("Can't reach the DaVinci Resolve scripting API from here. "
                        "This only works run on the same machine as Resolve, with "
                        "Resolve open and scripting enabled - see the Beat This "
                        "help for setup. Use the EDL export instead.")
    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        return False, "Resolve isn't running (or scripting isn't enabled)."
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        return False, "No project open in Resolve."
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        return False, "No timeline open in Resolve - open one first."
    try:
        fps = float(timeline.GetSetting("timelineFrameRate"))
    except (TypeError, ValueError):
        fps = 30.0
    start_frame = int(timeline.GetStartFrame())

    placed = skipped = 0
    for time, number, is_down in zip(result.beats, result.beat_numbers, result.is_downbeat):
        frame_id = start_frame + int(round(float(time) * fps))
        color = downbeat_color if is_down else beat_color
        name = "Downbeat" if is_down else f"Beat {int(number)}"
        if timeline.AddMarker(frame_id, color, name, "", 1):
            placed += 1
        else:
            skipped += 1

    if placed == 0:
        return False, ("Resolve rejected every marker - check for markers already "
                        "at those frames, or that the timeline covers the song's length.")
    msg = f"Added {placed} marker{'s' if placed != 1 else ''} to '{timeline.GetName()}'."
    if skipped:
        msg += f" ({skipped} skipped, likely duplicates.)"
    return True, msg
