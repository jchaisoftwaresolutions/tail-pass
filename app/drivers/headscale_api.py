import json
from typing import Dict, List
import httpx
from app.db import SystemConfig
from app.drivers.acl_base import ACLBaseDriver


class HeadscaleACLDriver(ACLBaseDriver):
    """Headscale REST API driver for ACL policy updates."""

    async def sync_acl_rules(self, rules: List[Dict], cfg: SystemConfig) -> tuple[bool, str]:
        base_url = cfg.headscale_url.rstrip("/")
        api_key = cfg.headscale_api_key.strip()
        if not base_url or not api_key:
            return True, "Coordination Only (Headscale URL/Key not configured)"

        url = f"{base_url}/api/v1/policy"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                get_resp = await client.get(url, headers=headers)
                policy_obj = {"acls": []}
                if get_resp.status_code == 200:
                    data = get_resp.json()
                    raw_policy = data.get("policy")
                    if isinstance(raw_policy, str) and raw_policy.strip():
                        try:
                            policy_obj = json.loads(raw_policy)
                        except Exception:
                            policy_obj = {"acls": []}
                    elif isinstance(raw_policy, dict):
                        policy_obj = raw_policy

                existing_acls = [
                    r for r in policy_obj.get("acls", [])
                    if not r.get("_tailpass_managed")
                ]
                tagged_rules = [{**r, "_tailpass_managed": True} for r in rules]
                policy_obj["acls"] = existing_acls + tagged_rules

                put_resp = await client.put(
                    url,
                    headers=headers,
                    json={"policy": json.dumps(policy_obj)},
                )
                if put_resp.status_code in (200, 201):
                    return True, f"Headscale ACL synced ({len(rules)} Seat rule(s))"
                return False, f"Headscale API HTTP {put_resp.status_code}: {put_resp.text[:160]}"
        except Exception as exc:
            return False, f"Headscale ACL sync error: {exc}"
