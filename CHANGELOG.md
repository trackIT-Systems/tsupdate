# Changelog

Versions follow [CalVer](https://calver.org) as `YYYY.M.N`: year, month (no zero padding) and the
running number of the release within that month. Entries are derived from the git history.

`2025.12.1` was set in the code from the initial commit on; the later versions were tagged
retroactively on the last commit of their month. Everything newer is unreleased.

## Unreleased

### Added
- Check for updates when the maintenance window opens, not only every `check_interval`.
- Tests for status, tryboot, apply, downloads, releases, daemon and CLI, run in CI.
- Status badges in the README and a published coverage badge.

### Changed
- Require Python 3.13 and the dependency versions shipped by Debian trixie; require scheduleparse 2026.10.3.
- Always re-extract the image in `restore` instead of reusing a cached `.img`.
- Scan loopback partitions with `losetup --partscan` instead of relying on `partprobe`.
- Only fetch as many releases as are searched instead of paging through all of them.
- Log the tryboot persist countdown about once a minute.
- Rewrite the README.
- Upgrade `actions/checkout` and `actions/setup-python` to v7.
- Pin update sources to the trackIT-Systems GitHub org, with tests for the org pin and cache verification.

### Fixed
- Derive the inactive partition (p2 <-> p3) from the booted disk, so NVMe, USB and other drives work and `syncroot`, `apply` and `restore` can no longer target `/dev/mmcblk0` when booted from elsewhere. Tryboot and rollback now fail instead of silently rebooting into the same partition when `cmdline.txt` has no `root=`. `root=PARTUUID=`/`UUID=`/`LABEL=` is not resolved (#3).
- Extract update archives to `/data/tsupdate/extract` instead of `/tmp`, remove the directory when extraction fails, and reject members that would escape it (`filter="data"`) (#7).
- Fix the atime assertion in the persist test.
- Stop re-applying an update that failed to boot (reboot loop).
- Make cancelling an update with SIGTERM stick.
- Write boot files atomically so a power cut cannot leave a truncated `cmdline.txt`.
- Reject unparseable maintenance window times.
- Validate `update_countdown`, `persist_timeout` and `max_releases` in the daemon config.
- Clean up downloads after successful updates.

## 2026.7.1 - 2026-07-20

- Preserve `cmdline.txt` mtime and atime during tryboot operations.

## 2026.4.1 - 2026-04-16

- Verify the syncroot using checksums.
- Add an rsync timeout for better error handling (#1).

## 2026.2.1 - 2026-02-27

- Use GitHub API URLs for private downloads.

## 2026.1.1 - 2026-01-09

- Add `do` / `maintenance_do` config parameters.
- Resumable downloads.
- Respect the maintenance window.
- Disable GitHub retry to fix signal handling.

## 2025.12.1 - 2025-12-23

- Initial version (2025-12-22): tryboot/persist tools, syncroot / apply and restore functionality.
- Add the tsupdate daemon.
- GitHub releases integration; load the GitHub token from the `gh` tool.
- Make rollback similar to tryboot (automatic reboot) and change the tryboot reboot default.
- Fix downloading public assets and image files.
- Add documentation.
