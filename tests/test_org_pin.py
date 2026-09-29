"""Tests for the trackIT-Systems org pin: parse_github_repo_url and
parse_github_release_url must accept only https://github.com URLs owned by
ALLOWED_OWNER, and must not be trickable by an owner-looking string embedded
elsewhere in an otherwise-untrusted URL.
"""

import pytest

from tsupdate.github import ALLOWED_OWNER, parse_github_release_url, parse_github_repo_url


class TestParseGithubRepoUrl:
    def test_accepts_bare_repo_url(self):
        assert parse_github_repo_url(f"https://github.com/{ALLOWED_OWNER}/tsOS-base") == (
            ALLOWED_OWNER,
            "tsOS-base",
        )

    def test_case_insensitive_owner_match(self):
        assert parse_github_repo_url("https://github.com/TRACKIT-systems/tsOS-base") is not None
        assert parse_github_repo_url("https://github.com/TRACKIT-SYSTEMS/tsOS-base") is not None

    @pytest.mark.parametrize(
        "suffix",
        ["/issues", "/releases", "/", "/issues?tab=open", "#readme"],
    )
    def test_accepts_documented_trailing_forms(self, suffix):
        assert parse_github_repo_url(f"https://github.com/{ALLOWED_OWNER}/tsOS-base{suffix}") is not None

    def test_rejects_other_owner(self):
        assert parse_github_repo_url("https://github.com/some-other-org/tsOS-base") is None

    def test_rejects_plain_http(self):
        assert parse_github_repo_url(f"http://github.com/{ALLOWED_OWNER}/tsOS-base") is None

    def test_rejects_owner_embedded_in_query_string(self):
        # A URL that isn't a github.com URL at all, but happens to contain one
        # in a query parameter - must not match.
        url = f"https://evil.example/?u=https://github.com/{ALLOWED_OWNER}/tsOS-base"
        assert parse_github_repo_url(url) is None

    def test_rejects_lookalike_host(self):
        assert parse_github_repo_url(f"https://github.com.evil.example/{ALLOWED_OWNER}/tsOS-base") is None

    def test_rejects_subdomain_prefix_trick(self):
        assert parse_github_repo_url(f"https://evilgithub.com/{ALLOWED_OWNER}/tsOS-base") is None

    def test_empty_and_none(self):
        assert parse_github_repo_url("") is None
        assert parse_github_repo_url(None) is None


class TestParseGithubReleaseUrl:
    def test_accepts_download_url(self):
        result = parse_github_release_url(
            f"https://github.com/{ALLOWED_OWNER}/tsOS-base/releases/download/v1.0/tsOS-base-arm64.img.gz"
        )
        assert result == (ALLOWED_OWNER, "tsOS-base", "v1.0", "tsOS-base-arm64.img.gz")

    def test_accepts_tag_url(self):
        result = parse_github_release_url(f"https://github.com/{ALLOWED_OWNER}/tsOS-base/releases/tag/v1.0")
        assert result == (ALLOWED_OWNER, "tsOS-base", "v1.0", None)

    def test_case_insensitive_owner_match(self):
        assert (
            parse_github_release_url("https://github.com/TRACKIT-systems/tsOS-base/releases/tag/v1.0")
            is not None
        )

    def test_rejects_other_owner_download_url(self):
        url = "https://github.com/some-other-org/tsOS-base/releases/download/v1.0/asset.tar"
        assert parse_github_release_url(url) is None

    def test_rejects_other_owner_tag_url(self):
        assert parse_github_release_url("https://github.com/some-other-org/tsOS-base/releases/tag/v1.0") is None

    def test_rejects_plain_http(self):
        url = f"http://github.com/{ALLOWED_OWNER}/tsOS-base/releases/download/v1.0/asset.tar"
        assert parse_github_release_url(url) is None

    def test_asset_name_cannot_contain_path_traversal(self):
        # The asset-name capture group excludes "/", so a multi-segment path
        # (e.g. an attempted "../../etc/passwd") cannot match at all - it's
        # rejected as unparseable rather than parsed with a dangerous value.
        url = f"https://github.com/{ALLOWED_OWNER}/tsOS-base/releases/download/v1.0/../../etc/passwd"
        assert parse_github_release_url(url) is None

    def test_rejects_owner_embedded_elsewhere(self):
        url = f"https://evil.example/?u=https://github.com/{ALLOWED_OWNER}/tsOS-base/releases/tag/v1.0"
        assert parse_github_release_url(url) is None
