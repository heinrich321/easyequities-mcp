"""MCP server wrapping easy-equities-client.

Exposes Easy Equities and Satrix account data as MCP tools so that MCP clients
(e.g. Claude Desktop) can query accounts, holdings, transactions, valuations,
and historical prices on demand.

Credentials are loaded from environment variables, never from chat. Sessions
are discovered from env vars named EE_SESSION_<NAME>_{USERNAME,PASSWORD,PLATFORM}
and authenticated once at startup; cookies are persisted to disk so subsequent
restarts reuse them instead of re-authenticating.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlparse

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from requests.cookies import RequestsCookieJar, create_cookie

from easy_equities_client import constants
from easy_equities_client.clients import (
    EasyEquitiesClient,
    PlatformClient,
    SatrixClient,
)
from easy_equities_client.instruments.types import Period

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "easy-equities",
    instructions=(
        "Unofficial Easy Equities and Satrix client. "
        "Sessions are pre-configured via environment variables and authenticated "
        "automatically at startup. Use list_sessions to see available sessions, "
        "then pass the session name to other tools. If a tool reports that a "
        "session has expired, ask the user to run "
        "'easy-equities-mcp-browser-login <session>' in a terminal, then retry."
    ),
)

_sessions: dict[str, PlatformClient] = {}

# Serialises platform calls within this process: account switching is
# server-side state, and the keepalive thread shares the clients.
_lock = threading.RLock()

# Session lifetime bookkeeping (see _record_event).
_last_ok: dict[str, float] = {}
_expired_since: dict[str, float] = {}
_loaded_mtime: dict[str, float] = {}
_last_refresh_attempt: dict[str, float] = {}

# After a failed headless refresh, don't launch Chromium again for this long,
# so an expired session doesn't start a browser on every tool call.
HEADLESS_RETRY_COOLDOWN_SECONDS = 300
HEADLESS_REFRESH_TIMEOUT_SECONDS = 90

T = TypeVar("T")


class SessionExpired(RuntimeError):
    """EE answered with its sign-in page instead of data."""

# Cookies are stored per-session outside of the repo, in a private dir.
COOKIE_DIR = Path(
    os.environ.get("EE_MCP_COOKIE_DIR", Path.home() / ".easy-equities-mcp")
)

# Config file lookup locations, in the order they are loaded. Existing env
# vars always win (load_dotenv does not override by default), so values set in
# claude_desktop_config.json's `env` block take precedence over any file.
_CONFIG_CANDIDATES: tuple[Path, ...] = (
    Path.home() / ".config" / "easy-equities-mcp" / ".env",
    Path.cwd() / ".env",
)

# Sent on every platform request. Matches the Chromium that browser_login
# drives, so cookies are always presented with the UA they were issued to.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-ZA,en-GB;q=0.9,en;q=0.8",
}


# ---------------------------------------------------------------------------
# Config / session discovery
# ---------------------------------------------------------------------------


def _config_files() -> list[Path]:
    """Return the list of .env files that will be searched, in order."""
    explicit = os.environ.get("EE_MCP_ENV_FILE")
    files = [Path(explicit)] if explicit else []
    files.extend(_CONFIG_CANDIDATES)
    return files


def _load_config(override: bool = False) -> None:
    """Load config from the first existing .env file in the search path.

    With override=False (the default) existing env vars are preserved, so
    values injected by the MCP client always win.
    """
    for path in _config_files():
        if path.exists():
            load_dotenv(path, override=override)
            logger.debug("Loaded config from %s", path)
            return
    logger.debug("No .env file found; relying on environment variables")


def _discover_sessions() -> dict[str, tuple[str, str, str]]:
    """Return {session_key: (username, password, platform)} discovered from env.

    Sessions are defined by environment variables of the form:
        EE_SESSION_<NAME>_USERNAME
        EE_SESSION_<NAME>_PASSWORD
        EE_SESSION_<NAME>_PLATFORM   (optional; defaults to "easyequities")
    """
    pattern = re.compile(r"^EE_SESSION_(.+?)_USERNAME$")
    result: dict[str, tuple[str, str, str]] = {}
    for key in os.environ:
        m = pattern.match(key)
        if not m:
            continue
        name = m.group(1)
        username = os.environ.get(f"EE_SESSION_{name}_USERNAME", "")
        password = os.environ.get(f"EE_SESSION_{name}_PASSWORD", "")
        platform = os.environ.get(
            f"EE_SESSION_{name}_PLATFORM", "easyequities"
        ).lower()
        result[name.lower()] = (username, password, platform)
    return result


# ---------------------------------------------------------------------------
# Cookie persistence
# ---------------------------------------------------------------------------


def _cookie_file(session: str) -> Path:
    return COOKIE_DIR / f"{session}.cookies.json"


def _cookies_to_json(jar: RequestsCookieJar) -> list[dict]:
    """Serialise a cookie jar to plain JSON-safe dicts (no pickle)."""
    return [
        {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "secure": c.secure,
            "expires": c.expires,
            "rest": dict(c._rest),  # e.g. {"HttpOnly": None}
        }
        for c in jar
    ]


def _cookies_from_json(data: object) -> RequestsCookieJar:
    """Rebuild a cookie jar from the JSON produced by _cookies_to_json.

    Validates shape so a tampered file fails closed rather than loading garbage.
    """
    if not isinstance(data, list):
        raise ValueError("cookie file must contain a JSON list")
    jar = RequestsCookieJar()
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("malformed cookie entry")
        rest = item.get("rest") or {}
        if not isinstance(rest, dict):
            raise ValueError("malformed cookie entry")
        jar.set_cookie(
            create_cookie(
                name=item["name"],
                value=str(item.get("value", "")),
                domain=str(item.get("domain", "")),
                path=str(item.get("path", "/")),
                secure=bool(item.get("secure", False)),
                expires=item.get("expires"),
                rest=rest,
            )
        )
    return jar


def _save_cookies(client: PlatformClient, session: str) -> None:
    COOKIE_DIR.mkdir(mode=0o700, exist_ok=True, parents=True)
    path = _cookie_file(session)
    # Create with 0600 from the start rather than chmod after writing, so the
    # file is never briefly readable by others.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(_cookies_to_json(client.session.cookies), f)
    path.chmod(0o600)


def _load_cookies(client: PlatformClient, session: str) -> bool:
    path = _cookie_file(session)
    if not path.exists():
        return False
    try:
        mtime = path.stat().st_mtime
        with open(path, encoding="utf-8") as f:
            client.session.cookies.update(_cookies_from_json(json.load(f)))
        _loaded_mtime[session] = mtime
        return True
    except Exception:
        logger.exception("Failed to load cookies for %s", session)
        return False


def _cookies_valid(client: PlatformClient) -> bool:
    """Verify loaded cookies still authenticate by hitting the account overview page."""
    try:
        response = client.session.get(
            client._url(constants.PLATFORM_ACCOUNT_OVERVIEW_PATH),
            allow_redirects=False,
        )
        return response.status_code == 200 and "My Investments" in response.text
    except SessionExpired:
        return False
    except Exception:
        logger.exception("Cookie validation failed")
        return False


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _build_client(platform: str) -> PlatformClient:
    client = SatrixClient() if platform == "satrix" else EasyEquitiesClient()
    # EE's CloudFront rejects the default python-requests User-Agent with 403
    # on every path, so present as the same browser used by browser_login.
    client.session.headers.update(BROWSER_HEADERS)
    # Every library call goes through this session, so one hook turns EE's
    # sign-in responses into SessionExpired instead of empty or garbled data.
    client.session.hooks["response"].append(_raise_if_signed_out)
    return client


def _points_to_sign_in(url: str) -> bool:
    return constants.PLATFORM_SIGN_IN_PATH.lower() in url.lower() or (
        "identity.openeasy.io" in url.lower()
    )


def _is_sign_in_response(response) -> bool:
    """True if EE answered with its sign-in flow rather than the requested data.

    EE signals an expired session three ways: a redirect to /Account/SignIn
    (or EasyID), a 200 JSON string holding the sign-in URL (as
    /Menu/UpdateCurrency does), or a 200 page that is the login form itself.
    Requests to the sign-in page itself are not treated as expiry.
    """
    if _points_to_sign_in(urlparse(response.url).path):
        return False
    if response.is_redirect:
        return _points_to_sign_in(response.headers.get("Location", ""))
    if response.status_code != 200:
        return False
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        text = response.text.strip()
        return len(text) < 300 and text.startswith('"') and _points_to_sign_in(text)
    if "html" in content_type:
        text = response.text
        return 'type="password"' in text and (
            'name="Username"' in text or 'name="UserIdentifier"' in text
        )
    return False


def _raise_if_signed_out(response, *args, **kwargs):
    if _is_sign_in_response(response):
        raise SessionExpired(f"EE answered {urlparse(response.url).path} with sign-in")


def _password_login(
    session_key: str, username: str, password: str, platform: str
) -> None:
    """Do a fresh password login, save cookies, and store the session."""
    client = _build_client(platform)
    client.login(username, password)
    _sessions[session_key] = client
    _save_cookies(client, session_key)


def _auto_login() -> None:
    """Restore every configured session, preferring cached cookies."""
    _load_config()

    for session_key, (username, password, platform) in sorted(
        _discover_sessions().items()
    ):
        client = _build_client(platform)

        # Try cached cookies first — avoids triggering OTP on every startup.
        if _load_cookies(client, session_key) and _cookies_valid(client):
            _sessions[session_key] = client
            logger.info("Session '%s' restored from cookies", session_key)
            continue

        if not username or not password:
            logger.warning(
                "Session %s: no valid cookies and no password; skipping. "
                "Run 'easy-equities-mcp-browser-login %s' to log in.",
                session_key,
                session_key,
            )
            continue

        try:
            _password_login(session_key, username, password, platform)
            logger.info(
                "Session '%s' logged in with password and cookies saved",
                session_key,
            )
        except Exception:
            logger.exception(
                "Session %s: password login failed. Call the refresh_session "
                "MCP tool, or run 'easy-equities-mcp-login %s' interactively.",
                session_key,
                session_key,
            )


def _expired_message(session: str) -> str:
    return (
        f"Easy Equities session '{session}' has expired. Run "
        f"'easy-equities-mcp-browser-login {session}' in a terminal, then retry."
    )


def _require_session(session: str) -> PlatformClient:
    if session not in _sessions:
        if session in _discover_sessions():
            raise SessionExpired(_expired_message(session))
        available = sorted(_sessions.keys())
        if not available:
            raise RuntimeError(
                "No active sessions. Configure EE_SESSION_<NAME>_USERNAME in the "
                "environment or in a .env file, then run "
                "'easy-equities-mcp-browser-login <name>'."
            )
        raise RuntimeError(
            f"Session '{session}' not found. Active sessions: {available}"
        )
    return _sessions[session]


# ---------------------------------------------------------------------------
# Session expiry: recovery and lifetime measurement
# ---------------------------------------------------------------------------


def _utc(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _record_event(session: str, event: str, **fields) -> None:
    """Append a session lifecycle event to COOKIE_DIR/session-events.jsonl.

    Kept so EE's session lifetime can be measured: comparing 'expired' events'
    minutes_since_login and minutes_since_last_ok over a few days shows whether
    expiry is fixed or inactivity-based.
    """
    entry = {"at": _utc(time.time()), "session": session, "event": event, **fields}
    logger.info("Session event: %s", json.dumps(entry))
    try:
        COOKIE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = COOKIE_DIR / "session-events.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        logger.exception("Could not record session event")


def _note_ok(session: str) -> None:
    _last_ok[session] = time.time()
    _expired_since.pop(session, None)


def _note_expired(session: str) -> None:
    """Record the first expiry after a period of successful calls."""
    if session in _expired_since:
        return
    now = time.time()
    _expired_since[session] = now
    login_at = _loaded_mtime.get(session)
    last_ok = _last_ok.get(session)
    _record_event(
        session,
        "expired",
        cookies_saved_at=_utc(login_at),
        last_ok_at=_utc(last_ok),
        minutes_since_login=round((now - login_at) / 60, 1) if login_at else None,
        minutes_since_last_ok=round((now - last_ok) / 60, 1) if last_ok else None,
    )


def _reload_newer_cookies(session: str, platform: str) -> bool:
    """Adopt cookies saved since this process loaded them, if they authenticate.

    Lets every running server pick up a CLI browser login without a restart.
    """
    path = _cookie_file(session)
    if not path.exists() or path.stat().st_mtime <= _loaded_mtime.get(session, 0):
        return False
    client = _build_client(platform)
    if not (_load_cookies(client, session) and _cookies_valid(client)):
        return False
    _sessions[session] = client
    _record_event(session, "reloaded_from_disk")
    return True


def _headless_refresh(session: str, platform: str, force: bool = False) -> bool:
    """Try a silent browser refresh (see browser_login.refresh_cookies)."""
    now = time.monotonic()
    last = _last_refresh_attempt.get(session)
    if not force and last is not None and now - last < HEADLESS_RETRY_COOLDOWN_SECONDS:
        return False
    _last_refresh_attempt[session] = now
    try:
        from easy_equities_mcp import browser_login

        # Playwright's sync API refuses to run on a thread with a running
        # asyncio loop, which is where FastMCP may call tools.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(browser_login.refresh_cookies, session, platform, True).result(
                timeout=HEADLESS_REFRESH_TIMEOUT_SECONDS
            )
    except Exception as e:
        _record_event(session, "headless_refresh_failed", reason=f"{type(e).__name__}: {e}"[:200])
        return False
    client = _build_client(platform)
    if not _load_cookies(client, session):
        return False
    _sessions[session] = client
    _record_event(session, "headless_refreshed")
    return True


def _recover(session: str, platform: str, force: bool = False) -> bool:
    """Get a working client for session without the user, if possible."""
    return _reload_newer_cookies(session, platform) or _headless_refresh(
        session, platform, force=force
    )


def _platform_of(client: PlatformClient) -> str:
    return "satrix" if isinstance(client, SatrixClient) else "easyequities"


def _with_session(session: str, call: Callable[[PlatformClient], T]) -> T:
    """Run call against the session's client, recovering once from expiry.

    On SessionExpired: reload newer cookies from disk or try a headless
    refresh, then retry once; otherwise raise with the command that fixes it.
    An expired session is never reported as empty data.
    """
    with _lock:
        if session not in _sessions:
            platform = _discover_sessions().get(session, (None, None, None))[2]
            if platform is not None:
                _recover(session, platform)
        client = _require_session(session)
        try:
            result = call(client)
        except SessionExpired:
            _note_expired(session)
            if not _recover(session, _platform_of(client)):
                raise SessionExpired(_expired_message(session)) from None
            try:
                result = call(_sessions[session])
            except SessionExpired:
                raise SessionExpired(_expired_message(session)) from None
        _note_ok(session)
        return result


def _accounts(client: PlatformClient):
    """Return client.accounts with its selected-account cache cleared.

    EE keeps the selected account server-side per login session, and that
    session is shared by every MCP process using the same cookies, so the
    library's per-process current_account cache can be stale. Clearing it
    makes the library POST /Menu/UpdateCurrency (one request) before each
    account-scoped call.
    """
    client.accounts.current_account = None
    return client.accounts


def _keepalive_minutes() -> float:
    raw = os.environ.get("EE_MCP_KEEPALIVE_MINUTES", "0")
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("Ignoring EE_MCP_KEEPALIVE_MINUTES=%r (not a number)", raw)
        return 0.0


def _keepalive_loop(interval_seconds: float) -> None:
    """Touch every active session periodically (only if keepalive is enabled)."""
    while True:
        time.sleep(interval_seconds)
        for session in sorted(_sessions):
            try:
                _with_session(
                    session,
                    lambda c: c.session.get(
                        c._url(constants.PLATFORM_ACCOUNT_OVERVIEW_PATH),
                        allow_redirects=False,
                    ),
                )
            except Exception as e:
                logger.warning("Keepalive for %s failed: %s", session, e)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_sessions() -> str:
    """List all active logged-in sessions."""
    if not _sessions:
        return "No active sessions."
    return json.dumps(sorted(_sessions.keys()))


def _refresh(session_key: str, platform: str) -> str:
    """Check one session and recover it if expired. Returns a short status."""
    with _lock:
        client = _sessions.get(session_key)
        if client is not None and _cookies_valid(client):
            _note_ok(session_key)
            return "ok (still valid)"
        if client is not None:
            _note_expired(session_key)
        if _recover(session_key, platform, force=True):
            _note_ok(session_key)
            return "ok (refreshed)"
        return "expired"


@mcp.tool()
def refresh_session(session: str) -> str:
    """Check a session and refresh its cookies without a password, if possible.

    Picks up cookies saved by 'easy-equities-mcp-browser-login' since startup,
    or tries a silent headless browser refresh. If neither works, the user
    must run the browser login in a terminal. Credentials never pass through
    chat.

    Args:
        session: The session name, e.g. "personal".
    """
    _load_config(override=True)
    sessions = _discover_sessions()
    session_key = session.lower()

    if session_key not in sessions:
        return (
            f"Session '{session_key}' is not configured. "
            f"Available: {sorted(sessions.keys())}"
        )

    status = _refresh(session_key, sessions[session_key][2])
    if status == "expired":
        return _expired_message(session_key)
    return f"Session '{session_key}': {status}."


@mcp.tool()
def refresh_all_sessions() -> str:
    """Check and refresh every configured session. Returns a per-session report."""
    _load_config(override=True)
    sessions = _discover_sessions()
    if not sessions:
        return "No sessions configured."

    results = {
        session_key: _refresh(session_key, platform)
        for session_key, (_, _, platform) in sorted(sessions.items())
    }
    return json.dumps(results, indent=2)


@mcp.tool()
def list_accounts(session: str) -> str:
    """List all accounts for a session.

    Args:
        session: The session name (from list_sessions).
    """
    accounts = _with_session(session, lambda c: c.accounts.list())
    return json.dumps([dataclasses.asdict(a) for a in accounts], indent=2)


@mcp.tool()
def get_holdings(
    session: str, account_id: str, include_shares: bool = False
) -> str:
    """Get holdings (stocks/ETFs) for an account.

    Args:
        session: The session name (from list_sessions).
        account_id: The account ID (from list_accounts).
        include_shares: If true, fetch number of shares per holding (slower).
    """
    holdings = _with_session(
        session,
        lambda c: _accounts(c).holdings(account_id, include_shares=include_shares),
    )
    return json.dumps(holdings, indent=2)


@mcp.tool()
def get_valuations(session: str, account_id: str) -> str:
    """Get the valuation summary for an account.

    Args:
        session: The session name (from list_sessions).
        account_id: The account ID (from list_accounts).
    """
    valuations = _with_session(session, lambda c: _accounts(c).valuations(account_id))
    return json.dumps(valuations, indent=2)


@mcp.tool()
def get_transactions(session: str, account_id: str) -> str:
    """Get transactions for the last year for an account.

    Args:
        session: The session name (from list_sessions).
        account_id: The account ID (from list_accounts).
    """
    transactions = _with_session(session, lambda c: _accounts(c).transactions(account_id))
    return json.dumps(transactions, indent=2)


@mcp.tool()
def get_transactions_for_period(
    session: str, account_id: str, start_date: str, end_date: str
) -> str:
    """Get transactions for an account between two dates.

    Args:
        session: The session name (from list_sessions).
        account_id: The account ID (from list_accounts).
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    transactions = _with_session(
        session, lambda c: _accounts(c).transactions_for_period(account_id, start, end)
    )
    return json.dumps(transactions, indent=2)


