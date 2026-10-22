"""Tests for the tryboot / persist / rollback state machine on the boot partition files."""

import os
from unittest.mock import patch

import pytest

from tsupdate import tryboot
from tsupdate.errors import UpdateError


class TestModifyCmdline:
    @pytest.mark.parametrize(
        "cmdline,target,expected",
        [
            ("root=/dev/mmcblk0p2 rootwait", "/dev/mmcblk0p3", "root=/dev/mmcblk0p3 rootwait"),
            ("root=/dev/mmcblk0p3 rootwait", "/dev/mmcblk0p2", "root=/dev/mmcblk0p2 rootwait"),
            ("console=tty1 root=mmcblk0p2 quiet", "/dev/mmcblk0p3", "console=tty1 root=/dev/mmcblk0p3 quiet"),
            ("root=/dev/nvme0n1p2 rootwait", "/dev/nvme0n1p3", "root=/dev/nvme0n1p3 rootwait"),
            ("console=tty1 root=/dev/sda3 quiet", "/dev/sda2", "console=tty1 root=/dev/sda2 quiet"),
            # Only the root= parameter is touched
            ("root=/dev/sda2 overlayroot=tmpfs", "/dev/sda3", "root=/dev/sda3 overlayroot=tmpfs"),
        ],
    )
    def test_switches_root(self, cmdline, target, expected):
        assert tryboot.modify_cmdline_root(cmdline, target) == expected

    def test_no_root_is_left_unchanged(self):
        assert tryboot.modify_cmdline_root("console=tty1 quiet", "/dev/sda3") == "console=tty1 quiet"

    @pytest.mark.parametrize("disk,device", [("/dev/nvme0n1p", "/dev/nvme0n1p3"), ("/dev/sda", "/dev/sda3")])
    def test_tryline_stays_on_booted_disk(self, boot_dir, booted_from, disk, device):
        (boot_dir / "cmdline.txt").write_text(f"console=tty1 root={disk}2 rootwait\n")
        booted_from(2, disk=disk)
        with patch("subprocess.run"):
            assert tryboot.execute_tryboot(reboot=False) == 0
        assert (boot_dir / "tryline.txt").read_text() == f"console=tty1 root={device} rootwait\n"

    def test_missing_root_is_not_silently_ignored(self, boot_dir, booted_from):
        (boot_dir / "cmdline.txt").write_text("console=tty1 rootwait\n")
        booted_from(2)
        with patch("subprocess.run"), pytest.raises(UpdateError, match="No root= parameter"):
            tryboot.execute_tryboot(reboot=False)
        assert not (boot_dir / "tryline.txt").exists()


class TestExecuteTryboot:
    def test_writes_tryline_and_tryboot_txt(self, boot_dir, booted_from):
        booted_from(2)
        with patch("subprocess.run") as run:
            assert tryboot.execute_tryboot(reboot=False) == 0
        run.assert_not_called()

        assert "root=/dev/mmcblk0p3" in (boot_dir / "tryline.txt").read_text()
        # cmdline.txt itself must not change - that's what makes tryboot fail safe
        assert "root=/dev/mmcblk0p2" in (boot_dir / "cmdline.txt").read_text()
        tb = (boot_dir / "tryboot.txt").read_text()
        assert tb.startswith("[all]\narm_64bit=1\n")
        assert tb.rstrip().endswith("cmdline=tryline.txt")

    def test_reboots_with_tryboot_flag(self, boot_dir, booted_from):
        booted_from(3)
        with patch("subprocess.run") as run:
            assert tryboot.execute_tryboot(reboot=True) == 0
        run.assert_called_once_with(["reboot", "0 tryboot"], check=True)
        assert "root=/dev/mmcblk0p2" in (boot_dir / "tryline.txt").read_text()

    def test_refuses_when_trybooted_and_not_persisted(self, boot_dir, booted_from):
        booted_from(3, tryboot_active=True)
        (boot_dir / "tryline.txt").write_text("root=/dev/mmcblk0p3\n")
        with patch("subprocess.run") as run, pytest.raises(UpdateError, match="already booted via tryboot"):
            tryboot.execute_tryboot(reboot=True)
        run.assert_not_called()

    def test_allowed_when_trybooted_and_persisted(self, boot_dir, booted_from):
        booted_from(3, tryboot_active=True)
        (boot_dir / "tryline.txt").write_text((boot_dir / "cmdline.txt").read_text())
        with patch("subprocess.run"):
            assert tryboot.execute_tryboot(reboot=False) == 0

    def test_fails_without_config_txt(self, boot_dir, booted_from):
        booted_from(2)
        (boot_dir / "config.txt").unlink()
        with pytest.raises(UpdateError, match="config.txt does not exist"):
            tryboot.execute_tryboot(reboot=False)

    def test_failed_reboot_is_raised(self, boot_dir, booted_from):
        booted_from(2)
        with patch("subprocess.run", side_effect=FileNotFoundError("reboot")), pytest.raises(
            UpdateError, match='please reboot manually using reboot "0 tryboot"'
        ):
            tryboot.execute_tryboot(reboot=True)

    def test_add_cmdline_entry_is_idempotent(self, boot_dir):
        (boot_dir / "tryboot.txt").write_text("arm_64bit=1")  # no trailing newline
        tryboot.add_cmdline_entry()
        tryboot.add_cmdline_entry()
        assert (boot_dir / "tryboot.txt").read_text() == "arm_64bit=1\ncmdline=tryline.txt\n"


