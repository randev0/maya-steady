# Maya Steady

Maya Steady is a policy-governed sales chatbot for Steadigital. It runs a FastAPI backend, stores lead and conversation state in PostgreSQL, and supports WhatsApp, Facebook Messenger, and a local test chat interface.

The current direction keeps Maya lean and deterministic:

- qualification, handoff, follow-up, and opt-out rules live in code
- the model is used for bounded phrasing and extraction, not business-critical decisions
- lead-state normalization and auditability are enforced in the backend

## Core Capabilities

- WhatsApp and Facebook inbound message handling
- lead qualification and state progression in code
- human handoff routing and pause controls
- follow-up scheduling and delivery
- admin dashboard for conversations, leads, analytics, and handoffs
- test coverage for policy, auditability, fallback behavior, and runtime safety

## Stack

| Component | Technology |
| --- | --- |
| API runtime | FastAPI |
| Database | PostgreSQL + asyncpg |
| LLM path | Ollama by default, optional OpenRouter path |
| Dashboard | Jinja2 templates + static assets |
| Messaging | WhatsApp gateway, Facebook Messenger, test chat |
| Tests | pytest |

## Repository Layout

```text
.
├── agent.py
├── main.py
├── manager_agent.py
├── policy.py
├── lead_state.py
├── llm.py
├── config.py
├── database/
├── tools/
├── agent_config/
├── dashboard/
├── wa_gateway/
├── tests/
└── k8s/
```

## Environment

Copy `.env.example` to `.env` and set the values you need.

Important settings:

- `DATABASE_URL`
- `LLM_PROVIDER`
- `OLLAMA_BASE_URL`
- `OLLAMA_MODEL`
- `OPENROUTER_API_KEY`
- `FB_PAGE_ACCESS_TOKEN`
- `FB_VERIFY_TOKEN`
- `WA_PHONE_NUMBER_ID`
- `WA_ACCESS_TOKEN`
- `WA_VERIFY_TOKEN`
- `MANAGER_WA_ID`
- `DASHBOARD_URL`

Note: the app also contains a local WhatsApp gateway integration in `wa_gateway/`. Review the gateway-specific service and package files before deploying that path.

## Local Run

1. Create a PostgreSQL database and set `DATABASE_URL`.
2. Install Python dependencies:

```bash
pip install -r requirements.txt
```

3. Start the API:

```bash
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

4. Open the dashboard at `http://localhost:8080`.

If you are using the Node-based WhatsApp gateway, install its dependencies separately:

```bash
cd wa_gateway
npm install
```

## Test

```bash
pytest
```

## Current Architecture Notes

- `policy.py` owns qualification progression, missing-field logic, duplicate-question prevention, handoff triggers, opt-out, and follow-up progression.
- `lead_state.py` normalizes lead-state values so legacy data can still be read safely.
- `agent.py` computes policy decisions before calling the model.
- `main.py` handles webhook ingestion, dashboard routes, message persistence, and follow-up dispatch.
- `database/schema.sql` includes auditability tables for state transitions and tool outcomes.

## Deployment

This repository includes:

- `agent.service` for a systemd-style runtime
- `wa_gateway/wa-gateway.service` for the gateway process
- `k8s/agent-ingress.yaml` for ingress configuration

Review environment variables, secrets handling, and channel configuration before deploying to production.
