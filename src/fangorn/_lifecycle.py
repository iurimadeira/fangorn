from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

ResourceKind = Literal["worktree", "service", "terminal"]
ObservationStatus = Literal["absent", "stopped", "ready", "degraded", "unknown"]
LifecycleState = Literal["ready", "stopped", "create_failed"]


@dataclass(frozen=True)
class Resource:
    name: str
    kind: ResourceKind


@dataclass(frozen=True)
class Observation:
    status: ObservationStatus


@dataclass(frozen=True)
class PlanStep:
    action: str
    resource_name: str
    enter_state: Literal["starting"] | None = None


@dataclass(frozen=True)
class CreatePlan:
    steps: tuple[PlanStep, ...]
    success_state: Literal["ready", "stopped"]


def plan_create(resources: tuple[Resource, ...], *, start: bool) -> CreatePlan:
    create = tuple(PlanStep("create", resource.name) for resource in resources)
    start_steps = (
        tuple(
            PlanStep(
                "start",
                resource.name,
                enter_state="starting" if position == 0 else None,
            )
            for position, resource in enumerate(resources)
        )
        if start
        else ()
    )
    inspect = tuple(PlanStep("inspect", resource.name) for resource in resources)
    return CreatePlan(
        steps=create + start_steps + inspect,
        success_state="ready" if start else "stopped",
    )


def finish_create(
    resources: tuple[Resource, ...],
    observations: Mapping[str, Observation],
    *,
    start: bool,
) -> LifecycleState:
    if start:
        expected = all(
            observations.get(resource.name) == Observation("ready")
            for resource in resources
        )
    else:
        expected = all(
            observations.get(resource.name) == Observation("ready")
            if resource.kind == "worktree"
            else observations.get(resource.name)
            in (Observation("stopped"), Observation("absent"))
            for resource in resources
        )
    if expected:
        return "ready" if start else "stopped"
    return "create_failed"


@dataclass(frozen=True)
class LifecyclePlan:
    actions: tuple[str, ...]
    success_state: str


def resource_steps(
    actions: tuple[str, ...], resources: tuple[Resource, ...]
) -> tuple[PlanStep, ...]:
    steps: list[PlanStep] = []
    for action in actions:
        ordered = (
            tuple(reversed(resources)) if action in {"stop", "delete"} else resources
        )
        if action == "delete":
            steps.extend(PlanStep("ownership", resource.name) for resource in ordered)
            for resource in ordered:
                steps.extend(
                    (
                        PlanStep("delete", resource.name),
                        PlanStep("absence", resource.name),
                    )
                )
        elif action == "forget":
            steps.append(PlanStep("forget", "worktree"))
        else:
            steps.extend(PlanStep(action, resource.name) for resource in ordered)
    return tuple(steps)


def plan_lifecycle(command: str, state: str) -> LifecyclePlan:
    """Plan one whole headless aggregate operation without external effects."""
    if command == "delete":
        return LifecyclePlan(() if state == "deleted" else ("delete",), "deleted")
    allowed = {
        "start": {"stopped", "start_failed", "ready", "starting"},
        "stop": {
            "ready",
            "starting",
            "start_failed",
            "stop_failed",
            "stopped",
            "stopping",
        },
        "restart": {
            "ready",
            "starting",
            "start_failed",
            "stop_failed",
            "stopped",
            "stopping",
        },
    }
    if command not in allowed or state not in allowed[command]:
        raise ValueError(f"Cannot {command} Workspace in {state}; inspect before retry")
    return LifecyclePlan(
        ("stop", "start") if command == "restart" else (command,),
        "stopped" if command == "stop" else "ready",
    )
