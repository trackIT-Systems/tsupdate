"""Tests for the tryboot / persist / rollback state machine on the boot partition files."""

import os
from unittest.mock import patch

import pytest

from tsupdate import tryboot


class TestModifyCmdline:
    @pytest.mark.parametrize(
        "cmdline,target,expected",
        [
            ("root=/dev/mmcblk0p2 rootwait", 3, "root=/dev/mmcblk0p3 rootwait"),
            ("root=/dev/mmcblk0p3 rootwait", 2, "root=/dev/mmcblk0p2 rootwait"),
            ("console=tty1 root=mmcblk0p2 quiet", 3, "console=tty1 root=/dev/mmcblk0p3 quiet"),
        ],
    )
    def test_switches_root(self, cmdline, target, expected):
        assert tryboot.modify_cmdline_partition(cmdline, target) == expected

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: a root= the regex doesn't recognise (e.g. PARTUUID) is left unchanged, so tryboot "
        "silently reboots into the same partition instead of failing",
    )
    def test_unrecognised_root_is_not_silently_ignored(self, boot_dir, booted_from):
        (boot_dir / "cmdline.txt").write_text("console=tty1 root=PARTUUID=abcd1234-02 rootwait\n")
        booted_from(2)
        with patch("subprocess.run"):
            rc = tryboot.execute_tryboot(reboot=False)
        tryline = (boot_dir / "tryline.txt").read_text() if (boot_dir / "tryline.txt").exists() else None
        assert rc != 0 or tryline != (boot_dir / "cmdline.txt").read_text()


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
        with patch("subprocess.run") as run:
            assert tryboot.execute_tryboot(reboot=True) == 1
        run.assert_not_called()

    def test_allowed_when_trybooted_and_persisted(self, boot_dir, booted_from):
        booted_from(3, tryboot_active=True)
        (boot_dir / "tryline.txt").write_text((boot_dir / "cmdline.txt").read_text())
        with patch("subprocess.run"):
            assert tryboot.execute_tryboot(reboot=False) == 0

    def test_fails_without_config_txt(self, boot_dir, booted_from):
        booted_from(2)
        (boot_dir / "config.txt").unlink()
        assert tryboot.execute_tryboot(reboot=False) == 1

    def test_add_cmdline_entry_is_idempotent(self, boot_dir):
        (boot_dir / "tryboot.txt").write_text("arm_64bit=1")  # no trailing newline
        assert tryboot.add_cmdline_entry()
        assert tryboot.add_cmdline_entry()
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

        assert tryboot.persist_boot_configuration()
        assert cmdline.read_text() == "root=/dev/mmcblk0p3 rootwait\n"
        st = cmdline.stat()
        assert st.st_mtime == 1_000_000_000
        assert st.st_atime == 1_000_000_000
        assert tryboot.is_tryboot_persisted()

    def test_persist_without_tryline_fails_and_keeps_cmdline(self, boot_dir):
        before = (boot_dir / "cmdline.txt").read_text()
        assert not tryboot.persist_boot_configuration()
        assert (boot_dir / "cmdline.txt").read_text() == before

    def test_execute_persist_requires_tryboot(self, boot_dir, booted_from):
        booted_from(2, tryboot_active=False)
        (boot_dir / "tryline.txt").write_text("root=/dev/mmcblk0p3\n")
        assert tryboot.execute_persist() == 1
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
        assert tryboot.rollback_tryboot(reboot=False) == 1


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
        with patch("os.replace", side_effect=OSError("power cut")):
            assert not tryboot.persist_boot_configuration()
        assert (boot_dir / "cmdline.txt").read_text() == before

    def test_rollback_failure_keeps_cmdline(self, boot_dir, booted_from):
        booted_from(3, tryboot_active=True)
        (boot_dir / "cmdline.txt").write_text("root=/dev/mmcblk0p3\n")
        with patch("os.replace", side_effect=OSError("power cut")):
            assert tryboot.rollback_tryboot(reboot=False) == 1
        assert (boot_dir / "cmdline.txt").read_text() == "root=/dev/mmcblk0p3\n"
