"""Data models for Adaptix C2 entities."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

# Packet type constants (must match server)
TYPE_SYNC_START = 0x11
TYPE_SYNC_FINISH = 0x12
SP_TYPE_EVENT = 0x13
TYPE_SYNC_BATCH = 0x14
TYPE_SYNC_CATEGORY_BATCH = 0x15
TYPE_CHAT_MESSAGE = 0x18
TYPE_SERVICE_DATA = 0x19

TYPE_LISTENER_REG = 0x21
TYPE_AGENT_REG = 0x22
TYPE_SERVICE_REG = 0x23

TYPE_LISTENER_START = 0x31
TYPE_LISTENER_EDIT = 0x32
TYPE_LISTENER_STOP = 0x33

TYPE_AGENT_NEW = 0x41
TYPE_AGENT_UPDATE = 0x42
TYPE_AGENT_REMOVE = 0x43
TYPE_AGENT_TICK = 0x44
TYPE_AGENT_LINK = 0x45

TYPE_AGENT_TASK_SYNC = 0x49
TYPE_AGENT_TASK_UPDATE = 0x4A
TYPE_AGENT_TASK_SEND = 0x4B
TYPE_AGENT_TASK_REMOVE = 0x4C
TYPE_AGENT_TASK_HOOK = 0x4D

TYPE_DOWNLOAD_CREATE = 0x51
TYPE_DOWNLOAD_UPDATE = 0x52
TYPE_DOWNLOAD_DELETE = 0x53
TYPE_DOWNLOAD_ACTUAL = 0x54

TYPE_TUNNEL_CREATE = 0x57
TYPE_TUNNEL_EDIT = 0x58
TYPE_TUNNEL_DELETE = 0x59

TYPE_SCREEN_CREATE = 0x5B
TYPE_SCREEN_UPDATE = 0x5C
TYPE_SCREEN_DELETE = 0x5D

TYPE_BROWSER_DISKS = 0x61
TYPE_BROWSER_FILES = 0x62
TYPE_BROWSER_STATUS = 0x63
TYPE_BROWSER_PROCESS = 0x64

TYPE_AGENT_CONSOLE_LOCAL = 0x67
TYPE_AGENT_CONSOLE_ERROR = 0x68
TYPE_AGENT_CONSOLE_OUT = 0x69
TYPE_AGENT_CONSOLE_TASK_SYNC = 0x6A
TYPE_AGENT_CONSOLE_TASK_UPD = 0x6B

TYPE_PIVOT_CREATE = 0x71
TYPE_PIVOT_DELETE = 0x72

TYPE_CREDS_CREATE = 0x81
TYPE_CREDS_EDIT = 0x82
TYPE_CREDS_DELETE = 0x83
TYPE_CREDS_SET_TAG = 0x84

TYPE_TARGETS_CREATE = 0x87
TYPE_TARGETS_EDIT = 0x88
TYPE_TARGETS_DELETE = 0x89
TYPE_TARGETS_SET_TAG = 0x8A

TYPE_AXSCRIPT_COMMANDS = 0x91

# OS constants
OS_UNKNOWN = 0
OS_WINDOWS = 1
OS_LINUX = 2
OS_MAC = 3

OS_NAMES = {OS_UNKNOWN: "Unknown", OS_WINDOWS: "Windows", OS_LINUX: "Linux", OS_MAC: "macOS"}

# Download states
DOWNLOAD_STATE_RUNNING = 1
DOWNLOAD_STATE_STOPPED = 2
DOWNLOAD_STATE_FINISHED = 3
DOWNLOAD_STATE_CANCELED = 4

DOWNLOAD_STATE_NAMES = {
    DOWNLOAD_STATE_RUNNING: "Running",
    DOWNLOAD_STATE_STOPPED: "Stopped",
    DOWNLOAD_STATE_FINISHED: "Finished",
    DOWNLOAD_STATE_CANCELED: "Canceled",
}

# Event types
EVENT_CLIENT_CONNECT = 1
EVENT_CLIENT_DISCONNECT = 2
EVENT_LISTENER_START = 3
EVENT_LISTENER_STOP = 4
EVENT_AGENT_NEW = 5
EVENT_TUNNEL_START = 6
EVENT_TUNNEL_STOP = 7


@dataclass
class ListenerReg:
    """Registered listener plugin type."""
    name: str = ""
    protocol: str = ""
    l_type: str = ""
    ax: str = ""


@dataclass
class AgentReg:
    """Registered agent plugin type."""
    agent: str = ""
    ax: str = ""
    listeners: list = field(default_factory=list)
    multi_listeners: bool = False
    groups: list = field(default_factory=list)


@dataclass
class Listener:
    """Active listener instance."""
    name: str = ""
    reg_name: str = ""
    protocol: str = ""
    l_type: str = ""
    bind_host: str = ""
    bind_port: str = ""
    agent_addr: str = ""
    create_time: int = 0
    status: str = ""
    data: str = ""


@dataclass
class Agent:
    """Agent session."""
    id: str = ""
    name: str = ""
    listener: str = ""
    async_agent: bool = False
    external_ip: str = ""
    internal_ip: str = ""
    gmt_offset: int = 0
    working_time: int = 0
    kill_date: int = 0
    sleep: int = 0
    jitter: int = 0
    acp: int = 0
    oemcp: int = 0
    pid: str = ""
    tid: str = ""
    arch: str = ""
    elevated: bool = False
    process: str = ""
    os: int = 0
    os_desc: str = ""
    domain: str = ""
    computer: str = ""
    username: str = ""
    impersonated: str = ""
    create_time: int = 0
    last_tick: int = 0
    tags: str = ""
    mark: str = ""
    color: str = ""
    active: bool = True

    @property
    def os_name(self) -> str:
        return OS_NAMES.get(self.os, "Unknown")

    @property
    def display_user(self) -> str:
        u = self.username
        if self.impersonated:
            u += f" ({self.impersonated})"
        if self.elevated:
            u += " *"
        return u

    @property
    def last_seen_ago(self) -> str:
        if self.last_tick == 0:
            return "never"
        diff = int(time.time()) - self.last_tick
        if diff < 0:
            diff = 0
        if diff < 60:
            return f"{diff}s"
        if diff < 3600:
            return f"{diff // 60}m {diff % 60}s"
        if diff < 86400:
            return f"{diff // 3600}h {(diff % 3600) // 60}m"
        return f"{diff // 86400}d {(diff % 86400) // 3600}h"


@dataclass
class Task:
    """Agent task."""
    task_id: str = ""
    task_type: int = 0
    agent_id: str = ""
    client: str = ""
    user: str = ""
    computer: str = ""
    cmdline: str = ""
    start_time: int = 0
    finish_time: int = 0
    message_type: int = 0
    message: str = ""
    text: str = ""
    completed: bool = False


@dataclass
class Download:
    """Downloaded file."""
    file_id: str = ""
    agent_id: str = ""
    agent_name: str = ""
    user: str = ""
    computer: str = ""
    filename: str = ""
    total_size: int = 0
    recv_size: int = 0
    state: int = 0
    date: int = 0


@dataclass
class Credential:
    """Credential entry."""
    cred_id: str = ""
    username: str = ""
    password: str = ""
    realm: str = ""
    cred_type: str = ""
    tag: str = ""
    date: int = 0
    storage: str = ""
    agent_id: str = ""
    host: str = ""


@dataclass
class Target:
    """Target host."""
    target_id: str = ""
    computer: str = ""
    domain: str = ""
    address: str = ""
    os: int = 0
    os_desc: str = ""
    tag: str = ""
    info: str = ""
    date: int = 0
    alive: bool = False
    agents: list = field(default_factory=list)


@dataclass
class Tunnel:
    """Tunnel/proxy."""
    tunnel_id: str = ""
    agent_id: str = ""
    computer: str = ""
    username: str = ""
    process: str = ""
    tun_type: str = ""
    info: str = ""
    interface: str = ""
    port: str = ""
    client: str = ""
    fhost: str = ""
    fport: str = ""


@dataclass
class Pivot:
    """Agent link."""
    pivot_id: str = ""
    pivot_name: str = ""
    parent_agent_id: str = ""
    child_agent_id: str = ""


@dataclass
class ChatMessage:
    """Team chat message."""
    username: str = ""
    message: str = ""
    date: int = 0


@dataclass
class Screenshot:
    """Screenshot entry."""
    screen_id: str = ""
    user: str = ""
    computer: str = ""
    note: str = ""
    date: int = 0
