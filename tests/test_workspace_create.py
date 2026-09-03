from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from git_helpers import git, initialize_repository

from fangorn.git import observe_worktree
from fangorn.git_worktree import create_worktree as real_create_worktree
from fangorn.registry import ProcessIdentity, Registry, RegistryError
from fangorn.workspaces import CreateWorkspace, WorkspaceError, Workspaces


def create_repository(path: Path) -> str:
    initialize_repository(path)
    (path / "README.md").write_text("root\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "root")
    return git(path, "rev-parse", "HEAD")


def facade(tmp_path: Path) -> Workspaces:
    return Workspaces(
        Registry(tmp_path / "state" / "registry.sqlite3"),
        data_home=tmp_path / "data",
        cache_home=tmp_path / "cache",
    )


def test_adoption_cannot_claim_an_incomplete_create_target(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    registry = Registry(tmp_path / "state" / "registry.sqlite3")
    registry.begin_create_intent(
        request_key="create-target",
        request_id=None,
        request_json="{}",
        target_path=str(repository.resolve()),
        workspace_id="workspace-create",
        operation_id="operation-create",
        prepare_cache=False,
    )

    with pytest.raises(WorkspaceError, match="creation is incomplete"):
        Workspaces(registry).adopt(repository)


def test_adoption_and_create_use_the_same_repository_identity(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    created = workspaces.create(
        CreateWorkspace(
            repository=str(repository),
            branch="created",
            path=tmp_path / "worktrees" / "created",
        )
    )
    adopted_path = tmp_path / "worktrees" / "adopted"
    git(repository, "worktree", "add", "-b", "adopted", str(adopted_path), "HEAD")

    adopted = workspaces.adopt(adopted_path)

    assert (
        adopted.workspace.binding.repository_id
        == created.workspace.definition.repository_id
    )


def test_create_cannot_poison_an_adopted_target(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    adopted = workspaces.adopt(repository)

    with pytest.raises(WorkspaceError, match="already belongs"):
        workspaces.create(
            CreateWorkspace(
                repository=str(repository),
                branch="topic",
                path=repository,
                request_id="conflicting-create",
            )
        )

    retried = workspaces.adopt(repository)
    assert retried.created is False
    assert retried.workspace.binding.id == adopted.workspace.binding.id
    with sqlite3.connect(tmp_path / "state" / "registry.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_create_intents"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("start", "expected_state"),
    [(True, "ready"), (False, "stopped")],
)
def test_create_root_headless_local_workspace(
    tmp_path: Path, start: bool, expected_state: str
) -> None:
    repository = tmp_path / "repository"
    created_from_sha = create_repository(repository)
    target = tmp_path / "worktrees" / expected_state

    result = facade(tmp_path).create(
        CreateWorkspace(
            repository=str(repository),
            branch=f"topic-{expected_state}",
            path=target,
            headless=True,
            start=start,
        )
    )

    assert result.created is True
    assert result.workspace.definition.created_from_sha == created_from_sha
    assert result.workspace.definition.parent_id is None
    assert result.workspace.state == expected_state
    assert result.workspace.path == str(target.resolve())
    assert result.workspace.branch == f"topic-{expected_state}"
    assert [
        (resource.name, resource.kind, resource.adapter_id)
        for resource in result.workspace.definition.resources
    ] == [("worktree", "worktree", "fangorn.git-worktree")]
    assert result.workspace.resource_states[0].provisioning_status == "created"
    assert result.operation.status == "completed"
    assert git(target, "rev-parse", "HEAD") == created_from_sha


def test_equivalent_retry_reuses_resolved_values_and_completed_operation(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    created_from_sha = create_repository(repository)
    target = tmp_path / "worktrees" / "topic"
    workspaces = facade(tmp_path)
    request = CreateWorkspace(
        repository=str(repository),
        branch="topic",
        path=target,
        request_id="retry-1",
        headless=True,
    )
    first = workspaces.create(request)
    (target / "work.txt").write_text("work\n", encoding="utf-8")
    git(target, "add", "work.txt")
    git(target, "commit", "-m", "workspace work")
    git(target, "branch", "-m", "renamed-topic")
    (repository / "later.txt").write_text("later\n", encoding="utf-8")
    git(repository, "add", "later.txt")
    git(repository, "commit", "-m", "later")
    retried = workspaces.create(request)

    assert retried.created is False
    assert retried.workspace.definition.id == first.workspace.definition.id
    assert retried.workspace.definition.created_from_sha == created_from_sha
    assert retried.workspace.path == first.workspace.path
    assert retried.operation.id == first.operation.id
    assert retried.operation.status == "completed"


def test_reused_request_id_with_divergent_definition_conflicts_before_effects(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    first_target = tmp_path / "worktrees" / "first"
    second_target = tmp_path / "worktrees" / "second"
    workspaces.create(
        CreateWorkspace(
            repository=str(repository),
            branch="first",
            path=first_target,
            request_id="same-key",
            headless=True,
        )
    )

    with pytest.raises(WorkspaceError, match=r"idempotency key.*different request"):
        workspaces.create(
            CreateWorkspace(
                repository=str(repository),
                branch="second",
                path=second_target,
                request_id="same-key",
                headless=True,
            )
        )

    assert not second_target.exists()


def test_created_workspace_remains_visible_through_schema_1_reads(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    target = tmp_path / "worktrees" / "topic"
    workspaces = facade(tmp_path)

    created = workspaces.create(
        CreateWorkspace(
            repository=str(repository),
            branch="topic",
            path=target,
            headless=True,
        )
    ).workspace
    legacy = workspaces.inspect(target)

    assert legacy.binding.id == created.definition.id
    assert legacy.current_git_facts.path == created.path
    with sqlite3.connect(tmp_path / "state" / "registry.sqlite3") as connection:
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone() == (2,)


def test_create_from_clone_url_uses_journaled_shared_cache(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    created_from_sha = create_repository(repository)
    target = tmp_path / "worktrees" / "clone"
    workspaces = facade(tmp_path)

    result = workspaces.create(
        CreateWorkspace(
            repository=repository.as_uri(),
            branch="from-clone",
            path=target,
            request_id="clone-1",
            headless=True,
        )
    )

    assert result.workspace.definition.created_from_sha == created_from_sha
    cache_entries = list(
        (tmp_path / "cache" / "fangorn" / "repositories").glob("*/*.git")
    )
    assert len(cache_entries) == 1
    assert git(cache_entries[0], "rev-parse", "--is-bare-repository") == "true"
    with sqlite3.connect(tmp_path / "state" / "registry.sqlite3") as connection:
        assert connection.execute(
            "SELECT action, resource_name, status FROM operation_steps "
            "WHERE operation_id = ? ORDER BY position",
            (result.operation.id,),
        ).fetchall() == [
            ("prepare", "repository-cache", "completed"),
            ("create", "worktree", "completed"),
            ("start", "worktree", "completed"),
            ("inspect", "worktree", "completed"),
        ]


def test_clone_cache_is_namespaced_by_registry_identity(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    cache_home = tmp_path / "cache"
    for name in ("a", "b"):
        Workspaces(
            Registry(tmp_path / f"state-{name}" / "registry.sqlite3"),
            data_home=tmp_path / f"data-{name}",
            cache_home=cache_home,
        ).create(
            CreateWorkspace(
                repository=repository.as_uri(),
                branch=f"topic-{name}",
                path=tmp_path / "worktrees" / name,
            )
        )

    caches = list((cache_home / "fangorn" / "repositories").glob("*/*.git"))
    assert len(caches) == 2
    assert caches[0].parent != caches[1].parent


def test_clone_url_resolves_tag_base(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    tagged_sha = create_repository(repository)
    git(repository, "tag", "release-base")

    result = facade(tmp_path).create(
        CreateWorkspace(
            repository=repository.as_uri(),
            branch="from-tag",
            base="release-base",
            path=tmp_path / "worktrees" / "from-tag",
            headless=True,
        )
    )

    assert result.workspace.definition.created_from_sha == tagged_sha


def test_new_clone_create_refreshes_cached_remote_head(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    first_sha = create_repository(repository)
    workspaces = facade(tmp_path)
    first = workspaces.create(
        CreateWorkspace(
            repository=repository.as_uri(),
            branch="first",
            path=tmp_path / "worktrees" / "first",
        )
    )
    (repository / "later.txt").write_text("later\n", encoding="utf-8")
    git(repository, "add", "later.txt")
    git(repository, "commit", "-m", "later")
    second_sha = git(repository, "rev-parse", "HEAD")

    second = workspaces.create(
        CreateWorkspace(
            repository=repository.as_uri(),
            branch="second",
            path=tmp_path / "worktrees" / "second",
        )
    )

    assert first.workspace.definition.created_from_sha == first_sha
    assert second.workspace.definition.created_from_sha == second_sha


def test_new_clone_create_tracks_changed_remote_default_branch(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    workspaces.create(
        CreateWorkspace(
            repository=repository.as_uri(),
            branch="first",
            path=tmp_path / "worktrees" / "first",
        )
    )
    git(repository, "checkout", "-b", "new-default")
    (repository / "new-default.txt").write_text("new default\n", encoding="utf-8")
    git(repository, "add", "new-default.txt")
    git(repository, "commit", "-m", "new default")
    expected = git(repository, "rev-parse", "HEAD")

    second = workspaces.create(
        CreateWorkspace(
            repository=repository.as_uri(),
            branch="second",
            path=tmp_path / "worktrees" / "second",
        )
    )

    assert second.workspace.definition.created_from_sha == expected


def test_clone_refresh_does_not_overwrite_checked_out_workspace_branch(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    workspaces.create(
        CreateWorkspace(
            repository=repository.as_uri(),
            branch="topic",
            path=tmp_path / "worktrees" / "topic",
        )
    )
    git(repository, "branch", "topic")

    result = workspaces.create(
        CreateWorkspace(
            repository=repository.as_uri(),
            branch="other",
            path=tmp_path / "worktrees" / "other",
        )
    )

    assert result.workspace.state == "ready"


def test_clone_retry_reuses_completed_cache_while_origin_is_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    request = CreateWorkspace(
        repository=repository.as_uri(),
        branch="offline-retry",
        path=tmp_path / "worktrees" / "offline-retry",
        request_id="offline-retry",
    )

    class SimulatedInterruption(BaseException):
        pass

    def interrupt_after_effect(*args: object, **kwargs: object) -> object:
        real_create_worktree(*args, **kwargs)  # type: ignore[arg-type]
        raise SimulatedInterruption("after Worktree publication")

    monkeypatch.setattr("fangorn.workspaces.create_worktree", interrupt_after_effect)
    with pytest.raises(SimulatedInterruption):
        workspaces.create(request)
    repository.rename(tmp_path / "origin-offline")
    monkeypatch.setattr("fangorn.workspaces.create_worktree", real_create_worktree)

    recovered = workspaces.create(request)

    assert recovered.workspace.state == "ready"


def test_proven_dead_lease_takeover_fences_stale_result(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state" / "registry.sqlite3")
    intent, _ = registry.begin_create_intent(
        request_key="request",
        request_id="lease-test",
        request_json=json.dumps({"request": "lease-test"}),
        target_path=str(tmp_path / "target"),
        workspace_id="workspace-1",
        operation_id="operation-1",
        prepare_cache=True,
    )
    old_owner = ProcessIdentity("old", "boot", 1001, "start-old")
    old_epoch = registry.acquire_lease(
        scope_kind="repository",
        scope_key="source",
        operation_id=intent.operation_id,
        owner=old_owner,
        owner_status=lambda _owner: "live",
    )
    registry.start_operation_step(
        intent.operation_id,
        position=0,
        scope_kind="repository",
        scope_key="source",
        lease_epoch=old_epoch,
    )
    new_owner = ProcessIdentity("new", "boot", 1002, "start-new")

    new_epoch = registry.acquire_lease(
        scope_kind="repository",
        scope_key="source",
        operation_id=intent.operation_id,
        owner=new_owner,
        owner_status=lambda owner: "dead" if owner == old_owner else "live",
    )

    assert new_epoch == old_epoch + 1
    with pytest.raises(RegistryError, match="Stale operation result"):
        registry.save_cache_entry(
            "source",
            path="/stale-cache",
            repository_generation=None,
            operation_id=intent.operation_id,
            lease_epoch=old_epoch,
        )
    assert registry.cache_entry("source") is None
    with pytest.raises(RegistryError, match="Stale operation result"):
        registry.finish_operation_step(
            intent.operation_id,
            position=0,
            scope_kind="repository",
            scope_key="source",
            lease_epoch=old_epoch,
            result={"path": "/stale"},
        )
    assert (
        registry.start_operation_step(
            intent.operation_id,
            position=0,
            scope_kind="repository",
            scope_key="source",
            lease_epoch=new_epoch,
        )
        == "unknown"
    )


def test_proven_dead_workspace_lease_takeover_fences_stale_result(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    registry = Registry(tmp_path / "state" / "registry.sqlite3")
    intent, _ = registry.begin_create_intent(
        request_key="workspace-request",
        request_id="workspace-lease-test",
        request_json=json.dumps({"request": "workspace-lease-test"}),
        target_path=str(tmp_path / "workspace-target"),
        workspace_id="workspace-2",
        operation_id="operation-2",
        prepare_cache=False,
    )
    old_owner = ProcessIdentity("old", "boot", 1001, "start-old")
    old_epoch = registry.acquire_lease(
        scope_kind="workspace",
        scope_key=intent.workspace_id,
        operation_id=intent.operation_id,
        owner=old_owner,
        owner_status=lambda _owner: "live",
    )
    registry.enrich_create_intent(
        intent.operation_id,
        workspace_id=intent.workspace_id,
        lease_epoch=old_epoch,
        resolved={"created_from_sha": "a" * 40},
        steps=(("create", "worktree"),),
    )
    registry.start_operation_step(
        intent.operation_id,
        position=0,
        scope_kind="workspace",
        scope_key=intent.workspace_id,
        lease_epoch=old_epoch,
    )

    new_epoch = registry.acquire_lease(
        scope_kind="workspace",
        scope_key=intent.workspace_id,
        operation_id=intent.operation_id,
        owner=ProcessIdentity("new", "boot", 1002, "start-new"),
        owner_status=lambda owner: "dead" if owner == old_owner else "live",
    )

    assert new_epoch == old_epoch + 1
    with pytest.raises(RegistryError, match="Stale operation result"):
        registry.persist_resolved_sha(
            intent.operation_id,
            "a" * 40,
            workspace_id=intent.workspace_id,
            lease_epoch=old_epoch,
        )
    with pytest.raises(RegistryError, match="Stale operation result"):
        registry.enrich_create_intent(
            intent.operation_id,
            workspace_id=intent.workspace_id,
            lease_epoch=old_epoch,
            resolved={"created_from_sha": "b" * 40},
            steps=(("create", "worktree"),),
        )
    with pytest.raises(RegistryError, match="Stale operation result"):
        registry.record_workspace_definition(
            workspace_id=intent.workspace_id,
            operation_id=intent.operation_id,
            lease_epoch=old_epoch,
            definition={"id": intent.workspace_id},
        )
    with pytest.raises(RegistryError, match="Stale operation result"):
        registry.fail_create_operation(
            operation_id=intent.operation_id,
            workspace_id=intent.workspace_id,
            lease_epoch=old_epoch,
            error="stale failure",
        )
    with pytest.raises(RegistryError, match="Stale operation result"):
        registry.finish_operation_step(
            intent.operation_id,
            position=0,
            scope_kind="workspace",
            scope_key=intent.workspace_id,
            lease_epoch=old_epoch,
            result={"observation": "ready"},
        )
    with pytest.raises(RegistryError, match="Stale operation result"):
        registry.complete_workspace_create(
            intent=intent,
            observation=observe_worktree(repository),
            created_from_sha="a" * 40,
            configuration=b"",
            configuration_json="{}",
            configuration_digest="b" * 64,
            repository_id="repository",
            state="ready",
            lease_epoch=old_epoch,
        )
    with sqlite3.connect(tmp_path / "state" / "registry.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM workspaces").fetchone() == (0,)
        assert connection.execute(
            "SELECT epoch, active FROM mutation_leases "
            "WHERE scope_kind = 'workspace' AND scope_key = ?",
            (intent.workspace_id,),
        ).fetchone() == (new_epoch, 1)
        assert connection.execute(
            "SELECT error FROM operations WHERE id = ?", (intent.operation_id,)
        ).fetchone() != ("stale failure",)
    assert (
        registry.start_operation_step(
            intent.operation_id,
            position=0,
            scope_kind="workspace",
            scope_key=intent.workspace_id,
            lease_epoch=new_epoch,
        )
        == "unknown"
    )


def test_inconclusive_lease_owner_is_not_taken_over(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state" / "registry.sqlite3")
    intent, _ = registry.begin_create_intent(
        request_key="request",
        request_id=None,
        request_json="{}",
        target_path=str(tmp_path / "target"),
        workspace_id="workspace-1",
        operation_id="operation-1",
        prepare_cache=False,
    )
    epoch = registry.acquire_lease(
        scope_kind="workspace",
        scope_key=intent.workspace_id,
        operation_id=intent.operation_id,
        owner=ProcessIdentity("old", "boot", 1001, "start-old"),
        owner_status=lambda _owner: "live",
    )
    registry.enrich_create_intent(
        intent.operation_id,
        workspace_id=intent.workspace_id,
        lease_epoch=epoch,
        resolved={"created_from_sha": "a" * 40},
        steps=(("create", "worktree"),),
    )

    with pytest.raises(RegistryError, match="Workspace mutation is busy"):
        registry.acquire_lease(
            scope_kind="workspace",
            scope_key=intent.workspace_id,
            operation_id=intent.operation_id,
            owner=ProcessIdentity("new", "boot", 1002, "start-new"),
            owner_status=lambda _owner: "inconclusive",
        )


def test_cross_process_ended_invocation_is_proven_dead_while_process_lives(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "registry.sqlite3"
    script = """
import json
import sys
from pathlib import Path
from dataclasses import asdict
from fangorn.registry import Registry
from fangorn.workspaces import Workspaces
w = Workspaces(Registry(Path(sys.argv[1])))
owner = w._invocation_process_identity()
print(json.dumps(asdict(owner)), flush=True)
sys.stdin.readline()
w._finish_invocation(owner)
print("released", flush=True)
sys.stdin.readline()
"""
    child = subprocess.Popen(  # noqa: S603 -- fixed interpreter and test script
        [sys.executable, "-c", script, str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdin is not None
    assert child.stdout is not None
    try:
        owner = ProcessIdentity(**json.loads(child.stdout.readline()))
        registry = Registry(database)
        intent, _ = registry.begin_create_intent(
            request_key="cross-process",
            request_id="cross-process",
            request_json="{}",
            target_path=str(tmp_path / "cross-process"),
            workspace_id="cross-process",
            operation_id="cross-process",
            prepare_cache=False,
        )
        old_epoch = registry.acquire_lease(
            scope_kind="workspace",
            scope_key=intent.workspace_id,
            operation_id=intent.operation_id,
            owner=owner,
            owner_status=lambda _owner: "live",
        )
        checker = facade(tmp_path)
        assert checker._owner_status(owner) == "live"

        child.stdin.write("release\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "released"
        assert child.poll() is None
        new_epoch = registry.acquire_lease(
            scope_kind="workspace",
            scope_key=intent.workspace_id,
            operation_id=intent.operation_id,
            owner=ProcessIdentity("new", "boot", os.getpid(), "start"),
            owner_status=checker._owner_status,
        )
        assert new_epoch == old_epoch + 1
    finally:
        child.stdin.write("exit\n")
        child.stdin.flush()
        child.wait(timeout=5)


def test_dead_process_invocation_marker_is_removed(tmp_path: Path) -> None:
    database = tmp_path / "state" / "registry.sqlite3"
    script = """
import json
import sys
from pathlib import Path
from dataclasses import asdict
from fangorn.registry import Registry
from fangorn.workspaces import Workspaces
w = Workspaces(Registry(Path(sys.argv[1])))
print(json.dumps(asdict(w._invocation_process_identity())))
"""
    completed = subprocess.run(  # noqa: S603 -- fixed interpreter and test script
        [sys.executable, "-c", script, str(database)],
        check=True,
        capture_output=True,
        text=True,
    )
    owner = ProcessIdentity(**json.loads(completed.stdout))
    marker = database.parent / "invocations" / owner.process_instance_id
    assert marker.exists()

    assert facade(tmp_path)._owner_status(owner) == "dead"
    assert not marker.exists()


def test_cli_workspace_create_emits_schema_2(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    created_from_sha = create_repository(repository)
    state_home = tmp_path / "state"
    data_home = tmp_path / "data"
    cache_home = tmp_path / "cache"
    target = tmp_path / "worktrees" / "cli"
    executable = Path(sys.executable).with_name("fangorn")

    completed = subprocess.run(  # noqa: S603 -- test controls installed executable
        [
            executable,
            "--json",
            "workspace",
            "create",
            "--repo",
            str(repository),
            "--branch",
            "cli-topic",
            "--path",
            str(target),
            "--headless",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **dict(os.environ),
            "XDG_STATE_HOME": str(state_home),
            "XDG_DATA_HOME": str(data_home),
            "XDG_CACHE_HOME": str(cache_home),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == 2
    assert payload["workspace"]["definition"]["created_from_sha"] == created_from_sha
    assert payload["workspace"]["state"] == "ready"
    assert payload["operation"]["status"] == "completed"
    resource = payload["workspace"]["definition"]["resources"][0]
    assert "provisioning_status" not in resource
    assert payload["workspace"]["resource_states"] == [
        {"name": "worktree", "provisioning_status": "created"}
    ]
    payload["workspace"]["definition"]["id"] = "<workspace-id>"
    payload["workspace"]["definition"]["repository_id"] = "<repository-id>"
    payload["workspace"]["definition"]["resources"][0]["ownership_token"] = (
        "<ownership-token>"  # noqa: S105 -- normalized non-secret test placeholder
    )
    payload["operation"]["id"] = "<operation-id>"
    assert payload == {
        "schema_version": 2,
        "created": True,
        "workspace": {
            "definition": {
                "id": "<workspace-id>",
                "parent_id": None,
                "repository_id": "<repository-id>",
                "created_from_sha": created_from_sha,
                "configuration": {
                    "bytes_base64": "",
                    "value": {"schema_version": 1},
                    "digest": (
                        "e3b0c44298fc1c149afbf4c8996fb924"
                        "27ae41e4649b934ca495991b7852b855"
                    ),
                },
                "resources": [
                    {
                        "name": "worktree",
                        "kind": "worktree",
                        "adapter_id": "fangorn.git-worktree",
                        "adapter_api_major": 1,
                        "configuration": {},
                        "external_reference": None,
                        "locator": str(target.resolve()),
                        "ownership_token": "<ownership-token>",
                    }
                ],
            },
            "resource_states": [{"name": "worktree", "provisioning_status": "created"}],
            "state": "ready",
            "version": 1,
            "path": str(target.resolve()),
            "branch": "cli-topic",
        },
        "operation": {
            "id": "<operation-id>",
            "kind": "create",
            "status": "completed",
        },
    }

    stopped = subprocess.run(  # noqa: S603 -- test controls installed executable
        [
            executable,
            "--json",
            "workspace",
            "create",
            "--repo",
            str(repository),
            "--branch",
            "cli-stopped",
            "--path",
            str(tmp_path / "worktrees" / "cli-stopped"),
            "--headless",
            "--no-start",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **dict(os.environ),
            "XDG_STATE_HOME": str(state_home),
            "XDG_DATA_HOME": str(data_home),
            "XDG_CACHE_HOME": str(cache_home),
        },
    )
    assert stopped.returncode == 0, stopped.stderr
    stopped_payload = json.loads(stopped.stdout)
    assert stopped_payload["workspace"]["state"] == "stopped"
    assert stopped_payload["operation"]["status"] == "completed"
    assert (tmp_path / "worktrees" / "cli-stopped").exists()


def test_cli_workspace_create_human_retry_and_scope_error(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    target = tmp_path / "worktrees" / "human"
    executable = Path(sys.executable).with_name("fangorn")
    environment = {
        **dict(os.environ),
        "XDG_STATE_HOME": str(state_home),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    arguments: list[str | Path] = [
        executable,
        "workspace",
        "create",
        "--repo",
        str(repository),
        "--branch",
        "human-topic",
        "--path",
        str(target),
        "--headless",
    ]

    created = subprocess.run(  # noqa: S603 -- test controls installed executable
        arguments, check=False, capture_output=True, text=True, env=environment
    )
    retried = subprocess.run(  # noqa: S603 -- test controls installed executable
        arguments, check=False, capture_output=True, text=True, env=environment
    )
    unsupported = subprocess.run(  # noqa: S603 -- test controls installed executable
        arguments[:-1], check=False, capture_output=True, text=True, env=environment
    )

    assert created.returncode == 0
    assert created.stdout.startswith("Created Workspace ")
    assert "State: ready\n" in created.stdout
    assert f"Path: {target}\n" in created.stdout
    assert retried.returncode == 0
    assert retried.stdout.startswith("Already created Workspace ")
    assert unsupported.returncode != 0
    assert "Only headless Workspace creation" in unsupported.stderr


def test_retry_reconciles_worktree_after_interrupted_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    target = tmp_path / "worktrees" / "interrupted"
    workspaces = facade(tmp_path)
    request = CreateWorkspace(
        repository=str(repository),
        branch="interrupted",
        path=target,
        request_id="interrupted-1",
        headless=True,
    )
    interrupted = False
    definition_seen = False

    class SimulatedInterruption(BaseException):
        pass

    def interrupt_after_effect(*args: object, **kwargs: object) -> object:
        nonlocal definition_seen, interrupted
        with sqlite3.connect(tmp_path / "state" / "registry.sqlite3") as connection:
            row = connection.execute(
                "SELECT definition_json FROM workspace_aggregates"
            ).fetchone()
        assert row is not None
        definition = json.loads(row[0])
        assert definition["id"]
        assert definition["repository_id"]
        assert definition["created_from_sha"]
        assert definition["configuration_digest"]
        assert definition["resources"] == [
            {
                "adapter_api_major": 1,
                "adapter_id": "fangorn.git-worktree",
                "configuration": {},
                "external_reference": None,
                "kind": "worktree",
                "locator": str(target.resolve()),
                "name": "worktree",
                "ownership_token": definition["resources"][0]["ownership_token"],
            }
        ]
        definition_seen = True
        observation = real_create_worktree(*args, **kwargs)  # type: ignore[arg-type]
        if not interrupted:
            interrupted = True
            raise SimulatedInterruption("simulated interruption after Git effect")
        return observation

    monkeypatch.setattr("fangorn.workspaces.create_worktree", interrupt_after_effect)
    with pytest.raises(SimulatedInterruption, match="simulated interruption"):
        workspaces.create(request)
    assert definition_seen

    with sqlite3.connect(tmp_path / "state" / "registry.sqlite3") as connection:
        assert connection.execute(
            "SELECT status FROM workspace_create_intents WHERE request_id = ?",
            (request.request_id,),
        ).fetchone() == ("create_failed",)
        assert connection.execute(
            "SELECT status FROM operations WHERE id = ("
            "SELECT operation_id FROM workspace_create_intents WHERE request_id = ?)",
            (request.request_id,),
        ).fetchone() == ("failed",)
        assert connection.execute(
            "SELECT active FROM mutation_leases WHERE scope_kind = 'workspace'"
        ).fetchone() == (0,)
        aggregate_row = connection.execute(
            "SELECT definition_json, lifecycle_state FROM workspace_aggregates "
            "WHERE workspace_id = (SELECT workspace_id "
            "FROM workspace_create_intents WHERE request_id = ?)",
            (request.request_id,),
        ).fetchone()
        assert aggregate_row is not None
        assert json.loads(aggregate_row[0])["created_from_sha"]
        assert aggregate_row[1] == "create_failed"

    with pytest.raises(WorkspaceError, match="creation is incomplete"):
        facade(tmp_path).adopt(target)
    recovered = facade(tmp_path).create(request)
    adopted = facade(tmp_path).adopt(target)

    assert recovered.created is False
    assert recovered.workspace.state == "ready"
    assert adopted.created is False
    assert adopted.workspace.binding.id == recovered.workspace.definition.id
    assert (
        git(target, "rev-parse", "HEAD")
        == recovered.workspace.definition.created_from_sha
    )


def test_ended_same_process_invocation_can_recover_when_cleanup_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    target = tmp_path / "worktrees" / "cleanup-contention"
    workspaces = facade(tmp_path)
    request = CreateWorkspace(
        repository=str(repository),
        branch="cleanup-contention",
        path=target,
        request_id="cleanup-contention-1",
        headless=True,
    )

    def interrupt(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("effect failed")

    def cleanup_busy(**_kwargs: object) -> None:
        raise RegistryError("Registry is busy")

    monkeypatch.setattr("fangorn.workspaces.create_worktree", interrupt)
    monkeypatch.setattr(workspaces._registry, "fail_create_operation", cleanup_busy)
    with pytest.raises(RuntimeError, match="effect failed"):
        workspaces.create(request)

    monkeypatch.setattr("fangorn.workspaces.create_worktree", real_create_worktree)
    recovered = facade(tmp_path).create(request)
    assert recovered.workspace.state == "ready"


def test_create_finishing_between_intent_read_and_lease_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    target = tmp_path / "worktrees" / "lease-race"
    workspaces = facade(tmp_path)
    request = CreateWorkspace(
        repository=str(repository),
        branch="lease-race",
        path=target,
        request_id="lease-race-1",
        headless=True,
    )
    first_waiting = Event()
    release_first = Event()
    acquire = workspaces._registry.acquire_lease
    workspace_calls = 0

    def interleaved_acquire(**kwargs: object) -> int:
        nonlocal workspace_calls
        if kwargs["scope_kind"] == "workspace":
            workspace_calls += 1
            if workspace_calls == 1:
                first_waiting.set()
                assert release_first.wait(timeout=10)
        return acquire(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(workspaces._registry, "acquire_lease", interleaved_acquire)
    with ThreadPoolExecutor(max_workers=2) as executor:
        delayed = executor.submit(workspaces.create, request)
        assert first_waiting.wait(timeout=5)
        completed = workspaces.create(request)
        release_first.set()
        retried = delayed.result(timeout=10)

    assert completed.workspace.state == "ready"
    assert completed.created is False
    assert retried.created is True
    assert retried.workspace.definition.id == completed.workspace.definition.id
    assert retried.operation.id == completed.operation.id


def test_same_facade_rejects_concurrent_create_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    target = tmp_path / "worktrees" / "concurrent"
    workspaces = facade(tmp_path)
    request = CreateWorkspace(
        repository=str(repository),
        branch="concurrent",
        path=target,
        request_id="concurrent-1",
        headless=True,
    )
    effect_started = Event()
    allow_effect = Event()

    def block_effect(*args: object, **kwargs: object) -> object:
        effect_started.set()
        assert allow_effect.wait(timeout=5)
        return real_create_worktree(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("fangorn.workspaces.create_worktree", block_effect)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(workspaces.create, request)
        assert effect_started.wait(timeout=5)
        with pytest.raises(WorkspaceError, match="Workspace mutation is busy"):
            workspaces.create(request)
        allow_effect.set()
        assert first.result(timeout=10).workspace.state == "ready"


def test_default_configuration_is_snapshotted_from_resolved_commit(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    configuration = b"schema_version = 1\n"
    (repository / "fangorn.toml").write_bytes(configuration)
    git(repository, "add", "fangorn.toml")
    git(repository, "commit", "-m", "configure")
    target = tmp_path / "worktrees" / "configured"
    workspaces = facade(tmp_path)
    request = CreateWorkspace(
        repository=str(repository),
        branch="configured",
        path=target,
        request_id="configured-1",
        headless=True,
    )
    first = workspaces.create(request)
    (repository / "fangorn.toml").write_text(
        "schema_version = 1\n[services.app]\nadapter = 'fangorn.command'\n",
        encoding="utf-8",
    )
    retried = workspaces.create(request)

    assert first.workspace.definition.configuration == configuration
    assert retried.workspace.definition.configuration == configuration
    assert (
        retried.workspace.definition.configuration_digest
        == first.workspace.definition.configuration_digest
    )


def test_resolved_sha_survives_configuration_failure_and_ref_movement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    original_sha = create_repository(repository)
    target = tmp_path / "worktrees" / "stable-sha"
    request = CreateWorkspace(
        repository=str(repository),
        branch="stable-sha",
        path=target,
        request_id="stable-sha-1",
        headless=True,
    )

    def fail_configuration(*_args: object, **_kwargs: object) -> bytes:
        raise OSError("configuration unavailable")

    monkeypatch.setattr("fangorn.workspaces.read_configuration", fail_configuration)
    with pytest.raises(WorkspaceError, match="configuration unavailable"):
        facade(tmp_path).create(request)
    with sqlite3.connect(tmp_path / "state" / "registry.sqlite3") as connection:
        assert connection.execute(
            "SELECT resolved_sha, status FROM workspace_create_intents "
            "WHERE request_id = ?",
            (request.request_id,),
        ).fetchone() == (original_sha, "create_failed")
        assert connection.execute(
            "SELECT status FROM operations WHERE id = (SELECT operation_id "
            "FROM workspace_create_intents WHERE request_id = ?)",
            (request.request_id,),
        ).fetchone() == ("failed",)
    (repository / "later.txt").write_text("later\n", encoding="utf-8")
    git(repository, "add", "later.txt")
    git(repository, "commit", "-m", "move source ref")
    monkeypatch.undo()

    recovered = facade(tmp_path).create(request)

    assert recovered.workspace.definition.created_from_sha == original_sha
    assert git(target, "rev-parse", "HEAD") == original_sha


def test_local_source_without_base_uses_that_checkout_head(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    source_head = create_repository(repository)
    source_checkout = tmp_path / "source-checkout"
    git(repository, "worktree", "add", "-b", "source", str(source_checkout), "HEAD")
    (repository / "main-only.txt").write_text("later\n", encoding="utf-8")
    git(repository, "add", "main-only.txt")
    git(repository, "commit", "-m", "advance main")
    target = tmp_path / "worktrees" / "from-source"

    created = (
        facade(tmp_path)
        .create(
            CreateWorkspace(
                repository=str(source_checkout),
                branch="from-source",
                path=target,
                headless=True,
            )
        )
        .workspace
    )

    assert created.definition.created_from_sha == source_head
    assert git(target, "rev-parse", "HEAD") == source_head


def test_explicit_target_is_canonicalized_before_definition(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    requested = linked_parent / "unused" / ".." / "topic"
    request = CreateWorkspace(
        repository=str(repository),
        branch="canonical-target",
        path=requested,
        request_id="canonical-target",
        headless=True,
    )

    created = facade(tmp_path).create(request)
    retried = facade(tmp_path).create(request)

    assert created.workspace.path == str((real_parent / "topic").resolve())
    assert retried.workspace.definition.id == created.workspace.definition.id


def test_symlink_loop_target_fails_as_workspace_error_before_state(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    first = tmp_path / "loop-a"
    second = tmp_path / "loop-b"
    first.symlink_to(second)
    second.symlink_to(first)

    with pytest.raises(WorkspaceError, match="cannot be canonicalized"):
        facade(tmp_path).create(
            CreateWorkspace(
                repository=str(repository),
                branch="loop-target",
                path=first / "target",
                headless=True,
            )
        )
    assert not (tmp_path / "state").exists()


def test_invalid_git_branch_does_not_poison_target(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    target = tmp_path / "worktrees" / "reusable"
    workspaces = facade(tmp_path)

    with pytest.raises(WorkspaceError, match="branch is invalid"):
        workspaces.create(
            CreateWorkspace(
                repository=str(repository),
                branch="bad..name",
                path=target,
                headless=True,
            )
        )
    assert not (tmp_path / "state").exists()

    created = workspaces.create(
        CreateWorkspace(
            repository=str(repository),
            branch="valid-name",
            path=target,
            headless=True,
        )
    )
    assert created.workspace.state == "ready"


def test_schema_2_definition_is_immutable_but_provisioning_status_is_operational(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    target = tmp_path / "worktrees" / "immutable"
    created = (
        facade(tmp_path)
        .create(
            CreateWorkspace(
                repository=str(repository),
                branch="immutable",
                path=target,
                headless=True,
            )
        )
        .workspace
    )
    database = tmp_path / "state" / "registry.sqlite3"

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="definition is immutable"):
            connection.execute(
                "UPDATE workspaces SET created_from_sha = ? WHERE id = ?",
                ("0" * 40, created.definition.id),
            )
        connection.execute(
            "UPDATE workspace_resources SET provisioning_status = 'uncreated' "
            "WHERE workspace_id = ?",
            (created.definition.id,),
        )
        assert connection.execute(
            "SELECT provisioning_status FROM workspace_resources "
            "WHERE workspace_id = ?",
            (created.definition.id,),
        ).fetchone() == ("uncreated",)
        with pytest.raises(sqlite3.IntegrityError, match="membership is immutable"):
            connection.execute(
                "INSERT INTO workspace_resources VALUES "
                "(?, 1, 'extra', 'worktree', 'fangorn.git-worktree', 1, '{}', "
                "NULL, ?, ?, 'created')",
                (created.definition.id, str(tmp_path / "extra"), "f" * 64),
            )
        with pytest.raises(sqlite3.IntegrityError, match="create intent is immutable"):
            connection.execute(
                "UPDATE workspace_create_intents SET target_path = ? "
                "WHERE workspace_id = ?",
                (str(tmp_path / "retargeted"), created.definition.id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="create intent is immutable"):
            connection.execute(
                "UPDATE workspace_create_intents SET resolved_json = '{}' "
                "WHERE workspace_id = ?",
                (created.definition.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="completion is immutable"):
            connection.execute(
                "UPDATE workspaces SET completed_operation_id = NULL WHERE id = ?",
                (created.definition.id,),
            )
        connection.execute(
            "INSERT INTO operations VALUES "
            "('orphan-create', 'missing-workspace', 'create', 'running', NULL, ?, ?)",
            ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="requires one worktree"):
            connection.execute(
                "UPDATE operations SET status = 'completed' WHERE id = 'orphan-create'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="requires one worktree"):
            connection.execute(
                "INSERT INTO operations VALUES "
                "('completed-bypass', 'missing-workspace', 'create', 'completed', "
                "NULL, ?, ?)",
                ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
        connection.execute(
            "INSERT INTO operations VALUES "
            "('kind-bypass', 'missing-workspace', 'stop', 'completed', NULL, ?, ?)",
            ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="requires one worktree"):
            connection.execute(
                "UPDATE operations SET kind = 'create' WHERE id = 'kind-bypass'"
            )
        connection.execute(
            "INSERT INTO operations VALUES "
            "('workspace-bypass', ?, 'create', 'running', NULL, ?, ?)",
            (
                created.definition.id,
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        connection.execute(
            "UPDATE operations SET status = 'completed' WHERE id = 'workspace-bypass'"
        )
        with pytest.raises(sqlite3.IntegrityError, match="requires one worktree"):
            connection.execute(
                "UPDATE operations SET workspace_id = 'missing-workspace' "
                "WHERE id = 'workspace-bypass'"
            )
        connection.execute(
            "INSERT INTO workspace_create_intents "
            "(operation_id, workspace_id, request_key, request_json, target_path, "
            "status, created_at, updated_at) VALUES "
            "('pending-op', 'pending-workspace', 'pending-key', '{}', '/pending', "
            "'creating', ?, ?)",
            ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO operations VALUES "
            "('pending-op', 'pending-workspace', 'stop', 'completed', NULL, ?, ?)",
            ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO workspace_aggregates "
            "(workspace_id, definition_json, lifecycle_state) "
            "VALUES ('pending-workspace', '{}', 'creating')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="completion is invalid"):
            connection.execute(
                "UPDATE workspace_aggregates SET completed_operation_id = 'pending-op' "
                "WHERE workspace_id = 'pending-workspace'"
            )
        completed_operation_id = connection.execute(
            "SELECT completed_operation_id FROM workspaces WHERE id = ?",
            (created.definition.id,),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="completion is immutable"):
            connection.execute(
                "UPDATE operations SET kind = 'stop' WHERE id = ?",
                (completed_operation_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="definition is immutable"):
            connection.execute(
                "DELETE FROM workspace_aggregates WHERE workspace_id = ?",
                (created.definition.id,),
            )
        repository_id = connection.execute(
            "SELECT repository_id FROM workspaces WHERE id = ?",
            (created.definition.id,),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="completion is invalid"):
            connection.execute(
                "INSERT INTO workspaces "
                "(id, repository_id, git_dir, git_dir_generation, path, created_at, "
                "last_observed_at, last_observation_token, completed_operation_id) "
                "VALUES ('ghost-workspace', ?, '/ghost-git-dir', ?, '/ghost', ?, ?, "
                "1, 'ghost-operation')",
                (
                    repository_id,
                    "e" * 64,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )

    with sqlite3.connect(database) as connection:
        resource = (
            "pending-resource",
            0,
            "worktree",
            "worktree",
            "fangorn.git-worktree",
            1,
            "{}",
            None,
            str(tmp_path / "pending-resource"),
            "d" * 64,
            "created",
        )
        connection.execute(
            "INSERT INTO workspace_resources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            resource,
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            connection.execute(
                "INSERT INTO workspace_resources VALUES "
                "(?, 1, 'second-worktree', 'worktree', 'fangorn.git-worktree', 1, "
                "'{}', NULL, ?, ?, 'created')",
                (
                    "pending-resource",
                    str(tmp_path / "second-pending-resource"),
                    "c" * 64,
                ),
            )


@pytest.mark.parametrize(
    ("create_request", "message"),
    [
        (
            CreateWorkspace(repository="unused", branch="topic", headless=False),
            "Only headless",
        ),
        (CreateWorkspace(repository="", branch="topic"), "requires a repository"),
        (CreateWorkspace(repository="unused", branch="-bad"), "branch is invalid"),
    ],
)
def test_create_rejects_unsupported_definition_before_state(
    tmp_path: Path, create_request: CreateWorkspace, message: str
) -> None:
    workspaces = facade(tmp_path)

    with pytest.raises(WorkspaceError, match=message):
        workspaces.create(create_request)

    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    ("configuration", "message"),
    [
        ("schema_version = 2\n", "schema_version = 1"),
        (
            "schema_version = 1\n[services.app]\nadapter = 'fangorn.command'\n",
            "Service Resources are not available",
        ),
        (
            "schema_version = 1\nrelease_date = 2026-09-02\n",
            "unsupported by schema-2 JSON",
        ),
    ],
)
def test_create_rejects_configuration_outside_f2_scope(
    tmp_path: Path, configuration: str, message: str
) -> None:
    source = tmp_path / "repository"
    create_repository(source)
    config = tmp_path / "fangorn.toml"
    config.write_text(configuration, encoding="utf-8")

    with pytest.raises(WorkspaceError, match=message):
        facade(tmp_path).create(
            CreateWorkspace(
                repository=str(source),
                branch="topic",
                path=tmp_path / "target",
                config=config,
                headless=True,
            )
        )

    assert not (tmp_path / "target").exists()


def test_cli_rejects_temporal_configuration_with_domain_error(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    config = tmp_path / "fangorn.toml"
    config.write_text(
        "schema_version = 1\nrelease_date = 2026-09-02\n", encoding="utf-8"
    )
    executable = Path(sys.executable).with_name("fangorn")

    completed = subprocess.run(  # noqa: S603 -- test controls installed executable
        [
            executable,
            "workspace",
            "create",
            "--repo",
            str(repository),
            "--branch",
            "temporal-config",
            "--path",
            str(tmp_path / "target"),
            "--config",
            str(config),
            "--headless",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **dict(os.environ),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
    )

    assert completed.returncode != 0
    assert "unsupported by schema-2 JSON" in completed.stderr
