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


def test_a_clip_with_no_premium_copy_goes_in_on_its_own(fake_resolve):
    master, _proxy, item, _pool, _storage = fake_resolve
    ok, message = ra.import_clips([(master, "")])

    assert ok is True
    assert item.linked is None
    assert "proxy" not in message
