from datetime import timedelta
from typing import Optional
import uuid
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session, select
from app.auth import check_secondary_auth, resolve_current_user
from app.config import settings
from app.db import (
    Lease,
    Resource,
    TakeoverRequest,
    User,
    get_session,
    utcnow,
)
from app.drivers.acl_base import sync_control_plane_acls
from app.drivers.notifier import send_notification
from app.drivers.ssh_runner import run_ssh_hook

router = APIRouter()


@router.post("/api/seats/{resource_id}/request-access")
async def request_seat_access(
    resource_id: int,
    request: Request,
    requested_minutes: int = Form(60),
    pin: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    if not check_secondary_auth(request, user, session, pin):
        return RedirectResponse(
            url="/?msg=Secondary+PIN+/+Passkey+verification+required+to+request+Seat+takeover.&level=warning",
            status_code=303,
        )

    resource = session.get(Resource, resource_id)
    if not resource:
        return RedirectResponse(url="/?msg=Seat+not+found&level=danger", status_code=303)

    lease = session.exec(
        select(Lease).where(
            Lease.resource_id == resource.id,
            Lease.status.in_(["ACTIVE", "AWAITING_CHECKIN"]),
        )
    ).first()
    if not lease:
        return RedirectResponse(url="/?msg=Seat+is+already+free!+You+can+check+it+out+directly.&level=info", status_code=303)

    if lease.user_id == user.id:
        return RedirectResponse(url="/?msg=You+already+hold+the+active+lease+on+this+Seat.&level=info", status_code=303)

    # 1. Policy checks: adhoc vs scheduled takeover
    if lease.lease_type == "SCHEDULED" and not resource.allow_takeover_scheduled:
        return RedirectResponse(
            url=f"/?msg=Takeover+requests+are+disabled+during+scheduled+reservations+on+Seat+'{resource.name}'.&level=warning",
            status_code=303,
        )
    if lease.lease_type == "ADHOC" and not resource.allow_takeover_adhoc:
        return RedirectResponse(
            url=f"/?msg=Takeover+requests+are+disabled+for+ad-hoc+sessions+on+Seat+'{resource.name}'.&level=warning",
            status_code=303,
        )

    now = utcnow()
    # Check if there is already a PENDING_HANDOFF
    existing_pending = session.exec(
        select(TakeoverRequest).where(
            TakeoverRequest.resource_id == resource.id,
            TakeoverRequest.status == "PENDING_HANDOFF",
            TakeoverRequest.expires_at > now,
        )
    ).first()
    if existing_pending:
        return RedirectResponse(
            url="/?msg=A+handoff+request+is+already+pending+on+this+Seat.&level=warning",
            status_code=303,
        )

    # Check anti-spam cooldown if recently denied
    if resource.takeover_cooldown_minutes > 0:
        cooldown_cutoff = now - timedelta(minutes=resource.takeover_cooldown_minutes)
        recent_denied = session.exec(
            select(TakeoverRequest).where(
                TakeoverRequest.resource_id == resource.id,
                TakeoverRequest.requester_id == user.id,
                TakeoverRequest.status == "DENIED",
                TakeoverRequest.responded_at >= cooldown_cutoff,
            )
        ).first()
        if recent_denied:
            return RedirectResponse(
                url=f"/?msg=Takeover+cooldown+active+({resource.takeover_cooldown_minutes}m)+after+a+recent+decline.&level=warning",
                status_code=303,
            )

    holder = session.get(User, lease.user_id)
    req_id = uuid.uuid4().hex[:12]
    timeout_secs = max(15, resource.handoff_timeout_seconds or 60)
    expires_at = now + timedelta(seconds=timeout_secs)

    handoff = TakeoverRequest(
        request_id=req_id,
        resource_id=resource.id,
        lease_id=lease.id,
        requester_id=user.id,
        holder_id=lease.user_id,
        requested_minutes=max(15, min(requested_minutes, resource.max_checkout_minutes)),
        status="PENDING_HANDOFF",
        created_at=now,
        expires_at=expires_at,
    )
    session.add(handoff)
    session.commit()

    callback_url = f"{settings.base_url.rstrip('/')}/api/handoff/{req_id}/respond"

    # Dispatch notification to current holder
    if holder:
        await send_notification(
            session,
            holder,
            f"Seat Handoff Request: {resource.name}",
            f"{user.display_name} ({user.login_name}) is requesting Seat '{resource.name}' ({timeout_secs}s timer). Respond in Tail-Pass.",
            level="warning",
            action_url=settings.base_url,
        )

    # Execute SSH OSD hook if host_verified and enable_osd_notify
    if resource.host_verified and resource.enable_osd_notify:
        await run_ssh_hook(
            resource,
            resource.cmd_osd_notify,
            {
                "requester": user.display_name,
                "requester_name": user.display_name,
                "timeout_seconds": timeout_secs,
                "minutes_left": int( max(0, (lease.expires_at - now).total_seconds() // 60) ),
                "request_id": req_id,
                "callback_url": callback_url,
            },
        )

    return RedirectResponse(
        url=f"/?msg=Handoff+request+sent+to+{holder.display_name if holder else 'current user'}!+Waiting+({timeout_secs}s+countdown)...&level=info",
        status_code=303,
    )


@router.post("/api/handoff/{request_id}/respond")
async def respond_handoff(
    request_id: str,
    request: Request,
    action: str = Form(None),
    session: Session = Depends(get_session),
):
    """
    Current User or Host Connector callback endpoint:
    action in ("accept", "deny", "extend_10m").
    Supports both Form POST (from UI) and JSON/Query POST (from SSH OSD script callback).
    """
    if not action:
        action = request.query_params.get("action", "")
        if not action:
            try:
                body = await request.json()
                action = body.get("action", "")
            except Exception:
                action = ""

    action = action.strip().lower()
    handoff = session.exec(select(TakeoverRequest).where(TakeoverRequest.request_id == request_id)).first()
    if not handoff:
        return JSONResponse({"ok": False, "error": "Handoff request not found"}, status_code=404)

    if handoff.status != "PENDING_HANDOFF":
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(url=f"/?msg=Handoff+already+resolved+({handoff.status})&level=info", status_code=303)
        return JSONResponse({"ok": False, "error": f"Handoff already {handoff.status}"}, status_code=400)

    now = utcnow()
    resource = session.get(Resource, handoff.resource_id)
    lease = session.get(Lease, handoff.lease_id)
    requester = session.get(User, handoff.requester_id)
    holder = session.get(User, handoff.holder_id)

    holder_name = holder.display_name if holder else "Current user"
    seat_name = resource.name if resource else "Seat"

    if action == "accept":
        handoff.status = "ACCEPTED"
        handoff.responded_at = now
        session.add(handoff)

        if lease and lease.status in ("ACTIVE", "AWAITING_CHECKIN"):
            lease.status = "ENDED"
            session.add(lease)

        if resource and requester:
            new_lease = Lease(
                resource_id=resource.id,
                user_id=requester.id,
                lease_type="ADHOC",
                status="ACTIVE",
                started_at=now,
                expires_at=now + timedelta(minutes=min(handoff.requested_minutes, resource.max_checkout_minutes)),
                checked_in=True,
            )
            session.add(new_lease)

        session.commit()
        await sync_control_plane_acls(session)

        if requester:
            await send_notification(
                session,
                requester,
                f"Access Granted! ({seat_name})",
                f"{holder_name} accepted your handoff request for '{seat_name}'. Tailnet ACL access is now open for your devices!",
                level="success",
            )
        msg = f"Handoff accepted! Transferred Seat '{seat_name}' to {requester.display_name if requester else 'requester'}."

    elif action == "deny":
        handoff.status = "DENIED"
        handoff.responded_at = now
        session.add(handoff)
        session.commit()

        if requester:
            await send_notification(
                session,
                requester,
                f"Request Declined ({seat_name})",
                f"{holder_name} declined the request (busy).",
                level="warning",
            )
        msg = f"Declined handoff request. Cooldown timer ({resource.takeover_cooldown_minutes if resource else 15}m) started."

    elif action == "extend_10m":
        handoff.status = "EXTENDED"
        handoff.responded_at = now
        session.add(handoff)
        if lease:
            # Ensure at least 10m from now if expiring sooner
            min_exp = now + timedelta(minutes=10)
            if lease.expires_at < min_exp:
                lease.expires_at = min_exp
                session.add(lease)
        session.commit()

        if requester:
            await send_notification(
                session,
                requester,
                f"10 More Minutes Requested ({seat_name})",
                f"{holder_name} requested 10 more minutes to wrap up.",
                level="info",
            )
        msg = "Notified requester that you need 10 more minutes to wrap up."

    else:
        return JSONResponse({"ok": False, "error": "Invalid action. Expected accept, deny, or extend_10m."}, status_code=400)

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url=f"/?msg={msg.replace(' ', '+')}&level=info", status_code=303)
    return JSONResponse({"ok": True, "status": handoff.status, "message": msg})


@router.post("/api/seats/{resource_id}/heartbeat")
async def seat_telemetry_heartbeat(
    resource_id: int,
    session: Session = Depends(get_session),
):
    """Optional host/controller telemetry heartbeat endpoint that marks AWAITING_CHECKIN leases as checked in."""
    lease = session.exec(
        select(Lease).where(
            Lease.resource_id == resource_id,
            Lease.status.in_(["ACTIVE", "AWAITING_CHECKIN"]),
        )
    ).first()
    if not lease:
        return {"ok": False, "detail": "No active lease"}

    lease.last_heartbeat_at = utcnow()
    if lease.status == "AWAITING_CHECKIN" or not lease.checked_in:
        lease.status = "ACTIVE"
        lease.checked_in = True
    session.add(lease)
    session.commit()
    return {"ok": True, "lease_id": lease.id, "status": lease.status}
