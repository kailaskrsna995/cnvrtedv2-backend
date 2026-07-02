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

# V2 routes
from app.routes.auth_route import router as auth_router
from app.routes.admin_route import router as admin_router
from app.routes.profiles import router as profiles_router
from app.routes.agents_route import router as agents_router
from app.routes.leads_v2 import router as leads_router

app = FastAPI(title="cnvrted V2", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # demo: no auth, open to the Vercel frontend
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
