# Daemon Module

The `daemon` module provides automatic background update management for tsOS-based systems.

## Overview

`tsupdated` is a standalone daemon that runs continuously in the background, automatically checking for and applying system updates using the tryboot mechanism for safe rollback. It is completely independent from the `tsupdate` CLI tool but uses the same underlying library functions.

## Features

- **Automatic update checking** - Periodically queries GitHub releases for available updates
- **Tryboot persistence** - On startup after tryboot, waits then persists configuration
- **Safe update application** - Syncs root partition, applies update, then uses tryboot
- **User notification** - Notifies via system logs and wall broadcasts
- **Cancellation support** - Users can cancel pending reboots via SIGTERM/SIGINT
- **Robust error handling** - Never crashes on errors, continues operation
- **YAML configuration** - Flexible configuration via YAML file

## Architecture

The daemon follows this workflow:

1. **Startup**: Check if running as root, load configuration, initialize GitHub token
2. **Tryboot Check**: If booted via tryboot, wait for persist timeout then persist config
3. **Main Loop**: Periodically check for updates
4. **Update Found**: Download → Syncroot → Apply → Notify → Countdown → Tryboot reboot
5. **After Reboot**: Daemon restarts, persists tryboot, continues checking

## Installation

### Install Package

```bash
# Install from source with daemon support
cd tsupdate
pip install -e .
```

This installs both `tsupdate` (CLI tool) and `tsupdated` (daemon) commands.

### Create Configuration

```bash
# Copy example configuration to boot partition
sudo cp tsupdate.example.yml /boot/firmware/tsupdate.yml

# Edit configuration as needed
sudo nano /boot/firmware/tsupdate.yml
```

### Install Systemd Service

```bash
# Copy service file to systemd directory
sudo cp tsupdated.service /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable daemon to start on boot
sudo systemctl enable tsupdated

# Start daemon immediately
sudo systemctl start tsupdated
```

## Configuration

Configuration is stored in YAML format. Default location: `/boot/firmware/tsupdate.yml`

### Configuration Options

**`check_interval`** (integer, default: 3600)
- How often to check for updates (in seconds)
- Example: `3600` (1 hour), `86400` (24 hours)

**`include_prereleases`** (boolean, default: false)
- Whether to include pre-releases when checking for updates
- Set to `true` to receive beta/RC updates

**`github_url`** (string, optional)
- Override GitHub repository URL
- If not set, uses `SUPPORT_URL` from `/etc/os-release`
- Example: `"https://github.com/owner/repo"`

**`max_releases`** (integer, default: 5)
- Maximum number of recent releases to check for batch updates
- Higher values check more releases but use more API calls

**`persist_timeout`** (integer, default: 600)
- System uptime threshold (in seconds) before persisting tryboot configuration
- When booted via tryboot, waits until system uptime reaches this value before persisting
- Allows time to verify update was successful, regardless of when daemon starts
- Example: `600` (10 minutes), `300` (5 minutes)

**`update_countdown`** (integer, default: 60)
- Seconds to wait before rebooting after applying update
- Users can cancel during this countdown
- Example: `60` (1 minute), `300` (5 minutes)

### Example Configuration

```yaml
# Check for updates every 6 hours
check_interval: 21600

# Only stable releases
include_prereleases: false

# Wait until 15 minutes of uptime before persisting after tryboot
persist_timeout: 900

# Give users 2 minutes to cancel update
update_countdown: 120
```

## Usage

### Running the Daemon

**As a systemd service (recommended):**

```bash
# Start the service
sudo systemctl start tsupdated

# Check status
sudo systemctl status tsupdated

# View logs
sudo journalctl -u tsupdated -f
```

**Manually (for testing):**

```bash
# Run with default config
sudo tsupdated

# Run with custom config
sudo tsupdated --config /path/to/config.yml

# Run with verbose logging
sudo tsupdated --verbose

# Run with custom config and verbose logging
sudo tsupdated -c /path/to/config.yml -v
```

### Monitoring

**View daemon logs:**

```bash
# Follow logs in real-time
sudo journalctl -u tsupdated -f

# View last 100 lines
sudo journalctl -u tsupdated -n 100

# View logs since last boot
sudo journalctl -u tsupdated -b
```

**Check daemon status:**

```bash
# Systemd status
sudo systemctl status tsupdated

# Check if daemon is running
pgrep -fa tsupdated
```

### Cancelling Updates

During the countdown before reboot, users can cancel the update:

**Using systemctl:**

