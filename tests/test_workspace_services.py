from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from git_helpers import git
from test_workspace_create import create_repository, facade

import fangorn.resource_adapters as discovery
from fangorn.resource_adapters import (
    AdapterContext,
    AdapterDescriptor,
    AdapterObservation,
    AdapterResult,
    CommandAdapter,
    ObservationStatus,
    ResourceDefinition,
)
from fangorn.workspaces import CreateWorkspace, WorkspaceError, Workspaces


class Services(CommandAdapter):
    descriptor = AdapterDescriptor("test.services", 1, frozenset({"service"}))

    def __init__(self) -> None:
        self.states: dict[str, ObservationStatus] = {}
        self.events: list[tuple[str, str]] = []
        self.failure: tuple[str, str] | None = None
        self.foreign: str | None = None
        self.create_status: ObservationStatus = "stopped"
        self.continuation: discovery.Continuation = "safe"
        self.retain_on_delete = False

    def inspect(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterObservation:
        self.events.append(("inspect", definition.name))
        status = self.states.get(definition.name, "absent")
        return AdapterObservation(
            status,
            definition.locator,
            self.foreign
            or (definition.ownership_token if status != "absent" else None),
        )

    def _effect(
        self, action: str, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        assert context.worktree.is_dir()
        self.events.append((action, definition.name))
        if self.failure == (action, definition.name):
            return AdapterResult(False, "disposable service refused", self.continuation)
        if action == "delete" and self.retain_on_delete:
            return AdapterResult(True, continuation="safe")
        outcomes: dict[str, ObservationStatus] = {
            "create": self.create_status,
            "start": "ready",
            "stop": "stopped",
            "delete": "absent",
        }
        self.states[definition.name] = outcomes[action]
        return AdapterResult(True, continuation="safe")

    def create(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        return self._effect("create", definition, context)

    def start(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        return self._effect("start", definition, context)

    def stop(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        return self._effect("stop", definition, context)

    def delete(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        return self._effect("delete", definition, context)


def configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Services, Path, Path]:
    service = Services()
    entry = SimpleNamespace(name="services", load=lambda: service)
    monkeypatch.setattr(discovery, "entry_points", lambda **kwargs: [entry])
    repository = tmp_path / "repository"
    create_repository(repository)
    config = tmp_path / "fangorn.toml"
    config.write_text(
        'schema_version = 1\n[services.zeta]\nadapter="test.services"\n'
        '[services.alpha]\nadapter="test.services"\n'
    )
    return service, repository, config


def test_services_follow_declaration_order_across_persistence_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    snapshot = workspaces.validate_configuration(config)
    workspaces.grant_consent(snapshot.digest)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic", config=config)
    )
    workspace_id = created.workspace.definition.id
    assert [r.name for r in created.workspace.definition.resources] == [
        "worktree",
        "zeta",
        "alpha",
    ]
    assert [event for event in service.events if event[0] != "inspect"] == [
        ("create", "zeta"),
        ("create", "alpha"),
        ("start", "zeta"),
        ("start", "alpha"),
    ]
    service.events.clear()
    inspected = facade(tmp_path).inspect_workspace(workspace_id)
    assert inspected.observed_status == "ready"
    assert service.events == [("inspect", "zeta"), ("inspect", "alpha")]
    assert inspected.workspace is not None
    assert [r.name for r in inspected.workspace.definition.resources] == [
        "worktree",
        "zeta",
        "alpha",
    ]
    service.events.clear()
    assert workspaces.stop(workspace_id).state == "stopped"
    assert [event for event in service.events if event[0] != "inspect"] == [
        ("stop", "alpha"),
        ("stop", "zeta"),
    ]
    service.events.clear()
    assert workspaces.delete(workspace_id).state == "deleted"
    assert [event for event in service.events if event[0] != "inspect"] == [
        ("delete", "alpha"),
        ("delete", "zeta"),
    ]
    assert not (tmp_path / "topic").exists()


def test_safe_service_failure_never_removes_worktree_and_retry_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    workspaces.grant_consent(workspaces.validate_configuration(config).digest)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic", config=config)
    )
    workspace_id = created.workspace.definition.id
    service.failure = ("delete", "alpha")
    service.events.clear()
    with pytest.raises(WorkspaceError, match="alpha") as failed_error:
        workspaces.delete(workspace_id, force=True)
    assert failed_error.value.details["resource"] == "alpha"  # type: ignore[attr-defined]
    assert (tmp_path / "topic").is_dir()
    assert ("delete", "zeta") in service.events
    failed = workspaces.inspect_workspace(workspace_id)
    assert failed.state == "delete_failed"
    assert any(
        step["resource_name"] == "alpha" and step["status"] == "failed"
        for step in failed.steps
    )
    service.failure = None
    service.events.clear()
    retried = workspaces.delete(workspace_id, force=True)
    assert retried.operation.id == failed.operation.id
    assert retried.state == "deleted"
    assert ("delete", "zeta") not in service.events
    assert any(event["status"] == "failed" for event in retried.history)


def test_failed_restart_stop_never_starts_any_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    workspaces.grant_consent(workspaces.validate_configuration(config).digest)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic", config=config)
    )
    service.failure = ("stop", "alpha")
    service.events.clear()
    with pytest.raises(WorkspaceError):
        workspaces.restart(created.workspace.definition.id)
    assert not any(action == "start" for action, _name in service.events)
    assert (
        workspaces.inspect_workspace(created.workspace.definition.id).state
        == "stop_failed"
    )


def test_unconsented_services_never_execute_and_inspection_never_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    with pytest.raises(WorkspaceError, match="consent grant"):
        workspaces.create(
            CreateWorkspace(str(repository), "topic", tmp_path / "topic", config=config)
        )
    assert service.events == []
    assert workspaces.list_consents() == ()
    assert not (tmp_path / "topic").exists()


def test_revocation_and_foreign_ownership_block_service_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    digest = workspaces.validate_configuration(config).digest
    workspaces.grant_consent(digest)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic", config=config)
    )
    workspace_id = created.workspace.definition.id
    workspaces.revoke_consent(digest)
    service.events.clear()
    before = (tmp_path / "state/registry.sqlite3").read_bytes()
    assert workspaces.inspect_workspace(workspace_id).observed_status == "degraded"
    assert service.events == []
    assert before == (tmp_path / "state/registry.sqlite3").read_bytes()
    workspaces.grant_consent(digest)
    service.foreign = "another-owner"
    with pytest.raises(WorkspaceError, match="ownership"):
        workspaces.delete(workspace_id, force=True)
    assert (tmp_path / "topic").is_dir()
    assert not any(action == "delete" for action, _name in service.events)


def test_installed_cli_configuration_adapters_and_consent_are_thin_facade_commands(
    tmp_path: Path,
) -> None:
    config = tmp_path / "fangorn.toml"
    config.write_text("schema_version = 1\n")
    environment = os.environ | {"XDG_STATE_HOME": str(tmp_path / "state")}

    def run(*arguments: str) -> dict[str, object]:
        result = subprocess.run(  # noqa: S603 -- installed CLI with disposable arguments
            [sys.executable, "-m", "fangorn", "--json", *arguments],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)  # type: ignore[no-any-return]

    digest = str(run("config", "validate", str(config))["digest"])
    assert run("consent", "list")["digests"] == []
    assert not (tmp_path / "state").exists()
    assert run("adapter", "list")["schema_version"] == 2
    assert run("consent", "grant", digest)["digest"] == digest
    assert run("consent", "list")["digests"] == [digest]
    run("consent", "revoke", digest)
    assert run("consent", "list")["digests"] == []


SERVICE_SCRIPT = """import json, os, sys
from pathlib import Path
action = sys.argv[1]
state = Path('.service-' + os.environ['FANGORN_RESOURCE_NAME'])
if action == 'inspect':
    value = (json.loads(state.read_text()) if state.exists()
             else {'status': 'absent', 'ownership_token': None})
    print(json.dumps({'schema_version': 1,
                      'locator': os.environ['FANGORN_RESOURCE_LOCATOR'], **value}))
else:
    if action == 'delete':
        state.unlink(missing_ok=True)
    else:
        state.write_text(json.dumps({
            'status': 'ready' if action == 'start' else 'stopped',
            'ownership_token': os.environ['FANGORN_OWNERSHIP_TOKEN']}))
    print('hook stdout must not reach machine output')
    print('hook stderr stays isolated', file=sys.stderr)
"""


@pytest.mark.parametrize("explicit", [True, False])
def test_real_scripts_execute_immutable_snapshot_with_clean_installed_cli_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    root = tmp_path if explicit else repository
    script = root / "service.py"
    script.write_text(SERVICE_SCRIPT)
    script.chmod(0o644)
    config = root / "fangorn.toml"
    config.write_text(
        'schema_version = 1\n[services.app]\nadapter="fangorn.command"\n'
        'scripts=["./service.py"]\n'
        + "".join(
            f"{action}={json.dumps([sys.executable, './service.py', action])}\n"
            for action in ("inspect", "start", "stop", "delete")
        )
    )
    if not explicit:
        git(repository, "add", "fangorn.toml", "service.py")
        git(repository, "commit", "-m", "declare service")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    workspaces = Workspaces.from_environment()
    digest = workspaces.validate_configuration(config).digest
    workspaces.grant_consent(digest)
    request = CreateWorkspace(
        str(repository),
        "topic",
        tmp_path / "topic",
        config=config if explicit else None,
        start=False,
    )
    created = workspaces.create(request)
    workspace_id = created.workspace.definition.id
    assert created.workspace.state == "stopped"
    script.write_text("raise RuntimeError('mutable source was executed')\n")
    assert workspaces.validate_configuration(config).digest != digest
    result = subprocess.run(  # noqa: S603 -- installed CLI with disposable Workspace
        [
            sys.executable,
            "-m",
            "fangorn",
            "--json",
            "workspace",
            "start",
            "--workspace",
            workspace_id,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["state"] == "ready"
    assert result.stderr == ""
    assert workspaces.stop(workspace_id).state == "stopped"
    assert workspaces.delete(workspace_id).state == "deleted"


def test_completed_create_retry_rejects_service_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    workspaces.grant_consent(workspaces.validate_configuration(config).digest)
    request = CreateWorkspace(
        str(repository), "topic", tmp_path / "topic", config=config
    )
    workspaces.create(request)
    service.states["alpha"] = "stopped"
    with pytest.raises(WorkspaceError, match="alpha"):
        workspaces.create(request)


@pytest.mark.parametrize(
    "status", ["absent", "stopped", "ready", "degraded", "unknown"]
)
def test_no_start_requires_fresh_stopped_or_absent_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: ObservationStatus,
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    service.create_status = status
    workspaces = facade(tmp_path)
    workspaces.grant_consent(workspaces.validate_configuration(config).digest)
    request = CreateWorkspace(
        str(repository), "topic", tmp_path / "topic", config=config, start=False
    )
    if status in {"absent", "stopped"}:
        assert workspaces.create(request).workspace.state == "stopped"
    else:
        with pytest.raises(WorkspaceError):
            workspaces.create(request)
    assert not any(action == "start" for action, _ in service.events)


@pytest.mark.parametrize("continuation", ["unsafe", "unknown"])
def test_cleanup_without_safe_attempt_evidence_stops_before_independent_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    continuation: discovery.Continuation,
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    workspaces.grant_consent(workspaces.validate_configuration(config).digest)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic", config=config)
    )
    service.failure = ("delete", "alpha")
    service.continuation = continuation
    service.events.clear()
    with pytest.raises(WorkspaceError):
        workspaces.delete(created.workspace.definition.id, force=True)
    assert ("delete", "zeta") not in service.events
    assert (tmp_path / "topic").is_dir()


def test_successful_service_delete_without_absence_keeps_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    workspaces.grant_consent(workspaces.validate_configuration(config).digest)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic", config=config)
    )
    service.retain_on_delete = True
    with pytest.raises(WorkspaceError):
        workspaces.delete(created.workspace.definition.id, force=True)
    assert (
        workspaces.inspect_workspace(created.workspace.definition.id).state
        == "delete_failed"
    )
    assert (tmp_path / "topic").is_dir()


def test_failed_create_retry_retains_original_snapshot_and_skips_ready_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    digest = workspaces.validate_configuration(config).digest
    workspaces.grant_consent(digest)
    request = CreateWorkspace(
        str(repository), "topic", tmp_path / "topic", config=config
    )
    service.failure = ("start", "alpha")
    with pytest.raises(WorkspaceError):
        workspaces.create(request)
    config.write_text("invalid replacement configuration")
    service.failure = None
    service.events.clear()
    retried = workspaces.create(request)
    assert retried.workspace.state == "ready"
    assert retried.workspace.definition.configuration_digest == digest
    assert [event for event in service.events if event[0] != "inspect"] == [
        ("start", "alpha")
    ]


def test_missing_adapter_preflight_keeps_operation_unchanged_and_forget_calls_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    workspaces.grant_consent(workspaces.validate_configuration(config).digest)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic", config=config)
    )
    workspace_id = created.workspace.definition.id
    monkeypatch.setattr(discovery, "entry_points", lambda **kwargs: [])
    service.events.clear()
    with pytest.raises(WorkspaceError, match="not installed"):
        workspaces.stop(workspace_id)
    assert (
        workspaces.inspect_workspace(workspace_id).operation.id == created.operation.id
    )
    assert service.events == []
    assert workspaces.forget(workspace_id, acknowledge_orphans=True).forgotten
    assert service.events == []


