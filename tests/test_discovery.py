"""Tests for session/config discovery — no network, no real credentials."""

from __future__ import annotations

import os

import pytest

from easy_equities_mcp import server


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip any EE_SESSION_* vars from the environment before each test."""
    for key in list(os.environ):
        if key.startswith("EE_SESSION_") or key.startswith("EE_MCP_"):
            monkeypatch.delenv(key, raising=False)
    yield


def test_discover_sessions_empty():
    assert server._discover_sessions() == {}


def test_discover_sessions_basic(monkeypatch):
    monkeypatch.setenv("EE_SESSION_PERSONAL_USERNAME", "alice")
    monkeypatch.setenv("EE_SESSION_PERSONAL_PASSWORD", "hunter2")

    sessions = server._discover_sessions()

    assert sessions == {"personal": ("alice", "hunter2", "easyequities")}


def test_discover_sessions_multiple_and_platform(monkeypatch):
    monkeypatch.setenv("EE_SESSION_PERSONAL_USERNAME", "alice")
    monkeypatch.setenv("EE_SESSION_PERSONAL_PASSWORD", "pw1")
    monkeypatch.setenv("EE_SESSION_TFSA_USERNAME", "bob")
    monkeypatch.setenv("EE_SESSION_TFSA_PASSWORD", "pw2")
    monkeypatch.setenv("EE_SESSION_TFSA_PLATFORM", "satrix")

    sessions = server._discover_sessions()

    assert sessions == {
        "personal": ("alice", "pw1", "easyequities"),
        "tfsa": ("bob", "pw2", "satrix"),
    }


def test_discover_sessions_is_case_insensitive_in_key(monkeypatch):
    monkeypatch.setenv("EE_SESSION_MiXeD_USERNAME", "u")
    monkeypatch.setenv("EE_SESSION_MiXeD_PASSWORD", "p")

    sessions = server._discover_sessions()

    assert "mixed" in sessions


def test_require_session_raises_when_empty():
    server._sessions.clear()
    with pytest.raises(RuntimeError, match="No active sessions"):
        server._require_session("personal")


def test_require_session_raises_with_available_list():
    server._sessions.clear()
    server._sessions["other"] = object()  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="not found.*other"):
            server._require_session("missing")
    finally:
        server._sessions.clear()


def test_cookie_file_uses_cookie_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "COOKIE_DIR", tmp_path)
    assert server._cookie_file("personal") == tmp_path / "personal.cookies.json"
