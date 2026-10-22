"""
Errors that tsupdate reports.

Helpers raise these instead of logging a failure and returning None, so that None only ever
means "nothing to do" (no update, no matching release). The caller decides how to report a
failure: the daemon's main loop logs it once per change of state, the CLI prints it and exits
with the error's exit code.

The message is the whole report: one capitalized sentence that says what failed and, where it
helps, what to do about it. No traceback is logged for these errors.

    UpdateError          an operation failed and needs attention          ERROR    exit 1
    ├── TransientError   expected in the field, goes away by itself       WARNING  exit 75
    └── ConfigurationError  needs a human to change something             ERROR    exit 78

The exit codes 75 and 78 are EX_TEMPFAIL and EX_CONFIG from sysexits.h.
"""

import re
import socket
import subprocess

import requests
import urllib3.exceptions


class UpdateError(Exception):
    """An operation failed, e.g. a corrupt archive, a failed rsync or a file that cannot be written."""

    exit_code = 1

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class TransientError(UpdateError):
    """
    A failure that is expected in the field and goes away by itself: no network, DNS failure,
    connection refused or reset, a timeout, GitHub 5xx, the GitHub rate limit, an interrupted
    download. The daemon retries at the next check.
    """

    exit_code = 75  # EX_TEMPFAIL


class ConfigurationError(UpdateError):
    """
    A failure that only a human can fix: repository or release not found, access denied, an
    update source that is not allowed, an invalid maintenance window.
    """

    exit_code = 78  # EX_CONFIG


# Connection objects in urllib3's messages: "<urllib3.connection.HTTPSConnection object at
# 0x7f...>: " (urllib3 < 2.5) or "HTTPSConnection(host='api.github.com', port=443): ". The
# address changes with every attempt, so the message would never repeat; and both are noise.
_CONNECTION_PREFIX = re.compile(r"^(?:<[^>]*>|\w*Connection\([^)]*\)): ")

# Network errors that are transient wherever they happen. OSError (which includes
# socket.gaierror and ConnectionError) is not listed: outside a network call it is a local
# problem, e.g. a full disk.
NETWORK_ERRORS = (
    requests.exceptions.RequestException,
    urllib3.exceptions.HTTPError,
    socket.timeout,
    TimeoutError,
    ConnectionError,
    socket.gaierror,
)


def describe_network_error(error: BaseException) -> str:
    """
    A short, stable description of a network error: its innermost cause without the
    connection object, e.g. "Failed to resolve 'api.github.com' ([Errno -3] ...)".

    requests wraps urllib3's MaxRetryError, which wraps the actual cause in `reason`.
    """
    cause: BaseException = error
    for _ in range(10):  # the chain is short; the limit only guards against a cycle
        reason = getattr(cause, "reason", None)
        if isinstance(reason, BaseException):
            cause = reason
        elif nested := [arg for arg in cause.args if isinstance(arg, BaseException)]:
            cause = nested[-1]  # e.g. ProxyError("Unable to connect to proxy", NewConnectionError(...))
        else:
            break
    text = _CONNECTION_PREFIX.sub("", str(cause)).strip()
    return text or type(cause).__name__


def network_error(error: BaseException, what: str) -> TransientError:
    """A TransientError for a network error while talking to `what` (e.g. "GitHub")."""
    if isinstance(error, (requests.exceptions.Timeout, urllib3.exceptions.TimeoutError, TimeoutError)):
        return TransientError(f"Timed out talking to {what}: {describe_network_error(error)}")
    return TransientError(f"Cannot reach {what}: {describe_network_error(error)}")


def timeout_error(error: subprocess.TimeoutExpired, what: str, hint: str = "") -> UpdateError:
    """An UpdateError (exit code 124) for a command that ran into its timeout.

    Not a TransientError: a sync that exceeds its limit once does so again at
    the next attempt, until someone raises the limit, so it is logged at ERROR.
    """
    limit = f"{int(error.timeout)}s" if error.timeout is not None else "unlimited"
    message = f"{what} timed out (limit was {limit})"
    return UpdateError(f"{message}; {hint}" if hint else message, exit_code=124)
