# AGENTS.md — robotctl project guide

Quick-start context for coding agents. Do not rebuild from scratch; this file + focused reads are enough.

## What this is
Universal robot controller, headless REST API. `POST /commands` takes natural language → LLM proposes a plan **in the robot's own command language** → deterministic validation against the uploaded robot profile → executor forwards each step to the real robot's HTTP endpoints. The core is robot-agnostic: ALL robot knowledge (actions, params, limits, example answers) comes from a profile JSON uploaded via `POST /profile` — nothing hardcoded. **No clarification mode**: the envelope is `{version, mode: action|plan, steps}` and the prompt tells the model to decide on the most reasonable interpretation with conservative values instead of asking. Removed by user request (minimization, Milestone 9): simulation, CLI, web view, `/do` manual commands, `confirm`/`RISKY_ACTIONS` flow, `GET /logs` + LLM JSONL logging, `StateMonitor` (telemetry is fetched **on demand**, no background polling), the `openai` SDK (planner POSTs `/chat/completions` over httpx directly), `pydantic-settings` (hand-rolled `.env`/env reading in `core.Settings`). The robot exposes `GET /telemetry`, `POST /command`, `POST /stop` (contract in `API_README.md`); its companion app is the user's to build. Loopback-only, docs disabled.

## Commands (project root, WSL2; always `uv`, never pip)
- `uv sync` — install deps
- `uv run pytest -q` — full suite (~7 s, 109 tests)
- `uv run robotctl` — API service on 127.0.0.1:8081 (foreground; Ctrl+C/SIGTERM safe stop, host/port from `API_HOST`/`API_PORT`). Boots fine with no robot (`ROBOT_BASE_URL` empty): `/health` ok:false, execution fails safely.

