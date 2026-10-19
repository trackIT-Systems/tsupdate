"""Shared fixtures.

tsupdate talks to real hardware paths (/boot/firmware, /proc/cmdline, block
devices) and runs privileged commands (mount, rsync, reboot). These fixtures
redirect the file paths into tmp_path and fake the partition/boot detection,
so the logic on top can be exercised on any machine, including CI.
"""

from pathlib import Path

import pytest

from tsupdate import tryboot


@pytest.fixture
def boot_dir(tmp_path, monkeypatch) -> Path:
    """A fake /boot/firmware with a typical cmdline.txt and config.txt, booted from p2."""
    boot = tmp_path / "boot"
    boot.mkdir()
    monkeypatch.setattr(tryboot, "BOOT_DIR", boot)
    monkeypatch.setattr(tryboot, "CMDLINE_FILE", boot / "cmdline.txt")
    monkeypatch.setattr(tryboot, "TRYLINE_FILE", boot / "tryline.txt")
    monkeypatch.setattr(tryboot, "CONFIG_FILE", boot / "config.txt")
    monkeypatch.setattr(tryboot, "TRYBOOT_CONFIG_FILE", boot / "tryboot.txt")

    (boot / "cmdline.txt").write_text("console=tty1 root=/dev/mmcblk0p2 rootfstype=ext4 fsck.repair=yes rootwait\n")
    (boot / "config.txt").write_text("[all]\narm_64bit=1\n")
    return boot


@pytest.fixture
def booted_from(monkeypatch):
    """Fake which partition is booted and whether it was via tryboot.

    Usage: booted_from(2) / booted_from(3, tryboot_active=True) / booted_from(2, disk="/dev/nvme0n1p")
    """

    def _set(partition: int, tryboot_active: bool = False, disk: str = "/dev/mmcblk0p") -> None:
        label = {2: "rootfs", 3: "clonefs"}.get(partition, "unknown")
        booted = (f"{disk}{partition}", partition, label)
        monkeypatch.setattr("tsupdate.status.get_booted_partition", lambda: booted)
        monkeypatch.setattr("tsupdate.tryboot.get_booted_partition", lambda: booted)
        monkeypatch.setattr("tsupdate.status.get_partition_label", lambda device: None)
        monkeypatch.setattr("tsupdate.status.is_tryboot_active", lambda: tryboot_active)
        monkeypatch.setattr("tsupdate.tryboot.is_tryboot_active", lambda: tryboot_active)
        monkeypatch.setattr("tsupdate.daemon.is_tryboot_active", lambda: tryboot_active)

    return _set
