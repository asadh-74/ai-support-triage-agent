import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field


def _new_id() -> str:
    return "T-" + uuid.uuid4().hex[:8]


class TicketIn(BaseModel):
    """What n8n (or the demo page) sends us."""
    ticket_id: str = Field(default_factory=_new_id, max_length=64)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    customer_email: Optional[str] = Field(default=None, max_length=200)


class TriageOut(BaseModel):
    """What we send back. n8n branches on `action`."""
    ticket_id: str
    category: str
    urgency: str
    sentiment: str
    action: Literal["auto_reply", "escalate"]
    reply: str = Field(description="Drafted answer, or a holding message when escalating")
    confidence: float
    reason: str = Field(description="Why the agent chose this action")
    sources: list[str] = []
    trace: list[dict] = Field(default=[], description="Step-by-step agent reasoning (audit log)")
    latency_ms: int
    mode: Literal["llm", "demo"]
    model: str
