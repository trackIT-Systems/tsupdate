"""Tests for maintenance schedule parsing and CLI dispatch."""

import json
import sys
from unittest.mock import patch

import pytest

from tsupdate import cli, schedule
from tsupdate.errors import ConfigurationError, TransientError, UpdateError


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

    @pytest.mark.parametrize(
        "entry",
        [
            {"start": "02:00"},
            {"stop": "02:00"},
            "nope",
            {"start": "bogus", "stop": "03:00"},
            {"start": "02:00", "stop": "3:OO"},
            {"start": "02:00", "stop": "sunrise+bogus"},
        ],
    )
    def test_bad_entries_are_rejected(self, entry):
        """An unparseable start/stop (a typo) must not silently become midnight, i.e. a ~22h window."""
        with pytest.raises(ConfigurationError):
            schedule.is_in_maintenance_window(entry)
        with pytest.raises(ConfigurationError):
            schedule.get_next_maintenance_window_start(entry)

    def test_missing_scheduleparse(self, monkeypatch):
        monkeypatch.setattr(schedule, "ScheduleEntry", None)
        entry = {"start": "02:00", "stop": "02:30"}
        with pytest.raises(ConfigurationError, match="scheduleparse library is not available"):
            schedule.is_in_maintenance_window(entry)
        with pytest.raises(ConfigurationError, match="scheduleparse library is not available"):
            schedule.get_next_maintenance_window_start(entry)

    def test_valid_entry_has_next_start(self):
        assert schedule.get_next_maintenance_window_start({"start": "02:00", "stop": "02:30"}) is not None


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

    @pytest.mark.parametrize("value", ["0", "-3", "many"])
    def test_max_releases_must_be_a_positive_integer(self, monkeypatch, value):
        with patch.object(cli, "execute_check") as check:
            assert run_cli(monkeypatch, "check", "--max-releases", value) == 2  # argparse usage error
        check.assert_not_called()

    def test_check_passes_max_releases_as_fetch_limit(self, monkeypatch):
        from tsupdate.status import OSRelease

        monkeypatch.setattr(cli, "read_booted_os_release", lambda: OSRelease(
            {"VERSION_ID": "2025.12.3", "SUPPORT_URL": "https://github.com/trackIT-Systems/tsOS-base"}))
        with patch.object(cli, "fetch_releases", return_value=[]) as fetch:
            assert cli.execute_check(max_releases=7) == 0  # no releases: nothing found, not an error
        fetch.assert_called_once_with("trackIT-Systems", "tsOS-base", include_prereleases=False, limit=7)

    @pytest.mark.parametrize(
        "error,code",
        [
            (TransientError("Cannot reach GitHub: Failed to resolve 'api.github.com'"), 75),
            (ConfigurationError("Repository not found: trackIT-Systems/nope"), 78),
            (UpdateError("GitHub API error 422: Validation Failed"), 1),
        ],
    )
    def test_check_reports_the_error_and_its_exit_code(self, monkeypatch, capsys, error, code):
        from tsupdate.status import OSRelease

        monkeypatch.setattr(cli, "read_booted_os_release", lambda: OSRelease(
            {"VERSION_ID": "2025.12.3", "SUPPORT_URL": "https://github.com/trackIT-Systems/tsOS-base"}))
        with patch.object(cli, "fetch_releases", side_effect=error):
            assert run_cli(monkeypatch, "check") == code
        captured = capsys.readouterr()
        assert str(error) in captured.err
        assert "Traceback" not in captured.err
        assert "Not found" not in captured.out  # no "no update"-like output for a failure

    def test_check_with_disallowed_url_is_a_configuration_error(self, monkeypatch, capsys):
        from tsupdate.status import OSRelease

        monkeypatch.setattr(cli, "read_booted_os_release", lambda: OSRelease({"VERSION_ID": "2025.12.3"}))
        with patch.object(cli, "fetch_releases") as fetch:
            assert run_cli(monkeypatch, "check", "--github-url", "https://github.com/evil/x") == 78
        fetch.assert_not_called()
        assert "Not an allowed update source: https://github.com/evil/x" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv,target",
        [(["apply", "u.tar"], "execute_apply"), (["syncroot"], "execute_syncroot"), (["tryboot"], "execute_tryboot"),
         (["download", "u.tar"], "execute_download")],
    )
    def test_commands_exit_with_the_error_exit_code(self, monkeypatch, capsys, argv, target):
        monkeypatch.setattr(cli, "is_root", lambda: True)
        with patch.object(cli, target, side_effect=UpdateError("Rsync failed with exit code 23: x", exit_code=23)):
            assert run_cli(monkeypatch, *argv) == 23
        assert "ERROR: Rsync failed with exit code 23: x" in capsys.readouterr().err

    def test_unexpected_errors_are_not_swallowed(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["tsupdate", "check"])
        monkeypatch.setattr(cli, "initialize_github_token", lambda: None)
        with patch.object(cli, "execute_check", side_effect=KeyError("bug")), pytest.raises(KeyError):
            cli.main()
