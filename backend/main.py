"""
CNVRTED V2 — Main Entry Point
Run locally: uvicorn main_v2:app --reload --port 8001

Port 8001 so V1 (port 8000) and V2 can run side by side during development.
"""

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s — %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.ratelimit import limiter

# V2 routes
from app.routes.auth_route import router as auth_router
from app.routes.admin_route import router as admin_router
from app.routes.profiles import router as profiles_router
from app.routes.agents_route import router as agents_router
from app.routes.leads_v2 import router as leads_router

app = FastAPI(title="cnvrted V2", version="2.0.0")

# Rate limiting — the limiter is attached to the app and a 429 handler is installed so
# @limiter.limit(...) decorators on the auth routes take effect (brute-force / signup-spam
# protection on the public, unauthenticated /auth/login and /auth/register endpoints).
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — only our own frontends may call this API from a browser. Auth is a Bearer
# token in the Authorization header (not cookies), so allow_credentials stays False.
# Exact prod origins + localhost for dev; the regex allows THIS project's Vercel preview
# deploys (cnvrted-ui-<hash>.vercel.app) so preview testing keeps working.
ALLOWED_ORIGINS = [
    "https://beta.cnvrted.com",
    "https://cnvrted-ui.vercel.app",
    "http://localhost:3000",
    "http://localhost:3001",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"^https://cnvrted-ui-[a-z0-9-]+\.vercel\.app$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(profiles_router)
app.include_router(agents_router)
app.include_router(leads_router)


@app.on_event("startup")
async def startup():
    # Scheduler disabled for demo — all runs are user-triggered via /leads/v2/run.
    # The background queue-worker competed with Run Now for the in-memory queue
    # and errored on a stale signals-table column. Re-enable with a proper job
    # queue (Celery/ARQ) + DB-backed queue when scaling beyond demo.
    print("cnvrted V2 running on http://localhost:8001 (scheduler off)")


@app.on_event("shutdown")
async def shutdown():
    pass  # scheduler disabled for demo


@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}
