"""Agent server entry point — OpenAI Agents SDK + managed sessions/memory.

Plain synchronous MLflow `AgentServer` (serves `/responses` — the standard Responses API —
and the equivalent `/invocations`; ask→answer). We serve our own dashboard
and `/api` routes from webapp.py, so the template's Node chat proxy is off. The authenticated user
(X-Forwarded-Email) is read per-request from the headers in agent_server.managed.get_user_id — no
middleware needed. load_dotenv must run before importing the agent (auth config).
"""

# ruff: noqa: E402
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=True)

from mlflow.genai.agent_server import AgentServer, setup_mlflow_git_based_version_tracking

# Import the agent to register the @invoke/@stream handlers with the server.
import agent_server.agent  # noqa: F401

agent_server = AgentServer("ResponsesAgent", enable_chat_proxy=False)

# Module-level app so multiple workers can import it.
app = agent_server.app  # noqa: F841

setup_mlflow_git_based_version_tracking()

# Dashboard + /api routes.
from agent_server import webapp  # noqa: E402

app.include_router(webapp.router)
app.include_router(webapp.ui)


def main():
    agent_server.run(app_import_string="agent_server.start_server:app")
