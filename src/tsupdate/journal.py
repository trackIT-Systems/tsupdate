"""Logging to the systemd journal.

Follows tsOS-base's logging convention (docs/logging.md). Under systemd, every
record goes to the journal over its native protocol, with its priority, the
program's SYSLOG_IDENTIFIER and the source location as separate fields; the
message itself is only `<logger name>: <message>`. On a terminal (or in tests)
records go to stderr in the format the caller chooses.
"""

import logging
import os
import socket
import struct
import sys

JOURNAL_SOCKET = "/run/systemd/journal/socket"

# Python level -> syslog priority (RFC 5424); 0, 1 and 5 are not used
PRIORITIES = ((logging.CRITICAL, 2), (logging.ERROR, 3), (logging.WARNING, 4), (logging.INFO, 6))
DEBUG_PRIORITY = 7

# Longer messages are cut, so a record always fits in one datagram
MAX_MESSAGE = 32 * 1024


def syslog_priority(levelno: int) -> int:
    """
    Map a Python log level to a syslog priority.

    Args:
        levelno: Python log level (e.g. logging.WARNING)

    Returns:
        Syslog priority (2 crit, 3 err, 4 warning, 6 info, 7 debug)
    """
    for level, priority in PRIORITIES:
        if levelno >= level:
            return priority
    return DEBUG_PRIORITY


def stderr_is_journal() -> bool:
    """
    Check whether systemd connected stderr to the journal.

    systemd sets JOURNAL_STREAM to the device and inode of that stream; a
    process whose stderr was redirected since (or a child that inherited the
    variable) has a different stderr. See systemd.exec(5).

    Returns:
        True if stderr is the journal stream, False otherwise
    """
    stream = os.environ.get("JOURNAL_STREAM")
    if not stream:
        return False
    try:
        dev, ino = (int(part) for part in stream.split(":"))
        stat = os.fstat(sys.stderr.fileno())
    except (ValueError, OSError, AttributeError):
        return False
    return (stat.st_dev, stat.st_ino) == (dev, ino)


def journal_field(key: str, value: str) -> bytes:
    """
    Encode one field of the native journal protocol.

    A value with a newline is sent with its length.

    Args:
        key: Field name (e.g. MESSAGE)
        value: Field value

    Returns:
        The encoded field
    """
    data = value.encode("utf-8", "replace")
    if b"\n" in data:
        return key.encode() + b"\n" + struct.pack("<Q", len(data)) + data + b"\n"
    return key.encode() + b"=" + data + b"\n"


class JournalHandler(logging.Handler):
    """
    Send records to journald over its native protocol (https://systemd.io/JOURNAL_NATIVE_PROTOCOL/).

    A record that cannot be sent (journald restarting) is written to stderr
    instead, with its priority as `<N>` prefix, which journald also understands.
    """

    def __init__(self, identifier: str, path: str = JOURNAL_SOCKET) -> None:
        super().__init__()
        self.identifier = identifier
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            self.socket.connect(path)
        except OSError:
            self.socket.close()
            raise

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)[:MAX_MESSAGE]
            priority = str(syslog_priority(record.levelno))
            fields = (
                ("MESSAGE", message),
                ("PRIORITY", priority),
                ("SYSLOG_IDENTIFIER", self.identifier),
                ("CODE_FILE", record.pathname),
                ("CODE_LINE", str(record.lineno)),
                ("CODE_FUNC", record.funcName),
            )
            try:
                self.socket.send(b"".join(journal_field(key, value) for key, value in fields))
            except OSError:
                sys.stderr.write(f"<{priority}>{message}\n")
                sys.stderr.flush()
        except Exception:  # noqa: BLE001 - logging must not raise into the caller
            self.handleError(record)

    def close(self) -> None:
        self.socket.close()
        super().close()


def log_handler(identifier: str, formatter: logging.Formatter) -> logging.Handler:
    """
    Create the handler for the program's log records.

    Under systemd this is the journal handler; otherwise, or when journald is
    unreachable, a handler writing to stderr with the given formatter.

    Args:
        identifier: SYSLOG_IDENTIFIER in the journal, the program's name
        formatter: Formatter for stderr (the journal always uses `<logger name>: <message>`)

    Returns:
        The handler
    """
    if stderr_is_journal():
        try:
            handler: logging.Handler = JournalHandler(identifier)
            handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
            return handler
        except OSError:
            pass
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    return handler
