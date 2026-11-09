"""Talking to a running copy of DaVinci Resolve.

Two features need this: Beat This drops beat markers onto the open
timeline, and the Library adds clips to the media pool. Both need the same
awkward first step - finding Resolve's scripting API, which its own README
tells you to wire up by hand through RESOLVE_SCRIPT_API,
RESOLVE_SCRIPT_LIB and PYTHONPATH. Almost nobody has, so this module looks
in the fixed places the installer uses instead, and reports what it tried
when it can't.

Nothing here imports anything heavy at module load: everything Resolve
lives behind function calls, so a machine without Resolve installed pays
nothing and breaks nothing.
"""

from __future__ import annotations

import os
import sys


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
        "What was tried:\n" + detail)




# ── connecting ──────────────────────────────────────────────────────────

def connect() -> tuple:
    """(resolve, None) when attached to a running Resolve, else
    (None, why). Every caller wants the same three checks in the same
    order, so they live here rather than in each feature."""
    module, problem = resolve_module()
    if module is None:
        return None, problem
    resolve = module.scriptapp("Resolve")
    if resolve is None:
        return None, ("Loaded Resolve's scripting API, but it can't attach to a "
                       "running Resolve. Open Resolve, then set Preferences > "
                       "System > General > 'External scripting using' to Local "
                       "and try again.")
    return resolve, None


def current_project() -> tuple:
    """(project, None) for the open project, else (None, why)."""
    resolve, problem = connect()
    if resolve is None:
        return None, problem
    manager = resolve.GetProjectManager()
    project = manager.GetCurrentProject() if manager else None
    if project is None:
        return None, "No project open in Resolve."
    return project, None


# ── media pool ──────────────────────────────────────────────────────────
#
# The Library's whole point is that it holds two copies of a clip: the
# converted one it indexes, and a 4K/60 edit-ready copy in the premium
# pool. That is exactly Resolve's master/proxy split, so importing sets
# it up rather than making you do it by hand: the 4K file becomes the
# clip, and the converted one becomes its proxy, so timeline playback is
# cheap and renders still come off the big file.

def _find_bin(pool, name: str):
    """The named bin under the media pool root, created if missing."""
    root = pool.GetRootFolder()
    if not name:
        return root
    for folder in (root.GetSubFolderList() or []):
        if folder.GetName() == name:
            return folder
    return pool.AddSubFolder(root, name) or root


def _every_clip(folder):
    """Every clip in a media-pool folder and all the folders under it."""
    for clip in (folder.GetClipList() or []) if hasattr(folder, "GetClipList") else []:
        yield clip
    for sub in folder.GetSubFolderList() or []:
        yield from _every_clip(sub)


def _same_file(a: str, b: str) -> bool:
    return bool(a) and bool(b) and \
        os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _link_pool(pool, proxy_for, skip=None) -> tuple:
    """Link `proxy_for(clip's file)` to every clip in the pool that has one
    and is not linked to it yet. (linked, already, refused)."""
    linked = already = refused = 0
    root = pool.GetRootFolder() if pool is not None else None
    if root is None:
        return 0, 0, 0
    for clip in _every_clip(root):
        if skip is not None and skip(clip):
            continue
        try:
            path = clip.GetClipProperty("File Path") or ""
        except Exception:
            continue
        proxy = proxy_for(path) if path else ""
        if not proxy or not os.path.exists(proxy):
            continue
        try:
            current = clip.GetClipProperty("Proxy Media Path") or ""
        except Exception:
            current = ""
        if _same_file(current, proxy):
            already += 1
            continue
        try:
            ok = clip.LinkProxyMedia(proxy)
        except Exception:
            ok = False
        if ok:
            linked += 1
        else:
            refused += 1
    return linked, already, refused


