import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from app.db import init_db
from app.routers import admin, api_handoff, dashboard
from app.scheduler import run_scheduler_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    stop_event = asyncio.Event()
    task = asyncio.create_task(run_scheduler_loop(stop_event))
    try:
        yield
    finally:
        stop_event.set()
        task.cancel()


app = FastAPI(
    title="Tail-Pass (ts-reserve)",
    description="Self-hosted Seat checkout, scheduling, and ACL gateway for Tailscale and Headscale tailnets.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(dashboard.router)
app.include_router(api_handoff.router)
app.include_router(admin.router)


@app.get("/manifest.json")
async def pwa_manifest():
    return JSONResponse(
        {
            "name": "Tail-Pass Seat Checkout",
            "short_name": "Tail-Pass",
            "description": "Tailscale & Headscale Seat Checkout, Scheduling, and ACL Gateway",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0f172a",
            "theme_color": "#4f46e5",
            "icons": [
                {
                    "src": "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/tailscale.png",
                    "sizes": "192x192",
                    "type": "image/png",
                }
            ],
        }
    )


@app.get("/sw.js")
async def service_worker():
    sw_code = """
self.addEventListener('install', (event) => {
    self.skipWaiting();
});
self.addEventListener('activate', (event) => {
    event.waitUntil(self.clients.claim());
});
self.addEventListener('push', (event) => {
    const data = event.data ? event.data.json() : { title: 'Tail-Pass Alert', body: 'Seat status updated' };
    event.waitUntil(
        self.registration.showNotification(data.title || 'Tail-Pass', {
            body: data.body || '',
            icon: 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/tailscale.png',
            data: { url: data.url || '/' }
        })
    );
});
self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    event.waitUntil(clients.openWindow(event.notification.data.url || '/'));
});
""".strip()
    return Response(content=sw_code, media_type="application/javascript")
