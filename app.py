from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
import psutil
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db, init_db
from models import Client, PaymentEvent, Subscription

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    # dotenv is optional; environment variables can be provided by the runtime
    pass

try:
    import paramiko
except Exception:  # pragma: no cover
    paramiko = None

app = FastAPI(title="3x-ui API Bridge")
templates = Jinja2Templates(directory="templates")
security = HTTPBasic()


class CreateClientRequest(BaseModel):
    tg_id: Optional[str] = None
    email: Optional[str] = None
    comment: Optional[str] = None
    flow: Optional[str] = None
    total_gb: Optional[float] = None
    limit_ip: Optional[int] = None
    expiry_days: Optional[int] = None
    expiry_time_ms: Optional[int] = None
    client_id: Optional[str] = None
    sub_id: Optional[str] = None
    server_id: Optional[str] = None
    # Optional overrides for link building
    sni: Optional[str] = None
    short_id: Optional[str] = None


class PaymentUpdateRequest(BaseModel):
    tg_id: str
    status: str
    paid_until: Optional[int] = None  # unix seconds
    client_uuid: Optional[str] = None
    amount: Optional[int] = None
    currency: Optional[str] = None
    provider: Optional[str] = None


class SshInstallRequest(BaseModel):
    host: str
    port: int = 22
    username: str = "root"
    password: str
    inbound_type: str = "vless_reality_tcp"
    ssl_type: str = "ip_cert"
    server_name: Optional[str] = None


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _required(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _db_disabled() -> bool:
    flag = _env("DISABLE_DB", "").strip().lower()
    return flag in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class PanelServer:
    id: str
    name: str
    scheme: str
    host: str
    port: int
    base_path: str
    username: str
    password: str
    twofa: str
    verify_tls: bool
    inbound_id: int
    vless_host: str
    protocol: str
    client_flow: str
    public_host: str
    public_port: int
    sub_path: str

    @property
    def base_url(self) -> str:
        path = self.base_path
        if not path or path == "/":
            path = ""
        if path and not path.startswith("/"):
            path = "/" + path
        return f"{self.scheme}://{self.host}:{self.port}{path}"


_PANEL_SERVERS: List[PanelServer] | None = None
_INSTALL_JOBS: Dict[str, Dict[str, Any]] = {}
_SERVER_NET_CACHE: Dict[str, Dict[str, float]] = {}


def _as_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: Optional[str], default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _as_float(value: Optional[str], default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _normalize_base_path(path: str) -> str:
    if not path or path == "/":
        return ""
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/")


def _servers_config_path() -> Path:
    return Path(__file__).resolve().parent / "servers.json"


def _env_file_path() -> Path:
    return Path(__file__).resolve().parent / ".env"


def _read_env_file() -> Dict[str, str]:
    path = _env_file_path()
    if not path.exists():
        return {}
    data: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _write_env_file(values: Dict[str, str]) -> None:
    path = _env_file_path()
    lines: List[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.strip().startswith("#") or "=" not in line:
                lines.append(line)
                continue
            key, _ = line.split("=", 1)
            key = key.strip()
            if key in values:
                lines.append(f"{key}={values[key]}")
                values.pop(key, None)
            else:
                lines.append(line)
    for key, value in values.items():
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def _load_servers_config() -> Optional[List[Dict[str, Any]]]:
    path = _servers_config_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("servers"), list):
        return raw["servers"]
    return []


def _save_servers_config(items: List[Dict[str, Any]]) -> None:
    path = _servers_config_path()
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _server_to_dict(server: PanelServer) -> Dict[str, Any]:
    return {
        "id": server.id,
        "name": server.name,
        "scheme": server.scheme,
        "host": server.host,
        "port": server.port,
        "base_path": server.base_path,
        "username": server.username,
        "password": server.password,
        "twofa": server.twofa,
        "verify_tls": server.verify_tls,
        "inbound_id": server.inbound_id,
        "vless_host": server.vless_host,
        "protocol": server.protocol,
        "client_flow": server.client_flow,
        "public_host": server.public_host,
        "public_port": server.public_port,
        "sub_path": server.sub_path,
    }


def _load_panel_servers() -> List[PanelServer]:
    items = _load_servers_config()
    raw = _env("PANEL_SERVERS_JSON", "")
    if items is None and raw:
        try:
            items = json.loads(raw)
        except Exception as exc:
            raise RuntimeError("PANEL_SERVERS_JSON is not valid JSON") from exc
    if items:
        servers: List[PanelServer] = []
        for idx, item in enumerate(items or []):
            if not isinstance(item, dict):
                continue
            server_id = str(item.get("id") or f"srv{idx+1}")
            name = str(item.get("name") or server_id)
            scheme = str(item.get("scheme") or _env("PANEL_SCHEME", "http"))
            host = str(item.get("host") or _required("PANEL_HOST"))
            port = _as_int(str(item.get("port") or _env("PANEL_PORT", "2053")), 2053)
            base_path = _normalize_base_path(str(item.get("base_path") or _env("PANEL_BASE_PATH", "/")))
            username = str(item.get("username") or _required("PANEL_USERNAME"))
            password = str(item.get("password") or _required("PANEL_PASSWORD"))
            twofa = str(item.get("twofa") or item.get("2fa") or _env("PANEL_2FA", ""))
            verify_tls = _as_bool(
                str(item.get("verify_tls")) if "verify_tls" in item else _env("PANEL_VERIFY_TLS", "true"),
                True,
            )
            inbound_id = _as_int(str(item.get("inbound_id") or _env("INBOUND_ID", "0")), 0)
            vless_host = str(item.get("vless_host") or _env("VLESS_HOST") or host)
            protocol = str(item.get("protocol") or "vless").lower()
            client_flow = str(item.get("client_flow") or "")
            public_host = str(item.get("public_host") or vless_host or host)
            public_port = _as_int(str(item.get("public_port") or "0"), 0)
            sub_path = str(item.get("sub_path") or "sub")
            if inbound_id <= 0:
                inbound_id = 0
            servers.append(
                PanelServer(
                    id=server_id,
                    name=name,
                    scheme=scheme,
                    host=host,
                    port=port,
                    base_path=base_path,
                    username=username,
                    password=password,
                    twofa=twofa,
                    verify_tls=verify_tls,
                    inbound_id=inbound_id,
                    vless_host=vless_host,
                    protocol=protocol,
                    client_flow=client_flow,
                    public_host=public_host,
                    public_port=public_port,
                    sub_path=sub_path,
                )
            )
        if servers:
            return servers

    # If no single server config provided, allow empty list (admin UI still works).
    if not _env("PANEL_HOST", ""):
        return []

    # Fallback to single server config
    inbound_id = _as_int(_env("INBOUND_ID", "0"), 0)
    if inbound_id <= 0:
        inbound_id = 0
    return [
        PanelServer(
            id="default",
            name="default",
            scheme=_env("PANEL_SCHEME", "http") or "http",
            host=_required("PANEL_HOST"),
            port=_as_int(_env("PANEL_PORT", "2053"), 2053),
            base_path=_normalize_base_path(_env("PANEL_BASE_PATH", "/")),
            username=_required("PANEL_USERNAME"),
            password=_required("PANEL_PASSWORD"),
            twofa=_env("PANEL_2FA", ""),
            verify_tls=_as_bool(_env("PANEL_VERIFY_TLS", "true"), True),
            inbound_id=inbound_id,
            vless_host=_env("VLESS_HOST") or _required("PANEL_HOST"),
            protocol="vless",
            client_flow="",
            public_host=_env("VLESS_HOST") or _required("PANEL_HOST"),
            public_port=0,
            sub_path="sub",
        )
    ]


def _get_panel_servers() -> List[PanelServer]:
    global _PANEL_SERVERS
    if _PANEL_SERVERS is None:
        _PANEL_SERVERS = _load_panel_servers()
    return _PANEL_SERVERS


def _get_panel_server(server_id: Optional[str]) -> PanelServer:
    servers = _get_panel_servers()
    if not servers:
        raise HTTPException(status_code=500, detail="No 3x-ui servers configured")
    if not server_id:
        return servers[0]
    for server in servers:
        if server.id == server_id:
            return server
    raise HTTPException(status_code=400, detail="Unknown server_id")


def _build_url(path: str) -> str:
    scheme = _env("PANEL_SCHEME", "http")
    host = _required("PANEL_HOST")
    port = _as_int(_env("PANEL_PORT", "2053"), 2053)
    base_path = _normalize_base_path(_env("PANEL_BASE_PATH", "/"))
    if not path.startswith("/"):
        path = "/" + path
    return f"{scheme}://{host}:{port}{base_path}{path}"


def _build_server_url(server: PanelServer, path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{server.base_url}{path}"


async def _xui_login(client: httpx.AsyncClient, server: PanelServer) -> None:
    login_data = {"username": server.username, "password": server.password}
    if server.twofa:
        login_data["twoFactorCode"] = server.twofa
    last_status: Optional[int] = None
    for path in ("/login", "/login/"):
        login_url = _build_server_url(server, path)
        resp = await client.post(login_url, data=login_data)
        last_status = resp.status_code
        if resp.status_code == 404:
            continue
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"3x-ui login failed (HTTP {resp.status_code})")
        try:
            data = resp.json()
            if data.get("success") is False:
                raise HTTPException(status_code=502, detail="3x-ui login failed")
        except Exception:
            pass
        return
    raise HTTPException(status_code=502, detail=f"3x-ui login failed (HTTP {last_status})")


async def _xui_add_client(client: httpx.AsyncClient, server: PanelServer, payload: Dict[str, Any]) -> None:
    url = _build_server_url(server, "/panel/api/inbounds/addClient")
    resp = await client.post(url, data=payload)
    if resp.status_code >= 400:
        resp = await client.post(url, json=payload)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"3x-ui addClient failed (HTTP {resp.status_code})")


async def _xui_get_inbound(
    client: httpx.AsyncClient, server: PanelServer, inbound_id: int
) -> Dict[str, Any]:
    url = _build_server_url(server, f"/panel/api/inbounds/get/{inbound_id}")
    resp = await client.get(url)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"3x-ui inbound get failed (HTTP {resp.status_code})")
    try:
        data = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="3x-ui inbound get failed (invalid JSON)")
    return data.get("obj") or {}


