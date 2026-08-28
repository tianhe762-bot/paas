import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from paas import settings_store
from paas.communication.registry import AdapterManager
from paas.config import settings
from paas.db import connect, init_db
from paas.log import setup_logging
from paas.models import InboundMessage
from paas.modules.account import service as account_service
from paas.modules.admin import service as admin_service
from paas.router import Router
from paas.scheduler import SchedulerManager

log = logging.getLogger("paas.main")

STATIC_DIR = Path(__file__).parent / "static"


class InboundPayload(BaseModel):
    namespace: str = "default"
    platform: str
    user_id: str
    chat_id: str | None = None
    message_id: str
    timestamp: str = ""
    message_type: str = "text"
    content: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    conn = connect()
    try:
        init_db(conn)
        settings_store.ensure_default_settings(conn)
        admin_service.ensure_admin(conn)
    finally:
        conn.close()

    router = Router()
    manager = AdapterManager(router)
    scheduler = SchedulerManager(manager)
    app.state.router = router
    app.state.manager = manager
    app.state.scheduler = scheduler

    scheduler.start()
    conn = connect()
    try:
        await manager.apply_configs(conn)
    finally:
        conn.close()
    log.info("PAAS 启动完成")
    yield
    await manager.shutdown()
    scheduler.shutdown()
    await router.shutdown()


app = FastAPI(title="PAAS - Personal Auto Accounting System", lifespan=lifespan)


def _check_api_key(x_api_key: str | None = Header(default=None)) -> None:
    import hmac

    expected = settings.api_key or ""
    if not expected or x_api_key is None or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="无效的 X-Api-Key")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/message/inbound", dependencies=[Depends(_check_api_key)])
async def message_inbound(payload: InboundPayload) -> dict:
    msg = InboundMessage(
        namespace=payload.namespace,
        platform=payload.platform.lower(),
        user_id=payload.user_id,
        chat_id=payload.chat_id or payload.user_id,
        message_id=payload.message_id,
        timestamp=payload.timestamp,
        message_type=payload.message_type,
        content=payload.content,
    )
    reply = await app.state.router.handle(msg)
    return reply.model_dump()


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/admin")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(
        str(STATIC_DIR / "admin.html"),
        headers={"Cache-Control": "no-store"},
    )


from paas.modules.admin.api import router as admin_router  # noqa: E402

app.include_router(admin_router, prefix="/admin/api", tags=["admin"])


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
