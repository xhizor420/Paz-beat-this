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


def import_clips(clips: list, bin_name: str = "") -> tuple:
    """Add clips to the open project's media pool.

    `clips` is a list of (master_path, proxy_path) pairs - proxy may be
    empty. Returns (ok, message).

    Resolve refuses a proxy whose duration doesn't match its master, and
    it is right to: a mismatched proxy would show the wrong frames. When
    that happens the clip is still imported against the real file and the
    count is reported, rather than failing the whole import.
    """
    project, problem = current_project()
    if project is None:
        return False, problem

    wanted = [(master, proxy) for master, proxy in clips
              if master and os.path.exists(master)]
    if not wanted:
        return False, "None of those files are where the library thinks they are."

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

    if not items:
        return False, ("Resolve didn't import anything. Clips already in the "
                        "media pool are skipped, so this usually means they "
                        "were already there.")

    # AddItemListToMediaPool returns items in the order it accepted them,
    # which is not necessarily the order asked for, and skips duplicates -
    # so match proxies back up by file name rather than by position.
    proxies = {os.path.basename(master): proxy for master, proxy in wanted if proxy}
    linked = mismatched = 0
    for item in items:
        proxy = proxies.get(item.GetClipProperty("File Name"))
        if not proxy or not os.path.exists(proxy):
            continue
        try:
            if item.LinkProxyMedia(proxy):
                linked += 1
            else:
                mismatched += 1
        except Exception:
            mismatched += 1

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
