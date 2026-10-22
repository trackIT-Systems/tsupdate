"""Tests for the security-relevant parts of tsupdate.utils.ensure_file:

- is_allowed_source_url(): the actual enforcement point for the org pin,
  checked against every remote source regardless of whether it happens to
  match the GitHub-release-URL shape.
- _verify_file(): digest/size verification used to stop a pre-placed or
  tampered file in the world-writable /data/tsupdate cache from being
  silently trusted.
- ensure_file() end-to-end: a tampered cached file must be rejected and
  replaced, not reused.
"""

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from tsupdate import utils
from tsupdate.errors import ConfigurationError
from tsupdate.github import ALLOWED_OWNER
from tsupdate.utils import _sha256_file, _verify_file, is_allowed_source_url


class TestIsAllowedSourceUrl:
    @pytest.mark.parametrize(
        "url",
        [
            f"https://github.com/{ALLOWED_OWNER}/tsOS-base/releases/download/v1/a.tar",
            f"https://api.github.com/repos/{ALLOWED_OWNER}/tsOS-base/releases/assets/123",
            f"https://github.com/{ALLOWED_OWNER.upper()}/tsOS-base/releases/download/v1/a.tar",
        ],
    )
    def test_allows_trackit_systems_urls(self, url):
        assert is_allowed_source_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example/payload.tar",
            "https://github.com/some-other-org/tsOS-base/releases/download/v1/a.tar",
            "https://api.github.com/repos/some-other-org/tsOS-base/releases/assets/123",
            f"http://github.com/{ALLOWED_OWNER}/tsOS-base/releases/download/v1/a.tar",
            f"https://github.com.evil.example/{ALLOWED_OWNER}/tsOS-base",
            "not-a-url-at-all",
            "",
        ],
    )
    def test_rejects_everything_else(self, url):
        assert is_allowed_source_url(url) is False


class TestVerifyFile:
    def test_digest_match(self, tmp_path):
        p = tmp_path / "f"
        p.write_bytes(b"hello world")
        digest = "sha256:" + hashlib.sha256(b"hello world").hexdigest()
        ok, _ = _verify_file(p, None, digest)
        assert ok is True

    def test_digest_mismatch(self, tmp_path):
        p = tmp_path / "f"
        p.write_bytes(b"hello world")
        ok, reason = _verify_file(p, None, "sha256:" + "0" * 64)
        assert ok is False
        assert "digest mismatch" in reason

    def test_digest_case_insensitive(self, tmp_path):
        p = tmp_path / "f"
        p.write_bytes(b"hello world")
        digest = "sha256:" + hashlib.sha256(b"hello world").hexdigest().upper()
        ok, _ = _verify_file(p, None, digest)
        assert ok is True

    def test_falls_back_to_size_when_no_digest(self, tmp_path):
        p = tmp_path / "f"
        p.write_bytes(b"hello world")
        ok, reason = _verify_file(p, 11, None)
        assert ok is True
        assert "size matches" in reason

    def test_size_mismatch_rejected(self, tmp_path):
        p = tmp_path / "f"
        p.write_bytes(b"hello world")
        ok, _ = _verify_file(p, 999, None)
        assert ok is False

    def test_no_metadata_at_all_is_rejected_not_trusted(self, tmp_path):
        # This is the important default: "cannot verify" must mean "reject",
        # since this exists specifically to stop a world-writable cache
        # directory from being blindly trusted.
        p = tmp_path / "f"
        p.write_bytes(b"hello world")
        ok, reason = _verify_file(p, None, None)
        assert ok is False
        assert "no digest or size available" in reason

    def test_unsupported_digest_algorithm_falls_back_to_size(self, tmp_path):
        p = tmp_path / "f"
        p.write_bytes(b"hello world")
        ok, reason = _verify_file(p, 11, "md5:deadbeef")
        assert ok is True
        assert "size matches" in reason


