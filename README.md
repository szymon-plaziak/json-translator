# robotctl

A **universal robot controller** served as a headless REST API: natural
language in via `POST /commands`, an LLM plan in the robot's own command
language, deterministic safety validation, and step-by-step execution
forwarded to the **real robot over HTTP**.

The core is robot-agnostic. Everything specific to a robot — its actions,
parameters, limits and the exact JSON format it expects — comes from a
**robot profile** you upload at runtime. The same binary controls a gantry
crane, a rover, or anything else that speaks HTTP.

The LLM only *proposes* plans. Deterministic application code validates every
proposal against the uploaded profile, executes plans one step at a time with
fresh telemetry between steps, and owns an emergency stop that bypasses the
LLM entirely. There is no simulation and no interactive UI.

```
┌──────────────────┐   text    ┌──────────┐  validated JSON  ┌────────────┐
│ Your application ├──────────►│ LLM      ├─────────────────►│ Safety     │
│ (REST client)    │           │ planner  │  (profile-driven)│ validator  │
└────────┬─────────┘           └──────────┘                  └─────┬──────┘
         │ HTTP 127.0.0.1          step-by-step, fresh             │
         │                         telemetry between               ▼
┌────────┴─────────┐                ┌──────────┐            ┌──────────────┐
│ Local REST API   ├───────────────►│ Executor ├───────────►│ Robot HTTP   │
│ (robotctl serve) │                │ + stop   │            │ endpoints    │
└──────────────────┘                └──────────┘            └──────────────┘
```

**See [`API_README.md`](API_README.md) for the full endpoint reference, the
robot profile format and the robot-side HTTP contract.**

## Requirements

- Linux (developed for WSL2 on Windows 11)
- [`uv`](https://docs.astral.sh/uv/) for Python + dependency management
- Any **OpenAI-compatible** chat-completions endpoint (vLLM, llama.cpp,
  Ollama, LM Studio, …)
- A robot (or its companion app) exposing the three HTTP endpoints of the
  bridge contract: telemetry, command, stop

## Quickstart

```bash
uv sync                                   # create venv, install dependencies
cp .env.example .env                      # then edit: LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
uv run robotctl                           # API service on 127.0.0.1:8081 (Ctrl+C to stop)
curl -X POST http://127.0.0.1:8081/profile -d @my_robot.json -H 'Content-Type: application/json'
curl -X POST http://127.0.0.1:8081/commands -d '{"command": "lift it half a meter"}' \
     -H 'Content-Type: application/json'
```

Without a robot configured the service still runs (planning, profile upload,
validation); `/health` reports `ok:false` and execution fails safely until
the robot is reachable.

## REST API (loopback only)

| Endpoint | Purpose |
| --- | --- |
| `POST /profile` | Upload/replace the robot profile (validated, persisted, hot-applied) |
| `GET /profile` | Current profile |
| `POST /commands` | Natural language → `{"id", "status"}` (202); 409 if busy |
| `GET /commands/{id}` | Command status, plan and per-step results |
| `GET /state` | Live robot telemetry (fetched on demand) |
| `POST /stop` | Emergency stop — bypasses the LLM immediately |
| `GET /health` | Robot link + profile status |

Bind address defaults to `127.0.0.1`; do **not** expose it to a network
without adding authentication first.

## Safety model

1. **Profile-driven validation** — every step is checked against the uploaded
   profile: action exists, parameter types, required parameters, min/max
   ranges. Unknown actions or parameters are rejected before execution.
2. **Never trust the LLM** — strict envelope parsing, per-profile step
   validation at parse time (with automatic corrective retries), and a
   deterministic safety layer as defense in depth. Ambiguous commands are
   decided by the model with conservative values, never asked back.
3. **Step-by-step execution** — a telemetry fetch is required between steps;
   an unreachable or disconnected robot aborts the plan with a stop command.
4. **Timeouts** — every step runs under a timeout; on expiry the robot is
   told to stop.
5. **Emergency stop** — `POST /stop` cancels the running task, cancels the
   in-flight robot request and fires the robot's stop endpoint; never routed
   through the LLM.

## Testing

```bash
uv run pytest
```

The suite covers profile validation and step checking, schema rejection of
invalid envelopes, planner retries and response-format fallbacks, executor
behaviour (timeouts, dead telemetry, e-stop mid-plan, busy executor), the
HTTP bridge adapter against a stubbed robot, and the REST endpoints — all
without hardware.
