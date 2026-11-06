# Tail-Pass (`ts-reserve`)

**Tail-Pass** is an open-source, self-hosted **Seat** checkout, scheduling, and dynamic access-control gateway built natively for **Tailscale** and **Headscale** tailnets.

In Tail-Pass, shared single-occupancy devices, services, and reservation timeslots (such as Sunshine/Moonlight console streamers, shared RTX/GPU workstations, or Windows RDP servers) are referred to as **Seats** (with a subtitle displaying the underlying device/service and Tailnet target).

---

## Key Features

- **Zero-Friction Tailnet Identity (`TAILNET_AUTO_AUTH`)**: Queries the local Tailscale daemon Unix socket (`/var/run/tailscale/tailscaled.sock`) via `GET http://local-tailscaled.sock/localapi/v0/whois?addr=<ip>:<port>` to resolve `UserProfile.LoginName`, `UserProfile.DisplayName`, and `Node.ComputedName` automatically. Includes a `DEV_MOCK_USER` fallback and built-in Persona Switcher for local testing outside a tailnet.
- **Optional Secondary Security (`PASSKEY_OR_PIN`)**: Admin toggle to require a 4–6 digit PIN (or WebAuthn Passkey) before checking out a Seat, requesting a takeover, or modifying Admin settings.
- **User-Level Tailnet ACL Enforcement**: Dynamically updates a managed `_tailpass_managed` rule block in Tailscale SaaS (`HuJSON`) or Headscale (`/api/v1/policy`). Check out a Seat from your phone and immediately connect from your gaming PC, handheld, or tablet. Falls back cleanly to **Coordination Only (Mock/Soft Mode)** if no API key is configured.
- **Watch Party / Co-Op & Admin Mirroring**: Leaseholders can open a **Watch Party / Co-Op** on their active Seat session, allowing other Tailnet users to click **Join Watch Party** and temporarily receive spectator/co-op port access in the Tailnet ACL until the primary lease ends.
- **Scheduling, Priority Preemption & No-Show Forfeit**:
  - Quick-select ad-hoc checkout (`30m`, `1h`, `2h`, `4h`, or custom duration) automatically caps or blocks against upcoming scheduled reservations.
  - Scheduled Seat blocks strictly preempt ad-hoc sessions (with `T-10m` and `T-5m` warnings via Web/ntfy/Webhook + optional SSH OSD).
  - Configurable `no_show_forfeit_minutes` window (`AWAITING_CHECKIN`) auto-releases unclaimed scheduled blocks unless the user checks in on the web UI or triggers a host telemetry heartbeat (`POST /api/seats/{id}/heartbeat`).
- **Takeover / Handoff State Machine & "Mode A"**:
  - Users can click **Request Access (Handoff)** on an `IN_USE` Seat, starting a live countdown (`handoff_timeout_seconds`) and dispatching interactive alerts (`Accept`, `Deny`, `Need +10m`).
  - **Mode A (`auto_evict_on_timeout`)**: When enabled, unanswered handoff requests automatically revoke the current holder's ACL, grant the Seat to the requester, and optionally fire the SSH eviction hook.
- **Agentless SSH & Progressive Admin UI**:
  - Verify SSH connectivity per Seat (**Test SSH & Verify Host**) and optionally deploy `connectors/generic_notify.sh` in one click (**Push Helper Script**).
  - Host-dependent checkboxes (`Enable On-Screen Overlay (OSD) Notifications`, `Auto-Release on Host Idle Telemetry`, `Run Local Eviction Command on Timeout`) remain strictly `disabled` (grayed out with a `"Requires verified SSH/Host connection"` badge) until `host_verified == True`.

---

## Quick Start (Direct GitHub Install / Local Python)

```bash
git clone https://github.com/jchaisoftwaresolutions/tail-pass.git
cd tail-pass

python -m venv .venv
# Linux / macOS:
source .venv/bin/activate
# Windows PowerShell:
# .\.venv\Scripts\Activate.ps1

pip install -r requirements.txt

# Run on port 8080:
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Then open `http://localhost:8080` in your browser.

---

## Deploying with Docker & Mounting `tailscaled.sock`

```bash
docker compose up -d --build
```

In `docker-compose.yml`, the host's Tailscale Unix socket is mounted read-only so Tail-Pass can resolve connecting Tailnet users via `localapi/v0/whois`:

```yaml
volumes:
  - ./data:/app/data
  - /var/run/tailscale/tailscaled.sock:/var/run/tailscale/tailscaled.sock:ro
```

---

## Configuring Tailscale or Headscale API Keys

1. Open **Admin & SSH** (`/admin`) in the Tail-Pass web UI.
2. Under **Control Plane ACL Enforcement & Secondary Auth**, select your mode:
   - **Coordination Only (Mock / Soft Mode)**: Tracks Seat leases, schedules, and handoffs without pushing external API calls.
   - **Tailscale SaaS API**: Set your Tailnet name (or `-` for default) and a Tailscale API access token (`tskey-api-...`) with ACL write permissions.
   - **Headscale REST API**: Set your Headscale server URL (e.g., `https://headscale.example.com`) and Headscale API key (`hskey-...`). Tail-Pass updates `/api/v1/policy` when Headscale is running in database policy mode.
3. Click **Save System & Control Plane Settings**.

---

## Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TAILPASS_DB_PATH` | `tailpass.db` | Path to the SQLite database file (WAL mode enabled). |
| `TAILSCALE_SOCKET` | `/var/run/tailscale/tailscaled.sock` | Path to the local `tailscaled` Unix socket for `WhoIs` resolution. |
| `DEV_MOCK_USER` | `admin@tailnet.local` | Fallback user identity when running outside a tailnet. |
| `TAILPASS_BASE_URL` | `http://localhost:8080` | Base URL passed to SSH OSD callback hooks (`{{callback_url}}`). |
| `SCHEDULER_INTERVAL_SECONDS` | `5` | Background tick interval for expirations, warnings, and handoff timeouts. |
