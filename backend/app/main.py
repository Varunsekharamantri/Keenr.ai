import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .config import settings, PROJECT_DIR
from .db.database import init_db, SessionLocal
from .db.seed_data import seed_companies
from .api.routes_companies import router as companies_router
from .api.routes_events import router as events_router
from .api.routes_ingest import router as ingest_router
from .api.routes_stats import router as stats_router
from .api.routes_signals import router as signals_router
from .api.routes_exports import router as exports_router
from .api.routes_graph import router as graph_router
from .api.routes_tenders import router as tenders_router
from .api.routes_results import router as results_router
from .api.routes_insights import router as insights_router
from .api.routes_company_profile import router as company_profile_router
from .api.routes_opportunities import router as opportunities_router
from .api.routes_refresh import router as refresh_router
from .api.routes_galaxy import router as galaxy_router
from .models.schema import Signal
from .signals.engine import signal_engine
from .scheduler import start_scheduler, stop_scheduler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("market_signals")

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="BFSI market intelligence for IT-services sales teams, built from public disclosures and news."
)


# Public mode is read-only. Refusing every write method at the door, rather
# than guarding each route, means an endpoint added later cannot be forgotten.
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def public_read_only(request: Request, call_next):
    if settings.PUBLIC_MODE and request.method not in _READ_METHODS and request.url.path.startswith("/api"):
        return JSONResponse({"detail": "This is a read-only public copy of Keenr.ai."}, status_code=403)
    return await call_next(request)


@app.get("/api/app-config")
def app_config():
    """What the frontend needs to know about this deployment."""
    return {"name": settings.PROJECT_NAME, "public_mode": settings.PUBLIC_MODE}

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API Routers under /api
app.include_router(stats_router, prefix="/api")
app.include_router(companies_router, prefix="/api")
app.include_router(events_router, prefix="/api")
app.include_router(ingest_router, prefix="/api")
app.include_router(signals_router, prefix="/api")
app.include_router(exports_router, prefix="/api")
app.include_router(graph_router, prefix="/api")
app.include_router(tenders_router, prefix="/api")
app.include_router(results_router, prefix="/api")
app.include_router(insights_router, prefix="/api")
app.include_router(company_profile_router, prefix="/api")
app.include_router(opportunities_router, prefix="/api")
app.include_router(refresh_router, prefix="/api")
app.include_router(galaxy_router, prefix="/api")

# Static frontend folder
FRONTEND_DIR = PROJECT_DIR / "frontend"
if FRONTEND_DIR.exists():

    class NoCacheStaticFiles(StaticFiles):
        """Serve the frontend without caching (single-user local tool)."""

        async def get_response(self, path: str, scope):
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

    app.mount("/static", NoCacheStaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def serve_index():
        """
        Serve index.html with a cache-busting token stamped onto the local JS
        and CSS URLs.

        No-cache headers alone proved unreliable — some browsers kept serving a
        stale app.js/components.js regardless, so edits silently did nothing and
        every change needed a manual hard-reload. Deriving the token from the
        files' own mtimes means the URL changes exactly when the code does,
        which no cache can defeat, and costs nothing when nothing has changed.
        """
        html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        stamp = 0
        for rel in ("js/app.js", "js/components.js", "css/styles.css"):
            f = FRONTEND_DIR / rel
            if f.exists():
                stamp = max(stamp, int(f.stat().st_mtime))
        html = html.replace('href="/static/css/styles.css"', f'href="/static/css/styles.css?v={stamp}"')
        html = html.replace('src="/static/js/components.js"', f'src="/static/js/components.js?v={stamp}"')
        html = html.replace('src="/static/js/app.js"', f'src="/static/js/app.js?v={stamp}"')
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

@app.on_event("startup")
def on_startup():
    logger.info("Initializing database...")
    init_db()
    
    if settings.PUBLIC_MODE:
        logger.info(f"{settings.PROJECT_NAME} v{settings.VERSION} ready (public, read-only).")
        return

    logger.info("Seeding initial company master universe...")
    db = SessionLocal()
    try:
        count = seed_companies(db)
        logger.info(f"Seeded {count} new companies into master universe.")

        # First deploy after Phase 3: build signals from the existing events so
        # the Ranked Signals tab isn't empty until the next scheduled run.
        if not settings.PUBLIC_MODE and settings.SIGNAL_RECOMPUTE_ON_STARTUP and db.query(Signal).count() == 0:
            logger.info("No signals yet — running initial signal recompute...")
            signal_engine.recompute(db, sector_filter=settings.SCHEDULE_SECTOR_FILTER)
    finally:
        db.close()
    
    logger.info("Starting ingestion scheduler...")
    start_scheduler()

    logger.info(f"{settings.PROJECT_NAME} v{settings.VERSION} ready.")

@app.on_event("shutdown")
def on_shutdown():
    stop_scheduler()

@app.get("/health")
def health_check():
    return {"status": "healthy", "version": settings.VERSION}
