"""robotctl: LLM-driven robot control as a headless REST API.

Layout: `core` (settings, universal envelope, robot profile, safety layer),
`runtime` (HTTP bridge adapter, state monitor, LLM planner, executor) and
`service` (REST API + service entry point).
"""

__version__ = "0.1.0"

from robotctl.core import (  # noqa: E402
    SCHEMA_VERSION,
    ActionStep,
    MotionStatus,
    PlanResponse,
    Pose,
    ProfileError,
    ProfileManager,
    RobotProfile,
    SafetyViolation,
    Settings,
    Telemetry,
    describe_step,
    get_settings,
    parse_do_action,
    parse_profile,
    requires_confirmation,
    validate_plan,
)
from robotctl.runtime import (  # noqa: E402
    AdapterError,
    Command,
    CommandStatus,
    Executor,
    ExecutorBusy,
    HttpRobotAdapter,
    LLMInteraction,
    LLMPlanner,
    PlanParseError,
    PlannerError,
    RobotAdapter,
    StateMonitor,
    StepResult,
    parse_plan_response,
    split_thinking,
)
from robotctl.service import create_app, main  # noqa: E402

__all__ = [
    "__version__",
    # core
    "SCHEMA_VERSION",
    "ActionStep",
    "MotionStatus",
    "PlanResponse",
    "Pose",
    "ProfileError",
    "ProfileManager",
    "RobotProfile",
    "SafetyViolation",
    "Settings",
    "Telemetry",
    "describe_step",
    "get_settings",
    "parse_do_action",
    "parse_profile",
    "requires_confirmation",
    "validate_plan",
    # runtime
    "AdapterError",
    "Command",
    "CommandStatus",
    "Executor",
    "ExecutorBusy",
    "HttpRobotAdapter",
    "LLMInteraction",
    "LLMPlanner",
    "PlanParseError",
    "PlannerError",
    "RobotAdapter",
    "StateMonitor",
    "StepResult",
    "parse_plan_response",
    "split_thinking",
    # service
    "create_app",
    "main",
]
