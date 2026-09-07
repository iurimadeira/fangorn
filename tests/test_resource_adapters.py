import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from git_helpers import git, initialize_repository

from fangorn.configuration import ConsentStore
from fangorn.git import repository_generation
from fangorn.resource_adapters import (
    AdapterContext,
    CommandAdapter,
    GitWorktreeContext,
    discover_adapters,
)
from fangorn.workspaces import ResourceDefinition


def _command_definition(configuration: dict[str, object]) -> ResourceDefinition:
    return ResourceDefinition(
        "app", "service", "fangorn.command", 1, configuration, None, "app-id", "owner"
    )


def _consented_context(tmp_path: Path) -> AdapterContext:
    store = ConsentStore(tmp_path / "consent")
    store.grant("a" * 64)
    return AdapterContext("ws", None, tmp_path, "a" * 64, store, {})


def test_discovered_git_adapter_requires_git_context(tmp_path: Path) -> None:
    definition = ResourceDefinition(
        "worktree",
        "worktree",
        "fangorn.git-worktree",
        1,
        {},
        None,
        str(tmp_path / "checkout"),
        "a" * 64,
    )
    context = _consented_context(tmp_path)
    adapter = discover_adapters()["fangorn.git-worktree"]
    observed = adapter.inspect(definition, context)
    assert observed.status == "unknown"
    assert observed.error == "GitWorktreeContext is required"
    for action in (adapter.create, adapter.start, adapter.stop, adapter.delete):
        result = action(definition, context)
        assert not result.success
        assert result.error == "GitWorktreeContext is required"
        assert result.continuation == "unsafe"
    assert not Path(definition.locator).exists()


def test_discovered_git_adapter_runs_owned_worktree_lifecycle(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    previous_umask = os.umask(0o077)
    try:
        initialize_repository(repository)
        (repository / "tracked.txt").write_text("committed\n")
        git(repository, "add", "tracked.txt")
        git(repository, "commit", "-m", "Initial")
    finally:
        os.umask(previous_umask)
    commit = git(repository, "rev-parse", "HEAD")
    common_dir = repository / ".git"
    generation = repository_generation(common_dir, create=True)
    assert generation is not None
    git_context = GitWorktreeContext(
        repository, common_dir, generation, commit, "workspace"
    )
    target = tmp_path / "checkout"
    definition = ResourceDefinition(
        "worktree",
        "worktree",
        "fangorn.git-worktree",
        1,
        {},
        None,
        str(target),
        "a" * 64,
    )
    read_fd, write_fd = os.pipe()
    try:
        context = AdapterContext(
            "ws",
            "operation",
            target,
            "b" * 64,
            ConsentStore(tmp_path / "ungranted-consent"),
            {},
            liveness_fd=read_fd,
            git=git_context,
        )
        adapter = discover_adapters()["fangorn.git-worktree"]
        initial = adapter.inspect(definition, context)
        assert initial.status == "absent", initial.error
        assert not adapter.start(definition, context).success
        assert adapter.stop(definition, context).success
        assert not adapter.create(
            definition, replace(context, liveness_fd=None)
        ).success
        assert not target.exists()
        for _ in range(2):
            assert adapter.create(definition, context).success
        assert git(target, "rev-parse", "HEAD") == commit
        assert git(target, "branch", "--show-current") == "workspace"
        observed = adapter.inspect(definition, context)
        assert observed.status == "ready"
        assert observed.ownership_token == definition.ownership_token
        git(target, "checkout", "-b", "user-branch")
        git(target, "commit", "--allow-empty", "-m", "User commit")
        user_commit = git(target, "rev-parse", "HEAD")
        assert user_commit != commit
        (target / "tracked.txt").write_text("user changes\n")
        foreign = replace(definition, ownership_token="c" * 64)
        assert adapter.inspect(foreign, context).status == "unknown"
        for action in (adapter.create, adapter.start, adapter.stop, adapter.delete):
            assert not action(foreign, context).success
        assert adapter.start(definition, context).success
        assert adapter.stop(definition, context).success
        assert git(target, "rev-parse", "HEAD") == user_commit
        assert git(target, "branch", "--show-current") == "user-branch"
        assert (target / "tracked.txt").read_text() == "user changes\n"
        assert adapter.inspect(definition, context).status == "ready"
        assert not adapter.delete(definition, context).success
        assert target.exists()
        assert adapter.delete(definition, replace(context, force=True)).success
        observed = adapter.inspect(definition, context)
        assert observed.status == "absent"
        assert observed.ownership_token is None
        assert not target.exists()
        assert adapter.delete(definition, context).success
        assert git(repository, "rev-parse", "workspace") == commit
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize(
    "output",
    [
        "null",
        "[]",
        "{}",
        "garbage",
        '{"schema_version":true,"status":"ready","locator":"app-id","ownership_token":"owner"}',
        '{"schema_version":2,"status":"ready","locator":"app-id","ownership_token":"owner"}',
        '{"schema_version":1,"status":"bad","locator":"app-id","ownership_token":"owner"}',
        '{"schema_version":1,"status":"ready","locator":"other","ownership_token":"owner"}',
        '{"schema_version":1,"status":"ready","locator":"app-id","ownership_token":"other"}',
        '{"schema_version":1,"status":"absent","locator":"app-id","ownership_token":"owner"}',
        '{"schema_version":1,"status":"unknown","status":"ready","locator":"app-id","ownership_token":"owner"}',
    ],
)
def test_probe_rejects_ambiguous_or_unowned_evidence(
    tmp_path: Path, output: str
) -> None:
    definition = _command_definition({"inspect": ["printf", "%s", output]})
    observation = CommandAdapter().inspect(definition, _consented_context(tmp_path))
    assert observation.status == "unknown"
    assert observation.error


def test_command_probe_requires_consent_and_exact_ownership(tmp_path: Path) -> None:
    definition = ResourceDefinition(
        "app",
        "service",
        "fangorn.command",
        1,
        {
            "inspect": [
                "printf",
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "ready",
                        "locator": "app-id",
                        "ownership_token": "owner",
                    }
                ),
            ],
        },
        None,
        "app-id",
        "owner",
    )
    store = ConsentStore(tmp_path / "consent")
    context = AdapterContext("ws", None, tmp_path, "a" * 64, store, {})
    adapter = CommandAdapter()
    assert adapter.inspect(definition, context).status == "unknown"
    store.grant("a" * 64)
    assert adapter.inspect(definition, context).status == "ready"
    store.revoke("a" * 64)
    assert adapter.inspect(definition, context).status == "unknown"


