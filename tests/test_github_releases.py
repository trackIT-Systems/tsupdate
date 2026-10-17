"""Tests for picking images/batch updates out of GitHub release listings (PyGithub objects faked)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tsupdate import github


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
        with patch.object(github, "_create_github_client", return_value=self._client()):
            assert github.resolve_github_release_asset(self.URL.replace(".zip", ".nope")) is None

    def test_tag_url_without_asset_is_rejected(self):
        url = "https://github.com/trackIT-Systems/tsOS-base/releases/tag/2025.12.4"
        assert github.resolve_github_release_asset(url) is None


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
