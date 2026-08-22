"""Beat This: no-UI logic for running the CPJKU "Beat This!" beat tracker
on an audio file and turning the result into things a video editor can
use - the .beats TSV the beat_this project already understands, a
CMX3600 EDL of timeline markers DaVinci Resolve reads via Timeline >
Import > Timeline Markers from EDL, and (best-effort, only when Resolve is running
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
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .files import NO_WINDOW

if TYPE_CHECKING:      # annotations only - never imported at runtime
    import numpy as np

# numpy is imported inside the functions that use it, not here. This module
# is reached from app.py via beat_tab, so a module-scope import would make
# numpy a hard requirement of the whole suite and take Convert/Library/Vault
# down with an ImportError on a machine that only installed the base
# requirements. The annotations below are lazy strings (see the __future__
# import), so nothing here needs numpy at import time.

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BEAT_THIS_ROOT = os.path.join(_REPO_ROOT, "beat_this")
if os.path.isdir(_BEAT_THIS_ROOT) and _BEAT_THIS_ROOT not in sys.path:
    sys.path.insert(0, _BEAT_THIS_ROOT)

# ── which models to offer ───────────────────────────────────────────────
#
# beat_this publishes about forty checkpoints, but most of them exist to
# reproduce tables in the paper, not to track beats well: `single_*` is
# trained on one reduced split, `fold0..7` on one fold each, and the
# `single_no*` family are deliberately crippled ablations (no tempo
# augmentation, no sum head, and so on). Offering those would only invite
# picking a worse model by accident.
#
# What's left is the two that are actually meant for use, per the project's
# own README: `final0/1/2`, the paper's main system trained on everything
# except GTZAN (78 MB each, three random seeds), and `small0/1/2`, the same
# recipe at a fraction of the size (8.1 MB). The three seeds are equivalent
# in expected quality - there is no "best seed".
#
# The default here is neither: running all three `final` seeds and averaging
# their frame-wise probabilities before peak-picking is the one option that
# beats any single one of them, at three times the compute. Averaging
# independently-seeded runs of the same architecture is standard practice
# and cancels per-seed noise; the beat_this authors report per-seed means
# rather than an ensemble, so this is a well-founded addition rather than a
# published number.

# Named for what it gives you, not for how it works. An earlier name of
# "best (3 models)" read like a choice between models, which is exactly the
# decision this option exists to remove.
ENSEMBLE = "Best quality"

# Settings written before the rename.
_RENAMED = {"best (3 models)": ENSEMBLE, "ensemble": ENSEMBLE}

CHECKPOINTS = {
    # UI name -> the beat_this shortnames to run and average
    ENSEMBLE:  ("final0", "final1", "final2"),
    "final0":  ("final0",),
    "final1":  ("final1",),
    "final2":  ("final2",),
    "small0":  ("small0",),
    "small1":  ("small1",),
    "small2":  ("small2",),
}

CHECKPOINT_NOTES = {
    ENSEMBLE: ("The recommended setting. Runs the main model's three trained "
               "copies and merges them - most accurate, ~3x slower. 234 MB."),
    "final0": "The paper's main model, seed 0 - beat_this's own default. 78 MB.",
    "final1": "The paper's main model, seed 1. Same quality as seed 0. 78 MB.",
    "final2": "The paper's main model, seed 2. Same quality as seed 0. 78 MB.",
    "small0": "Small model, seed 0. Much faster, a little less accurate. 8.1 MB.",
    "small1": "Small model, seed 1. 8.1 MB.",
    "small2": "Small model, seed 2. 8.1 MB.",
}

DEFAULT_CHECKPOINT = ENSEMBLE


def normalize_checkpoint(name: str) -> str:
    """A stored setting -> a name that exists now. Covers the rename above
    and anything hand-edited or dropped in a later version, so a stale
    config falls back to the recommended model instead of failing at
    analysis time."""
    name = _RENAMED.get(name, name)
    return name if name in CHECKPOINTS else DEFAULT_CHECKPOINT


def checkpoint_parts(name: str) -> tuple:
    """The beat_this shortnames a UI choice runs. An unknown name is passed
    straight through, so a checkpoint typed in by hand still works."""
    return CHECKPOINTS.get(_RENAMED.get(name, name), (name,))
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


# ── dependencies ────────────────────────────────────────────────────────
#
# Deliberately unpinned, unlike beat_this/requirements.txt: those pins
# (torch==2.3.1 and friends) have no wheels for current Python versions, so
# installing them verbatim fails outright on a new interpreter. Asking pip
# for the plain names gets whatever is current and compatible instead.

REQUIREMENTS = (
    # (import name, pip name, what it's for)
    ("torch",                  "torch",                  "the model itself"),
    ("torchaudio",             "torchaudio",             "mel spectrograms"),
    ("einops",                 "einops",                 "model layers"),
    ("rotary_embedding_torch", "rotary-embedding-torch", "model layers"),
    ("soxr",                   "soxr",                   "resampling"),
    ("numpy",                  "numpy",                  "arrays"),
)


def dependency_status() -> list:
    """[(pip name, purpose, installed?, version-or-error)] for the UI."""
    import importlib
    rows = []
    for module, package, purpose in REQUIREMENTS:
        try:
            mod = importlib.import_module(module)
            rows.append((package, purpose, True, getattr(mod, "__version__", "")))
        except Exception as exc:
            rows.append((package, purpose, False, str(exc).split("\n")[0]))
    return rows


def missing_packages() -> list:
    return [pkg for pkg, _purpose, ok, _detail in dependency_status() if not ok]


def install_dependencies(packages=None, progress_cb=None) -> tuple:
    """pip-install the beat tracker's dependencies into the interpreter
    that's running this app. Streams pip's output to progress_cb. Returns
    (ok, summary). Blocking - call off the UI thread."""
    packages = list(packages if packages is not None else missing_packages())
    if not packages:
        return True, "Everything is already installed."

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", *packages]
    if progress_cb:
        progress_cb("$ " + " ".join(cmd))
    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=NO_WINDOW)
    except OSError as exc:
        return False, f"Couldn't run pip: {exc}"

    for line in process.stdout:
        line = line.rstrip()
        if line and progress_cb:
            progress_cb(line)
    process.wait()

    if process.returncode != 0:
        return False, (f"pip exited with code {process.returncode}. If this is "
                        "PyTorch failing, install it manually for your platform "
                        "from https://pytorch.org/get-started/locally/")
    still_missing = missing_packages()
    if still_missing:
        return False, ("Installed, but still can't import: "
                        + ", ".join(still_missing) + " - restart the app.")
    return True, "Installed. Restart the app so the new packages load."


# ── checkpoints ─────────────────────────────────────────────────────────
#
# Downloaded on first use into torch's hub cache by load_checkpoint(). That
# happens silently inside the first analysis, which reads as a hang on a
# slow connection, so the tab shows what's cached and can fetch ahead.

def checkpoint_file(name: str) -> str | None:
    """Where torch.hub would cache this checkpoint, or None if torch is
    missing (in which case nothing is cached anyway)."""
    try:
        import torch
    except Exception:
        return None
    return os.path.join(torch.hub.get_dir(), "checkpoints", f"beat_this-{name}.ckpt")


def checkpoint_present(name: str) -> bool:
    """True once every checkpoint this choice needs is cached - the
    ensemble needs all three of its models, not just the first."""
    parts = checkpoint_parts(name)
    return all(bool(checkpoint_file(p)) and os.path.exists(checkpoint_file(p))
               for p in parts)


def missing_checkpoints(name: str) -> list:
    return [p for p in checkpoint_parts(name)
            if not (checkpoint_file(p) and os.path.exists(checkpoint_file(p)))]


def download_checkpoint(name: str, progress_cb=None) -> tuple:
    """Fetch everything this choice needs into the cache ahead of time, so
    the first analysis isn't a silent multi-minute download. Returns
    (ok, msg)."""
    missing = missing_checkpoints(name)
    if not missing:
        return True, f"'{name}' is already downloaded."
    try:
        from beat_this.inference import load_checkpoint
    except Exception as exc:
        return False, f"Can't download yet - {exc}"
    for index, part in enumerate(missing, 1):
        if progress_cb:
            progress_cb(f"Downloading checkpoint '{part}' "
                        f"({index} of {len(missing)})…")
        try:
            load_checkpoint(part, "cpu")
        except Exception as exc:
            return False, f"Couldn't download '{part}': {exc}"
    downloaded = ", ".join(missing)
    return True, f"Downloaded {downloaded}."


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


# ── audio loading ───────────────────────────────────────────────────────
#
# beat_this's own load_audio() goes torchaudio -> soundfile -> madmom and
# raises if all three fail. On a stock install that's a real cliff: recent
# torchaudio dropped its bundled decoders, soundfile only ships libsndfile
# (no mp3 on older builds), and madmom isn't installed. The result is
# "Could not load audio" on an ordinary .mp3 even though every dependency
# imported fine.
#
# PAZ already requires ffmpeg on PATH and shells out to it everywhere
# else, so decoding here is both the most reliable option and free: one
# subprocess that resamples straight to the 22.05 kHz mono the model wants,
# no extra Python packages and no format it can't read (audio or video).
# The vendored loader stays as a fallback for the unlikely case ffmpeg is
# missing. Note that a float decode of a lossy file legitimately overshoots
# +-1.0 on intersample peaks; that is what torchaudio would return too, so
# it is passed through rather than clipped.

SAMPLE_RATE = 22050


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def load_audio_ffmpeg(path: str) -> tuple:
    """Decode any audio/video file to a mono float64 array at 22.05 kHz.
    Raises RuntimeError with ffmpeg's own message if it can't be read."""
    import numpy as np
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", path,
           "-f", "f32le", "-acodec", "pcm_f32le",
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=NO_WINDOW)
    except OSError as exc:
        raise RuntimeError(f"Couldn't run ffmpeg: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else
                            f"ffmpeg failed to decode {os.path.basename(path)}")
    signal = np.frombuffer(result.stdout, dtype=np.float32)
    if signal.size == 0:
        raise RuntimeError(f"No audio stream found in {os.path.basename(path)}")
    return signal.astype(np.float64), SAMPLE_RATE


def load_audio(path: str) -> tuple:
    """ffmpeg first, then whatever backends beat_this itself can find."""
    if has_ffmpeg():
        return load_audio_ffmpeg(path)
    from beat_this.preprocessing import load_audio as _fallback
    return _fallback(path)


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
        import numpy as np
        return np.isin(self.beats, self.downbeats)


def estimate_bpm(beats: np.ndarray) -> float:
    import numpy as np
    if len(beats) < 2:
        return 0.0
    intervals = np.diff(beats)
    intervals = intervals[intervals > 0]
    if len(intervals) == 0:
        return 0.0
    return float(60.0 / np.median(intervals))


# One loaded model per (checkpoint, device, dbn, float16) combination, kept
# for the life of the process - reloading a 78 MB checkpoint before every
# analysis would make "try another song" painfully slow, and the ensemble
# holds three of them. Keyed without the DBN setting, since postprocessing
# now happens after the model rather than inside it.
_MODEL_CACHE: dict = {}


def analyze(audio_path: str, checkpoint: str = DEFAULT_CHECKPOINT,
            device: str = "cpu", dbn: bool = False, float16: bool = False,
            progress_cb=None) -> BeatResult:
    """Run the model on one audio file. Blocking - call off the UI thread.
    `progress_cb(str)`, if given, is called with short stage descriptions.

    A `checkpoint` naming more than one model (see CHECKPOINTS) runs each
    of them over the same spectrogram and averages their frame-wise
    probabilities before a single round of peak-picking. Probabilities
    rather than logits, because the postprocessor keeps peaks above 0.5
    probability - so an averaged probability makes that threshold mean
    "most of the models agree", which averaging logits would not.
    """
    import numpy as np
    import torch
    from beat_this.inference import Audio2Frames
    from beat_this.model.postprocessor import Postprocessor
    from beat_this.utils import infer_beat_numbers

    def note(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    parts = checkpoint_parts(checkpoint)

    # Decoded before the model is touched: a file ffmpeg can't read should
    # fail in a second with ffmpeg's reason, not after a checkpoint download.
    note("Reading audio…")
    signal, sample_rate = load_audio(audio_path)

    models = []
    for index, part in enumerate(parts, 1):
        key = (part, device, float16)
        model = _MODEL_CACHE.get(key)
        if model is None:
            note(f"Loading model '{part}' on {device}…"
                 + (f" ({index} of {len(parts)})" if len(parts) > 1 else ""))
            model = Audio2Frames(checkpoint_path=part, device=device,
                                 float16=float16)
            _MODEL_CACHE[key] = model
        models.append(model)

    # The mel spectrogram doesn't depend on which checkpoint is loaded, and
    # every model here is on the same device, so it is computed once and
    # reused rather than recomputed per model.
    spect = models[0].signal2spect(signal, sample_rate)

    beat_logits = downbeat_logits = None
    for index, model in enumerate(models, 1):
        note("Analyzing audio…" + (f" (model {index} of {len(models)})"
                                    if len(models) > 1 else ""))
        beat, down = model.spect2frames(spect)
        if len(models) == 1:
            beat_logits, downbeat_logits = beat, down
            break
        beat, down = torch.sigmoid(beat), torch.sigmoid(down)
        beat_logits = beat if beat_logits is None else beat_logits + beat
        downbeat_logits = down if downbeat_logits is None else downbeat_logits + down

    if len(models) > 1:
        note("Combining models…")
        scale = float(len(models))
        eps = 1e-6
        beat_logits = torch.logit((beat_logits / scale).clamp(eps, 1 - eps))
        downbeat_logits = torch.logit((downbeat_logits / scale).clamp(eps, 1 - eps))

    beats, downbeats = Postprocessor(type="dbn" if dbn else "minimal")(
        beat_logits, downbeat_logits)
    beats = np.asarray(beats, dtype=float)
    downbeats = np.asarray(downbeats, dtype=float)
    numbers = (infer_beat_numbers(beats, downbeats) if len(beats)
               else np.array([], dtype=int))
    return BeatResult(audio_path=audio_path, beats=beats, downbeats=downbeats,
                       beat_numbers=numbers)


# ── marker density ──────────────────────────────────────────────────────
#
# Two reasons this exists, and the second is the important one.
#
# An editor wants different marker spacing for different cuts: eighths for
# fast cutting, half-notes for slow. That is a preference.
#
# The other is that beat trackers make octave errors - locking onto half or
# double the intended tempo. It is the most common way this kind of model
# is wrong, and it is most likely on exactly the music this tab gets
# pointed at: phonk sits around 140 BPM with a halftime feel that reads
# convincingly as 70, and a four-to-the-floor techno track reads as
# convincingly at 128 as at 64. When it happens the beats are not wrong,
# they are all real - there are just half or twice as many as wanted, and
# one press fixes it without re-running the model.

DENSITIES = ("÷2", "1×", "×2")
DEFAULT_DENSITY = "1×"


def scale_beats(result: "BeatResult", density: str) -> "BeatResult":
    """`result` re-spaced. Downbeats are preserved either way, so bar
    starts keep landing on bar starts."""
    import numpy as np

    beats = np.asarray(result.beats, dtype=float)
    downbeats = np.asarray(result.downbeats, dtype=float)
    if density not in ("÷2", "×2") or len(beats) < 2:
        return result

    if density == "×2":
        midpoints = (beats[:-1] + beats[1:]) / 2.0
        beats = np.sort(np.concatenate([beats, midpoints]))
    else:
        # Keep alternate beats, starting from the first downbeat so the
        # ones that survive are the ones bars start on. Any downbeat that
        # would fall on the dropped side is kept regardless.
        first = 0
        if len(downbeats):
            hits = np.nonzero(np.isin(beats, downbeats))[0]
            if len(hits):
                first = int(hits[0])
        keep = (np.arange(len(beats)) - first) % 2 == 0
        keep |= np.isin(beats, downbeats)
        beats = beats[keep]

    from beat_this.utils import infer_beat_numbers
    numbers = (infer_beat_numbers(beats, downbeats) if len(beats)
               else np.array([], dtype=int))
    return BeatResult(audio_path=result.audio_path, beats=beats,
                       downbeats=downbeats, beat_numbers=numbers)


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

# Resolve names marker colours "ResolveColorBlue" and so on inside an EDL,
# and rejects anything it doesn't recognise.
RESOLVE_COLOR = {name: "ResolveColor" + name for name in MARKER_COLORS}

# Resolve's own timelines start at 01:00:00:00 out of the box, and EDL
# marker import places markers at absolute record timecode - so an EDL
# written from 00:00:00:00 puts every marker an hour before the start of
# the timeline, where they simply don't land. This is the single most
# common reason a beat EDL "imports" and produces nothing.
DEFAULT_START_TC = "01:00:00:00"
START_CHOICES = ("01:00:00:00", "00:00:00:00")


# Two different conversions live here and mixing them up is how markers
# end up drifting on a 23.976 or 29.97 timeline.
#
# A timecode *label* counts the nominal rate - 30 frames to the labelled
# second on a 29.97 non-drop timeline - which is exactly why such a
# timeline's clock runs slow against the wall. Frames themselves still
# arrive at the real rate. So: seconds -> frames uses the real rate, and
# frames -> label uses the nominal one. Counting a beat's position at the
# nominal rate put every marker 0.1% late, which is a quarter of a second
# by the end of a four-minute song - a visible miss on a cut.

def _nominal(fps: float) -> int:
    return max(int(round(fps)), 1)


def _seconds_to_frames(seconds: float, fps: float) -> int:
    return max(int(round(float(seconds) * float(fps))), 0)


def _tc_to_frames(timecode: str, fps: float) -> int:
    """A timecode label -> a frame count. Labels are nominal-rate."""
    nominal = _nominal(fps)
    parts = [int(p) for p in str(timecode).strip().replace(";", ":").split(":")]
    while len(parts) < 4:
        parts.insert(0, 0)
    hours, mins, secs, frames = parts[-4:]
    return ((hours * 60 + mins) * 60 + secs) * nominal + frames


def _frames_to_tc(frames: int, fps: float) -> str:
    """A frame count -> a non-drop timecode label."""
    nominal = _nominal(fps)
    frames = max(int(frames), 0)
    rest, frame = divmod(frames, nominal)
    rest, secs = divmod(rest, 60)
    hours, mins = divmod(rest, 60)
    return f"{hours % 24:02d}:{mins:02d}:{secs:02d}:{frame:02d}"


def build_edl(result: BeatResult, fps: float = 30.0, title: str | None = None,
              beat_color: str = "Blue", downbeat_color: str = "Red",
              downbeats_only: bool = False,
              start_tc: str = DEFAULT_START_TC) -> str:
    """A CMX3600 EDL of timeline markers in the form Resolve's own marker
    export writes, and the only form its marker import reads: one
    one-frame event per beat, each followed by a comment line carrying the
    colour, name and duration.

    An earlier version wrote `* LOC:` locator comments hung off a single
    long event. That is what Resolve emits when you export a *timeline*
    that happens to contain markers, but Timeline > Import > Timeline
    Markers from EDL does not read it, which is why importing produced an
    error instead of markers.

    Non-drop-frame throughout. On a drop-frame timeline the labels will
    not line up; use a non-drop timeline, which is what Resolve gives you
    unless you ask otherwise.

    Marker positions are record timecode counted from `start_tc`, which
    defaults to 01:00:00:00 because that is where Resolve starts a new
    timeline. Line the song up at the start of the timeline before
    importing - EDL marker import is absolute, it has no idea where the
    audio actually sits.
    """
    name = title or os.path.splitext(os.path.basename(result.audio_path))[0]
    base = _tc_to_frames(start_tc or DEFAULT_START_TC, fps)
    beat_name = RESOLVE_COLOR.get(beat_color, "ResolveColorBlue")
    down_name = RESOLVE_COLOR.get(downbeat_color, "ResolveColorRed")

    lines = [f"TITLE: {name} - Beat This markers", "FCM: NON-DROP FRAME", ""]
    event = 0
    for time, number, is_down in zip(result.beats, result.beat_numbers, result.is_downbeat):
        if downbeats_only and not is_down:
            continue
        event += 1
        frame = base + _seconds_to_frames(time, fps)
        tc_in = _frames_to_tc(frame, fps)
        tc_out = _frames_to_tc(frame + 1, fps)
        colour = down_name if is_down else beat_name
        label = "Downbeat" if is_down else f"Beat {int(number)}"
        lines.append(f"{event:03d}  001      V     C        "
                     f"{tc_in} {tc_out} {tc_in} {tc_out}")
        lines.append(f" |C:{colour} |M:{label} |D:1")
        lines.append("")
    return "\n".join(lines) + "\n"


def save_edl(result: BeatResult, outpath: str, **kw) -> None:
    text = build_edl(result, **kw)
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(outpath, "w", encoding="utf-8") as fh:
        fh.write(text)


# ── the Resolve scripting API ───────────────────────────────────────────
#
# Resolve's own README tells you to set RESOLVE_SCRIPT_API,
# RESOLVE_SCRIPT_LIB and PYTHONPATH by hand before any of this imports.
# Almost nobody has, so relying on a bare `import DaVinciResolveScript`
# meant the live handoff reported "can't reach Resolve" on machines where
# Resolve was open on the next monitor. The installer puts everything in
# fixed places, so look there instead.

def _resolve_paths() -> tuple:
    """(scripting API dirs, fusionscript library files) to try, in order,
    for this platform. Environment variables win when they are set."""
    api, lib = [], []
    env_api = os.environ.get("RESOLVE_SCRIPT_API")
    env_lib = os.environ.get("RESOLVE_SCRIPT_LIB")
    if env_api:
        api.append(env_api)
    if env_lib:
        lib.append(env_lib)

    if sys.platform.startswith("win"):
        data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        api.append(os.path.join(data, "Blackmagic Design", "DaVinci Resolve",
                                 "Support", "Developer", "Scripting"))
        for root in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     r"C:\Program Files"):
            lib.append(os.path.join(root, "Blackmagic Design", "DaVinci Resolve",
                                     "fusionscript.dll"))
    elif sys.platform == "darwin":
        api.append("/Library/Application Support/Blackmagic Design/"
                   "DaVinci Resolve/Developer/Scripting")
        lib.append("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/"
                   "Libraries/Fusion/fusionscript.so")
        lib.append("/Applications/DaVinci Resolve.app/Contents/Libraries/"
                   "Fusion/fusionscript.so")
    else:
        api.append("/opt/resolve/Developer/Scripting")
        api.append("/home/resolve/Developer/Scripting")
        lib.append("/opt/resolve/libs/Fusion/fusionscript.so")
        lib.append("/home/resolve/libs/Fusion/fusionscript.so")

    seen = set()
    api = [p for p in api if not (p in seen or seen.add(p))]
    seen = set()
    lib = [p for p in lib if not (p in seen or seen.add(p))]
    return api, lib


def resolve_module() -> tuple:
    """(module, None) once the Resolve scripting API is importable, else
    (None, a diagnostic saying exactly what was looked for and what to
    check). Three attempts, cheapest first: an already-configured
    PYTHONPATH, the installer's own Modules folder, and finally loading
    fusionscript straight off disk as an extension module."""
    import importlib
    notes = []

    try:
        return importlib.import_module("DaVinciResolveScript"), None
    except Exception:
        pass

    api_dirs, lib_files = _resolve_paths()
    lib = next((p for p in lib_files if os.path.exists(p)), "")
    if lib:
        # DaVinciResolveScript.py reads this to find the native library.
        os.environ.setdefault("RESOLVE_SCRIPT_LIB", lib)

    for api in api_dirs:
        modules = os.path.join(api, "Modules")
        if not os.path.isdir(modules):
            notes.append(f"not found: {modules}")
            continue
        if modules not in sys.path:
            sys.path.append(modules)
        try:
            return importlib.import_module("DaVinciResolveScript"), None
        except Exception as exc:
            notes.append(f"{modules}: {exc}")

    # Last resort: DaVinciResolveScript.py is only a thin wrapper that
    # loads this same library, so load it directly and skip the wrapper.
    if lib:
        try:
            import importlib.util
            from importlib.machinery import ExtensionFileLoader
            spec = importlib.util.spec_from_file_location(
                "fusionscript", lib, loader=ExtensionFileLoader("fusionscript", lib))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, "scriptapp"):
                return module, None
            notes.append(f"{lib}: loaded but has no scriptapp()")
        except Exception as exc:
            notes.append(f"{lib}: {exc}")
    else:
        notes.append("no fusionscript library found at: "
                     + ", ".join(lib_files))

    detail = "\n".join("  - " + n for n in notes)
    return None, (
        "Couldn't load the DaVinci Resolve scripting API.\n\n"
        "Check, in this order:\n"
        "  1. Resolve > Preferences > System > General > 'External scripting "
        "using' is set to Local (it is Disabled by default, and this is the "
        "usual cause even with Resolve open).\n"
        "  2. Resolve and this app are both 64-bit and on the same machine.\n"
        "  3. fusionscript is built for a specific Python version - if the "
        "detail below mentions a DLL or module load failure, run this app on "
        "the Python version Resolve supports.\n\n"
        "What was tried:\n" + detail + "\n\n"
        "The EDL export needs none of this and always works.")


def send_to_resolve(result: BeatResult, beat_color: str = "Blue",
                     downbeat_color: str = "Red",
                     downbeats_only: bool = False) -> tuple[bool, str]:
    """Best-effort live handoff: only works run on the same machine as a
    running copy of Resolve with Preferences > General > External
    scripting using set to Local/Network, and RESOLVE_SCRIPT_API /
    RESOLVE_SCRIPT_LIB / PYTHONPATH set per Resolve's own scripting
    README. Drops one marker per beat onto the currently open timeline, at
    its own frame rate, from its own start frame - so unlike the EDL, the
    audio clip doesn't need to sit at timeline zero first."""
    dvr, problem = resolve_module()
    if dvr is None:
        return False, problem
    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        return False, ("Loaded Resolve's scripting API, but it can't attach to a "
                        "running Resolve. Open Resolve, then set Preferences > "
                        "System > General > 'External scripting using' to Local "
                        "and try again.")
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
    try:
        start_frame = int(timeline.GetStartFrame())
    except (TypeError, ValueError):
        start_frame = 0

    def place(base: int) -> tuple:
        """Add every beat, offsetting frame numbers by `base`."""
        added = missed = 0
        for time, number, is_down in zip(result.beats, result.beat_numbers,
                                          result.is_downbeat):
            if downbeats_only and not is_down:
                continue
            frame_id = base + int(round(float(time) * fps))
            color = downbeat_color if is_down else beat_color
            name = "Downbeat" if is_down else f"Beat {int(number)}"
            if timeline.AddMarker(frame_id, color, name, "", 1):
                added += 1
            else:
                missed += 1
        return added, missed

    # AddMarker counts from the start of the timeline in some Resolve
    # builds and from absolute record frame in others, and the wrong one
    # silently places nothing. Try relative first, fall back to absolute -
    # whichever is wrong adds no markers, so there is nothing to undo.
    placed, skipped = place(0)
    if placed == 0 and start_frame:
        placed, skipped = place(start_frame)

    if placed == 0:
        return False, ("Resolve accepted the connection but rejected every marker. "
                        "Usually that means the timeline is shorter than the song, "
                        "or it already has markers on those frames.")
    msg = f"Added {placed} marker{'s' if placed != 1 else ''} to '{timeline.GetName()}'."
    if skipped:
        msg += f" ({skipped} skipped, likely duplicates.)"
    return True, msg
