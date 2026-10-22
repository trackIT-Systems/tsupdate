"""Tests for the error classes and the helpers that build their messages."""

import subprocess

import pytest
import requests

from tsupdate.errors import (
    ConfigurationError,
    TransientError,
    UpdateError,
    describe_network_error,
    network_error,
    timeout_error,
)


class TestHierarchy:
    def test_exit_codes(self):
        assert UpdateError("x").exit_code == 1
        assert TransientError("x").exit_code == 75  # EX_TEMPFAIL
        assert ConfigurationError("x").exit_code == 78  # EX_CONFIG

    def test_exit_code_can_be_overridden(self):
        assert UpdateError("rsync failed", exit_code=23).exit_code == 23
        assert UpdateError("x").exit_code == 1  # the class default is untouched

    def test_subclasses(self):
        assert issubclass(TransientError, UpdateError)
        assert issubclass(ConfigurationError, UpdateError)
        assert not issubclass(TransientError, ConfigurationError)


class TestDescribeNetworkError:
    @pytest.mark.parametrize(
        "text",
        [
            # urllib3 < 2.5 (Debian trixie): the connection object with its address
            "<urllib3.connection.HTTPSConnection object at 0x7f12ab34cd56>: Failed to resolve 'api.github.com'",
            # urllib3 >= 2.5
            "HTTPSConnection(host='api.github.com', port=443): Failed to resolve 'api.github.com'",
        ],
    )
    def test_connection_object_is_stripped(self, text):
        assert describe_network_error(OSError(text)) == "Failed to resolve 'api.github.com'"

    def test_innermost_cause(self):
        inner = OSError("Connection refused")
        outer = requests.exceptions.ConnectionError(Exception(inner))
        assert describe_network_error(outer) == "Connection refused"

    def test_cause_after_a_message_argument(self):
        proxy = requests.exceptions.ProxyError("Unable to connect to proxy", OSError("Connection refused"))
        assert describe_network_error(proxy) == "Connection refused"

    def test_empty_message_names_the_type(self):
        assert describe_network_error(TimeoutError()) == "TimeoutError"

    def test_network_error_kinds(self):
        assert str(network_error(requests.exceptions.ConnectTimeout("t"), "GitHub")).startswith("Timed out talking")
        assert str(network_error(requests.exceptions.ConnectionError("c"), "GitHub")) == "Cannot reach GitHub: c"


def test_timeout_error():
    error = timeout_error(subprocess.TimeoutExpired("rsync", 3600), "Rsync", "increase rsync_timeout")
    assert isinstance(error, UpdateError) and not isinstance(error, TransientError)
    assert str(error) == "Rsync timed out (limit was 3600s); increase rsync_timeout"
    assert error.exit_code == 124
