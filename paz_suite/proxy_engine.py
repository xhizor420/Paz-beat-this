"""Edit proxies for the 4K/60 pool: what needs one, and making one. No UI.

Every category folder in the pool gets a Proxy folder of its own beside the
masters - "Furry/Proxy/4182933.mov" for "Furry/4182933.mp4" - which is
where Resolve looks for a clip's proxy, by name. Keeping one per category
also means two categories holding the same file name cannot collide.

The recipe was picked by measuring it on this pool against the others
(ProRes Proxy, DNxHR LB, HEVC, smaller sizes) on the machine it runs on:

  * H.264 High on NVENC, 1920 on the long side, CQ 20. Within a VMAF point
    of CQ 16 at well under two thirds of the size; ProRes Proxy scored
    lower and was three times the size - more than the drive has free.
  * The same frames as the master, every one: no frame-rate conversion,
    no dropped or doubled frames, timestamps passed straight through. A
    proxy that is a frame out is worse than none - it shows the wrong
    picture at the cut.
  * Short GOP (half a second) and no B-frames, so a jump lands on a
    picture quickly when scrubbing.
  * The sound copied as it is - only the picture is made smaller.
  * Resolve Studio decodes it on the GPU.

A proxy is never left half-written under its real name. It is encoded to
"name.partial.mov", checked - frame count, frame rate, length and a full
decode - and only then renamed, so a crash, a Stop or a full drive leaves
a .partial file that the next scan deletes, never a broken proxy that looks
finished.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field

from .files import NO_WINDOW

# Bump when the recipe changes; proxies made by an older one are listed
# as such rather than silently mixed with new ones.
RECIPE = "h264-1920-cq20-v1"
RECIPE_LABEL = "H.264 · 1080p · CQ 20 · same frames as the 4K"

PROXY_DIR = "Proxy"
PROXY_EXT = ".mov"
PARTIAL_EXT = ".partial.mov"
MASTER_EXT = ".mp4"

LONG_SIDE = 1920
CQ = 20
# Audio a .mov takes as it is, so it is copied rather than re-encoded.
COPY_AUDIO = {"aac", "mp3", "alac", "pcm_s16le", "pcm_s24le", "pcm_f32le"}

# States an item can be in.
DONE = "done"          # a checked proxy, master unchanged since
NEEDED = "needed"      # no proxy yet
STALE = "stale"        # the master changed after its proxy was made
CHECK = "check"        # a proxy is there that this app has not checked
BAD = "bad"            # a proxy is there and it does not match its master
FAILED = "failed"      # the last attempt failed - see reason
WAITING = "waiting"    # the 4K file is too new - it may still be being written

TODO = (NEEDED, STALE, BAD, FAILED)

# The pool is live: Topaz and Convert drop new 4K files into it while a run
# goes on. A file written to in the last two minutes may not be finished,
# and a proxy of half a file would be a proxy of the wrong length - so it
# waits for a later scan.
SETTLE_SECONDS = 120
NOT_READY = "new - waiting until it has finished copying"


@dataclass
class Item:
    category: str
    master: str
    size: int
    mtime: int
    proxy: str
    state: str = NEEDED
    reason: str = ""
    proxy_size: int = 0
    seconds: float = 0.0          # length, when the library knows it

    @property
    def name(self) -> str:
        return os.path.basename(self.master)


def proxy_path(master: str) -> str:
    """Where the proxy for `master` lives: its folder's Proxy folder, same
    name, .mov."""
    folder, name = os.path.split(master)
    return os.path.join(folder, PROXY_DIR, os.path.splitext(name)[0] + PROXY_EXT)


def partial_path(target: str) -> str:
    """The name a proxy is written under until it has been checked."""
    return os.path.splitext(target)[0] + PARTIAL_EXT


# ── what needs a proxy ───────────────────────────────────────────────────

def arrived(stat) -> float:
    """When a file last changed or appeared here. A copy keeps the source's
    modified time, so a clip still being copied in can look days old by
    that alone - its creation time here is when it arrived."""
    born = getattr(stat, "st_birthtime", None)
    if born is None:
        born = stat.st_ctime if os.name == "nt" else 0.0
    return max(stat.st_mtime, born)


def scan(root: str, categories, known: dict, durations: dict | None = None,
         now: float | None = None) -> tuple:
    """Every master in these categories and where its proxy stands. Any
    thread; touches nothing but directory listings.

    `known` is what the proxies table remembers, by master path (see
    library_db.proxy_rows). Returns (items, partials, missing_dirs):
    leftover .partial files to delete, and categories that are not there.

    Two directory listings per category - the masters and their Proxy
    folder - and no probing. A proxy this app made and checked is trusted
    as long as neither it nor its master has changed size or date since;
    one it did not make is marked CHECK, for check_existing to look at.
    """
    durations = durations or {}
    now = time.time() if now is None else now
    items: list = []
    partials: list = []
    missing: list = []
    for category in categories:
        folder = os.path.join(root, category)
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
        except OSError:
            missing.append(folder)
            continue
        proxies: dict = {}
        try:
            for entry in os.scandir(os.path.join(folder, PROXY_DIR)):
                name = entry.name.lower()
                if name.endswith(PARTIAL_EXT):
                    partials.append(entry.path)
                elif name.endswith(PROXY_EXT):
                    proxies[os.path.normcase(entry.name)] = entry
        except OSError:
            pass                       # no Proxy folder yet: nothing made
        for entry in entries:
            if not entry.name.lower().endswith(MASTER_EXT):
                continue
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            stem = os.path.splitext(entry.name)[0]
            item = Item(category, entry.path, stat.st_size, int(stat.st_mtime),
                        proxy_path(entry.path),
                        seconds=float(durations.get(stem) or 0.0))
            found = proxies.get(os.path.normcase(stem + PROXY_EXT))
            row = known.get(entry.path)
            same_master = bool(row) and row.get("master_size") == item.size \
                and row.get("master_mtime") == item.mtime
            if found is not None:
                try:
                    item.proxy_size = found.stat().st_size
                except OSError:
                    item.proxy_size = 0
                if item.proxy_size <= 0:
                    item.state, item.reason = BAD, "empty file"
                elif row and row.get("status") == DONE \
                        and row.get("proxy_size") == item.proxy_size:
                    if same_master and row.get("recipe") == RECIPE:
                        item.state = DONE
                    elif same_master:
                        item.state, item.reason = STALE, "made with older settings"
                    else:
                        item.state, item.reason = STALE, "the 4K file changed since"
                elif row and row.get("status") == BAD and same_master \
                        and row.get("proxy_size") == item.proxy_size:
                    item.state, item.reason = BAD, row.get("reason") or ""
                else:
                    item.state = CHECK
            elif row and row.get("status") == FAILED and same_master:
                item.state, item.reason = FAILED, row.get("reason") or ""
            if item.state != DONE and now - arrived(stat) < SETTLE_SECONDS:
                item.state, item.reason = WAITING, NOT_READY
            items.append(item)
    return items, partials, missing


# ── probing ──────────────────────────────────────────────────────────────

@dataclass
class Stream:
    frames: int = 0
    rate: str = ""                # exact, as a fraction: "60/1"
    duration: float = 0.0
    width: int = 0
    height: int = 0
    codec: str = ""
    has_audio: bool = False
    audio_codec: str = ""
    # Whether `duration` is the video's own length. When the file does not
    # say, it is the whole file's - audio included, which can run past the
    # last picture - and is no measure of the video.
    video_duration: bool = False
    # Whether `frames` came from the file's index (exact) or had to be
    # counted from its packets (one can fail to decode).
    indexed: bool = True
    notes: list = field(default_factory=list)

    @property
    def fps(self) -> float:
        try:
            top, bottom = self.rate.split("/")
            return float(top) / float(bottom)
        except (ValueError, ZeroDivisionError):
            return 0.0


def _ffprobe(args: list, timeout: float) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", *args, "-of", "json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return {}
    try:
        return json.loads(result.stdout or "{}")
    except ValueError:
        return {}


def probe(path: str, count: bool = False, timeout: float = 60,
          decode: bool = False) -> Stream | None:
    """The first video stream's exact frame count and rate. The count comes
    from the container's index, which is instant; `count` reads every
    packet instead, for files whose index does not say; `decode` decodes
    every frame and counts those - the frames that will actually play."""
    args = ["-show_entries",
            "stream=codec_type,codec_name,width,height,r_frame_rate,nb_frames,"
            "nb_read_packets,nb_read_frames,duration:format=duration"]
    if count:
        args.append("-count_packets")
    if decode:
        args += ["-select_streams", "v:0", "-count_frames"]
    data = _ffprobe(args + [path], timeout)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return None
    sound = next((s for s in streams if s.get("codec_type") == "audio"), None)
    out = Stream(rate=video.get("r_frame_rate") or "", codec=video.get("codec_name") or "",
                 width=int(video.get("width") or 0), height=int(video.get("height") or 0),
                 has_audio=sound is not None,
                 audio_codec=(sound or {}).get("codec_name") or "")
    raw = (video.get("nb_read_frames") if decode
           else video.get("nb_read_packets") if count else video.get("nb_frames"))
    try:
        out.frames = int(raw)
    except (TypeError, ValueError):
        out.frames = 0
    out.indexed = not (count or decode)
    for source, own in ((video.get("duration"), True),
                        ((data.get("format") or {}).get("duration"), False)):
        try:
            out.duration = float(source)
            out.video_duration = own
            break
        except (TypeError, ValueError):
            continue
    return out


def master_stream(path: str) -> Stream | None:
    info = probe(path)
    if info is not None and info.frames <= 0:
        # No count in the index: read the packets, which is a pass over
        # the file but not a decode.
        counted = probe(path, count=True, timeout=600)
        if counted is not None:
            info.frames = counted.frames
            info.indexed = False
    return info


def decoded_frames(path: str) -> int:
    """Frames that actually decode - slow (a full decode), so only asked
    when a packet count and a proxy disagree. 0 if it cannot be told."""
    counted = probe(path, decode=True, timeout=1800)
    return counted.frames if counted is not None else 0


def matches(master: Stream, proxy: Stream | None) -> str:
    """"" when `proxy` has exactly the master's frames, else why not."""
    if proxy is None:
        return "could not read the proxy"
    if proxy.rate != master.rate:
        return f"frame rate {proxy.rate} - the 4K is {master.rate}"
    if not master.frames:
        # Nothing to hold the proxy to but its rate - not enough to call
        # one complete.
        return "could not count the 4K file's frames"
    if proxy.frames != master.frames:
        return f"{proxy.frames} frames - the 4K has {master.frames}"
    # Only against the video's own length: a file-wide length counts the
    # audio, and frames at the same rate already settle the video's.
    if master.video_duration and master.duration and proxy.duration and master.fps:
        if abs(proxy.duration - master.duration) > 1.5 / master.fps:
            return (f"{proxy.duration:.3f}s long - the 4K is "
                    f"{master.duration:.3f}s")
    return ""


