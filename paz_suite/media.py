"""Media probing, frame extraction and thumbnailing — one ffprobe/ffmpeg
layer shared by the encoder, the scrub preview, the gallery and the
duplicate finder.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter

from .config import THUMB_DIR
from .files import NO_WINDOW


def _split_mjpeg(blob: bytes) -> list:
    """Cut an MJPEG pipe into individual JPEGs on the SOI/EOI markers.
    ffmpeg writes them back to back with no container, so the markers are
    the only frame boundary there is."""
    frames = []
    start = blob.find(b"\xff\xd8")
    while start != -1:
        end = blob.find(b"\xff\xd9", start + 2)
        if end == -1:
            break
        frames.append(blob[start:end + 2])
        start = blob.find(b"\xff\xd8", end + 2)
    return frames


# ─────────────────────────────────────────────────────────────────────────
#  Probing
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class MediaInfo:
    width: int = 0
    height: int = 0
    fps: float = 0.0
    rfps: float = 0.0            # container frame rate; differs when VFR
    duration: float = 0.0
    size: int = 0
    vcodec: str = ""
    acodec: str = ""
    bitrate: int = 0

    @property
    def has_audio(self) -> bool:
        return bool(self.acodec)

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.width else "--"

    @property
    def vfr(self) -> bool:
        return bool(self.fps and self.rfps and abs(self.fps - self.rfps) > 0.5)

    @property
    def fps_text(self) -> str:
        return f"{self.fps:.2f}".rstrip("0").rstrip(".") if self.fps else "--"


_PROBE_CACHE_LIMIT = 60000    # a growing library shouldn't keep re-probing;
                               # raise via AppConfig.probe_cache_limit
_probe_cache: "OrderedDict" = OrderedDict()
_probe_lock = threading.Lock()


def set_probe_cache_limit(limit: int) -> None:
    """Applied once at startup from AppConfig.probe_cache_limit."""
    global _PROBE_CACHE_LIMIT
    _PROBE_CACHE_LIMIT = max(int(limit), 500)
    with _probe_lock:
        while len(_probe_cache) > _PROBE_CACHE_LIMIT:
            _probe_cache.popitem(last=False)


def _parse_rate(value: str) -> float:
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den = float(den)
            return float(num) / den if den else 0.0
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def probe(path: str, use_cache: bool = True) -> MediaInfo | None:
    """One ffprobe call for everything we need. Cached on path + mtime + size."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.normcase(path), st.st_mtime_ns, st.st_size)

    if use_cache:
        with _probe_lock:
            hit = _probe_cache.get(key)
            if hit is not None:
                _probe_cache.move_to_end(key)
        if hit is not None:
            return hit

    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=45, creationflags=NO_WINDOW,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

    info = MediaInfo(size=st.st_size)
    fmt = data.get("format", {})
    try:
        info.duration = float(fmt.get("duration", 0) or 0)
    except (TypeError, ValueError):
        info.duration = 0.0
    try:
        info.bitrate = int(fmt.get("bit_rate", 0) or 0)
    except (TypeError, ValueError):
        info.bitrate = 0

    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "video" and not info.width:
            info.width = int(stream.get("width", 0) or 0)
            info.height = int(stream.get("height", 0) or 0)
            info.vcodec = stream.get("codec_name", "")
            avg = _parse_rate(stream.get("avg_frame_rate", "0/0"))
            container = _parse_rate(stream.get("r_frame_rate", "0/0"))
            info.fps = avg or container
            info.rfps = container
            if not info.duration:
                try:
                    info.duration = float(stream.get("duration", 0) or 0)
                except (TypeError, ValueError):
                    pass
        elif kind == "audio" and not info.acodec:
            info.acodec = stream.get("codec_name", "")

    with _probe_lock:
        _probe_cache[key] = info
        _probe_cache.move_to_end(key)
        while len(_probe_cache) > _PROBE_CACHE_LIMIT:
            _probe_cache.popitem(last=False)   # evict the least-recently-used entry
    return info


