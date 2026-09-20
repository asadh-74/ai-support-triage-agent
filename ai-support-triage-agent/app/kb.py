"""Knowledge base = RAG (Retrieval-Augmented Generation).

The LLM should never *guess* company policy. Instead we:
  1. split policy documents (kb/*.md) into small chunks,
  2. turn each chunk into an embedding vector with a Hugging Face model,
  3. at question time, find the chunks closest to the question,
  4. hand only those chunks to the LLM as evidence.

If the embedding API is down, we fall back to plain keyword matching so the
system degrades gracefully instead of crashing.
"""
import re
from pathlib import Path

import numpy as np

from . import llm
from .llm import LLMUnavailable

STOP = set(
    "a an the and or but if to of in on for with is are was were be been my me i you your we our it its "
    "this that these those at by from as have has had do does did not no can could would should will just "
    "please hi hello thanks thank get got what when where why how there they them their about into out "
    "order orders item items help need want know going".split()  # generic in e-commerce, so not evidence
)


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 2}


MAX_CHUNK_CHARS = 1200


def _split_long(block: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Scraped pages can have very long sections. Split them at paragraph breaks,
    repeating the heading so every piece still says what it is about."""
    if len(block) <= limit:
        return [block]
    head, _, body = block.partition("\n")
    pieces, current = [], ""
    for para in re.split(r"\n{2,}", body):
        if current and len(current) + len(para) > limit:
            pieces.append(f"{head}\n{current.strip()}")
            current = ""
        current += para + "\n\n"
    if current.strip():
        pieces.append(f"{head}\n{current.strip()}")
    return pieces


def load_chunks(kb_dir: str) -> list[dict]:
    """Read every .md file under kb_dir (including kb/synced/ from Firecrawl) and
    split it into chunks at each heading. Files scraped from the web start with
    `<!-- source: URL -->`, and that URL becomes the citation shown to the user."""
    chunks = []
    for path in sorted(Path(kb_dir).rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        origin = path.name
        match = re.match(r"\s*<!--\s*source:\s*(\S+)\s*-->", text)
        if match:
            origin = match.group(1)
        for block in re.split(r"\n(?=#{1,3} )", text):
            block = re.sub(r"^\s*<!--.*?-->\s*", "", block.strip())
            if len(block) < 30:
                continue
            heading = block.splitlines()[0].lstrip("# ").strip()
            for piece in _split_long(block):
                chunks.append({"source": f"{origin} > {heading}", "text": piece})
    return chunks


class KnowledgeBase:
    def __init__(self, kb_dir: str):
        self.chunks = load_chunks(kb_dir)
        self._vecs: np.ndarray | None = None
        self._embed_failed = False

    def _ensure_vectors(self) -> None:
        """Embed all chunks once (lazily, on first search)."""
        if self._vecs is not None or self._embed_failed or not self.chunks:
            return
        try:
            self._vecs = llm.embed([c["text"] for c in self.chunks])
        except LLMUnavailable:
            self._embed_failed = True

    def search(self, query: str, k: int = 3) -> list[dict]:
        if not self.chunks:
            return []
        self._ensure_vectors()
        if self._vecs is not None:
            try:
                q = llm.embed([query])[0]
                sims = self._vecs @ q
                top = np.argsort(-sims)[:k]
                return [
                    {**self.chunks[i], "score": round(float(sims[i]), 3), "method": "embeddings"}
                    for i in top
                ]
            except LLMUnavailable:
                pass  # fall through to keyword search
        return self._keyword_search(query, k)

    def _keyword_search(self, query: str, k: int) -> list[dict]:
        q = tokens(query)
        denom = max(1, min(len(q), 6))
        scored = []
        for c in self.chunks:
            heading = c["source"].split(">", 1)[-1]
            # words that appear in the article heading count triple: headings say what the article is about
            raw = (len(q & tokens(c["text"])) + 2 * len(q & tokens(heading))) / denom
            scored.append((raw, c))
        scored.sort(key=lambda pair: -pair[0])  # rank on the uncapped score so strong matches don't tie
        return [{**c, "score": round(min(1.0, raw), 3), "method": "keyword"} for raw, c in scored[:k]]
