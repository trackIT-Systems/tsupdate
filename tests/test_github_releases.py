"""Tests for picking images/batch updates out of GitHub release listings (PyGithub objects faked)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
import urllib3.connection
import urllib3.exceptions
from github import GithubException, RateLimitExceededException

from tsupdate import github
from tsupdate.errors import ConfigurationError, TransientError, UpdateError


def release(tag, *names, prerelease=False, sizes=None):
    sizes = sizes or {}
    assets = [
        {
            "name": n,
            "size": sizes.get(n, 100),
            "browser_download_url": f"https://github.com/trackIT-Systems/tsOS-base/releases/download/{tag}/{n}",
            "id": i,
            "digest": "sha256:" + "0" * 64,
        }
        for i, n in enumerate(names)
    ]
    return SimpleNamespace(tag_name=tag, prerelease=prerelease, raw_data={"assets": assets})


RELEASES = [
    release(
        "2025.12.4",
        "tsOS-base-arm64-2025.12.4.img.xz",
        "tsOS-base-arm64-update-2025.12.3-to-2025.12.4.tar",
        "tsOS-base-arm64-2025.12.4.zip",
        "SHA256SUMS",
        sizes={"tsOS-base-arm64-2025.12.4.img.xz": 500, "tsOS-base-arm64-2025.12.4.zip": 600},
    ),
    release("2025.12.3", "tsOS-base-arm64-update-2025.12.2-to-2025.12.3.tar.gz"),
]


class TestFindApplicableBatchUpdate:
    def test_finds_update_for_current_version(self):
        url = github.find_applicable_batch_update(RELEASES, "2025.12.3")
        assert url.endswith("/2025.12.4/tsOS-base-arm64-update-2025.12.3-to-2025.12.4.tar")

    def test_finds_tar_gz_in_older_release(self):
        url = github.find_applicable_batch_update(RELEASES, "2025.12.2")
        assert url.endswith("tsOS-base-arm64-update-2025.12.2-to-2025.12.3.tar.gz")

    def test_respects_max_releases(self):
        assert github.find_applicable_batch_update(RELEASES, "2025.12.2", max_releases=1) is None

    def test_no_update_when_up_to_date(self):
        assert github.find_applicable_batch_update(RELEASES, "2025.12.4") is None

    def test_version_dots_are_not_regex_wildcards(self):
        # "2025x12x3" must not match "2025.12.3"
        assert github.find_applicable_batch_update(RELEASES, "2025x12x3") is None

    def test_version_is_not_a_prefix_match(self):
        # Current version 2025.12.3 must not pick up an update for 2025.12.31
        rels = [release("x", "tsOS-base-arm64-update-2025.12.31-to-2026.1.1.tar")]
        assert github.find_applicable_batch_update(rels, "2025.12.3") is None

    def test_empty_inputs(self):
        assert github.find_applicable_batch_update([], "2025.12.3") is None
        assert github.find_applicable_batch_update(RELEASES, "") is None


class TestFindLatestImage:
    def test_picks_largest_image_in_latest_release(self):
        assert github.find_latest_image(RELEASES).endswith("tsOS-base-arm64-2025.12.4.zip")

    def test_none_when_no_images(self):
        assert github.find_latest_image([release("x", "SHA256SUMS")]) is None
        assert github.find_latest_image([]) is None


class TestGetAssets:
    def test_falls_back_to_api_when_raw_data_missing(self):
        asset = SimpleNamespace(name="a.tar", size=3, browser_download_url="https://x/a.tar", id=7, digest=None)
        rel = MagicMock(spec=["get_assets", "tag_name"])
        rel.get_assets.return_value = [asset]
        assets = github._get_assets_from_release(rel)
        assert assets == [{"name": "a.tar", "size": 3, "url": "https://x/a.tar", "id": 7, "digest": None}]


class TestFetchReleases:
    @staticmethod
    def client_with(releases):
        """A client whose get_releases() is lazy like PyGithub's PaginatedList; counts what is read."""
        read = []

        def paginated():
            for r in releases:
                read.append(r.tag_name)
                yield r

        client = MagicMock()
        client.get_repo.return_value.get_releases.side_effect = paginated
        return client, read

    def test_filters_prereleases(self):
        client, _ = self.client_with([release("b", prerelease=True), release("a")])
        with patch.object(github, "_create_github_client", return_value=client):
            assert [r.tag_name for r in github.fetch_releases("trackIT-Systems", "x")] == ["a"]
            assert len(github.fetch_releases("trackIT-Systems", "x", include_prereleases=True)) == 2

    def test_limit_applies_after_the_prerelease_filter(self):
        rels = [release("pre1", prerelease=True), release("a"), release("pre2", prerelease=True), release("b"), release("c")]
        client, _ = self.client_with(rels)
        with patch.object(github, "_create_github_client", return_value=client):
            assert [r.tag_name for r in github.fetch_releases("o", "x", limit=2)] == ["a", "b"]
            assert [r.tag_name for r in github.fetch_releases("o", "x", include_prereleases=True, limit=2)] == ["pre1", "a"]

    def test_stops_reading_once_the_limit_is_reached(self):
        """Not every release of the repository is paged through just to use the first few."""
        client, read = self.client_with([release(f"r{i}") for i in range(500)])
        with patch.object(github, "_create_github_client", return_value=client):
            assert len(github.fetch_releases("o", "x", limit=5)) == 5
        assert read == ["r0", "r1", "r2", "r3", "r4"]

    def test_no_limit_returns_everything(self):
        client, _ = self.client_with([release(f"r{i}") for i in range(30)])
        with patch.object(github, "_create_github_client", return_value=client):
            assert len(github.fetch_releases("o", "x")) == 30

    def test_fewer_releases_than_the_limit(self):
        client, _ = self.client_with([release("only")])
        with patch.object(github, "_create_github_client", return_value=client):
            assert len(github.fetch_releases("o", "x", limit=5)) == 1


