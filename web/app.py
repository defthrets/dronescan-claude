"""
web/app.py
FastAPI application — REST API + WebSocket endpoint.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from web.websocket_manager import WebSocketManager

logger = logging.getLogger("drone_detect.web")

_START_TIME = time.time()


def create_app(
    device_table,
    location_tracker,
    ws_manager: WebSocketManager,
    config: dict,
) -> FastAPI:
    """
    Build and return the FastAPI application.
    Receives shared state objects injected by the orchestrator.
    """
    app = FastAPI(
        title="Drone Detection System",
        version="1.0.0",
        description="Real-time Wi-Fi based drone detection",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── REST endpoints ────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def root():
        html = static_dir / "index.html"
        if html.exists():
            return HTMLResponse(content=html.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>index.html not found</h1>", status_code=404)

    @app.get("/api/devices")
    async def get_devices():
        devices = device_table.to_json_list()
        return {"devices": devices, "count": len(devices), "ts": time.time()}

    @app.get("/api/drones")
    async def get_drones():
        drones = [d.to_dict() for d in device_table.get_drone_devices()]
        return {"drones": drones, "count": len(drones), "ts": time.time()}

    @app.get("/api/stats")
    async def get_stats():
        all_devs   = device_table.get_all_devices()
        drone_devs = device_table.get_drone_devices()
        return {
            "uptime":          time.time() - _START_TIME,
            "total_devices":   len(all_devs),
            "drone_devices":   len(drone_devs),
            "observer":        location_tracker.get_observer_dict(),
            "ws_connections":  ws_manager.count,
            "ts":              time.time(),
        }

    @app.get("/api/config")
    async def get_ui_config():
        theme  = config.get("theme", {})
        webcfg = config.get("web", {})
        return {
            "theme":              theme,
            "map_provider":       webcfg.get("map_provider", "openstreetmap"),
            "google_maps_api_key": webcfg.get("google_maps_api_key", ""),
        }

    @app.post("/api/gps/update")
    async def update_gps_from_browser(body: dict):
        """
        Accept GPS coordinates pushed from the browser's Geolocation API.
        Falls back to browser GPS when no hardware GPS dongle is connected.
        """
        try:
            lat = float(body.get("lat", 0))
            lon = float(body.get("lon", 0))
            alt = float(body.get("alt", 0))
            if lat and lon:
                location_tracker.update_observer(lat, lon, alt)
                logger.debug("Browser GPS fix: %.6f, %.6f", lat, lon)
                return {"ok": True, "source": "browser"}
        except Exception as exc:
            logger.debug("GPS update error: %s", exc)
        return {"ok": False}

    @app.get("/api/gps/status")
    async def get_gps_status():
        obs = location_tracker.get_observer_dict()
        return {
            "active":    obs is not None,
            "observer":  obs,
            "ts":        time.time(),
        }

    @app.get("/api/export")
    async def export_json():
        """Export all current device data as a JSON report."""
        all_devs = device_table.to_json_list()
        report = {
            "exported_at":   time.time(),
            "uptime_seconds": time.time() - _START_TIME,
            "total_devices": len(all_devs),
            "drone_devices": len([d for d in all_devs if d.get("is_drone")]),
            "observer":      location_tracker.get_observer_dict(),
            "devices":       all_devs,
        }
        return JSONResponse(content=report, headers={
            "Content-Disposition": f'attachment; filename="drone_report_{int(time.time())}.json"'
        })

    # ── WebSocket ─────────────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            # Push full initial state on connect
            await websocket.send_text(json.dumps({
                "type":     "init",
                "devices":  device_table.to_json_list(),
                "observer": location_tracker.get_observer_dict(),
                "ts":       time.time(),
            }, default=str))

            while True:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                    msg = json.loads(raw)
                    if msg.get("type") == "ping":
                        await websocket.send_text(json.dumps({"type": "pong"}))
                except asyncio.TimeoutError:
                    await websocket.send_text(json.dumps({"type": "keepalive"}))
                except WebSocketDisconnect:
                    break
        except Exception as exc:
            logger.debug("WS session error: %s", exc)
        finally:
            await ws_manager.disconnect(websocket)

    return app
