import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.middleware.body_limit import BodyLimit
from app.redis import close_redis, get_redis
from app.routers import (
    agents,
    alerts,
    auth,
    evaluations,
    governance,
    incidents,
    integrations,
    reports,
    trace_policies,
    traces,
    ws,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("agentops")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("AgentOps starting in %s mode", settings.APP_ENV)
    try:
        await get_redis().ping()
        log.info("Redis OK")
    except Exception as e:
        log.warning("Redis ping failed: %s", e)
    yield
    await close_redis()


app = FastAPI(title="AgentOps API", version="1.0.0", lifespan=lifespan)

app.add_middleware(BodyLimit)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(StarletteHTTPException)
async def _http_exc(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        message = detail.get("message", str(detail))
        code = detail.get("error", "http_error")
        details = detail
    else:
        message = str(detail)
        code = "http_error" if exc.status_code >= 400 else "ok"
        details = {}
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": message, "details": details}},
    )


@app.exception_handler(RequestValidationError)
async def _val_exc(request: Request, exc: RequestValidationError):
    details = jsonable_encoder(exc.errors(), custom_encoder={Exception: str})
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_error", "message": "invalid request", "details": details}},
    )


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "internal_error", "details": {}}},
    )


@app.get("/health")
async def health():
    ok = True
    try:
        await get_redis().ping()
    except Exception:
        ok = False
    return {"ok": ok, "env": settings.APP_ENV}


app.include_router(auth.router)
app.include_router(agents.router)
app.include_router(traces.ingest_router)
app.include_router(traces.router)
app.include_router(evaluations.router)
app.include_router(alerts.router)
app.include_router(incidents.router)
app.include_router(integrations.router)
app.include_router(governance.router)
app.include_router(reports.router)
app.include_router(trace_policies.router)
app.include_router(trace_policies.check_router)
app.include_router(ws.router)