class TestPersist:
    def test_is_tryboot_persisted(self, boot_dir):
        assert not tryboot.is_tryboot_persisted()  # no tryline.txt
        (boot_dir / "tryline.txt").write_text("root=/dev/mmcblk0p3\n")
        assert not tryboot.is_tryboot_persisted()
        (boot_dir / "tryline.txt").write_text((boot_dir / "cmdline.txt").read_text().strip() + "\n\n")
        assert tryboot.is_tryboot_persisted()  # whitespace-insensitive

    def test_persist_copies_tryline_and_preserves_times(self, boot_dir):
        cmdline = boot_dir / "cmdline.txt"
        os.utime(cmdline, (1_000_000_000, 1_000_000_000))
        (boot_dir / "tryline.txt").write_text("root=/dev/mmcblk0p3 rootwait\n")

        tryboot.persist_boot_configuration()
        # stat before reading: with relatime, reading the file would bump atime itself
        st = cmdline.stat()
        assert st.st_mtime == 1_000_000_000
        assert st.st_atime == 1_000_000_000
        assert cmdline.read_text() == "root=/dev/mmcblk0p3 rootwait\n"
        assert tryboot.is_tryboot_persisted()

    def test_persist_without_tryline_fails_and_keeps_cmdline(self, boot_dir):
        before = (boot_dir / "cmdline.txt").read_text()
        with pytest.raises(UpdateError, match="tryline.txt does not exist"):
            tryboot.persist_boot_configuration()
        assert (boot_dir / "cmdline.txt").read_text() == before

    def test_execute_persist_requires_tryboot(self, boot_dir, booted_from):
        booted_from(2, tryboot_active=False)
        (boot_dir / "tryline.txt").write_text("root=/dev/mmcblk0p3\n")
        with pytest.raises(UpdateError, match="not booted via tryboot"):
            tryboot.execute_persist()
        assert "mmcblk0p2" in (boot_dir / "cmdline.txt").read_text()

    def test_execute_persist_in_tryboot(self, boot_dir, booted_from):
        booted_from(3, tryboot_active=True)
        (boot_dir / "tryline.txt").write_text("root=/dev/mmcblk0p3\n")
        with patch("subprocess.run") as run:
            assert tryboot.execute_persist(reboot=True) == 0
        run.assert_called_once_with(["reboot"], check=True)
        assert (boot_dir / "cmdline.txt").read_text() == "root=/dev/mmcblk0p3\n"


