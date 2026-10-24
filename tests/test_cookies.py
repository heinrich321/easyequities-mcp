"""Tests for JSON cookie persistence (replaces the former pickle-based store)."""

import json
import os
import stat

import pytest
from requests.cookies import RequestsCookieJar, create_cookie

from easy_equities_mcp import server


class _FakeSession:
    def __init__(self) -> None:
        self.cookies = RequestsCookieJar()


class _FakeClient:
    def __init__(self) -> None:
        self.session = _FakeSession()


@pytest.fixture
def cookie_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "COOKIE_DIR", tmp_path / "cookies")
    return tmp_path / "cookies"


def test_round_trip_preserves_cookie_attributes(cookie_dir):
    client = _FakeClient()
    client.session.cookies.set_cookie(
        create_cookie(
            name="auth",
            value="abc123",
            domain="platform.easyequities.io",
            path="/",
            secure=True,
            expires=2_000_000_000,
            rest={"HttpOnly": None},
        )
    )
    client.session.cookies.set_cookie(
        create_cookie(name="pref", value="x", domain="platform.easyequities.io")
    )

    server._save_cookies(client, "personal")

    restored = _FakeClient()
    assert server._load_cookies(restored, "personal") is True

    jar = {c.name: c for c in restored.session.cookies}
    assert set(jar) == {"auth", "pref"}
    auth = jar["auth"]
    assert auth.value == "abc123"
    assert auth.domain == "platform.easyequities.io"
    assert auth.path == "/"
    assert auth.secure is True
    assert auth.expires == 2_000_000_000
    assert auth.has_nonstandard_attr("HttpOnly")


def test_cookie_file_is_json_and_private(cookie_dir):
    client = _FakeClient()
    client.session.cookies.set("k", "v", domain="example.com")
    server._save_cookies(client, "personal")

    path = server._cookie_file("personal")
    assert path.suffix == ".json"
    # Plain JSON — must parse without any custom loader.
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data[0]["name"] == "k"

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(cookie_dir).st_mode) == 0o700


@pytest.mark.parametrize(
    "payload",
    [
        '{"not": "a list"}',
        "[1, 2, 3]",
        '[{"value": "missing name"}]',
        '[{"name": "x", "rest": "not-a-dict"}]',
        "not json at all",
    ],
)
def test_tampered_or_malformed_file_fails_closed(cookie_dir, payload):
    cookie_dir.mkdir(parents=True)
    server._cookie_file("personal").write_text(payload)

    client = _FakeClient()
    assert server._load_cookies(client, "personal") is False
    assert len(client.session.cookies) == 0


def test_missing_file_returns_false(cookie_dir):
    assert server._load_cookies(_FakeClient(), "nope") is False


def test_no_pickle_import():
    """Guard against the pickle store being reintroduced."""
    import inspect
    import re

    assert not re.search(r"^\s*(import pickle|from pickle)", inspect.getsource(server), re.M)
