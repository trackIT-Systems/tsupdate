"""Tests for logging to the journal (native protocol) and to stderr."""

import logging
import os
import socket
import struct
import tempfile
from pathlib import Path

import pytest

from tsupdate import configure_logging, journal
from tsupdate.journal import JournalHandler, journal_field, stderr_is_journal, syslog_priority


def parse_datagram(data: bytes) -> dict[str, str]:
    """The fields of one native-protocol datagram."""
    fields = {}
    while data:
        line, _, rest = data.partition(b"\n")
        if b"=" in line:
            key, _, value = line.partition(b"=")
            fields[key.decode()] = value.decode()
            data = rest
        else:
            (length,) = struct.unpack("<Q", rest[:8])
            fields[line.decode()] = rest[8 : 8 + length].decode()
            assert rest[8 + length : 9 + length] == b"\n"
            data = rest[9 + length :]
    return fields


@pytest.fixture
def journald():
    """A datagram socket standing in for journald; yields (path, receive)."""
    # AF_UNIX paths are limited to ~100 bytes, pytest's tmp_path can be longer
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        path = os.path.join(directory, "socket")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(path)
        server.settimeout(1)

        def receive() -> dict[str, str]:
            return parse_datagram(server.recv(65536))

        yield path, receive
        server.close()


@pytest.fixture
def root_logger():
    """Restore the root logger's handlers and level after configure_logging replaced them."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def make_logger(handler: logging.Handler) -> logging.Logger:
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    logger = logging.getLogger("test.journal")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger


@pytest.mark.parametrize(
    "level, priority",
    [(logging.CRITICAL, 2), (logging.ERROR, 3), (logging.WARNING, 4), (logging.INFO, 6), (logging.DEBUG, 7), (25, 6)],
)
def test_syslog_priority(level, priority):
    assert syslog_priority(level) == priority


def test_journal_field_plain_and_multiline():
    assert journal_field("MESSAGE", "hello") == b"MESSAGE=hello\n"
    assert journal_field("MESSAGE", "a\nb") == b"MESSAGE\n" + struct.pack("<Q", 3) + b"a\nb\n"


def test_handler_sends_fields(journald):
    path, receive = journald
    logger = make_logger(JournalHandler("tstest", path))

    logger.warning("Cannot read %s", "/dev/x")
    fields = receive()

    assert fields["MESSAGE"] == "test.journal: Cannot read /dev/x"
    assert fields["PRIORITY"] == "4"
    assert fields["SYSLOG_IDENTIFIER"] == "tstest"
    assert fields["CODE_FILE"] == __file__
    assert fields["CODE_FUNC"] == "test_handler_sends_fields"
    assert int(fields["CODE_LINE"]) > 0


def test_traceback_is_one_entry(journald):
    path, receive = journald
    logger = make_logger(JournalHandler("tstest", path))

    try:
        raise ValueError("broken")
    except ValueError:
        logger.exception("Failed")
    fields = receive()

    assert fields["PRIORITY"] == "3"
    assert fields["MESSAGE"].startswith("test.journal: Failed\nTraceback")
    assert fields["MESSAGE"].endswith("ValueError: broken")


def test_send_failure_falls_back_to_stderr(journald, capsys):
    path, _ = journald
    handler = JournalHandler("tstest", path)
    logger = make_logger(handler)
    handler.socket.close()

    logger.error("Lost")

    assert capsys.readouterr().err == "<3>test.journal: Lost\n"


def test_missing_socket_raises(tmp_path):
    with pytest.raises(OSError):
        JournalHandler("tstest", str(tmp_path / "missing"))


def test_stderr_is_journal(monkeypatch, tmp_path):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    assert not stderr_is_journal()

    with open(tmp_path / "stderr", "w") as stderr:
        stat = os.fstat(stderr.fileno())
        monkeypatch.setattr(journal.sys, "stderr", stderr)
        monkeypatch.setenv("JOURNAL_STREAM", f"{stat.st_dev}:{stat.st_ino}")
        assert stderr_is_journal()
        monkeypatch.setenv("JOURNAL_STREAM", f"{stat.st_dev}:{stat.st_ino + 1}")
        assert not stderr_is_journal()
        monkeypatch.setenv("JOURNAL_STREAM", "garbage")
        assert not stderr_is_journal()


def test_configure_logging_uses_journal_under_systemd(monkeypatch, journald, root_logger):
    path, receive = journald
    monkeypatch.setattr(journal, "stderr_is_journal", lambda: True)
    monkeypatch.setattr(JournalHandler.__init__, "__defaults__", (path,))

    configure_logging("tsupdated")
    logging.getLogger("tsupdate.daemon").info("Started")
    fields = receive()

    assert fields["MESSAGE"] == "tsupdate.daemon: Started"
    assert fields["SYSLOG_IDENTIFIER"] == "tsupdated"
    assert fields["PRIORITY"] == "6"


def test_configure_logging_on_terminal(monkeypatch, capsys, root_logger):
    monkeypatch.setattr(journal, "stderr_is_journal", lambda: False)

    configure_logging("tsupdate")
    logging.getLogger("tsupdate.x").warning("Careful")

    assert capsys.readouterr().err == "WARNING: Careful\n"


def test_configure_logging_without_journald(monkeypatch, capsys, tmp_path, root_logger):
    monkeypatch.setattr(journal, "stderr_is_journal", lambda: True)
    monkeypatch.setattr(JournalHandler.__init__, "__defaults__", (str(Path(tmp_path) / "missing"),))

    configure_logging("tsupdate")
    logging.getLogger("tsupdate.x").warning("Careful")

    assert capsys.readouterr().err == "WARNING: Careful\n"