def offline_error():
    """What requests raises without DNS, wrapped like urllib3 does it (connection object and all)."""
    conn = urllib3.connection.HTTPSConnection("api.github.com", 443)
    cause = urllib3.exceptions.NameResolutionError(
        "api.github.com", conn, OSError("[Errno -3] Temporary failure in name resolution")
    )
    retry = urllib3.exceptions.MaxRetryError(None, "/repos/trackIT-Systems/tsOS-base", reason=cause)
    return requests.exceptions.ConnectionError(retry)


class TestErrorClassification:
    """Errors of a GitHub request are raised as the UpdateError class the daemon logs them by."""

    def fetch_raising(self, error, token=None, monkeypatch=None):
        monkeypatch.setattr(github, "_github_token", token)
        client = MagicMock()
        client.get_repo.side_effect = error
        with patch.object(github, "_create_github_client", return_value=client), pytest.raises(UpdateError) as exc:
            github.fetch_releases("trackIT-Systems", "tsOS-base")
        assert exc.value.__cause__ is error
        return exc.value

    @pytest.mark.parametrize(
        "error,token,cls,message",
        [
            (GithubException(404, {"message": "Not Found"}, None), None, ConfigurationError,
             "Repository not found: trackIT-Systems/tsOS-base; if it is a private repository, set the GH_TOKEN"),
            (GithubException(404, {"message": "Not Found"}, None), "t", ConfigurationError,
             ("Repository not found or access denied: trackIT-Systems/tsOS-base; if it is a private repository, "
              "ensure your token has 'repo' scope")),
            (GithubException(403, {"message": "Resource not accessible"}, None), None, ConfigurationError,
             "GitHub denied access to trackIT-Systems/tsOS-base"),
            (GithubException(403, {"message": "Resource not accessible"}, None), "t", ConfigurationError,
             "GitHub denied access to trackIT-Systems/tsOS-base; check that your token"),
            (GithubException(401, {"message": "Bad credentials"}, None), "t", ConfigurationError,
             "GitHub rejected the token"),
            (RateLimitExceededException(403, {"message": "API rate limit exceeded for 1.2.3.4."}, None), None,
             TransientError, "GitHub API rate limit exceeded; set GH_TOKEN for a higher limit"),
            (GithubException(403, {"message": "x"}, {"X-RateLimit-Remaining": "0"}), "t", TransientError,
             "GitHub API rate limit exceeded"),
            (GithubException(429, {"message": "Too Many Requests"}, None), "t", TransientError,
             "GitHub API rate limit exceeded"),
            (GithubException(502, {"message": "Server Error"}, None), None, TransientError,
             "GitHub API error 502: Server Error"),
            (GithubException(422, {"message": "Validation Failed"}, None), None, UpdateError,
             "GitHub API error 422: Validation Failed"),
            (offline_error(), None, TransientError,
             "Cannot reach GitHub: Failed to resolve 'api.github.com'"),
            (requests.exceptions.ConnectionError("Connection refused"), None, TransientError,
             "Cannot reach GitHub: Connection refused"),
            (requests.exceptions.ReadTimeout("Read timed out. (read timeout=15)"), None, TransientError,
             "Timed out talking to GitHub: Read timed out"),
            (TimeoutError("timed out"), None, TransientError, "Timed out talking to GitHub: timed out"),
        ],
    )
    def test_maps_to_class_and_message(self, monkeypatch, error, token, cls, message):
        exc = self.fetch_raising(error, token, monkeypatch)
        assert type(exc) is cls
        assert str(exc).startswith(message), str(exc)

    def test_offline_message_is_stable(self, monkeypatch):
        """The message must not contain anything that changes per attempt, or every check would log it."""
        assert str(self.fetch_raising(offline_error(), None, monkeypatch)) == str(
            self.fetch_raising(offline_error(), None, monkeypatch)
        )
        assert "object at 0x" not in str(self.fetch_raising(offline_error(), None, monkeypatch))

    def test_token_is_never_in_the_message(self, monkeypatch):
        exc = self.fetch_raising(GithubException(403, {"message": "x"}, None), "ghp_secret", monkeypatch)
        assert "ghp_secret" not in str(exc)

    def test_unexpected_errors_are_not_mapped(self):
        client = MagicMock()
        client.get_repo.side_effect = KeyError("bug")
        with patch.object(github, "_create_github_client", return_value=client), pytest.raises(KeyError):
            github.fetch_releases("trackIT-Systems", "tsOS-base")

    def test_no_releases_is_not_an_error(self):
        client = MagicMock()
        client.get_repo.return_value.get_releases.return_value = iter([])
        with patch.object(github, "_create_github_client", return_value=client):
            assert github.fetch_releases("trackIT-Systems", "tsOS-base") == []


