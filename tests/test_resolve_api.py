"""Cover for the Resolve hand-off that doesn't need Resolve.

The parts worth testing here are the ones that decide *what* gets sent -
which copy of a clip Resolve should treat as the real file, which as the
proxy, and what happens when neither is where the index says. Whether
Resolve then accepts them is Resolve's business and can only be checked
against a running copy.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import resolve_api as ra    # noqa: E402


def test_no_resolve_reports_what_it_looked_for_rather_than_just_failing():
    ok, message = ra.import_clips([("/nonexistent/clip.mp4", "")])
    assert ok is False
    # Either the API is missing (this machine) or the files are - both are
    # answers a person can act on, and neither is a traceback.
    assert message
    assert "\n" in message or "where the library thinks" in message


def test_missing_files_are_reported_before_resolve_is_blamed():
    ok, message = ra.import_clips([("", ""), ("/nope/gone.mp4", "")])
    assert ok is False


class FakeItem:
    def __init__(self, name):
        self.name = name
        self.linked = None
        self.refused = False

    def GetClipProperty(self, key):
        return self.name if key == "File Name" else ""

    def LinkProxyMedia(self, path):
        if self.refused:
            return False
        self.linked = path
        return True


class FakeFolder:
    def __init__(self, name=""):
        self.name = name

    def GetName(self):
        return self.name

    def GetSubFolderList(self):
        return []


class FakePool:
    def __init__(self):
        self.root = FakeFolder("Master")
        self.added = []
        self.current = self.root

    def GetRootFolder(self):
        return self.root

    def AddSubFolder(self, parent, name):
        self.added.append(name)
        return FakeFolder(name)

    def GetCurrentFolder(self):
        return self.current

    def SetCurrentFolder(self, folder):
        self.current = folder
        return True


class FakeStorage:
    def __init__(self, items):
        self.items = items
        self.asked = None

    def AddItemListToMediaPool(self, paths):
        self.asked = list(paths)
        return self.items


@pytest.fixture
def fake_resolve(monkeypatch, tmp_path):
    """A stand-in Resolve that accepts everything, so the pairing and
    proxy-linking logic can be exercised without the application."""
    master = tmp_path / "big.mov"
    proxy = tmp_path / "small.mp4"
    master.write_bytes(b"0")
    proxy.write_bytes(b"0")

    item = FakeItem("big.mov")
    pool = FakePool()
    storage = FakeStorage([item])

    class FakeProject:
        def GetMediaPool(self):
            return pool

    class FakeResolve:
        def GetMediaStorage(self):
            return storage

    monkeypatch.setattr(ra, "current_project", lambda: (FakeProject(), None))
    monkeypatch.setattr(ra, "connect", lambda: (FakeResolve(), None))
    return str(master), str(proxy), item, pool, storage


def test_the_big_file_is_the_clip_and_the_small_one_is_its_proxy(fake_resolve):
    master, proxy, item, pool, storage = fake_resolve
    ok, message = ra.import_clips([(master, proxy)], bin_name="PAZ Library")

    assert ok is True
    assert storage.asked == [master], "Resolve should be handed the 4K copy"
    assert item.linked == proxy, "the converted copy should become the proxy"
    assert "PAZ Library" in message
    assert pool.added == ["PAZ Library"]
    assert pool.current is pool.root, "the pool's own folder should be restored"


def test_a_refused_proxy_still_imports_the_clip(fake_resolve):
    master, proxy, item, _pool, _storage = fake_resolve
    item.refused = True
    ok, message = ra.import_clips([(master, proxy)])

    assert ok is True
    assert item.linked is None
    assert "duration doesn't match" in message


class PoolClip:
    """A clip already in the media pool, with a file and maybe a proxy."""

    def __init__(self, path, proxy="", refuse=False):
        self.path, self.proxy, self.refuse, self.asked = path, proxy, refuse, 0

    def GetClipProperty(self, key):
        return {"File Path": self.path, "Proxy Media Path": self.proxy,
                "File Name": os.path.basename(self.path)}.get(key, "")

    def LinkProxyMedia(self, path):
        self.asked += 1
        if self.refuse:
            return False
        self.proxy = path
        return True


class PoolFolder(FakeFolder):
    def __init__(self, name, clips=(), subs=()):
        super().__init__(name)
        self.clips, self.subs = list(clips), list(subs)

    def GetClipList(self):
        return self.clips

    def GetSubFolderList(self):
        return self.subs


def project_with(monkeypatch, root):
    pool = FakePool()
    pool.root = root

    class Project:
        def GetMediaPool(self):
            return pool

    monkeypatch.setattr(ra, "current_project", lambda: (Project(), None))


def test_every_clip_in_the_project_gets_its_proxy(monkeypatch, tmp_path):
    proxies = {}
    for name in ("a", "b", "c", "d"):
        proxies[str(tmp_path / f"{name}.mp4")] = str(tmp_path / f"{name}.mov")
        (tmp_path / f"{name}.mov").write_bytes(b"0")
    loose = PoolClip(str(tmp_path / "a.mp4"))                       # dragged in by hand
    nested = PoolClip(str(tmp_path / "b.mp4"))                      # in a bin in a bin
    done = PoolClip(str(tmp_path / "c.mp4"), proxy=str(tmp_path / "c.mov"))
    refused = PoolClip(str(tmp_path / "d.mp4"), refuse=True)
    other = PoolClip(str(tmp_path / "song.wav"))                    # not a pool master
    root = PoolFolder("Master", [loose, other],
                      [PoolFolder("PMV", [done], [PoolFolder("Cuts", [nested, refused])])])
    project_with(monkeypatch, root)

    ok, message, linked = ra.link_proxies(lambda path: proxies.get(path, ""))
    assert ok and linked == 2
    assert loose.proxy.endswith("a.mov") and nested.proxy.endswith("b.mov")
    assert done.asked == 0, "an already-linked clip is left alone"
    assert other.asked == 0
    assert "1 already linked" in message and "1 turned down" in message


def test_a_proxy_not_made_yet_is_not_linked(monkeypatch, tmp_path):
    clip = PoolClip(str(tmp_path / "a.mp4"))
    project_with(monkeypatch, PoolFolder("Master", [clip]))
    ok, message, linked = ra.link_proxies(lambda path: str(tmp_path / "a.mov"))
    assert ok and linked == 0 and clip.asked == 0


def test_no_resolve_open_is_an_answer_not_a_crash(monkeypatch):
    monkeypatch.setattr(ra, "current_project", lambda: (None, "No project open in Resolve."))
    ok, message, linked = ra.link_proxies(lambda path: "")
    assert not ok and linked == 0 and "No project" in message


def test_adding_clips_also_links_ones_already_in_the_pool(fake_resolve):
    master, proxy, item, pool, storage = fake_resolve
    already = PoolClip(master)
    pool.root = PoolFolder("Master", [already])
    storage.items = []                     # Resolve skipped it: already there
    ok, message = ra.import_clips([(master, proxy)])
    assert already.proxy == proxy


def test_a_clip_with_no_premium_copy_goes_in_on_its_own(fake_resolve):
    master, _proxy, item, _pool, _storage = fake_resolve
    ok, message = ra.import_clips([(master, "")])

    assert ok is True
    assert item.linked is None
    assert "proxy" not in message
