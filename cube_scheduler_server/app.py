from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .config import CubeSchedulerConfig
from .models import public_schedule
from .service import CubeSchedulerService
from .store import RuntimeStore, ScheduleSource
from .transport import HttpCubeTransport


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def create_app(
    config: CubeSchedulerConfig | None = None,
    *,
    source: ScheduleSource | None = None,
    runtime: RuntimeStore | None = None,
    transport: HttpCubeTransport | None = None,
    start_workers: bool = True,
) -> FastAPI:
    active_config = config or CubeSchedulerConfig.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        config_errors = active_config.validate()
        application.state.config_errors = config_errors
        application.state.source = source
        application.state.runtime = runtime
        application.state.service = None
        if not config_errors:
            active_source = source or ScheduleSource(active_config)
            active_runtime = runtime or RuntimeStore(active_config)
            active_transport = transport or HttpCubeTransport(active_config)
            application.state.source = active_source
            application.state.runtime = active_runtime
            service = CubeSchedulerService(
                active_source,
                active_runtime,
                active_transport,
                active_config.poll_seconds,
            )
            application.state.service = service
            if start_workers:
                await service.start()
            else:
                await asyncio.to_thread(active_runtime.ensure_indexes)
        try:
            yield
        finally:
            service = getattr(application.state, "service", None)
            if service is not None and start_workers:
                await service.close()
            elif service is not None:
                await asyncio.to_thread(service.source.close)
                await asyncio.to_thread(service.runtime.close)

    application = FastAPI(
        title="metadata_driven_v5 CUBE Scheduler Server",
        version="1.0.0",
        lifespan=lifespan,
    )

    @application.get("/live")
    @application.get("/health")
    def live() -> dict[str, Any]:
        return {"ok": True, "service": "metadata-driven-v5-cube-scheduler"}

    @application.get("/ready")
    def ready(request: Request) -> JSONResponse:
        errors = list(getattr(request.app.state, "config_errors", []))
        checks = {"configuration": not errors, "schedule_source": False, "runtime_store": False}
        if not errors:
            try:
                request.app.state.source.ping()
                checks["schedule_source"] = True
            except Exception as exc:  # noqa: BLE001
                errors.append(f"schedule_source: {type(exc).__name__}: {exc}")
            try:
                request.app.state.runtime.ping()
                checks["runtime_store"] = True
            except Exception as exc:  # noqa: BLE001
                errors.append(f"runtime_store: {type(exc).__name__}: {exc}")
        payload = {"ok": all(checks.values()), "checks": checks, "errors": errors}
        return JSONResponse(payload, status_code=200 if payload["ok"] else 503)

    def require_runtime(request: Request) -> RuntimeStore:
        value = getattr(request.app.state, "runtime", None)
        if value is None:
            raise HTTPException(status_code=503, detail="runtime store is not configured")
        return value

    @application.get("/api/v1/schedules")
    def list_schedules(request: Request) -> list[dict[str, Any]]:
        source_value = getattr(request.app.state, "source", None)
        if source_value is None:
            raise HTTPException(status_code=503, detail="schedule source is not configured")
        return _json_safe(source_value.read_all())

    @application.get("/api/v1/schedules/{schedule_id}")
    def get_schedule(request: Request, schedule_id: str) -> dict[str, Any]:
        source_value = getattr(request.app.state, "source", None)
        if source_value is None:
            raise HTTPException(status_code=503, detail="schedule source is not configured")
        document = source_value.collection.find_one({"schedule_id": schedule_id}, {"_id": 0})
        if not document:
            raise HTTPException(status_code=404, detail="schedule not found")
        return _json_safe(public_schedule(document))

    @application.get("/api/v1/schedules/{schedule_id}/runs")
    def list_runs(
        request: Request,
        schedule_id: str,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return _json_safe(require_runtime(request).list_runs(schedule_id, limit))

    @application.post("/api/cube/callback")
    @application.post("/qna")
    async def cube_callback(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="callback body must be JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("richnotificationmessage"), dict):
            raise HTTPException(status_code=400, detail="Invalid Payload Format")
        result = require_runtime(request).record_inbound(payload)
        if result["process_data"] == "!@#HelloChatBot#@!":
            return {"status": "ignored", "message_id": result["message_id"]}
        return {
            "status": "duplicate" if result["duplicate"] else "accepted",
            "message_id": result["message_id"],
        }

    return application


app = create_app()
