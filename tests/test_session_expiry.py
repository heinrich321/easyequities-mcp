"""Tests for session-expiry detection, recovery and account switching (no network)."""

import json
import time
from urllib.parse import urlparse

import pytest
from requests import PreparedRequest, Response
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from easy_equities_mcp import browser_login, server

BASE = "https://platform.easyequities.io"
SIGN_IN = f"{BASE}/Account/SignIn?ReturnUrl=%2FAccountOverview"
LOGIN_FORM = (
    '<form><input name="Username"><input type="password" name="Password">'
    '<input name="__RequestVerificationToken"></form>'
)


def _response(url, status=200, headers=None, body="", method="GET"):
    request = PreparedRequest()
    request.prepare(method=method, url=url)
    r = Response()
    r.status_code = status
    r.headers = CaseInsensitiveDict(headers or {})
    r._content = body.encode()
    r.encoding = "utf-8"
    r.url = url
    r.request = request
    return r


class FakePlatform(BaseAdapter):
    """Answers like EE: signed in only when the request carries auth=fresh."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    def send(self, request, **kwargs):
        path = urlparse(request.url).path
        self.calls.append((request.method, path))
        signed_in = "auth=fresh" in request.headers.get("Cookie", "")
        if request.method == "POST" and path == "/Menu/UpdateCurrency":
            body = '"ok"' if signed_in else f'"{BASE}/Account/SignIn"'
            return _response(request.url, 200, {"content-type": "application/json"}, body)
        if not signed_in:
            return _response(request.url, 302, {"Location": SIGN_IN})
        if path == "/AccountOverview":
            return _response(request.url, 200, {"content-type": "text/html"}, "My Investments")
        if path == "/AccountOverview/GetTrustAccountValuations":
            body = json.dumps(json.dumps({"TopSummary": {"AccountValue": 1}}))
            return _response(request.url, 200, {"content-type": "application/json"}, body)
        if path == "/AccountOverview/GetHoldingsView":
            return _response(request.url, 200, {"content-type": "text/html"}, "<html></html>")
        return _response(request.url, 404)

    def close(self):
        pass


@pytest.fixture
def platform(monkeypatch, tmp_path):
    fake = FakePlatform()
    original_build = server._build_client

    def build(platform_name):
        client = original_build(platform_name)
        client.session.mount("https://", fake)
        return client

    monkeypatch.setattr(server, "_build_client", build)
    monkeypatch.setattr(server, "COOKIE_DIR", tmp_path)
    for name in ("_sessions", "_last_ok", "_expired_since", "_loaded_mtime",
                 "_last_refresh_attempt"):
        monkeypatch.setattr(server, name, {})
    monkeypatch.setenv("EE_SESSION_PERSONAL_USERNAME", "someone")
    monkeypatch.delenv("EE_SESSION_PERSONAL_PASSWORD", raising=False)
    return fake


def _client(auth: str):
    client = server._build_client("easyequities")
    client.session.cookies.set("auth", auth, domain="platform.easyequities.io", path="/")
    return client


def _register(auth: str, loaded_at: float = 0.0):
    server._sessions["personal"] = _client(auth)
    server._loaded_mtime["personal"] = loaded_at


def _no_headless(monkeypatch):
    def fail(session_key, platform_name, headless=True):
        raise browser_login.LoginRequired("needs login")

    monkeypatch.setattr(browser_login, "refresh_cookies", fail)


# --- detection --------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expired"),
    [
        (_response(f"{BASE}/AccountOverview", 302, {"Location": SIGN_IN}), True),
        (
            _response(
                f"{BASE}/AccountOverview",
                302,
                {"Location": "https://identity.openeasy.io/connect/authorize?x=1"},
            ),
            True,
        ),
        (_response(f"{BASE}/Menu/X", 302, {"Location": "/Error/NotFound"}), False),
        (
            _response(
                f"{BASE}/Menu/UpdateCurrency",
                200,
                {"content-type": "application/json; charset=utf-8"},
                f'"{BASE}/Account/SignIn"',
                method="POST",
            ),
            True,
        ),
        (_response(f"{BASE}/x", 200, {"content-type": "application/json"}, '{"a": 1}'), False),
        (_response(f"{BASE}/x", 200, {"content-type": "text/html"}, LOGIN_FORM), True),
        (_response(f"{BASE}/x", 200, {"content-type": "text/html"}, "My Investments"), False),
        (_response(f"{BASE}/x", 403, {"content-type": "text/html"}, "ERROR"), False),
        (
            _response(
                f"{BASE}/Account/SignIn",
                302,
                {"Location": "https://identity.openeasy.io/Account/Login"},
            ),
            False,
        ),
    ],
    ids=[
        "redirect-sign-in", "redirect-easyid", "redirect-elsewhere", "json-sign-in-url",
        "json-data", "html-login-form", "html-data", "cloudfront-403", "sign-in-itself",
    ],
)
def test_sign_in_detection(response, expired):
    assert server._is_sign_in_response(response) is expired


# --- expiry never looks like empty data -------------------------------------


def test_expired_holdings_raise_instead_of_empty(platform, monkeypatch):
    _register("stale")
    _no_headless(monkeypatch)
    with pytest.raises(server.SessionExpired, match="easy-equities-mcp-browser-login personal"):
        server.get_holdings("personal", "420352")


def test_expired_valuations_raise_session_expired_not_json_error(platform, monkeypatch):
    _register("stale")
    _no_headless(monkeypatch)
    with pytest.raises(server.SessionExpired):
        server.get_valuations("personal", "420352")


def test_cookies_valid_is_false_not_raising_when_expired(platform):
    assert server._cookies_valid(_client("stale")) is False


# --- recovery -----------------------------------------------------------------


def test_recovers_from_cookies_saved_since_startup(platform, monkeypatch):
    _register("stale", loaded_at=1.0)
    _no_headless(monkeypatch)
    server._save_cookies(_client("fresh"), "personal")

    result = json.loads(server.get_valuations("personal", "420352"))
    assert result["TopSummary"]["AccountValue"] == 1


def test_recovers_via_headless_refresh(platform, monkeypatch):
    _register("stale", loaded_at=time.time() + 3600)  # disk copy is never "newer"
    headless_calls = []

    def refresh(session_key, platform_name, headless=True):
        headless_calls.append(headless)
        server._save_cookies(_client("fresh"), session_key)

    monkeypatch.setattr(browser_login, "refresh_cookies", refresh)
    assert json.loads(server.get_holdings("personal", "420352")) == []
    assert headless_calls == [True]


def test_headless_refresh_respects_cooldown(platform, monkeypatch):
    _register("stale", loaded_at=time.time() + 3600)
    headless_calls = []

    def fail(session_key, platform_name, headless=True):
        headless_calls.append(headless)
        raise browser_login.LoginRequired("needs login")

    monkeypatch.setattr(browser_login, "refresh_cookies", fail)
    for _ in range(3):
        with pytest.raises(server.SessionExpired):
            server.get_holdings("personal", "420352")
    assert headless_calls == [True]


def test_configured_but_inactive_session_reports_expiry(platform, monkeypatch):
    _no_headless(monkeypatch)
    with pytest.raises(server.SessionExpired, match="easy-equities-mcp-browser-login personal"):
        server.list_accounts("personal")


# --- lifetime measurement -----------------------------------------------------


def test_first_expiry_is_recorded_once(platform, monkeypatch):
    _register("fresh", loaded_at=1.0)
    server.get_valuations("personal", "420352")
    _no_headless(monkeypatch)
    server._sessions["personal"].session.cookies.set(
        "auth", "stale", domain="platform.easyequities.io", path="/"
    )
    for _ in range(2):
        with pytest.raises(server.SessionExpired):
            server.get_valuations("personal", "420352")

    events = [
        json.loads(line)
        for line in (server.COOKIE_DIR / "session-events.jsonl").read_text().splitlines()
    ]
    expired = [e for e in events if e["event"] == "expired"]
    assert len(expired) == 1
    assert expired[0]["last_ok_at"] is not None
    assert expired[0]["minutes_since_last_ok"] is not None
    assert expired[0]["cookies_saved_at"] is not None


# --- account switching --------------------------------------------------------


def test_switches_account_before_every_account_call(platform):
    _register("fresh")
    server.get_holdings("personal", "420352")
    server.get_holdings("personal", "420352")
    server.get_valuations("personal", "420352")
    switches = [c for c in platform.calls if c == ("POST", "/Menu/UpdateCurrency")]
    assert len(switches) == 3


# --- keepalive ----------------------------------------------------------------


@pytest.mark.parametrize(("raw", "minutes"), [(None, 0.0), ("10", 10.0), ("x", 0.0), ("-5", 0.0)])
def test_keepalive_minutes(monkeypatch, raw, minutes):
    if raw is None:
        monkeypatch.delenv("EE_MCP_KEEPALIVE_MINUTES", raising=False)
    else:
        monkeypatch.setenv("EE_MCP_KEEPALIVE_MINUTES", raw)
    assert server._keepalive_minutes() == minutes
