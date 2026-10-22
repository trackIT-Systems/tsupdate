"""Tests for the tsupdated daemon: config loading, behaviour selection, persist and the update workflow."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from tsupdate import daemon
from tsupdate import state as update_state
from tsupdate.errors import ConfigurationError, TransientError, UpdateError


@pytest.fixture(autouse=True)
def reset_daemon_flags(monkeypatch):
    monkeypatch.setattr(daemon, "_cancel_update", False)
    monkeypatch.setattr(daemon, "_shutdown_requested", False)


@pytest.fixture(autouse=True)
def isolated_update_state(monkeypatch, tmp_path):
    """Never touch /data, and pin the running version the daemon sees."""
    monkeypatch.setattr(update_state, "STATE_FILE", tmp_path / "state" / "tsupdate-state.json")
    monkeypatch.setattr(daemon, "_running_version", lambda: "2025.12.3")


@pytest.fixture
def no_wall(monkeypatch):
    monkeypatch.setattr(daemon, "notify_users", lambda msg: None)


def write_config(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "tsupdate.yml"
    p.write_text(text)
    return p


class TestLoadConfig:
    def test_missing_file_gives_defaults(self, tmp_path):
        cfg = daemon.load_config(tmp_path / "missing.yml")
        assert cfg["check_interval"] == 3600
        assert cfg["do"] == "check"
        assert cfg["rsync_timeout"] == 3600

    def test_empty_file_gives_defaults(self, tmp_path):
        assert daemon.load_config(write_config(tmp_path, ""))["do"] == "check"

    def test_example_config_loads(self):
        example = Path(__file__).parent.parent / "tsupdate.example.yml"
        cfg = daemon.load_config(example)
        assert cfg["check_interval"] == 3600
        assert cfg["maintenance_check_interval"] == 3600

    def test_user_values_and_maintenance_interval_default(self, tmp_path):
        cfg = daemon.load_config(write_config(tmp_path, "check_interval: 120\ndo: apply\nrsync_timeout: 0\n"))
        assert cfg["check_interval"] == 120
        assert cfg["maintenance_check_interval"] == 120
        assert cfg["do"] == "apply"
        assert cfg["rsync_timeout"] == 0

    def test_invalid_modes_fall_back(self, tmp_path):
        cfg = daemon.load_config(write_config(tmp_path, "do: yolo\nmaintenance_do: later\n"))
        assert cfg["do"] == "check"
        assert cfg["maintenance_do"] is None

    def test_interval_minimum_enforced(self, tmp_path):
        cfg = daemon.load_config(write_config(tmp_path, "check_interval: 5\nmaintenance_check_interval: 1\n"))
        assert cfg["check_interval"] == 60
        assert cfg["maintenance_check_interval"] == 60

    def test_zero_countdown_and_persist_timeout_are_valid(self, tmp_path):
        cfg = daemon.load_config(write_config(tmp_path, "update_countdown: 0\npersist_timeout: 0\n"))
        assert cfg["update_countdown"] == 0
        assert cfg["persist_timeout"] == 0

    @pytest.mark.parametrize(
        "text,key,default",
        [
            ("update_countdown: -5\n", "update_countdown", 60),
            ("persist_timeout: -1\n", "persist_timeout", 600),
            ("max_releases: 0\n", "max_releases", 5),
            ("max_releases: -1\n", "max_releases", 5),  # releases[:-1] would silently drop the newest
        ],
    )
    def test_out_of_range_values_use_the_default(self, tmp_path, text, key, default):
        assert daemon.load_config(write_config(tmp_path, text))[key] == default

    def test_null_values_use_the_default(self, tmp_path):
        cfg = daemon.load_config(write_config(tmp_path, "update_countdown:\nmax_releases: ~\n"))
        assert (cfg["update_countdown"], cfg["max_releases"]) == (60, 5)

    def test_negative_rsync_timeout_uses_default(self, tmp_path):
        assert daemon.load_config(write_config(tmp_path, "rsync_timeout: -1\n"))["rsync_timeout"] == 3600

    @pytest.mark.parametrize(
        "text",
        [
            "- a list\n",
            "update_countdown: soon\n",
            "update_countdown: 1.5\n",
            "persist_timeout: '10m'\n",
            "max_releases: five\n",
            "max_releases: true\n",
            "check_interval: soon\n",
            "maintenance_check_interval: 1.5\n",
            "rsync_timeout: true\n",
            "check_interval: [\n",
        ],
    )
    def test_invalid_config_exits(self, tmp_path, text):
        with pytest.raises(SystemExit):
            daemon.load_config(write_config(tmp_path, text))


class TestCurrentBehavior:
    CFG = {"check_interval": 3600, "do": "check", "maintenance_check_interval": 300, "maintenance_do": None}

    def test_no_schedule(self):
        assert daemon.get_current_behavior(self.CFG, None, False) == (3600, "check")

    def test_inside_window_defaults_to_apply(self):
        assert daemon.get_current_behavior(self.CFG, {"name": "maintenance"}, True) == (300, "apply")

    def test_inside_window_explicit_mode(self):
        cfg = dict(self.CFG, maintenance_do="download")
        assert daemon.get_current_behavior(cfg, {"name": "maintenance"}, True) == (300, "download")

    def test_outside_window(self):
        assert daemon.get_current_behavior(self.CFG, {"name": "maintenance"}, False) == (3600, "check")


class TestCountdown:
    def test_completes(self, monkeypatch, no_wall):
        sleeps = []
        monkeypatch.setattr(daemon.time, "sleep", sleeps.append)
        assert daemon.countdown_with_cancel(3) is True
        assert len(sleeps) == 3

    def test_cancelled_by_signal(self, monkeypatch, no_wall):
        def sleep(_):
            daemon.signal_handler(15, None)  # SIGTERM arrives mid-countdown

        monkeypatch.setattr(daemon.time, "sleep", sleep)
        assert daemon.countdown_with_cancel(30) is False


class TestTrybootPersist:
    def test_regular_boot_does_nothing(self, booted_from):
        booted_from(2, tryboot_active=False)
        with patch.object(daemon, "persist_boot_configuration") as persist:
            daemon.handle_tryboot_persist(600)
        persist.assert_not_called()

    def test_persists_immediately_when_uptime_exceeded(self, booted_from, monkeypatch):
        booted_from(3, tryboot_active=True)
        monkeypatch.setattr(daemon, "get_system_uptime", lambda: 900.0)
        monkeypatch.setattr(daemon.time, "sleep", lambda s: pytest.fail("should not sleep"))
        with patch.object(daemon, "persist_boot_configuration", return_value=True) as persist:
            daemon.handle_tryboot_persist(600)
        persist.assert_called_once()

    def test_waits_for_remaining_uptime(self, booted_from, monkeypatch):
        booted_from(3, tryboot_active=True)
        monkeypatch.setattr(daemon, "get_system_uptime", lambda: 575.0)
        sleeps = []
        monkeypatch.setattr(daemon.time, "sleep", sleeps.append)
        with patch.object(daemon, "persist_boot_configuration", return_value=True) as persist:
            daemon.handle_tryboot_persist(600)
        assert sum(sleeps) == pytest.approx(25.0)
        persist.assert_called_once()

    def test_logs_progress_about_once_a_minute(self, booted_from, monkeypatch, caplog):
        booted_from(3, tryboot_active=True)
        # a fractional uptime, as read from /proc/uptime: never an exact multiple of anything
        monkeypatch.setattr(daemon, "get_system_uptime", lambda: 12.34)
        monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
        with patch.object(daemon, "persist_boot_configuration", return_value=True), caplog.at_level("DEBUG"):
            daemon.handle_tryboot_persist(312)  # waits 299.66s
        progress = [r for r in caplog.records if "Persisting tryboot configuration in" in r.message]
        assert len(progress) == 4  # after ~60s, 120s, 180s and 240s
        assert all(r.levelname == "DEBUG" for r in progress)

    def test_returns_whether_the_boot_config_is_settled(self, booted_from, monkeypatch):
        booted_from(3, tryboot_active=True)
        monkeypatch.setattr(daemon, "get_system_uptime", lambda: 900.0)
        with patch.object(daemon, "persist_boot_configuration", return_value=True):
            assert daemon.handle_tryboot_persist(600) is True
        with patch.object(daemon, "persist_boot_configuration", side_effect=UpdateError("tryline.txt does not exist")):
            assert daemon.handle_tryboot_persist(600) is False
        booted_from(2, tryboot_active=False)
        assert daemon.handle_tryboot_persist(600) is True

    def test_shutdown_during_wait_does_not_persist(self, booted_from, monkeypatch):
        booted_from(3, tryboot_active=True)
        monkeypatch.setattr(daemon, "get_system_uptime", lambda: 0.0)
        monkeypatch.setattr(daemon.time, "sleep", lambda s: daemon.signal_handler(15, None))
        with patch.object(daemon, "persist_boot_configuration") as persist, pytest.raises(SystemExit):
            daemon.handle_tryboot_persist(600)
        persist.assert_not_called()


class TestCheckForUpdates:
    def test_uses_os_release(self, tmp_path, monkeypatch):
        rel = tmp_path / "os-release"
        rel.write_text('VERSION_ID="2025.12.3"\nSUPPORT_URL="https://github.com/trackIT-Systems/tsOS-base"\n')
        from tsupdate.status import read_os_release

        monkeypatch.setattr(daemon, "read_booted_os_release", lambda: read_os_release(rel))
        with patch.object(daemon, "fetch_releases", return_value=["r"]) as fetch, patch.object(
            daemon, "find_applicable_batch_update", return_value="https://url"
        ) as find:
            assert daemon.check_for_updates({"include_prereleases": True, "max_releases": 3}) == "https://url"
        fetch.assert_called_once_with("trackIT-Systems", "tsOS-base", include_prereleases=True, limit=3)
        find.assert_called_once_with(["r"], "2025.12.3", max_releases=3)

    def test_disallowed_owner_in_config_is_rejected(self, tmp_path, monkeypatch):
        rel = tmp_path / "os-release"
        rel.write_text('VERSION_ID="2025.12.3"\n')
        from tsupdate.status import read_os_release

        monkeypatch.setattr(daemon, "read_booted_os_release", lambda: read_os_release(rel))
        with patch.object(daemon, "fetch_releases") as fetch, pytest.raises(
            ConfigurationError, match="Not an allowed update source: https://github.com/evil/tsOS-base"
        ):
            daemon.check_for_updates({"github_url": "https://github.com/evil/tsOS-base"})
        fetch.assert_not_called()

    def test_github_errors_are_raised_not_swallowed(self, tmp_path, monkeypatch):
        rel = tmp_path / "os-release"
        rel.write_text('VERSION_ID="2025.12.3"\nSUPPORT_URL="https://github.com/trackIT-Systems/tsOS-base"\n')
        from tsupdate.status import read_os_release

        monkeypatch.setattr(daemon, "read_booted_os_release", lambda: read_os_release(rel))
        with (
            patch.object(daemon, "fetch_releases", side_effect=TransientError("Cannot reach GitHub: down")),
            pytest.raises(TransientError),
        ):
            daemon.check_for_updates({})

    def test_no_releases_is_no_update(self, tmp_path, monkeypatch):
        rel = tmp_path / "os-release"
        rel.write_text('VERSION_ID="2025.12.3"\nSUPPORT_URL="https://github.com/trackIT-Systems/tsOS-base"\n')
        from tsupdate.status import read_os_release

        monkeypatch.setattr(daemon, "read_booted_os_release", lambda: read_os_release(rel))
        with patch.object(daemon, "fetch_releases", return_value=[]):
            assert daemon.check_for_updates({}) is None


class TestUpdateWorkflow:
    CFG = {"rsync_timeout": 10, "update_countdown": 0}

    def test_happy_path_order(self, no_wall, tmp_path):
        calls = []
        with patch.object(daemon, "execute_syncroot", side_effect=lambda **kw: calls.append(("sync", kw)) or 0), patch.object(
            daemon, "execute_apply", side_effect=lambda *a, **kw: calls.append(("apply", a, kw)) or 0
        ), patch.object(daemon, "execute_tryboot", side_effect=lambda **kw: calls.append(("tryboot", kw)) or 0):
            assert daemon.sync_and_apply_update(tmp_path / "u.tar", self.CFG)
        assert [c[0] for c in calls] == ["sync", "apply", "tryboot"]
        assert calls[0][1] == {"rsync_timeout_sec": 10}
        assert calls[2][1] == {"reboot": True}

    def test_sync_failure_aborts(self, tmp_path, monkeypatch):
        wall = []
        monkeypatch.setattr(daemon, "notify_users", wall.append)
        with patch.object(daemon, "execute_syncroot", side_effect=UpdateError("Rsync failed")), patch.object(
            daemon, "execute_apply"
        ) as apply_, patch.object(daemon, "execute_tryboot") as tb, pytest.raises(UpdateError):
            daemon.sync_and_apply_update(tmp_path / "u.tar", self.CFG)
        apply_.assert_not_called()
        tb.assert_not_called()
        assert wall[-1] == "Update failed: Rsync failed"

    def test_apply_failure_aborts(self, no_wall, tmp_path):
        with patch.object(daemon, "execute_syncroot", return_value=0), patch.object(
            daemon, "execute_apply", side_effect=UpdateError("Rsync failed with exit code 23", exit_code=23)
        ), patch.object(daemon, "execute_tryboot") as tb, pytest.raises(UpdateError):
            daemon.sync_and_apply_update(tmp_path / "u.tar", self.CFG)
        tb.assert_not_called()

    def test_cancelled_countdown_does_not_reboot(self, no_wall, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon, "countdown_with_cancel", lambda s: False)
        with patch.object(daemon, "execute_syncroot", return_value=0), patch.object(
            daemon, "execute_apply", return_value=0
        ), patch.object(daemon, "execute_tryboot") as tb:
            assert not daemon.sync_and_apply_update(tmp_path / "u.tar", self.CFG)
        tb.assert_not_called()

    def test_download_failure_aborts(self, no_wall, monkeypatch):
        def offline(url, cfg):
            raise TransientError("Cannot reach GitHub: down")

        monkeypatch.setattr(daemon, "download_update", offline)
        with patch.object(daemon, "sync_and_apply_update") as s, pytest.raises(TransientError):
            daemon.apply_update_workflow("https://url", self.CFG)
        s.assert_not_called()


class TestMainLoop:
    def run_one_iteration(self, monkeypatch, config, maintenance_entry=None, in_window=False, update=None):
        """Run main_loop until its first sleep, then request shutdown. Returns total seconds slept."""
        slept = []

        def sleep(s):
            slept.append(s)
            daemon._shutdown_requested = True

        monkeypatch.setattr(daemon.time, "sleep", sleep)
        monkeypatch.setattr(daemon, "is_in_maintenance_window", lambda e: in_window)
        monkeypatch.setattr(daemon, "check_for_updates", lambda cfg: update)
        daemon.main_loop(config, maintenance_entry=maintenance_entry)
        return slept

    def test_check_mode_never_applies(self, monkeypatch):
        with patch.object(daemon, "apply_update_workflow") as wf, patch.object(daemon, "download_update") as dl:
            self.run_one_iteration(monkeypatch, {"check_interval": 60, "do": "check"}, update="https://u")
        wf.assert_not_called()
        dl.assert_not_called()

    def test_download_mode_only_downloads(self, monkeypatch):
        with patch.object(daemon, "apply_update_workflow") as wf, patch.object(
            daemon, "download_update", return_value=Path("/x")
        ) as dl:
            self.run_one_iteration(monkeypatch, {"check_interval": 60, "do": "download"}, update="https://u")
        dl.assert_called_once()
        wf.assert_not_called()

    def test_maintenance_window_applies(self, monkeypatch):
        cfg = {"check_interval": 3600, "do": "check", "maintenance_check_interval": 60, "maintenance_do": None}
        with patch.object(daemon, "apply_update_workflow", return_value=False) as wf:
            self.run_one_iteration(monkeypatch, cfg, maintenance_entry={"name": "maintenance"}, in_window=True, update="https://u")
        wf.assert_called_once()

    def test_does_not_sleep_past_next_maintenance_window(self, monkeypatch):
        cfg = {"check_interval": 3600, "do": "check", "maintenance_check_interval": 60, "maintenance_do": "apply"}
        monkeypatch.setattr(
            daemon, "get_next_maintenance_window_start", lambda e: datetime.now() + timedelta(seconds=600)
        )
        slept = []

        def sleep(s):
            slept.append(s)
            if sum(slept) >= 3600:
                daemon._shutdown_requested = True

        monkeypatch.setattr(daemon.time, "sleep", sleep)
        monkeypatch.setattr(daemon, "is_in_maintenance_window", lambda e: sum(slept) >= 600)
        monkeypatch.setattr(daemon, "check_for_updates", lambda c: None)
        checks = []
        monkeypatch.setattr(daemon, "check_for_updates", lambda c: checks.append(sum(slept)))
        daemon.main_loop(cfg, maintenance_entry={"name": "maintenance"})
        # There must be a check at (or shortly after) the window start at t=600s.
        assert any(600 <= t <= 660 for t in checks), checks


MAINTENANCE_ENTRY = {"name": "maintenance", "start": "02:00", "stop": "02:30"}


class TestMainLoopLogging:
    """A state that repeats on every iteration is logged once, when it changes."""

    def run(self, monkeypatch, caplog, config, results, in_window=None, maintenance_entry=None):
        """
        Run one main_loop iteration per entry of `results`: an exception is raised by the update
        check, anything else is returned as the found update URL. Returns the log records.
        """
        results = list(results)
        windows = list(in_window or [False] * len(results))
        iterations = len(results)
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            if len(sleeps) >= iterations:
                daemon._shutdown_requested = True

        def check(cfg):
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        # one sleep per iteration: until the next check, or 60 s after an error
        monkeypatch.setattr(daemon, "sleep_interruptible", sleep)
        monkeypatch.setattr(daemon.time, "sleep", sleep)
        monkeypatch.setattr(daemon, "check_for_updates", check)
        monkeypatch.setattr(daemon, "is_in_maintenance_window", lambda e: windows.pop(0))
        monkeypatch.setattr(
            daemon, "get_next_maintenance_window_start", lambda e: datetime.now(UTC) + timedelta(days=1)
        )
        with caplog.at_level("DEBUG", logger="tsupdate"):
            daemon.main_loop(config, maintenance_entry=maintenance_entry)
        return [r for r in caplog.records if r.name == "tsupdate.daemon"]

    @staticmethod
    def messages(records, level):
        return [r.getMessage() for r in records if r.levelname == level]

    def test_unexpected_error_is_logged_once_with_traceback(self, monkeypatch, caplog):
        boom = RuntimeError("bug")
        records = self.run(monkeypatch, caplog, {"do": "check"}, [boom, boom, boom, None, None])
        assert self.messages(records, "ERROR") == ["Checking for updates failed: RuntimeError: bug"]
        error = next(r for r in records if r.levelname == "ERROR")
        assert error.exc_info is not None
        again = [r for r in records if r.getMessage().startswith("Checking for updates failed again")]
        assert len(again) == 2
        assert all(r.levelname == "DEBUG" for r in again)
        assert self.messages(records, "INFO").count("Checking for updates works again") == 1

    def test_changed_error_is_logged_again(self, monkeypatch, caplog):
        errors = [RuntimeError("a"), RuntimeError("b"), RuntimeError("b")]
        records = self.run(monkeypatch, caplog, {"do": "check"}, errors)
        assert self.messages(records, "ERROR") == [
            "Checking for updates failed: RuntimeError: a",
            "Checking for updates failed: RuntimeError: b",
        ]
        assert "Checking for updates works again" not in self.messages(records, "INFO")

    def test_offline_station_warns_once_and_recovers_once(self, monkeypatch, caplog):
        offline = TransientError("Cannot reach GitHub: Failed to resolve 'api.github.com'")
        records = self.run(monkeypatch, caplog, {"do": "check"}, [offline] * 4 + [None, None])
        assert self.messages(records, "WARNING") == [
            "Checking for updates failed: Cannot reach GitHub: Failed to resolve 'api.github.com'"
        ]
        assert not self.messages(records, "ERROR")
        assert all(r.exc_info is None for r in records if r.levelno >= 30)  # expected: no traceback
        again = [m for m in self.messages(records, "DEBUG") if m.startswith("Checking for updates failed again")]
        assert len(again) == 3
        assert self.messages(records, "INFO").count("Checking for updates works again") == 1

    def test_failed_check_waits_the_usual_interval(self, monkeypatch, caplog):
        """A failed check is retried at the next check, as before (not after the main loop's 60 s)."""
        slept = []

        def sleep(s):
            slept.append(s)
            if len(slept) >= 2:
                daemon._shutdown_requested = True

        def offline(cfg):
            raise TransientError("Cannot reach GitHub: down")

        monkeypatch.setattr(daemon, "sleep_interruptible", sleep)
        monkeypatch.setattr(daemon.time, "sleep", lambda s: pytest.fail("main loop error path taken"))
        monkeypatch.setattr(daemon, "check_for_updates", offline)
        daemon.main_loop({"do": "check", "check_interval": 900})
        assert slept == [900, 900]

    def test_missing_repository_is_one_error(self, monkeypatch, caplog):
        missing = ConfigurationError("Repository not found: trackIT-Systems/nope")
        records = self.run(monkeypatch, caplog, {"do": "check"}, [missing] * 3)
        assert self.messages(records, "ERROR") == ["Checking for updates failed: Repository not found: trackIT-Systems/nope"]
        assert all(r.exc_info is None for r in records if r.levelno >= 30)
        assert not self.messages(records, "WARNING")

    def test_switch_from_transient_to_configuration_error_is_logged(self, monkeypatch, caplog):
        offline = TransientError("Cannot reach GitHub: down")
        missing = ConfigurationError("Repository not found: trackIT-Systems/nope")
        records = self.run(monkeypatch, caplog, {"do": "check"}, [offline, offline, missing, missing, offline])
        assert [(r.levelname, r.getMessage()) for r in records if r.levelno >= 30] == [
            ("WARNING", "Checking for updates failed: Cannot reach GitHub: down"),
            ("ERROR", "Checking for updates failed: Repository not found: trackIT-Systems/nope"),
            ("WARNING", "Checking for updates failed: Cannot reach GitHub: down"),
        ]
        assert "Checking for updates works again" not in self.messages(records, "INFO")

    def test_pending_update_is_not_reannounced_after_a_failed_check(self, monkeypatch, caplog):
        offline = TransientError("Cannot reach GitHub: down")
        records = self.run(monkeypatch, caplog, {"do": "check"}, ["https://u", offline, "https://u"])
        assert len([m for m in self.messages(records, "INFO") if m.startswith("Update found")]) == 1

    def test_failed_download_is_logged_once_and_retried(self, monkeypatch, caplog):
        calls = []

        def download(url, cfg):
            calls.append(url)
            if len(calls) <= 3:
                raise TransientError("Download of https://u interrupted at 5 of 10 bytes")
            return Path("/data/u.tar")

        monkeypatch.setattr(daemon, "download_update", download)
        records = self.run(monkeypatch, caplog, {"do": "download"}, ["https://u"] * 4)
        assert len(calls) == 4
        assert self.messages(records, "WARNING") == [
            "Downloading the update failed: Download of https://u interrupted at 5 of 10 bytes"
        ]
        assert self.messages(records, "INFO").count("Downloading the update works again") == 1

    def test_a_download_failure_ends_with_the_next_successful_apply(self, monkeypatch, caplog):
        # download mode fails; then the window opens and the apply workflow downloads fine
        modes = iter(["download", "apply", "download"])
        monkeypatch.setattr(daemon, "get_current_behavior", lambda config, entry, in_window: (60, next(modes)))
        monkeypatch.setattr(daemon, "download_update", lambda url, cfg: (_ for _ in ()).throw(TransientError("offline")))
        with patch.object(daemon, "apply_update_workflow", return_value=False):
            records = self.run(monkeypatch, caplog, {"do": "download"}, ["https://u"] * 3)
        # the second download failure is a new one, logged at WARNING again
        assert self.messages(records, "WARNING") == ["Downloading the update failed: offline"] * 2
        assert self.messages(records, "INFO").count("Downloading the update works again") == 1

    def test_failed_apply_is_logged_once_and_retried(self, monkeypatch, caplog):
        error = UpdateError("Rsync failed with exit code 23: Partial transfer", exit_code=23)
        with patch.object(daemon, "apply_update_workflow", side_effect=error) as wf:
            records = self.run(monkeypatch, caplog, {"do": "apply"}, ["https://u"] * 3)
        assert wf.call_count == 3
        assert self.messages(records, "ERROR") == [
            "Applying the update failed: Rsync failed with exit code 23: Partial transfer"
        ]
        assert all(r.exc_info is None for r in records if r.levelno >= 30)

    def test_unexpected_error_outside_the_stages_retries_after_60s(self, monkeypatch, caplog):
        monkeypatch.setattr(daemon, "get_current_behavior", lambda *a: (_ for _ in ()).throw(RuntimeError("bug")))
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            if len(sleeps) >= 2:
                daemon._shutdown_requested = True

        monkeypatch.setattr(daemon.time, "sleep", sleep)
        with caplog.at_level("DEBUG", logger="tsupdate"):
            daemon.main_loop({"do": "check"})
        assert sleeps == [60, 60]
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert [r.getMessage() for r in errors] == ["Main loop failed: RuntimeError: bug"]
        assert errors[0].exc_info is not None

    def test_invalid_maintenance_window_is_logged_once(self, monkeypatch, caplog):
        """Evaluated as outside the window and with check_interval as delay, as before; one ERROR."""
        bad = ConfigurationError("Cannot evaluate the maintenance window (02:00 - 3:OO): bad time")

        def raise_bad(entry):
            raise bad

        cfg = {"do": "check", "check_interval": 900, "maintenance_do": "apply"}
        slept = []
        checks = []

        def sleep(s):
            slept.append(s)
            if len(slept) >= 3:
                daemon._shutdown_requested = True

        monkeypatch.setattr(daemon, "sleep_interruptible", sleep)
        monkeypatch.setattr(daemon.time, "sleep", lambda s: pytest.fail("main loop error path taken"))
        monkeypatch.setattr(daemon, "check_for_updates", lambda c: checks.append(c) or None)
        monkeypatch.setattr(daemon, "is_in_maintenance_window", raise_bad)
        monkeypatch.setattr(daemon, "get_next_maintenance_window_start", raise_bad)
        with caplog.at_level("DEBUG", logger="tsupdate"):
            daemon.main_loop(cfg, maintenance_entry=MAINTENANCE_ENTRY)
        records = [r for r in caplog.records if r.name == "tsupdate.daemon"]
        assert slept == [900, 900, 900]
        assert len(checks) == 3  # still checked, in mode "check" (outside the window)
        assert self.messages(records, "ERROR") == [
            "Evaluating the maintenance window failed: Cannot evaluate the maintenance window (02:00 - 3:OO): bad time"
        ]
        assert not self.messages(records, "WARNING")

    def test_next_window_fallback_and_recovery(self, monkeypatch, caplog):
        """next_check_delay() raising falls back to check_interval; logged once, recovery once."""
        results = [ConfigurationError("Cannot determine when the maintenance window (02:00 - 02:30) starts")] * 2

        def next_start(entry):
            if results:
                raise results.pop(0)
            return datetime.now(UTC) + timedelta(days=1)

        slept = []

        def sleep(s):
            slept.append(s)
            if len(slept) >= 3:
                daemon._shutdown_requested = True

        monkeypatch.setattr(daemon, "sleep_interruptible", sleep)
        monkeypatch.setattr(daemon, "check_for_updates", lambda c: None)
        monkeypatch.setattr(daemon, "is_in_maintenance_window", lambda e: False)
        monkeypatch.setattr(daemon, "get_next_maintenance_window_start", next_start)
        with caplog.at_level("DEBUG", logger="tsupdate"):
            daemon.main_loop({"do": "check", "check_interval": 900}, maintenance_entry=MAINTENANCE_ENTRY)
        records = [r for r in caplog.records if r.name == "tsupdate.daemon"]
        assert slept == [900, 900, 900]
        assert len(self.messages(records, "ERROR")) == 1
        assert self.messages(records, "INFO").count("Evaluating the maintenance window works again") == 1

    def test_pending_update_is_logged_once(self, monkeypatch, caplog):
        updates = ["https://u1", "https://u1", "https://u1", "https://u2"]
        records = self.run(monkeypatch, caplog, {"do": "check"}, updates)
        found = [m for m in self.messages(records, "INFO") if m.startswith("Update found")]
        assert found == [
            "Update found (mode: check), deferring update until the next check: https://u1",
            "Update found (mode: check), deferring update until the next check: https://u2",
        ]
        assert len([m for m in self.messages(records, "DEBUG") if m.startswith("Update found")]) == 2

    def test_update_found_again_after_it_disappeared(self, monkeypatch, caplog):
        records = self.run(monkeypatch, caplog, {"do": "check"}, ["https://u", None, "https://u"])
        assert len([m for m in self.messages(records, "INFO") if m.startswith("Update found")]) == 2

    def test_pending_downloaded_update_is_logged_once(self, monkeypatch, caplog):
        monkeypatch.setattr(daemon, "download_update", lambda url, cfg: Path("/data/u.tar"))
        records = self.run(monkeypatch, caplog, {"do": "download"}, ["https://u"] * 3)
        info = [m for m in self.messages(records, "INFO") if m.startswith("Update")]
        assert len(info) == 2  # found, downloaded
        assert info[1].startswith("Update downloaded successfully to /data/u.tar")

    def test_blocked_update_is_warned_about_once(self, monkeypatch, caplog):
        monkeypatch.setattr(update_state, "is_blocked", lambda url, version: True)
        with patch.object(daemon, "apply_update_workflow") as wf:
            records = self.run(monkeypatch, caplog, {"do": "apply"}, ["https://u"] * 3)
        wf.assert_not_called()
        assert len([m for m in self.messages(records, "WARNING") if m.startswith("Not applying https://u")]) == 1

    def test_maintenance_window_is_logged_on_transitions_only(self, monkeypatch, caplog):
        cfg = {"do": "check", "maintenance_do": "check"}
        windows = [False, False, True, True, False, False]
        records = self.run(monkeypatch, caplog, cfg, [None] * 6, in_window=windows, maintenance_entry=MAINTENANCE_ENTRY)
        assert [m for m in self.messages(records, "INFO") if "maintenance window" in m] == [
            "Starting outside the maintenance window (02:00 - 02:30)",
            "Entering maintenance window (02:00 - 02:30)",
            "Leaving maintenance window (02:00 - 02:30)",
        ]

    def test_nothing_mode_inside_window_is_warned_about_once_per_window(self, monkeypatch, caplog):
        cfg = {"do": "nothing", "maintenance_do": "nothing"}
        windows = [True, True, False, True]
        records = self.run(monkeypatch, caplog, cfg, [None] * 4, in_window=windows, maintenance_entry=MAINTENANCE_ENTRY)
        assert len(self.messages(records, "WARNING")) == 2

    def test_quiet_in_normal_operation(self, monkeypatch, caplog):
        records = self.run(monkeypatch, caplog, {"do": "check"}, [None] * 5)
        assert self.messages(records, "INFO") == [
            "Entering main update check loop",
            "No maintenance schedule configured - check_interval=3600s, do=check",
            "Shutdown requested - exiting main loop",
        ]
        assert not [r for r in records if r.levelno >= 30]


class TestNextCheckDelay:
    ENTRY = {"name": "maintenance", "start": "02:00", "stop": "02:30"}

    def delay(self, monkeypatch, until, mode="check", in_window=False, entry=ENTRY, interval=3600):
        monkeypatch.setattr(
            daemon, "get_next_maintenance_window_start", lambda e: datetime.now() + timedelta(seconds=until)
        )
        return daemon.next_check_delay(interval, mode, entry, in_window)

    def test_no_schedule_or_inside_window_uses_interval(self, monkeypatch):
        assert self.delay(monkeypatch, 600, entry=None) == 3600
        assert self.delay(monkeypatch, 600, in_window=True) == 3600

    def test_capped_at_next_window(self, monkeypatch):
        assert self.delay(monkeypatch, 600) == pytest.approx(601, abs=2)

    def test_distant_window_keeps_interval(self, monkeypatch):
        assert self.delay(monkeypatch, 36000) == 3600

    def test_nothing_mode_sleeps_until_window(self, monkeypatch):
        assert self.delay(monkeypatch, 36000, mode="nothing") == pytest.approx(36001, abs=2)

    @pytest.mark.parametrize("until", [-5, 0])
    def test_past_start_is_an_error(self, monkeypatch, until):
        with pytest.raises(UpdateError, match=r"Next start of the maintenance window \(02:00 - 02:30\) is in the past"):
            self.delay(monkeypatch, until)

    def test_unknown_start_is_a_configuration_error(self, monkeypatch):
        def fail(entry):
            raise ConfigurationError("Cannot evaluate the maintenance window (02:00 - 3:OO): bad time")

        monkeypatch.setattr(daemon, "get_next_maintenance_window_start", fail)
        with pytest.raises(ConfigurationError):
            daemon.next_check_delay(3600, "check", self.ENTRY, False)


class TestFailedUpdateDoesNotLoop:
    """A new partition that fails to boot makes the Pi fall back to the old one, which then finds the
    same update again. That must not repeat forever."""

    CFG = {"check_interval": 60, "do": "apply", "update_countdown": 0, "rsync_timeout": 1}

    @pytest.fixture
    def reboots(self, monkeypatch, tmp_path, no_wall):
        reboots = []
        monkeypatch.setattr(daemon, "check_for_updates", lambda c: "https://u")
        monkeypatch.setattr(daemon, "download_update", lambda u, c: tmp_path / "u.tar")
        monkeypatch.setattr(daemon, "execute_syncroot", lambda **kw: 0)
        monkeypatch.setattr(daemon, "execute_apply", lambda *a, **kw: 0)
        monkeypatch.setattr(daemon, "countdown_with_cancel", lambda s: True)
        monkeypatch.setattr(daemon, "execute_tryboot", lambda **kw: reboots.append(1) or 0)
        return reboots

    def boot_daemon(self, monkeypatch):
        """One daemon lifetime: runs until it reboots (SystemExit) or reaches its first sleep."""
        monkeypatch.setattr(daemon, "_shutdown_requested", False)
        monkeypatch.setattr(daemon.time, "sleep", lambda s: setattr(daemon, "_shutdown_requested", True))
        try:
            daemon.main_loop(self.CFG)
        except SystemExit:
            pass

    def test_gives_up_after_max_attempts(self, monkeypatch, reboots):
        for _ in range(6):  # six boots, each of which falls back to the old partition
            self.boot_daemon(monkeypatch)
        assert len(reboots) == update_state.MAX_UPDATE_ATTEMPTS

    def test_new_update_is_still_applied(self, monkeypatch, reboots):
        for _ in range(4):
            self.boot_daemon(monkeypatch)
        monkeypatch.setattr(daemon, "check_for_updates", lambda c: "https://newer")
        self.boot_daemon(monkeypatch)
        assert len(reboots) == update_state.MAX_UPDATE_ATTEMPTS + 1

    def test_attempt_is_recorded_before_the_reboot(self, monkeypatch, reboots, tmp_path):
        seen = []
        monkeypatch.setattr(
            daemon, "execute_tryboot", lambda **kw: seen.append(update_state._load().get("attempts")) or 0
        )
        daemon.sync_and_apply_update(tmp_path / "u.tar", self.CFG, "https://u")
        assert seen == [1]

    def test_attempt_discarded_when_tryboot_could_not_be_configured(self, monkeypatch, reboots, tmp_path):
        def fail(**kw):
            raise UpdateError("config.txt does not exist")

        monkeypatch.setattr(daemon, "execute_tryboot", fail)
        with pytest.raises(UpdateError):
            daemon.sync_and_apply_update(tmp_path / "u.tar", self.CFG, "https://u")
        assert update_state._load() == {}


class TestCancellation:
    CFG = {"update_countdown": 30, "rsync_timeout": 1}

    @pytest.fixture
    def pipeline(self, monkeypatch, no_wall):
        monkeypatch.setattr(daemon, "execute_syncroot", lambda **kw: 0)
        monkeypatch.setattr(daemon, "execute_apply", lambda *a, **kw: 0)

    def test_sigterm_in_countdown_cancels_and_is_remembered(self, pipeline, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon.time, "sleep", lambda s: daemon.signal_handler(15, None))
        with patch.object(daemon, "execute_tryboot") as tb:
            assert not daemon.sync_and_apply_update(tmp_path / "u.tar", self.CFG, "https://u")
        tb.assert_not_called()
        # systemd restarts the daemon after it exits: it must not pick the same update up again
        assert update_state.is_blocked("https://u", "2025.12.3")

    def test_restarted_daemon_does_not_reapply_cancelled_update(self, pipeline, monkeypatch, tmp_path):
        update_state.block("https://u", "2025.12.3")
        monkeypatch.setattr(daemon, "check_for_updates", lambda c: "https://u")
        monkeypatch.setattr(daemon.time, "sleep", lambda s: setattr(daemon, "_shutdown_requested", True))
        with patch.object(daemon, "apply_update_workflow") as wf:
            daemon.main_loop({"check_interval": 60, "do": "apply"})
        wf.assert_not_called()

    def test_sigterm_during_apply_prevents_the_reboot(self, pipeline, monkeypatch, tmp_path):
        """SIGTERM arrives while rsync is still running (before the countdown starts)."""
        monkeypatch.setattr(daemon, "execute_apply", lambda *a, **kw: daemon.signal_handler(15, None) or 0)
        monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
        with patch.object(daemon, "execute_tryboot") as tb:
            assert not daemon.sync_and_apply_update(tmp_path / "u.tar", self.CFG, "https://u")
        tb.assert_not_called()


class TestFinalizeBoot:
    """The cached update is removed once the new version runs and its boot config is settled."""

    @pytest.fixture
    def artifacts(self, tmp_path, monkeypatch):
        d = tmp_path / "artifacts"
        d.mkdir()
        (d / "update.tar").write_bytes(b"x")
        monkeypatch.setattr(daemon, "ARTIFACTS_DIR", d)
        monkeypatch.setattr(daemon, "handle_tryboot_persist", lambda timeout: True)
        return d

    def test_pruned_after_successful_update(self, artifacts):
        update_state.record_attempt("https://u", "2025.12.2")  # started from an older version
        daemon.finalize_boot(600)
        assert list(artifacts.iterdir()) == []
        assert update_state._load() == {}

    def test_kept_when_boot_config_is_not_settled(self, artifacts, monkeypatch):
        update_state.record_attempt("https://u", "2025.12.2")
        monkeypatch.setattr(daemon, "handle_tryboot_persist", lambda timeout: False)
        daemon.finalize_boot(600)
        assert (artifacts / "update.tar").exists()

    def test_kept_after_failed_boot_so_the_retry_needs_no_download(self, artifacts):
        update_state.record_attempt("https://u", "2025.12.3")  # still running the version it started from
        daemon.finalize_boot(600)
        assert (artifacts / "update.tar").exists()
        assert update_state.record_attempt("https://u", "2025.12.3") == 2

    def test_kept_without_a_recorded_update(self, artifacts):
        daemon.finalize_boot(600)
        assert (artifacts / "update.tar").exists()
