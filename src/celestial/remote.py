"""スマホなど別の端末からの接続: Tailscale の状態、端末の登録(ペアリング)、認証。

- 接続は Tailscale のアドレスでだけ受け付ける(同じ Wi-Fi の他人や、インターネットからは届かない)。
- 登録は PC の画面に出る6桁のコード(10分で失効・1回限り)で行い、端末には長い合言葉(トークン)を渡す。
- トークンはハッシュ値だけを保存する。PC の画面からいつでも端末の登録を取り消せる。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL UNIQUE,
    created REAL NOT NULL,
    last_seen REAL NOT NULL DEFAULT 0,
    last_ip TEXT NOT NULL DEFAULT '',
    revoked INTEGER NOT NULL DEFAULT 0
);
"""
CODE_TTL = 600
_TAILNET = ipaddress.ip_network("100.64.0.0/10")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Devices:
    def __init__(self, store):
        self._db, self._lock = store._db, store._lock
        with self._lock, self._db:
            self._db.executescript(SCHEMA)
        self._codes: dict[str, float] = {}
        self._failures = 0
        self._code_lock = threading.Lock()

    # ---- 登録 ----

    def start_pairing(self) -> dict:
        with self._code_lock:
            now = time.time()
            self._codes = {c: t for c, t in self._codes.items() if t > now}
            code = f"{secrets.randbelow(10**6):06d}"
            self._codes[code] = now + CODE_TTL
            self._failures = 0
            return {"code": code, "expires": now + CODE_TTL}

    def pair(self, code: str, name: str, platform: str = "", ip: str = "") -> dict:
        code = "".join(ch for ch in str(code) if ch.isdigit())
        with self._code_lock:
            now = time.time()
            self._codes = {c: t for c, t in self._codes.items() if t > now}
            if code not in self._codes:
                self._failures += 1
                if self._failures >= 8:  # 総当たりを防ぐ: 失敗が続いたら、出ているコードをすべて無効にする
                    self._codes.clear()
                    self._failures = 0
                raise PermissionError("コードが違うか、有効期限が切れています。PC の画面で新しいコードを出してください")
            del self._codes[code]
        token = secrets.token_urlsafe(32)
        device_id = secrets.token_hex(8)
        name = (name or "スマホ").strip()[:60] or "スマホ"
        with self._lock, self._db:
            self._db.execute("INSERT INTO devices (id, name, platform, token_hash, created, last_seen, last_ip) VALUES (?,?,?,?,?,?,?)",
                             (device_id, name, (platform or "")[:30], _hash(token), time.time(), time.time(), ip))
        return {"token": token, "device_id": device_id, "name": name}

    # ---- 認証 ----

    def authenticate(self, token: str | None, ip: str = "") -> dict | None:
        if not token or len(token) > 200:
            return None
        with self._lock:
            row = self._db.execute("SELECT * FROM devices WHERE token_hash = ? AND revoked = 0", (_hash(token),)).fetchone()
        if row is None:
            return None
        device = {k: row[k] for k in row.keys() if k != "token_hash"}
        if time.time() - row["last_seen"] > 30 or row["last_ip"] != ip:
            with self._lock, self._db:
                self._db.execute("UPDATE devices SET last_seen = ?, last_ip = ? WHERE id = ?", (time.time(), ip, row["id"]))
        return device

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM devices WHERE revoked = 0 ORDER BY created").fetchall()
        return [{k: r[k] for k in r.keys() if k != "token_hash"} for r in rows]

    def get(self, device_id: str) -> dict | None:
        return next((d for d in self.list() if d["id"] == device_id), None)

    def revoke(self, device_id: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("UPDATE devices SET revoked = 1 WHERE id = ? AND revoked = 0", (device_id,)).rowcount == 1

    def rename(self, device_id: str, name: str) -> bool:
        name = name.strip()[:60]
        if not name:
            raise ValueError("名前が空です")
        with self._lock, self._db:
            return self._db.execute("UPDATE devices SET name = ? WHERE id = ? AND revoked = 0", (name, device_id)).rowcount == 1


# ---- Tailscale ------------------------------------------------------------

_cache: dict = {"at": 0.0, "value": None}


def _tailscale_exe() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    if sys.platform == "win32":
        default = Path(r"C:\Program Files\Tailscale\tailscale.exe")
        return str(default) if default.is_file() else None
    return None


def tailscale_status(max_age: float = 10.0) -> dict:
    """{state, ips, dns, hostname, peers}。state は Running(接続中)/ NeedsLogin / Stopped / NoState / missing など。"""
    if _cache["value"] is not None and time.time() - _cache["at"] < max_age:
        return _cache["value"]
    exe = _tailscale_exe()
    value = {"state": "missing", "ips": [], "dns": "", "hostname": socket.gethostname(), "peers": []}
    if exe:
        try:
            proc = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=6,
                                  creationflags=0x08000000 if sys.platform == "win32" else 0)
            data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
            me = data.get("Self") or {}
            ips = [ip for ip in me.get("TailscaleIPs") or [] if ":" not in ip]
            value = {"state": data.get("BackendState") or "unknown", "ips": ips,
                     "dns": (me.get("DNSName") or "").rstrip("."), "hostname": me.get("HostName") or socket.gethostname(),
                     "peers": [{"name": p.get("HostName") or "", "os": p.get("OS") or "", "online": bool(p.get("Online")),
                                "ip": next((ip for ip in p.get("TailscaleIPs") or [] if ":" not in ip), "")}
                               for p in (data.get("Peer") or {}).values()]}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            value["state"] = "error"
    if not value["ips"]:  # コマンドが使えなくても、Tailscale のアドレスが付いていれば使う
        try:
            local = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
            value["ips"] = sorted(ip for ip in local if ipaddress.ip_address(ip) in _TAILNET)
            if value["ips"] and value["state"] in ("missing", "error", "unknown"):
                value["state"] = "Running"
        except OSError:
            pass
    _cache.update(at=time.time(), value=value)
    return value


def is_tailnet(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in _TAILNET
    except ValueError:
        return False
