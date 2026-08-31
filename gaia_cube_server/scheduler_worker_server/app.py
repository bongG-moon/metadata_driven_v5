"""HCP ASGI entrypoint for the independent PTMORE scheduler Worker.

This service has no interactive CUBE callback route.  Its single long-running
background task reads due schedules from MongoDB, calls GAIA, and delivers the
final Rich Notification to the schedule owner's personal CUBE DM.

Configuration errors intentionally do not crash the HTTP process: ``/ready``
returns HTTP 503 with a safe error category so HCP does not route traffic to a
Worker that cannot execute schedules.  Secrets, connection strings, and remote
response content are never included in health responses or logs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from cube_runtime import Settings, SettingsError
from scheduler_worker import MongoScheduleRepository, SchedulerSettings, SchedulerWorker


LOGGER = logging.getLogger("ptmore_scheduler_hcp")


def _startup_error_category(error: BaseException) -> str:
    """Return a safe readiness category without exposing error text."""

    if isinstance(error, SettingsError):
        return "configuration_error"
    if isinstance(error, PyMongoError):
        return "mongodb_unavailable"
    return "initialization_error"


class SchedulerServiceRuntime:
    """Own the background Worker and its MongoDB client for one ASGI app.

    The small dependency-injection seams make the FastAPI lifecycle testable
    without a real MongoDB, GAIA endpoint, CUBE account, or HCP Secret.
    """

    def __init__(
        self,
        *,
        gaia_settings_loader: Callable[[], Settings] = Settings.from_env,
        scheduler_settings_loader: Callable[[], SchedulerSettings] = SchedulerSettings.from_env,
        mongo_client_factory: Callable[..., Any] = MongoClient,
        repository_factory: Callable[[Any, Any], Any] = MongoScheduleRepository,
        worker_factory: Callable[..., Any] = SchedulerWorker,
    ) -> None:
        self._gaia_settings_loader = gaia_settings_loader
        self._scheduler_settings_loader = scheduler_settings_loader
        self._mongo_client_factory = mongo_client_factory
        self._repository_factory = repository_factory
        self._worker_factory = worker_factory

        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._mongo_client: Any | None = None
        self._worker: Any | None = None
        self._ready = False
        self._worker_running = False
        self._error_category: str | None = None
        self._shutdown_timeout_seconds = 60.0

    @property
    def ready(self) -> bool:
        """Whether initialization succeeded and the Worker task is alive."""

        return bool(self._ready and self._task is not None and not self._task.done())

    def health_payload(self) -> dict[str, Any]:
        """Return only safe process state for liveness/readiness probes."""

        ready = self.ready
        payload: dict[str, Any] = {
            "status": "ok" if ready else "not_ready",
            "ready": ready,
            "worker": "running" if ready else "not_running",
        }
        if not ready and self._error_category:
            payload["reason"] = self._error_category
        return payload

    async def _close_mongo_client(self, client: Any | None) -> None:
        if client is None:
            return
        try:
            await asyncio.to_thread(client.close)
        except Exception as exc:  # pragma: no cover - defensive cleanup only
            LOGGER.error("Scheduler MongoDB client close failed: type=%s", type(exc).__name__)

    async def start(self) -> None:
        """Validate settings/MongoDB and start the long-running Worker task.

        Failure is deliberately captured as a readiness failure.  This keeps
        the HCP process inspectable at ``/health`` and lets ``/ready`` report
        false instead of exposing a partially configured scheduler as healthy.
        """

        if self._task is not None and not self._task.done():
            return

        self._ready = False
        self._worker_running = False
        self._error_category = None
        self._worker = None
        self._stop_event = asyncio.Event()
        mongo_client: Any | None = None

        try:
            gaia_settings = self._gaia_settings_loader()
            scheduler_settings = self._scheduler_settings_loader()
            scheduler_settings.validate_lease_duration(gaia_settings)

            mongo_client = self._mongo_client_factory(
                scheduler_settings.mongodb_uri,
                serverSelectionTimeoutMS=scheduler_settings.mongodb_server_selection_timeout_ms,
            )
            # PyMongo connects lazily.  A bounded ping proves that this Worker
            # can reach the shared Portal schedule store before it is ready.
            await asyncio.to_thread(mongo_client.admin.command, "ping")
            database = mongo_client[scheduler_settings.mongodb_database]
            repository = self._repository_factory(
                database[scheduler_settings.schedule_collection],
                database[scheduler_settings.schedule_run_collection],
            )
            worker = self._worker_factory(
                gaia_settings=gaia_settings,
                scheduler_settings=scheduler_settings,
                repository=repository,
            )
        except asyncio.CancelledError:
            await self._close_mongo_client(mongo_client)
            raise
        except Exception as exc:
            await self._close_mongo_client(mongo_client)
            self._error_category = _startup_error_category(exc)
            # Do not include str(exc): it may contain a URI, credential, or a
            # remote system detail that must not leave the Worker boundary.
            LOGGER.error(
                "Scheduler Worker initialization failed: category=%s type=%s",
                self._error_category,
                type(exc).__name__,
            )
            return

        self._mongo_client = mongo_client
        self._worker = worker
        # Let a normal in-flight GAIA+CUBE request finish on shutdown before a
        # forced cancellation is considered.  The Worker itself has finite
        # request timeouts; this is a graceful-shutdown ceiling, not a secret.
        self._shutdown_timeout_seconds = max(
            30.0,
            float(gaia_settings.gaia_timeout_seconds)
            + float(gaia_settings.cube_timeout_seconds)
            + 15.0,
        )
        self._ready = True
        self._task = asyncio.create_task(
            self._run_worker(worker),
            name="ptmore-scheduler-worker",
        )
        LOGGER.info("Independent scheduler Worker background task started.")

    async def _run_worker(self, worker: Any) -> None:
        self._worker_running = True
        try:
            await worker.run_forever(stop_event=self._stop_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._stop_event.is_set():
                self._ready = False
                self._error_category = "worker_runtime_error"
                LOGGER.error("Scheduler Worker stopped unexpectedly: type=%s", type(exc).__name__)
        finally:
            self._worker_running = False
            if not self._stop_event.is_set() and self._error_category is None:
                # A Worker should only normally return after shutdown.
                self._ready = False
                self._error_category = "worker_stopped"

    async def stop(self) -> None:
        """Signal shutdown, let an in-flight run finish, then close MongoDB."""

        self._ready = False
        self._stop_event.set()
        task = self._task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=self._shutdown_timeout_seconds,
                )
            except TimeoutError:
                LOGGER.warning("Scheduler Worker did not stop in its graceful shutdown window.")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # pragma: no cover - defensive only
                    LOGGER.error("Cancelled Worker task ended with type=%s", type(exc).__name__)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - task failure is already recorded
                LOGGER.error("Scheduler Worker task ended during shutdown: type=%s", type(exc).__name__)

        self._task = None
        self._worker = None
        client, self._mongo_client = self._mongo_client, None
        await self._close_mongo_client(client)


def create_application(
    *,
    runtime_factory: Callable[[], SchedulerServiceRuntime] | None = None,
) -> FastAPI:
    """Create the HCP Uvicorn application for this standalone Worker."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime = runtime_factory() if runtime_factory is not None else SchedulerServiceRuntime()
        application.state.scheduler_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    application = FastAPI(
        title="PTMORE Scheduler Worker",
        description="MongoDB schedule worker → GAIA → CUBE personal DM",
        version="1.0.0",
        lifespan=lifespan,
    )

    @application.get("/health")
    async def health(request: Request) -> JSONResponse:
        """Liveness endpoint; returns safely even when Worker is not ready."""

        runtime: SchedulerServiceRuntime = request.app.state.scheduler_runtime
        return JSONResponse(runtime.health_payload(), status_code=200)

    @application.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        """HCP readiness endpoint; configuration/init failures return 503."""

        runtime: SchedulerServiceRuntime = request.app.state.scheduler_runtime
        payload = runtime.health_payload()
        return JSONResponse(payload, status_code=200 if payload["ready"] else 503)

    return application


# Both names are intentional: ``application`` matches the established HCP
# command, while ``app`` supports normal Uvicorn import syntax.
application = create_application()
app = application


if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
