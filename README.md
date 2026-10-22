# tsupdate

[![Tests](https://github.com/trackIT-Systems/tsupdate/actions/workflows/tests.yml/badge.svg)](https://github.com/trackIT-Systems/tsupdate/actions/workflows/tests.yml)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/trackIT-Systems/tsupdate/badges/coverage.json)](https://github.com/trackIT-Systems/tsupdate/actions/workflows/coverage-badge.yml)
[![Python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2FtrackIT-Systems%2Ftsupdate%2Fmain%2Fpyproject.toml)](pyproject.toml)
[![Debian](https://img.shields.io/badge/debian-13%20trixie-A81D33?logo=debian&logoColor=white)](https://www.debian.org/releases/trixie/)

Update daemon for tsOS-based Raspberry Pi devices, with A/B partitions and automatic rollback.

`tsupdate` keeps two root partitions (`rootfs` and `clonefs`). An update is written to the one that is *not* running, the Pi is told to boot it **once** (tryboot), and only if the new system comes up and stays up is it made permanent. If it doesn't boot, the firmware falls back to the old partition by itself, so a bad update cannot brick a device that is deployed somewhere without physical access.

- **`tsupdate`** is the command line tool: inspect, update, restore and roll back by hand.
- **`tsupdated`** is the daemon: check GitHub for updates, download, apply, reboot and persist unattended, optionally only inside a maintenance window.

## Contents

- [How an update works](#how-an-update-works)
- [Safety](#safety)
- [Requirements](#requirements)
- [Installation](#installation)
- [Commands](#commands)
- [Typical workflows](#typical-workflows)
- [Automatic updates (daemon)](#automatic-updates-daemon)
- [GitHub authentication](#github-authentication)
- [Creating updates](#creating-updates)
- [Development](#development)
- [Documentation](#documentation)

## How an update works

```
 running on rootfs (p2)                    inactive: clonefs (p3)
 ──────────────────────                    ──────────────────────
 1. check      find an update for the running version on GitHub
 2. download   fetch it to /data/tsupdate, verified against GitHub's size/SHA-256
 3. syncroot   rsync the running system to clonefs (byte-accurate base)
 4. apply      apply the pidiff rsync batch to clonefs
 5. tryboot    write tryline.txt/tryboot.txt, reboot "0 tryboot"
                          │
                          ▼
              ┌── boots clonefs ──────────────┐
              │                               │
        works for persist_timeout        fails to boot
              │                               │
        6. persist: cmdline.txt           firmware falls back to rootfs;
           now points to clonefs          the daemon sees the same update again
        7. clean up /data/tsupdate        and gives up after 2 attempts
```

A full image can be written instead of a pidiff with `tsupdate restore`.

## Safety

| Risk | Protection |
|---|---|
| New system doesn't boot | tryboot is one-shot; the firmware falls back to the old partition. The boot configuration is only persisted after `persist_timeout` seconds of uptime. |
| Power cut while writing boot files | `cmdline.txt`, `tryline.txt` and `tryboot.txt` are written to a temp file, fsynced and renamed, so a truncated file is never left behind. |
| Update that fails to boot, again and again | Each attempt is recorded in `/data/tsupdate-state.json`. An update is tried at most twice from the same version, then skipped until a newer one appears. |
| Operator cancels the reboot | `SIGTERM` during the countdown cancels it, and the cancelled update is not re-applied when systemd restarts the daemon. |
| Wrong base system | `apply` refuses an update whose base `PRETTY_NAME` doesn't match the target partition. |
| Malicious update source | Update sources are pinned to the `trackIT-Systems` GitHub organisation in code, not configurable. Local files given on the command line are exempt. |
| Tampered or truncated download | Downloads are checked against the size/SHA-256 digest GitHub reports; a cached file is only reused if it verifies, and redirects may only end at GitHub's asset hosts. |
| Tampered extracted image | `restore` always extracts the image again; an `.img` found in the world-writable `/data` is never trusted. |
| Disk filling up | Downloads are deleted after a successful update. After a failure they are kept, so a retry doesn't download again (relevant on cellular links). |

## Requirements

- Raspberry Pi with tryboot support (Pi 4, 400, CM4, Pi 5)
- Python 3.13 or newer, and `PyGithub`, `PyYAML` and `requests` at least as new as in Debian 13 "trixie" (`python3-github`, `python3-yaml`, `python3-requests`)
- A/B layout on the SD card: `/dev/mmcblk0p2` (rootfs) and `/dev/mmcblk0p3` (clonefs). Booting from NVMe/USB is not supported yet ([#3](https://github.com/trackIT-Systems/tsupdate/issues/3)).
- Root privileges for everything except `status`, `check` and `download`
- `rsync`, `mount`/`umount`; `losetup` for `restore`

## Installation

On a tsOS image the tools are preinstalled. To install from source:

```bash
git clone https://github.com/trackIT-Systems/tsupdate.git
cd tsupdate
pip install .
```

This installs `tsupdate` and `tsupdated`. [`scheduleparse`](https://github.com/trackIT-Systems/scheduleparse) is pulled from GitHub because it isn't packaged in Debian.

## Commands

Add `-v` before the command (`tsupdate -v apply ...`) for debug logging.

### Status

```bash
tsupdate status          # OS version, active/inactive partition, boot method
tsupdate status --json
```

### Updates

```bash
tsupdate check                       # latest image and the next update for the running version
tsupdate check --pre                 # include pre-releases
tsupdate check --max-releases 10     # look at more recent releases (default 5)
tsupdate check --github-url URL      # another repository of the trackIT-Systems organisation

tsupdate download <url|file>         # fetch into /data/tsupdate without applying

sudo tsupdate apply <url|file>       # apply a pidiff update to the inactive partition
sudo tsupdate apply <url> -k         # keep the downloaded file
sudo tsupdate apply <url> --rsync-timeout 7200   # seconds, 0 = unlimited (default 3600)

sudo tsupdate restore <url|file>     # write a complete image (.img, .gz, .xz, .zip)
sudo tsupdate restore <url> -p 3     # use partition 3 of the image as rootfs (default 2)
sudo tsupdate restore <url> -k       # keep the downloaded image
```

`apply` and `restore` need a regular boot, or a tryboot that has been persisted.

### Boot management

```bash
sudo tsupdate tryboot [--no-reboot]    # boot the other partition once
sudo tsupdate persist [--reboot]       # make a working tryboot permanent
sudo tsupdate rollback [--no-reboot]   # go back to the previous partition
```

### Partitions

```bash
sudo tsupdate syncroot [--rsync-timeout SEC]   # clone the running system to the inactive partition
sudo tsupdate mount                            # mount the inactive partition at /media/root-up
sudo tsupdate unmount
```

### Exit codes

A failed command prints its error to stderr (`ERROR: ...`, no traceback) and exits with:

| Code | Meaning |
| --- | --- |
| 0 | Success. For `check` also when no image or update was found |
| 1 | The operation failed, e.g. a corrupt archive, a version mismatch, a mount failure |
| 75 | Temporary failure, retrying later may work: no network, DNS failure, timeout, GitHub 5xx or rate limit, interrupted download (`EX_TEMPFAIL`) |
| 78 | Needs a human: repository, release or asset not found, access denied, update source not allowed, file not found (`EX_CONFIG`) |
| 124 | rsync ran into `--rsync-timeout` |
| other | `apply`: rsync's own exit code, e.g. 23 for a partial transfer |

## Typical workflows

**Incremental update**

```bash
tsupdate check
sudo tsupdate syncroot                                # inactive partition = running system
sudo tsupdate apply <url-printed-by-check>            # apply the pidiff
sudo tsupdate tryboot                                 # reboot into it
# ... verify the new system ...
sudo tsupdate persist                                 # keep it, or: sudo tsupdate rollback
```

**Full image restore**

```bash
sudo tsupdate restore <image-url>
sudo tsupdate tryboot
sudo tsupdate persist
```

**Inspect the inactive partition**

```bash
sudo tsupdate mount
ls /media/root-up
sudo tsupdate unmount
```

## Automatic updates (daemon)

```bash
sudo cp tsupdate.example.yml /boot/firmware/tsupdate.yml
sudo cp tsupdated.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tsupdated
sudo journalctl -u tsupdated -f
```

`tsupdated [-c CONFIG] [-s SCHEDULE] [-v]` reads `/boot/firmware/tsupdate.yml` and `/boot/firmware/schedule.yml` by default.

### Configuration

| Option | Default | Meaning |
|---|---|---|
| `check_interval` | `3600` | Seconds between update checks (minimum 60) |
| `do` | `check` | `nothing`, `check` (only log), `download`, or `apply` (the full workflow including reboot) |
| `include_prereleases` | `false` | Also consider pre-releases |
| `max_releases` | `5` | How many recent releases to search (at least 1) |
| `github_url` | from `SUPPORT_URL` in `/etc/os-release` | Repository to check; must belong to `trackIT-Systems` |
| `update_countdown` | `60` | Seconds between "update applied" and the reboot, during which it can be cancelled |
| `persist_timeout` | `600` | Uptime after a tryboot before the new system is made permanent |
| `rsync_timeout` | `3600` | Wall-clock limit for rsync in seconds, `0` = unlimited |
| `maintenance_do` | `apply` if a schedule exists | Behaviour inside the maintenance window |
| `maintenance_check_interval` | `check_interval` | Check interval inside the window |

Invalid values are reported at startup: a value of the wrong type stops the daemon, a number out of range falls back to the default with a warning. See [`tsupdate.example.yml`](tsupdate.example.yml).

### Maintenance window

Restrict updates to a time of day with an entry named `maintenance` in `schedule.yml`:

```yaml
schedule:
- name: maintenance
  start: '02:00'
  stop: '03:30'
```

Outside the window the daemon uses `do`/`check_interval`; inside it uses `maintenance_do`/`maintenance_check_interval`. It wakes up when the window opens, even if that is sooner than `check_interval`, so a short window is never skipped. Use fixed `HH:MM` times; an unparseable time is an error and the window is ignored, not guessed.

### Cancelling and failed updates

After applying an update the daemon waits `update_countdown` seconds before rebooting. To cancel, stop it:

```bash
sudo systemctl stop tsupdated        # or: sudo kill -TERM $(pgrep -f tsupdated)
```

A cancelled update, and an update that was tried twice without success, is not applied again automatically. A newer release is picked up normally. To allow it again, remove the record:

```bash
sudo rm /data/tsupdate-state.json
```

See **[docs/daemon.md](docs/daemon.md)** for the complete reference.

## GitHub authentication

Update sources must be in the `trackIT-Systems` organisation. For its private repositories, give `tsupdate` a token via `GH_TOKEN`, or log in with the GitHub CLI (`gh auth login`):

```bash
export GH_TOKEN="ghp_..."
tsupdate check
sudo -E tsupdate apply https://github.com/trackIT-Systems/<repo>/releases/download/<tag>/<asset>
```

The daemon reads `GH_TOKEN` from its environment, or falls back to `gh auth token`. The token is only ever sent to `github.com` and `api.github.com`.

## Creating updates

Incremental updates are rsync batch files made with [pimod's `pidiff`](https://github.com/nature40/pimod):

```bash
pidiff --tar base.img updated.img      # produces a tar with batch.sh and the rsync batch
sudo tsupdate apply update.tar
```

Release assets are expected to be named `tsOS-<variant>-arm64-update-<from>-to-<to>.tar[.gz]`; the daemon picks the one whose `<from>` is the running `VERSION_ID`.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

The tests need no Pi and no root: `/boot/firmware` is redirected to a temp directory and partition detection is faked ([`tests/conftest.py`](tests/conftest.py)); `mount`, `rsync`, `losetup` and `reboot` are never executed. Bugs that are known but not fixed yet are recorded as strict `xfail` tests, which turn red as soon as the bug is fixed so the marker gets removed.

CI ([`tests.yml`](.github/workflows/tests.yml)) runs the tests on Python 3.13 with pip, and again inside a `debian:trixie` container with the distribution's own `python3-github`, `python3-yaml` and `python3-requests`, which is what the devices run. Coverage is published as the badge above by [`coverage-badge.yml`](.github/workflows/coverage-badge.yml) on every push to `main`.

| Module | Purpose |
|---|---|
| `cli.py`, `cli_daemon.py` | `tsupdate` and `tsupdated` entry points |
| `daemon.py`, `schedule.py`, `state.py` | daemon loop, maintenance windows, persistent attempt record |
| `apply.py`, `restore.py`, `syncroot.py` | pidiff apply, image restore, partition mount/sync |
| `tryboot.py`, `status.py` | boot files and tryboot, partition and OS detection |
| `github.py`, `utils.py` | releases, source pinning, verified downloads |

## Documentation

Module reference in [`docs/`](docs/): [daemon](docs/daemon.md), [status](docs/status.md), [tryboot](docs/tryboot.md), [syncroot](docs/syncroot.md), [apply](docs/apply.md), [restore](docs/restore.md), [github](docs/github.md), [utils](docs/utils.md).

## Version scheme

[CalVer](https://calver.org) as `YEAR.MONTH.COUNT`, e.g. `2025.12.1`. See the [changelog](CHANGELOG.md) for what changed in each version.

## Authors

Jonas Höchst <hoechst@trackit.systems>

## Links

- [Raspberry Pi tryboot documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#fail-safe-os-updates-tryboot)
- [pimod](https://github.com/nature40/pimod), for creating Raspberry Pi images and incremental updates
