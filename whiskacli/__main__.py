"""Entry point: python -m whiskacli"""
from __future__ import annotations

import argparse
import getpass
import random
import string
import sys
import time

from rich.console import Console

from .client import AdaptixClient
from .cli import AdaptixShell

console = Console()


def _random_username() -> str:
    return "cli-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def parse_args():
    parser = argparse.ArgumentParser(
        prog="whiska",
        description="Whiska - Command-Line Client for Adaptix C2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  whiska -H 192.168.1.10 -P 40056
  whiska -H c2.example.com -P 443 -e /endpoint -p 'secret'
  whiska -H 10.0.0.1 -P 40056 -u operator1 --pass secret
        """,
    )
    parser.add_argument("-H", "--host", required=True, help="Server host/IP")
    parser.add_argument("-P", "--port", type=int, default=40056, help="Server port (default: 40056)")
    parser.add_argument("-e", "--endpoint", default="", help="URL endpoint prefix (default: none)")
    parser.add_argument("-u", "--user", "--username", default="", help="Operator username (auto-generated if omitted)")
    parser.add_argument("-p", "--pass", "--password", dest="password", default="", help="Password (prompted if omitted)")
    parser.add_argument("--no-sync", action="store_true", help="Skip initial sync")
    parser.add_argument("--debug", action="store_true", help="Show raw WebSocket packets")
    return parser.parse_args()


def main():
    args = parse_args()

    username = args.user or _random_username()

    password = args.password
    if not password:
        try:
            password = getpass.getpass(f"Password for {username}@{args.host}: ")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[red]Cancelled[/red]")
            sys.exit(1)

    if not password:
        console.print("[red]Password is required[/red]")
        sys.exit(1)

    client = AdaptixClient(
        host=args.host,
        port=args.port,
        endpoint=args.endpoint,
        username=username,
        password=password,
    )

    # Debug mode: print raw packets
    if args.debug:
        import json
        def _debug_pkt(pkt):
            ptype = pkt.get("type", "?")
            console.print(f"[dim]WS PKT type=0x{ptype:02x}: {json.dumps(pkt)[:200]}[/dim]")
        client.on_raw_packet = _debug_pkt

    # Step 1: Login
    console.print(f"[dim]Connecting to {args.host}:{args.port} as {username}...[/dim]")
    try:
        client.login()
        console.print("[green]✓ Authenticated[/green]")
    except Exception as e:
        console.print(f"[red]✗ Login failed: {e}[/red]")
        sys.exit(1)

    # Step 2: WebSocket (with auto-reconnect)
    try:
        client.connect_ws(auto_reconnect=True)
        console.print("[green]✓ WebSocket connected (auto-reconnect enabled)[/green]")
    except Exception as e:
        console.print(f"[red]✗ WebSocket failed: {e}[/red]")
        sys.exit(1)

    # Step 3: Sync
    if not args.no_sync:
        try:
            client.trigger_sync()
            console.print("[dim]Syncing state...[/dim]", end="")
            if client.wait_sync(timeout=30):
                console.print(
                    f" [green]✓[/green] "
                    f"({len(client.agents)} agents, "
                    f"{len(client.listeners)} listeners, "
                    f"{len(client.credentials)} creds, "
                    f"{len(client.targets)} targets)"
                )
            else:
                console.print(" [yellow]⚠ Sync timed out (partial data)[/yellow]")
        except Exception as e:
            console.print(f"[yellow]⚠ Sync error: {e}[/yellow]")

    # Step 4: Interactive shell
    try:
        shell = AdaptixShell(client)
        shell.run()
    except Exception as e:
        console.print(f"[red]Shell error: {e}[/red]")
    finally:
        client.disconnect_ws()
        console.print("[dim]Disconnected.[/dim]")


if __name__ == "__main__":
    main()
