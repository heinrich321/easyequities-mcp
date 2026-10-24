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
from datetime import date
from pathlib import Path

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
        "then pass the session name to other tools. Call refresh_session if a "
        "tool call fails because cookies have expired."
    ),
)

_sessions: dict[str, PlatformClient] = {}

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
        with open(path, encoding="utf-8") as f:
            client.session.cookies.update(_cookies_from_json(json.load(f)))
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
    return client


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


def _require_session(session: str) -> PlatformClient:
    if session not in _sessions:
        available = sorted(_sessions.keys())
        if not available:
            raise RuntimeError(
                "No active sessions. Configure EE_SESSION_<NAME>_USERNAME and "
                "EE_SESSION_<NAME>_PASSWORD in the environment or in a .env file."
            )
        raise RuntimeError(
            f"Session '{session}' not found. Active sessions: {available}"
        )
    return _sessions[session]


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_sessions() -> str:
    """List all active logged-in sessions."""
    if not _sessions:
        return "No active sessions."
    return json.dumps(sorted(_sessions.keys()))


@mcp.tool()
def refresh_session(session: str) -> str:
    """Re-authenticate one session using credentials from the environment/.env.

    Call this from chat when a tool call fails because cookies have expired or
    to force a fresh login. Credentials are never passed through chat.

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

    username, password, platform = sessions[session_key]
    if not username or not password:
        return (
            f"Session '{session_key}' has no password configured. Run "
            f"'easy-equities-mcp-browser-login {session_key}' in a terminal, "
            "then restart the MCP server."
        )

    try:
        _password_login(session_key, username, password, platform)
    except Exception as e:
        return (
            f"Refresh failed for '{session_key}': {e}. "
            "If EE challenged with OTP, log in once via the EE website in a "
            "browser on this machine (to trust the device), then call "
            "refresh_session again."
        )
    return f"Session '{session_key}' refreshed and cookies saved."


@mcp.tool()
def refresh_all_sessions() -> str:
    """Re-authenticate every configured session. Returns a per-session report."""
    _load_config(override=True)
    sessions = _discover_sessions()
    if not sessions:
        return "No sessions configured."

    results: dict[str, str] = {}
    for session_key, (username, password, platform) in sorted(sessions.items()):
        if not username or not password:
            results[session_key] = "missing credentials"
            continue
        try:
            _password_login(session_key, username, password, platform)
            results[session_key] = "ok"
        except Exception as e:
            results[session_key] = f"failed: {e}"
    return json.dumps(results, indent=2)


@mcp.tool()
def list_accounts(session: str) -> str:
    """List all accounts for a session.

    Args:
        session: The session name (from list_sessions).
    """
    client = _require_session(session)
    accounts = client.accounts.list()
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
    client = _require_session(session)
    holdings = client.accounts.holdings(account_id, include_shares=include_shares)
    return json.dumps(holdings, indent=2)


@mcp.tool()
def get_valuations(session: str, account_id: str) -> str:
    """Get the valuation summary for an account.

    Args:
        session: The session name (from list_sessions).
        account_id: The account ID (from list_accounts).
    """
    client = _require_session(session)
    valuations = client.accounts.valuations(account_id)
    return json.dumps(valuations, indent=2)


@mcp.tool()
def get_transactions(session: str, account_id: str) -> str:
    """Get transactions for the last year for an account.

    Args:
        session: The session name (from list_sessions).
        account_id: The account ID (from list_accounts).
    """
    client = _require_session(session)
    transactions = client.accounts.transactions(account_id)
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
    client = _require_session(session)
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    transactions = client.accounts.transactions_for_period(account_id, start, end)
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
    client = _require_session(session)
    p = Period(period)
    prices = client.instruments.historical_prices(contract_code, p)
    return json.dumps(prices, indent=2)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio."""
    _auto_login()
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
