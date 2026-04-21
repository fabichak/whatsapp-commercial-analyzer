"""Pydantic models for every pipeline artifact.

See TECH_PLAN.md §"Shared schemas" for the canonical field list.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Message(BaseModel):
    msg_id: int
    ts_ms: int
    from_me: bool
    text: str
    text_raw: str


class Conversation(BaseModel):
    chat_id: int
    phone: str
    messages: list[Message]


class SpaTemplate(BaseModel):
    template_id: int
    canonical_text: str
    instance_count: int
    example_msg_ids: list[int]
    first_seen_ts: int
    last_seen_ts: int


class ScriptStep(BaseModel):
    id: str
    name: str
    canonical_texts: list[str]
    expected_customer_intents: list[str]
    transitions_to: list[str]


ObjectionId = Literal[
    "price",
    "location",
    "time_slot",
    "competitor",
    "hesitation_vou_pensar",
    "delegated_talk_to_someone",
    "delayed_response_te_falo",
    "trust_boundary_male",
    "other",
]


class ObjectionType(BaseModel):
    id: ObjectionId
    name_pt: str
    triggers: list[str]


class LabeledMessage(BaseModel):
    msg_id: int
    chat_id: int
    from_me: bool
    step_id: str | None
    step_context: Literal["on_script", "off_script", "transition", "unknown"]
    intent: str | None = None
    objection_type: str | None = None
    sentiment: Literal["pos", "neu", "neg"] | None = None
    matches_script: bool | None = None
    deviation_note: str | None = None


class TemplateSentiment(BaseModel):
    template_id: int
    warmth: int
    clarity: int
    script_adherence: int
    polarity: Literal["pos", "neu", "neg"]
    critique: str


class ConversationConversion(BaseModel):
    chat_id: int
    phone: str
    conversion_score: int
    conversion_evidence: str
    first_objection_idx: int | None
    first_objection_type: str | None
    resolution_idx: int | None
    winning_reply_excerpt: str | None
    final_outcome: Literal["booked", "lost", "ambiguous"]


class Turnaround(BaseModel):
    chat_id: int
    phone: str
    date: str
    objection_type: str
    customer_message: str
    winning_reply: str
    winning_reply_msg_id: int
    confirmation: str
    paired_lost_deals: list[int]


class OffScriptCluster(BaseModel):
    step_id: str
    medoid_text: str
    size: int
    example_msg_ids: list[int]


class PerStepAgg(BaseModel):
    step_id: str
    on_script_count: int
    off_script_count: int
    top_intents: list[tuple[str, int]]
    top_clusters: list[OffScriptCluster]
    top_objections: list[tuple[str, int]]


class Aggregation(BaseModel):
    per_step: dict[str, PerStepAgg]
    off_script_clusters: list[OffScriptCluster]
