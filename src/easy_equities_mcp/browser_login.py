"""Browser-assisted login for Easy Equities / Satrix.

EE moved sign-in to EasyID (identity.openeasy.io, OpenID Connect), so the
form POST in easy-equities-client no longer authenticates. This drives a real
Chromium with a persistent per-session profile, lets the user log in there
(including any OTP), then exports the platform cookies into the same JSON
store the server reads at startup.

refresh_cookies(headless=True) only succeeds if the profile still holds a
valid EasyID login; EasyID currently keeps that login in a browser-session
cookie with no "remember me" option, so in practice the headed CLI is needed
for each new session.

Only cookies are taken from the browser; form fields are never read.
"""

from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import urlparse

from easy_equities_mcp import server

LOGIN_TIMEOUT_SECONDS = 300
HEADLESS_TIMEOUT_SECONDS = 45
POLL_INTERVAL_SECONDS = 3


class BrowserUnavailable(RuntimeError):
    """Playwright or its Chromium is not installed or would not start."""


class LoginRequired(RuntimeError):
    """No valid session could be obtained without the user logging in."""


def _cookie_applies_to_host(cookie_domain: str, host: str) -> bool:
    """True if a cookie set for cookie_domain would be sent to host."""
    domain = cookie_domain.lstrip(".").lower()
    host = host.lower()
    return bool(domain) and (host == domain or host.endswith("." + domain))


def _playwright_cookies_to_json(cookies: list[dict], host: str) -> list[dict]:
    """Convert Playwright context.cookies() to server._cookies_to_json's schema.

    Keeps only cookies that apply to host, so EasyID's own cookies on
    identity.openeasy.io never reach the cookie store.
    """
    result = []
    for c in cookies:
        if not _cookie_applies_to_host(c.get("domain", ""), host):
            continue
        expires = c.get("expires")
        result.append(
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c["domain"],
                "path": c.get("path") or "/",
                "secure": bool(c.get("secure", False)),
                # Playwright reports session cookies as -1; the jar uses None.
                "expires": int(expires) if expires is not None and expires >= 0 else None,
                "rest": {"HttpOnly": None} if c.get("httpOnly") else {},
            }
        )
    return result


def _save_if_valid(cookies: list[dict], client, session_key: str) -> bool:
    """Load cookies into client; save them if they authenticate."""
    if not cookies:
        return False
    client.session.cookies.clear()
    client.session.cookies.update(server._cookies_from_json(cookies))
    if not server._cookies_valid(client):
        return False
    server._save_cookies(client, session_key)
    return True


def refresh_cookies(session_key: str, platform: str, headless: bool = True) -> None:
    """Obtain fresh platform cookies via Chromium and save them for session_key.

    Headless: succeeds only if the persistent profile is still logged in to
    EasyID; otherwise raises LoginRequired without waiting. Headed: waits up to
    LOGIN_TIMEOUT_SECONDS for the user to log in in the window.

    Raises BrowserUnavailable if Playwright/Chromium can't be used.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise BrowserUnavailable(
            "Playwright is not installed. Run: uv sync --extra browser && "
            "uv run playwright install chromium"
        ) from e

    client = server._build_client(platform)
    host = urlparse(client.base_url).hostname or ""
    profile_dir = server.COOKIE_DIR / f"browser-profile-{session_key}"
    server.COOKIE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile_dir.mkdir(mode=0o700, exist_ok=True)
    profile_dir.chmod(0o700)

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                str(profile_dir),
                headless=headless,
                channel="chromium",
                user_agent=server.BROWSER_USER_AGENT,
            )
        except PlaywrightError as e:
            raise BrowserUnavailable(f"Could not start Chromium: {e}") from e
        try:
            page = context.pages[0] if context.pages else context.new_page()
            if headless:
                page.goto(
                    client.base_url,
                    wait_until="networkidle",
                    timeout=HEADLESS_TIMEOUT_SECONDS * 1000,
                )
                cookies = _playwright_cookies_to_json(context.cookies(), host)
                if _save_if_valid(cookies, client, session_key):
                    return
                raise LoginRequired("EasyID needs an interactive login")

            page.goto(client.base_url)
            print("Log in in the browser window; I'll continue automatically.", flush=True)
            deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
            last_checked: frozenset = frozenset()
            while time.monotonic() < deadline:
                cookies = _playwright_cookies_to_json(context.cookies(), host)
                # Only hit the platform when the cookie set has changed.
                fingerprint = frozenset((c["name"], c["value"]) for c in cookies)
                if fingerprint != last_checked:
                    last_checked = fingerprint
                    if _save_if_valid(cookies, client, session_key):
                        return
                time.sleep(POLL_INTERVAL_SECONDS)
        except PlaywrightError as e:
            if headless:
                raise LoginRequired(f"Headless refresh failed: {e}") from e
            raise LoginRequired("Browser window closed before login completed.") from e
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass

    raise LoginRequired(
        f"Timed out after {LOGIN_TIMEOUT_SECONDS // 60} minutes waiting for login."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Log in to Easy Equities / Satrix in a browser and save the session "
            "cookies for the MCP server to reuse. Tries a silent headless "
            "refresh first, then opens a window."
        )
    )
    parser.add_argument(
        "session",
        help="Session name as defined in the environment (e.g. personal).",
    )
    parser.add_argument(
        "--platform",
        choices=("easyequities", "satrix"),
        help="Defaults to EE_SESSION_<NAME>_PLATFORM, else easyequities.",
    )
    args = parser.parse_args()

    server._load_config(override=True)
    sessions = server._discover_sessions()
    session_key = args.session.lower()
    if session_key not in sessions:
        print(
            f"Session '{session_key}' not configured. Set "
            f"EE_SESSION_{session_key.upper()}_USERNAME in your .env first.",
            file=sys.stderr,
        )
        print(f"Available: {sorted(sessions.keys())}", file=sys.stderr)
        sys.exit(1)
    platform = args.platform or sessions[session_key][2]

    try:
        try:
            refresh_cookies(session_key, platform, headless=True)
        except LoginRequired:
            print("No saved EasyID login; opening a browser window.", flush=True)
            refresh_cookies(session_key, platform, headless=False)
    except (BrowserUnavailable, LoginRequired) as e:
        print(f"{e} Nothing was saved.", file=sys.stderr)
        sys.exit(1)

    print(
        f"Session '{session_key}' logged in. "
        f"Cookies saved to {server._cookie_file(session_key)}"
    )
    print("Running MCP servers pick up the new cookies on their next call.")


if __name__ == "__main__":
    main()
