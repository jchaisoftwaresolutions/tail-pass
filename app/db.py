from datetime import datetime, timezone
from typing import Optional
from sqlmodel import Field, Session, SQLModel, create_engine, select
from sqlalchemy import event
from app.config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    login_name: str = Field(index=True, unique=True)
    display_name: str = Field(default="")
    node_name: str = Field(default="")
    is_admin: bool = Field(default=False)
    pin_hash: Optional[str] = Field(default=None)
    webauthn_credential_id: Optional[str] = Field(default=None)
    webauthn_public_key: Optional[str] = Field(default=None)
    push_subscription_json: Optional[str] = Field(default=None)
    last_seen: datetime = Field(default_factory=utcnow)


class Resource(SQLModel, table=True):
    """Represents a Seat (Device / Service Object)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    resource_type: str = Field(default="Moonlight/Sunshine")  # Moonlight/Sunshine, RDP, VNC, SSH, Custom
    tailnet_target: str = Field(default="100.64.0.10")        # e.g., tag:switch2 or 100.x.y.z
    allowed_ports: str = Field(default="*")                   # e.g., * or tcp:47984,udp:47998
    spectator_ports: str = Field(default="tcp:47984,tcp:5900")
    connection_uri: str = Field(default="moonlight://100.64.0.10")
    connection_instructions: str = Field(default="Connect via Moonlight or native client using the target IP above.")

    # Checkout & Schedule Policies
    max_checkout_minutes: int = Field(default=240)
    no_show_forfeit_minutes: int = Field(default=15)

    # Watch Party / Co-Op & Admin Mirroring
    allow_watch_party: bool = Field(default=True)
    allow_admin_mirror: bool = Field(default=True)
    spectator_url: str = Field(default="")
    spectator_instructions: str = Field(default="Join as spectator/co-op player once the Watch Party is open.")

    # Takeover / Handoff Policies
    allow_takeover_adhoc: bool = Field(default=True)
    allow_takeover_scheduled: bool = Field(default=False)
    takeover_cooldown_minutes: int = Field(default=15)
    handoff_timeout_seconds: int = Field(default=60)
    auto_evict_on_timeout: bool = Field(default=False)  # "Mode A" checkbox

    # Agentless SSH Configuration
    ssh_host: str = Field(default="")
    ssh_port: int = Field(default=22)
    ssh_user: str = Field(default="root")
    ssh_auth_type: str = Field(default="password")  # password or key
    ssh_secret: str = Field(default="")
    host_verified: bool = Field(default=False)
    last_ssh_check_msg: str = Field(default="Not verified yet")

    # Progressive Host-Dependent Toggles (disabled in UI unless host_verified == True)
    enable_osd_notify: bool = Field(default=False)
    enable_idle_release: bool = Field(default=False)
    enable_local_evict_cmd: bool = Field(default=False)

    # Custom Command Hooks
    cmd_osd_notify: str = Field(
        default='/opt/tailpass/generic_notify.sh "{{requester}}" "{{timeout_seconds}}" "{{request_id}}" "{{callback_url}}"'
    )
    cmd_evict: str = Field(default="systemctl restart sunshine || true")
    cmd_idle_check: str = Field(default="exit 0")


class Lease(SQLModel, table=True):
    """Represents an active or historical Seat checkout session."""
    id: Optional[int] = Field(default=None, primary_key=True)
    resource_id: int = Field(index=True, foreign_key="resource.id")
    user_id: int = Field(index=True, foreign_key="user.id")
    schedule_block_id: Optional[int] = Field(default=None)
    lease_type: str = Field(default="ADHOC")  # ADHOC or SCHEDULED
    status: str = Field(default="ACTIVE")     # ACTIVE, AWAITING_CHECKIN, ENDED, FORFEITED, EVICTED
    started_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    checked_in: bool = Field(default=True)
    watch_party_open: bool = Field(default=False)
    spectator_logins_csv: str = Field(default="")  # comma-separated login_names
    warned_10m: bool = Field(default=False)
    warned_5m: bool = Field(default=False)
    last_heartbeat_at: datetime = Field(default_factory=utcnow)


class ScheduleBlock(SQLModel, table=True):
    """Represents a reserved Seat timeslot on the calendar."""
    id: Optional[int] = Field(default=None, primary_key=True)
    resource_id: int = Field(index=True, foreign_key="resource.id")
    user_id: int = Field(index=True, foreign_key="user.id")
    title: str = Field(default="Reserved Seat")
    start_time: datetime = Field(index=True)
    end_time: datetime = Field(index=True)
    status: str = Field(default="SCHEDULED")  # SCHEDULED, ACTIVE, COMPLETED, CANCELLED, FORFEITED
    checked_in: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)


class TakeoverRequest(SQLModel, table=True):
    """State machine record for Seat handoff / takeover requests."""
    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: str = Field(index=True, unique=True)
    resource_id: int = Field(index=True, foreign_key="resource.id")
    lease_id: int = Field(index=True, foreign_key="lease.id")
    requester_id: int = Field(index=True, foreign_key="user.id")
    holder_id: int = Field(index=True, foreign_key="user.id")
    requested_minutes: int = Field(default=60)
    status: str = Field(default="PENDING_HANDOFF")  # PENDING_HANDOFF, ACCEPTED, DENIED, EXTENDED, TIMEOUT_EVICTED, UNANSWERED
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    responded_at: Optional[datetime] = Field(default=None)
    note: str = Field(default="")


class SystemConfig(SQLModel, table=True):
    id: Optional[int] = Field(default=1, primary_key=True)
    require_pin_or_passkey: bool = Field(default=False)
    control_plane_mode: str = Field(default="mock")  # mock, tailscale, headscale
    tailscale_tailnet: str = Field(default="-")
    tailscale_api_key: str = Field(default="")
    headscale_url: str = Field(default="")
    headscale_api_key: str = Field(default="")
    ntfy_url: str = Field(default="")
    ntfy_topic: str = Field(default="")
    apprise_urls: str = Field(default="")
    custom_webhook_url: str = Field(default="")
    last_acl_sync_status: str = Field(default="Coordination Only (Mock Mode)")
    last_acl_sync_at: Optional[datetime] = Field(default=None)
    last_acl_payload_preview: str = Field(default="{}")


class NotificationEvent(SQLModel, table=True):
    """Stores recent toast/alert events for live UI polling & history."""
    id: Optional[int] = Field(default=None, primary_key=True)
    target_user_id: Optional[int] = Field(default=None, index=True)
    title: str
    message: str
    level: str = Field(default="info")  # info, warning, success, danger
    action_url: str = Field(default="")
    created_at: datetime = Field(default_factory=utcnow)
    read: bool = Field(default=False)


sqlite_url = f"sqlite:///{settings.db_path}"
engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        cfg = session.get(SystemConfig, 1)
        if not cfg:
            cfg = SystemConfig(id=1)
            session.add(cfg)
            session.commit()

        existing_resources = session.exec(select(Resource)).first()
        if not existing_resources:
            demo_seats = [
                Resource(
                    name="Living Room Console Streamer",
                    resource_type="Moonlight/Sunshine",
                    tailnet_target="tag:sunshine-rig",
                    allowed_ports="tcp:47984,tcp:47989,tcp:48010,udp:47998-48000",
                    spectator_ports="tcp:47984,tcp:47989,udp:47998",
                    connection_uri="moonlight://100.100.10.20",
                    connection_instructions="1. Open **Moonlight** on your phone, tablet, or PC.\n2. Select host `100.100.10.20` (`sunshine-rig`).\n3. Launch **Desktop** or **Steam Big Picture**.",
                    max_checkout_minutes=240,
                    no_show_forfeit_minutes=15,
                    allow_watch_party=True,
                    allow_admin_mirror=True,
                    spectator_url="https://100.100.10.20:47990",
                    spectator_instructions="Connect via Moonlight in view-only/controller-2 mode once Watch Party is open.",
                    allow_takeover_adhoc=True,
                    allow_takeover_scheduled=False,
                    takeover_cooldown_minutes=15,
                    handoff_timeout_seconds=60,
                    auto_evict_on_timeout=True,
                    ssh_host="100.100.10.20",
                    ssh_port=22,
                    ssh_user="gamer",
                ),
                Resource(
                    name="Shared RTX 4090 Workstation",
                    resource_type="RDP",
                    tailnet_target="100.100.20.50",
                    allowed_ports="tcp:3389,udp:3389,tcp:22",
                    spectator_ports="tcp:5900",
                    connection_uri="rdp://full%20address=s:100.100.20.50:3389",
                    connection_instructions="Connect using Windows Remote Desktop or Windows App to `100.100.20.50:3389`.",
                    max_checkout_minutes=180,
                    no_show_forfeit_minutes=15,
                    allow_watch_party=False,
                    allow_admin_mirror=True,
                    allow_takeover_adhoc=True,
                    allow_takeover_scheduled=False,
                    auto_evict_on_timeout=False,
                    ssh_host="100.100.20.50",
                    ssh_port=22,
                    ssh_user="admin",
                ),
            ]
            for seat in demo_seats:
                session.add(seat)
            session.commit()


def get_session():
    with Session(engine) as session:
        yield session
