"""Core HTTP + WebSocket client for Adaptix C2 server."""
from __future__ import annotations

import base64
import json
import os
import ssl
import threading
import time
import uuid
from typing import Any, Callable, Optional

import requests
import websocket

from .models import (
    Agent, AgentReg, ChatMessage, Credential, Download, Listener,
    ListenerReg, Pivot, Screenshot, Target, Task, Tunnel,
    TYPE_SYNC_START, TYPE_SYNC_FINISH, TYPE_SYNC_BATCH, TYPE_SYNC_CATEGORY_BATCH,
    SP_TYPE_EVENT, TYPE_CHAT_MESSAGE, TYPE_SERVICE_DATA,
    TYPE_LISTENER_REG, TYPE_AGENT_REG, TYPE_SERVICE_REG,
    TYPE_LISTENER_START, TYPE_LISTENER_EDIT, TYPE_LISTENER_STOP,
    TYPE_AGENT_NEW, TYPE_AGENT_UPDATE, TYPE_AGENT_REMOVE, TYPE_AGENT_TICK,
    TYPE_AGENT_TASK_SYNC, TYPE_AGENT_TASK_UPDATE, TYPE_AGENT_TASK_SEND,
    TYPE_AGENT_TASK_REMOVE, TYPE_AGENT_TASK_HOOK,
    TYPE_DOWNLOAD_CREATE, TYPE_DOWNLOAD_UPDATE, TYPE_DOWNLOAD_DELETE, TYPE_DOWNLOAD_ACTUAL,
    TYPE_TUNNEL_CREATE, TYPE_TUNNEL_EDIT, TYPE_TUNNEL_DELETE,
    TYPE_SCREEN_CREATE, TYPE_SCREEN_UPDATE, TYPE_SCREEN_DELETE,
    TYPE_AGENT_CONSOLE_LOCAL, TYPE_AGENT_CONSOLE_ERROR, TYPE_AGENT_CONSOLE_OUT,
    TYPE_AGENT_CONSOLE_TASK_SYNC, TYPE_AGENT_CONSOLE_TASK_UPD,
    TYPE_PIVOT_CREATE, TYPE_PIVOT_DELETE,
    TYPE_CREDS_CREATE, TYPE_CREDS_EDIT, TYPE_CREDS_DELETE, TYPE_CREDS_SET_TAG,
    TYPE_TARGETS_CREATE, TYPE_TARGETS_EDIT, TYPE_TARGETS_DELETE, TYPE_TARGETS_SET_TAG,
    TYPE_AXSCRIPT_COMMANDS, TYPE_BROWSER_DISKS, TYPE_BROWSER_FILES,
    TYPE_BROWSER_STATUS, TYPE_BROWSER_PROCESS,
)

CLIENT_VERSION = "v1.2"
CLIENT_TYPE_CLI = 3

# Default subscription categories for full-featured CLI
DEFAULT_SUBSCRIPTIONS = [
    "extenders", "scripts", "listeners", "agents", "pivots",
    "console_history", "tasks_history", "tasks_manager",
    "chat_history", "chat_realtime",
    "downloads_history", "downloads_realtime",
    "screenshot_history", "screenshot_realtime",
    "credentials_history", "credentials_realtime",
    "targets_history", "targets_realtime",
    "notifications", "tunnels",
]


