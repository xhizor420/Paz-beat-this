"""Unified configuration for the whole suite — one file, one database, one
tag cache, shared by the Convert tab and the Library tab.

Replaces the two standalone configs (config.json for PAZ Studio,
den_config.json for PAZ Den). On first run, if neither the new unified file
nor either legacy file exists, sensible empty defaults are used and the
Folders / Settings dialogs prompt for real paths — this suite makes no
assumption about where your drives are mounted. If a legacy install is
found, its settings are migrated once so upgrading costs nothing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".video_tool")
CONFIG_PATH = os.path.join(CONFIG_DIR, "paz_config.json")
DB_PATH = os.path.join(CONFIG_DIR, "paz_library.sqlite3")
THUMB_DIR = os.path.join(CONFIG_DIR, "paz_thumbs")
E621_META_PATH = os.path.join(CONFIG_DIR, "e621_meta.json")

# Legacy per-app files, from before the two tools were combined.
_LEGACY_STUDIO_CONFIG = os.path.join(CONFIG_DIR, "config.json")
_LEGACY_DEN_CONFIG = os.path.join(CONFIG_DIR, "den_config.json")


@dataclass
class AppConfig:
    # ── Convert: locations ──────────────────────────────────────────────
    source_root: str = ""
    output_root: str = ""
    upscale_root: str = ""
    log_dir: str = ""
    source_extensions: list = field(
        default_factory=lambda: [".webm", ".mp4", ".gif", ".mkv", ".mov",
                                  ".avi", ".m4v"])

    # ── Shared: locations & taxonomy ────────────────────────────────────
    premium_root: str = ""             # 4K/60+ edit-ready copies
    subfolders: list = field(default_factory=list)   # category folder names

    # ── Convert: encoding ────────────────────────────────────────────────
    codec: str = "h264"          # h264 | hevc | av1
    use_gpu: bool = True
    gpu_quality: int = 18
    cpu_quality: int = 18
    cpu_preset: str = "slow"
    audio_bitrate: str = "192k"
    audio_mode: str = "keep"     # keep | mute | none
    verify_output: bool = True
    workers: int = 1

    # ── Convert: frame-rate handling for editing ────────────────────────
    force_cfr: bool = True
    edit_gop: bool = True
    loop_short: bool = False
    loop_min: float = 5.0

    # ── Convert: timeouts ────────────────────────────────────────────────
    stall_timeout: int = 120
    hard_timeout: int = 3600

    # ── Convert: sorting rules ───────────────────────────────────────────
    sort_enabled: bool = True
    sort_existing: bool = False
    gap_check_enabled: bool = True
    min_height: int = 2160
    min_fps: float = 60.0
    transfer_mode: str = "copy"  # copy | move | hardlink

    # ── Convert: interface ───────────────────────────────────────────────
    auto_preview: bool = False
    filmstrip_frames: int = 10
    watch: bool = False
    watch_resume: bool = False
    hover_peek: bool = True

    # ── Library: indexing scope ─────────────────────────────────────────
    library_root: str = ""             # defaults to output_root when unset
    library_subfolders: list = field(default_factory=list)  # [] = all of `subfolders`
    library_recursive: bool = False
    library_extensions: list = field(
        default_factory=lambda: [".mp4", ".webm", ".gif", ".mkv", ".mov", ".m4v"])

    # ── Library: display ─────────────────────────────────────────────────
    page_size: int = 48
    thumb_width: int = 480
    thumb_fit: str = "contain"
    card_width: int = 224              # gallery tile width in px (Settings)
    sidebar_open: bool = True
    search_history: list = field(default_factory=list)
    theater: bool = False
    last_search: str = ""
    last_sort: str = ""
    last_rating: str = "All"
    detail_open: dict = field(default_factory=dict)
    sidebar_group_open: dict = field(default_factory=dict)
    sort: str = "Newest"
    hidden_tags: list = field(default_factory=list)

    # ── Library: player ──────────────────────────────────────────────────
    player_height: int = 540
    player_loop: bool = True
    player_volume: int = 80
    player_muted: bool = False
    # Prefer the matching 4K/60+ edit-pool copy over the indexed original
    # when one exists - that's the point of the pool, so playback should
    # default to it instead of the source file.
    player_prefer_premium: bool = True

    # ── e621 lookup (shared cache, shared credentials) ──────────────────
    e621_enabled: bool = True
    e621_user: str = ""
    e621_key: str = ""
    e621_fetch_delay: float = 0.6      # e621 allows ~2/sec; this stays under
    library_autofetch: bool = True     # fill in missing tags right after a sync
    # Every regular tag fetch also folds in up to this many "due for
    # refresh" posts (see E621Meta.is_stale) alongside the genuinely
    # uncached ones, so scores/tags on posts already in the library
    # quietly stay current without ever re-checking the whole library
    # at once. 0 disables the ambient refresh (manual only).
    library_stale_refresh_budget: int = 40

    # ── Performance (tune upward as the library grows) ──────────────────
    # In-memory ffprobe result cache, shared by both tabs. Each entry is a
    # few hundred bytes, so even a six-figure value costs tens of MB.
    probe_cache_limit: int = 60000
    # On-disk scrub/hover-preview JPEG cache (temp dir, not the persistent
    # gallery thumbnails - those have no cap and live one-per-clip).
    frame_cache_limit: int = 30000

    # ── Beat This: song analysis + Resolve marker export ────────────────
    # The 3-model ensemble - see beat_engine.CHECKPOINTS. Slower than any
    # single model and more accurate than all of them.
    beat_checkpoint: str = "Best quality"
    # One-time upgrade marker: installs made before the ensemble existed
    # hold the old default of "final0", which nobody chose deliberately -
    # it was simply what beat_this defaults to. Moved across once, then
    # never touched again, so a later deliberate choice of final0 sticks.
    beat_default_upgraded: bool = False
    beat_device: str = "Auto"           # Auto | CPU | GPU
    beat_dbn: bool = False
    beat_float16: bool = False
    beat_fps: float = 30.0
    beat_beat_color: str = "Blue"
    beat_downbeat_color: str = "Red"
    beat_downbeats_only: bool = False
    # Where the Resolve timeline the markers are for begins. EDL marker
    # import is absolute, and Resolve starts a new timeline at 01:00:00:00.
    beat_start_tc: str = "01:00:00:00"
    beat_last_audio_dir: str = ""
    beat_last_export_dir: str = ""

    # ── App-wide ──────────────────────────────────────────────────────────
    # Your own picture across the header strip. Empty means the built-in
    # sweep in the four tab colours; see theme.banner_image.
    banner_path: str = ""
    banner_dir: str = ""      # last folder a header picture came from
    last_tab: str = "Convert"

    @classmethod
    def load(cls) -> "AppConfig":
        cfg = cls()
        if os.path.exists(CONFIG_PATH):
            cfg._read(CONFIG_PATH)
            if cfg._upgrade_beat_default():
                cfg.save()
            return cfg
        # First run of the merged suite: fold in whatever the two
        # standalone apps had, so upgrading loses nothing.
        migrated = cfg._migrate_legacy()
        if migrated:
            cfg.save()
        return cfg

    def _upgrade_beat_default(self) -> bool:
        """Move pre-ensemble installs onto the new default exactly once.
        Returns True when something changed and the file needs writing."""
        if self.beat_default_upgraded:
            return False
        self.beat_default_upgraded = True
        if self.beat_checkpoint == "final0":
            self.beat_checkpoint = AppConfig.beat_checkpoint
        return True

    def _read(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def _migrate_legacy(self) -> bool:
        found = False
        try:
            with open(_LEGACY_STUDIO_CONFIG, "r", encoding="utf-8") as fh:
                studio = json.load(fh)
            found = True
            rename = {"extensions": "source_extensions"}
            skip = {"e621_meta_path"}
            for key, value in studio.items():
                target = rename.get(key, key)
                if target in skip:
                    continue
                if hasattr(self, target):
                    setattr(self, target, value)
        except (OSError, ValueError):
            pass
        try:
            with open(_LEGACY_DEN_CONFIG, "r", encoding="utf-8") as fh:
                den = json.load(fh)
            found = True
            rename = {
                "extensions": "library_extensions",
                "subfolders": "library_subfolders",
                "recursive": "library_recursive",
                "autofetch": "library_autofetch",
                "fetch_delay": "e621_fetch_delay",
            }
            roots = den.get("roots") or []
            if roots:
                self.library_root = roots[0]
            for key, value in den.items():
                if key in ("roots", "schema"):
                    continue
                target = rename.get(key, key)
                if hasattr(self, target):
                    setattr(self, target, value)
        except (OSError, ValueError):
            pass
        if not self.library_root and self.output_root:
            self.library_root = self.output_root
        return found

    def save(self) -> str | None:
        """Write the config. Returns an error message on failure, else None."""
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, indent=2)
            return None
        except OSError as exc:
            return str(exc)

    @property
    def source_ext_set(self) -> set:
        return {e.lower() if e.startswith(".") else "." + e.lower()
                for e in self.source_extensions}

    @property
    def library_ext_set(self) -> set:
        return {e.lower() if e.startswith(".") else "." + e.lower()
                for e in self.library_extensions}

    def effective_library_root(self) -> str:
        return self.library_root or self.output_root

    def effective_library_subfolders(self) -> list:
        """The ticked subset of `subfolders` to index; [] on both means all."""
        return self.library_subfolders or list(self.subfolders)
