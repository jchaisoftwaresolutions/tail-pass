from typing import Optional
import httpx
from sqlmodel import Session
from app.db import NotificationEvent, SystemConfig, User


async def send_notification(
    session: Session,
    target_user: Optional[User],
    title: str,
    message: str,
    level: str = "info",
    action_url: str = "",
) -> None:
    """
    Record an in-app notification event (for live HTMX/PWA banners & browser notifications)
    and dispatch to external configured channels (ntfy, custom webhook, Apprise).
    """
    event = NotificationEvent(
        target_user_id=target_user.id if target_user else None,
        title=title,
        message=message,
        level=level,
        action_url=action_url,
    )
    session.add(event)
    session.commit()

    cfg = session.get(SystemConfig, 1)
    if not cfg:
        return

    # 1. Dispatch to ntfy if configured
    if cfg.ntfy_url and cfg.ntfy_topic:
        ntfy_endpoint = f"{cfg.ntfy_url.rstrip('/')}/{cfg.ntfy_topic.strip('/')}"
        headers = {"Title": title, "Priority": "high" if level in ("warning", "danger") else "default"}
        if action_url:
            headers["Click"] = action_url
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                await client.post(ntfy_endpoint, content=message.encode("utf-8"), headers=headers)
        except Exception:
            pass

    # 2. Dispatch to custom webhook if configured
    if cfg.custom_webhook_url:
        payload = {
            "user": target_user.login_name if target_user else "broadcast",
            "title": title,
            "message": message,
            "level": level,
            "action_url": action_url,
        }
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                await client.post(cfg.custom_webhook_url, json=payload)
        except Exception:
            pass
