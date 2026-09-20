"""Firecrawl integration: keep the knowledge base in sync with the company's live website.

Real companies don't keep policies in a folder. They live on a help-center or website
that marketing and support update all the time. Firecrawl turns those web pages into
clean markdown, and we save that markdown next to the hand-written KB files so the
agent's RAG search covers it automatically.

Safety notes
  * Only URLs listed in the KB_SOURCE_URLS environment variable are ever fetched.
    There is no endpoint that accepts a URL from a user (that would be an SSRF risk).
  * Web content is untrusted. It is only ever shown to the LLM as reference data.
  * A sync that returns nothing usable never wipes the existing knowledge.

Run manually:  python -m app.crawler
"""
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from .config import get_settings

log = logging.getLogger("crawler")

MAX_PAGE_CHARS = 20_000   # keep one huge page from flooding the knowledge base
MIN_PAGE_CHARS = 200      # pages shorter than this are usually error or empty pages
FILE_PREFIX = "web_"      # we only ever create or delete files with this prefix

_sync_lock = threading.Lock()
last_sync: dict = {"status": "never", "at": None, "pages": 0, "error": None}


class CrawlerError(Exception):
    """Raised for configuration problems or when Firecrawl returns nothing usable."""


def _client():
    key = get_settings().firecrawl_api_key
    if not key:
        raise CrawlerError("FIRECRAWL_API_KEY is not set")
    from firecrawl import Firecrawl  # imported lazily so the app starts fast without it

    return Firecrawl(api_key=key)


def _get(obj, name, default=None):
    """Read a field from either an SDK object or a plain dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def fetch_pages(urls: tuple, crawl_limit: int) -> list[dict]:
    """Ask Firecrawl for the main content of each page, as markdown.

    crawl_limit == 1  -> scrape exactly the URLs you listed (1 credit per page)
    crawl_limit  > 1  -> treat each URL as a starting point and crawl up to that many pages
    """
    fc = _client()
    pages: list[dict] = []
    for url in urls:
        try:
            if crawl_limit > 1:
                job = fc.crawl(url, limit=crawl_limit, formats=["markdown"], only_main_content=True)
                docs = _get(job, "data", []) or []
            else:
                docs = [fc.scrape(url, formats=["markdown"], only_main_content=True)]
        except Exception as exc:  # network, quota, blocked page...
            log.warning("Firecrawl failed for %s: %s", url, exc)
            continue
        for doc in docs:
            meta = _get(doc, "metadata")
            pages.append({
                "url": _get(meta, "source_url") or _get(meta, "url") or url,
                "title": _get(meta, "title") or "",
                "markdown": _get(doc, "markdown") or "",
            })
    return pages


def clean_markdown(md: str) -> str:
    """Strip things that only add noise for retrieval: images, link URLs, blank-line runs."""
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)          # images
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)       # [text](url) -> text
    md = re.sub(r"<!--.*?-->", "", md, flags=re.DOTALL)    # html comments (also blocks fake source tags)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()[:MAX_PAGE_CHARS]


def _slug(url: str) -> str:
    parsed = urlparse(url)
    raw = f"{parsed.netloc}{parsed.path}".strip("/") or "page"
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:80]


def to_kb_file(page: dict) -> tuple[str, str] | None:
    """Turn one scraped page into (filename, file content), or None if it is not usable."""
    body = clean_markdown(page["markdown"])
    if len(body) < MIN_PAGE_CHARS:
        return None
    title = page["title"].strip() or _slug(page["url"])
    header = f"<!-- source: {page['url']} -->\n"
    if not body.lstrip().startswith("#"):
        body = f"# {title}\n\n{body}"
    return f"{FILE_PREFIX}{_slug(page['url'])}.md", header + body + "\n"


def _check_sync_dir(kb_dir: str, sync_dir: str) -> Path:
    """The sync folder must be a subfolder of the KB, never the KB itself,
    because a sync replaces the old synced files."""
    kb, out = Path(kb_dir).resolve(), Path(sync_dir).resolve()
    if out == kb or kb not in out.parents:
        raise CrawlerError("KB_SYNC_DIR must be a sub-folder of KB_DIR (default kb/synced)")
    return out


def sync_kb() -> dict:
    """Scrape the configured URLs and replace the synced part of the knowledge base."""
    s = get_settings()
    if not s.kb_source_urls:
        raise CrawlerError("KB_SOURCE_URLS is empty. Set it to a comma-separated list of page URLs.")
    out = _check_sync_dir(s.kb_dir, s.kb_sync_dir)

    if not _sync_lock.acquire(blocking=False):
        raise CrawlerError("A sync is already running")
    try:
        started = time.time()
        pages = fetch_pages(s.kb_source_urls, s.kb_crawl_limit)
        files = {}
        for page in pages:
            item = to_kb_file(page)
            if item:
                files[item[0]] = item[1]
        if not files:
            raise CrawlerError("Firecrawl returned no usable content. The existing knowledge base was left unchanged.")

        # Only now that we have good content do we replace the old synced files.
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob(f"{FILE_PREFIX}*.md"):
            old.unlink()
        for name, content in files.items():
            (out / name).write_text(content, encoding="utf-8")

        from . import tools  # local import avoids a circular import at startup
        tools.get_kb.cache_clear()  # next search re-reads and re-embeds the knowledge base

        last_sync.update(status="ok", at=int(time.time()), pages=len(files), error=None)
        log.info("KB sync ok: %d pages in %.1fs", len(files), time.time() - started)
        return {"status": "ok", "pages": len(files), "files": sorted(files), "seconds": round(time.time() - started, 1)}
    except Exception as exc:
        last_sync.update(status="failed", at=int(time.time()), error=str(exc)[:300])
        raise
    finally:
        _sync_lock.release()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        print(sync_kb())
    except CrawlerError as err:
        raise SystemExit(f"Sync failed: {err}")