async def _xui_list_inbounds(client: httpx.AsyncClient, server: PanelServer) -> List[Dict[str, Any]]:
    url = _build_server_url(server, "/panel/api/inbounds/list")
    resp = await client.get(url)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"3x-ui inbounds list failed (HTTP {resp.status_code})")
    try:
        data = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="3x-ui inbounds list failed (invalid JSON)")
    return data.get("obj") or []


async def _xui_get_onlines(client: httpx.AsyncClient, server: PanelServer) -> List[str]:
    url = _build_server_url(server, "/panel/api/inbounds/onlines")
    resp = await client.post(url)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"3x-ui onlines failed (HTTP {resp.status_code})")
    try:
        data = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="3x-ui onlines failed (invalid JSON)")
    return data.get("obj") or []


def _parse_json_maybe(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return {}


def _extract_inbound_reality(inbound_obj: Dict[str, Any]) -> Dict[str, Any]:
    stream_settings = _parse_json_maybe(
        inbound_obj.get("streamSettings") or inbound_obj.get("stream_settings")
    )
    tls_settings = _parse_json_maybe(
        stream_settings.get("tlsSettings") or inbound_obj.get("tlsSettings")
    )
    reality_settings = _parse_json_maybe(
        stream_settings.get("realitySettings")
        or tls_settings.get("realitySettings")
        or inbound_obj.get("realitySettings")
    )
    reality_inner = _parse_json_maybe(
        reality_settings.get("settings") or tls_settings.get("settings")
    )
    security = (
        stream_settings.get("security")
        or tls_settings.get("security")
        or inbound_obj.get("security")
        or ""
    )
    security = str(security).strip().lower()
    has_reality = bool(reality_settings) or bool(reality_inner)
    return {
        "stream_settings": stream_settings,
        "tls_settings": tls_settings,
        "reality_settings": reality_settings,
        "reality_inner": reality_inner,
        "security": security,
        "has_reality": has_reality,
    }


def _extract_alpn(*sources: Dict[str, Any]) -> str:
    for source in sources:
        if not isinstance(source, dict):
            continue
        alpn = source.get("alpn")
        if isinstance(alpn, list) and alpn:
            return ",".join(str(item) for item in alpn if item)
        if isinstance(alpn, str) and alpn:
            return alpn
    return _env("DEFAULT_ALPN", "")


def _payment_is_paid(status: str) -> bool:
    normalized = status.strip().lower()
    return normalized in {"paid", "success", "ok", "confirmed"}


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def _to_float(value: Any) -> Optional[float]:
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        return float(value)
    except Exception:
        return None


def _to_number_with_unit(value: Any) -> Optional[tuple[float, Optional[str]]]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value), None
    if isinstance(value, str):
        v = value.strip().lower()
        if not v:
            return None
        unit = None
        for u in ("kb", "mb", "gb", "tb", "kib", "mib", "gib", "tib"):
            if v.endswith(u):
                unit = u
                v = v[: -len(u)].strip()
                break
        try:
            return float(v), unit
        except Exception:
            return None
    return None


