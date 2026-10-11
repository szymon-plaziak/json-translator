# AGENTS.md — robotctl project guide

Quick-start context for coding agents. Original brief: `INSTRUCTIONS.md`. Do not rebuild from scratch; this file + focused reads are enough.

## What this is
Universal robot controller, headless REST API. `POST /commands` takes natural language (or manual `/do …`) → LLM proposes a plan **in the robot's own command language** → safety layer validates against the uploaded robot profile → executor forwards each step to the real robot's HTTP endpoints. The core is robot-agnostic: ALL robot knowledge (actions, params, limits, example answers) comes from a profile JSON uploaded via `POST /profile` — nothing hardcoded. No simulation (mock adapter deleted), no interactive CLI, no web view (all removed by user request). The robot exposes `GET /telemetry`, `POST /command`, `POST /stop` (contract in `API_README.md`); its companion app is the user's to build. Loopback-only, docs disabled.

## Commands (project root, WSL2; always `uv`, never pip)
- `uv sync` — install deps
- `uv run pytest -q` — full suite (~40 s, 137 tests)
- `uv run robotctl` — API service on 127.0.0.1:8081 (foreground; `--host`/`--port`; Ctrl+C/SIGTERM safe stop). Boots fine with no robot (`ROBOT_BASE_URL` empty): `/health` ok:false, execution fails safely.

