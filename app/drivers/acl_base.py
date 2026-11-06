from abc import ABC, abstractmethod
import json
from typing import Dict, List
from sqlmodel import Session, select
from app.db import Lease, Resource, SystemConfig, User, utcnow


class ACLBaseDriver(ABC):
    """Abstract interface for Tailnet Control Plane ACL drivers."""

    @abstractmethod
    async def sync_acl_rules(self, rules: List[Dict], cfg: SystemConfig) -> tuple[bool, str]:
        """Push the compiled managed Seat ACL rules to the Control Plane."""
        raise NotImplementedError


def build_managed_acl_rules(session: Session) -> List[Dict]:
    """
    Build the managed Tail-Pass ACL rules mapping active Seat leaseholders
    and Watch Party spectators to their target Seat (`resource.tailnet_target`).
    """
    active_leases = session.exec(
        select(Lease).where(Lease.status.in_(["ACTIVE", "AWAITING_CHECKIN"]))
    ).all()

    rules: List[Dict] = []
    for lease in active_leases:
        resource = session.get(Resource, lease.resource_id)
        holder = session.get(User, lease.user_id)
        if not resource or not holder:
            continue

        ports = [p.strip() for p in (resource.allowed_ports or "*").split(",") if p.strip()]
        dst_entries = [f"{resource.tailnet_target}:{p}" if ":" not in p else f"{resource.tailnet_target}:{p.split(':', 1)[1]}" for p in ports]
        if not dst_entries:
            dst_entries = [f"{resource.tailnet_target}:*"]

        # Primary leaseholder rule
        rules.append(
            {
                "_tailpass_seat": resource.name,
                "_tailpass_lease_id": lease.id,
                "action": "accept",
                "src": [holder.login_name],
                "dst": dst_entries,
            }
        )

        # Spectator / Watch Party rule
        if lease.watch_party_open and lease.spectator_logins_csv:
            spectators = [s.strip() for s in lease.spectator_logins_csv.split(",") if s.strip()]
            if spectators:
                spec_ports = [p.strip() for p in (resource.spectator_ports or "*").split(",") if p.strip()]
                spec_dst = [
                    f"{resource.tailnet_target}:{p}" if ":" not in p else f"{resource.tailnet_target}:{p.split(':', 1)[1]}"
                    for p in spec_ports
                ]
                rules.append(
                    {
                        "_tailpass_seat": f"{resource.name} (Watch Party)",
                        "_tailpass_lease_id": lease.id,
                        "action": "accept",
                        "src": spectators,
                        "dst": spec_dst,
                    }
                )

    return rules


async def sync_control_plane_acls(session: Session) -> tuple[bool, str]:
    """Compile managed Seat ACL rules and push via configured driver (or Mock/Soft Mode)."""
    from app.drivers.tailscale_api import TailscaleACLDriver
    from app.drivers.headscale_api import HeadscaleACLDriver

    cfg = session.get(SystemConfig, 1)
    if not cfg:
        return True, "No system config"

    managed_rules = build_managed_acl_rules(session)
    clean_rules = [
        {"action": r["action"], "src": r["src"], "dst": r["dst"]}
        for r in managed_rules
    ]
    cfg.last_acl_payload_preview = json.dumps({"acls": managed_rules}, indent=2)
    cfg.last_acl_sync_at = utcnow()

    mode = (cfg.control_plane_mode or "mock").lower()
    if mode == "tailscale" and cfg.tailscale_api_key:
        driver = TailscaleACLDriver()
        ok, msg = await driver.sync_acl_rules(clean_rules, cfg)
    elif mode == "headscale" and cfg.headscale_api_key and cfg.headscale_url:
        driver = HeadscaleACLDriver()
        ok, msg = await driver.sync_acl_rules(clean_rules, cfg)
    else:
        ok = True
        msg = f"Coordination Only (Mock Mode) — {len(clean_rules)} active Seat ACL rule(s) compiled"

    cfg.last_acl_sync_status = msg
    session.add(cfg)
    session.commit()
    return ok, msg
