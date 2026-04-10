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
uv sync
uv run easy-equities-mcp
```

## Configuration

### Define your sessions

Each session is defined by three environment variables:

```dotenv
EE_SESSION_<NAME>_USERNAME=...
EE_SESSION_<NAME>_PASSWORD=...
EE_SESSION_<NAME>_PLATFORM=easyequities   # or "satrix" — defaults to easyequities
```

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

## Using it with Claude Desktop

1. Install the server (see above).
2. Put your session credentials in `~/.config/easy-equities-mcp/.env`, or
   define them in the `env` block below.
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

When cookies eventually expire:

```
You:    My personal session keeps failing.
Claude: [calls refresh_session("personal")]
```

No terminal required.

## CLI

`easy-equities-mcp-login` is a small helper for when the MCP client isn't
running yet — for example, if you want to seed cookies after a fresh OTP
challenge.

```bash
easy-equities-mcp-login --list           # list configured sessions
easy-equities-mcp-login personal         # log in and save cookies
```

## OTP / 2FA

The underlying library does a plain form POST and does **not** implement EE's
OTP flow. In practice EE only challenges on a new device/IP and then sets a
trust cookie, so the typical workaround is:

1. Log in once via the EE website in a browser on the same machine
   (satisfies the OTP challenge and marks the device as trusted).
2. Run `easy-equities-mcp-login <name>` or call the `refresh_session` MCP
   tool — the Python client inherits the trusted-device status and saves its
   own cookies.
3. From then on, the MCP server reuses those cookies on every restart.

If EE still challenges the CLI login after trusting the device in a browser,
please open an issue with the error message so OTP support can be added.

## Security notes

- The server stores session cookies under `~/.easy-equities-mcp/` with mode
  `0700`/`0600`. Anyone with access to that directory can impersonate the
  logged-in user.
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
