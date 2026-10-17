"""Tests for the persistent update-attempt record that prevents boot loops."""

import pytest

from tsupdate import state


@pytest.fixture(autouse=True)
def state_file(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "sub" / "state.json")
    return state.STATE_FILE


def test_attempts_accumulate_per_update_and_version():
    assert state.record_attempt("u1", "1.0") == 1
    assert state.record_attempt("u1", "1.0") == 2
    assert state.record_attempt("u2", "1.0") == 1  # different update restarts the count
    assert state.record_attempt("u2", "1.1") == 1  # so does a different running version


def test_blocked_only_after_max_attempts():
    for _ in range(state.MAX_UPDATE_ATTEMPTS - 1):
        state.record_attempt("u", "1.0")
    assert not state.is_blocked("u", "1.0")
    state.record_attempt("u", "1.0")
    assert state.is_blocked("u", "1.0")
    assert not state.is_blocked("other", "1.0")
    assert not state.is_blocked("u", "1.1")


def test_block_is_immediate():
    state.block("u", "1.0")
    assert state.is_blocked("u", "1.0")


def test_stale_record_dropped_once_version_changes():
    state.block("u", "1.0")
    state.forget_if_version_changed("1.0")
    assert state.is_blocked("u", "1.0")
    state.forget_if_version_changed("1.1")
    assert not state.STATE_FILE.exists()


def test_unknown_version_never_clears_state():
    state.block("u", "1.0")
    state.forget_if_version_changed(None)
    assert state.is_blocked("u", "1.0")


@pytest.mark.parametrize("content", ["", "not json", "[1, 2]", '"str"'])
def test_corrupt_state_is_ignored(content):
    state.STATE_FILE.parent.mkdir(parents=True)
    state.STATE_FILE.write_text(content)
    assert not state.is_blocked("u", "1.0")
    assert state.record_attempt("u", "1.0") == 1


def test_discard_without_file_is_fine():
    state.discard_attempt()
    state.record_attempt("u", "1.0")
    state.discard_attempt()
    assert not state.STATE_FILE.exists()
