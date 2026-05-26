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
class MessageRequest(BaseModel):
    prompt: str = Field(min_length=1)


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
