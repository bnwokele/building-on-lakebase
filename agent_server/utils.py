"""Request/stream helpers for the OpenAI Agents SDK server.

Kept close to the `agent-openai-agents-sdk` template's utils, with one change: `get_session_id`
also reads `custom_inputs.thread_id`, which is what this app's single-page UI sends (see
static/index.html) — so a browser "New chat" starts a fresh managed session.
"""
import logging
from typing import AsyncGenerator, AsyncIterator, Optional
from uuid import uuid4

from agents.result import StreamEvent
from databricks.sdk import WorkspaceClient
from mlflow.genai.agent_server import get_request_headers
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentStreamEvent


def get_session_id(request: ResponsesAgentRequest) -> str | None:
    """The conversation/session id. The UI sends custom_inputs.thread_id; also accept session_id
    and the platform conversation_id. None → the caller mints a fresh one."""
    ci = getattr(request, "custom_inputs", None)
    if isinstance(ci, dict):
        for key in ("thread_id", "session_id"):
            if ci.get(key):
                return str(ci[key])
    ctx = getattr(request, "context", None)
    if ctx and getattr(ctx, "conversation_id", None):
        return request.context.conversation_id
    return None


def get_databricks_host(workspace_client: WorkspaceClient | None = None) -> Optional[str]:
    workspace_client = workspace_client or WorkspaceClient()
    try:
        return workspace_client.config.host
    except Exception as e:  # noqa: BLE001
        logging.exception(f"Error getting databricks host from env: {e}")
        return None


def get_user_workspace_client() -> WorkspaceClient:
    """On-behalf-of-user client (from the forwarded access token) — for tools that must act AS the
    signed-in user. Our data tools run as the app SP, so this is here for parity/extension."""
    token = get_request_headers().get("x-forwarded-access-token")
    return WorkspaceClient(token=token, auth_type="pat")


async def process_agent_stream_events(
    async_stream: AsyncIterator[StreamEvent],
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    """Adapt OpenAI Agents SDK stream events to MLflow ResponsesAgent stream events.

    Copied from the template: rewrite raw response events with a stable per-item id, and surface
    each tool-call OUTPUT as a `response.output_item.done` so the UI can pick up proposals."""
    curr_item_id = str(uuid4())
    async for event in async_stream:
        if event.type == "raw_response_event":
            event_data = event.data.model_dump()
            if event_data["type"] == "response.output_item.added":
                curr_item_id = str(uuid4())
                event_data["item"]["id"] = curr_item_id
            elif event_data.get("item") is not None and event_data["item"].get("id") is not None:
                event_data["item"]["id"] = curr_item_id
            elif event_data.get("item_id") is not None:
                event_data["item_id"] = curr_item_id
            yield event_data
        elif event.type == "run_item_stream_event" and event.item.type == "tool_call_output_item":
            yield ResponsesAgentStreamEvent(
                type="response.output_item.done",
                item=event.item.to_input_item(),
            )
