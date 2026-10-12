# API_README — robotctl REST API

robotctl is a **universal robot controller** served as a headless REST API:
your application sends natural-language commands, an LLM proposes a plan in
the robot's own command language, a deterministic safety layer validates it,
and each step is forwarded to the **real robot over HTTP**.

The core app is robot-agnostic. Everything it knows about a particular robot
— which actions exist, which parameters they take, what a correct answer
looks like — comes from a **robot profile** you upload. Point it at a crane
and it plans `lift`/`slew`; point it at a rover and it plans `move`/`turn`.

- **Base URL:** `http://127.0.0.1:8081` (loopback only by default; do not
  expose without adding authentication)
- **Interactive docs:** disabled by design

```
Your app ──POST /commands──► robotctl ──► LLM (profile-driven) ──► safety ──► robot HTTP endpoints
```

## Running the service

```bash
uv sync                # install dependencies
cp .env.example .env   # then set LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
uv run robotctl        # serves the API until Ctrl+C / SIGTERM
```

Host/port come from `API_HOST`/`API_PORT` in `.env`.

Startup order: set `LLM_*` in `.env`, upload a robot profile, optionally set
`ROBOT_BASE_URL` when the robot is online. Without a robot the service still
runs — `/health` reports `ok:false` and commands fail safely; planning,
profile management and validation all work.

## Quick start

```bash
# 1. upload a robot profile (see "Robot profile" below for the full format)
curl -s -X POST http://127.0.0.1:8081/profile \
     -H 'Content-Type: application/json' \
     -d @crane_profile.json

# 2. submit a natural-language command
curl -s -X POST http://127.0.0.1:8081/commands \
     -H 'Content-Type: application/json' \
     -d '{"command": "lift the hook half a meter then slew 90 degrees"}'
# → {"id":"85465aaf27ff","status":"planning"}

# 3. poll until terminal
curl -s http://127.0.0.1:8081/commands/85465aaf27ff
```

## Robot profile

The profile is the only source of robot knowledge. It defines the action
vocabulary, the parameters (types and hard limits enforced by the safety
layer), and one or more **example answers** that are shown to the LLM
verbatim — this is how the LLM "learns" your robot.

```json
{
  "profile_version": 1,
  "robot": {
    "name": "Crane-9",
    "description": "workshop gantry crane with a hook"
  },
  "actions": [
    {
      "action": "lift",
      "description": "raise the hook",
      "params": [
        { "name": "meters", "type": "float", "unit": "m", "min": 0, "max": 5, "required": true },
        { "name": "speed_mps", "type": "float", "unit": "m/s", "min": 0, "max": 1, "default": 0.2 }
      ]
    },
    {
      "action": "slew",
      "description": "rotate the crane",
      "params": [
        { "name": "degrees", "type": "float", "unit": "deg", "min": -180, "max": 180, "required": true }
      ]
    }
  ],
  "examples": [
    {
      "version": 1,
      "mode": "plan",
      "steps": [
        { "action": "slew", "degrees": 90 },
        { "action": "lower", "meters": 2.0 }
      ]
    }
  ]
}
```

Rules:

- `actions` (1–32): each has a lowercase `action` name and optional `params`.
  A param has `name`, `type` (`int` | `float` | `str` | `bool`), optional
  `description`, `unit`, `min`, `max`, `required`, `default`.
  Optional params with a `default` are filled in automatically when the LLM
  omits them, so the robot always receives complete commands.
- `examples` (1–5): **exactly-correct answers** in the universal envelope
  (`{"version": 1, "mode": ..., "steps": [...]}`) whose steps must validate
  against `actions`. They are injected into the LLM prompt as the format to
  mimic — make them exemplary. Invalid examples are rejected at upload.
- Per-action limits are yours to declare: the safety layer rejects any step
  outside `min`/`max`, with the wrong type, with unknown parameters or a
  missing required parameter.

**Universal envelope (fixed by the core):** `{"version": 1, "mode": "action"
| "plan", "steps": [...]}` — `mode: "action"` = exactly one step; `"plan"` =
2–8 ordered steps. There is no clarification mode: the LLM is told to
**decide, not ask**. A vague command gets the most reasonable interpretation
with conservative values (smaller distances, slower speeds); a request
exceeding a single action's limits becomes a multi-step plan.

Upload endpoints:

| Endpoint | Purpose |
| --- | --- |
| `POST /profile` | Upload/replace the profile (validated immediately; invalid → 422 with the reason). Hot-applies and persists to `PROFILE_PATH` |
| `GET /profile` | The current profile (`404` when none loaded) |

A profile at `PROFILE_PATH` is reloaded at startup; with `PROFILE_PATH` empty
profiles live in memory only.

## Endpoints

### `POST /commands` — submit a command

```json
{ "command": "lift the hook half a meter" }
```

| Field | Type | Notes |
| --- | --- | --- |
| `command` | string (1–500 chars) | Natural language; the LLM translates it into the robot's command language |

Responses:

