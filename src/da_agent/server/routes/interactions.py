"""Resume a paused interaction (`AskUserQuestion` answers / plan verdict).

Frontend calls these endpoints while an SSE stream is open for the session — the
posted body resolves the matching `Future` parked by `WebAgentUI`, the SDK gets
its `PermissionResultAllow / Deny`, and more events flow into the same SSE stream.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..schemas import (
    PendingInteractionResponse,
    PendingInteractionsListResponse,
    PlanResponseSubmission,
    QuestionResponseSubmission,
)
from ..state import AppState

router = APIRouter(prefix="/sessions", tags=["interactions"])


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


@router.get(
    "/{sid}/interactions/pending",
    response_model=PendingInteractionsListResponse,
)
async def list_pending(
    sid: str, state: AppState = Depends(get_state)
) -> PendingInteractionsListResponse:
    if await state.registry.get(sid) is None:
        raise HTTPException(status_code=404, detail="session not found")
    items = await state.interactions.pending(sid)
    return PendingInteractionsListResponse(
        pending=[
            PendingInteractionResponse(
                tool_use_id=p.tool_use_id, kind=p.kind, payload=p.payload
            )
            for p in items
        ]
    )


@router.post(
    "/{sid}/interactions/{tool_use_id}/respond",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def respond_interaction(
    sid: str,
    tool_use_id: str,
    request: Request,
    state: AppState = Depends(get_state),
) -> None:
    if await state.registry.get(sid) is None:
        raise HTTPException(status_code=404, detail="session not found")

    pending_items = await state.interactions.pending(sid)
    target = next((p for p in pending_items if p.tool_use_id == tool_use_id), None)
    if target is None:
        raise HTTPException(
            status_code=404, detail="no pending interaction for this tool_use_id"
        )

    body = await request.json()
    if target.kind == "question":
        submission = QuestionResponseSubmission.model_validate(body)
        value = [a.model_dump() for a in submission.answers]
    elif target.kind == "plan":
        submission = PlanResponseSubmission.model_validate(body)
        value = submission.model_dump()
    else:
        raise HTTPException(status_code=400, detail=f"unknown kind: {target.kind}")

    if not await state.interactions.resolve(sid, tool_use_id, value):
        raise HTTPException(status_code=409, detail="interaction was already resolved")

    # Tell the live SSE stream the parked entry is gone so the FE reducer can
    # pop it from `pendingInteractions[]`. Without this signal the reducer
    # accumulates stale entries and the head of the queue never advances to a
    # second AskUserQuestion within the same turn.
    runtime = await state.get_or_create_runtime(sid)
    if runtime is not None and runtime.ui is not None:
        runtime.ui.emit_interaction_resolved(tool_use_id, target.kind)
