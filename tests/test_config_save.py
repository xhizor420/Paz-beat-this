"""The config file is the app's memory of itself.

The library root, every saved search, the project colours, the display
scale, the panel widths: all of it lives in one JSON file, and it is
rewritten on a great many small actions - every search, every drag of a
handle, every settings change. Writing over the file in place empties it
first, so anything that stops the process in that window leaves a
truncated file. A truncated file does not parse, a config that does not
parse is silently replaced by the defaults, and the user's whole setup
is gone with no sign of why.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import config as cfgmod                        # noqa: E402


@pytest.fixture
def somewhere(tmp_path, monkeypatch):
    """Point the config at a scratch directory, not the real one."""
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(tmp_path / "paz_config.json"))
    return tmp_path


def test_a_save_round_trips(somewhere):
    cfg = cfgmod.AppConfig()
    cfg.library_root = "D:\\Clips"
    assert cfg.save() is None
    written = json.loads((somewhere / "paz_config.json").read_text("utf-8"))
    assert written["library_root"] == "D:\\Clips"


def test_a_save_leaves_no_scratch_file_behind(somewhere):
    cfgmod.AppConfig().save()
    assert [p.name for p in somewhere.iterdir()] == ["paz_config.json"]


def test_a_save_that_dies_mid_write_leaves_the_old_config_intact(somewhere):
    """The failure this exists for: the process stops between emptying
    the file and filling it again."""
    good = cfgmod.AppConfig()
    good.library_root = "D:\\Clips"
    good.save()

    class Boom(Exception):
        pass

    def explode(*_a, **_kw):
        raise Boom

    later = cfgmod.AppConfig()
    later.library_root = "E:\\Elsewhere"
    dump = cfgmod.json.dump
    cfgmod.json.dump = explode
    try:
        with pytest.raises(Boom):
            later.save()
    finally:
        cfgmod.json.dump = dump

    # The file on disk is still the one that was there, in one piece.
    back = cfgmod.AppConfig()
    back._read(str(somewhere / "paz_config.json"))
    assert back.library_root == "D:\\Clips"


def test_a_truncated_config_is_the_thing_this_avoids(somewhere):
    """Spelling out the cost, so the reason for the tmp file is on the
    record: a half-written file reads back as no settings at all."""
    (somewhere / "paz_config.json").write_text('{"library_root": "D:\\\\Cl',
                                               encoding="utf-8")
    back = cfgmod.AppConfig()
    back._read(str(somewhere / "paz_config.json"))
    assert back.library_root == cfgmod.AppConfig().library_root
