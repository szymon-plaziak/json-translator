# Robot Control via LLM — Project Instructions

## Goal
Build a Python robot-control system that accepts natural-language commands in the terminal, uses an LLM to convert them into validated JSON actions, executes them through a robot adapter, and reports live robot state. **Do not build a web frontend or UI.** The REST API is a local backend interface, not a user-facing web app.

## Environment
- Target **Linux running inside WSL2 on Windows 11**.
- Manage Python and project dependencies with **`uv`** (`pyproject.toml`, `uv sync`, `uv run`). Do not use `pip` directly.
- Keep configuration and secrets outside source control. Read the OpenAI-compatible API base URL, API token, model ID, and robot settings from environment variables or a local config file; include `.env.example`, never real credentials.
- Allow the user to configure any **OpenAI-compatible** model endpoint using its URL, API token, and model ID. Do not hard-code a provider.

## Architecture
Keep the implementation small and separated into these components:
1. **Terminal CLI** — accepts commands, shows progress/state, and supports an immediate stop command.
2. **Local REST API** — use FastAPI; bind to `127.0.0.1` by default. Suggested endpoints: `POST /commands`, `GET /state`, `GET /commands/{id}`, `POST /stop`, and `GET /health`.
3. **LLM planner** — receives the user's command, the robot's supported-action schema, safety limits, and relevant current telemetry. It returns only structured JSON.
4. **Validator and executor** — validate every response against typed models (for example, Pydantic), then execute only supported actions.
5. **Robot adapter** — isolate hardware-specific communication behind an interface. Start with a simulator/mock adapter; add a real-robot adapter only when its protocol is known.
6. **State monitor** — consume real telemetry from the adapter and expose timestamped position, orientation, motion/status, sensor readings, and connection state. The terminal should display updates continuously.

## LLM response contract
- Require a **short, low-latency response**: no explanations, no chain-of-thought, no markdown, and no unnecessary prose. Return only valid JSON. Use JSON Schema / structured-output mode when the chosen API supports it; otherwise validate the response locally.
- Keep prompts and outputs small. Use a fast model and a low output-token limit appropriate to the schema. Do not add unnecessary model parameters or provider-specific settings.
- For a simple command (e.g. “turn slightly left”), return one bounded action. For a compound command (e.g. “move forward and go around the obstacle”), return a short ordered list of supported steps.
- Use an explicit, versioned schema with a finite set of action types and typed parameters. Example shape:

  ```json
  {
    "version": 1,
    "mode": "action",
    "steps": [
      { "action": "turn", "angle_deg": -10 }
    ]
  }
  ```

  The exact action names and parameters must match the selected robot adapter. Support a separate `plan` mode and a structured `clarification`/error result when a request is ambiguous or cannot safely be completed. Never accept arbitrary code, shell commands, raw motor commands, or unknown JSON fields from the LLM.
- Provide the model only current, timestamped sensor/state data. Never ask it to invent a position, infer unseen obstacles, or claim an action succeeded without telemetry.

## Execution and safety
- The LLM proposes actions; **deterministic application code remains in control**. Validate types, ranges, supported actions, plan length, and robot-specific speed/distance/angle/duration limits before execution.
- Execute plans one step at a time and check fresh telemetry between steps. Use closed-loop feedback where possible; stop and re-plan if an obstacle, unexpected movement, stale state, disconnect, timeout, or failed step is detected. Obstacle avoidance must rely on robot sensors and/or a local navigation controller, not on the LLM alone.
- Implement an emergency stop that bypasses the LLM and immediately invokes the adapter's stop mechanism. Add timeouts and a safe stop on critical errors or loss of communication. Require confirmation for configured risky actions.
- Do not send motion commands to a real robot by default. Simulation/mock mode is the default; real hardware requires an explicit configuration flag.
- Bind the API to localhost by default and do not expose it to the network without deliberate authentication and security changes.

## Implementation priorities
1. Define action/state schemas and the mock robot adapter.
2. Implement the configurable OpenAI-compatible LLM client and strict JSON validation.
3. Implement the executor, stop path, bounded step-by-step plans, and telemetry monitoring.
4. Add the local REST API and terminal CLI; no browser UI.
5. Test invalid JSON, unsupported actions, limits, timeouts, stale telemetry, emergency stop, and multi-step plan interruption using mocks before connecting hardware.

Prefer clear modules, type hints, concise documentation, and focused tests over unnecessary abstractions. Keep hardware-specific code isolated so the simulator and real robot use the same validated command interface.
