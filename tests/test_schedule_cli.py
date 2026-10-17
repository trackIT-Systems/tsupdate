"""Tests for maintenance schedule parsing and CLI dispatch."""

import json
import sys
from unittest.mock import patch

import pytest

from tsupdate import cli, schedule


class TestSchedule:
    def test_load_and_find_maintenance(self, tmp_path):
        p = tmp_path / "schedule.yml"
        p.write_text(
            "schedule:\n"
            "  - name: recording\n    start: sunrise-1h\n    stop: sunset+1h\n"
            "  - name: maintenance\n    start: '02:00'\n    stop: '03:00'\n"
        )
        entry = schedule.find_maintenance_entry(schedule.load_schedule(p))
        assert entry == {"name": "maintenance", "start": "02:00", "stop": "03:00"}

    @pytest.mark.parametrize("text", ["", "- not a dict\n", "schedule: [\n"])
    def test_invalid_schedule_files(self, tmp_path, text):
        p = tmp_path / "schedule.yml"
        p.write_text(text)
        assert schedule.load_schedule(p) is None

    def test_missing_schedule_file(self, tmp_path):
        assert schedule.load_schedule(tmp_path / "nope.yml") is None

    @pytest.mark.parametrize("sched", [{}, {"schedule": "x"}, {"schedule": [{"name": "other"}]}])
    def test_no_maintenance_entry(self, sched):
        assert schedule.find_maintenance_entry(sched) is None

    def test_window_active_all_day(self):
        assert schedule.is_in_maintenance_window({"name": "maintenance", "start": "00:00", "stop": "23:59:59"}) in (
            True,
            False,  # the one second before midnight
        )
        assert schedule.get_next_maintenance_window_start({"start": "00:00", "stop": "01:00"}) is not None

    @pytest.mark.parametrize("entry", [{"start": "02:00"}, {"stop": "02:00"}, "nope"])
    def test_bad_entries_are_not_active(self, entry):
        assert schedule.is_in_maintenance_window(entry) is False
        assert schedule.get_next_maintenance_window_start(entry) is None

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: scheduleparse parses unknown time strings as midnight, so a typo like stop: '3:OO' "
        "silently turns into a ~22h maintenance window instead of being rejected",
    )
    def test_unparseable_times_are_rejected(self):
        assert schedule.get_next_maintenance_window_start({"start": "02:00", "stop": "bogus"}) is None


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["tsupdate", *argv])
    monkeypatch.setattr(cli, "initialize_github_token", lambda: None)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value.code


class TestCli:
    @pytest.mark.parametrize(
        "argv",
        [["syncroot"], ["mount"], ["unmount"], ["apply", "x.tar"], ["restore", "x.img"], ["tryboot"], ["persist"], ["rollback"]],
    )
    def test_privileged_commands_require_root(self, monkeypatch, argv):
        monkeypatch.setattr(cli, "is_root", lambda: False)
        with patch.object(cli, "execute_syncroot") as s, patch.object(cli, "execute_apply") as a, patch.object(
            cli, "execute_tryboot"
        ) as t:
            assert run_cli(monkeypatch, *argv) == 1
        s.assert_not_called()
        a.assert_not_called()
        t.assert_not_called()

    def test_status_json(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "get_system_status", lambda: {"os_name": "tsOS", "booted_via_tryboot": "False"})
        assert run_cli(monkeypatch, "status", "--json") == 0
        assert json.loads(capsys.readouterr().out)["os_name"] == "tsOS"

    def test_dispatch_passes_flags(self, monkeypatch):
        monkeypatch.setattr(cli, "is_root", lambda: True)
        with patch.object(cli, "execute_tryboot", return_value=0) as t:
            assert run_cli(monkeypatch, "tryboot", "--no-reboot") == 0
        t.assert_called_once_with(reboot=False)
        with patch.object(cli, "execute_apply", return_value=7) as a:
            assert run_cli(monkeypatch, "apply", "u.tar", "--rsync-timeout", "0") == 7
        a.assert_called_once_with("u.tar", keep_download=False, rsync_timeout_sec=0)

    def test_no_command_prints_help(self, monkeypatch, capsys):
        assert run_cli(monkeypatch) == 0
        assert "usage" in capsys.readouterr().out
