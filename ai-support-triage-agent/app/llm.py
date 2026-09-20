"""Thin wrapper around the Hugging Face Inference API.

Why a wrapper? Every call to an external AI service can fail (rate limit,
cold start, network). We convert *any* failure into one exception type,
`LLMUnavailable`, so the rest of the app can handle it in one place.
"""
import logging

import numpy as np
from huggingface_hub import InferenceClient

from .config import get_settings

log = logging.getLogger("llm")


class LLMUnavailable(Exception):
    """Raised when the Hugging Face API cannot be used or fails."""


def _client() -> InferenceClient:
    s = get_settings()
    if not s.hf_token:
        raise LLMUnavailable("HF_TOKEN is not set")
    return InferenceClient(token=s.hf_token, timeout=45)


def chat(messages: list[dict], max_tokens: int = 400, temperature: float = 0.2) -> str:
    """Send a chat conversation to the LLM and return the text answer."""
    s = get_settings()
    try:
        out = _client().chat_completion(
            messages=messages,
            model=s.llm_model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return out.choices[0].message.content or ""
    except LLMUnavailable:
        raise
    except Exception as exc:  # network, quota, model loading, bad response...
        log.warning("LLM call failed: %s", exc)
        raise LLMUnavailable(str(exc)) from exc


def embed(texts: list[str]) -> np.ndarray:
    """Turn texts into unit-length vectors so cosine similarity = dot product."""
    s = get_settings()
    client = _client()
    vectors = []
    try:
        for text in texts:
            v = np.asarray(client.feature_extraction(text, model=s.embed_model), dtype="float32")
            if v.ndim > 1:  # some models return one vector per token -> average them
                v = v.reshape(-1, v.shape[-1]).mean(axis=0)
            vectors.append(v / (np.linalg.norm(v) + 1e-9))
    except LLMUnavailable:
        raise
    except Exception as exc:
        log.warning("Embedding call failed: %s", exc)
        raise LLMUnavailable(str(exc)) from exc
    return np.vstack(vectors)
