"""Tests for execute_restore(): extraction, reuse of cached files and cleanup, with mount/losetup/rsync faked."""

import gzip
from contextlib import contextmanager

import pytest

from tsupdate import restore

IMG = b"\x00REAL-IMAGE" * 500


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A restore run with everything privileged faked. `env.ensure` controls what ensure_file returns."""

    class Env:
        artifacts = tmp_path / "artifacts"
        partition = tmp_path / "loop9p2"
        seen_images: list[bytes] = []  # content of the image at the time losetup was called
        sync_ok = True
        ensure_result: tuple = (None, False)
        ensure_calls: list[str] = []

    e = Env()
    e.artifacts.mkdir()
    e.partition.write_bytes(b"")
    e.seen_images = []
    e.ensure_calls = []

    monkeypatch.setattr(restore, "ARTIFACTS_DIR", e.artifacts)
    monkeypatch.setattr(restore, "can_run_syncroot", lambda: True)
    monkeypatch.setattr(restore, "get_inactive_partition_device", lambda: "/dev/mmcblk0p3")
    monkeypatch.setattr("tsupdate.status.get_inactive_partition", lambda: ("/dev/mmcblk0p3", 3, "clonefs"))

    def ensure_file(source, artifacts_path):
        e.ensure_calls.append(source)
        return e.ensure_result

    def setup_loopback(image_path):
        e.seen_images.append(image_path.read_bytes())
        return "/dev/loop9"

    @contextmanager
    def mount_context(device, mount_point):
        yield mount_point

    monkeypatch.setattr(restore, "ensure_file", ensure_file)
    monkeypatch.setattr(restore, "setup_loopback", setup_loopback)
    monkeypatch.setattr(restore, "remove_loopback", lambda dev: True)
    monkeypatch.setattr(restore, "get_partition_device", lambda dev, part: str(e.partition))
    monkeypatch.setattr(restore, "mount_context", mount_context)
    monkeypatch.setattr(restore, "sync_root_partitions", lambda **kw: e.sync_ok)
    return e


def make_download(env, name="tsOS-base.img.gz", content=IMG) -> "Path":
    p = env.artifacts / name
    p.write_bytes(gzip.compress(content))
    return p


class TestExtraction:
    def test_extracts_and_restores(self, env):
        download = make_download(env)
        env.ensure_result = (download, False)
        assert restore.execute_restore("https://x/tsOS-base.img.gz") == 0
        assert env.seen_images == [IMG]

    def test_never_reuses_an_existing_extracted_image(self, env):
        """/data is world-writable: a pre-placed .img must be replaced, not written to the partition."""
        download = make_download(env)
        (env.artifacts / "tsOS-base.img").write_bytes(b"malicious image placed by another local user")
        env.ensure_result = (download, False)
        assert restore.execute_restore("https://x/tsOS-base.img.gz") == 0
        assert env.seen_images == [IMG]

    def test_leftover_tmp_file_does_not_break_extraction(self, env):
        download = make_download(env)
        (env.artifacts / "tsOS-base.img.tmp").write_bytes(b"half-written")
        env.ensure_result = (download, False)
        assert restore.execute_restore("https://x/tsOS-base.img.gz") == 0
        assert env.seen_images == [IMG]

    def test_failed_extraction_does_not_fall_back_to_old_image(self, env):
        download = env.artifacts / "tsOS-base.img.gz"
        download.write_bytes(b"corrupt, not gzip")
        (env.artifacts / "tsOS-base.img").write_bytes(b"stale image from an earlier run")
        env.ensure_result = (download, False)
        assert restore.execute_restore("https://x/tsOS-base.img.gz") == 1
        assert env.seen_images == []  # nothing was written to the partition

    def test_download_is_asked_for_once_and_not_repeated(self, env):
        """Re-extracting must not turn into re-downloading (costly on cellular): the download is
        obtained once via ensure_file, which itself reuses a verified cached file."""
        env.ensure_result = (make_download(env), False)
        restore.execute_restore("https://x/tsOS-base.img.gz")
        assert env.ensure_calls == ["https://x/tsOS-base.img.gz"]
        assert (env.artifacts / "tsOS-base.img.gz").exists()
