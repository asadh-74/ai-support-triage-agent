"""HTTP layer.

  POST /api/triage   used by n8n (protected by X-API-Key)
  POST /api/demo     used by the web page (public, but rate limited)
  GET  /api/metrics  live counters shown on the page
  GET  /api/kb       what the agent currently knows, and when it was last refreshed
  POST /api/kb/sync  re-scrape the company website with Firecrawl (X-API-Key required)
  GET  /health       used by Render to know the service is alive
"""
import logging
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from . import crawler
from .agent import handle_ticket
from .config import get_settings
from .schemas import TicketIn, TriageOut
from .tools import get_kb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("api")

def _startup_sync() -> None:
    try:
        crawler.sync_kb()
    except Exception as exc:  # never let a failed sync stop the service from starting
        log.warning("Startup KB sync failed: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    s = get_settings()
    # Render's free disk is wiped on every restart, so scraped pages must be re-fetched on boot.
    # Runs in the background so the service answers health checks immediately.
    if s.kb_sync_on_startup and s.firecrawl_api_key and s.kb_source_urls:
        threading.Thread(target=_startup_sync, daemon=True).start()
    yield


app = FastAPI(title="AI Support Triage Agent", version="1.1.0", lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"


# ---------- tiny in-memory metrics (resets on restart; use Prometheus/DB in production) ----------

class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.total = self.auto = self.escalated = 0
        self.latency_sum = 0

    def record(self, result: TriageOut) -> None:
        with self.lock:
            self.total += 1
            self.latency_sum += result.latency_ms
            if result.action == "auto_reply":
                self.auto += 1
            else:
                self.escalated += 1

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "total": self.total, "auto_replied": self.auto, "escalated": self.escalated,
                "avg_latency_ms": int(self.latency_sum / self.total) if self.total else 0,
                "auto_resolution_rate": round(self.auto / self.total, 2) if self.total else 0.0,
            }


metrics = Metrics()

# ---------- simple per-IP rate limit for the public demo endpoint ----------

_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")  # Render sits behind a proxy
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "?")


def _rate_limit(request: Request) -> None:
    limit = get_settings().demo_rate_limit_per_min
    now = time.time()
    ip = _client_ip(request)
    with _hits_lock:
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, f"Demo limit reached: {limit} tickets per minute. Wait a moment and retry.")
        q.append(now)


def _process(ticket: TicketIn) -> TriageOut:
    result = handle_ticket(ticket)
    metrics.record(result)
    log.info("ticket=%s category=%s urgency=%s action=%s conf=%.2f latency_ms=%d mode=%s",
             result.ticket_id, result.category, result.urgency, result.action,
             result.confidence, result.latency_ms, result.mode)
    return result


# ---------- routes ----------

@app.get("/health")
def health():
    s = get_settings()
    return {"status": "ok", "mode": "llm" if s.hf_token else "demo", "model": s.llm_model}


def _check_key(x_api_key: str | None, *, required: bool = False) -> None:
    expected = get_settings().api_key
    if required and not expected:
        raise HTTPException(403, "This endpoint is disabled until API_KEY is configured on the server")
    if expected and not secrets.compare_digest(x_api_key or "", expected):
        raise HTTPException(401, "Invalid or missing X-API-Key header")


@app.post("/api/triage", response_model=TriageOut)
def triage_endpoint(ticket: TicketIn, x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    return _process(ticket)


@app.post("/api/demo", response_model=TriageOut)
def demo_endpoint(ticket: TicketIn, request: Request):
    _rate_limit(request)
    return _process(ticket)


@app.get("/api/metrics")
def metrics_endpoint():
    return metrics.snapshot()


@app.get("/api/kb")
def kb_status():
    chunks = get_kb().chunks
    documents = sorted({c["source"].split(" > ")[0] for c in chunks})
    return {
        "sections": len(chunks),
        "documents": len(documents),
        "web_pages": [d for d in documents if d.startswith("http")],
        "firecrawl_configured": bool(get_settings().firecrawl_api_key and get_settings().kb_source_urls),
        "last_sync": crawler.last_sync,
    }


@app.post("/api/kb/sync")
def kb_sync(x_api_key: str | None = Header(default=None)):
    """Spends Firecrawl credits, so it always requires an API key (and refuses to run if none is set)."""
    _check_key(x_api_key, required=True)
    try:
        return crawler.sync_kb()
    except crawler.CrawlerError as exc:
        log.warning("KB sync failed: %s", exc)
        raise HTTPException(502, str(exc))


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")