@mcp.tool()
def get_historical_prices(
    session: str, contract_code: str, period: str = "OneYear"
) -> str:
    """Get historical prices for an instrument.

    Args:
        session: The session name (from list_sessions).
        contract_code: The instrument contract code, e.g. "EQU.ZA.SYGJP".
        period: One of "OneMonth", "ThreeMonths", "SixMonths", "OneYear", "Max".
    """
    p = Period(period)
    prices = _with_session(session, lambda c: c.instruments.historical_prices(contract_code, p))
    return json.dumps(prices, indent=2)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio."""
    _auto_login()
    minutes = _keepalive_minutes()
    if minutes:
        logger.info("Keepalive every %s minutes", minutes)
        threading.Thread(
            target=_keepalive_loop, args=(minutes * 60,), daemon=True, name="ee-keepalive"
        ).start()
    mcp.run(transport="stdio")


def login_cli() -> None:
    """CLI helper: do an interactive login and persist cookies to disk.

    Useful when Claude Desktop isn't running yet, or to seed cookies after the
    first OTP challenge.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Log in to Easy Equities / Satrix for a configured session and "
            "save cookies for the MCP server to reuse."
        )
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="Session name as defined in the environment (e.g. personal).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List configured sessions and exit.",
    )
    args = parser.parse_args()

    _load_config(override=True)
    sessions = _discover_sessions()

    if args.list:
        if not sessions:
            print("No sessions configured.")
            return
        print("Configured sessions:")
        for name in sorted(sessions):
            print(f"  - {name}")
        return

    if not args.session:
        parser.error("session name is required (or pass --list)")

    session_key = args.session.lower()
    if session_key not in sessions:
        print(f"Session '{session_key}' not configured.", file=sys.stderr)
        print(f"Available: {sorted(sessions.keys())}", file=sys.stderr)
        sys.exit(1)

    username, password, platform = sessions[session_key]
    if not username or not password:
        print(
            f"Session '{session_key}' is missing username or password.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Logging in to {platform} as session '{session_key}'...")
    try:
        _password_login(session_key, username, password, platform)
    except Exception as e:
        print(f"Login failed: {e}", file=sys.stderr)
        print(
            "\nIf EE challenged with OTP, log in once via the EE website in a "
            "browser on this machine (to trust the device), then rerun this "
            "command.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"Session '{session_key}' logged in. "
        f"Cookies saved to {_cookie_file(session_key)}"
    )
    print("Restart your MCP client (e.g. Claude Desktop) to pick up the session.")


if __name__ == "__main__":
    main()
