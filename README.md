# Customer Support Copilot on Databricks Apps + Lakebase

A customer-support copilot built as a **Databricks App**, on the **OpenAI Agents SDK (using** `agent-openai-agents-sdk` [template)](https://github.com/databricks/app-templates/tree/main/agent-openai-agents-sdk), with **Databricks managed sessions + memory** and **Lakebase** for operational data and hybrid search.

It serves a single web page — an operational **tickets dashboard** and a **chat copilot** — backed by
one Databricks App that runs the agent and the dashboard API.

## What it does

- **Grounded answers.** The agent looks up a customer's profile and tickets and searches a knowledge
base before answering, instead of guessing.
- **Remembers the conversation** (short-term) and **remembers the user across conversations**
(long-term) using Databricks managed state — no memory database to run yourself.
- **Acts safely.** When the agent wants to change a ticket, it *proposes* the change; a human confirms
in the UI before anything is written, and the change is recorded as made by the agent.
- **Observable.** Every turn is traced to MLflow automatically.



## Architecture

The app talks to **two Lakebase projects, two different ways**: *your* project over a direct Postgres
connection, and a *Databricks-managed* project over a REST API.

```
          ┌─────────────────────────────────────────────────────────────┐
          │                   Browser — one web page                     │
          │          Tickets panel        │        Copilot chat          │
          └───────────┬───────────────────────────────────┬─────────────┘
                      │ /api/*                             │ /responses
                      ▼                                    ▼
          ┌─────────────────────────────────────────────────────────────┐
          │  Databricks App "customer-support-copilot" (OpenAI Agents SDK)│
          │     runs as a SERVICE PRINCIPAL (SP)                          │
          │                                                               │
          │   /api routes (webapp.py)      MLflow ResponsesAgent          │
          │        │                       → Agent + Runner (agent.py)    │
          │        │                   tools: search_customer ·           │
          │        │                   search_kb · propose_ticket ·       │
          │        │                   search_memory · save_memory        │
          │        ▼                        │              │              │
          │   db.py → LakebasePool    managed.py: session_store + memory  │
          └──────────┬──────────────────────┬──────────────┬─────────────┘
                     │                       │              │
      DIRECT POSTGRES│ (psycopg)       REST  │ (databricks-sdk / AgentKit)
      OAuth token AS │ the password          │ bearer OAuth, auto-rotated
                     ▼                       ▼              ▼
   ┌──────────────────────────┐  ┌───────────────────────────────────────────┐
   │  YOUR Lakebase project   │  │  Databricks-managed project                 │
   │   customer_support (rw)  │  │   memory store  → database  memory-<name>   │
   │   kb_synced (synced +    │  │   session store → session items             │
   │     vector + bm25 search)│  │   • owned by the app service principal      │
   └──────────┬───────────────┘  │   • entries/sessions scoped by actor_id     │
              ▲ synced tables     │   • Databricks runs the Postgres for you    │
   ┌──────────────────────────┐  └───────────────────────────────────────────┘
   │  Unity Catalog / Lakehouse│   Model Serving: an LLM + an embedding model
   └──────────────────────────┘
```

- **Operational data** (customers, tickets, knowledge base) lives in *your* Lakebase project. The app
connects to it like any Postgres database via `databricks_ai_bridge.lakebase.LakebasePool`, using a
short-lived Databricks OAuth token in place of a password (auto-rotated — no stored secret).
- **Session + memory** live in a Databricks-managed project. The app never opens a database to it;
every call is a REST request over an auto-rotated OAuth token. Databricks provisions and runs that
database.
- **Identity.** The user that scopes memory comes from the authenticated request header
(`X-Forwarded-Email`), never from client input.



## Repo layout


| Path                                 | What                                                                                            |
| ------------------------------------ | ----------------------------------------------------------------------------------------------- |
| `agent_server/agent.py`              | The agent: `Agent` + `Runner`, model routed at Databricks serving, `@invoke`/`@stream` handlers |
| `agent_server/managed.py`            | Databricks managed session + memory (session store adapter, memory tools)                       |
| `agent_server/tools_support.py`      | Agent tools: `search_customer`, `search_kb`, `propose_ticket_action`                            |
| `agent_server/db.py` · `search.py`   | Lakebase access (`LakebasePool`) + hybrid KB search (vector + BM25, RRF)                        |
| `agent_server/webapp.py` · `static/` | `/api/*` dashboard routes + the single-page UI                                                  |
| `agent_server/start_server.py`       | MLflow `AgentServer` entry point (serves `/responses` + `/invocations`)                         |
| `app.yaml` · `databricks.yml`        | App run spec + Databricks Asset Bundle (resources, env, variables)                              |
| `scripts/`                           | Setup + deploy helpers (synced tables, seed, deploy pipeline, tests)                            |
| `sql/` · `lakehouse/`                | Operational table DDL/seed, KB search indexes, and the lakehouse KB build                       |




## Prerequisites

- A Databricks workspace with **Lakebase**, **Model Serving** (an LLM + an embedding endpoint), and
**managed sessions/memory** available.
- The [Databricks CLI](https://docs.databricks.com/dev-tools/cli/) authenticated to your workspace.
- `[uv](https://docs.astral.sh/uv/)` for local Python.



## Quick start

```bash
cp .env.example .env          # fill in your profile, Lakebase project, UC catalog, endpoints
uv sync                       # install dependencies

# Run locally (serves the dashboard + agent on http://localhost:8000)
uv run start-server

# Deploy to a Databricks App (Databricks Asset Bundle)
databricks bundle deploy --var app_name=customer-support-copilot
```

See **[DEPLOY.md](DEPLOY.md)** for the full deploy walkthrough (synced tables, seed data, resource
binding, and granting the app's service principal access to Lakebase).

## Built on

- [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) (`openai-agents`)
- `[databricks-openai](https://pypi.org/project/databricks-openai/)` — routes the SDK at Databricks model serving
- [Databricks AgentKit SDK](https://docs.databricks.com/aws/en/agents/agent-memory/) — managed sessions + memory
([managed sessions](https://docs.databricks.com/aws/en/agents/agent-memory/managed-sessions),
[managed memory](https://docs.databricks.com/aws/en/agents/agent-memory/managed-memory))
- `[databricks-ai-bridge](https://pypi.org/project/databricks-ai-bridge/)` — Lakebase connection pool
- [MLflow](https://mlflow.org/) `ResponsesAgent` — standard agent interface + tracing
- Starts from the `agent-openai-agents-sdk` [template](https://github.com/databricks/app-templates/tree/main/agent-openai-agents-sdk)

