"""Tests for download_file(): resume, redirect pinning and verification, with requests faked."""

import hashlib
from unittest.mock import patch

import pytest
import requests

from tsupdate import utils
from tsupdate.utils import resolve_rsync_timeout_seconds

ASSET_URL = "https://objects.githubusercontent.com/asset"


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, url: str = ASSET_URL, content_length: bool = True):
        self.body = body
        self.status_code = status
        self.url = url
        self.headers = {"Content-Length": str(len(body))} if content_length else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i : i + chunk_size]

    def close(self):
        pass


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


CONTENT = b"0123456789" * 1000


class TestDownloadFile:
    def test_fresh_download_verified(self, tmp_path):
        out = tmp_path / "f.tar"
        with patch.object(utils.requests, "get", return_value=FakeResponse(CONTENT)) as get:
            assert utils.download_file(ASSET_URL, out, len(CONTENT), sha(CONTENT))
        assert out.read_bytes() == CONTENT
        assert not (tmp_path / "f.tar.tmp").exists()
        assert "Range" not in get.call_args.kwargs["headers"]

    def test_resume_with_206(self, tmp_path):
        out = tmp_path / "f.tar"
        (tmp_path / "f.tar.tmp").write_bytes(CONTENT[:300])
        with patch.object(utils.requests, "get", return_value=FakeResponse(CONTENT[300:], status=206)) as get:
            assert utils.download_file(ASSET_URL, out, len(CONTENT), sha(CONTENT))
        assert get.call_args.kwargs["headers"]["Range"] == "bytes=300-"
        assert out.read_bytes() == CONTENT

    def test_server_ignoring_range_restarts_from_scratch(self, tmp_path):
        out = tmp_path / "f.tar"
        (tmp_path / "f.tar.tmp").write_bytes(b"garbage")
        with patch.object(utils.requests, "get", return_value=FakeResponse(CONTENT, status=200)) as get:
            assert utils.download_file(ASSET_URL, out, len(CONTENT), sha(CONTENT))
        assert get.call_count == 2
        assert out.read_bytes() == CONTENT

    def test_tampered_partial_is_discarded_after_resume(self, tmp_path):
        out = tmp_path / "f.tar"
        tmp = tmp_path / "f.tar.tmp"
        tmp.write_bytes(b"X" * 300)  # same length as the real prefix, wrong bytes
        with patch.object(utils.requests, "get", return_value=FakeResponse(CONTENT[300:], status=206)):
            assert not utils.download_file(ASSET_URL, out, len(CONTENT), sha(CONTENT))
        assert not out.exists()
        assert not tmp.exists()

    def test_redirect_to_untrusted_host_is_refused(self, tmp_path):
        out = tmp_path / "f.tar"
        resp = FakeResponse(CONTENT, url="https://evil.example/payload")
        with patch.object(utils.requests, "get", return_value=resp):
            assert not utils.download_file("https://github.com/trackIT-Systems/x/releases/download/v/f", out)
        assert not out.exists()

    def test_incomplete_download_keeps_tmp_for_resume(self, tmp_path):
        out = tmp_path / "f.tar"
        resp = FakeResponse(CONTENT[:100])
        resp.headers["Content-Length"] = str(len(CONTENT))
        with patch.object(utils.requests, "get", return_value=resp):
            assert not utils.download_file(ASSET_URL, out)
        assert (tmp_path / "f.tar.tmp").read_bytes() == CONTENT[:100]

    def test_network_error(self, tmp_path):
        with patch.object(utils.requests, "get", side_effect=requests.exceptions.ConnectionError("down")):
            assert not utils.download_file(ASSET_URL, tmp_path / "f")

    def test_http_error(self, tmp_path):
        with patch.object(utils.requests, "get", return_value=FakeResponse(b"", status=404)):
            assert not utils.download_file(ASSET_URL, tmp_path / "f")

    def test_token_sent_only_for_github_urls(self, tmp_path, monkeypatch):
        monkeypatch.setattr(utils, "get_github_token", lambda: "secret")
        with patch.object(utils.requests, "get", return_value=FakeResponse(CONTENT)) as get:
            utils.download_file("https://api.github.com/repos/trackIT-Systems/x/releases/assets/1", tmp_path / "a")
            assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"
            utils.download_file(ASSET_URL, tmp_path / "b")
            assert "Authorization" not in get.call_args.kwargs["headers"]


class TestEnsureFileLocal:
    def test_local_file_is_used_as_is(self, tmp_path):
        p = tmp_path / "update.tar"
        p.write_bytes(b"x")
        assert utils.ensure_file(str(p), artifacts_path=tmp_path / "artifacts") == (p, True)


@pytest.mark.parametrize(
    "value,expected",
    [(None, 3600.0), (0, None), (-5, 3600.0), (120, 120.0)],
)
def test_resolve_rsync_timeout(value, expected):
    assert resolve_rsync_timeout_seconds(value) == expected


class TestPruneArtifacts:
    def test_removes_files_and_leaves_directories(self, tmp_path):
        (tmp_path / "update.tar").write_bytes(b"x")
        (tmp_path / "update.tar.tmp").write_bytes(b"x")
        (tmp_path / "image.img").write_bytes(b"x")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "keep").write_bytes(b"x")
        assert utils.prune_artifacts(tmp_path) == 3
        assert sorted(p.name for p in tmp_path.iterdir()) == ["subdir"]
        assert (tmp_path / "subdir" / "keep").exists()

    def test_symlinks_are_unlinked_not_followed(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "precious").write_bytes(b"x")
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "link-to-dir").symlink_to(outside, target_is_directory=True)
        (artifacts / "link-to-file").symlink_to(outside / "precious")
        assert utils.prune_artifacts(artifacts) == 2
        assert list(artifacts.iterdir()) == []
        assert (outside / "precious").exists()  # removing a link never removes what it points to

    def test_missing_directory(self, tmp_path):
        assert utils.prune_artifacts(tmp_path / "nope") == 0