## Architecture (src/robotctl/)
- `profile.py` — THE robot-specific contract: `RobotProfile{profile_version, robot{name,description}, actions[{action, description, params[{name,type(int|float|str|bool),unit,min,max,required,default}]}], examples[]}`. `examples` must be exactly-correct universal-envelope answers whose steps validate against `actions` (rejected at upload otherwise) — injected verbatim into the LLM prompt. `check_step()` validates/normalizes a step (unknown action/param, type, min/max; fills defaults). `response_schema()` builds the strict structured-output schema dynamically per profile. `ProfileManager` holds current profile, persists to `PROFILE_PATH`, reloads at startup (corrupt file → warn, continue with none). `parse_do_action(args, profile)` = positional `/do` parsing.
- `config.py` — pydantic-settings, reads `.env`. Universal knobs only: `max_plan_steps`, `step_timeout_s`, `telemetry_max_age_s`, `risky_actions` (NoDecode + CSV; empty must not crash). Robot bridge: `robot_base_url` (empty = no robot, fails safe), `robot_command_path`/`robot_telemetry_path`/`robot_stop_path`, `robot_timeout_s`, `robot_poll_hz`. `profile_path`. Old mock settings and fixed motion limits (MAX_DISTANCE_M etc.) are GONE — per-action limits live in the profile now.
- `schemas.py` — universal envelope only: `PlanResponse{version=1, mode: action|plan|clarification, steps[], question, options}`, `extra="forbid"`. Steps are `ActionStep{action: str}` with `extra="allow"` (params come from the profile; validated by safety, NOT by the envelope). `Telemetry`: only timestamp/monotonic/connected/motion_status required; pose/battery/obstacle/speeds optional (real robots vary). `describe_step()` generic.
- `planner.py` — `AsyncOpenAI` → configurable endpoint. System prompt built per call from the current profile (robot name/description, action vocabulary with limits, examples verbatim, split-don't-ask rule). json_schema response_format generated from the profile; fallback chain json_schema → json_object → none (2 attempts each); auto-drops `LLM_EXTRA_BODY` if rejected. Strips think-blocks + captures `reasoning_content`. `parse_plan_response(content, profile)` also validates steps against the profile so the retry loop auto-corrects unknown/invalid actions. Carries unresolved clarifications into the next command (`_pending_clarification`, bounded history, restored on failed attempts, cleared on resolution). `LLMInteraction` deque + JSONL at `LLM_LOG_FILE`; `entries()` feeds `GET /logs`. Planner raises `PlannerError("no robot profile loaded…")` without a profile.
- `safety.py` — deterministic gate: envelope rules (action⇒1 step, plan length), every step through `profile.check_step()`. `requires_confirmation()` matches step action names against `RISKY_ACTIONS`.
- `executor.py` — one command at a time (`ExecutorBusy`). `submit()` (via LLM) and `submit_plan()` (skip LLM, used by `/do`). Constructor takes `(settings, adapter, monitor, planner, profiles)`. Steps run under `wait_for` timeout with fresh-telemetry checks between steps; failure → safe stop + terminal status. E-stop swallows cancellation → status `stopped`, adapter halted directly. Without a profile: command `rejected`.
- `monitor.py` — telemetry snapshot + freshness (`telemetry_max_age_s`) + history deque (unchanged).
- `adapters/base.py` — `RobotAdapter` ABC (connect/disconnect/execute/stop/telemetry); `execute` must halt motion on CancelledError.
- `adapters/http.py` — the ONLY adapter: generic HTTP bridge. `connect()` starts a poll task (`robot_poll_hz`, default 5 Hz) GETting the telemetry endpoint and mapping it to universal `Telemetry` (unknown `motion_status` → "error"; missing fields → None; tolerates 3 consecutive poll failures, then flips connected=False and marks the cached snapshot `connected: false, motion_status: "error"`). `execute()` POSTs the validated step JSON to the command endpoint; robot responds after finishing — 2xx ok, 4xx/5xx or body `ok:false`/`status: failed|error` → `AdapterError`. Cancellation → best-effort `POST /stop` before re-raising. `stop()` is best-effort, never raises. Accepts an optional `transport` (httpx.AsyncBaseTransport) for tests. No `ROBOT_BASE_URL` → stays disconnected, execution fails safely.
- `api.py` — the app's only interface. `POST /profile` (validate via `parse_profile`, 422 + reason on error; persists + hot-applies), `GET /profile` (404 when none), `POST /commands` (202; 409 busy; 422 without profile or on bad `/do`; body `{command, confirm}` — risky plans rejected unless `"confirm": true`), `GET /commands/{id}`, `GET /state` (503 without telemetry), `GET /logs?limit=N` (duck-typed planner `entries()`), `POST /stop`, `GET /health{ok, adapter:"http", robot_configured, profile_loaded}`.
- `cli.py` — despite the name, the headless service entry point (`robotctl` script → `cli:main`): builds adapter+monitor+profiles+planner+executor, runs uvicorn on the SAME asyncio loop (`await server.serve()`; uvicorn handles SIGINT/SIGTERM), safe teardown in `finally`. Does NOT abort when the robot is unreachable — warns and serves. No UI code, no rich/prompt_toolkit.

## Tests (tests/)
- conftest `settings` fixture: `_env_file=None`, `profile_path=""`, prompt-only LLM format, no log file, step_timeout 5 s / telemetry age 2 s — generous margins because drvfs stalls; tests that exercise timeouts/staleness override these.
- `helpers.py`: `make_profile()` (move/turn/wait profile with an example), `make_profiles()`, `step()`, `action_plan`/`multi_plan` (dicts coerce to ActionStep), `FakePlanner`, `FakeAdapter` (records executed steps; `fail_on`/`hang_on`/`execute_delay_s`/`drop_link()` controls; `executed`/`halted`/`stops` assertions), `stack()` (adapter+profiles+monitor+executor with teardown), `wait_for_status()`.
- `test_profile.py` profile validation/examples/check_step/schema/manager/`/do`; `test_http_adapter.py` the bridge against `httpx.MockTransport` (injected via `HttpRobotAdapter(settings, transport=…)`); `test_planner.py` uses `FakeClient`/`FakeCompletions` (str content, `(content, reasoning_content)` tuple, or Exception) + `make_profiles()`; `test_api.py` in-process via httpx `ASGITransport`, `app_client()` helper builds `create_app(executor, monitor, settings, planner, profiles)`.
- Verified against the real endpoint (psnc HPC, Qwen3.8-27B): uploaded a crane profile (lift/lower/slew), NL command "lift the hook half a meter then turn 90 degrees" → plan `[{"action":"lift","meters":0.5},{"action":"slew","degrees":90}]` in 1 attempt. Execution correctly failed safe (no robot).

## Environment gotchas (WSL2, project on /mnt/c → NTFS/drvfs)
- Stale `__pycache__` after edits causes bizarre behavior: `find . -name __pycache__ -type d -prune -exec rm -rf {} +`
- Port 8080 is occupied by another service here → API default is 8081.
- In the agent sandbox, cross-process networking fails (curl → background server refused); verify HTTP in-process (ASGITransport / urllib from a thread — threads share the loopback namespace, cross-thread probes work).
- Literal think tags written through tool calls can get stripped — build them by string concat (`"<" + "think>"`). Same trick for f-strings: Python 3.12 chokes on `f'{{"x": "{expr}"'` inside list displays ("unmatched ']'" SyntaxError) — build such strings by concatenation instead.
- `httpx.MockTransport` handlers are awaited but do NOT enforce client timeouts — don't write timeout tests against it (test ConnectError paths instead).
- Load spikes can stall the event loop for seconds: keep generous wall-clock margins in tests.
- `os._exit(0)` in probe scripts skips stdout flushing — run `python -u` or print with flush.

## State / progress (verified)
- Milestones 1–5: schemas, mock adapter, planner, executor+safety, API+CLI (historical; mock/CLI since removed).
- Milestone 6: headless conversion (CLI dashboard + web view removed, `GET /logs` + `confirm` added).
- Milestone 7 (universal controller, complete): simulation deleted; `profile.py` with upload/persist/hot-apply; profile-driven prompt + dynamic strict schema + profile-driven safety; generic HTTP bridge adapter (`adapters/http.py`) replacing the mock; universal envelope with profile-defined steps; `Telemetry` optional fields; `.env.example` rewritten; `API_README.md` documents profile format + robot contract; 137 tests green; real-endpoint LLM verified with an uploaded crane profile (see Tests section above).
- User's real credentials: belong in `.env` only (gitignored). `config.py` defaults currently contain them (user's edit; flagged, left as-is).
- User's `.env` may still contain stale MOCK_*/ROBOT_ADAPTER keys — harmless (config has `extra="ignore"`).

## Next steps (not started)
- End-to-end test once the user's robot-side HTTP app exists (bridge contract in `API_README.md`).
- Re-plan-on-obstacle / re-plan-on-step-failure using fresh telemetry (retry the remaining plan with an error-context prompt).
- Optional: profile versioning/migration, `DELETE /profile`, command history export.

## Hard rules
- The core must stay robot-agnostic: never hardcode actions; all robot knowledge flows from the profile.
- LLM output is never trusted: planner parses (+ profile re-check) → safety validates → executor controls; e-stop bypasses the LLM.
- No motion without a reachable robot: `ROBOT_BASE_URL` empty or telemetry dead → execution fails safe.
- No `git commit`/`push` — the user handles version control.
- Keep prompts small, responses JSON-only; `LLM_TEMPERATURE=0`.
