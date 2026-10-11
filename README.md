# PAZ Suite

**A personal video pipeline for making PMVs** - grab new clips, convert
them to an edit-ready format, get them to true 4K 60, browse and search
the whole library, keep track of what each project used, and turn a song
into beat markers for DaVinci Resolve. Four tabs, one app, Windows first.

| Tab | What it is for |
|---|---|
| **Convert** | Grab new favorites, encode to MP4 on the GPU, sort into the 4K 60 pool or the needs-work folder, and hand what needs it to Topaz |
| **Library** | Browse, search and play everything - by artist, character, species, rating, tag, category |
| **Vault** | Projects: which clips each one used, and what it is made of |
| **Beat This** | Song → beats and downbeats → markers in Resolve |

---

## Contents

- [Quick start](#quick-start)
- [The workflow](#the-workflow)
- [Convert](#convert)
- [Library](#library)
- [Vault](#vault)
- [Beat This](#beat-this)
- [Playback](#playback)
- [Keyboard](#keyboard)
- [Search syntax](#search-syntax)
- [Settings](#settings)
- [Troubleshooting](#troubleshooting)
- [Where things are kept](#where-things-are-kept)
- [Development](#development)
- [Credits](#credits)

---

## Quick start

**Needs:** Python 3.12+ and `ffmpeg` / `ffprobe` on PATH.

```
pip install -r requirements.txt
python main.py
```

On Windows, double-click **`PAZ Suite.pyw`** to start it without a
console, then right-click its taskbar button → **Pin to taskbar** (it has
its own taskbar identity, so the pin starts PAZ Suite, not Python).

**First launch:** open **Settings → Folders** and point Source, Converted,
4K 60+ and Needs work at your real folders. The Library indexes the
Converted folder unless you choose others under **Settings → Library**.

For the best player, also install **libmpv** - see [Playback](#playback).

---

## The workflow

```
 Grabber ──► Convert ──► sorted ──► 4K 60 pool ───────────► Library ──► Vault
 (Grab new)  (GPU encode)  │                                  │
                           └► needs work ──► Topaz ──► 4K 60 ─┘
                                (Needs 4K 60 window)

 Song ──► Beat This ──► markers ──► DaVinci Resolve ◄── clips (→ Resolve)
```

A clip counts as **4K 60** only when its shorter side is at least the
Minimum short side (2160) **and** its frame rate is at least the Minimum
fps (60) - exactly. 59.94 is not 60: in an edit cut to 60 it drifts
against the music, so it goes to Topaz for real interpolation instead of
being rounded up.

---

## Convert

- **Grab new** runs [Grabber](https://github.com/Bionus/imgbrd-grabber)
  on the search it already has open, using its own folder rules and its
  own "already downloaded" list. A strip under the tab shows progress,
  says where each video went (*Grabbed 3 new · MLP Futa 2 · Furry 1*), has
  a **Stop** button, and tucks itself away when it is done. The Convert log
  lists every file and its folder.
- **Scan folders / Start** encode on the GPU (NVENC) with a CPU fallback,
  keep frame rates exactly as they are, and sort each result into the 4K 60
  pool or the needs-work folder.
- **Find upscale gaps** opens the **Needs 4K 60** window:
  - every clip that still needs work, in three sections by what Topaz has
    to do - **Needs 60 fps**, **Needs 4K**, **Needs 4K and 60** - then by
    shape (16:9, 9:16, 1:1…), then by resolution, highest frame rate first;
  - every heading folds, and its **Select all** takes that whole batch - one
    set of Topaz settings - in one click;
  - **Copy** (Ctrl+C) puts the files on the clipboard like Explorer does,
    to paste into Topaz; or drag them straight onto it;
  - it is its own window, so clicking it never pulls the main window over
    Topaz.
- **Fetch e621 tags**, the duplicate finder, verify and reports are under
  the tab's buttons and **More**.

---

## Library

- **Cards are just the picture.** The border colour is the category -
  Furry, Furry Futa, MLP, MLP Futa - with a key beside the clip count.
  Resolution and fps, length, rating and the project a clip was used in
  sit on the picture as small badges. Gallery tile width is in Settings.
- **Hover a card** to scrub through it, with a larger preview beside the
  pointer. Rest on it and a short reel plays.
- **Click** plays - from the spot you were hovering. **Up / Down** walk the
  results and always start each clip from the beginning.
- **Search** by any tag; save searches as chips; sort by score, newest,
  length, or **Like this** - everything ranked by how much it shares with
  the selected clip.
- The tag sidebar (collapsible) shows what is in the current results.
- Right-click a card for play, folder, e621, fetch tags, add to Resolve,
  mark as used and copy.
- **Grid** shows a contact sheet of the selected clip; click a frame to play
  from it.

---

## Vault

- **Projects** list the clips each one used; pick a clip to play it right
  there. **Open in Library** shows the project as a search.
- **Overview** (next to Clips) shows what a project is made of: clips,
  total length and size, how many are real 60 fps / 4K / 4K 60, the
  category mix, the **tags its clips share**, the tags that **go together**
  on the same clips, and the artists used most.
  - Tags most of your library carries are left out on their own.
    Right-click any other tag to **hide it here** - only this screen.
  - Click a tag or a pair to show just those clips.
- **Import IDs…** opens the paste panel: paste post IDs or filenames, look
  them up, and mark the ones you used in a project.
- Each project can have its own **cover picture** (right-click a project).

---

## Beat This

Runs the [Beat This!](https://github.com/CPJKU/beat_this) neural beat
tracker (CPJKU, ISMIR 2024, vendored in [`beat_this/`](beat_this/)) on a
song and exports the beats and downbeats:

| Export | Use |
|---|---|
| **Save .beats** | plain `time<TAB>beat` text, as beat_this and Sonic Visualiser read it |
| **Save EDL for Resolve** | one marker per beat; **Timeline → Import → Timeline Markers from EDL**, with the song at the start of the timeline |
| **Send to Resolve now** | adds the markers to Resolve's open timeline directly, using its own frame rate and start |

**Install:** the tab's Setup panel installs what is missing (then restart),
and **Download model** fetches the model ahead of the first run. By hand:

```
pip install torch torchaudio einops rotary-embedding-torch soxr numpy
```

If PyTorch will not install, get the build for your machine from
[pytorch.org](https://pytorch.org/get-started/locally/). Audio is read
with ffmpeg, so anything ffmpeg opens works - mp3, wav, flac, m4a, or a
video's sound. *Send to Resolve now* needs Resolve open with
**Preferences → General → External scripting** set to Local.

The rest of the app runs fine without any of this installed.

---

## Playback

The player uses the best engine it finds, in this order (change it under
**Settings → Player**):

| Engine | Needs | Notes |
|---|---|---|
| **libmpv** | `libmpv-2.dll` + `python-mpv` | the one to have: a new clip on screen in about 0.1 s, GPU decoding, no dropped frames at 4K 60 |
| VLC | [VLC](https://videolan.org) + `python-vlc` | in-process, sound locked to picture |
| mpv | `mpv` on PATH | runs beside the app |
| built-in | ffmpeg + `sounddevice` | picture follows the sound device's clock |

**libmpv:** take `libmpv-2.dll` from an `mpv-dev-x86_64-….7z` build at
[shinchiro/mpv-winbuild-cmake](https://github.com/shinchiro/mpv-winbuild-cmake/releases)
and put it in `%LOCALAPPDATA%\PAZ Suite\mpv\` (or point `PAZ_LIBMPV_DIR`
at its folder). `python-mpv` comes with `requirements.txt`.

**Looping** is on by default and seamless: on libmpv the end runs into the
start one frame later, on VLC about 3 ms later.

Which engine is live, and why the others are not, is under the player's
settings button → *Copy playback report*.

---

## Keyboard

| Keys | Does |
|---|---|
| Space / Enter | play / pause |
| ← → | seek 5 s (Shift: 1 s) |
| `,` `.` | one frame back / forward |
| Home / End | start / end of the clip |
| 0-9 | jump to that tenth of the clip |
| M | mute |
| ↑ ↓ | previous / next clip (Library, Vault) |
| F5 | the tab's main action - scan, sync, look up, analyze |
| Ctrl+C | copy (Needs 4K 60 window: the selected clips' files) |
| F1 | every shortcut, for the tab you are on |

All of them leave a text box alone, so typing a post ID still types.

---

## Search syntax

| | |
|---|---|
| `tag` / `-tag` | has / has not |
| `artist:` `character:` `species:` `rating:` `folder:` `id:` | one kind of tag |
| `*` | wildcard |
| `used:"project name"` | clips used in a project |
| `is:4k` `is:no4k` | 4K, or has a 4K copy - or not |
| `is:portrait` `is:widescreen` `is:square` | shape |
| `is:untagged` `is:noid` | missing tags / no post ID |

The Library's Help button covers the rest.

---

## Settings

| Tab | What is in it |
|---|---|
| Folders | source, converted, 4K 60+, needs work, reports; categories |
| Encoding | codec, GPU, quality, keyframes, audio |
| Sorting | Minimum short side, Minimum fps (exact), copy / move / hardlink |
| Library | indexed folders, thumbnails, tile width, hover preview, tag fetching |
| Player | loop, theater, 4K copy, which engine |
| App | e621 account, watch mode, display scale, your pictures, Beat This model |

**Your pictures:** a header strip, a window icon and a blurred backdrop
under the gallery - each crops itself to the size it needs, and the
original file is never touched.

---

## Troubleshooting

**The app froze or crashed.** It writes its own post-mortem to
`%USERPROFILE%\.video_tool\freeze.log`: stalls, hangs (with every thread's
stack - the one marked `MAIN/UI` is the stuck one) and errors from inside
the window. Reproduce it once and send that file.

**Topaz says a file "doesn't exist".** The file is fine - Topaz opens each
clip with a GPU decoder, and while it is rendering it can hold nearly all
of the graphics card's memory, so a clip added then fails to open. Add the
next batch when the current one is done.

**It is slow on Windows.** Add `%USERPROFILE%\.video_tool` - only that
folder, not the library - to Microsoft Defender's exclusions (Windows
Security → Virus & threat protection → Manage settings → Exclusions). It
holds thousands of small thumbnail and cache files the app reads all the
time, and Defender scanning each one is the biggest slowdown the app
cannot remove by itself.

**Sound and picture drift.** Install libmpv or VLC (see
[Playback](#playback)); the player's report says which engine is in use.

While it runs, the app keeps Windows awake for a conversion, sync or tag
fetch you started (the screen can still turn off), asks for 1 ms timers,
and runs its window thread a notch above normal priority so clicks are
answered first while ffmpeg or Topaz are busy.

---

## Where things are kept

| | |
|---|---|
| `%USERPROFILE%\.video_tool\paz_config.json` | settings |
| `%USERPROFILE%\.video_tool\paz_library.sqlite3` | the library index and Vault projects |
| `%USERPROFILE%\.video_tool\e621_meta.sqlite3` | cached e621 tags |
| `%USERPROFILE%\.video_tool\paz_thumbs\` | gallery thumbnails |
| `%USERPROFILE%\.video_tool\freeze.log` | stalls, hangs and errors |
| `%USERPROFILE%\.video_tool\paz_suite.log` | console output when started without one |
| `%LOCALAPPDATA%\PAZ Suite\mpv\` | `libmpv-2.dll` |

---

## Development

```
pip install pytest pyflakes ruff
python -m pytest tests -q
python -m pyflakes paz_suite tools
python -m ruff check paz_suite tests
```

Every push runs three checks on GitHub (the **Actions** tab): unit tests
and lint on Windows with Python 3.14, the same on Python 3.12 with a
minimal install, and a **smoke run** that starts the real app, visits
every tab and screenshots it. A failing test is posted as an annotation on
the run page, so the reason is readable without the log.

<details>
<summary>Code layout</summary>

```
main.py, PAZ Suite.pyw      entry points (console / no console)
paz_suite/
  app.py                    window shell, tab switching, keyboard dispatch
  config.py                 settings (+ upgrades from older versions)
  theme.py, format.py       palette, fonts, sizes; human-readable numbers
  widgets.py                shared widgets: menus, dialogs, previews, logs
  uithread.py               hand-off from worker threads to the window
  watchdog.py               freeze and crash post-mortems
  winsys.py                 Windows bits: timers, priority, keep-awake, fonts

  convert_engine.py         encode plans, ffmpeg, sorting rules (no UI)
  convert_tab.py            the Convert tab
  convert_widgets.py        queue table, preview, contact sheet, duplicates
  grabber.py                runs Grabber and reads its progress
  upscale_window.py         the Needs 4K 60 window
  filedrag.py               Windows file drag and clipboard copy

  library_db.py             SQLite schema, search parser, Vault marks (no UI)
  library_tab.py            the Library tab
  library_player.py         the embedded player
  library_windows.py        help, folders, hidden tags, verifier
  tag_rail.py               the tag sidebar
  media.py                  ffprobe, thumbnails, hover frames
  prefetch.py               reads the next clips into memory ahead of time
  e621.py                   e621 tag lookups, paced, and their cache

  vault_tab.py              the Vault tab
  project_stats.py          a project's overview: totals, shared tags, pairs

  beat_engine.py            Beat This inference, EDL, Resolve scripting
  beat_tab.py               the Beat This tab
  resolve_api.py            DaVinci Resolve scripting

  libmpv_player.py, vlc_player.py, mpv_player.py, player_engine.py,
  audio_out.py              the playback engines
  settings_window.py        Settings
  artwork.py, artwork_window.py   your pictures
  fonts/                    Space Grotesk, Bricolage Grotesque, JetBrains Mono,
                            Lucide icons - loaded for the app only, nothing installed
beat_this/                  vendored CPJKU/beat_this
tests/                      unit tests
tools/                      smoke run, CI helpers
```

</details>

---

## Credits

- [Beat This!](https://github.com/CPJKU/beat_this) - CPJKU, beat and downbeat tracking
- [mpv](https://mpv.io) / [shinchiro builds](https://github.com/shinchiro/mpv-winbuild-cmake), [VLC](https://videolan.org), [FFmpeg](https://ffmpeg.org)
- [Grabber](https://github.com/Bionus/imgbrd-grabber) - downloads
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) - UI toolkit
- Fonts: Space Grotesk, Bricolage Grotesque, JetBrains Mono (SIL OFL); icons: [Lucide](https://lucide.dev) (ISC) - licences in `paz_suite/fonts/`
