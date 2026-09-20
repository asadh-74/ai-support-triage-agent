"""All configuration comes from environment variables.

This is the "12-factor app" rule: code stays the same, config changes per
environment (your laptop, GitHub Actions, Render). No secrets live in the repo.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    hf_token: str
    llm_model: str
    embed_model: str
    api_key: str
    confidence_threshold: float
    max_steps: int
    kb_dir: str
    demo_rate_limit_per_min: int
    company_name: str
    firecrawl_api_key: str
    kb_source_urls: tuple
    kb_crawl_limit: int
    kb_sync_dir: str
    kb_sync_on_startup: bool


def get_settings() -> Settings:
    """Read env on every call so tests can change it easily."""
    return Settings(
        hf_token=os.getenv("HF_TOKEN", ""),
        llm_model=os.getenv("HF_LLM_MODEL", "Qwen/Qwen3.2-3B-Instruct"),
        embed_model=os.getenv("HF_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
        api_key=os.getenv("API_KEY", ""),
        confidence_threshold=float(os.getenv("CONFIDENCE_THRESHOLD", "0.7")),
        max_steps=int(os.getenv("MAX_AGENT_STEPS", "4")),
        kb_dir=os.getenv("KB_DIR", "kb"),
        demo_rate_limit_per_min=int(os.getenv("DEMO_RATE_LIMIT_PER_MIN", "8")),
        company_name=os.getenv("COMPANY_NAME", "ShopNova"),
        # --- Firecrawl: pull live help-center pages into the knowledge base ---
        firecrawl_api_key=os.getenv("FIRECRAWL_API_KEY", ""),
        kb_source_urls=tuple(u.strip() for u in os.getenv("KB_SOURCE_URLS", "").split(",") if u.strip()),
        kb_crawl_limit=int(os.getenv("KB_CRAWL_LIMIT", "1")),  # 1 = scrape only the listed pages
        kb_sync_dir=os.getenv("KB_SYNC_DIR", "kb/synced"),
        kb_sync_on_startup=os.getenv("KB_SYNC_ON_STARTUP", "false").lower() == "true",
    )
