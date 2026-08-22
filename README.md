# PAZ Suite

Personal video pipeline: batch-convert clips to 4K/60, browse and search
the library, track what's already used in a project, and pull beat
markers out of a song for the edit. Four tabs, one app.

- **Convert** — watches a source folder, encodes to MP4 (GPU with CPU
  fallback), sorts output by resolution/fps into a 4K 60+ pool vs. a
  needs-work folder.
- **Library** — browse/search everything by artist/character/species/
  rating/tag, play clips (defaults to the 4K/60 copy when one exists),
  fix missing metadata, verify integrity.
- **Vault** — paste a list of post IDs or filenames, find them in the
  library, mark the ones used in a named project so they're visibly
  flagged later instead of getting mixed back into the unused pool.
- **Beat This** — pick a song, run the [Beat This!](https://github.com/CPJKU/beat_this)
  neural beat tracker on it, and export the beats/downbeats as markers
  for DaVinci Resolve (or a plain `.beats` file). See below.

## Setup

Python 3.9+, plus `ffmpeg`/`ffprobe`/`ffplay` on PATH.

```
pip install -r requirements.txt
python main.py
```

First launch: **Settings → Convert Folders** to point Source / Converted /
4K 60+ / Needs work at your real folders. Library indexes Convert's
"Converted" folder by default — change that under **Settings → Library**
if it lives elsewhere.

Convert, Library and Vault only need the dependencies above. Beat This
needs its own, heavier set — see the next section — and its tab just
shows an "install these" message and stays disabled until they're
present, so the rest of the suite runs fine without them.

## Beat This: song → Resolve markers

The **Beat This** tab wraps the vendored [`beat_this/`](beat_this/)
checkout (the CPJKU beat tracker, ISMIR 2024) for one job: upload a song,
get beat/downbeat markers you can bring into a DaVinci Resolve timeline.

### Install

The tab's **Setup** panel lists what's present and installs what isn't —
press **Install dependencies**, then restart the app. **Download model**
fetches the checkpoint ahead of time so the first analysis isn't a long
silent wait. Equivalent by hand:

```
pip install torch torchaudio einops rotary-embedding-torch soxr numpy
```

Note this deliberately doesn't use `beat_this/requirements.txt` — those
pins (`torch==2.3.1` and friends) have no wheels for current Python
versions and fail outright on a new interpreter. If PyTorch won't install,
get the right build for your machine from
[pytorch.org](https://pytorch.org/get-started/locally/).

Audio is decoded with `ffmpeg` (already required above) rather than
torchaudio, so anything ffmpeg opens works — mp3, wav, flac, m4a, ogg,
or the audio track of a video file. This matters: recent torchaudio
delegates `load()` to TorchCodec and raises `Could not load audio from
"…"` on an ordinary MP3 when it isn't installed.

DBN postprocessing is optional and needs
`pip install git+https://github.com/CPJKU/madmom.git` on top.

### Using it

1. Browse to a song, pick a model checkpoint (`final0` is the default;
   `small*` is faster/lighter) and a device, then **Analyze** (or F5).
   The first run per checkpoint downloads it and is slower.
2. The table lists every beat: its time and its position in the bar
   (1 = downbeat).
3. Export:
   - **Save .beats** — the plain `time<TAB>beat number` format `beat_this`
     and Sonic Visualiser already read.
   - **Save EDL for Resolve** — a CMX3600 EDL with one marker per beat.
     Import with **Timeline → Import → Timeline Markers from EDL**.
     Markers land at record timecode = time into the song, counting from
     `00:00:00:00` — put the song at the very start of a timeline (or a
     fresh one) before importing, since EDL marker import always uses
     absolute record position, not the clip's own position. Pick the
     frame rate matching your timeline first; timecode is non-drop-frame.
   - **Send to Resolve now** — skips the file and adds markers straight
     to Resolve's current timeline via its scripting API. Only works run
     on the same machine as Resolve, with Resolve open and
     **Preferences → General → External scripting using** set to Local
     (or Network), plus `RESOLVE_SCRIPT_API` / `RESOLVE_SCRIPT_LIB` /
     `PYTHONPATH` set per Resolve's own Developer/Scripting README.
     Unlike the EDL, this reads the timeline's own frame rate and start
     frame, so the clip doesn't need to sit at timeline zero.

## Search syntax (Library)

`artist:` `character:` `species:` `rating:` `folder:` `id:` `is:` `used:` as
prefixes, `-term` to exclude, `*` wildcards. `is:untagged`, `is:noid`,
`is:4k`/`is:no4k`, `is:portrait`/`is:widescreen`/`is:square`, and
`used:"project name"` (quoted for names with spaces) are the useful
specials. In-app Help (the tab's Help button) covers the rest.

## Layout

```
main.py                     entry point
paz_suite/
  config.py                 AppConfig (+ legacy migration)
  theme.py, format.py       palette/fonts, human-readable formatting
  uithread.py               safe hand-off from worker threads to the UI
  files.py                  proxy-folder filtering, post-ID parsing, open/reveal
  e621.py                   e621 tag lookup + cache
  media.py                  ffprobe, thumbnailing, frame/storyboard cache, dhash
  player_engine.py          shared ffmpeg-decode + ffplay-audio playback engine
  widgets.py                Card/Bar/StatTile/PeekWindow/Toaster/JobPanel/LogView
  convert_engine.py         encode planning + ffmpeg command/run/verify (no UI)
  convert_widgets.py        queue table, scrub/play preview, contact sheet, dupe finder
  convert_tab.py            the Convert tab
  library_db.py             SQLite schema, search parser, Vault mark helpers (no UI)
  library_player.py         the embedded clip player
  library_windows.py        hidden tags, help, folders, integrity verifier
  library_tab.py            the Library tab
  vault_tab.py              the Vault tab
  beat_engine.py            Beat This inference, BPM/TSV/EDL, Resolve scripting (no UI)
  beat_tab.py               the Beat This tab
  settings_window.py        the settings dialog
  app.py                    window shell, tab switcher, keyboard dispatch
beat_this/                  vendored CPJKU/beat_this checkout (the beat tracker itself)
```

## Note

Developed without a display/Tk available, so changes are validated with
`py_compile`/`pyflakes` and standalone logic tests, not by actually
running the GUI. Smoke-test after pulling changes.
