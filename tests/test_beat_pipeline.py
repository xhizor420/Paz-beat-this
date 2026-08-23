"""End-to-end cover for the Beat This pipeline on real audio.

Everything from ffmpeg's decode through to the EDL text is exercised here:
audio loading, the mel spectrogram, the multi-model averaging, the
postprocessor, beat numbering, the BPM estimate and the timecode maths.
Only the learned weights are stubbed - a crude spectral-flux onset
detector stands in for the network, so this runs without the 234 MB of
checkpoints and without a network connection.

It therefore says nothing about how well the real model tracks beats. It
says the plumbing around it is correct, which is the part that lives in
this repository.
"""

from __future__ import annotations

import os
import struct
import sys
import wave

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import beat_engine as be    # noqa: E402

torch = pytest.importorskip("torch")
pytest.importorskip("beat_this.preprocessing")

BPM = 140.0            # phonk / techno territory
BARS = 8
BEATS = BARS * 4
SPB = 60.0 / BPM


@pytest.fixture(scope="module")
def track(tmp_path_factory) -> str:
    """A four-to-the-floor kick pattern at a known tempo, accented on the
    downbeat, with an offbeat hat - the shape of the music this tab is
    actually pointed at."""
    sr = 44100
    duration = SPB * BEATS
    samples = np.zeros(int(sr * duration))
    for index in range(BEATS):
        start = int(index * SPB * sr)
        length = int(0.09 * sr)
        envelope = np.exp(-np.arange(length) / (sr * 0.020))
        sweep = 150 * np.exp(-np.arange(length) / (sr * 0.012)) + 45
        phase = 2 * np.pi * np.cumsum(sweep) / sr
        loud = 1.0 if index % 4 == 0 else 0.62
        samples[start:start + length] += loud * envelope * np.sin(phase)
        if index % 2:
            hat = int(0.03 * sr)
            noise = np.random.default_rng(index).normal(0, 1, hat)
            samples[start:start + hat] += (
                0.18 * np.exp(-np.arange(hat) / (sr * 0.006)) * noise)
    samples = 0.85 * samples / np.max(np.abs(samples))

    path = str(tmp_path_factory.mktemp("audio") / "phonk140.wav")
    with wave.open(path, "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(b"".join(
            struct.pack("<h", int(v * 32767)) for v in samples))
    return path


class FluxStub:
    """Stands in for a trained checkpoint. Emits per-frame logits from
    spectral flux, so peaks land on real onsets in the real spectrogram."""

    def __init__(self, seed: int):
        self.rng = np.random.default_rng(seed)

    def signal2spect(self, signal, sr):
        from beat_this.preprocessing import LogMelSpect
        return LogMelSpect(device=torch.device("cpu"))(
            torch.tensor(signal, dtype=torch.float32))

    def spect2frames(self, spect):
        energy = spect.sum(-1)
        flux = torch.diff(energy, prepend=energy[:1]).clamp(min=0)
        flux = (flux - flux.mean()) / (flux.std() + 1e-9)
        beat = flux * 2.2 - 1.4 + torch.tensor(
            self.rng.normal(0, 0.7, len(flux)), dtype=torch.float32)
        return beat, beat - 1.0


@pytest.fixture
def stubbed():
    be._MODEL_CACHE.clear()
    for index, name in enumerate(("final0", "final1", "final2")):
        be._MODEL_CACHE[(name, "cpu", False)] = FluxStub(index)
    yield
    be._MODEL_CACHE.clear()


def test_analyze_runs_every_model_and_lands_near_the_beat(track, stubbed):
    stages = []
    result = be.analyze(track, progress_cb=stages.append)

    # All three checkpoints ran, and their outputs were merged.
    assert sum("model 1 of 3" in s for s in stages) == 1
    assert sum("model 3 of 3" in s for s in stages) == 1
    assert any("Combining models" in s for s in stages)

    assert len(result.beats) > 0
    assert result.bpm == pytest.approx(BPM, rel=0.06)

    # Every true beat has a detection near it. The stub is noisy and emits
    # spurious extras, so this checks recall, not precision.
    truth = np.arange(BEATS) * SPB
    misses = [t for t in truth if np.min(np.abs(result.beats - t)) > 0.07]
    assert len(misses) <= BEATS * 0.1, f"missed {len(misses)} of {BEATS} beats"


