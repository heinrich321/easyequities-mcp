"""Browser-assisted login for Easy Equities / Satrix.

EE moved sign-in to EasyID (identity.openeasy.io, OpenID Connect), so the
form POST in easy-equities-client no longer authenticates. This opens a real
Chromium window, lets the user log in there (including any OTP), then exports
the platform cookies into the same JSON store the server reads at startup.

Only cookies are taken from the browser; form fields are never read.
"""

from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import urlparse

from easy_equities_mcp import server

LOGIN_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 3


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Log in to Easy Equities / Satrix in a browser window and save the "
            "session cookies for the MCP server to reuse."
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
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is not installed. Run:\n"
            "  uv sync --extra browser\n"
            "  uv run playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    client = server._build_client(platform)
    host = urlparse(client.base_url).hostname or ""
    profile_dir = server.COOKIE_DIR / f"browser-profile-{session_key}"
    server.COOKIE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile_dir.mkdir(mode=0o700, exist_ok=True)
    profile_dir.chmod(0o700)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            user_agent=server.BROWSER_USER_AGENT,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(client.base_url)
            print("Log in in the browser window; I'll continue automatically.", flush=True)

            deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
            last_checked: frozenset = frozenset()
            while time.monotonic() < deadline:
                cookies = _playwright_cookies_to_json(context.cookies(), host)
                # Only hit the platform when the cookie set has changed.
                fingerprint = frozenset((c["name"], c["value"]) for c in cookies)
                if cookies and fingerprint != last_checked:
                    last_checked = fingerprint
                    client.session.cookies.clear()
                    client.session.cookies.update(server._cookies_from_json(cookies))
                    if server._cookies_valid(client):
                        server._save_cookies(client, session_key)
                        print(
                            f"Session '{session_key}' logged in. "
                            f"Cookies saved to {server._cookie_file(session_key)}"
                        )
                        print("Restart your MCP client to pick up the session.")
                        return
                time.sleep(POLL_INTERVAL_SECONDS)
        except PlaywrightError:
            print("Browser window closed before login completed.", file=sys.stderr)
            sys.exit(1)
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass

    print(
        f"Timed out after {LOGIN_TIMEOUT_SECONDS // 60} minutes waiting for login. "
        "Nothing was saved.",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
