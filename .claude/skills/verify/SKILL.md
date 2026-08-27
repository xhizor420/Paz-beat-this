---
name: verify
description: Build, launch and drive PAZ Suite (Tkinter GUI) headlessly to observe a change actually running, and render the design canvas. Use when verifying changes to paz_suite/ or design/.
---

# Verifying PAZ Suite

The app is a **Tkinter GUI**, so the surface is pixels. `py_compile` and
`pyflakes` pass on code that crashes at startup — they missed a
module-scope `import numpy` that took the whole app down. Launch it.

## Gotcha: which Python

The default `python3` (`/usr/local/bin/python3`, 3.11) has **no tkinter**.
Use `/usr/bin/python3.12`, which does:

```bash
/usr/bin/python3.12 -m pip install --break-system-packages customtkinter Pillow
```

Beat This deps (only needed to drive analysis) — heavy, several minutes:

```bash
/usr/bin/python3.12 -m pip install --break-system-packages \
    torch torchaudio einops rotary-embedding-torch soxr numpy
```

## Launch

`Xvfb` must be its own persistent background process — it dies with the
shell that spawned it if backgrounded with `&`.

```bash
apt-get install -y python3-tk xvfb x11-apps xdotool ffmpeg   # ffmpeg gives ffprobe
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp    # run_in_background
until DISPLAY=:99 xdpyinfo >/dev/null 2>&1; do sleep 1; done
DISPLAY=:99 /usr/bin/python3.12 main.py          # run_in_background
```

Window is `PAZ Suite  0.8 beta`, positioned 0,0. Its **size follows the
display** (~78% of screen width, floor 1760x1020, clamped to fit), so a
1920x1080 Xvfb gives 1760x960 and a 3840x2160 one gives ~2992x1780. Ask
rather than assume:

```bash
DISPLAY=:99 xdotool search --name "PAZ Suite" | while read w; do
  xdotool getwindowgeometry --shell $w; done
```

The UI also **scales itself** from the display: `Auto` picks 1.5x at 4K
and 1.25x at 1440p even when the desktop reports no DPI scaling, so every
coordinate below is 1080p-only. Pin it with `"ui_scale": "100%"` in the
config if you want fixed coordinates on a big virtual screen.

## Screenshot + drive

No ImageMagick/scrot; **ffmpeg x11grab** is the working capture path:

```bash
ffmpeg -y -f x11grab -video_size 1760x960 -i :99+0,0 -frames:v 1 shot.png
ffmpeg -y -f x11grab -video_size 900x140  -i :99+0,180 -frames:v 1 crop.png  # region
```

At 1080p / scale 1.0, tab strip y=99: Convert x=74, Library x=152,
Vault x=277, Beat This x=383. Gallery cards start x=325 y=265, each
274px wide and 215px tall, so card centres are (473|779|1085, 375|590).

**Menus are the fiddly part.** `tk_popup` coordinates are easy to miss by
a few pixels, and a missed click looks exactly like a feature that did
nothing — several "the guard didn't fire" dead ends here were really just
that. Prefer a keyboard route where one exists, and confirm the menu is
open with a screenshot before clicking an item. F5 = sync (Library) /
scan (Convert) / look up (Vault) / analyze (Beat This).

```bash
DISPLAY=:99 xdotool mousemove 266 73 click 1     # switch tab
DISPLAY=:99 xdotool key F5                       # per-tab action
```

## Give it real data

Empty config leaves the census/thumbnail threads with nothing to do, so
the interesting worker-thread paths never run. Generate real clips:

```bash
ffmpeg -f lavfi -i "testsrc2=size=1920x1080:rate=30:duration=2" \
       -f lavfi -i "sine=frequency=440:duration=2" \
       -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest 4482910.mp4
```

Then write `~/.video_tool/paz_config.json` with `library_root` /
`output_root` pointing at them, plus `"e621_enabled": false` (no egress
to e621 here) and `"last_tab"` to land on the tab under test.
`"beat_last_audio_dir"` makes the Beat This file dialog open there.

