"""Support Copilot agent — OpenAI Agents SDK + Databricks managed sessions + memory.

State:
  • short-term  → the OpenAI Agents SDK **Session** slot, filled by the Databricks managed session
    (`managed.managed_session`). One arg to `Runner.run(session=...)` — the SDK loads prior turns
    before the run and appends the new ones after. No hand-rolled transcript plumbing.
  • long-term   → Databricks managed memory tools (`managed.memory_tools`), scoped per user.

The model runs through Databricks model serving via AsyncDatabricksOpenAI (chat_completions).
"""
import logging
import os
from datetime import datetime
from typing import AsyncGenerator

import mlflow
from agents import (
    Agent,
    Runner,
    function_tool,
    set_default_openai_api,
    set_default_openai_client,
)
from agents.tracing import set_trace_processors
from databricks_openai import AsyncDatabricksOpenAI
from mlflow.genai.agent_server import invoke, stream
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
)

from agent_server import managed
from agent_server.history import normalize_history_items
from agent_server.prompts import SYSTEM_PROMPT
from agent_server.tools_support import support_tools
from agent_server.utils import get_session_id, process_agent_stream_events

logger = logging.getLogger(__name__)

# Route the Agents SDK at Databricks model serving (works for all Databricks models except GPT-OSS).
set_default_openai_client(AsyncDatabricksOpenAI())
set_default_openai_api("chat_completions")
set_trace_processors([])  # trace via MLflow only
mlflow.openai.autolog()
logging.getLogger("mlflow.utils.autologging_utils").setLevel(logging.ERROR)

LLM_ENDPOINT_NAME = os.getenv("LLM_ENDPOINT", "databricks-claude-sonnet-4-5")


@function_tool
def get_current_time() -> str:
    """Get the current date and time."""
    return datetime.now().isoformat()


def create_agent(actor_id: str) -> Agent:
    """Build the agent. Tools = grounding/action tools + long-term memory tools bound to this user.
    Short-term state is NOT a tool — it's the Session passed to Runner.run."""
    tools = [get_current_time, *support_tools(), *managed.memory_tools(actor_id)]
    return Agent(
        name="Support Copilot",
        instructions=SYSTEM_PROMPT,
        model=LLM_ENDPOINT_NAME,
        tools=tools,
    )


def _prepare(request: ResponsesAgentRequest):
    """Resolve (session_id, actor, input messages, managed session) for a turn."""
    managed.ensure_stores()
    session_id = get_session_id(request) or f"s-{os.urandom(4).hex()}"
    mlflow.update_current_trace(metadata={"mlflow.trace.session": session_id})
    actor = managed.get_user_id(request) or "anonymous"
    if actor == "anonymous":
        logger.warning("No authenticated user / user_id — memory + session will use 'anonymous'.")
    messages = normalize_history_items([i.model_dump() for i in request.input])
    session = managed.managed_session(session_id, actor)  # native Databricks managed Session
    return session_id, actor, messages, session


@invoke()
async def invoke_handler(request: ResponsesAgentRequest) -> ResponsesAgentResponse:
    session_id, actor, messages, session = _prepare(request)
    agent = create_agent(actor)
    # Managed session in one arg: the SDK prepends prior turns and appends this one for us.
    result = await Runner.run(agent, messages, session=session)
    custom_outputs = {"session_id": session_id}
    if actor != "anonymous":
        custom_outputs["user_id"] = actor
    return ResponsesAgentResponse(
        output=[item.to_input_item() for item in result.new_items],
        custom_outputs=custom_outputs,
    )


@stream()
async def stream_handler(
    request: ResponsesAgentRequest,
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    _session_id, actor, messages, session = _prepare(request)
    agent = create_agent(actor)
    result = Runner.run_streamed(agent, input=messages, session=session)
    async for event in process_agent_stream_events(result.stream_events()):
        yield event