def _bytes_from_number(num: float, unit: Optional[str]) -> float:
    if unit is None:
        return num
    unit = unit.lower()
    if unit in {"kb", "kib"}:
        return num * 1024
    if unit in {"mb", "mib"}:
        return num * 1024 * 1024
    if unit in {"gb", "gib"}:
        return num * 1024 * 1024 * 1024
    if unit in {"tb", "tib"}:
        return num * 1024 * 1024 * 1024 * 1024
    return num


def _format_bytes(num: Optional[float]) -> Optional[str]:
    if num is None:
        return None
    try:
        num = float(num)
    except Exception:
        return None
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.2f} {unit}"
        num /= 1024
    return f"{num:.2f} PB"


def _extract_size_pair(value: Any) -> tuple[Optional[str], Optional[str], Optional[float]]:
    if isinstance(value, dict):
        used = _pick(value, ["used", "current", "usedBytes", "used_bytes", "usedMemory", "used_mem", "usedGB", "usedMb"])
        total = _pick(value, ["total", "totalBytes", "total_bytes", "totalMemory", "total_mem", "totalGB", "totalMb"])
    else:
        used = None
        total = None
    used_num = _to_number_with_unit(used)
    total_num = _to_number_with_unit(total)
    used_b = _bytes_from_number(*used_num) if used_num else None
    total_b = _bytes_from_number(*total_num) if total_num else None
    pct = None
    if used_b is not None and total_b:
        pct = round((used_b / total_b) * 100, 1) if total_b > 0 else None
    return _format_bytes(used_b) if used_b is not None else None, _format_bytes(total_b) if total_b is not None else None, pct


