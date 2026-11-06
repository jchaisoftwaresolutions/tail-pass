from typing import Dict, List
import httpx
from app.db import SystemConfig
from app.drivers.acl_base import ACLBaseDriver


class TailscaleACLDriver(ACLBaseDriver):
    """Tailscale SaaS API driver for dynamic Seat ACL updates."""

    async def sync_acl_rules(self, rules: List[Dict], cfg: SystemConfig) -> tuple[bool, str]:
        tailnet = (cfg.tailscale_tailnet or "-").strip()
        api_key = cfg.tailscale_api_key.strip()
        if not api_key:
            return True, "Coordination Only (No Tailscale API key configured)"

        url = f"https://api.tailscale.com/api/v2/tailnet/{tailnet}/acl"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                get_resp = await client.get(url, headers=headers)
                if get_resp.status_code == 200:
                    policy = get_resp.json()
                else:
                    policy = {"acls": []}

                existing_acls = [
                    r for r in policy.get("acls", [])
                    if not r.get("_tailpass_managed")
                ]
                tagged_rules = [{**r, "_tailpass_managed": True} for r in rules]
                policy["acls"] = existing_acls + tagged_rules

                post_resp = await client.post(url, headers=headers, json=policy)
                if post_resp.status_code in (200, 201):
                    return True, f"Tailscale ACL synced ({len(rules)} Seat rule(s))"
                return False, f"Tailscale API HTTP {post_resp.status_code}: {post_resp.text[:160]}"
        except Exception as exc:
            return False, f"Tailscale ACL sync error: {exc}"
