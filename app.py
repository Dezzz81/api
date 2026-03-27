from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
import psutil
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
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

    @property
    def base_url(self) -> str:
        path = self.base_path
        if not path or path == "/":
            path = ""
        if path and not path.startswith("/"):
            path = "/" + path
        return f"{self.scheme}://{self.host}:{self.port}{path}"


_PANEL_SERVERS: List[PanelServer] | None = None


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


def _load_panel_servers() -> List[PanelServer]:
    raw = _env("PANEL_SERVERS_JSON", "")
    if raw:
        try:
            items = json.loads(raw)
        except Exception as exc:
            raise RuntimeError("PANEL_SERVERS_JSON is not valid JSON") from exc
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
            if inbound_id <= 0:
                raise RuntimeError(f"INBOUND_ID is not set for server {server_id}")
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
                )
            )
        if servers:
            return servers

    # Fallback to single server config
    inbound_id = _as_int(_env("INBOUND_ID", "0"), 0)
    if inbound_id <= 0:
        raise RuntimeError("INBOUND_ID is not set")
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
        )
    ]


def _get_panel_servers() -> List[PanelServer]:
    global _PANEL_SERVERS
    if _PANEL_SERVERS is None:
        _PANEL_SERVERS = _load_panel_servers()
    return _PANEL_SERVERS


def _get_panel_server(server_id: Optional[str]) -> PanelServer:
    servers = _get_panel_servers()
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
    login_url = _build_server_url(server, "/login/")
    login_data = {"username": server.username, "password": server.password}
    if server.twofa:
        login_data["twoFactorCode"] = server.twofa
    resp = await client.post(login_url, data=login_data)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"3x-ui login failed (HTTP {resp.status_code})")
    try:
        data = resp.json()
        if data.get("success") is False:
            raise HTTPException(status_code=502, detail="3x-ui login failed")
    except Exception:
        pass


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
    return resp.json().get("obj") or {}


async def _xui_list_inbounds(client: httpx.AsyncClient, server: PanelServer) -> List[Dict[str, Any]]:
    url = _build_server_url(server, "/panel/api/inbounds/list")
    resp = await client.get(url)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"3x-ui inbounds list failed (HTTP {resp.status_code})")
    return resp.json().get("obj") or []


async def _xui_get_onlines(client: httpx.AsyncClient, server: PanelServer) -> List[str]:
    url = _build_server_url(server, "/panel/api/inbounds/onlines")
    resp = await client.post(url)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"3x-ui onlines failed (HTTP {resp.status_code})")
    return resp.json().get("obj") or []


def _parse_json_maybe(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return {}


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
    if not admin_user or not admin_pass:
        raise HTTPException(status_code=500, detail="Admin credentials not configured")
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
    await init_db()


@app.post("/api/v1/create")
async def create_client(
    payload: CreateClientRequest,
    authorization: Optional[str] = Header(default=None),
    x_api_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    _check_auth(authorization, x_api_token)

    server = _get_panel_server(payload.server_id)
    inbound_id = server.inbound_id
    verify_tls = server.verify_tls
    timeout = _as_float(_env("REQUEST_TIMEOUT", "10"), 10.0)

    client_uuid = payload.client_id or str(uuid.uuid4())
    sub_id = payload.sub_id or secrets.token_hex(8)
    flow = payload.flow or _env("DEFAULT_FLOW", "xtls-rprx-vision")
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
        await _xui_add_client(client, server, add_client_data)
        inbound_obj = await _xui_get_inbound(client, server, inbound_id)

    stream_settings = _parse_json_maybe(inbound_obj.get("streamSettings"))
    reality_settings = _parse_json_maybe(stream_settings.get("realitySettings"))
    reality_inner = _parse_json_maybe(reality_settings.get("settings"))

    security = stream_settings.get("security")
    if security != "reality":
        raise HTTPException(
            status_code=500,
            detail="Inbound security is not set to reality",
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
    tls_settings = _parse_json_maybe(stream_settings.get("tlsSettings"))
    alpn = _extract_alpn(reality_inner, reality_settings, stream_settings, tls_settings)
    tcp_settings = _parse_json_maybe(stream_settings.get("tcpSettings"))
    header_type = _parse_json_maybe(tcp_settings.get("header")).get("type", "none")

    port = int(inbound_obj.get("port", 0)) or 0
    if port <= 0:
        raise HTTPException(status_code=500, detail="Inbound port is missing")

    vless_host = server.vless_host
    if not vless_host:
        raise HTTPException(status_code=500, detail="VLESS_HOST is not set")

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
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    _check_auth(authorization, x_api_token)

    paid_until_dt: datetime | None = None
    if payload.paid_until is not None:
        paid_until_dt = datetime.fromtimestamp(payload.paid_until, tz=timezone.utc)

    is_paid = _payment_is_paid(payload.status)
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
async def admin_dashboard(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    cpu = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(os.getcwd())

    total_clients = (await db.execute(select(func.count(Client.id)))).scalar() or 0
    total_paid = (await db.execute(select(func.count(Subscription.id)).where(Subscription.is_paid.is_(True)))).scalar() or 0

    servers = _get_panel_servers()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "cpu": cpu,
            "mem": mem,
            "disk": disk,
            "total_clients": total_clients,
            "total_paid": total_paid,
            "servers_count": len(servers),
        },
    )


@app.get("/admin/clients", response_class=HTMLResponse, dependencies=[Depends(_admin_auth)])
async def admin_clients(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    result = await db.execute(select(Client).order_by(desc(Client.id)).limit(500))
    clients = result.scalars().all()
    return templates.TemplateResponse(
        "clients.html",
        {
            "request": request,
            "clients": clients,
        },
    )


@app.get("/admin/servers", response_class=HTMLResponse, dependencies=[Depends(_admin_auth)])
async def admin_servers(request: Request) -> HTMLResponse:
    servers = _get_panel_servers()
    timeout = _as_float(_env("REQUEST_TIMEOUT", "10"), 10.0)

    async def fetch_server(server: PanelServer) -> Dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=timeout,
            verify=server.verify_tls,
            follow_redirects=True,
        ) as client:
            await _xui_login(client, server)
            inbounds = await _xui_list_inbounds(client, server)
            onlines = await _xui_get_onlines(client, server)
        return {
            "id": server.id,
            "name": server.name,
            "host": server.host,
            "port": server.port,
            "inbounds": inbounds,
            "onlines": onlines,
            "onlines_count": len(onlines),
        }

    results = await asyncio.gather(*[fetch_server(server) for server in servers])
    return templates.TemplateResponse(
        "servers.html",
        {
            "request": request,
            "servers": results,
        },
    )


@app.get("/admin/payments", response_class=HTMLResponse, dependencies=[Depends(_admin_auth)])
async def admin_payments(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    payments = (await db.execute(select(PaymentEvent).order_by(desc(PaymentEvent.id)).limit(500))).scalars().all()
    subs = (await db.execute(select(Subscription))).scalars().all()
    subs_map = {sub.tg_id: sub for sub in subs}
    return templates.TemplateResponse(
        "payments.html",
        {
            "request": request,
            "payments": payments,
            "subs_map": subs_map,
        },
    )