```bash
# Restart the daemon (cancels countdown)
sudo systemctl restart tsupdated
```

**Using kill:**

```bash
# Find daemon process ID
PID=$(pgrep -f tsupdated)

# Send SIGTERM to cancel
sudo kill -TERM $PID
```

**From wall message:**

When the daemon is about to reboot, it broadcasts a message via `wall` showing:
- The countdown timer
- Instructions for cancellation

## Daemon Workflow

### Startup Sequence

1. **Root Check**: Verify running as root (required for partition operations)
2. **Load Config**: Parse YAML configuration file, apply defaults
3. **GitHub Token**: Initialize GitHub authentication (from `GH_TOKEN` or `gh` CLI)
4. **Signal Handlers**: Register handlers for SIGTERM and SIGINT
5. **Tryboot Check**: If booted via tryboot, wait until system uptime reaches persist timeout then persist

### Update Check Loop

1. **Check Timer**: Wait for `check_interval` seconds (sleeping in small increments)
2. **Query GitHub**: Fetch releases from repository
3. **Find Update**: Look for applicable batch update matching current version
4. **If Found**: Proceed to update workflow
5. **If Not Found**: Log and return to step 1

### Update Application Workflow

1. **Sync Root**: Execute `syncroot` to prepare inactive partition
2. **Apply Update**: Download and apply batch update to inactive partition
3. **Notify Users**: 
   - Log message to systemd journal
   - Broadcast via `wall` command to all terminals
   - Include countdown time and cancellation instructions
4. **Countdown**: 
   - Count down from `update_countdown` seconds
   - Check for cancellation signal every second
   - Send reminders at 30s and 10s remaining
5. **Tryboot Reboot**: 
   - Execute `tryboot` command with automatic reboot
   - Daemon exits (systemd will restart after reboot)
6. **After Reboot**: 
   - Daemon starts in new partition
   - Waits until system uptime reaches `persist_timeout` then persists configuration
   - Continues normal operation

### Error Handling

The daemon is designed to never crash:

- **Configuration Errors**: Use defaults if config is missing/invalid
- **Update Check Errors**: Log and retry at next interval
- **Update Application Errors**: Log, clean up, return to main loop
- **Network Errors**: Log and retry at next interval
- **GitHub API Errors**: Log and retry at next interval

All exceptions are caught, logged with full stack trace, and the daemon continues operation.

## Signal Handling

### SIGTERM / SIGINT

**During countdown**: Cancels the pending reboot, returns to main loop

**During normal operation**: Initiates graceful shutdown

### Example

```bash
# Find daemon PID
PID=$(pgrep -f tsupdated)

# Send SIGTERM (graceful shutdown)
sudo kill -TERM $PID

# Send SIGINT (Ctrl+C equivalent)
sudo kill -INT $PID
```

## Logging

The daemon logs to stdout/stderr, which is captured by systemd journal.

### Log Levels

**INFO**: Normal operation messages
- "Checking for updates..."
- "Update found: ..."
- "Update applied successfully"

**WARNING**: Non-critical issues
- "Configuration file not found"
- "wall command not available"

**ERROR**: Critical issues that prevent operations
- "Failed to sync root partition"
- "Failed to apply update"

**DEBUG**: Detailed operation info (enabled with `--verbose`)
- Configuration details
- GitHub API calls
- File operations

### Structured Logging

Messages follow a consistent format:

```
2025-12-23 14:30:00 - INFO: Checking for updates...
2025-12-23 14:30:05 - INFO: ✓ Update available: https://github.com/.../update.tar
2025-12-23 14:30:10 - INFO: Preparing inactive partition...
2025-12-23 14:35:00 - INFO: ✓ Update applied successfully
```

## Security Considerations

### Root Privileges

The daemon must run as root to:
- Mount/unmount partitions
- Modify boot configuration files in `/boot/firmware`
- Execute reboot command

### Systemd Hardening

The provided systemd service includes security features:

```ini
ProtectHome=read-only          # Limit access to home directories
ProtectSystem=strict            # Protect most of filesystem
ReadWritePaths=/boot/firmware   # Only allow writes to needed paths
PrivateTmp=yes                  # Use private /tmp
```

### Network Access

The daemon requires network access to:
- Query GitHub API for releases
- Download update files

Consider using firewall rules to restrict to GitHub domains if needed.

## Troubleshooting

### Daemon Won't Start

**Check if running as root:**

```bash
sudo systemctl status tsupdated
```

**Check configuration file:**

