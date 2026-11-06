import asyncio
from datetime import timedelta
from sqlmodel import Session, select
from app.config import settings
from app.db import (
    Lease,
    Resource,
    ScheduleBlock,
    TakeoverRequest,
    User,
    engine,
    utcnow,
)
from app.drivers.acl_base import sync_control_plane_acls
from app.drivers.notifier import send_notification
from app.drivers.ssh_runner import run_ssh_hook


async def scheduler_tick() -> None:
    """Single tick of the Tail-Pass reservation, warning, no-show forfeit, and handoff timeout engine."""
    now = utcnow()
    acl_dirty = False

    with Session(engine) as session:
        # 1. Expire finished leases
        active_leases = session.exec(
            select(Lease).where(Lease.status.in_(["ACTIVE", "AWAITING_CHECKIN"]))
        ).all()

        for lease in active_leases:
            resource = session.get(Resource, lease.resource_id)
            holder = session.get(User, lease.user_id)
            if not resource or not holder:
                continue

            # Check regular expiration
            if now >= lease.expires_at:
                lease.status = "ENDED"
                session.add(lease)
                if lease.schedule_block_id:
                    blk = session.get(ScheduleBlock, lease.schedule_block_id)
                    if blk and blk.status == "ACTIVE":
                        blk.status = "COMPLETED"
                        session.add(blk)
                acl_dirty = True
                await send_notification(
                    session,
                    holder,
                    f"Seat Checkout Ended: {resource.name}",
                    f"Your checkout session on Seat '{resource.name}' ({resource.tailnet_target}) has expired and ACL access was closed.",
                    level="info",
                )
                continue

            # Check No-Show Forfeit Window for scheduled sessions in AWAITING_CHECKIN
            if (
                lease.status == "AWAITING_CHECKIN"
                and not lease.checked_in
                and resource.no_show_forfeit_minutes > 0
            ):
                forfeit_deadline = lease.started_at + timedelta(minutes=resource.no_show_forfeit_minutes)
                if now >= forfeit_deadline:
                    lease.status = "FORFEITED"
                    session.add(lease)
                    if lease.schedule_block_id:
                        blk = session.get(ScheduleBlock, lease.schedule_block_id)
                        if blk:
                            blk.status = "FORFEITED"
                            session.add(blk)
                    acl_dirty = True
                    await send_notification(
                        session,
                        holder,
                        f"Seat Reservation Forfeited: {resource.name}",
                        f"No check-in or telemetry within {resource.no_show_forfeit_minutes}m for Seat '{resource.name}'. Seat has been freed.",
                        level="warning",
                    )
                    continue

            # Check Idle Telemetry Auto-Release if verified & enabled
            if (
                lease.status == "ACTIVE"
                and resource.host_verified
                and resource.enable_idle_release
                and (now - lease.last_heartbeat_at).total_seconds() >= 300
            ):
                ok, exit_code, _ = await run_ssh_hook(
                    resource,
                    resource.cmd_idle_check,
                    {"user": holder.login_name, "seat": resource.name},
                )
                lease.last_heartbeat_at = now
                session.add(lease)
                if exit_code == 1:
                    lease.status = "ENDED"
                    session.add(lease)
                    acl_dirty = True
                    await send_notification(
                        session,
                        holder,
                        f"Idle Seat Released: {resource.name}",
                        f"Host telemetry reported Seat '{resource.name}' as idle. Lease was automatically released.",
                        level="info",
                    )
                    continue

            # Check warnings at T-10m and T-5m (either before lease end or before an upcoming scheduled block)
            minutes_left = (lease.expires_at - now).total_seconds() / 60.0
            if 5.0 < minutes_left <= 10.0 and not lease.warned_10m:
                lease.warned_10m = True
                session.add(lease)
                msg = f"10 minutes remaining on Seat '{resource.name}' ({resource.tailnet_target})."
                await send_notification(session, holder, f"10m Warning: {resource.name}", msg, level="warning")
                if resource.host_verified and resource.enable_osd_notify:
                    await run_ssh_hook(
                        resource,
                        resource.cmd_osd_notify,
                        {
                            "requester": "System Timer",
                            "minutes_left": 10,
                            "timeout_seconds": 600,
                            "request_id": f"warn10-{lease.id}",
                            "callback_url": settings.base_url,
                        },
                    )
            elif 0 < minutes_left <= 5.0 and not lease.warned_5m:
                lease.warned_5m = True
                session.add(lease)
                msg = f"5 minutes remaining on Seat '{resource.name}' ({resource.tailnet_target}). Wrap up soon!"
                await send_notification(session, holder, f"5m Warning: {resource.name}", msg, level="danger")
                if resource.host_verified and resource.enable_osd_notify:
                    await run_ssh_hook(
                        resource,
                        resource.cmd_osd_notify,
                        {
                            "requester": "System Timer",
                            "minutes_left": 5,
                            "timeout_seconds": 300,
                            "request_id": f"warn5-{lease.id}",
                            "callback_url": settings.base_url,
                        },
                    )

        # 2. Scheduled Priority Rule: Start due ScheduleBlocks and preempt any overlapping Ad-Hoc Lease
        due_blocks = session.exec(
            select(ScheduleBlock).where(
                ScheduleBlock.status == "SCHEDULED",
                ScheduleBlock.start_time <= now,
                ScheduleBlock.end_time > now,
            )
        ).all()

        for block in due_blocks:
            resource = session.get(Resource, block.resource_id)
            sched_user = session.get(User, block.user_id)
            if not resource or not sched_user:
                continue

            # Preempt any existing active lease on this Seat
            current_leases = session.exec(
                select(Lease).where(
                    Lease.resource_id == resource.id,
                    Lease.status.in_(["ACTIVE", "AWAITING_CHECKIN"]),
                )
            ).all()
            for cur in current_leases:
                if cur.schedule_block_id == block.id:
                    continue
                old_holder = session.get(User, cur.user_id)
                cur.status = "EVICTED"
                session.add(cur)
                if old_holder:
                    await send_notification(
                        session,
                        old_holder,
                        f"Scheduled Priority Preemption: {resource.name}",
                        f"Your session on '{resource.name}' ended because {sched_user.display_name}'s scheduled reservation started.",
                        level="danger",
                    )

            # Activate the scheduled block into a Lease
            block.status = "ACTIVE"
            session.add(block)

            initial_status = (
                "AWAITING_CHECKIN"
                if (resource.no_show_forfeit_minutes > 0 and not block.checked_in)
                else "ACTIVE"
            )
            new_lease = Lease(
                resource_id=resource.id,
                user_id=sched_user.id,
                schedule_block_id=block.id,
                lease_type="SCHEDULED",
                status=initial_status,
                started_at=now,
                expires_at=block.end_time,
                checked_in=block.checked_in,
            )
            session.add(new_lease)
            acl_dirty = True

            checkin_note = (
                f" Please click 'Check In / Start Session' within {resource.no_show_forfeit_minutes} minutes."
                if initial_status == "AWAITING_CHECKIN"
                else " Your Tailnet ACL access is now open!"
            )
            await send_notification(
                session,
                sched_user,
                f"Scheduled Seat Ready: {resource.name}",
                f"Your reserved timeslot for '{resource.name}' ({resource.tailnet_target}) has started.{checkin_note}",
                level="success",
            )

        # 3. Process Pending Takeover / Handoff Timeouts ("Mode A" Checkbox vs Unchecked)
        expired_handoffs = session.exec(
            select(TakeoverRequest).where(
                TakeoverRequest.status == "PENDING_HANDOFF",
                TakeoverRequest.expires_at <= now,
            )
        ).all()

        for req in expired_handoffs:
            resource = session.get(Resource, req.resource_id)
            lease = session.get(Lease, req.lease_id)
            requester = session.get(User, req.requester_id)
            holder = session.get(User, req.holder_id)

            if not resource or not requester or not holder:
                req.status = "UNANSWERED"
                session.add(req)
                continue

            if resource.auto_evict_on_timeout and lease and lease.status in ("ACTIVE", "AWAITING_CHECKIN"):
                # Mode A Checked: Auto-evict current holder & transfer lease/ACL to requester
                req.status = "TIMEOUT_EVICTED"
                req.responded_at = now
                session.add(req)

                lease.status = "EVICTED"
                session.add(lease)

                new_lease = Lease(
                    resource_id=resource.id,
                    user_id=requester.id,
                    lease_type="ADHOC",
                    status="ACTIVE",
                    started_at=now,
                    expires_at=now + timedelta(minutes=min(req.requested_minutes, resource.max_checkout_minutes)),
                    checked_in=True,
                )
                session.add(new_lease)
                acl_dirty = True

                if resource.host_verified and resource.enable_local_evict_cmd:
                    await run_ssh_hook(
                        resource,
                        resource.cmd_evict,
                        {
                            "requester": requester.display_name,
                            "holder": holder.display_name,
                            "request_id": req.request_id,
                            "callback_url": settings.base_url,
                        },
                    )

                await send_notification(
                    session,
                    requester,
                    f"Access Granted (Auto-Evict): {resource.name}",
                    f"No response from {holder.display_name} before timeout. Seat '{resource.name}' and ACLs transferred to you!",
                    level="success",
                )
                await send_notification(
                    session,
                    holder,
                    f"Seat Evicted on Handoff Timeout: {resource.name}",
                    f"Handoff request from {requester.display_name} timed out with auto-evict enabled.",
                    level="danger",
                )
            else:
                # Mode A Unchecked: Leave current user connected, mark UNANSWERED
                req.status = "UNANSWERED"
                req.responded_at = now
                session.add(req)

                await send_notification(
                    session,
                    requester,
                    f"No Response: {resource.name}",
                    f"No response from {holder.display_name}. Auto-disconnect is disabled for this resource.",
                    level="warning",
                )

        session.commit()

        if acl_dirty:
            await sync_control_plane_acls(session)


async def run_scheduler_loop(stop_event: asyncio.Event) -> None:
    """Background asyncio loop running scheduler_tick periodically."""
    while not stop_event.is_set():
        try:
            await scheduler_tick()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.scheduler_interval_seconds)
        except asyncio.TimeoutError:
            pass
