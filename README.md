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

### On Windows

Double-click **`PAZ Suite.pyw`** to start it with no console window.
Anything that would have gone to the console - including the details of
an error - is written to `%USERPROFILE%\.video_tool\paz_suite.log`.
Once it is running, right-click its taskbar button and **Pin to taskbar**:
it has its own taskbar identity, so the pin starts PAZ Suite, not Python.

On Windows the app also:

- keeps the PC from going to sleep while a conversion, a library sync or
  a tag fetch you started is running (the screen can still turn off);
- asks for 1 ms timers, so its redraws and animations are not rounded up
  to Windows' default 15.6 ms tick;
- runs the thread that draws the window a notch above normal priority,
  so clicks are answered first while ffmpeg or Topaz keep the CPU busy
  (background ffmpeg work - thumbnails, previews - already runs below
  normal).

**Worth doing once:** add `%USERPROFILE%\.video_tool` to Microsoft
Defender's exclusions (Windows Security → Virus & threat protection →
Manage settings → Exclusions → Add an exclusion → Folder). That folder
holds the app's own thumbnails, database and tag cache - thousands of
small files it writes and reads constantly - and Defender scanning each
one is the single biggest Windows-only slowdown the app cannot remove by
itself. Only exclude that folder, not your video library.

The tests run on Windows on every push (the **Windows tests** workflow
under the repository's Actions tab).

**For playback, install VLC.** The player uses the best backend it finds,
in this order:

| | needs | sound against picture |
|---|---|---|
| VLC | [VLC](https://videolan.org) + `python-vlc` | locked - VLC keeps one clock |
| mpv | `mpv` on PATH | locked |
| built-in | ffmpeg + `sounddevice` | locked - see below |
| built-in | ffmpeg alone | drifts; adjustable by hand |

VLC is the one to have. It is loaded into the program rather than run
beside it, so unlike mpv there is no connection between the two that can
fail to open - which is what makes it dependable on Windows. `python-vlc`
comes with `requirements.txt`; VLC itself is a normal install.

The built-in player used to decode video with ffmpeg and play sound with a
separate ffplay - two processes with no clock between them, so sound sat a
fixed distance from the picture for the life of the clip. With
`sounddevice` installed it drives the audio device itself, which means it
knows how much sound has actually reached the speakers, and it paces the
picture to that: frames are dropped or held to stay level with what is
being heard. Audio is the master because a gap in sound is audible and a
repeated frame is not.

Measured over a 24-second clip against a recording of the output: picture
stays within one frame of the sound and does not accumulate error
(-13 ms end to end), and the sound plays at 1000.21 ms per second with no
dropouts.

Without `sounddevice` it falls back to the old ffplay path, which drifts.
The player's **sync** button shifts it by ear there, 50 to 400 ms either
way, and remembers the setting; on every other path it says there is
nothing to set.

Which one is live, and why the others aren't, is under the player's
**sync** button → *Copy playback report*.

### Your own pictures

Right-click the header strip, or **Settings → Your pictures**. Three slots:

| | shape | where it shows |
|---|---|---|
| Header strip | 1760 × 76 or wider | across the top, every tab |
| Window icon | 256 × 256 | taskbar, window, alt-tab |
| Backdrop | 1920 × 1080 | under the clip grid, blurred and dimmed |

Each one says the size it wants, reports the resolution of whatever you
picked, and tells you in words what is about to be cropped. Drag the crop
to place it, scroll to pull in closer. The backdrop also has blur and dim
sliders - it is the ground the app sits on, so it is softened until it
reads as texture rather than as something to look at.

Nothing is copied: a slot stores where the picture lives plus a focal
point and a zoom, so the original file is untouched and the header
re-crops as the window resizes instead of stretching. Move or delete the
file and the slot simply empties.

Not slots, on purpose: behind the player and behind the thumbnails. Both
are covered by a clip seconds after the app opens.

Vault projects get a **cover picture** of their own (right-click a project
→ Cover picture) at 1280 × 720 - the still for the finished video, kept
with the project instead of loose in a folder, and shown against it in the
project list.

### If it freezes or crashes

The app writes its own post-mortem to `~/.video_tool/freeze.log`
(`C:\Users\<you>\.video_tool\freeze.log` on Windows).

A hung window is the one failure that normally leaves no evidence: nothing
crashed, so there is no traceback, and killing it from Task Manager throws
away the only copy of where it stopped. So a heartbeat runs on the thread
that draws the window, and a watcher notices when the beat stops and dumps
every thread's stack to that file. The thread marked `MAIN/UI` is the one
that is stuck and its top frame is the line to fix. Native crashes inside a
video or audio library, and errors thrown out of UI callbacks, land in the
same file.

If playback misbehaves: reproduce it once, then send that file.

### Playing keys

`,` and `.` step one frame - that is how you find the frame a beat lands
on, and it comes off the decoder already running, so holding it walks
through a clip rather than waiting on one. Space or Enter plays, arrows
seek five seconds, Shift+arrows one, Home and End are the ends of the
clip, a digit jumps that tenth of the way in, `M` mutes. All of them
defer to the search box, so typing a post ID still types.

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
  audio_out.py              audio device output, and the clock the picture follows
  vlc_player.py             libVLC playback, in-process, same surface as above
  mpv_player.py             mpv playback over a socket, same surface as above
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