class TestUpdateSource:
    def os_release(self, **fields):
        from tsupdate.status import OSRelease

        return OSRelease(fields)

    def test_support_url_and_version(self):
        rel = self.os_release(VERSION_ID="2025.12.3", SUPPORT_URL="https://github.com/trackIT-Systems/tsOS-base")
        assert github.update_source(rel) == ("trackIT-Systems", "tsOS-base", "2025.12.3")

    def test_configured_url_wins(self):
        rel = self.os_release(VERSION_ID="1", SUPPORT_URL="https://github.com/trackIT-Systems/a")
        assert github.update_source(rel, "https://github.com/trackIT-Systems/b")[:2] == ("trackIT-Systems", "b")

    @pytest.mark.parametrize(
        "fields,message",
        [
            ({"VERSION_ID": "1"}, "SUPPORT_URL not found"),
            ({"SUPPORT_URL": "https://github.com/trackIT-Systems/a"}, "VERSION_ID not found"),
            ({"VERSION_ID": "1", "SUPPORT_URL": "https://github.com/evil/a"}, "Not an allowed update source"),
            ({"VERSION_ID": "1", "SUPPORT_URL": "not a url"}, "Not an allowed update source"),
        ],
    )
    def test_configuration_errors(self, fields, message):
        with pytest.raises(ConfigurationError, match=message):
            github.update_source(self.os_release(**fields))

    def test_unreadable_os_release(self):
        with pytest.raises(UpdateError, match="Could not read /etc/os-release"):
            github.update_source(None)


