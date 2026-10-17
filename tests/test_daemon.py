"""Tests for the tsupdated daemon: config loading, behaviour selection, persist and the update workflow."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from tsupdate import daemon


@pytest.fixture(autouse=True)
def reset_daemon_flags(monkeypatch):
    monkeypatch.setattr(daemon, "_cancel_update", False)
    monkeypatch.setattr(daemon, "_shutdown_requested", False)


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

    def test_negative_rsync_timeout_uses_default(self, tmp_path):
        assert daemon.load_config(write_config(tmp_path, "rsync_timeout: -1\n"))["rsync_timeout"] == 3600

    @pytest.mark.parametrize(
        "text",
        [
            "- a list\n",
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
        fetch.assert_called_once_with("trackIT-Systems", "tsOS-base", include_prereleases=True)
        find.assert_called_once_with(["r"], "2025.12.3", max_releases=3)

    def test_disallowed_owner_in_config_is_rejected(self, tmp_path, monkeypatch):
        rel = tmp_path / "os-release"
        rel.write_text('VERSION_ID="2025.12.3"\n')
        from tsupdate.status import read_os_release

        monkeypatch.setattr(daemon, "read_booted_os_release", lambda: read_os_release(rel))
        with patch.object(daemon, "fetch_releases") as fetch:
            assert daemon.check_for_updates({"github_url": "https://github.com/evil/tsOS-base"}) is None
        fetch.assert_not_called()


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

    def test_sync_failure_aborts(self, no_wall, tmp_path):
        with patch.object(daemon, "execute_syncroot", return_value=1), patch.object(
            daemon, "execute_apply"
        ) as apply_, patch.object(daemon, "execute_tryboot") as tb:
            assert not daemon.sync_and_apply_update(tmp_path / "u.tar", self.CFG)
        apply_.assert_not_called()
        tb.assert_not_called()

    def test_apply_failure_aborts(self, no_wall, tmp_path):
        with patch.object(daemon, "execute_syncroot", return_value=0), patch.object(
            daemon, "execute_apply", return_value=23
        ), patch.object(daemon, "execute_tryboot") as tb:
            assert not daemon.sync_and_apply_update(tmp_path / "u.tar", self.CFG)
        tb.assert_not_called()

    def test_cancelled_countdown_does_not_reboot(self, no_wall, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon, "countdown_with_cancel", lambda s: False)
        with patch.object(daemon, "execute_syncroot", return_value=0), patch.object(
            daemon, "execute_apply", return_value=0
        ), patch.object(daemon, "execute_tryboot") as tb:
            assert not daemon.sync_and_apply_update(tmp_path / "u.tar", self.CFG)
        tb.assert_not_called()

    def test_download_failure_aborts(self, no_wall, monkeypatch):
        monkeypatch.setattr(daemon, "download_update", lambda url, cfg: None)
        with patch.object(daemon, "sync_and_apply_update") as s:
            assert not daemon.apply_update_workflow("https://url", self.CFG)
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

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: outside the maintenance window the daemon sleeps a full check_interval, so a window "
        "shorter than check_interval can be skipped entirely and updates never get applied",
    )
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