**Library rendering thumbnails is the proof the UI dispatcher works** —
those are placed by `uithread.post()` from a worker thread. Blank tiles
or `RuntimeError: main thread is not in main loop` on stderr means it
regressed.

Ignore this pre-existing stderr noise, it is not the app:
`ModuleNotFoundError: No module named '_distutils_hack'`.

## Design canvas (design/)

Re-seed then render. Headless `--virtual-time-budget` leaves artboards
stuck on "Loading artboard…" — it cuts off async iframe mounting. Use a
**real browser window on Xvfb with real wall-clock time**:

```bash
python3 -m http.server 8899 --bind 127.0.0.1        # file:// also fails
DISPLAY=:98 /opt/pw-browsers/chromium-1194/chrome-linux/chrome \
  --no-sandbox --disable-gpu --kiosk --window-size=1920,1200 \
  "http://127.0.0.1:8899/paz-neon-den.html"
sleep 30 && ffmpeg -y -f x11grab -video_size 1920x1200 -i :98+0,0 -frames:v 1 canvas.png
```

Click an artboard's ▷ icon to expand it to full resolution.

## Known environment limits

- The beat_this checkpoint host (`cloud.cp.jku.at`) is **blocked by the
  egress proxy**, so a full analysis always fails at `Could not load the
  checkpoint`. Everything up to that point (ffmpeg decode, model
  construction) is still verifiable, and reaching that error is itself
  evidence the audio path succeeded.
- No CUDA; device resolves to cpu.


## Driving in-process (no mainloop) — read this before believing a result

Constructing `PazApp` under `root.update()` instead of `root.mainloop()`
**silently swallows every worker-thread callback**: `uithread.post()`
parks them until the main loop is actually running, so status text never
updates, toasts never appear and anything posted from a thread looks like
it never ran. It is fine for timing synchronous work (search, render,
library load) and useless for observing anything a background thread
reports. For that, launch the real app and drive it with xdotool.

One Tk root per process, too — creating and destroying several aborts the
interpreter partway through, because PhotoImages belonging to a destroyed
interpreter free themselves by calling into it. `tests/test_player_engine.py`
shares a module-scoped root for exactly this reason.

## Measuring at a realistic size

Six clips hide everything. Two of the worst problems found so far - a
600ms tag-sidebar rebuild inside every search, and a 1.4s full library
re-read after fetching one clip's tags - only appeared against a
synthetic 11,000-clip library. Seed one by writing rows straight into
`files` and a matching `e621_meta.json`, back up the real ones first, and
put them back afterwards:

```python
conn = db_connect(); conn.execute("DELETE FROM files")
conn.executemany("INSERT INTO files (path,name,folder,pid,size,mtime,"
                 "duration,width,height,fps) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
```

## Destructive paths worth testing deliberately

Sync deletes rows and thumbnail files. To check the guard, point
`library_root` at a folder that exists but is empty (a re-mounted drive),
press F5, and confirm the clip count is unchanged and a red toast
explains why. A root that does not exist at all is caught earlier, by
`_sync`, which opens the Folders dialog instead.

## The player has two backends

`player_backend` in the config picks: `auto` uses mpv when installed,
`builtin` forces the ffmpeg-pipe engine. Test both — they are separate
code paths and the app has to work with no mpv at all.

```bash
apt-get install -y mpv          # not present by default in this container
```

**Xvfb has no OpenGL**, so mpv's default `gpu` output comes up as a black
rectangle that still reports a running clock — playback looks broken and
the stall detector will not catch it, because the clock *is* moving. Set
`"player_mpv_vo": "x11"` in the config when testing here. On a real
desktop leave it empty.

mpv attaches to a Tk widget's `winfo_id()`, so that widget must already
have real geometry before the engine is built, and must never be
unmapped afterwards — `place_forget()` on it takes the drawable out from
under mpv. Stack the surfaces with `lift()` instead. Note that
`Canvas.lift` is `tag_raise` (it raises canvas *items*); use
`tk.Misc.lift(widget)` to restack the widget itself.
