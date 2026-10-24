"""Tests for browser-login cookie export (no live browser or network)."""

import json

import pytest

from easy_equities_mcp import browser_login, server

HOST = "platform.easyequities.io"


def _pw_cookie(**overrides) -> dict:
    """A cookie dict shaped like Playwright's context.cookies() output."""
    cookie = {
        "name": "auth",
        "value": "abc123",
        "domain": HOST,
        "path": "/",
        "expires": 2_000_000_000.5,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }
    cookie.update(overrides)
    return cookie


def test_exported_cookies_round_trip_through_server_loader():
    exported = browser_login._playwright_cookies_to_json(
        [
            _pw_cookie(),
            _pw_cookie(name="session", value="s1", expires=-1, httpOnly=False, secure=False),
        ],
        HOST,
    )
    # Same JSON shape the server writes and reads from disk.
    jar = server._cookies_from_json(json.loads(json.dumps(exported)))
    by_name = {c.name: c for c in jar}

    auth = by_name["auth"]
    assert auth.value == "abc123"
    assert auth.domain == HOST
    assert auth.path == "/"
    assert auth.secure is True
    assert auth.expires == 2_000_000_000
    assert auth.has_nonstandard_attr("HttpOnly")

    session = by_name["session"]
    assert session.expires is None
    assert session.secure is False
    assert not session.has_nonstandard_attr("HttpOnly")


def test_export_matches_server_schema():
    exported = browser_login._playwright_cookies_to_json([_pw_cookie()], HOST)
    jar = server._cookies_from_json(exported)
    assert exported == server._cookies_to_json(jar)


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("platform.easyequities.io", True),
        (".easyequities.io", True),
        ("easyequities.io", True),
        ("identity.openeasy.io", False),
        (".openeasy.io", False),
        ("other.easyequities.io", False),
        ("evil-easyequities.io", False),
        ("easyequities.io.evil.com", False),
        ("", False),
    ],
)
def test_host_filter(domain, expected):
    assert browser_login._cookie_applies_to_host(domain, HOST) is expected


def test_export_drops_cookies_for_other_hosts():
    exported = browser_login._playwright_cookies_to_json(
        [
            _pw_cookie(name="keep"),
            _pw_cookie(name="parent", domain=".easyequities.io"),
            _pw_cookie(name="idp", domain="identity.openeasy.io"),
        ],
        HOST,
    )
    assert [c["name"] for c in exported] == ["keep", "parent"]


def test_build_client_sends_browser_user_agent():
    client = server._build_client("easyequities")
    assert client.session.headers["User-Agent"] == server.BROWSER_USER_AGENT


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COOKIE_DIR", tmp_path)
    monkeypatch.setattr(server, "_load_config", lambda override=False: None)
    monkeypatch.setenv("EE_SESSION_PERSONAL_USERNAME", "someone")
    monkeypatch.setattr("sys.argv", ["easy-equities-mcp-browser-login", "personal"])
    calls: list[bool] = []
    return calls


def test_cli_falls_back_to_headed_when_headless_needs_login(cli_env, monkeypatch):
    def fake_refresh(session_key, platform, headless=True):
        cli_env.append(headless)
        if headless:
            raise browser_login.LoginRequired("needs login")

    monkeypatch.setattr(browser_login, "refresh_cookies", fake_refresh)
    browser_login.main()
    assert cli_env == [True, False]


def test_cli_skips_window_when_headless_refresh_succeeds(cli_env, monkeypatch):
    monkeypatch.setattr(
        browser_login,
        "refresh_cookies",
        lambda session_key, platform, headless=True: cli_env.append(headless),
    )
    browser_login.main()
    assert cli_env == [True]


def test_cli_exits_non_zero_when_headed_login_fails(cli_env, monkeypatch):
    def fake_refresh(session_key, platform, headless=True):
        raise browser_login.LoginRequired("Timed out.")

    monkeypatch.setattr(browser_login, "refresh_cookies", fake_refresh)
    with pytest.raises(SystemExit) as exc:
        browser_login.main()
    assert exc.value.code == 1
