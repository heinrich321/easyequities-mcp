# easy-equities-mcp

An [MCP](https://modelcontextprotocol.io/) server for
[Easy Equities](https://www.easyequities.io/) and
[Satrix](https://satrix.co.za/) that lets any MCP-compatible client
(e.g. Claude Desktop, Claude Code, Continue) query your investment accounts
on demand.

Built on top of the unofficial
[`easy-equities-client`](https://github.com/delenamalan/easy-equities-client)
Python library.

> [!WARNING]
> Unofficial. Not affiliated with Easy Equities or Satrix. Intended for
> **personal use**. You are responsible for your credentials and for
> respecting Easy Equities' terms of service.

## Features

- **Multiple accounts** — configure any number of Easy Equities or Satrix
  logins side-by-side and reference them by name.
- **No credentials in chat** — usernames and passwords are loaded from
  environment variables or a `.env` file, never passed through the MCP
  client.
- **Cookie persistence** — successful logins are cached to disk so subsequent
  starts don't re-authenticate (and won't retrigger OTP).
- **Refresh from chat** — when cookies expire, the MCP client can refresh a
  session with a single tool call; no terminal required.
- **CLI fallback** — an `easy-equities-mcp-login` command is included for
  seeding cookies before the MCP client is running.

## Tools exposed

| Tool | Description |
|---|---|
| `list_sessions` | List active logged-in session names. |
| `refresh_session(session)` | Re-authenticate one session from config and save fresh cookies. |
| `refresh_all_sessions()` | Re-authenticate every configured session. |
| `list_accounts(session)` | List all accounts for a session (id, name, trading currency). |
| `get_holdings(session, account_id, include_shares=False)` | Holdings for an account. |
| `get_valuations(session, account_id)` | Valuation summary for an account. |
| `get_transactions(session, account_id)` | Transactions for the last year. |
| `get_transactions_for_period(session, account_id, start_date, end_date)` | Transactions between two `YYYY-MM-DD` dates. |
| `get_historical_prices(session, contract_code, period="OneYear")` | Historical prices. `period` is `OneMonth`, `ThreeMonths`, `SixMonths`, `OneYear`, or `Max`. |

## Installation

Requires Python 3.13+. The recommended runner is
[`uv`](https://docs.astral.sh/uv/).

### Option A — Run directly with `uvx` (zero install)

```bash
uvx easy-equities-mcp            # run the MCP server
uvx easy-equities-mcp-login --list
```

### Option B — `pip install`

```bash
pip install easy-equities-mcp
```

### Option C — From source

```bash
git clone https://github.com/heinrich321/easyequities-mcp.git
cd easyequities-mcp
uv sync --extra browser
uv run playwright install chromium   # for easy-equities-mcp-browser-login
uv run easy-equities-mcp
```

## Configuration

### Define your sessions

Each session is defined by three environment variables:

```dotenv
EE_SESSION_<NAME>_USERNAME=...
EE_SESSION_<NAME>_PASSWORD=               # legacy; leave empty with browser login
EE_SESSION_<NAME>_PLATFORM=easyequities   # or "satrix" — defaults to easyequities
```

`USERNAME` is what registers the session. With the browser login (see
[Logging in](#logging-in-easyid)) the password is never used and can be left
empty.

`<NAME>` is any label you choose (letters, digits, underscores). It becomes
the lowercased session key you pass to the MCP tools. You can define as many
sessions as you want.

Example:

```dotenv
EE_SESSION_PERSONAL_USERNAME=you@example.com
EE_SESSION_PERSONAL_PASSWORD=hunter2
EE_SESSION_PERSONAL_PLATFORM=easyequities

EE_SESSION_TFSA_USERNAME=you@example.com
EE_SESSION_TFSA_PASSWORD=hunter2
EE_SESSION_TFSA_PLATFORM=satrix
```

You can reference `personal` and `tfsa` from chat after that.

### Where to put the config

The server loads the first `.env` file it finds, in this order:

1. `$EE_MCP_ENV_FILE` (an explicit path)
2. `~/.config/easy-equities-mcp/.env` — **recommended for personal use**
3. `./.env` in the current working directory (handy for local dev)

Existing environment variables always win, so you can also skip the file and
put everything in your MCP client's config block — see below.

### Optional settings

| Variable | Default | Description |
|---|---|---|
| `EE_MCP_ENV_FILE` | *unset* | Explicit path to a `.env` file. |
| `EE_MCP_COOKIE_DIR` | `~/.easy-equities-mcp` | Where session cookies are cached. |
| `EE_MCP_KEEPALIVE_MINUTES` | `0` (off) | If set, touch each session every N minutes to stop an inactivity timeout. |

## Using it with Claude Desktop

1. Install the server (see above).
2. Put your session config in `~/.config/easy-equities-mcp/.env`, or
   define it in the `env` block below, then run
   `easy-equities-mcp-browser-login <name>` once to save session cookies.
3. Edit your Claude Desktop config:

   - **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

   Add the server. If you installed via `pip` / `uvx`:

   ```json
   {
     "mcpServers": {
       "easy-equities": {
         "command": "uvx",
         "args": ["easy-equities-mcp"]
       }
     }
   }
   ```

   Or, equivalently with credentials in the config (skip the `.env` file):

   ```json
   {
     "mcpServers": {
       "easy-equities": {
         "command": "uvx",
         "args": ["easy-equities-mcp"],
         "env": {
           "EE_SESSION_PERSONAL_USERNAME": "you@example.com",
           "EE_SESSION_PERSONAL_PASSWORD": "hunter2",
           "EE_SESSION_PERSONAL_PLATFORM": "easyequities"
         }
       }
     }
   }
   ```

   Or, from a local checkout:

   ```json
   {
     "mcpServers": {
       "easy-equities": {
         "command": "uv",
         "args": ["run", "--directory", "/absolute/path/to/easyequities-mcp", "easy-equities-mcp"]
       }
     }
   }
   ```

4. Restart Claude Desktop.

## Typical chat flow

```
You:    Show me my current holdings.
Claude: [calls list_sessions → list_accounts("personal") → get_holdings("personal", "12345")]
```

When the EE session expires, tools return an error that names the fix:
run `easy-equities-mcp-browser-login <name>` in a terminal. Running servers
pick up the new cookies on their next call, so no restart is needed. An
expired session is never reported as an empty account.

## CLI

`easy-equities-mcp-browser-login` is the primary way to log in. It first tries
a silent headless refresh with the saved browser profile, then opens a window.
It needs the
`browser` extra (`uv sync --extra browser && uv run playwright install chromium`).

```bash
easy-equities-mcp-browser-login personal                 # log in, save cookies
easy-equities-mcp-browser-login personal --platform satrix
```

`easy-equities-mcp-login` is the legacy password login. It is kept for
`--list` and in case EE restores the old endpoint.

```bash
easy-equities-mcp-login --list           # list configured sessions
easy-equities-mcp-login personal         # legacy password login
```

## Logging in (EasyID)

EE moved sign-in to **EasyID** (`identity.openeasy.io`, OpenID Connect).
`/Account/SignIn` on the platform now redirects there and no longer accepts
the form POST that `easy-equities-client` sends, so password login fails
(typically `403 Forbidden` from CloudFront). EE's CloudFront also rejects the
default `python-requests` User-Agent on every path, so the server sends a
browser User-Agent on all requests.

`easy-equities-mcp-browser-login <name>`:

1. Opens a Chromium window with a persistent profile at
   `~/.easy-equities-mcp/browser-profile-<name>/` (mode `0700`), on the
   platform's sign-in page.
2. Waits while you log in, including any OTP. Nothing you type is read; only
   cookies are taken from the browser.
3. Once `/AccountOverview` loads with those cookies, saves the platform's
   cookies (EasyID's own are dropped) to `<name>.cookies.json` and exits.
   Gives up after 5 minutes.

The MCP server then reuses those cookies on every restart until they expire.

### When the session expires

Every platform response is checked for EE's sign-in flow (a redirect to
`/Account/SignIn` or EasyID, the sign-in URL returned as JSON, or the login
form itself). On expiry the server, once per call:

1. adopts cookies saved by the browser login since it loaded its own, or
2. tries a headless browser refresh with the saved profile (at most every
   5 minutes per session), then retries the call.

If both fail, the tool returns an error naming the browser-login command.
`refresh_session` / `refresh_all_sessions` run the same check on demand.

EasyID currently keeps its login in a browser-session cookie and has no
"remember me" option, so the headless refresh only works while the profile
still holds a live EasyID login; in practice each new EE session needs the
browser login.

Expiry and refresh events are appended to
`~/.easy-equities-mcp/session-events.jsonl` (mode `0600`), with minutes since
login and since the last successful call, so you can tell whether EE expires
sessions after a fixed time or after inactivity. If it's inactivity, set
`EE_MCP_KEEPALIVE_MINUTES` (e.g. `10`).

The server switches account before every account-scoped call, because EE
keeps the selected account per login session and that session is shared by
every MCP process using the same cookies.

## Security notes

- The server stores session cookies as plain JSON (`<session>.cookies.json`)
  under `~/.easy-equities-mcp/` with mode `0700`/`0600`. (Earlier versions
  used `pickle`, which is unsafe to load from a writable location.) Anyone with access to that directory can impersonate the
  logged-in user.
- `browser-profile-<name>/` holds the Chromium profile used for login,
  including EasyID's cookies and trusted-device state. It is created `0700`;
  delete it to force a fresh login.
- `.env` files should be kept at `0600` and never committed. The repo's
  `.gitignore` excludes them by default.
- No credentials are ever sent through the MCP channel itself. Only metadata
  like session names.

## Development

```bash
git clone https://github.com/heinrich321/easyequities-mcp.git
cd easyequities-mcp
uv sync
uv run pytest
```

The server uses stdio transport. You can poke at it directly with JSON-RPC:

```bash
printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | uv run easy-equities-mcp
```

## Credits

- [`easy-equities-client`](https://github.com/delenamalan/easy-equities-client)
  by Dean Malan — the library this server wraps.
- [Model Context Protocol](https://modelcontextprotocol.io/) by Anthropic.

## License

[MIT](./LICENSE)
