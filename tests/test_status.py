"""Tests for os-release parsing, partition detection and status formatting."""

import json
from unittest.mock import patch

import pytest

from tsupdate import status


class TestReadOsRelease:
    def test_parses_quoted_unquoted_and_skips_comments(self, tmp_path):
        p = tmp_path / "os-release"
        p.write_text(
            "# comment\n"
            "\n"
            'PRETTY_NAME="tsOS base 2025.12.4"\n'
            "ID=tsos\n"
            "VERSION_ID='2025.12.4'\n"
            'SUPPORT_URL="https://github.com/trackIT-Systems/tsOS-base"\n'
            "WEIRD=a=b\n"
        )
        rel = status.read_os_release(p)
        assert rel.pretty_name == "tsOS base 2025.12.4"
        assert rel.id == "tsos"
        assert rel.version_id == "2025.12.4"
        assert rel.get("SUPPORT_URL") == "https://github.com/trackIT-Systems/tsOS-base"
        assert rel.get("WEIRD") == "a=b"
        assert rel.get("MISSING", "dflt") == "dflt"

    def test_missing_file_returns_none(self, tmp_path):
        assert status.read_os_release(tmp_path / "nope") is None

    def test_undecodable_file_returns_none(self, tmp_path):
        p = tmp_path / "os-release"
        p.write_bytes(b"NAME=\xff\xfe\n")
        assert status.read_os_release(p) is None


class TestPartitionDetection:
    @pytest.mark.parametrize(
        "device,expected",
        [
            ("/dev/mmcblk0p2", 2),
            ("/dev/mmcblk0p3", 3),
            ("/dev/sda1", 1),
            ("", None),
            ("/dev/mmcblk0", 0),  # documents current behaviour: trailing digit of the disk itself
        ],
    )
    def test_get_partition_number(self, device, expected):
        assert status.get_partition_number(device) == expected

    @pytest.mark.parametrize(
        "root,expected",
        [("/dev/mmcblk0p2", "/dev/mmcblk0p2"), ("mmcblk0p3", "/dev/mmcblk0p3")],
    )
    def test_root_from_cmdline_is_normalised(self, root, expected):
        with patch.object(status, "parse_cmdline", return_value={"root": root}):
            assert status.get_root_partition_from_cmdline() == expected

    def test_no_root_in_cmdline(self):
        with patch.object(status, "parse_cmdline", return_value={"quiet": True}):
            assert status.get_root_partition_from_cmdline() is None
            assert status.get_booted_partition() is None
            assert status.get_inactive_partition() is None

    @pytest.mark.parametrize(
        "booted,inactive",
        [
            ("/dev/mmcblk0p2", ("/dev/mmcblk0p3", 3, "clonefs")),
            ("/dev/mmcblk0p3", ("/dev/mmcblk0p2", 2, "rootfs")),
            ("/dev/mmcblk0p1", None),
        ],
    )
    def test_inactive_partition_is_the_other_root(self, booted, inactive):
        with patch.object(status, "parse_cmdline", return_value={"root": booted}), patch.object(
            status, "get_partition_label", return_value=None
        ):
            assert status.get_inactive_partition() == inactive

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: inactive partition is hardcoded to /dev/mmcblk0p{2,3}; booting from NVMe/USB "
        "would make syncroot/apply/restore write to the SD card instead",
    )
    def test_inactive_partition_stays_on_booted_disk(self):
        with patch.object(status, "parse_cmdline", return_value={"root": "/dev/nvme0n1p2"}), patch.object(
            status, "get_partition_label", return_value=None
        ):
            inactive = status.get_inactive_partition()
        assert inactive is None or inactive[0].startswith("/dev/nvme0n1")


class TestStatusFormatting:
    STATUS = {
        "os_pretty_name": "tsOS base 2025.12.4",
        "os_version_commit": "abc123",
        "support_url": "https://github.com/trackIT-Systems/tsOS-base",
        "active_partition": "/dev/mmcblk0p2",
        "active_partition_label": "rootfs",
        "inactive_partition": "/dev/mmcblk0p3",
        "inactive_partition_label": "clonefs",
        "booted_via_tryboot": "True",
    }

    def test_text(self):
        text = status.format_status_text(self.STATUS)
        assert "OS: tsOS base 2025.12.4" in text
        assert "Active: /dev/mmcblk0p2 (rootfs)" in text
        assert "Inactive: /dev/mmcblk0p3 (clonefs)" in text
        assert "Method: tryboot" in text

    def test_text_with_unknown_partitions(self):
        text = status.format_status_text({"booted_via_tryboot": "False"})
        assert "Active: (unknown)" in text
        assert "Method: regular" in text

    def test_json_roundtrip(self):
        assert json.loads(status.format_status_json(self.STATUS)) == self.STATUS
