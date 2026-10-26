"""Persistent config for whiskacli — stored at ~/.whiskacli/config."""
from __future__ import annotations

import configparser
import os

_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".whiskacli", "config")
_SECTION = "whiska"


def load_config() -> dict[str, str]:
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_PATH)
    if _SECTION in cfg:
        return dict(cfg[_SECTION])
    return {}


def save_config(key: str, value: str) -> None:
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_PATH)
    if _SECTION not in cfg:
        cfg[_SECTION] = {}
    cfg[_SECTION][key] = value
    with open(_CONFIG_PATH, "w") as f:
        cfg.write(f)


def get(key: str, env_var: str = "", default: str = "") -> str:
    """Get a config value: env var takes priority, then config file, then default."""
    if env_var:
        v = os.environ.get(env_var, "")
        if v:
            return v
    return load_config().get(key, default)