def test_single_model_skips_the_averaging(track, stubbed):
    stages = []
    be.analyze(track, checkpoint="final0", progress_cb=stages.append)
    assert not any("Combining models" in s for s in stages)
    assert not any("of 3" in s for s in stages)


def test_edl_is_one_event_per_beat_in_resolve_marker_form(track, stubbed):
    result = be.analyze(track)
    edl = be.build_edl(result, fps=30.0)

    assert edl.startswith("TITLE: ")
    assert "FCM: NON-DROP FRAME" in edl
    comments = [l for l in edl.splitlines() if l.startswith(" |C:")]
    assert len(comments) == len(result.beats)
    assert all("|M:" in l and "|D:1" in l for l in comments)
    assert all("ResolveColor" in l for l in comments)
    # No `* LOC:` lines - Resolve's marker import does not read them.
    assert "* LOC:" not in edl


def test_edl_counts_frames_at_the_real_rate_not_the_nominal_one():
    """A 29.97 timeline labels 30 frames to the second but only delivers
    29.97 of them. Counting at 30 put markers 0.1% late - a quarter of a
    second adrift by the end of a four-minute song."""
    assert be._seconds_to_frames(240, 29.97) == 7193      # not 7200
    assert be._seconds_to_frames(240, 23.976) == 5754     # not 5760
    assert be._seconds_to_frames(240, 30.0) == 7200
    # Labels stay nominal, which is what makes a non-drop clock run slow.
    assert be._frames_to_tc(7193, 29.97) == "00:03:59:23"


def test_edl_starts_where_the_timeline_starts():
    result = be.BeatResult(audio_path="x.wav", beats=np.array([0.0, 1.0]),
                           downbeats=np.array([0.0]),
                           beat_numbers=np.array([1, 2]))
    default = be.build_edl(result, fps=30.0)
    assert "01:00:00:00 01:00:00:01" in default
    assert "01:00:01:00 01:00:01:01" in default

    from_zero = be.build_edl(result, fps=30.0, start_tc="00:00:00:00")
    assert "00:00:00:00 00:00:00:01" in from_zero


def test_unknown_checkpoint_names_fall_back_to_the_recommended_one():
    assert be.normalize_checkpoint("best (3 models)") == be.ENSEMBLE
    assert be.normalize_checkpoint("fold3") == be.DEFAULT_CHECKPOINT
    assert be.normalize_checkpoint("small0") == "small0"
    assert be.checkpoint_parts(be.ENSEMBLE) == ("final0", "final1", "final2")


# ── marker density ──────────────────────────────────────────────────────

@pytest.fixture
def eight_beats():
    """Two bars of 4/4 at 140 BPM."""
    spb = 60.0 / BPM
    beats = np.arange(8) * spb
    return be.BeatResult(audio_path="x.wav", beats=beats,
                         downbeats=beats[::4],
                         beat_numbers=np.array([1, 2, 3, 4, 1, 2, 3, 4]))


def test_density_halves_and_doubles_without_losing_downbeats(eight_beats):
    downbeats = eight_beats.downbeats

    same = be.scale_beats(eight_beats, "1×")
    assert len(same.beats) == 8

    half = be.scale_beats(eight_beats, "÷2")
    assert len(half.beats) == 4
    assert half.bpm == pytest.approx(BPM / 2, rel=0.01)
    assert np.isin(downbeats, half.beats).all()

    double = be.scale_beats(eight_beats, "×2")
    assert len(double.beats) == 15          # midpoints between each pair
    assert double.bpm == pytest.approx(BPM * 2, rel=0.01)
    assert np.isin(downbeats, double.beats).all()


def test_density_is_a_view_not_a_mutation(eight_beats):
    before = eight_beats.beats.copy()
    be.scale_beats(eight_beats, "×2")
    be.scale_beats(eight_beats, "÷2")
    assert np.array_equal(eight_beats.beats, before)


def test_density_leaves_a_too_short_result_alone():
    lone = be.BeatResult(audio_path="x.wav", beats=np.array([1.0]),
                         downbeats=np.array([1.0]), beat_numbers=np.array([1]))
    assert be.scale_beats(lone, "×2") is lone
    assert be.scale_beats(lone, "÷2") is lone