def _extract_percent(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        used_str, total_str, pct = _extract_size_pair(value)
        if pct is not None:
            return pct
        if "used" in value and "total" in value:
            try:
                used = float(value.get("used"))
                total = float(value.get("total"))
                if total > 0:
                    return (used / total) * 100
            except Exception:
                pass
        for key in ("percent", "usedPercent", "usage", "used_percent", "value"):
            if key in value:
                return _to_float(value.get(key))
    if isinstance(value, list):
        for item in value:
            pct = _extract_percent(item)
            if pct is not None:
                return pct
    return _to_float(value)


def _format_percent(value: Any) -> Optional[float]:
    pct = _extract_percent(value)
    if pct is None:
        return None
    return round(pct, 1)


def _format_rate(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        if any(unit in v.lower() for unit in ("mbps", "kbps", "gbps", "bps", "mb/s", "kb/s", "gb/s")):
            return v
        num = _to_float(v)
        if num is None:
            return v
        value = num
    num = _to_float(value)
    if num is None:
        return None
    if num < 500:
        return f"{num:.1f} Mbps"
    mbps = (num * 8) / 1_000_000
    return f"{mbps:.1f} Mbps"


def _format_rate_bps(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    mbps = (value * 8) / 1_000_000
    return f"{mbps:.1f} Mbps"


def _calc_rate(server_id: str, up_total: Optional[float], down_total: Optional[float]) -> Dict[str, Optional[str]]:
    now = time.time()
    prev = _SERVER_NET_CACHE.get(server_id)
    _SERVER_NET_CACHE[server_id] = {
        "ts": now,
        "up": float(up_total or 0),
        "down": float(down_total or 0),
    }
    if not prev or up_total is None or down_total is None:
        return {"up": None, "down": None}
    dt = now - prev.get("ts", now)
    if dt <= 0:
        return {"up": None, "down": None}
    up_delta = float(up_total) - float(prev.get("up", up_total))
    down_delta = float(down_total) - float(prev.get("down", down_total))
    if up_delta < 0 or down_delta < 0:
        return {"up": None, "down": None}
    return {
        "up": _format_rate_bps(up_delta / dt),
        "down": _format_rate_bps(down_delta / dt),
    }


def _pick(d: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        if key in d:
            return d.get(key)
    return None


def _find_stats_dict(obj: Any) -> Optional[Dict[str, Any]]:
    """Best-effort search for a dict that contains system stats fields."""
    targets = {
        "cpu",
        "cpuPercent",
        "cpu_percent",
        "cpuUsage",
        "mem",
        "memory",
        "memPercent",
        "memoryPercent",
        "disk",
        "diskPercent",
        "disk_percent",
        "diskUsage",
        "netTraffic",
        "traffic",
        "network",
        "net",
    }
    queue = [obj]
    visited = 0
    while queue and visited < 200:
        visited += 1
        current = queue.pop(0)
        if isinstance(current, dict):
            if any(key in current for key in targets):
                return current
            # common wrapper keys
            for key in ("obj", "data", "result", "status", "stats", "stat", "system", "server", "info"):
                if key in current:
                    queue.append(current[key])
            # also traverse all values
            for value in current.values():
                if isinstance(value, (dict, list)):
                    queue.append(value)
        elif isinstance(current, list):
            for item in current:
                if isinstance(item, (dict, list)):
                    queue.append(item)
    return None


async def _tcp_ping(host: str, port: int, timeout: float) -> Optional[float]:
    start = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return round((time.perf_counter() - start) * 1000, 1)
    except Exception:
        return None


async def _xui_get_server_stats(client: httpx.AsyncClient, server: PanelServer) -> Dict[str, Any]:
    endpoints = [
        "/panel/api/server/status",
        "/panel/api/server/stat",
        "/panel/api/server/info",
        "/panel/api/system/status",
        "/panel/api/system/info",
        "/panel/api/system",
        "/panel/api/monitoring",
        "/panel/api/monitoring/get",
    ]
    data: Dict[str, Any] = {}
    for path in endpoints:
        try:
            resp = await client.get(_build_server_url(server, path))
            if resp.status_code >= 400:
                continue
            payload = resp.json()
            obj = payload
            if isinstance(payload, dict) and "obj" in payload:
                obj = payload.get("obj")
            stats_dict = _find_stats_dict(obj)
            if not isinstance(stats_dict, dict):
                continue
            data = stats_dict
            break
        except Exception:
            continue
    if not data:
        return {}
    net = _pick(data, ["netTraffic", "traffic", "network", "net"]) or {}
    up_raw = _pick(net, ["up", "upload", "tx", "sent", "out"]) or _pick(data, ["up", "upload", "tx"])
    down_raw = _pick(net, ["down", "download", "rx", "received", "in"]) or _pick(data, ["down", "download", "rx"])
    up_total = None
    down_total = None
    if not (isinstance(up_raw, str) and any(unit in up_raw.lower() for unit in ("mbps", "kbps", "gbps", "bps"))):
        up_total = _to_float(up_raw)
    if not (isinstance(down_raw, str) and any(unit in down_raw.lower() for unit in ("mbps", "kbps", "gbps", "bps"))):
        down_total = _to_float(down_raw)
    mem_obj = _pick(data, ["mem", "memory", "ram"])
    swap_obj = _pick(data, ["swap", "swapMemory"])
    disk_obj = _pick(data, ["disk", "storage"])
    mem_used, mem_total, mem_pct = _extract_size_pair(mem_obj)
    swap_used, swap_total, swap_pct = _extract_size_pair(swap_obj)
    disk_used, disk_total, disk_pct = _extract_size_pair(disk_obj)

    load = _pick(data, ["load", "loadavg", "loadAvg"])
    if isinstance(load, (list, tuple)):
        load_str = " ".join(str(x) for x in load[:3])
    else:
        load_str = str(load) if load is not None else None

    xray = _pick(data, ["xray", "xrayStatus", "xrayInfo"]) or {}
    xray_state = None
    xray_version = None
    if isinstance(xray, dict):
        xray_state = _pick(xray, ["state", "status", "running"])
        xray_version = _pick(xray, ["version", "ver"])

    uptime = _pick(data, ["uptime", "upTime", "uptimes", "runningTime"]) or {}
    os_uptime = None
    xray_uptime = None
    if isinstance(uptime, dict):
        os_uptime = _pick(uptime, ["system", "os", "host"])
        xray_uptime = _pick(uptime, ["xray", "core"])

    threads = _pick(data, ["threads", "goroutines", "processes"])

    cpu_pct = _format_percent(_pick(data, ["cpu", "cpuPercent", "cpu_percent", "cpuUsage", "cpu_used"]))
    if mem_pct is None:
        mem_pct = _format_percent(_pick(data, ["memPercent", "memoryPercent", "mem_percent", "ramPercent"]))
    if disk_pct is None:
        disk_pct = _format_percent(_pick(data, ["diskPercent", "disk_percent", "diskUsage"]))

    return {
        "cpu": cpu_pct,
        "ram": mem_pct,
        "disk": disk_pct,
        "mem_used": mem_used,
        "mem_total": mem_total,
        "swap_used": swap_used,
        "swap_total": swap_total,
        "swap": swap_pct,
        "disk_used": disk_used,
        "disk_total": disk_total,
        "load": load_str,
        "xray_state": xray_state,
        "xray_version": xray_version,
        "os_uptime": os_uptime,
        "xray_uptime": xray_uptime,
        "threads": threads,
        "up_total": up_total,
        "down_total": down_total,
        "up": _format_rate(up_raw),
        "down": _format_rate(down_raw),
    }


def _job_log(job_id: str, message: str) -> None:
    job = _INSTALL_JOBS.get(job_id)
    if not job:
        return
    job["log"].append(message)
    if len(job["log"]) > 500:
        job["log"] = job["log"][-500:]


def _run_ssh_install(job_id: str, payload: SshInstallRequest) -> None:
    if paramiko is None:
        _INSTALL_JOBS[job_id]["status"] = "error"
        _INSTALL_JOBS[job_id]["error"] = "paramiko is not installed"
        return

    job = _INSTALL_JOBS[job_id]
    job["status"] = "running"
    job["started_at"] = time.time()

    try:
        _job_log(job_id, f"Connecting to {payload.host}:{payload.port} as {payload.username}...")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=payload.host,
            port=payload.port,
            username=payload.username,
            password=payload.password,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
        )
        _job_log(job_id, "Connected. Starting 3x-ui installation...")

        install_cmd = "bash -lc \"curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh | bash\""
        stdin, stdout, stderr = client.exec_command(install_cmd, get_pty=True)

        for line in iter(stdout.readline, ""):
            if line:
                _job_log(job_id, line.rstrip())
        for line in iter(stderr.readline, ""):
            if line:
                _job_log(job_id, line.rstrip())

        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            job["status"] = "error"
            job["error"] = f"Installer exited with status {exit_status}"
            _job_log(job_id, job["error"])
        else:
            job["status"] = "success"
            _job_log(job_id, "Install completed.")

        client.close()
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        _job_log(job_id, f"Error: {exc}")
    finally:
        job["finished_at"] = time.time()


def _render_template(request: Request | str, name: str | Dict[str, Any], context: Dict[str, Any] | None = None) -> HTMLResponse:
    # Backward-compatible call style:
    # _render_template("template.html", {"request": request, ...})
    if context is None and isinstance(request, str) and isinstance(name, dict):
        context = name
        name = request
        request = context.get("request")
    payload = dict(context or {})
    if "request" not in payload and request is not None:
        payload["request"] = request
    template = templates.get_template(name)
    return HTMLResponse(template.render(payload))


def _build_vless_link(
    *,
    client_id: str,
    host: str,
    port: int,
    flow: str,
    sni: str,
    fingerprint: str,
    public_key: str,
    short_id: str,
    spider_x: str,
    alpn: Optional[str],
    network: str,
    header_type: str,
    label: str,
) -> str:
    query = {
        "encryption": "none",
        "flow": flow,
        "security": "reality",
        "sni": sni,
        "fp": fingerprint,
        "pbk": public_key,
        "sid": short_id,
        "spx": spider_x,
        "alpn": alpn or "",
        "type": network,
        "headerType": header_type,
    }
    query_string = "&".join(
        f"{k}={quote(str(v))}" for k, v in query.items() if v is not None and v != ""
    )
    return f"vless://{client_id}@{host}:{port}?{query_string}#{quote(label)}"


def _check_auth(authorization: Optional[str], x_api_token: Optional[str]) -> None:
    expected = _env("API_TOKEN", "")
    if not expected:
        return
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    elif x_api_token:
        token = x_api_token.strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid API token")


def _admin_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    admin_user = _env("ADMIN_USER", "")
    admin_pass = _env("ADMIN_PASS", "")
    # If admin_user is empty, allow access without auth (dev mode)
    if not admin_user:
        return
    # If password is empty, allow any password (press Enter)
    if not admin_pass:
        if not secrets.compare_digest(credentials.username, admin_user):
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )
        return
    if not secrets.compare_digest(credentials.username, admin_user) or not secrets.compare_digest(
        credentials.password, admin_pass
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.on_event("startup")
async def on_startup() -> None:
    if not _db_disabled():
        await init_db()


@app.post("/api/v1/create")
async def create_client(
    payload: CreateClientRequest,
    authorization: Optional[str] = Header(default=None),
    x_api_token: Optional[str] = Header(default=None),
    db: Optional[AsyncSession] = Depends(get_db),
) -> Dict[str, Any]:
    _check_auth(authorization, x_api_token)

    server = _get_panel_server(payload.server_id)
    if server.inbound_id <= 0:
        raise HTTPException(status_code=500, detail="INBOUND_ID is not set")
    inbound_id = server.inbound_id
    verify_tls = server.verify_tls
    timeout = _as_float(_env("REQUEST_TIMEOUT", "10"), 10.0)

    client_uuid = payload.client_id or str(uuid.uuid4())
    sub_id = payload.sub_id or secrets.token_hex(8)
    flow = payload.flow or server.client_flow or _env("DEFAULT_FLOW", "xtls-rprx-vision")
    email = payload.email or f"tg_{payload.tg_id or 'user'}_{client_uuid[:8]}"
    comment = payload.comment or ""

    if payload.expiry_time_ms is not None:
        expiry_time = int(payload.expiry_time_ms)
    elif payload.expiry_days is not None:
        expiry_time = int(time.time() * 1000) + int(payload.expiry_days) * 86400000
    else:
        expiry_time = 0

    total_gb_bytes = 0
    if payload.total_gb is not None:
        total_gb_bytes = int(payload.total_gb * 1024 * 1024 * 1024)

    client_obj = {
        "id": client_uuid,
        "flow": flow,
        "email": email,
        "limitIp": payload.limit_ip or 0,
        "totalGB": total_gb_bytes,
        "expiryTime": expiry_time,
        "enable": True,
        "tgId": payload.tg_id or "",
        "subId": sub_id,
        "comment": comment,
        "reset": 0,
    }

    add_client_data = {
        "id": str(inbound_id),
        "settings": json.dumps({"clients": [client_obj]}, ensure_ascii=False),
    }

    async with httpx.AsyncClient(
        timeout=timeout,
        verify=verify_tls,
        follow_redirects=True,
    ) as client:
        await _xui_login(client, server)
        inbound_obj = await _xui_get_inbound(client, server, inbound_id)
        inbound_meta = _extract_inbound_reality(inbound_obj)
        stream_settings = inbound_meta["stream_settings"]
        tls_settings = inbound_meta["tls_settings"]
        reality_settings = inbound_meta["reality_settings"]
        reality_inner = inbound_meta["reality_inner"]
        security = inbound_meta["security"]
        has_reality = inbound_meta["has_reality"]
        if security != "reality" and not has_reality:
            raise HTTPException(
                status_code=500,
                detail=f"Inbound security is not set to reality (security={security or 'missing'})",
            )
        await _xui_add_client(client, server, add_client_data)

    if security != "reality" and not has_reality:
        raise HTTPException(
            status_code=500,
            detail=f"Inbound security is not set to reality (security={security or 'missing'})",
        )

    server_names = reality_settings.get("serverNames") or []
    sni = payload.sni or (server_names[0] if server_names else reality_inner.get("serverName", ""))
    if not sni:
        raise HTTPException(
            status_code=500,
            detail="SNI is missing in inbound reality settings",
        )

    short_ids = reality_settings.get("shortIds") or []
    short_id = payload.short_id or (short_ids[0] if short_ids else "")
    if not short_id:
        raise HTTPException(
            status_code=500,
            detail="shortId is missing in inbound reality settings",
        )

    public_key = reality_inner.get("publicKey") or reality_settings.get("publicKey", "")
    if not public_key:
        raise HTTPException(
            status_code=500,
            detail="publicKey is missing in inbound reality settings",
        )

    fingerprint = reality_inner.get("fingerprint") or _env("DEFAULT_FINGERPRINT", "random")
    spider_x = reality_inner.get("spiderX") or "/"

    network = stream_settings.get("network", "tcp")
    alpn = _extract_alpn(reality_inner, reality_settings, stream_settings, tls_settings)
    tcp_settings = _parse_json_maybe(stream_settings.get("tcpSettings"))
    header_type = _parse_json_maybe(tcp_settings.get("header")).get("type", "none")

    port = int(inbound_obj.get("port", 0)) or 0
    if port <= 0:
        raise HTTPException(status_code=500, detail="Inbound port is missing")

    vless_host = server.public_host or server.vless_host
    if not vless_host:
        raise HTTPException(status_code=500, detail="VLESS_HOST is not set")
    if server.public_port and server.public_port > 0:
        port = server.public_port

    label = comment or email
    vless_url = _build_vless_link(
        client_id=client_uuid,
        host=vless_host,
        port=port,
        flow=flow,
        sni=sni,
        fingerprint=fingerprint,
        public_key=public_key,
        short_id=short_id,
        spider_x=spider_x,
        alpn=alpn,
        network=network,
        header_type=header_type,
        label=label,
    )

    if db is not None:
        new_client = Client(
            tg_id=payload.tg_id,
            email=email,
            comment=comment,
            client_uuid=client_uuid,
            sub_id=sub_id,
            vless_url=vless_url,
            inbound_id=inbound_id,
            server_id=server.id,
            status="active",
        )
        db.add(new_client)
        if payload.tg_id:
            existing = await db.execute(select(Subscription).where(Subscription.tg_id == payload.tg_id))
            if existing.scalar_one_or_none() is None:
                db.add(Subscription(tg_id=payload.tg_id, is_paid=False))
        await db.commit()

    return {
        "ok": True,
        "vless_url": vless_url,
        "client_id": client_uuid,
        "email": email,
        "sub_id": sub_id,
        "inbound_id": inbound_id,
        "server_id": server.id,
    }


@app.post("/api/v1/payment")
async def update_payment(
    payload: PaymentUpdateRequest,
    authorization: Optional[str] = Header(default=None),
    x_api_token: Optional[str] = Header(default=None),
    db: Optional[AsyncSession] = Depends(get_db),
) -> Dict[str, Any]:
    _check_auth(authorization, x_api_token)

    paid_until_dt: datetime | None = None
    if payload.paid_until is not None:
        paid_until_dt = datetime.fromtimestamp(payload.paid_until, tz=timezone.utc)

    is_paid = _payment_is_paid(payload.status)

    if db is not None:
        result = await db.execute(select(Subscription).where(Subscription.tg_id == payload.tg_id))
        sub = result.scalar_one_or_none()
        if sub is None:
            sub = Subscription(tg_id=payload.tg_id, is_paid=is_paid, paid_until=paid_until_dt)
            db.add(sub)
        else:
            sub.is_paid = is_paid
            if paid_until_dt is not None:
                sub.paid_until = paid_until_dt

        db.add(
            PaymentEvent(
                tg_id=payload.tg_id,
                client_uuid=payload.client_uuid,
                status=payload.status,
                amount=payload.amount,
                currency=payload.currency,
                provider=payload.provider,
                paid_at=paid_until_dt if is_paid else None,
            )
        )
        await db.commit()

    return {"ok": True, "tg_id": payload.tg_id, "is_paid": is_paid}


@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(_admin_auth)])
async def admin_dashboard(request: Request, db: Optional[AsyncSession] = Depends(get_db)) -> HTMLResponse:
    cpu = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(os.getcwd())

    if db is None:
        total_clients = 0
        total_paid = 0
    else:
        total_clients = (await db.execute(select(func.count(Client.id)))).scalar() or 0
        total_paid = (await db.execute(select(func.count(Subscription.id)).where(Subscription.is_paid.is_(True)))).scalar() or 0

    servers = _get_panel_servers()
    return _render_template(
        request,
        "dashboard.html",
        {
            "cpu": cpu,
            "mem": mem,
            "disk": disk,
            "total_clients": total_clients,
            "total_paid": total_paid,
            "servers_count": len(servers),
        },
    )


@app.get("/admin/clients", response_class=HTMLResponse, dependencies=[Depends(_admin_auth)])
async def admin_clients(request: Request, db: Optional[AsyncSession] = Depends(get_db)) -> HTMLResponse:
    if db is None:
        clients = []
    else:
        result = await db.execute(select(Client).order_by(desc(Client.id)).limit(500))
        clients = result.scalars().all()
    return _render_template(
        request,
        "clients.html",
        {
            "clients": clients,
        },
    )


@app.get("/admin/servers", response_class=HTMLResponse, dependencies=[Depends(_admin_auth)])
async def admin_servers(request: Request) -> HTMLResponse:
    servers = _get_panel_servers()
    timeout = _as_float(_env("REQUEST_TIMEOUT", "10"), 10.0)

    async def fetch_server(server: PanelServer) -> Dict[str, Any]:
        try:
            ping_ms = await _tcp_ping(server.host, server.port, timeout=timeout)
            async with httpx.AsyncClient(
                timeout=timeout,
                verify=server.verify_tls,
                follow_redirects=True,
            ) as client:
                await _xui_login(client, server)
                inbounds = await _xui_list_inbounds(client, server)
                onlines = await _xui_get_onlines(client, server)
                stats = await _xui_get_server_stats(client, server)
            rate = _calc_rate(server.id, stats.get("up_total"), stats.get("down_total"))
            up_value = rate.get("up")
            down_value = rate.get("down")
            return {
                "id": server.id,
                "name": server.name,
                "host": server.host,
                "port": server.port,
                "panel_url": server.base_url,
                "inbounds": inbounds,
                "onlines": onlines,
                "onlines_count": len(onlines),
                "cpu": stats.get("cpu"),
                "ram": stats.get("ram"),
                "disk": stats.get("disk"),
                "up": up_value,
                "down": down_value,
                "ping_ms": ping_ms,
                "ok": True,
                "error": None,
            }
        except Exception as exc:
            return {
                "id": server.id,
                "name": server.name,
                "host": server.host,
                "port": server.port,
                "panel_url": server.base_url,
                "inbounds": [],
                "onlines": [],
                "onlines_count": 0,
                "cpu": None,
                "ram": None,
                "disk": None,
                "up": None,
                "down": None,
                "ping_ms": None,
                "ok": False,
                "error": str(exc),
            }

    results: List[Dict[str, Any]] = []
    if servers:
        results = await asyncio.gather(*[fetch_server(server) for server in servers])

    total_servers = len(results)
    active_servers = sum(1 for item in results if item.get("ok"))
    online_total = sum(int(item.get("onlines_count", 0) or 0) for item in results)

    return _render_template(
        request,
        "servers.html",
        {
            "servers": results,
            "total_servers": total_servers,
            "active_servers": active_servers,
            "online_total": online_total,
            "servers_configured": len(servers) > 0,
        },
    )


@app.get("/admin/servers/status", dependencies=[Depends(_admin_auth)])
async def admin_servers_status() -> Dict[str, Any]:
    servers = _get_panel_servers()
    timeout = _as_float(_env("REQUEST_TIMEOUT", "10"), 10.0)

    async def fetch_status(server: PanelServer) -> Dict[str, Any]:
        try:
            ping_ms = await _tcp_ping(server.host, server.port, timeout=timeout)
            async with httpx.AsyncClient(
                timeout=timeout,
                verify=server.verify_tls,
                follow_redirects=True,
            ) as client:
                await _xui_login(client, server)
                onlines = await _xui_get_onlines(client, server)
                stats = await _xui_get_server_stats(client, server)
            rate = _calc_rate(server.id, stats.get("up_total"), stats.get("down_total"))
            return {
                "id": server.id,
                "ok": True,
                "onlines_count": len(onlines),
                "cpu": stats.get("cpu"),
                "ram": stats.get("ram"),
                "disk": stats.get("disk"),
                "up": rate.get("up"),
                "down": rate.get("down"),
                "ping_ms": ping_ms,
            }
        except Exception as exc:
            return {
                "id": server.id,
                "ok": False,
                "onlines_count": 0,
                "cpu": None,
                "ram": None,
                "disk": None,
                "up": None,
                "down": None,
                "ping_ms": None,
                "error": str(exc),
            }

    items: List[Dict[str, Any]] = []
    if servers:
        items = await asyncio.gather(*[fetch_status(server) for server in servers])
    return {"items": items, "ts": time.time()}


@app.get("/admin/servers/new", response_class=HTMLResponse, dependencies=[Depends(_admin_auth)])
async def admin_server_new(request: Request) -> HTMLResponse:
    empty = {
        "id": "",
        "name": "",
        "scheme": "https",
        "host": "",
        "port": 2053,
        "base_path": "/",
        "username": "admin",
        "password": "",
        "twofa": "",
        "verify_tls": True,
        "inbound_id": 1,
        "vless_host": "",
        "protocol": "vless",
        "client_flow": "",
        "public_host": "",
        "public_port": 0,
        "sub_path": "sub",
    }
    return _render_template(
        request,
        "server_edit.html",
        {"server": empty, "is_new": True, "panel_info": None},
    )


@app.get("/admin/servers/edit/{server_id}", response_class=HTMLResponse, dependencies=[Depends(_admin_auth)])
async def admin_server_edit(request: Request, server_id: str) -> HTMLResponse:
    items = _load_servers_config()
    if items is None:
        items = [_server_to_dict(s) for s in _get_panel_servers()]
    server = next((item for item in items if str(item.get("id")) == server_id), None)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")
    panel_info: Dict[str, Any] | None = None
    try:
        server_obj = _get_panel_server(server_id)
        async with httpx.AsyncClient(
            timeout=_as_float(_env("REQUEST_TIMEOUT", "10"), 10.0),
            verify=server_obj.verify_tls,
            follow_redirects=True,
        ) as client:
            await _xui_login(client, server_obj)
            inbound = await _xui_get_inbound(client, server_obj, int(server_obj.inbound_id or 0))
            stats = await _xui_get_server_stats(client, server_obj)
        inbound_meta = _extract_inbound_reality(inbound)
        security = inbound_meta["security"]
        has_reality = inbound_meta["has_reality"]
        panel_info = {
            "protocol": inbound.get("protocol"),
            "port": inbound.get("port"),
            "remark": inbound.get("remark"),
            "enable": inbound.get("enable"),
            "up": inbound.get("up"),
            "down": inbound.get("down"),
            "total": inbound.get("total"),
            "security": security,
            "has_reality": has_reality,
            "cpu": stats.get("cpu"),
            "ram": stats.get("ram"),
            "mem_used": stats.get("mem_used"),
            "mem_total": stats.get("mem_total"),
            "swap": stats.get("swap"),
            "swap_used": stats.get("swap_used"),
            "swap_total": stats.get("swap_total"),
            "disk": stats.get("disk"),
            "disk_used": stats.get("disk_used"),
            "disk_total": stats.get("disk_total"),
            "load": stats.get("load"),
            "xray_state": stats.get("xray_state"),
            "xray_version": stats.get("xray_version"),
            "os_uptime": stats.get("os_uptime"),
            "xray_uptime": stats.get("xray_uptime"),
            "threads": stats.get("threads"),
        }
    except Exception as exc:
        panel_info = {"error": str(exc)}

    return _render_template(
        request,
        "server_edit.html",
        {"server": server, "is_new": False, "panel_info": panel_info},
    )


@app.post("/admin/servers/save", dependencies=[Depends(_admin_auth)])
async def admin_server_save(request: Request) -> RedirectResponse:
    form = await request.form()
    server_id = str(form.get("id") or "").strip()
    if not server_id:
        server_id = f"srv{int(time.time())}"

    host = str(form.get("host") or "").strip()
    scheme = str(form.get("scheme") or "https").strip()
    port = _as_int(str(form.get("port") or "2053"), 2053)
    base_path = _normalize_base_path(str(form.get("base_path") or "/"))
    username = str(form.get("username") or "admin").strip()
    password = str(form.get("password") or "")
    twofa = str(form.get("twofa") or "")
    verify_tls = str(form.get("verify_tls") or "").lower() in {"on", "true", "1", "yes"}
    inbound_id = _as_int(str(form.get("inbound_id") or "0"), 0)
    vless_host = str(form.get("vless_host") or host).strip() or host
    name = str(form.get("name") or server_id).strip() or server_id
    protocol = str(form.get("protocol") or "vless").strip().lower()
    client_flow = str(form.get("client_flow") or "").strip()
    public_host = str(form.get("public_host") or host).strip() or host
    public_port = _as_int(str(form.get("public_port") or "0"), 0)
    sub_path = str(form.get("sub_path") or "sub").strip()

    item = {
        "id": server_id,
        "name": name,
        "scheme": scheme,
        "host": host,
        "port": port,
        "base_path": base_path,
        "username": username,
        "password": password,
        "twofa": twofa,
        "verify_tls": verify_tls,
        "inbound_id": inbound_id,
        "vless_host": vless_host,
        "protocol": protocol,
        "client_flow": client_flow,
        "public_host": public_host,
        "public_port": public_port,
        "sub_path": sub_path,
    }

    items = _load_servers_config()
    if items is None:
        items = [_server_to_dict(s) for s in _get_panel_servers()]
    updated = False
    for idx, existing in enumerate(items):
        if str(existing.get("id")) == server_id:
            items[idx] = item
            updated = True
            break
    if not updated:
        items.append(item)

    _save_servers_config(items)
    global _PANEL_SERVERS
    _PANEL_SERVERS = None
    return RedirectResponse(url="/admin/servers", status_code=303)


@app.post("/admin/servers/delete/{server_id}", dependencies=[Depends(_admin_auth)])
async def admin_server_delete(server_id: str) -> RedirectResponse:
    items = _load_servers_config()
    if items is None:
        items = [_server_to_dict(s) for s in _get_panel_servers()]
    items = [item for item in items if str(item.get("id")) != server_id]
    _save_servers_config(items)
    global _PANEL_SERVERS
    _PANEL_SERVERS = None
    return RedirectResponse(url="/admin/servers", status_code=303)


@app.get("/admin/servers/config", response_class=HTMLResponse, dependencies=[Depends(_admin_auth)])
async def admin_servers_config(request: Request) -> HTMLResponse:
    servers = _get_panel_servers()
    safe_servers = [
        {
            "id": s.id,
            "name": s.name,
            "scheme": s.scheme,
            "host": s.host,
            "port": s.port,
            "base_path": s.base_path,
            "username": s.username,
            "password": _mask_secret(s.password),
            "verify_tls": s.verify_tls,
            "inbound_id": s.inbound_id,
            "vless_host": s.vless_host,
            "protocol": s.protocol,
            "client_flow": s.client_flow,
            "public_host": s.public_host,
            "public_port": s.public_port,
            "sub_path": s.sub_path,
        }
        for s in servers
    ]
    example = [
        {
            "id": "srv1",
            "name": "Main",
            "scheme": "https",
            "host": "panel.example.com",
            "port": 2053,
            "base_path": "/random",
            "username": "admin",
            "password": "pass",
            "verify_tls": True,
            "inbound_id": 1,
            "vless_host": "vpn.example.com",
            "protocol": "vless",
            "client_flow": "xtls-rprx-vision",
            "public_host": "vpn.example.com",
            "public_port": 2096,
            "sub_path": "sub",
        }
    ]
    return _render_template(
        request,
        "servers_config.html",
        {
            "servers": safe_servers,
            "example": json.dumps(example, indent=2, ensure_ascii=False),
        },
    )


@app.get("/admin/settings", response_class=HTMLResponse, dependencies=[Depends(_admin_auth)])
async def admin_settings(request: Request) -> HTMLResponse:
    env_file = _read_env_file()

    def val(key: str, default: str = "") -> str:
        return env_file.get(key, _env(key, default) or default)

    context = {
        "api_token": val("API_TOKEN"),
        "admin_user": val("ADMIN_USER", "admin"),
        "admin_pass": "",
        "panel_scheme": val("PANEL_SCHEME", "https"),
        "panel_host": val("PANEL_HOST"),
        "panel_port": val("PANEL_PORT", "2053"),
        "panel_base_path": val("PANEL_BASE_PATH", "/"),
        "panel_username": val("PANEL_USERNAME", "admin"),
        "panel_password": "",
        "panel_2fa": val("PANEL_2FA"),
        "panel_verify_tls": val("PANEL_VERIFY_TLS", "true").lower() in {"1", "true", "yes", "y", "on"},
        "inbound_id": val("INBOUND_ID", "1"),
        "vless_host": val("VLESS_HOST"),
        "default_flow": val("DEFAULT_FLOW", "xtls-rprx-vision"),
        "default_fingerprint": val("DEFAULT_FINGERPRINT", "random"),
        "default_alpn": val("DEFAULT_ALPN"),
        "request_timeout": val("REQUEST_TIMEOUT", "10"),
        "disable_db": val("DISABLE_DB", "").lower() in {"1", "true", "yes", "y", "on"},
    }
    return _render_template(request, "settings.html", context)


@app.post("/admin/settings/save", dependencies=[Depends(_admin_auth)])
async def admin_settings_save(request: Request) -> RedirectResponse:
    form = await request.form()
    existing = _read_env_file()

    def keep_or_update(key: str, value: str) -> None:
        existing[key] = value
        os.environ[key] = value

    def update_if_present(key: str, value: str) -> None:
        if value != "":
            keep_or_update(key, value)

    keep_or_update("API_TOKEN", str(form.get("api_token") or "").strip())
    keep_or_update("ADMIN_USER", str(form.get("admin_user") or "admin").strip() or "admin")
    update_if_present("ADMIN_PASS", str(form.get("admin_pass") or "").strip())

    keep_or_update("PANEL_SCHEME", str(form.get("panel_scheme") or "https").strip())
    keep_or_update("PANEL_HOST", str(form.get("panel_host") or "").strip())
    keep_or_update("PANEL_PORT", str(form.get("panel_port") or "2053").strip())
    keep_or_update("PANEL_BASE_PATH", str(form.get("panel_base_path") or "/").strip())
    keep_or_update("PANEL_USERNAME", str(form.get("panel_username") or "admin").strip())
    update_if_present("PANEL_PASSWORD", str(form.get("panel_password") or "").strip())
    keep_or_update("PANEL_2FA", str(form.get("panel_2fa") or "").strip())
    keep_or_update("PANEL_VERIFY_TLS", "true" if form.get("panel_verify_tls") else "false")

    keep_or_update("INBOUND_ID", str(form.get("inbound_id") or "1").strip())
    keep_or_update("VLESS_HOST", str(form.get("vless_host") or "").strip())
    keep_or_update("DEFAULT_FLOW", str(form.get("default_flow") or "xtls-rprx-vision").strip())
    keep_or_update("DEFAULT_FINGERPRINT", str(form.get("default_fingerprint") or "random").strip())
    keep_or_update("DEFAULT_ALPN", str(form.get("default_alpn") or "").strip())
    keep_or_update("REQUEST_TIMEOUT", str(form.get("request_timeout") or "10").strip())
    keep_or_update("DISABLE_DB", "true" if form.get("disable_db") else "false")

    _write_env_file(existing)
    global _PANEL_SERVERS
    _PANEL_SERVERS = None
    return RedirectResponse(url="/admin/settings", status_code=303)


@app.get("/admin/servers/ssh", response_class=HTMLResponse, dependencies=[Depends(_admin_auth)])
async def admin_servers_ssh(request: Request) -> HTMLResponse:
    return _render_template(
        request,
        "servers_ssh.html",
        {},
    )


@app.post("/admin/servers/ssh/install", dependencies=[Depends(_admin_auth)])
async def admin_servers_ssh_install(payload: SshInstallRequest) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    _INSTALL_JOBS[job_id] = {
        "id": job_id,
        "status": "queued",
        "log": [],
        "error": None,
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "host": payload.host,
        "port": payload.port,
        "username": payload.username,
        "inbound_type": payload.inbound_type,
        "ssl_type": payload.ssl_type,
        "server_name": payload.server_name or payload.host,
    }
    asyncio.create_task(asyncio.to_thread(_run_ssh_install, job_id, payload))
    return {"ok": True, "job_id": job_id}


@app.get("/admin/servers/ssh/status/{job_id}", dependencies=[Depends(_admin_auth)])
async def admin_servers_ssh_status(job_id: str) -> Dict[str, Any]:
    job = _INSTALL_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "ok": True,
        "job": {
            "id": job["id"],
            "status": job["status"],
            "log": job["log"],
            "error": job["error"],
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "host": job["host"],
            "port": job["port"],
            "username": job["username"],
            "inbound_type": job["inbound_type"],
            "ssl_type": job["ssl_type"],
            "server_name": job["server_name"],
        },
    }


@app.get("/admin/payments", response_class=HTMLResponse, dependencies=[Depends(_admin_auth)])
async def admin_payments(request: Request, db: Optional[AsyncSession] = Depends(get_db)) -> HTMLResponse:
    if db is None:
        payments = []
        subs = []
    else:
        payments = (await db.execute(select(PaymentEvent).order_by(desc(PaymentEvent.id)).limit(500))).scalars().all()
        subs = (await db.execute(select(Subscription))).scalars().all()
    subs_map = {sub.tg_id: sub for sub in subs}
    return _render_template(
        request,
        "payments.html",
        {
            "payments": payments,
            "subs_map": subs_map,
        },
    )