def check_existing(item: Item) -> tuple:
    """A proxy this app did not make (or no longer remembers): does it match
    its master? (state, reason). Header reads only - fast."""
    master = master_stream(item.master)
    if master is None:
        return BAD, "could not read the 4K file"
    why = matches(master, probe(item.proxy))
    return (BAD, why) if why else (DONE, "")


# ── making one ───────────────────────────────────────────────────────────

def scale_filter() -> str:
    """Long side to 1920, the other in proportion and even, never upscaled."""
    n = LONG_SIDE
    return (f"scale='if(gte(iw,ih),min(iw,{n}),-2)':'if(gte(iw,ih),-2,min(ih,{n}))'"
            ":flags=lanczos")


def build_command(master: str, target: str, info: Stream) -> list:
    gop = max(1, int(round(info.fps / 2))) if info.fps else 30
    cmd = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats",
           # Decoded on the GPU where it can be; ffmpeg falls back to the
           # CPU on its own for anything NVDEC cannot take.
           "-hwaccel", "cuda",
           "-i", master,
           "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", scale_filter(),
           # Every frame, at its own time. No rate conversion of any kind.
           "-fps_mode", "passthrough",
           "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
           "-profile:v", "high", "-rc", "vbr", "-cq", str(CQ), "-b:v", "0",
           # Without a ceiling NVENC applies one of its own and the CQ
           # stops deciding - see convert_engine.video_args.
           "-maxrate", "80M", "-bufsize", "160M",
           "-g", str(gop), "-bf", "0", "-spatial-aq", "1",
           "-pix_fmt", "yuv420p"]
    if info.has_audio:
        # The sound exactly as it is in the 4K file. Only a codec a .mov
        # cannot hold is re-encoded, and then at a rate nobody would hear.
        if info.audio_codec in COPY_AUDIO:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "320k"]
    cmd += ["-movflags", "+faststart", target]
    return cmd


