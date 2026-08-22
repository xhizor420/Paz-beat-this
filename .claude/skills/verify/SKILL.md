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

Window is `PAZ Suite  1.0` at 1760x1020, positioned 0,0.

## Screenshot + drive

No ImageMagick/scrot; **ffmpeg x11grab** is the working capture path:

```bash
ffmpeg -y -f x11grab -video_size 1760x1020 -i :99+0,0 -frames:v 1 shot.png
ffmpeg -y -f x11grab -video_size 900x140  -i :99+0,180 -frames:v 1 crop.png  # region
```

Tab strip y=73: Convert x=50, Library x=125, Vault x=191, Beat This x=266.

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