class AdaptixClient:
    """Adaptix C2 client handling REST API and WebSocket communication."""

    def __init__(self, host: str, port: int, endpoint: str, username: str, password: str):
        self.host = host
        self.port = port
        self.endpoint = endpoint.rstrip("/")
        self.username = username
        self.password = password

        self.base_url = f"https://{host}:{port}{self.endpoint}"
        self.access_token: str = ""
        self.refresh_token: str = ""

        self.ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_connected = threading.Event()
        self._ws_error: str = ""
        self._ws_lock = threading.Lock()
        self._ws_connect_failed = threading.Event()
        self._ws_subscriptions: list[str] = []
        self._auto_reconnect = False
        self._reconnecting = False
        self._reconnect_thread: Optional[threading.Thread] = None

        # Callback when connection drops/restores
        self.on_disconnect: Optional[Callable] = None
        self.on_reconnect: Optional[Callable] = None

        # State stores
        self.listener_regs: dict[str, ListenerReg] = {}
        self.agent_regs: dict[str, AgentReg] = {}
        self.listeners: dict[str, Listener] = {}
        self.agents: dict[str, Agent] = {}
        self.downloads: dict[str, Download] = {}
        self.credentials: dict[str, Credential] = {}
        self.targets: dict[str, Target] = {}
        self.tunnels: dict[str, Tunnel] = {}
        self.pivots: dict[str, Pivot] = {}
        self.screenshots: dict[str, Screenshot] = {}
        self.tasks: dict[str, Task] = {}
        self.chat_messages: list[ChatMessage] = []
        self.notifications: list[dict] = []
        self.services: dict[str, dict] = {}
        self.server_interfaces: list[str] = []
        self.axscript_commands: list[dict] = []

        # Sync tracking
        self._sync_done = threading.Event()
        self._sync_count = 0
        self._sync_received = 0

        # Console output callback: (agent_id, msg_type, message, clear_text)
        self.on_console_output: Optional[Callable] = None
        # Notification callback: (notification_dict)
        self.on_notification: Optional[Callable] = None
        # Chat callback: (ChatMessage)
        self.on_chat: Optional[Callable] = None
        # Agent event callback: (event_type, agent)
        self.on_agent_event: Optional[Callable] = None
        # Task update callback: (task)
        self.on_task_update: Optional[Callable] = None
        # Generic packet callback for debug: (packet_dict)
        self.on_raw_packet: Optional[Callable] = None

        # SSL: ignore certificate verification (self-signed certs)
        self._session = requests.Session()
        self._session.verify = False
        requests.packages.urllib3.disable_warnings(
            requests.packages.urllib3.exceptions.InsecureRequestWarning
        )

    # ─── Authentication ───────────────────────────────────────────────

    def login(self) -> bool:
        """Authenticate and obtain JWT tokens."""
        data = {"username": self.username, "password": self.password, "version": CLIENT_VERSION}
        try:
            resp = self._session.post(f"{self.base_url}/login", json=data, timeout=10)
            j = resp.json()
        except Exception as e:
            raise ConnectionError(f"Login failed: {e}")

        if "access_token" in j and "refresh_token" in j:
            self.access_token = j["access_token"]
            self.refresh_token = j["refresh_token"]
            return True
        raise ConnectionError(f"Login failed: {j}")

    def refresh_access_token(self) -> bool:
        """Refresh the access token using the refresh token."""
        try:
            resp = self._session.post(
                f"{self.base_url}/refresh",
                json={},
                headers=self._auth_header(self.refresh_token),
                timeout=10,
            )
            j = resp.json()
        except Exception:
            return False

        if "access_token" in j:
            self.access_token = j["access_token"]
            return True
        return False

    def _auth_header(self, token: str = "") -> dict:
        t = token or self.access_token
        return {"Authorization": f"Bearer {t}", "Content-Type": "application/json"}

    # ─── Generic REST helpers ─────────────────────────────────────────

    def _post(self, path: str, data: dict | None = None, timeout: int = 15) -> dict:
        """POST to API with auto token refresh."""
        url = f"{self.base_url}{path}"
        headers = self._auth_header()
        try:
            resp = self._session.post(url, json=data or {}, headers=headers, timeout=timeout)
        except Exception as e:
            raise ConnectionError(f"Request failed: {e}")

        if resp.status_code == 401 or b"invalid token" in resp.content.lower():
            if self.refresh_access_token():
                headers = self._auth_header()
                resp = self._session.post(url, json=data or {}, headers=headers, timeout=timeout)
            else:
                raise ConnectionError("Token expired and refresh failed")

        try:
            return resp.json()
        except Exception:
            return {"ok": False, "message": resp.text}

    def _get(self, path: str, params: dict | None = None, timeout: int = 15) -> Any:
        """GET from API with auto token refresh."""
        url = f"{self.base_url}{path}"
        headers = self._auth_header()
        try:
            resp = self._session.get(url, headers=headers, params=params, timeout=timeout)
        except Exception as e:
            raise ConnectionError(f"Request failed: {e}")

        if resp.status_code == 401:
            if self.refresh_access_token():
                headers = self._auth_header()
                resp = self._session.get(url, headers=headers, params=params, timeout=timeout)
            else:
                raise ConnectionError("Token expired and refresh failed")

        ct = resp.headers.get("Content-Type", "")
        if "application/json" in ct:
            try:
                return resp.json()
            except Exception:
                return resp.text
        if "image/" in ct:
            return resp.content
        return resp.text

    def _check_response(self, resp: dict, action: str = "") -> str:
        """Check standard {ok, message} response. Return message or raise."""
        if isinstance(resp, dict):
            if resp.get("ok"):
                return resp.get("message", "")
            msg = resp.get("message", str(resp))
            raise RuntimeError(f"{action} failed: {msg}" if action else msg)
        return str(resp)

    # ─── WebSocket Connection ─────────────────────────────────────────

    def connect_ws(self, subscriptions: list[str] | None = None, max_retries: int = 3,
                   auto_reconnect: bool = False) -> bool:
        """Establish WebSocket connection via OTP. Retries on 'already connected'."""
        subs = subscriptions or DEFAULT_SUBSCRIPTIONS
        self._ws_subscriptions = subs
        self._auto_reconnect = auto_reconnect

        for attempt in range(max_retries):
            otp_data = {
                "client_type": CLIENT_TYPE_CLI,
                "console_team_mode": True,
                "subscriptions": subs,
            }
            resp = self._post("/otp/generate", {"type": "connect", "data": otp_data})
            if not resp.get("ok"):
                raise ConnectionError(f"OTP generation failed: {resp.get('message')}")
            otp = resp["message"]

            ws_url = f"wss://{self.host}:{self.port}{self.endpoint}/connect?otp={otp}"

            self._ws_connected.clear()
            self._ws_connect_failed.clear()
            self._ws_error = ""

            sslopt = {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}

            self.ws = websocket.WebSocketApp(
                ws_url,
                on_open=self._on_ws_open,
                on_message=self._on_ws_message,
                on_error=self._on_ws_error,
                on_close=self._on_ws_close,
            )

            self._ws_thread = threading.Thread(
                target=self.ws.run_forever,
                kwargs={"sslopt": sslopt, "ping_interval": 30, "ping_timeout": 10},
                daemon=True,
            )
            self._ws_thread.start()

            # Wait for either open or error
            deadline = time.time() + 15
            while time.time() < deadline:
                if self._ws_connected.is_set() or self._ws_connect_failed.is_set():
                    break
                time.sleep(0.2)

            if self._ws_connected.is_set() and not self._ws_error:
                return True

            if "already connected" in self._ws_error.lower():
                if attempt < max_retries - 1:
                    time.sleep(3)
                    continue
            if self._ws_error:
                raise ConnectionError(f"WebSocket connect failed: {self._ws_error}")
            raise ConnectionError("WebSocket connect timed out")

        raise ConnectionError("WebSocket connect failed after retries: client already connected")

    def disconnect_ws(self):
        """Close WebSocket."""
        self._auto_reconnect = False
        self._reconnecting = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def _start_reconnect(self):
        """Background reconnect loop with exponential backoff."""
        if self._reconnecting:
            return
        self._reconnecting = True

        def _reconnect_loop():
            delays = [3, 5, 10, 15, 30, 30, 30, 60, 60]
            attempt = 0
            while self._auto_reconnect and self._reconnecting:
                delay = delays[min(attempt, len(delays) - 1)]
                time.sleep(delay)
                if not self._auto_reconnect:
                    break
                try:
                    self.login()
                    self.connect_ws(
                        subscriptions=self._ws_subscriptions,
                        max_retries=1,
                        auto_reconnect=True,
                    )
                    self.trigger_sync()
                    self.wait_sync(timeout=15)
                    self._reconnecting = False
                    if self.on_reconnect:
                        self.on_reconnect()
                    return
                except Exception:
                    attempt += 1
            self._reconnecting = False

        self._reconnect_thread = threading.Thread(target=_reconnect_loop, daemon=True)
        self._reconnect_thread.start()

    def trigger_sync(self) -> bool:
        """Trigger server-side sync push."""
        self._sync_done.clear()
        self._sync_count = 0
        self._sync_received = 0
        resp = self._post("/sync")
        return resp.get("ok", False)

    def wait_sync(self, timeout: float = 30.0) -> bool:
        """Wait for sync to complete."""
        return self._sync_done.wait(timeout=timeout)

    # ─── WS Callbacks ─────────────────────────────────────────────────

    def _on_ws_open(self, ws):
        self._ws_error = ""
        self._ws_connected.set()

    def _on_ws_error(self, ws, error):
        self._ws_error = str(error)
        self._ws_connect_failed.set()
        self._ws_connected.clear()

    def _on_ws_close(self, ws, close_status_code, close_msg):
        self._ws_connected.clear()
        if self._auto_reconnect and not self._reconnecting:
            if self.on_disconnect:
                self.on_disconnect()
            self._start_reconnect()

    def _on_ws_message(self, ws, message):
        """Handle incoming WebSocket message."""
        try:
            if isinstance(message, bytes):
                data = json.loads(message.decode("utf-8"))
            else:
                data = json.loads(message)
        except json.JSONDecodeError:
            return

        if self.on_raw_packet:
            try:
                self.on_raw_packet(data)
            except Exception:
                pass

        self._dispatch_packet(data)

    # ─── Packet Dispatch ──────────────────────────────────────────────

    def _dispatch_packet(self, pkt: dict):
        """Route a sync packet to the appropriate handler."""
        ptype = pkt.get("type")
        if ptype is None:
            return

        # Batch wrappers
        if ptype == TYPE_SYNC_BATCH:
            for sub in pkt.get("packets", []):
                self._dispatch_packet(sub)
            self._sync_received += 1
            self._check_sync_done()
            return

        if ptype == TYPE_SYNC_CATEGORY_BATCH:
            for sub in pkt.get("packets", []):
                self._dispatch_packet(sub)
            self._sync_received += 1
            self._check_sync_done()
            return

        if ptype == TYPE_SYNC_START:
            self._sync_count = pkt.get("count", 0)
            self._sync_received = 0
            self.server_interfaces = pkt.get("interfaces", [])
            return

        if ptype == TYPE_SYNC_FINISH:
            self._sync_done.set()
            return

        # Notifications
        if ptype == SP_TYPE_EVENT:
            notif = {"event_type": pkt.get("event_type"), "message": pkt.get("message", ""), "date": pkt.get("date", 0)}
            self.notifications.append(notif)
            if self.on_notification:
                try:
                    self.on_notification(notif)
                except Exception:
                    pass
            return

        # Listener registration
        if ptype == TYPE_LISTENER_REG:
            lr = ListenerReg(name=pkt.get("l_name", ""), protocol=pkt.get("l_protocol", ""),
                             l_type=pkt.get("l_type", ""), ax=pkt.get("ax", ""))
            self.listener_regs[lr.name] = lr
            return

        # Agent registration
        if ptype == TYPE_AGENT_REG:
            ar = AgentReg(agent=pkt.get("agent", ""), ax=pkt.get("ax", ""),
                          listeners=pkt.get("listeners", []),
                          multi_listeners=pkt.get("multi_listeners", False),
                          groups=pkt.get("groups", []))
            self.agent_regs[ar.agent] = ar
            return

        # Service registration
        if ptype == TYPE_SERVICE_REG:
            name = pkt.get("service", "")
            self.services[name] = {"name": name, "ax": pkt.get("ax", "")}
            return

        if ptype == TYPE_SERVICE_DATA:
            name = pkt.get("service", "")
            if name in self.services:
                self.services[name]["data"] = pkt.get("data", "")
            return

        # AxScript commands
        if ptype == TYPE_AXSCRIPT_COMMANDS:
            import json as _json
            groups = pkt.get("groups", [])
            for g in groups:
                cmds_raw = g.get("commands", "[]")
                if isinstance(cmds_raw, str):
                    cmds_raw = _json.loads(cmds_raw)
                self.axscript_commands.append({
                    "agent": g.get("agent", ""),
                    "listener": g.get("listener", ""),
                    "os": g.get("os", 0),
                    "groups": cmds_raw,
                })
            return

        # Listeners
        if ptype in (TYPE_LISTENER_START, TYPE_LISTENER_EDIT):
            li = Listener(
                name=pkt.get("l_name", ""), reg_name=pkt.get("l_reg_name", ""),
                protocol=pkt.get("l_protocol", ""), l_type=pkt.get("l_type", ""),
                bind_host=pkt.get("l_bind_host", ""), bind_port=pkt.get("l_bind_port", ""),
                agent_addr=pkt.get("l_agent_addr", ""),
                create_time=int(pkt.get("l_create_time", 0)),
                status=pkt.get("l_status", ""), data=pkt.get("l_data", ""),
            )
            self.listeners[li.name] = li
            return

        if ptype == TYPE_LISTENER_STOP:
            name = pkt.get("l_name", "")
            self.listeners.pop(name, None)
            return

        # Agents
        if ptype == TYPE_AGENT_NEW:
            agent = self._parse_agent(pkt)
            self.agents[agent.id] = agent
            if self.on_agent_event:
                try:
                    self.on_agent_event("new", agent)
                except Exception:
                    pass
            return

        if ptype == TYPE_AGENT_UPDATE:
            aid = pkt.get("a_id", "")
            if aid in self.agents:
                a = self.agents[aid]
                self._update_agent(a, pkt)
            return

        if ptype == TYPE_AGENT_TICK:
            ids = pkt.get("a_id", [])
            now = int(time.time())
            if isinstance(ids, list):
                for aid in ids:
                    if aid in self.agents:
                        self.agents[aid].last_tick = now
                        self.agents[aid].active = True
            return

        if ptype == TYPE_AGENT_REMOVE:
            aid = pkt.get("a_id", "")
            removed = self.agents.pop(aid, None)
            if removed and self.on_agent_event:
                try:
                    self.on_agent_event("remove", removed)
                except Exception:
                    pass
            return

        # Tasks
        if ptype in (TYPE_AGENT_TASK_SYNC, TYPE_AGENT_CONSOLE_TASK_SYNC):
            task = self._parse_task(pkt)
            self._store_task(task)
            return

        if ptype == TYPE_AGENT_TASK_UPDATE:
            task = self._parse_task(pkt)
            self._store_task(task)
            return

        if ptype == TYPE_AGENT_CONSOLE_TASK_UPD:
            task = self._parse_task(pkt)
            self._store_task(task)
            if self.on_task_update:
                try:
                    self.on_task_update(task)
                except Exception:
                    pass
            return

        if ptype == TYPE_AGENT_TASK_HOOK:
            task = self._parse_task(pkt)
            if self.on_task_update:
                try:
                    self.on_task_update(task)
                except Exception:
                    pass
            return

        if ptype == TYPE_AGENT_TASK_REMOVE:
            task_ids = pkt.get("a_task_id", [])
            if isinstance(task_ids, list):
                for tid in task_ids:
                    self.tasks.pop(tid, None)
            return

        # Console output
        if ptype in (TYPE_AGENT_CONSOLE_OUT, TYPE_AGENT_CONSOLE_ERROR, TYPE_AGENT_CONSOLE_LOCAL):
            aid = pkt.get("a_id", "")
            msg_type = pkt.get("a_msg_type", 0)
            message = pkt.get("a_message", "")
            if ptype == TYPE_AGENT_CONSOLE_ERROR:
                message = pkt.get("a_message", "")
                cmdline = pkt.get("a_cmdline", "")
                if cmdline:
                    message = f"[{cmdline}] {message}"
            clear_text = pkt.get("a_text", "")
            if ptype == TYPE_AGENT_CONSOLE_LOCAL:
                cmdline = pkt.get("a_cmdline", "")
                if cmdline and not message:
                    message = cmdline
            if self.on_console_output:
                try:
                    self.on_console_output(aid, ptype, msg_type, message, clear_text)
                except Exception:
                    pass
            return

        # Downloads
        if ptype == TYPE_DOWNLOAD_CREATE:
            dl = Download(
                file_id=pkt.get("d_file_id", ""), agent_id=pkt.get("d_agent_id", ""),
                agent_name=pkt.get("d_agent_name", ""), user=pkt.get("d_user", ""),
                computer=pkt.get("d_computer", ""), filename=pkt.get("d_file", ""),
                total_size=int(pkt.get("d_size", 0)), date=int(pkt.get("d_date", 0)),
                state=1,
            )
            self.downloads[dl.file_id] = dl
            return

        if ptype == TYPE_DOWNLOAD_UPDATE:
            fid = pkt.get("d_file_id", "")
            if fid in self.downloads:
                self.downloads[fid].recv_size = int(pkt.get("d_recv_size", 0))
                self.downloads[fid].state = int(pkt.get("d_state", 0))
            return

        if ptype == TYPE_DOWNLOAD_DELETE:
            for fid in pkt.get("d_files_id", []):
                self.downloads.pop(fid, None)
            return

        if ptype == TYPE_DOWNLOAD_ACTUAL:
            dl = Download(
                file_id=pkt.get("d_file_id", ""), agent_id=pkt.get("d_agent_id", ""),
                agent_name=pkt.get("d_agent_name", ""), user=pkt.get("d_user", ""),
                computer=pkt.get("d_computer", ""), filename=pkt.get("d_file", ""),
                total_size=int(pkt.get("d_size", 0)), date=int(pkt.get("d_date", 0)),
                recv_size=int(pkt.get("d_recv_size", 0)), state=int(pkt.get("d_state", 0)),
            )
            self.downloads[dl.file_id] = dl
            return

        # Tunnels
        if ptype == TYPE_TUNNEL_CREATE:
            tun = Tunnel(
                tunnel_id=pkt.get("p_tunnel_id", ""), agent_id=pkt.get("p_agent_id", ""),
                computer=pkt.get("p_computer", ""), username=pkt.get("p_username", ""),
                process=pkt.get("p_process", ""), tun_type=pkt.get("p_type", ""),
                info=pkt.get("p_info", ""), interface=pkt.get("p_interface", ""),
                port=pkt.get("p_port", ""), client=pkt.get("p_client", ""),
                fhost=pkt.get("p_fhost", ""), fport=pkt.get("p_fport", ""),
            )
            self.tunnels[tun.tunnel_id] = tun
            return

        if ptype == TYPE_TUNNEL_EDIT:
            tid = pkt.get("p_tunnel_id", "")
            if tid in self.tunnels:
                self.tunnels[tid].info = pkt.get("p_info", "")
            return

        if ptype == TYPE_TUNNEL_DELETE:
            tid = pkt.get("p_tunnel_id", "")
            self.tunnels.pop(tid, None)
            return

        # Screenshots
        if ptype == TYPE_SCREEN_CREATE:
            sc = Screenshot(
                screen_id=pkt.get("s_screen_id", ""), user=pkt.get("s_user", ""),
                computer=pkt.get("s_computer", ""), note=pkt.get("s_note", ""),
                date=int(pkt.get("s_date", 0)),
            )
            self.screenshots[sc.screen_id] = sc
            return

        if ptype == TYPE_SCREEN_UPDATE:
            sid = pkt.get("s_screen_id", "")
            if sid in self.screenshots:
                self.screenshots[sid].note = pkt.get("s_note", "")
            return

        if ptype == TYPE_SCREEN_DELETE:
            sid = pkt.get("s_screen_id", "")
            self.screenshots.pop(sid, None)
            return

        # Credentials
        if ptype == TYPE_CREDS_CREATE:
            for c in pkt.get("c_creds", []):
                cred = Credential(
                    cred_id=c.get("c_creds_id", ""), username=c.get("c_username", ""),
                    password=c.get("c_password", ""), realm=c.get("c_realm", ""),
                    cred_type=c.get("c_type", ""), tag=c.get("c_tag", ""),
                    date=int(c.get("c_date", 0)), storage=c.get("c_storage", ""),
                    agent_id=c.get("c_agent_id", ""), host=c.get("c_host", ""),
                )
                self.credentials[cred.cred_id] = cred
            return

        if ptype == TYPE_CREDS_EDIT:
            cid = pkt.get("c_creds_id", "")
            if cid in self.credentials:
                c = self.credentials[cid]
                c.username = pkt.get("c_username", c.username)
                c.password = pkt.get("c_password", c.password)
                c.realm = pkt.get("c_realm", c.realm)
                c.cred_type = pkt.get("c_type", c.cred_type)
                c.tag = pkt.get("c_tag", c.tag)
                c.storage = pkt.get("c_storage", c.storage)
                c.host = pkt.get("c_host", c.host)
            return

        if ptype == TYPE_CREDS_DELETE:
            for cid in pkt.get("c_creds_id", []):
                self.credentials.pop(cid, None)
            return

        if ptype == TYPE_CREDS_SET_TAG:
            tag = pkt.get("c_tag", "")
            for cid in pkt.get("c_creds_id", []):
                if cid in self.credentials:
                    self.credentials[cid].tag = tag
            return

        # Targets
        if ptype == TYPE_TARGETS_CREATE:
            for t in pkt.get("t_targets", []):
                tgt = Target(
                    target_id=t.get("t_target_id", ""), computer=t.get("t_computer", ""),
                    domain=t.get("t_domain", ""), address=t.get("t_address", ""),
                    os=int(t.get("t_os", 0)), os_desc=t.get("t_os_desk", ""),
                    tag=t.get("t_tag", ""), info=t.get("t_info", ""),
                    date=int(t.get("t_date", 0)), alive=t.get("t_alive", False),
                    agents=t.get("t_agents", []),
                )
                self.targets[tgt.target_id] = tgt
            return

        if ptype == TYPE_TARGETS_EDIT:
            tid = pkt.get("t_target_id", "")
            if tid in self.targets:
                t = self.targets[tid]
                t.computer = pkt.get("t_computer", t.computer)
                t.domain = pkt.get("t_domain", t.domain)
                t.address = pkt.get("t_address", t.address)
                t.os = int(pkt.get("t_os", t.os))
                t.os_desc = pkt.get("t_os_desk", t.os_desc)
                t.tag = pkt.get("t_tag", t.tag)
                t.info = pkt.get("t_info", t.info)
                t.alive = pkt.get("t_alive", t.alive)
                t.agents = pkt.get("t_agents", t.agents)
            return

        if ptype == TYPE_TARGETS_DELETE:
            for tid in pkt.get("t_target_id", []):
                self.targets.pop(tid, None)
            return

        if ptype == TYPE_TARGETS_SET_TAG:
            tag = pkt.get("t_tag", "")
            for tid in pkt.get("t_targets_id", []):
                if tid in self.targets:
                    self.targets[tid].tag = tag
            return

        # Pivots
        if ptype == TYPE_PIVOT_CREATE:
            pv = Pivot(
                pivot_id=pkt.get("p_pivot_id", ""), pivot_name=pkt.get("p_pivot_name", ""),
                parent_agent_id=pkt.get("p_parent_agent_id", ""),
                child_agent_id=pkt.get("p_child_agent_id", ""),
            )
            self.pivots[pv.pivot_id] = pv
            return

        if ptype == TYPE_PIVOT_DELETE:
            pid = pkt.get("p_pivot_id", "")
            self.pivots.pop(pid, None)
            return

        # Chat
        if ptype == TYPE_CHAT_MESSAGE:
            msg = ChatMessage(
                username=pkt.get("c_username", ""),
                message=pkt.get("c_message", ""),
                date=int(pkt.get("c_date", 0)),
            )
            self.chat_messages.append(msg)
            if self.on_chat:
                try:
                    self.on_chat(msg)
                except Exception:
                    pass
            return

    def _check_sync_done(self):
        if self._sync_count > 0 and self._sync_received >= self._sync_count:
            self._sync_done.set()

    # ─── Parsing helpers ──────────────────────────────────────────────

    @staticmethod
    def _parse_agent(pkt: dict) -> Agent:
        return Agent(
            id=pkt.get("a_id", ""), name=pkt.get("a_name", ""),
            listener=pkt.get("a_listener", ""), async_agent=pkt.get("a_async", False),
            external_ip=pkt.get("a_external_ip", ""), internal_ip=pkt.get("a_internal_ip", ""),
            gmt_offset=int(pkt.get("a_gmt_offset", 0)),
            working_time=int(pkt.get("a_workingtime", 0)),
            kill_date=int(pkt.get("a_killdate", 0)),
            sleep=int(pkt.get("a_sleep", 0)), jitter=int(pkt.get("a_jitter", 0)),
            acp=int(pkt.get("a_acp", 0)), oemcp=int(pkt.get("a_oemcp", 0)),
            pid=pkt.get("a_pid", ""), tid=pkt.get("a_tid", ""),
            arch=pkt.get("a_arch", ""), elevated=pkt.get("a_elevated", False),
            process=pkt.get("a_process", ""), os=int(pkt.get("a_os", 0)),
            os_desc=pkt.get("a_os_desc", ""), domain=pkt.get("a_domain", ""),
            computer=pkt.get("a_computer", ""), username=pkt.get("a_username", ""),
            impersonated=pkt.get("a_impersonated", ""),
            create_time=int(pkt.get("a_create_time", 0)),
            last_tick=int(pkt.get("a_last_tick", 0)),
            tags=pkt.get("a_tags", ""), mark=pkt.get("a_mark", ""),
            color=pkt.get("a_color", ""),
        )

    @staticmethod
    def _update_agent(agent: Agent, pkt: dict):
        for field_name, key in [
            ("sleep", "a_sleep"), ("jitter", "a_jitter"), ("working_time", "a_workingtime"),
            ("kill_date", "a_killdate"), ("impersonated", "a_impersonated"),
            ("tags", "a_tags"), ("mark", "a_mark"), ("color", "a_color"),
            ("internal_ip", "a_internal_ip"), ("external_ip", "a_external_ip"),
            ("gmt_offset", "a_gmt_offset"), ("acp", "a_acp"), ("oemcp", "a_oemcp"),
            ("pid", "a_pid"), ("tid", "a_tid"), ("arch", "a_arch"),
            ("elevated", "a_elevated"), ("process", "a_process"),
            ("os", "a_os"), ("os_desc", "a_os_desc"),
            ("domain", "a_domain"), ("computer", "a_computer"),
            ("username", "a_username"), ("listener", "a_listener"),
        ]:
            if key in pkt and pkt[key] is not None:
                setattr(agent, field_name, pkt[key])

    @staticmethod
    def _parse_task(pkt: dict) -> Task:
        return Task(
            task_id=pkt.get("a_task_id", ""), task_type=int(pkt.get("a_task_type", 0)),
            agent_id=pkt.get("a_id", ""), client=pkt.get("a_client", ""),
            user=pkt.get("a_user", ""), computer=pkt.get("a_computer", ""),
            cmdline=pkt.get("a_cmdline", ""),
            start_time=int(pkt.get("a_start_time", 0)),
            finish_time=int(pkt.get("a_finish_time", 0)),
            message_type=int(pkt.get("a_msg_type", 0)),
            message=pkt.get("a_message", ""), text=pkt.get("a_text", ""),
            completed=pkt.get("a_completed", False),
        )

    def _store_task(self, task: Task):
        existing = self.tasks.get(task.task_id)
        if existing:
            if task.message:
                existing.message = task.message
            if task.text:
                existing.text = task.text
            if task.finish_time:
                existing.finish_time = task.finish_time
            if task.completed:
                existing.completed = True
        else:
            self.tasks[task.task_id] = task

    # ─── Listener Management ──────────────────────────────────────────

    def listener_list(self) -> list:
        return self._get("/listener/list")

    def listener_start(self, name: str, config_type: str, config: str) -> str:
        resp = self._post("/listener/create", {"name": name, "type": config_type, "config": config})
        return self._check_response(resp, "Listener start")

    def listener_edit(self, name: str, config_type: str, config: str) -> str:
        resp = self._post("/listener/edit", {"name": name, "type": config_type, "config": config})
        return self._check_response(resp, "Listener edit")

    def listener_stop(self, name: str, config_type: str) -> str:
        resp = self._post("/listener/stop", {"name": name, "type": config_type})
        return self._check_response(resp, "Listener stop")

    def listener_pause(self, name: str, config_type: str) -> str:
        resp = self._post("/listener/pause", {"name": name, "type": config_type})
        return self._check_response(resp, "Listener pause")

    def listener_resume(self, name: str, config_type: str) -> str:
        resp = self._post("/listener/resume", {"name": name, "type": config_type})
        return self._check_response(resp, "Listener resume")

    # ─── Agent Management ─────────────────────────────────────────────

    def agent_list(self) -> list:
        return self._get("/agent/list")

    def agent_remove(self, agent_ids: list[str]) -> str:
        resp = self._post("/agent/remove", {"agent_id_array": agent_ids})
        return self._check_response(resp, "Agent remove")

    def agent_set_tag(self, agent_ids: list[str], tag: str) -> str:
        resp = self._post("/agent/set/tag", {"agent_id_array": agent_ids, "tag": tag})
        return self._check_response(resp, "Agent set tag")

    def agent_set_mark(self, agent_ids: list[str], mark: str) -> str:
        resp = self._post("/agent/set/mark", {"agent_id_array": agent_ids, "mark": mark})
        return self._check_response(resp, "Agent set mark")

    def agent_set_color(self, agent_ids: list[str], bg: str = "", fg: str = "", reset: bool = False) -> str:
        resp = self._post("/agent/set/color", {"agent_id_array": agent_ids, "bc": bg, "fc": fg, "reset": reset})
        return self._check_response(resp, "Agent set color")

    def agent_command_raw(self, agent_id: str, cmdline: str) -> str:
        """Send raw command to agent (server parses via AxScript)."""
        resp = self._post("/agent/command/raw", {"id": agent_id, "cmdline": cmdline})
        return self._check_response(resp, "Command")

    def agent_command_execute(self, agent_id: str, cmdline: str, data: dict | None = None,
                              hook_id: str = "", handler_id: str = "") -> str:
        """Send parsed command to agent."""
        payload = {
            "id": agent_id, "ui": False, "cmdline": cmdline,
            "data": json.dumps(data or {}),
            "ax_hook_id": hook_id, "ax_handler_id": handler_id,
            "wait_answer": False,
        }
        resp = self._post("/agent/command/execute", payload)
        return self._check_response(resp, "Command execute")

    def agent_update_data(self, agent_id: str, **fields) -> str:
        payload = {"agent_id": agent_id, **fields}
        resp = self._post("/agent/update/data", payload)
        return self._check_response(resp, "Agent update")

    def agent_generate(self, agent_name: str, config: str, listener_names: list[str]) -> tuple[str, bytes]:
        """Generate agent binary. Returns (filename, content)."""
        resp = self._post("/agent/generate",
                          {"agent": agent_name, "config": config, "listener_name": listener_names},
                          timeout=120)
        msg = self._check_response(resp, "Agent generate")
        parts = msg.split(":", 1)
        if len(parts) != 2:
            raise RuntimeError(f"Unexpected generate response: {msg[:100]}")
        filename = base64.b64decode(parts[0]).decode("utf-8")
        content = base64.b64decode(parts[1])
        return filename, content

    def agent_console_remove(self, agent_ids: list[str]) -> str:
        resp = self._post("/agent/console/remove", {"agent_id_array": agent_ids})
        return self._check_response(resp, "Console remove")

    # ─── Task Management ──────────────────────────────────────────────

    def task_list(self, agent_id: str, limit: int = 200, offset: int = 0) -> list:
        return self._get("/agent/task/list", params={"agent_id": agent_id, "limit": limit, "offset": offset})

    def task_cancel(self, agent_id: str, task_ids: list[str]) -> str:
        resp = self._post("/agent/task/cancel", {"agent_id": agent_id, "tasks_array": task_ids})
        return self._check_response(resp, "Task cancel")

    def task_delete(self, agent_id: str, task_ids: list[str]) -> str:
        resp = self._post("/agent/task/delete", {"agent_id": agent_id, "tasks_array": task_ids})
        return self._check_response(resp, "Task delete")

    # ─── Downloads ────────────────────────────────────────────────────

    def download_list(self) -> list:
        return self._get("/download/list")

    def download_sync(self, file_id: str) -> tuple[str, bytes]:
        """Download a file. Returns (filename, content)."""
        resp = self._post("/download/sync", {"file_id": file_id})
        if isinstance(resp, dict) and resp.get("ok"):
            filename = resp.get("filename", "unknown")
            content = base64.b64decode(resp.get("content", ""))
            return filename, content
        raise RuntimeError(f"Download failed: {resp}")

    def download_delete(self, file_ids: list[str]) -> str:
        resp = self._post("/download/delete", {"file_id_array": file_ids})
        return self._check_response(resp, "Download delete")

    # ─── Screenshots ──────────────────────────────────────────────────

    def screenshot_list(self) -> list:
        return self._get("/screen/list")

    def screenshot_get_image(self, screen_id: str) -> bytes:
        return self._get("/screen/image", params={"screen_id": screen_id})

    def screenshot_set_note(self, screen_ids: list[str], note: str) -> str:
        resp = self._post("/screen/setnote", {"screen_id_array": screen_ids, "note": note})
        return self._check_response(resp, "Screenshot note")

    def screenshot_remove(self, screen_ids: list[str]) -> str:
        resp = self._post("/screen/remove", {"screen_id_array": screen_ids})
        return self._check_response(resp, "Screenshot remove")

    # ─── Credentials ──────────────────────────────────────────────────

    def creds_list(self) -> list:
        return self._get("/creds/list")

    def creds_add(self, creds: list[dict]) -> str:
        resp = self._post("/creds/add", {"creds": creds})
        return self._check_response(resp, "Creds add")

    def creds_edit(self, cred_id: str, **fields) -> str:
        payload = {"cred_id": cred_id, **fields}
        resp = self._post("/creds/edit", payload)
        return self._check_response(resp, "Creds edit")

    def creds_remove(self, cred_ids: list[str]) -> str:
        resp = self._post("/creds/remove", {"cred_id_array": cred_ids})
        return self._check_response(resp, "Creds remove")

    def creds_set_tag(self, cred_ids: list[str], tag: str) -> str:
        resp = self._post("/creds/set/tag", {"id_array": cred_ids, "tag": tag})
        return self._check_response(resp, "Creds tag")

    # ─── Targets ──────────────────────────────────────────────────────

    def targets_list(self) -> list:
        return self._get("/targets/list")

    def targets_add(self, targets: list[dict]) -> str:
        resp = self._post("/targets/add", {"targets": targets})
        return self._check_response(resp, "Targets add")

    def target_edit(self, target_id: str, **fields) -> str:
        payload = {"t_target_id": target_id, **fields}
        resp = self._post("/targets/edit", payload)
        return self._check_response(resp, "Target edit")

    def target_remove(self, target_ids: list[str]) -> str:
        resp = self._post("/targets/remove", {"target_id_array": target_ids})
        return self._check_response(resp, "Target remove")

    def target_set_tag(self, target_ids: list[str], tag: str) -> str:
        resp = self._post("/targets/set/tag", {"id_array": target_ids, "tag": tag})
        return self._check_response(resp, "Target tag")

    # ─── Tunnels ──────────────────────────────────────────────────────

    def tunnel_list(self) -> list:
        return self._get("/tunnel/list")

    def tunnel_start_socks5(self, agent_id: str, lhost: str, lport: int,
                            desc: str = "", listen: bool = False,
                            use_auth: bool = False, username: str = "", password: str = "") -> str:
        payload = {
            "agent_id": agent_id, "listen": listen, "desc": desc,
            "l_host": lhost, "l_port": lport,
            "use_auth": use_auth, "username": username, "password": password,
        }
        resp = self._post("/tunnel/start/socks5", payload)
        return self._check_response(resp, "SOCKS5 start")

    def tunnel_start_socks4(self, agent_id: str, lhost: str, lport: int,
                            desc: str = "", listen: bool = False) -> str:
        payload = {"agent_id": agent_id, "listen": listen, "desc": desc, "l_host": lhost, "l_port": lport}
        resp = self._post("/tunnel/start/socks4", payload)
        return self._check_response(resp, "SOCKS4 start")

    def tunnel_start_lportfwd(self, agent_id: str, lhost: str, lport: int,
                              thost: str, tport: int, desc: str = "", listen: bool = False) -> str:
        payload = {
            "agent_id": agent_id, "listen": listen, "desc": desc,
            "l_host": lhost, "l_port": lport, "t_host": thost, "t_port": tport,
        }
        resp = self._post("/tunnel/start/lportfwd", payload)
        return self._check_response(resp, "Local port forward start")

    def tunnel_start_rportfwd(self, agent_id: str, port: int,
                              thost: str, tport: int, desc: str = "", listen: bool = False) -> str:
        payload = {
            "agent_id": agent_id, "listen": listen, "desc": desc,
            "port": port, "t_host": thost, "t_port": tport,
        }
        resp = self._post("/tunnel/start/rportfwd", payload)
        return self._check_response(resp, "Reverse port forward start")

    def tunnel_stop(self, tunnel_id: str) -> str:
        resp = self._post("/tunnel/stop", {"p_tunnel_id": tunnel_id})
        return self._check_response(resp, "Tunnel stop")

    def tunnel_set_info(self, tunnel_id: str, info: str) -> str:
        resp = self._post("/tunnel/set/info", {"p_tunnel_id": tunnel_id, "p_info": info})
        return self._check_response(resp, "Tunnel info")

    # ─── Chat ─────────────────────────────────────────────────────────

    def chat_send(self, message: str) -> str:
        resp = self._post("/chat/send", {"message": message})
        return self._check_response(resp, "Chat send")

    # ─── Services ─────────────────────────────────────────────────────

    def service_list(self) -> dict:
        return self._get("/service/list")

    def service_call(self, service: str, command: str, args: str = "") -> str:
        resp = self._post("/service/call", {"service": service, "command": command, "args": args})
        return str(resp)

    # ─── AxScript command parsing ─────────────────────────────────────

    def get_agent_commands(self, agent_name: str) -> list[dict]:
        """Parse available commands from agent's AxScript JS source."""
        reg = self.agent_regs.get(agent_name)
        if not reg or not reg.ax:
            return []
        return self._parse_axscript_commands(reg.ax)

    @staticmethod
    def _parse_axscript_commands(js_source: str) -> list[dict]:
        """Extract command definitions from ax.create_command() calls."""
        import re
        commands = []
        var_to_cmd = {}

        for line in js_source.split("\n"):
            line = line.strip()
            m = re.match(
                r'let\s+(\w+)\s*=\s*ax\.create_command\(\s*"([^"]+)"\s*,\s*"([^"]*)"'
                r'(?:\s*,\s*"([^"]*)")?',
                line,
            )
            if m:
                var_name, name, desc, example = m.group(1), m.group(2), m.group(3), m.group(4) or ""
                cmd = {"name": name, "description": desc, "example": example, "subcommands": []}
                var_to_cmd[var_name] = cmd
                commands.append(cmd)
                continue

            # Detect: parent.addSubCommands([child1, child2, ...])
            m2 = re.match(r'(\w+)\.addSubCommands\(\[([^\]]+)\]\)', line)
            if m2:
                parent_var = m2.group(1)
                children_str = m2.group(2)
                parent = var_to_cmd.get(parent_var)
                if parent:
                    for child_var in re.split(r',\s*', children_str):
                        child = var_to_cmd.get(child_var.strip())
                        if child:
                            parent["subcommands"].append(child)
                            if child in commands:
                                commands.remove(child)

        # Deduplicate by name (e.g. win/unix variants)
        seen = {}
        deduped = []
        for cmd in commands:
            if cmd["name"] not in seen:
                seen[cmd["name"]] = cmd
                deduped.append(cmd)
        return deduped

    # ─── Subscribe ────────────────────────────────────────────────────

    def subscribe(self, categories: list[str], console_team_mode: bool | None = None) -> str:
        payload: dict = {"categories": categories}
        if console_team_mode is not None:
            payload["console_team_mode"] = console_team_mode
        resp = self._post("/subscribe", payload)
        return self._check_response(resp, "Subscribe")

    # ─── File Upload (OTP-based) ──────────────────────────────────────

    def upload_temp_file(self, filepath: str) -> str:
        """Upload a file to server temp storage. Returns the file ID."""
        file_id = str(uuid.uuid4())
        # Get OTP for upload
        resp = self._post("/otp/generate", {"type": "tmp_upload", "data": {"id": file_id}})
        if not resp.get("ok"):
            raise RuntimeError(f"Upload OTP failed: {resp.get('message')}")
        otp = resp["message"]

        # Upload via multipart
        upload_url = f"{self.base_url}/otp/upload/temp?otp={otp}"
        with open(filepath, "rb") as f:
            files = {"file": (os.path.basename(filepath), f)}
            r = self._session.post(upload_url, files=files, timeout=300)
        if r.status_code != 200:
            raise RuntimeError(f"Upload failed: {r.text}")

        return file_id