```bash
# Verify config exists
ls -l /boot/firmware/tsupdate.yml

# Check YAML syntax
python3 -c "import yaml; yaml.safe_load(open('/boot/firmware/tsupdate.yml'))"
```

### Updates Not Being Applied

**Check logs for errors:**

```bash
sudo journalctl -u tsupdated -n 100
```

**Verify GitHub URL:**

```bash
# Check SUPPORT_URL in os-release
grep SUPPORT_URL /etc/os-release
```

**Test GitHub connectivity:**

```bash
# Check if GitHub is reachable
curl -I https://api.github.com

# Test with tsupdate CLI
tsupdate check
```

### Daemon Keeps Restarting

**Check for configuration errors:**

```bash
# View last crash log
sudo journalctl -u tsupdated -n 50

# Validate configuration
sudo tsupdated --config /boot/firmware/tsupdate.yml --verbose
```

**Check disk space:**

```bash
# Verify /data/tsupdate has space
df -h /data/tsupdate

# Check boot partition space
df -h /boot/firmware
```

### GitHub Rate Limiting

**Symptom**: Daemon logs "GitHub API rate limit exceeded"

**Solution**: Set up GitHub authentication

```bash
# Option 1: Export GH_TOKEN
export GH_TOKEN="ghp_your_token_here"
sudo systemctl restart tsupdated

# Option 2: Use GitHub CLI
gh auth login
sudo systemctl restart tsupdated
```

## Best Practices

### Configuration

1. **Check Interval**: Balance between responsiveness and API usage
   - Production: 3600-86400 seconds (1-24 hours)
   - Development: 300-900 seconds (5-15 minutes)

2. **Persist Timeout**: Allow enough time to verify system stability
   - Production: 600-1800 seconds (10-30 minutes)
   - Development: 60-300 seconds (1-5 minutes)

3. **Update Countdown**: Give users time to react
   - Production: 60-300 seconds (1-5 minutes)
   - Maintenance windows: Can be shorter

### Monitoring

1. **Set up log monitoring**: Use log aggregation to track updates
2. **Monitor success rate**: Track update application success/failure
3. **Alert on errors**: Set up alerts for repeated errors

### Testing

Before deploying to production:

1. **Test with mock repository**: Use `github_url` override
2. **Test cancellation**: Verify users can cancel updates
3. **Test error handling**: Simulate network failures, bad updates
4. **Test tryboot persistence**: Verify persistence after successful boot

## Integration with CLI Tool

The daemon is independent but complementary to the CLI tool:

### When to Use CLI

- Manual update checks
- One-time update application
- System inspection and status
- Partition operations
- Rollback operations

### When to Use Daemon

- Automatic background updates
- Production environments
- Unattended systems
- IoT/embedded devices

### Using Both

The daemon and CLI can coexist:

```bash
# Daemon runs in background
sudo systemctl start tsupdated

# Meanwhile, use CLI for manual operations
tsupdate status
sudo tsupdate mount
# ... inspect files ...
sudo tsupdate unmount

# CLI and daemon share the same library functions
# They won't interfere with each other
```

## Examples

### Basic Setup

```bash
# 1. Install package
cd tsupdate
pip install -e .

# 2. Create configuration
sudo cp tsupdate.example.yml /boot/firmware/tsupdate.yml

# 3. Install and start service
sudo cp tsupdated.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tsupdated

# 4. Monitor logs
sudo journalctl -u tsupdated -f
```

### Private Repository Setup

```bash
# 1. Set up GitHub token
export GH_TOKEN="ghp_your_token_here"

# 2. Configure custom repository
sudo tee /boot/firmware/tsupdate.yml << 'EOF'
check_interval: 3600
github_url: "https://github.com/private-org/tsos"
include_prereleases: false
EOF

# 3. Start daemon with environment
sudo -E systemctl start tsupdated
```

### Development Testing

```bash
# 1. Run daemon in foreground with verbose logging
sudo tsupdated --config ./test-config.yml --verbose

# 2. In another terminal, monitor logs
sudo journalctl -u tsupdated -f

# 3. To cancel update during countdown
sudo kill -TERM $(pgrep -f tsupdated)
```

## See Also

- **[status.md](status.md)** - System status and partition detection
- **[tryboot.md](tryboot.md)** - Tryboot configuration and management
- **[syncroot.md](syncroot.md)** - Partition mounting and syncing
- **[apply.md](apply.md)** - Incremental update application
- **[github.md](github.md)** - GitHub API integration

## Authors

Jonas Höchst <hoechst@trackit.systems>

