"""The Grab new button must hand Grabber its own search, unchanged.

The folder rules and the "already downloaded" list live in Grabber. The
command therefore must not pass a filename or a save location, or those
rules would be replaced by whatever was typed here.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import grabber


def test_a_saved_grabber_tab_is_the_search(tmp_path):
    path = tmp_path / "tabs.json"
    path.write_text(json.dumps({
        "current": 0,
        "tabs": [{
            "tags": ["fav:Xhizor"],
            "sites": ["e621.net"],
            "page": 1,
            "perpage": 250,
            "lastUrls": {"e621.net": {"Html": "https://e621.net/posts?api_key=secret"}},
        }],
    }), encoding="utf-8")
    tags, source, page, per_page = grabber.saved_search(str(path))
    assert tags == "fav:Xhizor"
    assert source == "e621.net"
    assert page == 1
    assert per_page == 250


def test_the_command_is_one_page_and_leaves_folders_to_grabber():
    cmd = grabber.command("fav:Xhizor", "e621.net", 1, 250)
    assert cmd[0].endswith("Grabber.com")
    assert "-c" not in cmd
    assert "--download" in cmd
    assert "-t" in cmd and cmd[cmd.index("-t") + 1] == "fav:Xhizor"
    assert "-s" in cmd and cmd[cmd.index("-s") + 1] == "e621.net"
    assert cmd[cmd.index("-p") + 1] == "1"
    assert cmd[cmd.index("-i") + 1] == "250"
    assert cmd[cmd.index("-m") + 1] == "250"
    assert "-f" not in cmd
    assert "-l" not in cmd
    assert "secret" not in " ".join(cmd)


def test_a_save_line_counts_as_new_and_drops_the_url():
    line = ("[Info] Loading and saving image from "
            "`https://static1.e621.net/data/x.webm?api_key=secret` "
            "in `P:\\PAZ\\Main\\Furry\\6767467.mp4`")
    kind, name = grabber.parse_progress(line)
    assert kind == "new"
    assert name == "6767467.mp4"
    assert "secret" not in name
    assert "http" not in name


def test_an_existing_file_counts_as_a_skip():
    kind, name = grabber.parse_progress(
        "[Info] File already exists: `P:\\PAZ\\Main\\Furry\\6767077.mp4`")
    assert kind == "skip"
    assert name == "6767077.mp4"


def test_the_bar_shows_the_queue_and_then_how_many_were_new():
    running = grabber.status_text({
        "new": 2, "skipped": 10, "total": 250, "done": False,
        "name": "6767077.mp4",
        "recent": ["6767467.mp4", "6767121.mp4"],
    })
    assert running["title"] == "Pulling new videos"
    assert running["count"] == "2"
    assert running["detail"] == "12 of 250 · 6767467.mp4 · 6767121.mp4"
    assert running["fraction"] == 12 / 250
    done = grabber.status_text({
        "new": 2, "skipped": 248, "total": 250, "done": True, "name": "",
    })
    assert done["title"] == "Grabbed 2 new"
    assert done["detail"] == "248 already had"
    assert done["count"] == "2"
    assert done["fraction"] == 1.0
    stopped = grabber.status_text({
        "new": 0, "skipped": 0, "total": 250, "done": True, "failed": True,
    })
    assert stopped["title"] == "Grabber stopped"
    assert stopped["detail"] == "Nothing was saved"


class _FakeProc:
    def __init__(self, code=0):
        self.returncode = None
        self._code = code

    def poll(self):
        self.returncode = self._code
        return self._code

    def wait(self, timeout=None):
        self.returncode = self._code
        return self._code


def test_a_missing_log_is_an_unknown_count_not_nothing_new(tmp_path, monkeypatch):
    monkeypatch.setattr(grabber, "_logs_dir", lambda: str(tmp_path / "nowhere"))
    events = []
    grabber._watch(_FakeProc(0), 250, set(), events.append)
    assert events[-1]["done"] and events[-1]["logged"] is False
    view = grabber.status_text(events[-1])
    assert view["title"] == "Grabber finished"
    assert "unknown" in view["detail"]


def test_the_last_report_comes_even_when_watching_breaks(tmp_path, monkeypatch):
    def boom(_before):
        raise RuntimeError("surprise")
    monkeypatch.setattr(grabber, "_newest_log", boom)
    events = []
    try:
        grabber._watch(_FakeProc(0), 10, set(), events.append)
    except RuntimeError:
        pass
    assert events and events[-1]["done"]


def test_a_log_is_counted_to_the_end(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(grabber, "_logs_dir", lambda: str(logs))
    (logs / "main.log").write_text(
        "[Info] Loading and saving image from `https://x/a.webm` in `P:/A/1.mp4`\n"
        "[Info] File already exists: `P:/A/2.mp4`\n"
        "[Info] Loading and saving image from `https://x/b.webm` in `P:/A/3.mp4`",
        encoding="utf-8")
    events = []
    grabber._watch(_FakeProc(0), 3, set(), events.append)
    last = events[-1]
    assert (last["new"], last["skipped"], last["logged"]) == (2, 1, True)
    assert grabber.status_text(last)["title"] == "Grabbed 2 new  ·  A 2"


def test_a_stopped_grab_says_stopped():
    view = grabber.status_text({"new": 3, "skipped": 1, "total": 10,
                                "done": True, "failed": True, "stopped": True})
    assert view["title"] == "Stopped"
    assert view["detail"] == "3 saved before it stopped"


def test_a_save_line_names_the_folder_grabber_chose():
    line = ("[Info] Loading and saving image from `https://x/a.webm?api_key=s` "
            "in `P:/PAZ/Main/MLP Futa/6767467.mp4`")
    assert grabber.saved_folder(line) == "MLP Futa"
    assert grabber.saved_folder("[Info] File already exists: `P:/A/1.mp4`") == ""


def test_the_finished_bar_says_where_the_new_files_went():
    done = grabber.status_text({
        "new": 3, "skipped": 247, "total": 250, "done": True, "logged": True,
        "saved": [("1.mp4", "MLP Futa"), ("2.mp4", "Furry"), ("3.mp4", "MLP Futa")],
    })
    assert done["title"] == "Grabbed 3 new  ·  MLP Futa 2 · Furry 1"
    assert done["detail"] == "247 already had"


def test_the_watcher_reports_each_file_with_its_folder(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(grabber, "_logs_dir", lambda: str(logs))
    (logs / "main.log").write_text(
        "[Info] Loading and saving image from `https://x/a` in `P:/M/MLP Futa/1.mp4`\n"
        "[Info] Loading and saving image from `https://x/b` in `P:/M/Furry/2.mp4`\n",
        encoding="utf-8")
    events = []
    grabber._watch(_FakeProc(0), 2, set(), events.append)
    assert events[-1]["saved"] == [("1.mp4", "MLP Futa"), ("2.mp4", "Furry")]