def link_proxies(proxy_for) -> tuple:
    """Link a proxy to every clip in the open project that has one -
    clips added by hand, by drag and drop, or before the proxy was made.
    `proxy_for(path)` gives the proxy for a clip's file, or "".
    Returns (ok, message, linked)."""
    project, problem = current_project()
    if project is None:
        return False, problem, 0
    linked, already, refused = _link_pool(project.GetMediaPool(), proxy_for)
    if not (linked or already or refused):
        return True, "No clip in the open Resolve project has a proxy yet.", 0
    bits = []
    if linked:
        bits.append(f"linked {linked} proxy{'s' if linked != 1 else ''}")
    if already:
        bits.append(f"{already} already linked")
    if refused:
        bits.append(f"{refused} turned down by Resolve")
    message = "Resolve: " + ", ".join(bits) + "."
    return True, message, linked


def import_clips(clips: list, bin_name: str = "") -> tuple:
    """Add clips to the open project's media pool.

    `clips` is a list of (master_path, proxy_path) pairs - proxy may be
    empty. Returns (ok, message).

    Resolve refuses a proxy whose duration doesn't match its master, and
    it is right to: a mismatched proxy would show the wrong frames. When
    that happens the clip is still imported against the real file and the
    count is reported, rather than failing the whole import.
    """
    # Missing files first: that is something the Library can fix, and it
    # holds whether or not Resolve happens to be open.
    wanted = [(master, proxy) for master, proxy in clips
              if master and os.path.exists(master)]
    if not wanted:
        return False, "None of those files are where the library thinks they are."

    project, problem = current_project()
    if project is None:
        return False, problem

    resolve, _ = connect()
    pool = project.GetMediaPool()
    storage = resolve.GetMediaStorage() if resolve else None
    if pool is None or storage is None:
        return False, "Resolve is open but didn't hand over its media pool."

    previous = pool.GetCurrentFolder()
    target = _find_bin(pool, bin_name)
    pool.SetCurrentFolder(target)
    try:
        items = storage.AddItemListToMediaPool([m for m, _p in wanted]) or []
    finally:
        if previous:
            pool.SetCurrentFolder(previous)

    # AddItemListToMediaPool returns items in the order it accepted them,
    # which is not necessarily the order asked for, and skips duplicates -
    # so match proxies back up by the file rather than by position. By its
    # full path first: two categories can hold the same file name, and
    # going by name alone gave both clips whichever proxy came last.
    by_path = {os.path.normcase(os.path.normpath(master)): proxy
               for master, proxy in wanted if proxy}
    by_name = {os.path.basename(master): proxy for master, proxy in wanted if proxy}
    linked = mismatched = 0
    for item in items:
        where = item.GetClipProperty("File Path") or ""
        proxy = (by_path.get(os.path.normcase(os.path.normpath(where))) if where
                 else by_name.get(item.GetClipProperty("File Name")))
        if not proxy or not os.path.exists(proxy):
            continue
        try:
            if item.LinkProxyMedia(proxy):
                linked += 1
            else:
                mismatched += 1
        except Exception:
            mismatched += 1

    # Clips that were in the media pool already were skipped by the import
    # above - and so had no proxy linked. Link theirs too.
    done = {id(item) for item in items}
    extra, _same, extra_refused = _link_pool(
        pool, lambda path: by_path.get(os.path.normcase(os.path.normpath(path))),
        skip=lambda clip: id(clip) in done)
    linked += extra
    mismatched += extra_refused

    if not items:
        if extra:
            return True, (f"Already in the media pool - linked {extra} "
                          f"prox{'ies' if extra != 1 else 'y'} to them.")
        return False, ("Resolve didn't import anything. Clips already in the "
                        "media pool are skipped, so this usually means they "
                        "were already there.")

    where = f" into '{bin_name}'" if bin_name else ""
    message = f"Added {len(items)} clip{'s' if len(items) != 1 else ''}{where}."
    if linked:
        message += f" {linked} linked to a proxy."
    if mismatched:
        message += (f" {mismatched} kept the full-size file - Resolve turned "
                     "the proxy down, which means its duration doesn't match.")
    if len(items) < len(wanted):
        message += (f" {len(wanted) - len(items)} skipped, already in the "
                     "media pool.")
    return True, message