class TestRollback:
    def test_rollback_restores_previous_partition_and_removes_files(self, boot_dir, booted_from):
        booted_from(3, tryboot_active=True)
        # Simulate an already-persisted tryboot that we now want to undo
        (boot_dir / "cmdline.txt").write_text("console=tty1 root=/dev/mmcblk0p3 rootwait\n")
        (boot_dir / "tryline.txt").write_text("console=tty1 root=/dev/mmcblk0p3 rootwait\n")
        (boot_dir / "tryboot.txt").write_text("cmdline=tryline.txt\n")

        with patch("subprocess.run") as run:
            assert tryboot.rollback_tryboot(reboot=False) == 0
        run.assert_not_called()

        assert (boot_dir / "cmdline.txt").read_text() == "console=tty1 root=/dev/mmcblk0p2 rootwait\n"
        assert not (boot_dir / "tryline.txt").exists()
        assert not (boot_dir / "tryboot.txt").exists()

    def test_rollback_requires_tryboot(self, boot_dir, booted_from):
        booted_from(2, tryboot_active=False)
        with pytest.raises(UpdateError, match="not booted via tryboot"):
            tryboot.rollback_tryboot(reboot=False)


class TestWriteFileAtomic:
    def test_replaces_content(self, tmp_path):
        p = tmp_path / "cmdline.txt"
        p.write_text("old")
        tryboot.write_file_atomic(p, "new\n")
        assert p.read_text() == "new\n"
        assert list(tmp_path.iterdir()) == [p]  # no temp file left behind

    def test_creates_missing_file(self, tmp_path):
        p = tmp_path / "new"
        tryboot.write_file_atomic(p, "z", preserve_times=True)
        assert p.read_text() == "z"

    def test_preserves_times_only_when_asked(self, tmp_path):
        p = tmp_path / "f"
        p.write_text("x")
        os.utime(p, (1_000_000_000, 1_000_000_000))
        tryboot.write_file_atomic(p, "y", preserve_times=True)
        assert (p.stat().st_atime, p.stat().st_mtime) == (1_000_000_000, 1_000_000_000)
        tryboot.write_file_atomic(p, "z")
        assert p.stat().st_mtime > 1_000_000_000

    def test_failure_before_rename_leaves_original_intact(self, tmp_path):
        """A crash while writing must not truncate the file: that is what would leave the Pi unbootable."""
        p = tmp_path / "cmdline.txt"
        p.write_text("root=/dev/mmcblk0p2\n")
        with patch("os.replace", side_effect=OSError("power cut")), pytest.raises(OSError):
            tryboot.write_file_atomic(p, "root=/dev/mmcblk0p3\n")
        assert p.read_text() == "root=/dev/mmcblk0p2\n"
        assert list(tmp_path.iterdir()) == [p]

    def test_failure_while_writing_leaves_original_intact(self, tmp_path):
        p = tmp_path / "cmdline.txt"
        p.write_text("root=/dev/mmcblk0p2\n")
        with patch("os.fsync", side_effect=OSError("disk full")), pytest.raises(OSError):
            tryboot.write_file_atomic(p, "root=/dev/mmcblk0p3\n")
        assert p.read_text() == "root=/dev/mmcblk0p2\n"
        assert list(tmp_path.iterdir()) == [p]

    def test_data_is_synced_before_rename(self, tmp_path):
        order = []
        real_replace = os.replace
        with patch("os.fsync", side_effect=lambda fd: order.append("fsync")), patch(
            "os.replace", side_effect=lambda a, b: (order.append("replace"), real_replace(a, b))
        ):
            tryboot.write_file_atomic(tmp_path / "f", "x")
        assert order[:2] == ["fsync", "replace"]


class TestBootFileWritesAreAtomic:
    def test_persist_failure_keeps_cmdline(self, boot_dir):
        before = (boot_dir / "cmdline.txt").read_text()
        (boot_dir / "tryline.txt").write_text("root=/dev/mmcblk0p3\n")
        with patch("os.replace", side_effect=OSError("power cut")), pytest.raises(UpdateError, match="power cut"):
            tryboot.persist_boot_configuration()
        assert (boot_dir / "cmdline.txt").read_text() == before

    def test_rollback_failure_keeps_cmdline(self, boot_dir, booted_from):
        booted_from(3, tryboot_active=True)
        (boot_dir / "cmdline.txt").write_text("root=/dev/mmcblk0p3\n")
        with patch("os.replace", side_effect=OSError("power cut")), pytest.raises(UpdateError, match="power cut"):
            tryboot.rollback_tryboot(reboot=False)
        assert (boot_dir / "cmdline.txt").read_text() == "root=/dev/mmcblk0p3\n"
