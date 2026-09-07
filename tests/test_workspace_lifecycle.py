import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from git_helpers import git
from test_workspace_create import create_repository, facade

from fangorn.git import GitQuiescenceError, observe_worktree
from fangorn.git_worktree import inspect_owned_worktree
from fangorn.registry import ProcessIdentity, Registry, RegistryError
from fangorn.workspaces import CreateWorkspace, WorkspaceError


def test_headless_lifecycle_reconciles_without_changing_git(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic", start=False)
    )
    workspace_id = created.workspace.definition.id
    target = Path(created.workspace.path)
    (target / "README.md").write_text("tracked changes\n")
    (target / "staged").write_text("staged changes\n")
    git(target, "add", "staged")
    (target / "untracked").write_text("untracked changes\n")
    index = observe_worktree(target).git_dir / "index"
    before = (
        git(target, "rev-parse", "HEAD"),
        git(target, "branch", "--show-current"),
        index.read_bytes(),
        (target / "README.md").read_bytes(),
        (target / "staged").read_bytes(),
        (target / "untracked").read_bytes(),
    )
    assert workspaces.inspect_workspace(workspace_id).state == "stopped"
    started = workspaces.start(workspace_id)
    assert started.state == "ready"
    assert workspaces.start(workspace_id).operation.id == started.operation.id
    stopped = workspaces.stop(workspace_id)
    assert stopped.state == "stopped"
    assert workspaces.stop(workspace_id).operation.id == stopped.operation.id
    assert workspaces.restart(workspace_id).state == "ready"
    assert workspaces.inspect(Path(created.workspace.path)).binding.id == workspace_id
    assert before == (
        git(target, "rev-parse", "HEAD"),
        git(target, "branch", "--show-current"),
        index.read_bytes(),
        (target / "README.md").read_bytes(),
        (target / "staged").read_bytes(),
        (target / "untracked").read_bytes(),
    )


def test_first_restart_journals_stop_then_start_even_when_ready(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic")
    )
    restarted = workspaces.restart(created.workspace.definition.id)
    assert restarted.operation.kind == "restart"
    assert [(step["action"], step["status"]) for step in restarted.steps] == [
        ("stop", "completed"),
        ("start", "completed"),
    ]