class TestResolveReleaseAsset:
    URL = "https://github.com/trackIT-Systems/tsOS-base/releases/download/2025.12.4/tsOS-base-arm64-2025.12.4.zip"

    def _client(self):
        client = MagicMock()
        client.get_repo.return_value.get_release.return_value = RELEASES[0]
        return client

    def test_public_resolution_returns_browser_url_and_metadata(self, monkeypatch):
        monkeypatch.setattr(github, "_github_token", None)
        with patch.object(github, "_create_github_client", return_value=self._client()):
            asset = github.resolve_github_release_asset(self.URL)
        assert asset == {"url": self.URL, "size": 600, "digest": "sha256:" + "0" * 64}

    def test_with_token_uses_api_endpoint(self, monkeypatch):
        monkeypatch.setattr(github, "_github_token", "t")
        with patch.object(github, "_create_github_client", return_value=self._client()):
            asset = github.resolve_github_release_asset(self.URL)
        assert asset["url"] == "https://api.github.com/repos/trackIT-Systems/tsOS-base/releases/assets/2"

    def test_unknown_asset(self, monkeypatch):
        with (
            patch.object(github, "_create_github_client", return_value=self._client()),
            pytest.raises(ConfigurationError, match="Asset 'tsOS-base-arm64-2025.12.4.nope' not found"),
        ):
            github.resolve_github_release_asset(self.URL.replace(".zip", ".nope"))

    def test_unknown_tag(self):
        client = self._client()
        client.get_repo.return_value.get_release.side_effect = GithubException(404, {"message": "Not Found"}, None)
        with (
            patch.object(github, "_create_github_client", return_value=client),
            pytest.raises(ConfigurationError, match="Release tag '2025.12.4' not found"),
        ):
            github.resolve_github_release_asset(self.URL)

    def test_tag_url_without_asset_is_rejected(self):
        url = "https://github.com/trackIT-Systems/tsOS-base/releases/tag/2025.12.4"
        with pytest.raises(ConfigurationError, match="asset name required"):
            github.resolve_github_release_asset(url)

    def test_disallowed_url_is_rejected(self):
        with pytest.raises(ConfigurationError, match="Invalid GitHub release URL"):
            github.resolve_github_release_asset("https://github.com/evil/x/releases/download/v1/a.tar")

    def test_unreachable_github_is_transient(self):
        client = self._client()
        client.get_repo.side_effect = requests.exceptions.ConnectionError("down")
        with (
            patch.object(github, "_create_github_client", return_value=client),
            pytest.raises(TransientError, match="Cannot reach GitHub: down"),
        ):
            github.resolve_github_release_asset(self.URL)


class TestToken:
    def test_env_token_wins(self, monkeypatch):
        monkeypatch.setattr(github, "_github_token", None)  # restored after the test
        monkeypatch.setenv("GH_TOKEN", "  abc  ")
        with patch("subprocess.run") as run:
            github.initialize_github_token()
        run.assert_not_called()
        assert github.get_github_token() == "abc"

    def test_no_gh_cli(self, monkeypatch):
        monkeypatch.setattr(github, "_github_token", "stale")
        monkeypatch.delenv("GH_TOKEN", raising=False)
        with patch("subprocess.run", side_effect=FileNotFoundError):
            github.initialize_github_token()
        assert github.get_github_token() is None
