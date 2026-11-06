from datetime import datetime, timedelta
from typing import Optional
import markdown
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from app.auth import (
    check_secondary_auth,
    hash_pin,
    make_verified_cookie_token,
    resolve_current_user,
    verify_pin,
)
from app.db import (
    Lease,
    NotificationEvent,
    Resource,
    ScheduleBlock,
    SystemConfig,
    TakeoverRequest,
    User,
    get_session,
    utcnow,
)
from app.drivers.acl_base import sync_control_plane_acls
from app.drivers.notifier import send_notification

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _render_md(text: str) -> str:
    if not text:
        return ""
    return markdown.markdown(text, extensions=["fenced_code", "tables"])


templates.env.filters["markdown"] = _render_md


def _build_dashboard_context(
    request: Request,
    session: Session,
    user: User,
    flash_msg: Optional[str] = None,
    flash_level: str = "info",
) -> dict:
    now = utcnow()
    cfg = session.get(SystemConfig, 1)
    resources = session.exec(select(Resource).order_by(Resource.id)).all()
    users_by_id = {u.id: u for u in session.exec(select(User)).all()}

    active_leases = session.exec(
        select(Lease).where(Lease.status.in_(["ACTIVE", "AWAITING_CHECKIN"]))
    ).all()
    lease_by_resource = {l.resource_id: l for l in active_leases}

    upcoming_blocks = session.exec(
        select(ScheduleBlock)
        .where(
            ScheduleBlock.status.in_(["SCHEDULED", "ACTIVE"]),
            ScheduleBlock.end_time > now,
        )
        .order_by(ScheduleBlock.start_time)
    ).all()

    pending_handoffs = session.exec(
        select(TakeoverRequest).where(
            TakeoverRequest.status == "PENDING_HANDOFF",
            TakeoverRequest.expires_at > now,
        )
    ).all()
    handoff_by_resource = {h.resource_id: h for h in pending_handoffs}

    seat_cards = []
    for res in resources:
        lease = lease_by_resource.get(res.id)
        holder = users_by_id.get(lease.user_id) if lease else None
        mins_left = max(0, int((lease.expires_at - now).total_seconds() // 60)) if lease else 0
        res_blocks = [b for b in upcoming_blocks if b.resource_id == res.id]
        next_block = res_blocks[0] if res_blocks else None
        next_block_user = users_by_id.get(next_block.user_id) if next_block else None
        spectators = (
            [s.strip() for s in lease.spectator_logins_csv.split(",") if s.strip()]
            if lease and lease.spectator_logins_csv
            else []
        )
        handoff = handoff_by_resource.get(res.id)
        handoff_requester = users_by_id.get(handoff.requester_id) if handoff else None
        handoff_secs_left = max(0, int((handoff.expires_at - now).total_seconds())) if handoff else 0

        seat_cards.append(
            {
                "resource": res,
                "lease": lease,
                "holder": holder,
                "mins_left": mins_left,
                "next_block": next_block,
                "next_block_user": next_block_user,
                "schedule_blocks": res_blocks,
                "spectators": spectators,
                "handoff": handoff,
                "handoff_requester": handoff_requester,
                "handoff_secs_left": handoff_secs_left,
            }
        )

    # Active handoff targeting current user (holder) or requested by current user
    incoming_handoff = next((c for c in seat_cards if c["handoff"] and c["handoff"].holder_id == user.id), None)
    outgoing_handoff = next((c for c in seat_cards if c["handoff"] and c["handoff"].requester_id == user.id), None)

    recent_notifications = session.exec(
        select(NotificationEvent)
        .where((NotificationEvent.target_user_id == user.id) | (NotificationEvent.target_user_id.is_(None)))
        .order_by(NotificationEvent.id.desc())
        .limit(6)
    ).all()

    return {
        "request": request,
        "user": user,
        "cfg": cfg,
        "seat_cards": seat_cards,
        "upcoming_blocks": upcoming_blocks,
        "users_by_id": users_by_id,
        "incoming_handoff": incoming_handoff,
        "outgoing_handoff": outgoing_handoff,
        "recent_notifications": recent_notifications,
        "now": now,
        "flash_msg": flash_msg or request.query_params.get("msg"),
        "flash_level": request.query_params.get("level", flash_level),
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard_home(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    ctx = _build_dashboard_context(request, session, user)
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/partials/live-state", response_class=HTMLResponse)
async def live_state_partial(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    ctx = _build_dashboard_context(request, session, user)
    return templates.TemplateResponse(request, "partials/live_state.html", ctx)


@router.post("/seats/{resource_id}/checkout")
async def checkout_seat(
    resource_id: int,
    request: Request,
    duration_minutes: int = Form(60),
    pin: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    if not check_secondary_auth(request, user, session, pin):
        return RedirectResponse(
            url="/?msg=Secondary+PIN+/+Passkey+verification+required+before+checking+out+a+Seat.&level=warning",
            status_code=303,
        )

    resource = session.get(Resource, resource_id)
    if not resource:
        return RedirectResponse(url="/?msg=Seat+not+found&level=danger", status_code=303)

    now = utcnow()
    # Ensure Seat isn't already in use
    existing = session.exec(
        select(Lease).where(
            Lease.resource_id == resource.id,
            Lease.status.in_(["ACTIVE", "AWAITING_CHECKIN"]),
        )
    ).first()
    if existing:
        return RedirectResponse(
            url=f"/?msg=Seat+'{resource.name}'+is+currently+in+use.+Use+Request+Access+to+initiate+a+handoff.&level=warning",
            status_code=303,
        )

    requested = max(5, min(duration_minutes, resource.max_checkout_minutes))
    desired_end = now + timedelta(minutes=requested)

    # Check calendar for conflicting or upcoming scheduled reservations
    overlapping_now = session.exec(
        select(ScheduleBlock).where(
            ScheduleBlock.resource_id == resource.id,
            ScheduleBlock.status.in_(["SCHEDULED", "ACTIVE"]),
            ScheduleBlock.start_time <= now,
            ScheduleBlock.end_time > now,
        )
    ).first()
    if overlapping_now:
        sched_holder = session.get(User, overlapping_now.user_id)
        holder_name = sched_holder.display_name if sched_holder else "another user"
        if overlapping_now.user_id != user.id:
            start_s = overlapping_now.start_time.strftime("%H:%M UTC")
            end_s = overlapping_now.end_time.strftime("%H:%M UTC")
            return RedirectResponse(
                url=f"/?msg=Reserved+by+{holder_name}+from+{start_s}+to+{end_s}.+Contact+{holder_name}+or+an+Admin+to+request+schedule+removal.&level=danger",
                status_code=303,
            )

    next_scheduled = session.exec(
        select(ScheduleBlock)
        .where(
            ScheduleBlock.resource_id == resource.id,
            ScheduleBlock.status == "SCHEDULED",
            ScheduleBlock.start_time > now,
            ScheduleBlock.start_time < desired_end,
        )
        .order_by(ScheduleBlock.start_time)
    ).first()

    cap_notice = ""
    if next_scheduled:
        sched_holder = session.get(User, next_scheduled.user_id)
        holder_name = sched_holder.display_name if sched_holder else "another user"
        # Cap 5 minutes before the scheduled reservation starts (or at start_time if tight)
        buffer_end = next_scheduled.start_time - timedelta(minutes=5)
        avail_mins = int((buffer_end - now).total_seconds() // 60)
        if avail_mins < 5:
            start_s = next_scheduled.start_time.strftime("%H:%M UTC")
            end_s = next_scheduled.end_time.strftime("%H:%M UTC")
            return RedirectResponse(
                url=f"/?msg=Reserved+by+{holder_name}+from+{start_s}+to+{end_s}.+Contact+{holder_name}+or+an+Admin+to+request+schedule+removal.&level=warning",
                status_code=303,
            )
        desired_end = now + timedelta(minutes=avail_mins)
        cap_notice = f" (Capped to {avail_mins}m due to upcoming reservation by {holder_name})"

    lease = Lease(
        resource_id=resource.id,
        user_id=user.id,
        lease_type="ADHOC",
        status="ACTIVE",
        started_at=now,
        expires_at=desired_end,
        checked_in=True,
    )
    session.add(lease)
    session.commit()

    await sync_control_plane_acls(session)
    await send_notification(
        session,
        user,
        f"Seat Checked Out: {resource.name}",
        f"ACL access opened for {user.login_name} -> {resource.tailnet_target}{cap_notice}.",
        level="success",
    )

    return RedirectResponse(
        url=f"/?msg=Seat+'{resource.name}'+checked+out!+Tailnet+ACL+opened{cap_notice}.&level=success",
        status_code=303,
    )


@router.post("/seats/{resource_id}/release")
async def release_seat(
    resource_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    lease = session.exec(
        select(Lease).where(
            Lease.resource_id == resource_id,
            Lease.status.in_(["ACTIVE", "AWAITING_CHECKIN"]),
        )
    ).first()
    if not lease:
        return RedirectResponse(url="/?msg=No+active+Seat+session+to+release&level=info", status_code=303)

    if lease.user_id != user.id and not user.is_admin:
        return RedirectResponse(url="/?msg=Only+the+active+leaseholder+or+Admin+can+release+this+Seat&level=danger", status_code=303)

    lease.status = "ENDED"
    session.add(lease)
    if lease.schedule_block_id:
        blk = session.get(ScheduleBlock, lease.schedule_block_id)
        if blk and blk.status == "ACTIVE":
            blk.status = "COMPLETED"
            session.add(blk)
    session.commit()

    await sync_control_plane_acls(session)
    return RedirectResponse(url="/?msg=Seat+released+and+Tailnet+ACL+closed.&level=info", status_code=303)


@router.post("/seats/{resource_id}/checkin")
async def checkin_scheduled_seat(
    resource_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    lease = session.exec(
        select(Lease).where(
            Lease.resource_id == resource_id,
            Lease.user_id == user.id,
            Lease.status.in_(["AWAITING_CHECKIN", "ACTIVE"]),
        )
    ).first()
    if not lease:
        return RedirectResponse(url="/?msg=No+scheduled+Seat+awaiting+check-in&level=warning", status_code=303)

    lease.status = "ACTIVE"
    lease.checked_in = True
    lease.last_heartbeat_at = utcnow()
    session.add(lease)
    if lease.schedule_block_id:
        blk = session.get(ScheduleBlock, lease.schedule_block_id)
        if blk:
            blk.checked_in = True
            session.add(blk)
    session.commit()
    await sync_control_plane_acls(session)
    return RedirectResponse(url="/?msg=Checked+in!+Enjoy+your+Seat+session.&level=success", status_code=303)


@router.post("/seats/{resource_id}/watch-party/toggle")
async def toggle_watch_party(
    resource_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    resource = session.get(Resource, resource_id)
    lease = session.exec(
        select(Lease).where(
            Lease.resource_id == resource_id,
            Lease.status.in_(["ACTIVE", "AWAITING_CHECKIN"]),
        )
    ).first()
    if not resource or not lease or (lease.user_id != user.id and not user.is_admin):
        return RedirectResponse(url="/?msg=Not+authorized+to+toggle+Watch+Party&level=danger", status_code=303)

    if not resource.allow_watch_party:
        return RedirectResponse(url="/?msg=Watch+Party+is+disabled+by+policy+for+this+Seat&level=warning", status_code=303)

    lease.watch_party_open = not lease.watch_party_open
    if not lease.watch_party_open:
        lease.spectator_logins_csv = ""
    session.add(lease)
    session.commit()
    await sync_control_plane_acls(session)

    state_str = "opened" if lease.watch_party_open else "closed"
    return RedirectResponse(url=f"/?msg=Watch+Party+/+Co-Op+{state_str}!&level=info", status_code=303)


@router.post("/seats/{resource_id}/watch-party/join")
async def join_watch_party(
    resource_id: int,
    admin_mirror: bool = Form(False),
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    resource = session.get(Resource, resource_id)
    lease = session.exec(
        select(Lease).where(
            Lease.resource_id == resource_id,
            Lease.status.in_(["ACTIVE", "AWAITING_CHECKIN"]),
        )
    ).first()
    if not resource or not lease:
        return RedirectResponse(url="/?msg=No+active+session+to+join&level=warning", status_code=303)

    if admin_mirror:
        if not (user.is_admin and resource.allow_admin_mirror):
            return RedirectResponse(url="/?msg=Admin+Mirror+not+permitted&level=danger", status_code=303)
        lease.watch_party_open = True
    else:
        if not (resource.allow_watch_party and lease.watch_party_open):
            return RedirectResponse(url="/?msg=Watch+Party+is+not+open+on+this+Seat&level=warning", status_code=303)

    spectators = [s.strip() for s in lease.spectator_logins_csv.split(",") if s.strip()]
    if user.login_name not in spectators:
        spectators.append(user.login_name)
    lease.spectator_logins_csv = ",".join(spectators)
    session.add(lease)
    session.commit()

    await sync_control_plane_acls(session)
    mode_label = "Admin Mirror" if admin_mirror else "Watch Party / Co-Op"
    return RedirectResponse(
        url=f"/?msg=Joined+{mode_label}!+Temporary+ACL+access+granted+for+{user.login_name}.&level=success",
        status_code=303,
    )


@router.post("/schedule/create")
async def create_schedule_block(
    request: Request,
    resource_id: int = Form(...),
    start_iso: str = Form(...),
    duration_minutes: int = Form(60),
    title: str = Form("Reserved Seat"),
    pin: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    if not check_secondary_auth(request, user, session, pin):
        return RedirectResponse(
            url="/?msg=Secondary+PIN+/+Passkey+verification+required+to+book+a+Seat+schedule.&level=warning",
            status_code=303,
        )

    resource = session.get(Resource, resource_id)
    if not resource:
        return RedirectResponse(url="/?msg=Seat+not+found&level=danger", status_code=303)

    try:
        start_dt = datetime.fromisoformat(start_iso.replace("Z", ""))
    except ValueError:
        return RedirectResponse(url="/?msg=Invalid+start+time+format&level=danger", status_code=303)

    dur = max(15, min(duration_minutes, resource.max_checkout_minutes))
    end_dt = start_dt + timedelta(minutes=dur)

    # Check for schedule conflicts
    conflict = session.exec(
        select(ScheduleBlock).where(
            ScheduleBlock.resource_id == resource.id,
            ScheduleBlock.status.in_(["SCHEDULED", "ACTIVE"]),
            ScheduleBlock.start_time < end_dt,
            ScheduleBlock.end_time > start_dt,
        )
    ).first()
    if conflict:
        holder = session.get(User, conflict.user_id)
        holder_name = holder.display_name if holder else "another user"
        s_str = conflict.start_time.strftime("%Y-%m-%d %H:%M UTC")
        e_str = conflict.end_time.strftime("%H:%M UTC")
        return RedirectResponse(
            url=f"/?msg=Reserved+by+{holder_name}+from+{s_str}+to+{e_str}.+Contact+{holder_name}+or+an+Admin+to+request+schedule+removal.&level=danger",
            status_code=303,
        )

    block = ScheduleBlock(
        resource_id=resource.id,
        user_id=user.id,
        title=title.strip() or "Reserved Seat",
        start_time=start_dt,
        end_time=end_dt,
        status="SCHEDULED",
    )
    session.add(block)
    session.commit()
    return RedirectResponse(
        url=f"/?msg=Scheduled+Seat+'{resource.name}'+for+{start_dt.strftime('%b %d %H:%M UTC')}+({dur}m).&level=success",
        status_code=303,
    )


@router.post("/schedule/{block_id}/cancel")
async def cancel_schedule_block(
    block_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    block = session.get(ScheduleBlock, block_id)
    if not block:
        return RedirectResponse(url="/?msg=Reservation+not+found&level=warning", status_code=303)

    if block.user_id != user.id and not user.is_admin:
        return RedirectResponse(url="/?msg=Only+the+reservation+owner+or+Admin+can+cancel+this+slot&level=danger", status_code=303)

    block.status = "CANCELLED"
    session.add(block)
    session.commit()
    return RedirectResponse(url="/?msg=Scheduled+Seat+timeslot+cancelled.&level=info", status_code=303)


@router.post("/auth/pin")
async def set_or_verify_pin(
    request: Request,
    pin: str = Form(...),
    mode: str = Form("verify"),  # set or verify
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    pin_clean = pin.strip()
    if len(pin_clean) < 4 or len(pin_clean) > 8:
        return RedirectResponse(url="/?msg=PIN+must+be+4+to+8+digits&level=warning", status_code=303)

    if mode == "set" or not user.pin_hash:
        user.pin_hash = hash_pin(pin_clean)
        session.add(user)
        session.commit()
        resp = RedirectResponse(url="/?msg=PIN+registered+and+session+verified!&level=success", status_code=303)
        resp.set_cookie("tailpass_sec_verified", make_verified_cookie_token(user), max_age=3600 * 12, httponly=True)
        return resp

    if verify_pin(user, pin_clean):
        resp = RedirectResponse(url="/?msg=Secondary+PIN+verified+for+this+session!&level=success", status_code=303)
        resp.set_cookie("tailpass_sec_verified", make_verified_cookie_token(user), max_age=3600 * 12, httponly=True)
        return resp

    return RedirectResponse(url="/?msg=Invalid+PIN+entered&level=danger", status_code=303)


@router.post("/dev/switch-user")
async def dev_switch_user(
    login_name: str = Form(...),
    is_admin: bool = Form(False),
    session: Session = Depends(get_session),
):
    """Helper in Dev/Mock mode to test multi-user Seat checkout, Watch Party, and Handoff countdowns."""
    clean_login = login_name.strip().lower()
    user = session.exec(select(User).where(User.login_name == clean_login)).first()
    if not user:
        user = User(
            login_name=clean_login,
            display_name=clean_login.split("@")[0].replace(".", " ").title(),
            node_name=f"node-{clean_login.split('@')[0]}",
            is_admin=is_admin,
        )
        session.add(user)
        session.commit()
    resp = RedirectResponse(url=f"/?msg=Switched+active+Tailnet+persona+to+{clean_login}&level=info", status_code=303)
    resp.set_cookie("tailpass_mock_user", clean_login, max_age=86400 * 7)
    return resp
