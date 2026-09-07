from __future__ import annotations

import pytest

from fangorn._lifecycle import (
    Observation,
    PlanStep,
    Resource,
    finish_create,
    plan_create,
    plan_lifecycle,
)


def test_default_create_plans_declared_resources_to_ready() -> None:
    resources = (
        Resource("worktree", "worktree"),
        Resource("api", "service"),
        Resource("terminal", "terminal"),
    )

    plan = plan_create(resources, start=True)

    assert plan.steps == (
        PlanStep("create", "worktree"),
        PlanStep("create", "api"),
        PlanStep("create", "terminal"),
        PlanStep("start", "worktree", enter_state="starting"),
        PlanStep("start", "api"),
        PlanStep("start", "terminal"),
        PlanStep("inspect", "worktree"),
        PlanStep("inspect", "api"),
        PlanStep("inspect", "terminal"),
    )
    assert plan.success_state == "ready"
    assert (
        finish_create(
            resources,
            {
                "worktree": Observation("ready"),
                "api": Observation("ready"),
                "terminal": Observation("ready"),
            },
            start=True,
        )
        == "ready"
    )


def test_no_start_accepts_ready_worktree_but_not_ready_optional_resources() -> None:
    resources = (
        Resource("worktree", "worktree"),
        Resource("api", "service"),
        Resource("terminal", "terminal"),
    )

    plan = plan_create(resources, start=False)

    assert plan.steps == (
        PlanStep("create", "worktree"),
        PlanStep("create", "api"),
        PlanStep("create", "terminal"),
        PlanStep("inspect", "worktree"),
        PlanStep("inspect", "api"),
        PlanStep("inspect", "terminal"),
    )
    assert plan.success_state == "stopped"
    assert (
        finish_create(
            resources,
            {
                "worktree": Observation("ready"),
                "api": Observation("stopped"),
                "terminal": Observation("absent"),
            },
            start=False,
        )
        == "stopped"
    )
    assert (
        finish_create(
            resources,
            {
                "worktree": Observation("ready"),
                "api": Observation("ready"),
                "terminal": Observation("absent"),
            },
            start=False,
        )
        == "create_failed"
    )


def test_create_never_guesses_success_from_unknown_observation() -> None:
    resources = (Resource("worktree", "worktree"),)

    assert (
        finish_create(
            resources,
            {"worktree": Observation("unknown")},
            start=True,
        )
        == "create_failed"
    )


@pytest.mark.parametrize(
    ("command", "state", "actions", "success"),
    [
        ("start", "stopped", ("start",), "ready"),
        ("start", "start_failed", ("start",), "ready"),
        ("stop", "start_failed", ("stop",), "stopped"),
        ("restart", "stop_failed", ("stop", "start"), "ready"),
        ("delete", "create_failed", ("delete",), "deleted"),
        ("delete", "deleted", (), "deleted"),
    ],
)
def test_headless_command_plans(
    command: str,
    state: str,
    actions: tuple[str, ...],
    success: str,
) -> None:
    plan = plan_lifecycle(command, state)
    assert plan.actions == actions
    assert plan.success_state == success


@pytest.mark.parametrize("command", ["start", "stop", "restart"])
@pytest.mark.parametrize(
    "state", ["creating", "create_failed", "deleting", "deleted", "delete_failed"]
)
def test_headless_commands_refuse_incompatible_states(command: str, state: str) -> None:
    with pytest.raises(ValueError, match="inspect before retry"):
        plan_lifecycle(command, state)