@pytest.mark.parametrize(
    ("adapter_id", "major", "error"),
    [
        ("acme.sample", 1, None),
        ("fangorn.command", 1, "Duplicate"),
        ("acme.sample", 2, "Incompatible"),
    ],
)
def test_installed_entry_point_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_id: str,
    major: int,
    error: str | None,
) -> None:
    module = f"f4_adapter_{major}_{adapter_id.replace('.', '_')}"
    (tmp_path / f"{module}.py").write_text(
        "from fangorn.resource_adapters import CommandAdapter, AdapterDescriptor\n"
        "class Adapter(CommandAdapter):\n"
        f"    descriptor = AdapterDescriptor({adapter_id!r}, {major}, "
        "frozenset({'service'}))\n"
    )
    distribution = tmp_path / f"{module}-1.0.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text(f"Name: {module}\nVersion: 1.0\n")
    (distribution / "entry_points.txt").write_text(
        f"[fangorn.resource_adapters]\nsample = {module}:Adapter\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    if error:
        with pytest.raises(ValueError, match=error):
            discover_adapters()
    else:
        assert discover_adapters()[adapter_id].descriptor.kinds == frozenset(
            {"service"}
        )


@pytest.mark.parametrize(
    "descriptor",
    [
        'AdapterDescriptor(123, 1, frozenset({"service"}))',
        'AdapterDescriptor("acme.bad", True, frozenset({"service"}))',
        'AdapterDescriptor("acme.bad", 1, ["service"])',
        'AdapterDescriptor("acme.bad", 1, frozenset())',
        'AdapterDescriptor("acme.bad", 1, frozenset({"invalid"}))',
        'AdapterDescriptor("acme.bad", 1, frozenset({"service"}), "force")',
        'AdapterDescriptor("acme.bad", 1, frozenset({"service"}), frozenset({1}))',
    ],
)
def test_discovery_rejects_malformed_runtime_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, descriptor: str
) -> None:
    module = "adapter_" + uuid4().hex
    (tmp_path / f"{module}.py").write_text(
        "from fangorn.resource_adapters import CommandAdapter, AdapterDescriptor\n"
        f"class Adapter(CommandAdapter):\n    descriptor = {descriptor}\n"
    )
    distribution = tmp_path / f"{module}-1.0.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text(f"Name: {module}\nVersion: 1.0\n")
    (distribution / "entry_points.txt").write_text(
        f"[fangorn.resource_adapters]\nsample = {module}:Adapter\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ValueError):
        discover_adapters()


@pytest.mark.parametrize("timeout", [False, True])
def test_probe_reaps_term_ignoring_descendants(tmp_path: Path, timeout: bool) -> None:
    pidfile = tmp_path / "child.pid"
    body = (
        "import os,signal,time,json\n"
        "pid=os.fork()\n"
        "if pid==0:\n"
        " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        f" open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
        " while True: time.sleep(.01)\n"
        f"while not os.path.exists({str(pidfile)!r}): time.sleep(.01)\n"
        + (
            "time.sleep(60)\n"
            if timeout
            else "print(json.dumps({'schema_version':1,'status':'ready',"
            "'locator':'app-id','ownership_token':'owner'}))\n"
        )
    )
    definition = _command_definition(
        {"inspect": [sys.executable, "-c", body], "timeout": 1 if timeout else 10}
    )
    started = time.monotonic()
    observation = CommandAdapter().inspect(definition, _consented_context(tmp_path))
    assert time.monotonic() - started < 12
    assert observation.status == ("unknown" if timeout else "ready")
    pid = int(pidfile.read_text())
    try:
        result = subprocess.run(  # noqa: S603 -- fixed ps argv and test-owned numeric PID
            ["/bin/ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert not result.stdout.strip() or result.stdout.strip().startswith("Z")
    finally:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def test_command_preserves_literal_argv_cwd_and_environment_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    literal = "spaces ; $(touch surprise) *"
    monkeypatch.setenv("F4_REFERENCE", "runtime-value")
    script = (
        "import json,os,sys\n"
        f"assert os.getcwd()=={str(tmp_path)!r}\n"
        f"assert sys.argv[1]=={literal!r}\n"
        "assert os.environ['F4_REFERENCE']=='runtime-value'\n"
        "print(json.dumps({'schema_version':1,'status':'ready','locator':os.environ['FANGORN_RESOURCE_LOCATOR'],'ownership_token':os.environ['FANGORN_OWNERSHIP_TOKEN']}))\n"
    )
    definition = _command_definition(
        {
            "inspect": [sys.executable, "-c", script, literal],
            "environment": ["F4_REFERENCE"],
        }
    )
    assert (
        CommandAdapter().inspect(definition, _consented_context(tmp_path)).status
        == "ready"
    )
    assert not (tmp_path / "surprise").exists()