class TestEnsureFileRejectsDisallowedSources:
    def test_non_github_source_rejected_outright(self, tmp_path):
        with pytest.raises(ConfigurationError, match="Refusing update source"):
            utils.ensure_file("https://evil.example/malicious.tar", artifacts_path=tmp_path)
        # No artifact should have been written for a rejected source.
        assert list(tmp_path.iterdir()) == []

    def test_wrong_org_source_rejected_outright(self, tmp_path):
        url = "https://github.com/some-other-org/repo/releases/download/v1/a.tar"
        with pytest.raises(ConfigurationError, match="Refusing update source"):
            utils.ensure_file(url, artifacts_path=tmp_path)

    def test_missing_local_file_is_named_as_such(self, tmp_path):
        with pytest.raises(ConfigurationError, match="File not found: /nonexistent/update.tar"):
            utils.ensure_file("/nonexistent/update.tar", artifacts_path=tmp_path)


class TestEnsureFileCacheBypassFix:
    """The core regression test for the cache-bypass finding: a file
    pre-placed (or left over, tampered) in the world-writable artifacts
    directory must not be trusted just because a same-named file exists -
    it has to verify against the GitHub API's asset metadata.
    """

    def test_tampered_cached_file_is_replaced_not_trusted(self, tmp_path):
        malicious_path = tmp_path / "update.tar"
        malicious_path.write_bytes(b"malicious payload placed by another local user")

        good_content = b"the real, verified update content"
        good_digest = "sha256:" + hashlib.sha256(good_content).hexdigest()

        def fake_download(url, output_path, expected_size=None, expected_digest=None):
            output_path.write_bytes(good_content)

        with patch.object(
            utils,
            "resolve_github_release_asset",
            return_value={
                "url": "https://objects.githubusercontent.com/real-asset",
                "size": len(good_content),
                "digest": good_digest,
            },
        ), patch.object(utils, "download_file", side_effect=fake_download) as mock_dl:
            result_path, is_local = utils.ensure_file(
                f"https://github.com/{ALLOWED_OWNER}/tsOS-base/releases/download/v1/update.tar",
                artifacts_path=tmp_path,
            )

        assert is_local is False
        assert result_path == malicious_path
        assert result_path.read_bytes() == good_content, "tampered cached file was trusted instead of replaced"
        mock_dl.assert_called_once()

    def test_matching_cached_file_is_reused_without_downloading(self, tmp_path):
        good_content = b"already-correct cached content"
        good_digest = "sha256:" + hashlib.sha256(good_content).hexdigest()
        cached_path = tmp_path / "update.tar"
        cached_path.write_bytes(good_content)

        with patch.object(
            utils,
            "resolve_github_release_asset",
            return_value={
                "url": "https://objects.githubusercontent.com/real-asset",
                "size": len(good_content),
                "digest": good_digest,
            },
        ), patch.object(utils, "download_file") as mock_dl:
            result_path, is_local = utils.ensure_file(
                f"https://github.com/{ALLOWED_OWNER}/tsOS-base/releases/download/v1/update.tar",
                artifacts_path=tmp_path,
            )

        assert result_path == cached_path
        assert is_local is False
        mock_dl.assert_not_called()

    def test_asset_filename_cannot_escape_artifacts_dir(self, tmp_path):
        """ensure_file() derives the asset filename from the URL itself
        (os.path.basename'd) *before* ever calling resolve_github_release_asset,
        so even with that call mocked to unconditionally "succeed" (unlike a
        real GitHub API, which would 404 on a mismatched asset name), a
        traversal-shaped source must still resolve to a path inside
        artifacts_path, never outside it. This is a second, independent layer
        behind parse_github_release_url's regex already being structurally
        unable to capture a "/"-containing asset name (see test_org_pin.py).
        """
        outside_dir = tmp_path.parent / "outside-marker-dir"
        outside_dir.mkdir(exist_ok=True)
        try:
            with patch.object(
                utils,
                "resolve_github_release_asset",
                return_value={"url": "https://objects.githubusercontent.com/x", "size": None, "digest": None},
            ) as mock_resolve, patch.object(utils, "download_file") as mock_dl:
                utils.ensure_file(
                    f"https://github.com/{ALLOWED_OWNER}/tsOS-base/releases/download/v1/../../outside-marker-dir/pwned",
                    artifacts_path=tmp_path,
                )
            # Whatever path download_file was asked to write to, it must be
            # inside artifacts_path (tmp_path), never the marker directory.
            assert mock_dl.called
            written_path = mock_dl.call_args.args[1]
            assert written_path.parent == tmp_path
            assert list(outside_dir.iterdir()) == []
        finally:
            outside_dir.rmdir()