def check_dependencies() -> list:
    return [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]


def has_ffplay() -> bool:
    return shutil.which("ffplay") is not None


_encoder_cache: set | None = None


def available_encoders() -> set:
    """Ask ffmpeg once which encoders this build actually has."""
    global _encoder_cache
    if _encoder_cache is not None:
        return _encoder_cache
    names = set()
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20, creationflags=NO_WINDOW,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) == 6:
                names.add(parts[1])
    except (OSError, subprocess.SubprocessError):
        pass
    _encoder_cache = names
    return names


# ─────────────────────────────────────────────────────────────────────────
#  Frame compositing
# ─────────────────────────────────────────────────────────────────────────

def fit_frame(image, box_w: int, box_h: int, mode: str = "contain",
              blur: bool = False):
    """
    Return an image of exactly box_w x box_h.

    "contain" scales the frame to fit entirely inside the tile, filling the
    leftover letterbox with a blurred, zoomed copy of the same frame instead
    of dead black - portrait, ultrawide and square clips all land intact.
    "cover" crops to fill for anyone who prefers edge-to-edge tiles.
    """
    box_w = max(int(box_w), 1)
    box_h = max(int(box_h), 1)
    source = image.convert("RGB")
    src_w, src_h = source.size
    if not src_w or not src_h:
        return Image.new("RGB", (box_w, box_h), (15, 9, 23))

    scale_cover = max(box_w / src_w, box_h / src_h)
    if mode == "cover":
        wide = source.resize((max(int(src_w * scale_cover), box_w),
                               max(int(src_h * scale_cover), box_h)),
                              Image.LANCZOS)
        left = (wide.width - box_w) // 2
        top = (wide.height - box_h) // 2
        canvas = wide.crop((left, top, left + box_w, top + box_h))
        return canvas.filter(ImageFilter.GaussianBlur(9)) if blur else canvas

    backdrop = source.resize((max(int(src_w * scale_cover), box_w),
                               max(int(src_h * scale_cover), box_h)),
                              Image.BILINEAR)
    left = (backdrop.width - box_w) // 2
    top = (backdrop.height - box_h) // 2
    canvas = backdrop.crop((left, top, left + box_w, top + box_h))
    canvas = canvas.filter(ImageFilter.GaussianBlur(14)).point(lambda v: int(v * 0.55))

    scale_fit = min(box_w / src_w, box_h / src_h)
    inner = source.resize((max(int(src_w * scale_fit), 1),
                            max(int(src_h * scale_fit), 1)), Image.LANCZOS)
    canvas.paste(inner, ((box_w - inner.width) // 2, (box_h - inner.height) // 2))
    return canvas.filter(ImageFilter.GaussianBlur(9)) if blur else canvas


def round_corners(image, radius: int, bg: str = "#161021"):
    """Give a frame softly rounded corners against the gallery surface."""
    rgb = tuple(int(bg.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, image.width - 1, image.height - 1), radius=radius, fill=255)
    base = Image.new("RGB", image.size, rgb)
    base.paste(image, (0, 0), mask)
    return base


def read_exact(stream, count: int, alive) -> bytes | None:
    """
    Read exactly `count` bytes from a pipe.

    A pipe hands back whatever happens to be buffered - typically 64 KB -
    so a single read() of a 300 KB video frame ALWAYS comes up short. Loop
    until the frame is whole, or the stream genuinely ends.
    """
    chunks = []
    remaining = count
    while remaining > 0:
        if not alive():
            return None
        try:
            piece = stream.read(remaining)
        except (OSError, ValueError):
            return None
        if not piece:
            return None
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)


# ─────────────────────────────────────────────────────────────────────────
#  Thumbnail / frame cache
# ─────────────────────────────────────────────────────────────────────────

class ThumbCache:
    """On-disk JPEG cache so scrubbing back over a frame is instant.

    Shared by the Convert inspector, the gallery's hover-scrub, the contact
    sheet and the duplicate finder - one cache instead of two nearly
    identical ones.
    """

    # Grid size for the hover-scrub storyboard - a fixed 30 cells is plenty
    # of granularity for anything from a few seconds to a long clip. Not to
    # be confused with cfg.filmstrip_frames / ScrubPreview's own filmstrip,
    # the row of individual thumbnails under Convert's timeline - this is
    # a separate, invisible sprite sheet purely for instant hover preview.
    # Cell width has to be at least as wide as the preview bubble it feeds
    # (PeekWindow.W = 380): a cell narrower than the bubble can only ever
    # be shown small, since fitting an image to a box never enlarges it.
    BOARD_COLS = 6
    BOARD_ROWS = 5
    BOARD_CELL_W = 384

    def __init__(self, limit: int = 30000, subdir: str = "paz_frames"):
        self.root = os.path.join(tempfile.gettempdir(), subdir)
        self.limit = limit
        self._lock = threading.Lock()
        self._count = 0
        try:
            os.makedirs(self.root, exist_ok=True)
        except OSError:
            self.root = ""
        # A few decoded sprite sheets kept in memory - hovering back and
        # forth over the *same* clip (the common case) then costs nothing
        # but a PIL crop, not even a disk read. Kept small deliberately:
        # each sheet is ~2300x1080 RGB, so this is tens of MB, not a
        # handful of KB.
        self._sprites: "OrderedDict[str, Image.Image]" = OrderedDict()
        self._sprites_lock = threading.Lock()
        self._SPRITE_LIMIT = 4
        # Paths whose sheet is being built right now, so a burst of hovers
        # over one clip kicks off exactly one build.
        self._building: set = set()

    def _key(self, path: str, pos: float, width: int) -> str:
        try:
            st = os.stat(path)
            stamp = f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            stamp = "0"
        raw = f"{os.path.normcase(path)}|{stamp}|{pos:.2f}|{width}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest() + ".jpg"

    # ── hover reel ───────────────────────────────────────────────────────
    #
    # A run of CONSECUTIVE frames, decoded in one pass, for playing a clip
    # inside a gallery tile. The storyboard above is the wrong tool for
    # that: its cells are seconds apart, so flipping them is a slideshow no
    # matter how fast you flip, and asking it for a frame it hasn't built
    # yet costs an ffmpeg seek *per frame* - which is what made the tile
    # preview stutter. One decode, real consecutive frames, play from
    # memory: that is what makes it look like video instead of a flipbook.

    REEL_FPS = 15
    REEL_SECONDS = 6.0

    def preview_reel(self, path: str, duration: float, width: int,
                      fps: int = REEL_FPS, seconds: float = REEL_SECONDS) -> list:
        """JPEG bytes for a contiguous run of frames, ready to flip at
        `fps`. Starts a little way in - the first moments of a clip are
        often a fade or a title card, which is a poor thing to preview."""
        if not path or duration <= 0 or not os.path.exists(path):
            return []
        start = duration * 0.08 if duration > 6 else 0.0
        span = min(seconds, max(duration - start, 0.5))
        cmd = [
            "ffmpeg", "-nostdin", "-v", "error",
            "-ss", f"{start:.3f}", "-i", path, "-t", f"{span:.3f}",
            "-an", "-sn",
            "-vf", f"fps={fps},scale={int(width)}:-2:flags=fast_bilinear",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "6", "-",
        ]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=25,
                                    creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0 or not result.stdout:
            return []
        return _split_mjpeg(result.stdout)

    def frame(self, path: str, pos: float, width: int = 640,
              fast: bool = False) -> bytes | None:
        """Return JPEG bytes for the frame at `pos` seconds, extracting if
        needed. `fast=True` tries only the quick input-side seek (a
        keyframe-snapped jump, no full decode from the start) and gives up
        immediately rather than falling back through slower attempts - for
        callers where a transient miss is fine because something else
        (the storyboard cache) will have an answer a moment later, and
        showing nothing now beats blocking on a slow decode."""
        if not os.path.exists(path):
            return None

        cached = None
        if self.root:
            cached = os.path.join(self.root, self._key(path, pos, width))
            if os.path.exists(cached):
                try:
                    with open(cached, "rb") as fh:
                        return fh.read()
                except OSError:
                    pass

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
        os.close(tmp_fd)
        try:
            attempts = []
            if pos > 0.05:
                attempts.append(["ffmpeg", "-y", "-ss", f"{pos:.3f}", "-i", path])
            if not fast:
                if pos > 0.05:
                    attempts.append(["ffmpeg", "-y", "-i", path, "-ss", f"{pos:.3f}"])
                attempts.append(["ffmpeg", "-y", "-i", path])
            elif not attempts:
                attempts.append(["ffmpeg", "-y", "-i", path])

            for head in attempts:
                cmd = head + [
                    "-frames:v", "1",
                    "-vf", f"scale={width}:-2:flags=bicubic",
                    "-q:v", "3", "-f", "image2", tmp_path,
                ]
                try:
                    subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=20,
                                   creationflags=NO_WINDOW)
                except (OSError, subprocess.SubprocessError):
                    continue
                if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                    with open(tmp_path, "rb") as fh:
                        data = fh.read()
                    if cached:
                        self._store(cached, data)
                    return data
            return None
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # ── storyboard: hover-scrub without spawning ffmpeg per hover ────────
    #
    # frame() above is a full ffmpeg spawn (seek + decode + scale + encode
    # + write) per call - fine for a one-off frame, but no amount of
    # debouncing or coalescing makes a ~100ms+ subprocess round trip per
    # mouse-move feel instant. YouTube's own hover scrub works because
    # it's indexing into a pre-built storyboard image, not re-decoding the
    # source on every hover. storyboard() does the same thing: one ffmpeg
    # pass tiles evenly-spaced thumbnails for the *whole* clip into a
    # single sprite sheet, cached on disk and in memory; hover_frame()
    # then just crops the nearest cell out of it - no subprocess involved
    # once the sprite exists.

    def _board_key(self, path: str, cols: int, rows: int, cell_w: int) -> str:
        try:
            st = os.stat(path)
            stamp = f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            stamp = "0"
        raw = f"board|{os.path.normcase(path)}|{stamp}|{cols}x{rows}|{cell_w}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest() + ".jpg"

    def _board_cached(self, path: str, cols: int, rows: int, cell_w: int):
        """The sheet if it's already in memory or on disk. Never runs
        ffmpeg, so it's safe to call on the hover path."""
        key = self._board_key(path, cols, rows, cell_w)
        with self._sprites_lock:
            hit = self._sprites.get(key)
            if hit is not None:
                self._sprites.move_to_end(key)
                return hit
        cached = os.path.join(self.root, key) if self.root else None
        if not (cached and os.path.exists(cached)):
            return None
        try:
            image = Image.open(cached)
            image.load()
        except Exception:
            return None
        self._board_remember(key, image)
        return image

    def _board_remember(self, key: str, image) -> None:
        with self._sprites_lock:
            self._sprites[key] = image
            self._sprites.move_to_end(key)
            while len(self._sprites) > self._SPRITE_LIMIT:
                self._sprites.popitem(last=False)

    def storyboard(self, path: str, duration: float, cols: int = BOARD_COLS,
                    rows: int = BOARD_ROWS, cell_w: int = BOARD_CELL_W):
        """Build (or fetch) the sprite sheet. Blocking - the ffmpeg pass
        can take a while on a long 4K clip, so hover paths should go
        through hover_frame() instead of calling this directly."""
        if not path or duration <= 0:
            return None
        hit = self._board_cached(path, cols, rows, cell_w)
        if hit is not None:
            return hit
        if not os.path.exists(path):
            return None

        key = self._board_key(path, cols, rows, cell_w)
        cached = os.path.join(self.root, key) if self.root else None
        n = cols * rows
        interval = max(duration / n, 0.1)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
        os.close(tmp_fd)
        try:
            vf = (f"fps=1/{interval:.4f},scale={cell_w}:-2:flags=bilinear,"
                  f"tile={cols}x{rows}")
            # -skip_frame nokey decodes only keyframes. Without it this is
            # a full decode of the entire file just to sample 30 frames,
            # which on a long 4K60 clip takes many seconds - that stall is
            # what made the first hover on a clip look like hovering was
            # simply broken. The fps filter still picks by timestamp, so
            # the cells stay evenly spaced across the clip; they just land
            # on the nearest keyframe, which is invisible at thumbnail
            # size and orders of magnitude cheaper.
            cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                   "-skip_frame", "nokey", "-i", path,
                   "-an", "-sn", "-frames:v", "1",
                   "-vf", vf, "-q:v", "4", tmp_path]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=90,
                               creationflags=NO_WINDOW)
            except (OSError, subprocess.SubprocessError):
                return None
            if not (os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0):
                return None
            try:
                image = Image.open(tmp_path)
                image.load()
            except Exception:
                return None
            if cached:
                try:
                    image.save(cached, "JPEG", quality=82)
                except OSError:
                    pass
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        self._board_remember(key, image)
        return image

    def _board_crop(self, sheet, frac: float, cols: int, rows: int) -> bytes | None:
        n = cols * rows
        index = max(0, min(int(max(0.0, min(frac, 1.0)) * n), n - 1))
        cw = sheet.width // cols
        ch = sheet.height // rows
        cx, cy = index % cols, index // cols
        box = (cx * cw, cy * ch, (cx + 1) * cw, (cy + 1) * ch)
        try:
            cell = sheet.crop(box)
            buf = io.BytesIO()
            cell.convert("RGB").save(buf, format="JPEG", quality=85)
            return buf.getvalue()
        except Exception:
            return None

    def hover_frame(self, path: str, duration: float, frac: float,
                     cols: int = BOARD_COLS, rows: int = BOARD_ROWS,
                     cell_w: int = BOARD_CELL_W) -> bytes | None:
        """The hover-scrub entry point: JPEG bytes for the moment `frac`
        (0..1) into the clip, as fast as possible.

        Once this clip's sheet exists, that's a pure in-memory crop. Until
        then, waiting on the sheet would stall the very first hover on
        every clip, so this falls back to one quick single-frame seek
        (fast - `-ss` before `-i` lands on a keyframe without decoding
        what came before) and builds the sheet in the background so every
        later hover on the same clip is instant.
        """
        if not path or duration <= 0:
            return None
        sheet = self._board_cached(path, cols, rows, cell_w)
        if sheet is not None:
            return self._board_crop(sheet, frac, cols, rows)
        self._board_build_async(path, duration, cols, rows, cell_w)
        # fast=True: one quick attempt, give up rather than fall back
        # through slower extraction - a miss here is invisible (nothing
        # painted this tick) whereas a slow fallback is exactly the drag
        # behind the cursor this whole cache exists to avoid.
        return self.frame(path, max(0.0, min(frac, 1.0)) * duration, cell_w, fast=True)

    def prime_hover(self, path: str, duration: float, cols: int = BOARD_COLS,
                     rows: int = BOARD_ROWS, cell_w: int = BOARD_CELL_W) -> None:
        """Start building this clip's storyboard sheet now, without
        waiting on it. Call this the moment a clip becomes the active one
        in a player (not on every gallery card - that would be dozens of
        concurrent ffmpeg passes) so the sheet is usually already done by
        the time the user actually reaches for the seek bar, instead of
        the first several seconds of scrubbing paying the slow per-hover
        fallback while the sheet is still mid-build."""
        if path and duration > 0 and self._board_cached(path, cols, rows, cell_w) is None:
            self._board_build_async(path, duration, cols, rows, cell_w)

    def _board_build_async(self, path: str, duration: float,
                            cols: int, rows: int, cell_w: int) -> None:
        """Build this clip's sheet once, off to the side. A burst of
        hovers over one clip must not start a burst of ffmpeg passes."""
        key = self._board_key(path, cols, rows, cell_w)
        with self._sprites_lock:
            if key in self._building:
                return
            self._building.add(key)

        def work():
            try:
                self.storyboard(path, duration, cols, rows, cell_w)
            finally:
                with self._sprites_lock:
                    self._building.discard(key)

        threading.Thread(target=work, daemon=True).start()

    def _store(self, dest: str, data: bytes) -> None:
        try:
            with open(dest, "wb") as fh:
                fh.write(data)
        except OSError:
            return
        with self._lock:
            self._count += 1
            if self._count > self.limit:
                self._count = 0
                self.trim()

    def trim(self) -> None:
        """Drop the oldest half when the cache grows past the limit."""
        try:
            entries = [(os.path.getmtime(os.path.join(self.root, n)),
                        os.path.join(self.root, n))
                       for n in os.listdir(self.root)]
        except OSError:
            return
        if len(entries) <= self.limit:
            return
        entries.sort()
        for _, path in entries[: len(entries) // 2]:
            try:
                os.remove(path)
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────────
#  Gallery thumbnails (persistent, one per library clip)
# ─────────────────────────────────────────────────────────────────────────

def thumb_key(path: str) -> str:
    return hashlib.sha1(os.path.normcase(path).encode("utf-8")).hexdigest() + ".jpg"


def _looks_blank(path: str) -> bool:
    """True for a frame that is essentially one flat colour (fade / black)."""
    try:
        with Image.open(path) as image:
            small = image.convert("L").resize((32, 32), Image.BILINEAR)
        pixels = list(small.getdata())
        low = min(pixels)
        high = max(pixels)
        mean = sum(pixels) / len(pixels)
        return (high - low) < 18 or mean < 12
    except Exception:
        return False


def make_thumb(path: str, duration: float, width: int) -> bool:
    """
    Write a representative gallery thumbnail into the persistent thumb dir.

    Several clips opened on a fade or a title card, which produced black
    tiles. Candidate positions are tried in turn and a frame that is
    basically one flat colour is rejected, so the grid shows the actual
    content rather than the intro.
    """
    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
    except OSError:
        return False
    dest = os.path.join(THUMB_DIR, thumb_key(path))

    spots = [duration * f for f in (0.35, 0.55, 0.20, 0.72, 0.05)] if duration > 1 else [0.0]

    fallback_written = False
    for index, pos in enumerate(spots):
        heads = []
        if pos > 0.05:
            heads.append(["ffmpeg", "-y", "-ss", f"{pos:.3f}", "-i", path])
        heads.append(["ffmpeg", "-y", "-i", path])
        for head in heads:
            cmd = head + ["-frames:v", "1",
                          "-vf", f"scale={width}:-2:flags=bicubic",
                          "-q:v", "3", "-f", "image2", dest]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=25,
                               creationflags=NO_WINDOW)
            except (OSError, subprocess.SubprocessError):
                continue
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                fallback_written = True
                if index == len(spots) - 1 or not _looks_blank(dest):
                    return True
                break
    return fallback_written


# ─────────────────────────────────────────────────────────────────────────
#  Perceptual hashing (duplicate finder)
# ─────────────────────────────────────────────────────────────────────────

def dhash(image) -> int:
    """64-bit difference hash for near-duplicate detection."""
    gray = image.convert("L").resize((9, 8), Image.LANCZOS)
    pixels = list(gray.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            left = pixels[row * 9 + col]
            right = pixels[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")
