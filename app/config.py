import os
from pydantic import BaseModel


class Settings(BaseModel):
    app_name: str = "Tail-Pass"
    db_path: str = os.getenv("TAILPASS_DB_PATH", "tailpass.db")
    tailscale_socket: str = os.getenv("TAILSCALE_SOCKET", "/var/run/tailscale/tailscaled.sock")
    dev_mock_user: str = os.getenv("DEV_MOCK_USER", "admin@tailnet.local")
    dev_mock_name: str = os.getenv("DEV_MOCK_NAME", "Local Admin")
    dev_mock_node: str = os.getenv("DEV_MOCK_NODE", "workstation-01")
    secret_key: str = os.getenv("TAILPASS_SECRET_KEY", "tailpass-dev-secret-change-in-prod")
    base_url: str = os.getenv("TAILPASS_BASE_URL", "http://localhost:8080")
    scheduler_interval_seconds: int = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "5"))


settings = Settings()