- `202` → `{ "id": "<id>", "status": "planning" }`
- `409` → another command is still running
- `422` → invalid body or **no profile loaded**
  (upload one via `POST /profile` first)

Execution is asynchronous: poll `GET /commands/{id}` until terminal.

### `GET /commands/{id}` — status, plan and per-step results

Status lifecycle:

```
planning → executing
executing → completed | failed | stopped | rejected
rejected is terminal
```

- `rejected`: the safety layer refused the plan — unknown action for this
  robot, out-of-range parameter or a plan longer than 8 steps.
- `failed`: a step failed against the robot (HTTP error, failure ack,
  timeout) or telemetry became unavailable; the robot was told to stop.
- `stopped`: emergency-stopped via `POST /stop`.

### `GET /state` — live robot telemetry

Fetches the robot's telemetry endpoint on demand; `503` when the robot is
unreachable. Fields that the robot does not provide are `null`:

```json
{
  "timestamp": 1762267200.1,
  "monotonic": 12345.6,
  "connected": true,
  "motion_status": "moving",
  "pose": { "x_m": 1.25, "y_m": -0.5, "heading_deg": 45.0 },
  "linear_speed_mps": 0.3,
  "angular_speed_dps": null,
  "battery_pct": 55.5,
  "obstacle_distance_m": 0.8,
  "last_action_id": null
}
```

### `POST /stop` — emergency stop

Cancels the running command immediately and fires a best-effort
`POST {ROBOT_STOP_PATH}` at the robot. Bypasses the LLM. Returns
`{ "stopped": true }`. Safe to call any time.

### `GET /health`

```json
{ "ok": true, "adapter": "http", "robot_configured": true, "profile_loaded": true }
```

`ok` is true only while the robot answers telemetry requests (each `/health`
call performs a live fetch with a short timeout).

## Robot HTTP bridge (what your robot must expose)

Three endpoints on the robot side (paths configurable via `.env`):

| Endpoint | Direction | Contract |
| --- | --- | --- |
| `GET /telemetry` | robot → controller | JSON snapshot. Required: `motion_status` (`idle` \| `moving` \| `turning` \| `stopped` \| `error`; unknown values are mapped to `error`). Optional: `pose: {x_m, y_m, heading_deg}`, `battery_pct`, `obstacle_distance_m`, `linear_speed_mps`, `angular_speed_dps`, `last_action_id`, `timestamp`, `connected`. Fetched on demand (before planning, before every step, and for `/state` + `/health`), not polled in the background. |
| `POST /command` | controller → robot | Body: one validated step JSON, e.g. `{"action": "lift", "meters": 0.5, "speed_mps": 0.2}` — exactly the format the profile teaches the LLM. The robot should respond **after the action finishes**: 2xx = success; 4xx/5xx = failure; a 2xx body with `"ok": false` or `"status": "failed"|"error"` is also treated as a failure (its `error` field is surfaced). |
| `POST /stop` | controller → robot | Emergency halt, best effort (2 s timeout, result ignored). Empty `ROBOT_STOP_PATH` disables it. |

Timeouts: each command request runs under `ROBOT_TIMEOUT_S` and the
executor's `STEP_TIMEOUT_S`; both expiry paths stop the robot.

## Safety model (what your app can rely on)

- The LLM is **never trusted**: its answer must fit the universal envelope
  and every step is validated against the uploaded profile (action exists,
  parameter types, required parameters, min/max). Invalid actions trigger an
  automatic corrective retry before anything reaches the robot.
- Ambiguous commands are **decided, not asked about**: the model picks the
  most reasonable interpretation with conservative values. Hard profile
  limits (`min`/`max`, types, unknown actions) remain the safety net.
- A reachable robot is required between steps: a telemetry fetch that fails
  (or reports `connected: false`) aborts the plan and the robot is told to
  stop.
- `POST /stop` always works and bypasses the LLM.

## Configuration (`.env`)

| Key | Purpose |
| --- | --- |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | OpenAI-compatible endpoint |
| `LLM_RESPONSE_FORMAT` | `json_schema` (schema generated from the profile) → `json_object` → none fallback chain |
| `LLM_EXTRA_BODY` | Extra request-body JSON (e.g. disable Qwen3 thinking) |
| `LLM_TIMEOUT_S`, `LLM_MAX_TOKENS`, `LLM_TEMPERATURE` | LLM request tuning |
| `ROBOT_BASE_URL` | Robot base URL (empty = no robot; fails safe) |
| `ROBOT_COMMAND_PATH` / `ROBOT_TELEMETRY_PATH` / `ROBOT_STOP_PATH` | Bridge endpoints (defaults `/command`, `/telemetry`, `/stop`) |
| `ROBOT_TIMEOUT_S` | Per-request timeout for robot command calls |
| `PROFILE_PATH` | Where uploaded profiles persist (empty = memory only) |
| `STEP_TIMEOUT_S` | Per-step execution timeout (plan length is fixed at 8 steps) |
| `API_HOST`, `API_PORT` | REST API bind address (loopback only by default) |