## Architecture (src/robotctl/ — 3 moduły + __init__ z samą wersją)
- `core.py` — contracts + config, robot-agnostic. **schemas** (uniwersalna koperta `PlanResponse{version=1, mode: action|plan, steps}` z `extra="forbid"`; kroki to `ActionStep{action}` z `extra="allow"` — params definiuje profil; `Telemetry` — wymagane tylko timestamp/monotonic/connected/motion_status; `describe_step()` generic; stała `MAX_PLAN_STEPS=8`), **config** (zwykła klasa `Settings` czytająca os.environ, kwargs-override dla testów; `.env` parsowany ręcznie w `_load_env_file`, wołany tylko przez `get_settings()`; nieznane zmienne ignorowane), **profile** (THE robot-specific contract: `RobotProfile{profile_version, robot, actions[{action, description, params[{name,type,unit,min,max,required,default}]}], examples[]}`; `examples` muszą być dokładnie poprawnymi odpowiedziami w kopercie, walidowanymi przeciw `actions` — wstrzykiwane verbatim do promptu LLM; `check_step()` waliduje/normalizuje krok (nieznane action/param, typ, min/max; wypełnia defaulty); `response_schema()` buduje strict structured-output schemę per profil; `ProfileManager` trzyma bieżący profil, persistuje do `PROFILE_PATH`, przeładowuje przy starcie) oraz **safety** (tylko `SafetyViolation` — walidacja planu żyje w `Executor._validated_steps`).
- `runtime.py` — execution engine. **HTTP bridge** (`HttpRobotAdapter`, JEDYNY adapter, bez ABC: `fetch_telemetry()` = GET na żądanie z krótkim timeoutem 3 s, None przy każdym błędzie; `execute()` POSTuje zwalidowany step JSON, robot odpowiada po zakończeniu — 2xx ok, 4xx/5xx lub body `ok:false`/`status: failed|error` → `AdapterError`; CancelledError → best-effort `POST /stop` przed re-raise; `stop()` best-effort, nigdy nie rzuca; brak `ROBOT_BASE_URL` → fails safe), **planner** (`LLMPlanner`: httpx → `{llm_base_url}/chat/completions` z nagłówkiem Bearer; system prompt per profil (nazwa/opis, słownik akcji z limitami, examples verbatim, zasada guess-don't-ask); łańcuch fallbacków json_schema → json_object → none, **jedna próba na wariant**, feedback o korekcie przenoszony do kolejnych wariantów; `extra_body` scalany w payload, auto-odrzucany gdy serwer go odrzuca; wycina think-blocki (`split_thinking`); `parse_plan_response` waliduje kroki przeciw profilowi; bez profilu rzuca `PlannerError`), **executor** (`Executor`: jedna komenda naraz (`ExecutorBusy`); `submit()` jedyna ścieżka; statusy jako stringi: planning/executing/completed/failed/rejected/stopped; przed KAŻDYM krokiem `fetch_telemetry()` — None lub `connected:false` → abort + safe stop; kroki pod `wait_for` timeout; failure → safe stop + status terminalny; e-stop połyka cancellation → status `stopped`; walidacja planu inline (`_validated_steps`: action⇒1 krok, limit `MAX_PLAN_STEPS`, każdy krok przez `profile.check_step`)).
- `service.py` — REST API + entry point. `create_app(executor, adapter, settings, profiles)`: `POST /profile` (walidacja przez `parse_profile`, 422 + powód; persist + hot-apply), `GET /profile` (404 gdy brak), `POST /commands` (202; 409 busy; 422 bez profilu; body `{command}` — bez confirm), `GET /commands/{id}`, `GET /state` (503 gdy fetch się nie uda), `POST /stop`, `GET /health{ok, adapter:"http", robot_configured, profile_loaded}` (live probe telemetrii). **Entry point**: `main()` (`robotctl` script → `service:main`): buduje adapter+profiles+planner+executor, uvicorn na TYM SAMYM pętli asyncio (`await server.serve()`), teardown w `finally` (executor.stop → planner.aclose → adapter.disconnect). NIE przerywa gdy robot nieosiągalny. Żadnego argparse (host/port z env), żadnego UI.

## Tests (tests/)
- conftest `settings` fixture: `Settings(...)` z process-env tylko (bez .env), `profile_path=""`, prompt-only LLM format, `step_timeout_s=5` — generous margins because drvfs stalls; timeout tests override.
- `helpers.py`: `make_profile()` (move/turn/wait + example), `make_profiles()`, `step()`, `action_plan`/`multi_plan`, `FakePlanner` (canned plan / PlannerError), `FakeAdapter` (async `fetch_telemetry()`; `fail_on`/`hang_on`/`execute_delay_s`; `drop_link()` → fetch zwraca None; `executed`/`stops`/`halted`), `stack()` → `(executor, adapter)` z teardown, `wait_for_status(command, "completed", ...)` — statusy jako stringi.
- `test_planner.py`: `FakeLLM` (httpx-podobny double: `post(url, json=...)` → `SimpleNamespace(status_code=200, json=...)`; skrypt: str / (content, reasoning) / Exception) + `TelemetryStub` (async `fetch_telemetry`). Retry-testy używają `Settings(LLM_RESPONSE_FORMAT="json_schema")` (3 warianty); fixture conftest ma "none" (1 wariant).
- `test_http_adapter.py` bridge against `httpx.MockTransport` (wstrzykiwany przez `HttpRobotAdapter(settings, transport=…)`); `test_api.py` in-process via httpx `ASGITransport`, `app_client()` buduje `create_app(executor, adapter, settings, profiles)`.

## Environment gotchas (WSL2, project on /mnt/c → NTFS/drvfs)
- Stale `__pycache__` after edits causes bizarre behavior: `find . -name __pycache__ -type d -prune -exec rm -rf {} +`
- Port 8080 is occupied by another service here → API default is 8081.
- In the agent sandbox, cross-process networking fails (curl → background server refused); verify HTTP in-process (ASGITransport / urllib from a thread — threads share the loopback namespace, cross-thread probes work).
- Literal think tags written through tool calls can get stripped — build them by string concat (`"<" + "think>"`). Same trick for f-strings: Python 3.12 chokes on `f'{{"x": "{expr}"'` inside list displays ("unmatched ']'" SyntaxError) — build such strings by concatenation instead.
- `httpx.MockTransport` handlers are awaited but do NOT enforce client timeouts — don't write timeout tests against it (test ConnectError paths instead).
- Load spikes can stall the event loop for seconds: keep generous wall-clock margins in tests.
- `os._exit(0)` in probe scripts skips stdout flushing — run `python -u` or print with flush.

## State / progress (verified)
- Milestones 1–7 (historical): schemas, planner, executor+safety, API, headless conversion, universal controller with profile-driven prompt/safety + generic HTTP bridge.
- Milestone 8 (historical): structural consolidation 13 → 4 files.
- Milestone 9 (minimization, complete): clarification mode usunięty z koperty/parsera/executora — model zawsze zgaduje ("decide, don't ask", conservative values); `/do`, `confirm`/`RISKY_ACTIONS`, `GET /logs` + JSONL, `StateMonitor`, ABC adaptera, `openai` SDK, `pydantic-settings`, argparse — wszystkie usunięte; telemetria on-demand (fetch przed planowaniem/krokiem + /state + /health); plan-length hardcode `MAX_PLAN_STEPS=8`; Settings ręcznie z env; koperta `{"version": 1, "mode": "action"|"plan", "steps": [...]}` (pole `mode` ZOSTAŁO — istniejący profil w `data/robot_profile.json` pozostaje ważny); prompt systemowy zachowany niemal 1:1 (3 zmiany: enum trybów, bez question/options, bullet clarification → bullet guessing); 109 testów zielonych; boot smoke probe (serve + HTTP z wątku) przeszedł.
- User's real credentials: belong in `.env` only (gitignored). `LLM_EXTRA_BODY` (disable Qwen thinking) nadal wspierany i używany.
- Repo sizes after M9: src 1432 linii (core 553, runtime 698, service 173, __init__ 8), tests 1686, zależności: fastapi/uvicorn/pydantic/httpx.

## Next steps (not started)
- End-to-end test once the user's robot-side HTTP app exists (bridge contract in `API_README.md`).
- Re-plan-on-step-failure using fresh telemetry (retry the remaining plan with an error-context prompt).
- Optional: profile versioning/migration, `DELETE /profile`, command history export.

## Hard rules
- The core must stay robot-agnostic: never hardcode actions; all robot knowledge flows from the profile.
- LLM output is never trusted: planner parses (+ profile re-check) → executor validates → adapter executes; e-stop bypasses the LLM.
- No motion without a reachable robot: `ROBOT_BASE_URL` empty or telemetry fetch failing → execution fails safe.
- No clarification mode: the model decides on the most reasonable interpretation (conservative values), the profile's hard limits are the safety net.
- No `git commit`/`push` — the user handles version control.
- Keep prompts small, responses JSON-only; `LLM_TEMPERATURE=0`.
