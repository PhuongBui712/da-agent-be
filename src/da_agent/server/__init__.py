"""FastAPI backend for the DA-Agent.

Mirrors the in-process CLI flow over HTTP + SSE: each session owns a long-lived
`AgentRunner` whose events are pushed to clients as Server-Sent Events. The same
`AgentUI` Protocol is reused — `WebAgentUI` is the only adapter that changes.
"""

from .app import create_app

__all__ = ["create_app"]