def test_unknown_service_quiescence_retains_lease_until_execution_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fangorn.git import GitQuiescenceError

    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    workspaces.grant_consent(workspaces.validate_configuration(config).digest)
    created = workspaces.create(
        CreateWorkspace(
            str(repository), "topic", tmp_path / "topic", config=config, start=False
        )
    )
    children: list[subprocess.Popen[bytes]] = []

    def unproved(
        definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        assert context.liveness_fd is not None
        children.append(
            subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                pass_fds=(context.liveness_fd,),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        raise GitQuiescenceError("Service termination is unproven")

    monkeypatch.setattr(service, "start", unproved)
    try:
        with pytest.raises(WorkspaceError, match="unproven"):
            workspaces.start(created.workspace.definition.id)
        with pytest.raises(WorkspaceError, match="busy"):
            workspaces.delete(created.workspace.definition.id, force=True)
        assert (tmp_path / "topic").is_dir()
    finally:
        for child in children:
            child.terminate()
            child.wait(timeout=5)


def test_installed_create_failure_is_machine_json_with_resource_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    script = tmp_path / "service.py"
    script.write_text(
        SERVICE_SCRIPT.replace(
            "else:\n    if action == 'delete':",
            "else:\n    if action == 'start': sys.exit(17)\n    if action == 'delete':",
        )
    )
    config = tmp_path / "fangorn.toml"
    config.write_text(
        'schema_version = 1\n[services.app]\nadapter="fangorn.command"\n'
        'scripts=["./service.py"]\n'
        + "".join(
            f"{action}={json.dumps([sys.executable, './service.py', action])}\n"
            for action in ("inspect", "start", "stop", "delete")
        )
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    workspaces = Workspaces.from_environment()
    workspaces.grant_consent(workspaces.validate_configuration(config).digest)
    result = subprocess.run(  # noqa: S603 -- installed CLI with disposable Service failure
        [
            sys.executable,
            "-m",
            "fangorn",
            "--json",
            "workspace",
            "create",
            "--repo",
            str(repository),
            "--branch",
            "topic",
            "--path",
            str(tmp_path / "topic"),
            "--config",
            str(config),
            "--headless",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["schema_version"] == 2
    assert payload["error"]["resource"] == "app"
    assert payload["error"]["step"] == "start"


def test_service_probes_and_cleanup_do_not_use_a_foreign_worktree_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    workspaces.grant_consent(workspaces.validate_configuration(config).digest)
    target = tmp_path / "topic"
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", target, config=config)
    )
    target.rename(tmp_path / "moved")
    target.mkdir()
    service.events.clear()
    assert (
        workspaces.inspect_workspace(created.workspace.definition.id).observed_status
        == "degraded"
    )
    assert service.events == []
    with pytest.raises(WorkspaceError):
        workspaces.stop(created.workspace.definition.id)
    assert service.events == []


@pytest.mark.parametrize("start", [True, False])
def test_failed_absent_provisioning_is_reconciled_on_create_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start: bool
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    workspaces.grant_consent(workspaces.validate_configuration(config).digest)
    request = CreateWorkspace(
        str(repository), "topic", tmp_path / "topic", config=config, start=start
    )
    service.failure = ("create", "zeta")
    with pytest.raises(WorkspaceError):
        workspaces.create(request)
    service.failure = None
    service.events.clear()
    result = workspaces.create(request)
    assert ("create", "zeta") in service.events
    assert result.workspace.state == ("ready" if start else "stopped")


@pytest.mark.parametrize("command", ["start", "restart"])
def test_last_service_cannot_invalidate_worktree_and_commit_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    workspaces.grant_consent(workspaces.validate_configuration(config).digest)
    target = tmp_path / "topic"
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", target, config=config, start=False)
    )
    original = service._effect

    def move_after_start(
        action: str, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        result = original(action, definition, context)
        if action == "start" and definition.name == "alpha":
            target.rename(tmp_path / "moved")
        return result

    monkeypatch.setattr(service, "_effect", move_after_start)
    with pytest.raises(WorkspaceError):
        getattr(workspaces, command)(created.workspace.definition.id)
    failed = workspaces.inspect_workspace(created.workspace.definition.id)
    assert failed.state == "start_failed"
    assert failed.operation.status == "failed"
    assert any(step["status"] == "failed" for step in failed.steps)


@pytest.mark.parametrize(
    ("callback", "exception"),
    [
        ("inspect", RuntimeError),
        ("start", TypeError),
        ("stop", KeyError),
        ("delete", RuntimeError),
    ],
)
def test_unexpected_adapter_exception_is_journaled_and_cli_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    callback: str,
    exception: type[Exception],
) -> None:
    from click.testing import CliRunner

    from fangorn.cli import main

    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    workspaces.grant_consent(workspaces.validate_configuration(config).digest)
    created = workspaces.create(
        CreateWorkspace(
            str(repository),
            "topic",
            tmp_path / "topic",
            config=config,
            start=callback != "start",
        )
    )
    workspace_id = created.workspace.definition.id
    original = getattr(service, callback)

    def fail(definition: ResourceDefinition, context: AdapterContext) -> None:
        raise exception("unexpected adapter failure")

    monkeypatch.setattr(service, callback, fail)
    monkeypatch.setattr(Workspaces, "from_environment", lambda: workspaces)
    command = "stop" if callback == "inspect" else callback
    argv = ["--json", "workspace", command, "--workspace", workspace_id]
    if command == "delete":
        argv.append("--yes")
    result = CliRunner().invoke(main, argv)
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["schema_version"] == 2
    assert payload["error"]["resource"] in {"zeta", "alpha"}
    failed = workspaces.inspect_workspace(workspace_id)
    assert failed.state == f"{command}_failed"
    assert failed.operation.status == "failed"
    assert any(step["status"] == "failed" for step in failed.steps)
    monkeypatch.setattr(service, callback, original)
    assert (
        getattr(workspaces, command)(workspace_id).state
        == {"start": "ready", "stop": "stopped", "delete": "deleted"}[command]
    )


def test_delete_reconciles_interruption_after_worktree_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fangorn.workspaces as workspace_api
    from fangorn.git_worktree import delete_owned_worktree

    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    workspaces.grant_consent(workspaces.validate_configuration(config).digest)
    target = tmp_path / "topic"
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", target, config=config)
    )
    original = delete_owned_worktree

    def interrupt(target: Path, **kwargs: object) -> None:
        original(target, **kwargs)  # type: ignore[arg-type]
        raise SystemExit("after Worktree effect")

    monkeypatch.setattr(workspace_api, "delete_owned_worktree", interrupt)
    with pytest.raises(SystemExit):
        workspaces.delete(created.workspace.definition.id)
    assert not target.exists()
    interrupted = workspaces.inspect_workspace(created.workspace.definition.id)
    assert interrupted.operation.status == "running"
    assert any(
        step["action"] == "barrier" and step["status"] == "completed"
        for step in interrupted.steps
    )
    monkeypatch.setattr(workspace_api, "delete_owned_worktree", original)
    service.events.clear()
    recovered = facade(tmp_path).delete(created.workspace.definition.id)
    assert recovered.state == "deleted"
    assert recovered.operation.id == interrupted.operation.id
    assert all(step["status"] == "completed" for step in recovered.steps)
    assert service.events == []


def test_deleted_service_inspection_reports_historical_cleanup_without_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, config = configured(tmp_path, monkeypatch)
    workspaces = facade(tmp_path)
    digest = workspaces.validate_configuration(config).digest
    workspaces.grant_consent(digest)
    created = workspaces.create(
        CreateWorkspace(str(repository), "topic", tmp_path / "topic", config=config)
    )
    deleted = workspaces.delete(created.workspace.definition.id)
    workspaces.revoke_consent(digest)
    service.events.clear()
    before = (tmp_path / "state/registry.sqlite3").read_bytes()
    inspected = facade(tmp_path).inspect_workspace(created.workspace.definition.id)
    assert inspected.observed_status == "unknown"
    assert inspected.operation == deleted.operation
    resources = inspected.observation["resources"]
    assert isinstance(resources, dict)
    assert resources["zeta"]["observation"] == "unknown"
    assert resources["zeta"]["historical_observation"] == "absent"
    assert resources["zeta"]["evidence_source"] == "completed_delete"
    assert service.events == []
    assert before == (tmp_path / "state/registry.sqlite3").read_bytes()
