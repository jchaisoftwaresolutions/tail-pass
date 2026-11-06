from typing import Optional
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from app.auth import check_secondary_auth, resolve_current_user
from app.db import Resource, SystemConfig, User, get_session
from app.drivers.acl_base import sync_control_plane_acls
from app.drivers.ssh_runner import push_helper_script, test_ssh_connection

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
async def admin_panel(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    cfg = session.get(SystemConfig, 1)
    resources = session.exec(select(Resource).order_by(Resource.id)).all()
    users = session.exec(select(User).order_by(User.id)).all()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "request": request,
            "user": user,
            "cfg": cfg,
            "resources": resources,
            "users": users,
            "flash_msg": request.query_params.get("msg"),
            "flash_level": request.query_params.get("level", "info"),
        },
    )


@router.post("/system-config")
async def update_system_config(
    request: Request,
    require_pin_or_passkey: bool = Form(False),
    control_plane_mode: str = Form("mock"),
    tailscale_tailnet: str = Form("-"),
    tailscale_api_key: str = Form(""),
    headscale_url: str = Form(""),
    headscale_api_key: str = Form(""),
    ntfy_url: str = Form(""),
    ntfy_topic: str = Form(""),
    apprise_urls: str = Form(""),
    custom_webhook_url: str = Form(""),
    pin: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    if not check_secondary_auth(request, user, session, pin):
        return RedirectResponse(
            url="/admin?msg=Secondary+PIN+/+Passkey+verification+required+for+Admin+settings.&level=warning",
            status_code=303,
        )

    cfg = session.get(SystemConfig, 1)
    if not cfg:
        cfg = SystemConfig(id=1)

    cfg.require_pin_or_passkey = require_pin_or_passkey
    cfg.control_plane_mode = control_plane_mode
    cfg.tailscale_tailnet = tailscale_tailnet.strip() or "-"
    if tailscale_api_key.strip():
        cfg.tailscale_api_key = tailscale_api_key.strip()
    cfg.headscale_url = headscale_url.strip()
    if headscale_api_key.strip():
        cfg.headscale_api_key = headscale_api_key.strip()
    cfg.ntfy_url = ntfy_url.strip()
    cfg.ntfy_topic = ntfy_topic.strip()
    cfg.apprise_urls = apprise_urls.strip()
    cfg.custom_webhook_url = custom_webhook_url.strip()

    session.add(cfg)
    session.commit()

    ok, sync_msg = await sync_control_plane_acls(session)
    lvl = "success" if ok else "warning"
    return RedirectResponse(
        url=f"/admin?msg=System+Configuration+Saved.+({sync_msg})&level={lvl}",
        status_code=303,
    )


@router.post("/seats/create")
async def create_seat(
    name: str = Form(...),
    resource_type: str = Form("Moonlight/Sunshine"),
    tailnet_target: str = Form("100.64.0.10"),
    allowed_ports: str = Form("*"),
    connection_uri: str = Form(""),
    connection_instructions: str = Form(""),
    max_checkout_minutes: int = Form(240),
    no_show_forfeit_minutes: int = Form(15),
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    seat = Resource(
        name=name.strip(),
        resource_type=resource_type,
        tailnet_target=tailnet_target.strip(),
        allowed_ports=allowed_ports.strip() or "*",
        connection_uri=connection_uri.strip(),
        connection_instructions=connection_instructions.strip(),
        max_checkout_minutes=max_checkout_minutes,
        no_show_forfeit_minutes=no_show_forfeit_minutes,
        host_verified=False,
    )
    session.add(seat)
    session.commit()
    return RedirectResponse(url=f"/admin?msg=Created+Seat+'{seat.name}'&level=success", status_code=303)


@router.post("/seats/{resource_id}/update")
async def update_seat(
    resource_id: int,
    name: str = Form(...),
    resource_type: str = Form("Moonlight/Sunshine"),
    tailnet_target: str = Form(...),
    allowed_ports: str = Form("*"),
    spectator_ports: str = Form("tcp:47984,tcp:5900"),
    connection_uri: str = Form(""),
    connection_instructions: str = Form(""),
    max_checkout_minutes: int = Form(240),
    no_show_forfeit_minutes: int = Form(15),
    allow_watch_party: bool = Form(False),
    allow_admin_mirror: bool = Form(False),
    spectator_url: str = Form(""),
    spectator_instructions: str = Form(""),
    allow_takeover_adhoc: bool = Form(False),
    allow_takeover_scheduled: bool = Form(False),
    takeover_cooldown_minutes: int = Form(15),
    handoff_timeout_seconds: int = Form(60),
    auto_evict_on_timeout: bool = Form(False),
    ssh_host: str = Form(""),
    ssh_port: int = Form(22),
    ssh_user: str = Form("root"),
    ssh_auth_type: str = Form("password"),
    ssh_secret: str = Form(""),
    enable_osd_notify: bool = Form(False),
    enable_idle_release: bool = Form(False),
    enable_local_evict_cmd: bool = Form(False),
    cmd_osd_notify: str = Form(""),
    cmd_evict: str = Form(""),
    cmd_idle_check: str = Form(""),
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    seat = session.get(Resource, resource_id)
    if not seat:
        return RedirectResponse(url="/admin?msg=Seat+not+found&level=danger", status_code=303)

    # If SSH connection target changed, reset host_verified until re-tested
    if (
        seat.ssh_host != ssh_host.strip()
        or seat.ssh_port != ssh_port
        or seat.ssh_user != ssh_user.strip()
    ):
        seat.host_verified = False
        seat.last_ssh_check_msg = "SSH settings changed — re-verification required"

    seat.name = name.strip()
    seat.resource_type = resource_type
    seat.tailnet_target = tailnet_target.strip()
    seat.allowed_ports = allowed_ports.strip() or "*"
    seat.spectator_ports = spectator_ports.strip() or "*"
    seat.connection_uri = connection_uri.strip()
    seat.connection_instructions = connection_instructions
    seat.max_checkout_minutes = max(5, max_checkout_minutes)
    seat.no_show_forfeit_minutes = max(0, no_show_forfeit_minutes)

    seat.allow_watch_party = allow_watch_party
    seat.allow_admin_mirror = allow_admin_mirror
    seat.spectator_url = spectator_url.strip()
    seat.spectator_instructions = spectator_instructions

    seat.allow_takeover_adhoc = allow_takeover_adhoc
    seat.allow_takeover_scheduled = allow_takeover_scheduled
    seat.takeover_cooldown_minutes = max(0, takeover_cooldown_minutes)
    seat.handoff_timeout_seconds = max(10, handoff_timeout_seconds)
    seat.auto_evict_on_timeout = auto_evict_on_timeout

    seat.ssh_host = ssh_host.strip()
    seat.ssh_port = ssh_port
    seat.ssh_user = ssh_user.strip()
    seat.ssh_auth_type = ssh_auth_type
    if ssh_secret.strip():
        seat.ssh_secret = ssh_secret

    seat.cmd_osd_notify = cmd_osd_notify
    seat.cmd_evict = cmd_evict
    seat.cmd_idle_check = cmd_idle_check

    # Progressive UI Enforcement: Only allow host-dependent checkboxes if host_verified == True
    if seat.host_verified:
        seat.enable_osd_notify = enable_osd_notify
        seat.enable_idle_release = enable_idle_release
        seat.enable_local_evict_cmd = enable_local_evict_cmd
    else:
        seat.enable_osd_notify = False
        seat.enable_idle_release = False
        seat.enable_local_evict_cmd = False

    session.add(seat)
    session.commit()
    await sync_control_plane_acls(session)
    return RedirectResponse(url=f"/admin?msg=Saved+policies+for+Seat+'{seat.name}'&level=success", status_code=303)


@router.post("/seats/{resource_id}/verify-ssh")
async def verify_seat_ssh(
    resource_id: int,
    simulate: bool = Form(False),
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    seat = session.get(Resource, resource_id)
    if not seat:
        return RedirectResponse(url="/admin?msg=Seat+not+found&level=danger", status_code=303)

    ok, msg = await test_ssh_connection(seat, simulate_if_demo=simulate)
    seat.host_verified = ok
    seat.last_ssh_check_msg = msg
    if not ok:
        seat.enable_osd_notify = False
        seat.enable_idle_release = False
        seat.enable_local_evict_cmd = False
    session.add(seat)
    session.commit()

    lvl = "success" if ok else "danger"
    return RedirectResponse(
        url=f"/admin?msg=Seat+'{seat.name}'+SSH+Check:+{msg.replace(' ', '+')}&level={lvl}#seat-{seat.id}",
        status_code=303,
    )


@router.post("/seats/{resource_id}/push-connector")
async def push_seat_connector(
    resource_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    seat = session.get(Resource, resource_id)
    if not seat:
        return RedirectResponse(url="/admin?msg=Seat+not+found&level=danger", status_code=303)

    ok, msg = await push_helper_script(seat)
    if ok:
        seat.host_verified = True
        seat.last_ssh_check_msg = msg
        session.add(seat)
        session.commit()

    lvl = "success" if ok else "danger"
    return RedirectResponse(
        url=f"/admin?msg={msg.replace(' ', '+')}&level={lvl}#seat-{seat.id}",
        status_code=303,
    )


@router.post("/seats/{resource_id}/delete")
async def delete_seat(
    resource_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(resolve_current_user),
):
    seat = session.get(Resource, resource_id)
    if seat:
        session.delete(seat)
        session.commit()
        await sync_control_plane_acls(session)
    return RedirectResponse(url="/admin?msg=Seat+deleted&level=info", status_code=303)
