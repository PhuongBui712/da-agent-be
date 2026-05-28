"""Pydantic request / response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# --- Sessions --------------------------------------------------------- #
class CreateSessionRequest(BaseModel):
    name: str = "untitled"


class RenameSessionRequest(BaseModel):
    name: str = Field(min_length=1)


class ForkSessionRequest(BaseModel):
    name: str | None = None


class SessionResponse(BaseModel):
    id: str
    name: str
    created_at: float
    updated_at: float
    parent_id: str | None = None


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]


# --- Messages --------------------------------------------------------- #
class AttachmentRef(BaseModel):
    """Wire ref used in MessageRequest body (spec §8.5)."""
    attachment_id: str


class MessageRequest(BaseModel):
    """Spec §8.5 — `prompt` is required; `kb_scope` and `attachments` are optional.

    `kb_scope=None`/missing → default-all READY KBs.
    `kb_scope=[]` → 400 (forces explicit per §8.5 validation table).
    """
    prompt: str = Field(min_length=1)
    kb_scope: list[str] | None = None
    attachments: list[AttachmentRef] = Field(default_factory=list)


# --- Interactions ----------------------------------------------------- #
class AnswerSubmission(BaseModel):
    header: str = ""
    selected: list[str] = Field(default_factory=list)
    other_text: str | None = None


class QuestionResponseSubmission(BaseModel):
    answers: list[AnswerSubmission] = Field(default_factory=list)


class PlanResponseSubmission(BaseModel):
    verdict: Literal["approve", "reject"]
    feedback: str | None = None


class PendingInteractionResponse(BaseModel):
    tool_use_id: str
    kind: str
    payload: dict[str, Any]


class PendingInteractionsListResponse(BaseModel):
    pending: list[PendingInteractionResponse]


# --- KB --------------------------------------------------------------- #
class KbFileResponse(BaseModel):
    id: str
    filename: str
    size_bytes: int
    status: Literal["PENDING", "PROCESSING", "READY", "FAILED"]
    created_at: float
    updated_at: float
    error: str | None = None


class KbFileListResponse(BaseModel):
    files: list[KbFileResponse]


# --- KB versions (spec §7, §8.2) ------------------------------------- #
class KbVersionResponse(BaseModel):
    version: str                              # "v1", "v2", ...
    parent_version: str                       # "raw" | "v<N-1>"
    operation: Literal["add_sheet", "overwrite_sheet"] | None = None
    sheet_affected: str | None = None
    source_session_id: str | None = None
    created_at: float
    size_bytes: int


class KbVersionListResponse(BaseModel):
    versions: list[KbVersionResponse]


# --- Attachments (spec §5.3) ----------------------------------------- #
class AttachmentResponse(BaseModel):
    attachment_id: str
    filename: str
    size_bytes: int
    mime: str
    uploaded_at: float


class AttachmentListResponse(BaseModel):
    attachments: list[AttachmentResponse]


# --- Outputs (spec §8.2) --------------------------------------------- #
class OutputResponse(BaseModel):
    output_id: str
    kind: Literal["standalone", "kb_version"]
    title: str
    filename: str
    mime: str
    size_bytes: int
    source_session_id: str | None = None
    source_kb_ids: list[str] = Field(default_factory=list)
    created_at: float


class OutputListResponse(BaseModel):
    outputs: list[OutputResponse]