def test_delete_retains_dirty_failure_then_force_proves_absence(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    target = tmp_path / "topic"
    created = workspaces.create(CreateWorkspace(str(repository), "topic", target))
    workspace_id = created.workspace.definition.id
    (target / "untracked").write_text("keep me")
    with pytest.raises(WorkspaceError, match="dirty"):
        workspaces.delete(workspace_id)
    failed = workspaces.inspect_workspace(workspace_id)
    assert failed.workspace is not None
    assert failed.workspace.state == "delete_failed"
    assert failed.error and "delete" in failed.error
    assert (target / "untracked").read_text() == "keep me"
    deleted = workspaces.delete(workspace_id, force=True)
    assert deleted.workspace is not None
    assert deleted.workspace.state == "deleted"
    assert not target.exists()
    assert workspaces.delete(workspace_id).operation.id == deleted.operation.id


def test_forget_requires_acknowledgement_and_leaves_worktree(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    target = tmp_path / "topic"
    created = workspaces.create(CreateWorkspace(str(repository), "topic", target))
    workspace_id = created.workspace.definition.id
    with pytest.raises(WorkspaceError, match="acknowledge"):
        workspaces.forget(workspace_id)
    receipt = workspaces.forget(workspace_id, acknowledge_orphans=True)
    assert receipt.forgotten
    assert receipt.observed_status == "unknown"
    assert target.is_dir()
    assert not workspaces.list()
    assert workspaces.inspect_workspace(workspace_id).forgotten


def test_forget_never_inspects_git_or_resurrects_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    request = CreateWorkspace(str(repository), "topic", tmp_path / "topic")
    created = workspaces.create(request)
    workspace_id = created.workspace.definition.id
    with monkeypatch.context() as patch:
        patch.setenv("PATH", "")
        assert workspaces.forget(workspace_id, acknowledge_orphans=True).forgotten
        assert workspaces.inspect_workspace(workspace_id).forgotten
    with pytest.raises(WorkspaceError, match="forgotten"):
        workspaces.create(request)
    with pytest.raises(WorkspaceError, match="forgotten"):
        workspaces.inspect(tmp_path / "topic")
    with pytest.raises(WorkspaceError, match="forgotten"):
        workspaces.adopt(tmp_path / "topic")


def test_inspection_reports_drift_without_mutating_state(tmp_path: Path) -> None:
    workspaces = facade(tmp_path)
    with pytest.raises(WorkspaceError):
        workspaces.inspect_workspace("unknown")
    assert not (tmp_path / "state").exists()
    repository = tmp_path / "repository"
    create_repository(repository)
    target = tmp_path / "topic"
    created = workspaces.create(CreateWorkspace(str(repository), "topic", target))
    workspace_id = created.workspace.definition.id
    observed = observe_worktree(target)
    marker = observed.git_dir / "fangorn-worktree-generation"
    marker.write_text("f" * 64)
    paths = [
        path
        for root in (repository / ".git", tmp_path / "state")
        for path in root.rglob("*")
        if path.is_file()
    ]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    inspected = workspaces.inspect_workspace(workspace_id)
    assert inspected.state == "ready"
    assert inspected.observed_status == "degraded"
    assert before == {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths
    }
    with pytest.raises(WorkspaceError, match="ownership"):
        workspaces.delete(workspace_id, force=True)
    assert target.exists()


def test_restart_never_starts_after_failed_stop(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    target = tmp_path / "topic"
    created = workspaces.create(CreateWorkspace(str(repository), "topic", target))
    workspace_id = created.workspace.definition.id
    observed = observe_worktree(target)
    marker = observed.git_dir / "fangorn-worktree-generation"
    token = marker.read_text()
    marker.write_text("f" * 64)
    with pytest.raises(WorkspaceError, match="stop"):
        workspaces.restart(workspace_id)
    failed = workspaces.inspect_workspace(workspace_id)
    assert failed.state == "stop_failed"
    assert [(step["action"], step["status"]) for step in failed.steps] == [
        ("stop", "failed"),
        ("start", "pending"),
    ]
    marker.write_text(token)
    retried = workspaces.restart(workspace_id)
    assert retried.state == "ready"
    assert retried.operation.id == failed.operation.id
    assert any(event["status"] == "failed" for event in retried.history)


def test_lifecycle_accepts_current_branch_and_head(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    target = tmp_path / "topic"
    created = workspaces.create(CreateWorkspace(str(repository), "topic", target))
    (target / "new.txt").write_text("new commit")
    git(target, "add", "new.txt")
    git(target, "commit", "-m", "new")
    git(target, "branch", "-m", "renamed")
    assert workspaces.stop(path=target).state == "stopped"
    assert workspaces.start(path=target).state == "ready"
    assert workspaces.delete(created.workspace.definition.id).state == "deleted"


def test_delete_crash_after_effect_reconciles_without_replay(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    target = tmp_path / "topic"
    created = workspaces.create(CreateWorkspace(str(repository), "topic", target))
    workspace_id = created.workspace.definition.id
    database = tmp_path / "state" / "registry.sqlite3"
    script = """
import os, sys
from pathlib import Path
from fangorn.registry import Registry
from fangorn.workspaces import Workspaces
original = Registry.finish_operation_step
def finish(self, operation_id, **kwargs):
    if kwargs["position"] == 1:
        os._exit(73)
    return original(self, operation_id, **kwargs)
Registry.finish_operation_step = finish
Workspaces(Registry(Path(sys.argv[1]))).delete(sys.argv[2])
"""
    process = subprocess.run(  # noqa: S603 -- fixed interpreter, disposable test state
        [sys.executable, "-c", script, str(database), workspace_id],
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 73, process.stderr
    assert not target.exists()
    interrupted = workspaces.inspect_workspace(workspace_id)
    assert interrupted.state == "deleting"
    assert interrupted.steps[0]["status"] == "completed"
    completed = workspaces.delete(workspace_id)
    assert completed.state == "deleted"
    assert completed.operation.id == interrupted.operation.id
    assert any(event["status"] == "unknown" for event in completed.history)


def test_cli_lifecycle_json_and_force_errors(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    environment = os.environ | {"XDG_STATE_HOME": str(tmp_path / "cli-state")}

    def cli(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 -- installed CLI and controlled test argv
            [sys.executable, "-m", "fangorn", "--json", "workspace", *arguments],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    created = cli(
        "create",
        "--repo",
        str(repository),
        "--branch",
        "topic",
        "--path",
        str(tmp_path / "topic"),
        "--headless",
    )
    assert created.returncode == 0, created.stderr
    workspace_id = json.loads(created.stdout)["workspace"]["definition"]["id"]
    assert (
        json.loads(cli("stop", "--workspace", workspace_id).stdout)["state"]
        == "stopped"
    )
    assert (
        json.loads(cli("start", "--path", str(tmp_path / "topic")).stdout)["state"]
        == "ready"
    )
    (tmp_path / "topic" / "dirty").write_text("keep")
    failed = cli("delete", "--workspace", workspace_id, "--yes")
    assert failed.returncode == 1
    assert failed.stdout == ""
    error = json.loads(failed.stderr)["error"]
    assert error["operation_id"] and error["resource"] == "worktree"
    assert error["step"] == "delete" and error["next_action"]
    recursive = cli("delete", "--workspace", workspace_id, "--recursive", "--yes")
    assert recursive.returncode != 0 and "--recursive" in recursive.stderr
    deleted = cli("delete", "--workspace", workspace_id, "--force", "--yes")
    assert deleted.returncode == 0, deleted.stderr
    assert json.loads(deleted.stdout)["state"] == "deleted"


@pytest.mark.parametrize("stage", ["before_definition", "after_target", "staging"])
def test_partial_creation_is_inspectable_and_deletable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    database = tmp_path / "state" / "registry.sqlite3"
    registry = Registry(database)
    workspaces = facade(tmp_path)
    if stage == "before_definition":
        registry.begin_create_intent(
            request_key="preliminary",
            request_id=None,
            request_json="{}",
            target_path=str(tmp_path / "topic"),
            workspace_id="preliminary",
            operation_id="create-preliminary",
            prepare_cache=False,
        )
        workspace_id = "preliminary"
    else:

        def interrupt(*args: Any, **kwargs: Any) -> None:
            raise RegistryError("injected crash boundary")

        with monkeypatch.context() as patch:
            if stage == "after_target":
                patch.setattr(Registry, "complete_workspace_create", interrupt)
            else:
                import fangorn.git_worktree as adapter

                original = adapter._run_git_process

                def run(path: Path, *args: str, **kwargs: Any) -> Any:
                    if args[:2] == ("worktree", "move"):
                        raise RegistryError("injected crash boundary")
                    return original(path, *args, **kwargs)

                patch.setattr(adapter, "_run_git_process", run)
            with pytest.raises(WorkspaceError, match="injected"):
                workspaces.create(
                    CreateWorkspace(str(repository), "topic", tmp_path / "topic")
                )
        with sqlite3.connect(database) as connection:
            workspace_id = str(
                connection.execute(
                    "SELECT workspace_id FROM workspace_create_intents"
                ).fetchone()[0]
            )
    inspected = workspaces.inspect_workspace(workspace_id)
    assert inspected.operation.kind == "create"
    if stage != "before_definition":
        assert inspected.workspace is not None
        assert inspected.workspace.resource_states[0].provisioning_status == (
            "created" if stage == "after_target" else "uncreated"
        )
    deleted = workspaces.delete(workspace_id)
    assert deleted.state == "deleted"
    assert not (tmp_path / "topic").exists()
    assert not list(tmp_path.glob(".fangorn-*"))
    assert workspaces.delete(workspace_id).operation.id == deleted.operation.id


def test_live_lease_blocks_lifecycle_and_forget(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic")
    )
    workspace_id = created.workspace.definition.id
    database = tmp_path / "state" / "registry.sqlite3"
    script = """
import sys
from pathlib import Path
from fangorn.registry import Registry
from fangorn.workspaces import Workspaces
registry = Registry(Path(sys.argv[1]))
workspaces = Workspaces(registry)
owner = workspaces._invocation_process_identity()
registry.acquire_lease(scope_kind="workspace", scope_key=sys.argv[2],
    operation_id="held",
    owner=owner, owner_status=workspaces._owner_status, update_operation=False)
print("held", flush=True)
sys.stdin.readline()
workspaces._finish_invocation(owner)
"""
    child = subprocess.Popen(  # noqa: S603 -- fixed subprocess and disposable registry
        [sys.executable, "-c", script, str(database), workspace_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout and child.stdout.readline().strip() == "held"
        for command in (workspaces.start, workspaces.stop, workspaces.delete):
            with pytest.raises(WorkspaceError, match="busy"):
                command(workspace_id)
        with pytest.raises(WorkspaceError, match="active operation"):
            workspaces.forget(workspace_id, acknowledge_orphans=True)
    finally:
        child.communicate("exit\n", timeout=10)
    assert workspaces.stop(workspace_id).state == "stopped"


def test_stale_lifecycle_success_failure_and_release_are_fenced(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    created = facade(tmp_path).create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic")
    )
    workspace_id = created.workspace.definition.id
    registry = Registry(tmp_path / "state" / "registry.sqlite3")
    old = registry.acquire_lease(
        scope_kind="workspace",
        scope_key=workspace_id,
        operation_id="old",
        owner=ProcessIdentity("old", "boot", 1, "old"),
        owner_status=lambda _: "dead",
        update_operation=False,
    )
    registry.begin_lifecycle(
        workspace_id,
        "old",
        old,
        kind="stop",
        state="stopping",
        actions=("stop",),
        expected_operation=created.operation.id,
    )
    newer = registry.acquire_lease(
        scope_kind="workspace",
        scope_key=workspace_id,
        operation_id="new",
        owner=ProcessIdentity("new", "boot", 2, "new"),
        owner_status=lambda _: "dead",
        update_operation=False,
    )
    assert newer > old
    for error in (None, "late failure"):
        with pytest.raises(RegistryError, match="lease fence"):
            registry.finish_lifecycle(
                workspace_id,
                "old",
                old,
                state="stopped" if error is None else "stop_failed",
                error=error,
            )
    with pytest.raises(RegistryError, match="lease fence"):
        registry.release_lease(
            scope_kind="workspace",
            scope_key=workspace_id,
            operation_id="old",
            lease_epoch=old,
        )


def test_force_does_not_bypass_locked_worktree(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    target = tmp_path / "topic"
    created = workspaces.create(CreateWorkspace(str(repository), "topic", target))
    git(repository, "worktree", "lock", str(target), "--reason", "keep")
    with pytest.raises(WorkspaceError, match="locked"):
        workspaces.delete(created.workspace.definition.id, force=True)
    assert target.exists()
    assert (
        workspaces.inspect_workspace(created.workspace.definition.id).state
        == "delete_failed"
    )


def test_force_does_not_remove_submodules(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    module = tmp_path / "module"
    create_repository(module)
    workspaces = facade(tmp_path)
    target = tmp_path / "topic"
    created = workspaces.create(CreateWorkspace(str(repository), "topic", target))
    git(
        target,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(module),
        "module",
    )
    with pytest.raises(WorkspaceError, match="submodule"):
        workspaces.delete(created.workspace.definition.id, force=True)
    assert (target / "module").exists()


def test_delete_requires_post_effect_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fangorn.git_worktree as adapter

    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    target = tmp_path / "topic"
    created = workspaces.create(CreateWorkspace(str(repository), "topic", target))
    original = adapter._run_git_process

    def no_remove(path: Path, *args: str, **kwargs: Any) -> Any:
        if args[:2] == ("worktree", "remove"):
            return subprocess.CompletedProcess(args, 0, b"", b"")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(adapter, "_run_git_process", no_remove)
    with pytest.raises(WorkspaceError, match="remains"):
        workspaces.delete(created.workspace.definition.id, force=True)
    assert target.exists()
    assert (
        workspaces.inspect_workspace(created.workspace.definition.id).state
        == "delete_failed"
    )


def test_partial_markerless_staging_never_authorizes_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fangorn.git_worktree as adapter

    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)

    def interrupt(*args: Any, **kwargs: Any) -> None:
        raise RegistryError("marker not persisted")

    with monkeypatch.context() as patch:
        patch.setattr(adapter, "establish_worktree_generation", interrupt)
        with pytest.raises(WorkspaceError, match="marker not persisted"):
            workspaces.create(
                CreateWorkspace(str(repository), "topic", tmp_path / "topic")
            )
    with sqlite3.connect(tmp_path / "state" / "registry.sqlite3") as connection:
        workspace_id = str(
            connection.execute(
                "SELECT workspace_id FROM workspace_create_intents"
            ).fetchone()[0]
        )
    with pytest.raises(WorkspaceError, match="ownership"):
        workspaces.delete(workspace_id, force=True)
    assert workspaces.inspect_workspace(workspace_id).state == "delete_failed"
    assert len(list(tmp_path.glob(".fangorn-*"))) == 2


def test_children_block_delete_force_and_forget(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic")
    )
    workspace_id = created.workspace.definition.id
    registry = Registry(tmp_path / "state" / "registry.sqlite3")
    registry.begin_create_intent(
        request_key="child",
        request_id=None,
        request_json="{}",
        target_path=str(tmp_path / "child"),
        workspace_id="child",
        operation_id="child-op",
        prepare_cache=False,
    )
    epoch = registry.acquire_lease(
        scope_kind="workspace",
        scope_key="child",
        operation_id="child-op",
        owner=ProcessIdentity("child", "boot", 1, "start"),
        owner_status=lambda _: "dead",
    )
    registry.record_workspace_definition(
        workspace_id="child",
        operation_id="child-op",
        lease_epoch=epoch,
        definition={"parent_id": workspace_id},
    )
    for force in (False, True):
        with pytest.raises(WorkspaceError, match="children"):
            workspaces.delete(workspace_id, force=force)
    with pytest.raises(WorkspaceError, match="children"):
        workspaces.forget(workspace_id, acknowledge_orphans=True)
    assert (tmp_path / "topic").exists()
    assert workspaces.inspect_workspace(workspace_id).error


def test_repeated_command_retains_unknown_quiescence_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fangorn.workspaces as application

    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic")
    )
    workspace_id = created.workspace.definition.id
    workspaces.stop(workspace_id)
    original = inspect_owned_worktree
    calls = 0

    def unknown(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GitQuiescenceError("quiescence remains unknown")
        return original(*args, **kwargs)

    monkeypatch.setattr(application, "inspect_owned_worktree", unknown)
    with pytest.raises(WorkspaceError, match="quiescence remains unknown"):
        workspaces.stop(workspace_id)
    assert calls == 1
    with sqlite3.connect(tmp_path / "state" / "registry.sqlite3") as connection:
        assert connection.execute(
            "SELECT active FROM mutation_leases WHERE scope_key = ?", (workspace_id,)
        ).fetchone() == (1,)
    with pytest.raises(WorkspaceError, match="busy"):
        workspaces.start(workspace_id)


@pytest.mark.parametrize("repeated", [False, True])
def test_command_returns_its_own_receipt_when_next_command_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repeated: bool,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic")
    )
    workspace_id = created.workspace.definition.id
    if repeated:
        workspaces.stop(workspace_id)
    interleaved = False
    original_finish = Registry.finish_lifecycle
    original_release = Registry.release_lease

    def finish(self: Registry, *args: Any, **kwargs: Any) -> Any:
        nonlocal interleaved
        result = original_finish(self, *args, **kwargs)
        if not interleaved and kwargs["state"] == "stopped":
            interleaved = True
            facade(tmp_path).start(workspace_id)
        return result

    def release(self: Registry, **kwargs: Any) -> None:
        nonlocal interleaved
        original_release(self, **kwargs)
        if not interleaved:
            interleaved = True
            facade(tmp_path).start(workspace_id)

    monkeypatch.setattr(
        Registry,
        "release_lease" if repeated else "finish_lifecycle",
        release if repeated else finish,
    )
    stopped = workspaces.stop(workspace_id)
    assert interleaved
    assert stopped.operation.kind == "stop" and stopped.operation.status == "completed"
    assert stopped.state == "stopped"
    assert workspaces.inspect_workspace(workspace_id).state == "ready"


def test_missing_moved_checkout_never_proves_deletion(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    target = tmp_path / "topic"
    created = workspaces.create(CreateWorkspace(str(repository), "topic", target))
    moved = tmp_path / "moved"
    hidden = tmp_path / "unavailable"
    git(repository, "worktree", "move", str(target), str(moved))
    moved.rename(hidden)
    with pytest.raises(WorkspaceError, match=r"unknown|ownership"):
        workspaces.delete(created.workspace.definition.id)
    assert hidden.exists()
    assert (
        workspaces.inspect_workspace(created.workspace.definition.id).state
        == "delete_failed"
    )


def test_inspection_retains_failed_operations_after_command_switch(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    target = tmp_path / "topic"
    created = workspaces.create(CreateWorkspace(str(repository), "topic", target))
    workspace_id = created.workspace.definition.id
    marker = observe_worktree(target).git_dir / "fangorn-worktree-generation"
    token = marker.read_text()
    marker.write_text("f" * 64)
    with pytest.raises(WorkspaceError, match=r"ownership|identity"):
        workspaces.stop(workspace_id)
    failed_before = workspaces.inspect_workspace(workspace_id)
    failed_id = failed_before.operation.id
    marker.write_text(token)
    workspaces.delete(workspace_id)
    inspected = workspaces.inspect_workspace(workspace_id)
    failed = next(op for op in inspected.operations if op["id"] == failed_id)
    assert failed["status"] == "failed" and failed["error"] == failed_before.error
    assert cast(list[dict[str, object]], failed["steps"])[0]["status"] == "failed"


def test_registry_rejects_cross_workspace_operation_reuse(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    a = workspaces.create(CreateWorkspace(str(repository), "a", tmp_path / "a"))
    b = workspaces.create(CreateWorkspace(str(repository), "b", tmp_path / "b"))
    stopped = workspaces.stop(a.workspace.definition.id)
    registry = Registry(tmp_path / "state" / "registry.sqlite3")
    epoch = registry.acquire_lease(
        scope_kind="workspace",
        scope_key=b.workspace.definition.id,
        operation_id=stopped.operation.id,
        owner=ProcessIdentity("writer", "boot", 1, "start"),
        owner_status=lambda _: "dead",
        update_operation=False,
    )
    with pytest.raises(
        RegistryError, match=r"operation.*(Workspace|workspace|identity)"
    ):
        registry.begin_lifecycle(
            b.workspace.definition.id,
            stopped.operation.id,
            epoch,
            kind="stop",
            state="stopping",
            actions=("stop",),
            expected_operation=b.operation.id,
        )
    assert (
        workspaces.inspect_workspace(a.workspace.definition.id).operation.status
        == "completed"
    )
    with sqlite3.connect(registry.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO workspace_lifecycle "
                "(workspace_id, operation_id, lifecycle_state) "
                "VALUES (?, ?, 'stopped')",
                (b.workspace.definition.id, stopped.operation.id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE operations SET workspace_id = ? WHERE id = ?",
                (b.workspace.definition.id, stopped.operation.id),
            )


@pytest.mark.parametrize("terminal", ["delete", "forget"])
def test_registry_tombstones_cannot_be_removed_or_reopened(
    tmp_path: Path, terminal: str
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic")
    )
    workspace_id = created.workspace.definition.id
    if terminal == "delete":
        receipt = workspaces.delete(workspace_id)
    else:
        receipt = workspaces.forget(workspace_id, acknowledge_orphans=True)
    registry = Registry(tmp_path / "state" / "registry.sqlite3")
    with sqlite3.connect(registry.path) as connection:
        with pytest.raises(sqlite3.OperationalError, match="rowid"):
            connection.execute(
                "INSERT OR REPLACE INTO workspace_lifecycle "
                "(rowid, workspace_id, operation_id, lifecycle_state, forgotten) "
                "SELECT rowid, 'replacement', operation_id, 'ready', 0 "
                "FROM workspace_lifecycle WHERE workspace_id = ?",
                (workspace_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match=r"immutable|tombstone"):
            connection.execute(
                "INSERT OR REPLACE INTO workspace_lifecycle "
                "(workspace_id, operation_id, lifecycle_state, forgotten) "
                "SELECT workspace_id, operation_id, 'ready', 0 "
                "FROM workspace_lifecycle WHERE workspace_id = ?",
                (workspace_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match=r"immutable|tombstone"):
            connection.execute(
                "DELETE FROM workspace_lifecycle WHERE workspace_id = ?",
                (workspace_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match=r"immutable|tombstone"):
            connection.execute(
                "UPDATE workspace_lifecycle "
                "SET lifecycle_state = 'starting', forgotten = 0 "
                "WHERE workspace_id = ?",
                (workspace_id,),
            )
    epoch = registry.acquire_lease(
        scope_kind="workspace",
        scope_key=workspace_id,
        operation_id="reopen",
        owner=ProcessIdentity("writer", "boot", 1, "start"),
        owner_status=lambda _: "dead",
        update_operation=False,
    )
    with pytest.raises(RegistryError, match=r"deleted|forgotten"):
        registry.begin_lifecycle(
            workspace_id,
            "reopen",
            epoch,
            kind="start",
            state="starting",
            actions=("start",),
            expected_operation=receipt.operation.id,
        )
    registry.release_lease(
        scope_kind="workspace",
        scope_key=workspace_id,
        operation_id="reopen",
        lease_epoch=epoch,
    )
    if terminal == "delete":
        assert workspaces.forget(workspace_id, acknowledge_orphans=True).forgotten
