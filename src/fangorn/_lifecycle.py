from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

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
    action: Literal["create", "start", "inspect"]
    resource_name: str


@dataclass(frozen=True)
class CreatePlan:
    steps: tuple[PlanStep, ...]
    success_state: Literal["ready", "stopped"]


def plan_create(resources: tuple[Resource, ...], *, start: bool) -> CreatePlan:
    create = tuple(PlanStep("create", resource.name) for resource in resources)
    start_steps = (
        tuple(PlanStep("start", resource.name) for resource in resources)
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