def _signature(path: str):
    """(size, mtime) of a file, or None if it is not there."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_size, stat.st_mtime


def _discard(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def make_proxy(item: Item, cfg, progress_cb=None, cancel=None) -> tuple:
    """Make and check one proxy. (ok, reason, proxy_stream).

    Raises convert_engine.Cancelled when `cancel` is set; the partial file
    is gone either way."""
    from .convert_engine import Cancelled, Unverifiable, run_ffmpeg, verify, verify_budget

    # The pool is live: look at the file again now rather than trusting
    # the scan, which may be hours old by the time this one's turn comes.
    before = _signature(item.master)
    if before is None:
        return False, "the 4K file is gone", None
    try:
        landed = arrived(os.stat(item.master))
    except OSError:
        return False, "the 4K file is gone", None
    if time.time() - landed < SETTLE_SECONDS:
        return False, NOT_READY, None
    item.size, item.mtime = before[0], int(before[1])

    master = master_stream(item.master)
    if master is None or not master.rate:
        return False, "could not read the 4K file", None
    target = item.proxy
    partial = partial_path(target)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
    except OSError as exc:
        return False, f"could not make the Proxy folder: {exc}", None
    _discard(partial)

    cmd = build_command(item.master, partial, master)
    try:
        ok, error = run_ffmpeg(cmd, master.duration, cfg, progress_cb, cancel)
    except Cancelled:
        _discard(partial)
        raise
    if not ok:
        _discard(partial)
        return False, error or "the encode failed", None

    made = probe(partial)
    why = matches(master, made)
    if why and made is not None and not master.indexed and made.rate == master.rate:
        # The 4K count was of packets, and a packet can fail to decode -
        # then it is not a frame anyone will see. Count what really plays.
        real = decoded_frames(item.master)
        if real:
            master.frames = real
            why = matches(master, made)
    if why:
        _discard(partial)
        return False, why, made
    try:
        good, bad_why = verify(partial, verify_budget(master.duration))
    except Unverifiable as exc:
        _discard(partial)
        return False, f"could not check it: {exc}", made
    if not good:
        _discard(partial)
        return False, f"does not decode cleanly: {bad_why}", made
    if cancel is not None and cancel.is_set():
        _discard(partial)
        raise Cancelled()
    if _signature(item.master) != before:
        _discard(partial)
        return False, "the 4K file changed while its proxy was being made", made
    try:
        os.replace(partial, target)
    except OSError as exc:
        _discard(partial)
        return False, f"could not put it in place: {exc}", made
    return True, "", made


def clean_partials(paths) -> int:
    """Delete leftovers of encodes that never finished."""
    gone = 0
    for path in paths:
        if path.lower().endswith(PARTIAL_EXT):
            try:
                os.remove(path)
                gone += 1
            except OSError:
                pass
    return gone


def record_fields(item: Item, state: str, reason: str = "", made: Stream | None = None) -> dict:
    """The proxies-table row for `item` in `state`."""
    try:
        proxy_size = os.path.getsize(item.proxy) if state in (DONE, BAD) else 0
    except OSError:
        proxy_size = 0
    return {"proxy": item.proxy, "master_size": item.size, "master_mtime": item.mtime,
            "proxy_size": proxy_size, "status": state, "reason": reason,
            "recipe": RECIPE if state == DONE else "",
            "frames": made.frames if made else 0, "rate": made.rate if made else "",
            "made_at": int(time.time())}
