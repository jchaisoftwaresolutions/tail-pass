import hashlib
import os
from typing import Optional
import httpx
from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session, select
from app.config import settings
from app.db import SystemConfig, User, get_session, utcnow


def hash_pin(pin: str) -> str:
    salted = f"{settings.secret_key}:{pin.strip()}"
    return hashlib.sha256(salted.encode("utf-8")).hexdigest()


def verify_pin(user: User, pin: str) -> bool:
    if not user.pin_hash or not pin:
        return False
    return user.pin_hash == hash_pin(pin)


async def query_tailscaled_whois(client_ip: str, client_port: int = 0) -> Optional[dict]:
    """Query the local Tailscale daemon socket `/var/run/tailscale/tailscaled.sock`."""
    socket_path = settings.tailscale_socket
    if not os.path.exists(socket_path):
        return None

    addr_param = f"{client_ip}:{client_port}" if client_port else client_ip
    try:
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        async with httpx.AsyncClient(transport=transport, timeout=2.0) as client:
            resp = await client.get(
                "http://local-tailscaled.sock/localapi/v0/whois",
                params={"addr": addr_param},
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        return None
    return None


async def resolve_current_user(
    request: Request,
    session: Session = Depends(get_session),
) -> User:
    """Resolve user via Tailscale local socket WhoIs, switcher cookie (in dev), or DEV_MOCK_USER fallback."""
    # Allow quick persona switching in Dev Mock mode via cookie
    mock_override = request.cookies.get("tailpass_mock_user")

    forwarded = request.headers.get("X-Forwarded-For")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "127.0.0.1")
    client_port = request.client.port if request.client and request.client.port else 0

    whois_data = await query_tailscaled_whois(client_ip, client_port)

    if whois_data:
        profile = whois_data.get("UserProfile") or {}
        node = whois_data.get("Node") or {}
        login_name = profile.get("LoginName") or f"node-{node.get('ComputedName', 'unknown')}@tailnet"
        display_name = profile.get("DisplayName") or login_name.split("@")[0]
        computed_name = node.get("ComputedName") or client_ip
    else:
        login_name = mock_override or settings.dev_mock_user or "admin@tailnet.local"
        display_name = login_name.split("@")[0].replace(".", " ").title()
        computed_name = settings.dev_mock_node if not mock_override else f"device-{display_name.lower().replace(' ', '-')}"

    user = session.exec(select(User).where(User.login_name == login_name)).first()
    if not user:
        # First created user defaults to admin
        any_user = session.exec(select(User)).first()
        user = User(
            login_name=login_name,
            display_name=display_name,
            node_name=computed_name,
            is_admin=(any_user is None),
            last_seen=utcnow(),
        )
        session.add(user)
        session.commit()
        session.refresh(user)
    else:
        user.node_name = computed_name
        user.last_seen = utcnow()
        session.add(user)
        session.commit()
        session.refresh(user)

    return user


def check_secondary_auth(
    request: Request,
    user: User,
    session: Session,
    supplied_pin: Optional[str] = None,
) -> bool:
    """Verify secondary PIN / Passkey requirement when enabled by Admin."""
    cfg = session.get(SystemConfig, 1)
    if not cfg or not cfg.require_pin_or_passkey:
        return True

    # Check if session cookie already verified recently
    verified_cookie = request.cookies.get("tailpass_sec_verified")
    expected_token = hashlib.sha256(f"{settings.secret_key}:verified:{user.login_name}".encode()).hexdigest()[:24]
    if verified_cookie == expected_token:
        return True

    if supplied_pin and verify_pin(user, supplied_pin):
        return True

    return False


def make_verified_cookie_token(user: User) -> str:
    return hashlib.sha256(f"{settings.secret_key}:verified:{user.login_name}".encode()).hexdigest()[:24]
