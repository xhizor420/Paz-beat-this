"""Drag real files out of a window and onto another program, or put
them on the clipboard the way Explorer's Copy does.

Topaz and Explorer accept a drop or a paste only when it is a Windows
file list. This builds that list and hands it over. Nothing is copied or
moved here; the program the files land on decides what to do with the
paths.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import uuid
from ctypes import (
    POINTER, Structure, WINFUNCTYPE, byref, c_int, c_long, c_uint,
    c_ulong, c_ushort, c_void_p, sizeof,
)

# Set by tests so a drag ends at once instead of waiting for the mouse.
_cancel_drag = False

S_OK = 0
S_FALSE = 1
E_NOTIMPL = -2147467263          # 0x80004001
E_NOINTERFACE = -2147467262     # 0x80004002
E_POINTER = -2147467261         # 0x80004003
E_UNEXPECTED = -2147418113      # 0x8000FFFF
DV_E_FORMATETC = -2147221404    # 0x80040064
OLE_E_ADVISENOTSUPPORTED = -2147221501  # 0x80040003
DRAGDROP_S_DROP = 0x00040100
DRAGDROP_S_CANCEL = 0x00040101
DRAGDROP_S_USEDEFAULTCURSORS = 0x00040102
DROPEFFECT_COPY = 1
MK_LBUTTON = 0x0001
CF_HDROP = 15
CF_UNICODETEXT = 13
TYMED_HGLOBAL = 1
DVASPECT_CONTENT = 1
DATADIR_GET = 1
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
RPC_E_CHANGED_MODE = 0x80010106


class GUID(Structure):
    _fields_ = [
        ("Data1", c_ulong),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class FORMATETC(Structure):
    _fields_ = [
        ("cfFormat", c_ushort),
        ("ptd", c_void_p),
        ("dwAspect", c_ulong),
        ("lindex", c_long),
        ("tymed", c_ulong),
    ]


class STGMEDIUM(Structure):
    _fields_ = [
        ("tymed", c_ulong),
        ("hGlobal", c_void_p),
        ("pUnkForRelease", c_void_p),
    ]


class DROPFILES(Structure):
    _fields_ = [
        ("pFiles", c_ulong),
        ("x", c_long),
        ("y", c_long),
        ("fNC", c_int),
        ("fWide", c_int),
    ]


def _guid(text: str) -> GUID:
    value = GUID()
    raw = uuid.UUID(text).bytes_le
    ctypes.memmove(byref(value), raw, 16)
    return value


_IID_UNKNOWN = _guid("00000000-0000-0000-C000-000000000046")
_IID_DATA = _guid("0000010e-0000-0000-C000-000000000046")
_IID_DROP = _guid("00000121-0000-0000-C000-000000000046")
_IID_ENUM = _guid("00000103-0000-0000-C000-000000000046")

_live: dict[int, object] = {}


def _same(ptr, expected: GUID) -> bool:
    return ctypes.string_at(ptr, 16) == ctypes.string_at(byref(expected), 16)


def hdrop_alloc(paths: list[str]):
    """An HGLOBAL the shell can read as a list of files. Caller frees it."""
    names = [os.path.abspath(path) for path in paths if path]
    if not names:
        return None
    payload = ("\0".join(names) + "\0\0").encode("utf-16-le")
    header = DROPFILES()
    header.pFiles = sizeof(DROPFILES)
    header.fWide = 1
    return _global_bytes(bytes(header) + payload)


def _global_bytes(data: bytes):
    """`data` in a fresh movable HGLOBAL, or None. Caller frees it."""
    kernel = ctypes.windll.kernel32
    kernel.GlobalAlloc.argtypes = [c_uint, ctypes.c_size_t]
    kernel.GlobalAlloc.restype = c_void_p
    kernel.GlobalLock.argtypes = [c_void_p]
    kernel.GlobalLock.restype = c_void_p
    kernel.GlobalUnlock.argtypes = [c_void_p]
    kernel.GlobalUnlock.restype = c_int
    handle = kernel.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(data))
    if not handle:
        return None
    locked = kernel.GlobalLock(handle)
    if not locked:
        _release_global(handle)
        return None
    ctypes.memmove(locked, data, len(data))
    kernel.GlobalUnlock(handle)
    return handle


def hdrop_paths(handle) -> list[str]:
    """The file paths stored in an HGLOBAL from `hdrop_alloc`."""
    shell = ctypes.windll.shell32
    shell.DragQueryFileW.argtypes = [c_void_p, c_uint, ctypes.c_wchar_p, c_uint]
    shell.DragQueryFileW.restype = c_uint
    count = shell.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
    found = []
    for index in range(count):
        length = shell.DragQueryFileW(handle, index, None, 0)
        buf = ctypes.create_unicode_buffer(length + 1)
        shell.DragQueryFileW(handle, index, buf, length + 1)
        found.append(buf.value)
    return found


def _release_global(handle) -> None:
    kernel = ctypes.windll.kernel32
    kernel.GlobalFree.argtypes = [c_void_p]
    kernel.GlobalFree.restype = c_void_p
    kernel.GlobalFree(handle)


class _Block(Structure):
    _fields_ = [("lpVtbl", c_void_p), ("ref", c_long)]


def _qi_factory(accepted: tuple[GUID, ...]):
    query = WINFUNCTYPE(c_long, c_void_p, c_void_p, POINTER(c_void_p))
    add = WINFUNCTYPE(c_ulong, c_void_p)
    release = WINFUNCTYPE(c_ulong, c_void_p)

    def query_interface(this, riid, ppv):
        try:
            if not ppv:
                return E_POINTER
            if any(_same(riid, iid) for iid in accepted):
                ppv[0] = this
                block = ctypes.cast(this, POINTER(_Block)).contents
                block.ref += 1
                return S_OK
            ppv[0] = None
            return E_NOINTERFACE
        except Exception:
            return E_UNEXPECTED

    def add_ref(this):
        block = ctypes.cast(this, POINTER(_Block)).contents
        block.ref += 1
        return block.ref

    def drop(this):
        # The object stays in _live at zero and _sweep lets it go later.
        # Dropping it here would free the very callback that is running.
        block = ctypes.cast(this, POINTER(_Block)).contents
        block.ref -= 1
        return max(block.ref, 0)

    return query(query_interface), add(add_ref), release(drop)


class _Enum:
    """One FORMATETC: a wide file list in global memory."""

    def __init__(self):
        fields = ["QueryInterface", "AddRef", "Release", "Next", "Skip",
                  "Reset", "Clone"]
        self.vtbl = _vtable(fields)
        qi, add, rel = _qi_factory((_IID_UNKNOWN, _IID_ENUM))
        nxt = WINFUNCTYPE(c_long, c_void_p, c_ulong, c_void_p, POINTER(c_ulong))
        skip = WINFUNCTYPE(c_long, c_void_p, c_ulong)
        reset = WINFUNCTYPE(c_long, c_void_p)
        clone = WINFUNCTYPE(c_long, c_void_p, POINTER(c_void_p))
        self._done = False
        self._cb = [qi, add, rel, nxt(self._next), skip(self._skip),
                    reset(self._reset), clone(self._clone)]
        for name, callback in zip(fields, self._cb):
            setattr(self.vtbl, name, ctypes.cast(callback, c_void_p))
        self.ptr = ctypes.pointer(self.vtbl)
        self.block = _Block(ctypes.cast(self.ptr, c_void_p), 1)
        _live[ctypes.addressof(self.block)] = self

    def _next(self, this, celt, rgelt, fetched):
        try:
            if self._done or celt < 1:
                if fetched:
                    fetched[0] = 0
                return S_FALSE
            fmt = ctypes.cast(rgelt, POINTER(FORMATETC)).contents
            fmt.cfFormat = CF_HDROP
            fmt.ptd = None
            fmt.dwAspect = DVASPECT_CONTENT
            fmt.lindex = -1
            fmt.tymed = TYMED_HGLOBAL
            self._done = True
            if fetched:
                fetched[0] = 1
            return S_OK
        except Exception:
            return E_UNEXPECTED

    def _skip(self, this, celt):
        if self._done:
            return S_FALSE
        self._done = True
        return S_OK

    def _reset(self, this):
        self._done = False
        return S_OK

    def _clone(self, this, out):
        return E_NOTIMPL


def _vtable(names: list[str]):
    cls = type("_Vtbl", (Structure,), {"_fields_": [(name, c_void_p) for name in names]})
    return cls()


class _Data:
    def __init__(self, paths: list[str]):
        fields = [
            "QueryInterface", "AddRef", "Release", "GetData", "GetDataHere",
            "QueryGetData", "GetCanonicalFormatEtc", "SetData", "EnumFormatEtc",
            "DAdvise", "DUnadvise", "EnumDAdvise",
        ]
        self.paths = list(paths)
        self.vtbl = _vtable(fields)
        qi, add, rel = _qi_factory((_IID_UNKNOWN, _IID_DATA))
        get = WINFUNCTYPE(c_long, c_void_p, POINTER(FORMATETC), POINTER(STGMEDIUM))
        query = WINFUNCTYPE(c_long, c_void_p, POINTER(FORMATETC))
        canon = WINFUNCTYPE(c_long, c_void_p, POINTER(FORMATETC), POINTER(FORMATETC))
        setdata = WINFUNCTYPE(c_long, c_void_p, POINTER(FORMATETC), POINTER(STGMEDIUM), c_int)
        enum = WINFUNCTYPE(c_long, c_void_p, c_ulong, POINTER(c_void_p))
        # DAdvise(this, FORMATETC*, advf, sink, connection)
        advise = WINFUNCTYPE(
            c_long, c_void_p, c_void_p, c_ulong, c_void_p, POINTER(c_ulong))
        unadvise = WINFUNCTYPE(c_long, c_void_p, c_ulong)
        enum_advise = WINFUNCTYPE(c_long, c_void_p, POINTER(c_void_p))
        self._cb = [
            qi, add, rel, get(self._get), get(self._notimpl), query(self._query),
            canon(self._notimpl2), setdata(self._notimpl3), enum(self._enum),
            advise(self._advise), unadvise(self._unadvise),
            enum_advise(self._enum_advise),
        ]
        for name, callback in zip(fields, self._cb):
            setattr(self.vtbl, name, ctypes.cast(callback, c_void_p))
        self.ptr = ctypes.pointer(self.vtbl)
        self.block = _Block(ctypes.cast(self.ptr, c_void_p), 1)
        _live[ctypes.addressof(self.block)] = self

    def _matches(self, fmt: FORMATETC) -> bool:
        return (fmt.cfFormat == CF_HDROP and fmt.tymed & TYMED_HGLOBAL
                and fmt.dwAspect == DVASPECT_CONTENT)

    def _get(self, this, fmt, medium):
        try:
            if not self._matches(fmt.contents):
                return DV_E_FORMATETC
            handle = hdrop_alloc(self.paths)
            if not handle:
                return E_UNEXPECTED
            medium.contents.tymed = TYMED_HGLOBAL
            medium.contents.hGlobal = handle
            medium.contents.pUnkForRelease = None
            return S_OK
        except Exception:
            return E_UNEXPECTED

    def _query(self, this, fmt):
        try:
            return S_OK if self._matches(fmt.contents) else DV_E_FORMATETC
        except Exception:
            return E_UNEXPECTED

    def _enum(self, this, direction, out):
        try:
            if direction != DATADIR_GET:
                return E_NOTIMPL
            enum = _Enum()
            out[0] = ctypes.addressof(enum.block)
            return S_OK
        except Exception:
            return E_UNEXPECTED

    def _notimpl(self, this, fmt, medium):
        return E_NOTIMPL

    def _notimpl2(self, this, src, dest):
        return E_NOTIMPL

    def _notimpl3(self, this, fmt, medium, release):
        return E_NOTIMPL

    def _advise(self, this, fmt, advf, sink, connection):
        return OLE_E_ADVISENOTSUPPORTED

    def _unadvise(self, this, connection):
        return OLE_E_ADVISENOTSUPPORTED

    def _enum_advise(self, this, out):
        return OLE_E_ADVISENOTSUPPORTED


class _Source:
    def __init__(self):
        fields = ["QueryInterface", "AddRef", "Release",
                  "QueryContinueDrag", "GiveFeedback"]
        self.vtbl = _vtable(fields)
        qi, add, rel = _qi_factory((_IID_UNKNOWN, _IID_DROP))
        cont = WINFUNCTYPE(c_long, c_void_p, c_int, c_ulong)
        feed = WINFUNCTYPE(c_long, c_void_p, c_ulong)
        self._cb = [qi, add, rel, cont(self._continue), feed(self._feedback)]
        for name, callback in zip(fields, self._cb):
            setattr(self.vtbl, name, ctypes.cast(callback, c_void_p))
        self.ptr = ctypes.pointer(self.vtbl)
        self.block = _Block(ctypes.cast(self.ptr, c_void_p), 1)
        _live[ctypes.addressof(self.block)] = self

    def _continue(self, this, escape, keys):
        if _cancel_drag or escape:
            return DRAGDROP_S_CANCEL
        if not (keys & MK_LBUTTON):
            return DRAGDROP_S_DROP
        return S_OK

    def _feedback(self, this, effect):
        return DRAGDROP_S_USEDEFAULTCURSORS


def _sweep() -> None:
    """Forget the objects nobody holds any more. Never called from inside
    one of their callbacks."""
    for key, obj in list(_live.items()):
        if obj.block.ref <= 0:
            _live.pop(key, None)


def drag_files(paths: list[str]) -> str:
    """Start a file drag; returns when it ends. "dropped" when the files
    landed somewhere, "cancelled" when the drag was let go of nowhere or
    escaped, "" when there was nothing to drag or it could not start -
    so a plain truth test still reads as "it worked"."""
    if sys.platform != "win32":
        return ""
    paths = [path for path in paths if path and os.path.isfile(path)]
    if not paths:
        return ""
    ole = ctypes.windll.ole32
    ole.OleInitialize.argtypes = [c_void_p]
    ole.OleInitialize.restype = c_long
    ole.DoDragDrop.argtypes = [c_void_p, c_void_p, c_ulong, POINTER(c_ulong)]
    ole.DoDragDrop.restype = c_long
    started = ole.OleInitialize(None)
    if started < 0 and (started & 0xFFFFFFFF) != RPC_E_CHANGED_MODE:
        return ""
    _sweep()
    data = _Data(paths)
    source = _Source()
    effect = c_ulong(0)
    hr = -1
    try:
        hr = ole.DoDragDrop(
            ctypes.addressof(data.block), ctypes.addressof(source.block),
            DROPEFFECT_COPY, byref(effect))
    except Exception:
        return ""
    finally:
        # Our own reference to each. Whatever the drop target still holds
        # keeps its count above zero, and so keeps it in _live.
        data.block.ref -= 1
        source.block.ref -= 1
        _sweep()
        # S_FALSE (already initialised on this thread) still counts, and
        # needs its own uninitialise to balance.
        if started >= 0:
            ole.OleUninitialize()
    if hr < 0:
        return ""
    # Let go over a program that said no is a drop with no effect: the
    # files went nowhere, so it counts as cancelled.
    if hr == DRAGDROP_S_DROP and effect.value:
        return "dropped"
    return "cancelled"


def _clipboard_payload(paths: list[str]) -> list:
    """(format, HGLOBAL) pairs for a file copy: the file list, the paths
    as text for anything that only takes text, and "this is a copy, not
    a cut" so a paste into Explorer never moves the originals. None when
    one of them could not be made (and nothing is left allocated)."""
    names = [os.path.abspath(path) for path in paths if path]
    if not names:
        return None
    user = ctypes.windll.user32
    user.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
    user.RegisterClipboardFormatW.restype = c_uint
    effect_format = user.RegisterClipboardFormatW("Preferred DropEffect")
    items = [
        (CF_HDROP, hdrop_alloc(names)),
        (CF_UNICODETEXT, _global_bytes(
            ("\r\n".join(names) + "\0").encode("utf-16-le"))),
        (effect_format, _global_bytes(DROPEFFECT_COPY.to_bytes(4, "little"))),
    ]
    if not all(handle for _fmt, handle in items) or not effect_format:
        for _fmt, handle in items:
            if handle:
                _release_global(handle)
        return None
    return items


def copy_files(paths: list[str]) -> bool:
    """Put files on the clipboard as Explorer's Copy does, so Ctrl+V in
    Topaz or a folder pastes them. False when nothing could be copied."""
    if sys.platform != "win32":
        return False
    paths = [path for path in paths if path and os.path.isfile(path)]
    items = _clipboard_payload(paths) if paths else None
    if not items:
        return False
    user = ctypes.windll.user32
    user.OpenClipboard.argtypes = [c_void_p]
    user.OpenClipboard.restype = c_int
    user.EmptyClipboard.restype = c_int
    user.CloseClipboard.restype = c_int
    user.SetClipboardData.argtypes = [c_uint, c_void_p]
    user.SetClipboardData.restype = c_void_p
    # Another program can hold the clipboard for a moment.
    for _ in range(10):
        if user.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        for _fmt, handle in items:
            _release_global(handle)
        return False
    ok = True
    try:
        user.EmptyClipboard()
        for fmt, handle in items:
            # On success the clipboard owns the memory; on failure we do.
            if not user.SetClipboardData(fmt, handle):
                _release_global(handle)
                if fmt == CF_HDROP:
                    ok = False
    finally:
        user.CloseClipboard()
    return ok
