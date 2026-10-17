"""Tests for syncroot (mount + rsync) and restore (image extraction), with privileged commands faked."""

import gzip
import lzma
import subprocess
import zipfile
from unittest.mock import patch

import pytest

from tsupdate import restore, syncroot


class TestSyncRootPartitions:
    def test_rsync_command(self, tmp_path):
        with patch("subprocess.run") as run:
            assert syncroot.sync_root_partitions(tmp_path / "src", tmp_path / "dst", rsync_timeout_sec=0)
        cmd = run.call_args.args[0]
        assert cmd[:6] == ["rsync", "-a", "-h", "--stats", "--delete", "--checksum"]
        assert cmd[-2:] == [f"{tmp_path / 'src'}/", f"{tmp_path / 'dst'}/"]
        assert run.call_args.kwargs["timeout"] is None

    @pytest.mark.parametrize(
        "exc",
        [subprocess.TimeoutExpired("rsync", 1), subprocess.CalledProcessError(23, "rsync"), FileNotFoundError()],
    )
    def test_failures_return_false(self, tmp_path, exc):
        with patch("subprocess.run", side_effect=exc):
            assert not syncroot.sync_root_partitions(tmp_path, tmp_path)


class TestMountContext:
    def test_unmounts_even_on_error(self, tmp_path):
        with patch.object(syncroot, "mount_partition", return_value=True), patch.object(
            syncroot, "unmount_partition", return_value=True
        ) as umount:
            with pytest.raises(ValueError):
                with syncroot.mount_context("/dev/x", tmp_path):
                    raise ValueError("boom")
        umount.assert_called_once_with(tmp_path)

    def test_mount_failure_raises(self, tmp_path):
        with patch.object(syncroot, "mount_partition", return_value=False), pytest.raises(RuntimeError):
            with syncroot.mount_context("/dev/x", tmp_path):
                pass


class TestExecuteSyncroot:
    def test_refuses_in_unpersisted_tryboot(self, boot_dir, booted_from):
        booted_from(3, tryboot_active=True)
        (boot_dir / "tryline.txt").write_text("root=/dev/mmcblk0p3\n")
        with patch.object(syncroot, "mount_partition") as mount:
            assert syncroot.execute_syncroot() == 1
        mount.assert_not_called()

    def test_syncs_to_inactive_partition(self, boot_dir, booted_from, tmp_path, monkeypatch):
        booted_from(2)
        monkeypatch.setattr(syncroot, "ROOT_UP", tmp_path / "root-up")
        with patch.object(syncroot, "mount_partition", return_value=True) as mount, patch.object(
            syncroot, "unmount_partition", return_value=True
        ), patch.object(syncroot, "sync_root_partitions", return_value=True):
            assert syncroot.execute_syncroot() == 0
        mount.assert_called_once_with("/dev/mmcblk0p3", tmp_path / "root-up")


IMG = b"\x00IMAGE" * 1000


class TestExtractImage:
    @pytest.mark.parametrize(
        "name,writer",
        [
            ("os.img.gz", lambda p: p.write_bytes(gzip.compress(IMG))),
            ("os.img.xz", lambda p: p.write_bytes(lzma.compress(IMG))),
        ],
    )
    def test_stream_compressed(self, tmp_path, name, writer):
        src = tmp_path / name
        writer(src)
        out = tmp_path / "os.img"
        assert restore.extract_image(src, out)
        assert out.read_bytes() == IMG
        assert not (tmp_path / "os.img.tmp").exists()

    def test_zip_uses_largest_img(self, tmp_path):
        src = tmp_path / "os.zip"
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("small.img", b"x")
            z.writestr("big.img", IMG)
            z.writestr("README.txt", b"y" * 100000)
        assert restore.find_largest_img_in_zip(src) == "big.img"
        out = tmp_path / "os.img"
        assert restore.extract_image(src, out)
        assert out.read_bytes() == IMG

    def test_zip_without_img(self, tmp_path):
        src = tmp_path / "os.zip"
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("README.txt", b"y")
        assert not restore.extract_image(src, tmp_path / "os.img")

    def test_corrupt_gzip_cleans_up(self, tmp_path):
        src = tmp_path / "os.img.gz"
        src.write_bytes(b"not gzip")
        out = tmp_path / "os.img"
        assert not restore.extract_image(src, out)
        assert not out.exists()
        assert not (tmp_path / "os.img.tmp").exists()


class TestLoopback:
    def test_setup_loopback(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="/dev/loop3\n")) as run:
            assert restore.setup_loopback("/data/x.img") == "/dev/loop3"
        assert run.call_args_list[0].args[0][0] == "losetup"

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: losetup is called without --partscan; partition devices only appear if partprobe "
        "happens to be installed",
    )
    def test_loopback_scans_partitions_itself(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="/dev/loop3\n")) as run:
            restore.setup_loopback("/data/x.img")
        losetup_args = run.call_args_list[0].args[0]
        assert "-P" in losetup_args or "--partscan" in losetup_args
