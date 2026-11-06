import os
from typing import Any, Dict
import asyncssh
from jinja2 import Template
from app.db import Resource


def render_hook_command(template_str: str, context: Dict[str, Any]) -> str:
    """Render Jinja-style command hook variables like {{requester}}, {{minutes_left}}, {{request_id}}, {{callback_url}}."""
    if not template_str:
        return ""
    try:
        return Template(template_str).render(**context)
    except Exception:
        return template_str


async def _connect_ssh(resource: Resource):
    if not resource.ssh_host:
        raise ValueError("SSH Host is empty")

    kwargs: Dict[str, Any] = {
        "host": resource.ssh_host,
        "port": resource.ssh_port or 22,
        "username": resource.ssh_user or "root",
        "known_hosts": None,
        "connect_timeout": 6,
    }
    if resource.ssh_auth_type == "key" and resource.ssh_secret.strip():
        pkey = asyncssh.import_private_key(resource.ssh_secret.strip())
        kwargs["client_keys"] = [pkey]
    else:
        kwargs["password"] = resource.ssh_secret or ""

    return await asyncssh.connect(**kwargs)


async def test_ssh_connection(resource: Resource, simulate_if_demo: bool = False) -> tuple[bool, str]:
    """
    Test SSH connectivity to the Seat host and verify command execution.
    Supports a safe local loop/demo verification mode when ssh_host is 'localhost-demo' or 'mock'.
    """
    if not resource.ssh_host:
        return False, "No SSH host configured."

    if resource.ssh_host.lower() in ("mock", "localhost-demo", "demo.tailnet") or simulate_if_demo:
        return True, f"Verified host '{resource.ssh_host}' (Simulated / Loopback Check OK)"

    try:
        async with await _connect_ssh(resource) as conn:
            result = await conn.run("uname -a || ver", check=False, timeout=6)
            out = (result.stdout or "").strip() or "Connected OK"
            return True, f"SSH Verified: {out[:120]}"
    except Exception as exc:
        return False, f"SSH failed: {exc}"


async def run_ssh_hook(resource: Resource, cmd_template: str, context: Dict[str, Any]) -> tuple[bool, int, str]:
    """Execute a rendered SSH command on a verified Seat host."""
    if not resource.host_verified:
        return False, -1, "Host is not verified; skipping SSH hook."

    rendered_cmd = render_hook_command(cmd_template, context)
    if not rendered_cmd.strip():
        return False, -1, "Empty SSH command hook."

    if resource.ssh_host.lower() in ("mock", "localhost-demo", "demo.tailnet"):
        return True, 0, f"[Simulated SSH on {resource.ssh_host}] Executed: {rendered_cmd}"

    try:
        async with await _connect_ssh(resource) as conn:
            result = await conn.run(rendered_cmd, check=False, timeout=10)
            code = result.exit_status if result.exit_status is not None else 0
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            return (code == 0), code, output[:240]
    except Exception as exc:
        return False, -1, f"SSH execution error: {exc}"


async def push_helper_script(resource: Resource, script_path: str = "connectors/generic_notify.sh") -> tuple[bool, str]:
    """One-click push of connectors/generic_notify.sh to /opt/tailpass/generic_notify.sh on the remote Seat host."""
    if not os.path.exists(script_path):
        return False, f"Connector script not found at {script_path}"

    with open(script_path, "r", encoding="utf-8") as f:
        script_content = f.read()

    if resource.ssh_host.lower() in ("mock", "localhost-demo", "demo.tailnet"):
        return True, f"Pushed generic_notify.sh ({len(script_content)} bytes) to {resource.ssh_host}:/opt/tailpass/generic_notify.sh"

    try:
        async with await _connect_ssh(resource) as conn:
            await conn.run("mkdir -p /opt/tailpass", check=False, timeout=6)
            await conn.run(
                "cat << 'EOF' > /opt/tailpass/generic_notify.sh\n"
                + script_content
                + "\nEOF\nchmod +x /opt/tailpass/generic_notify.sh",
                check=True,
                timeout=10,
            )
            return True, "Successfully deployed /opt/tailpass/generic_notify.sh (+x)"
    except Exception as exc:
        return False, f"Failed to push helper script: {exc}"
