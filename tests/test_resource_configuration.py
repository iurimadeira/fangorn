from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from fangorn.configuration import (
    ConsentStore,
    parse_configuration,
    snapshot_configuration,
)
from fangorn.git import GitError
from fangorn.registry import RegistryError


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "2000-01-01", "12:30:00"])
def test_configuration_requires_finite_json_values(value: str) -> None:
    content = (
        f'schema_version=1\n[services.app]\nadapter="acme.demo"\nvalue={value}\n'
    ).encode()
    with pytest.raises(ValueError, match="JSON"):
        parse_configuration(content)


@pytest.mark.parametrize("scripts", ['"file"', "[1]", '[""]', '["\\u0000"]'])
def test_all_adapters_require_valid_direct_script_names(
    tmp_path: Path, scripts: str
) -> None:
    content = (
        f'schema_version=1\n[services.app]\nadapter="acme.demo"\nscripts={scripts}\n'
    ).encode()
    with pytest.raises(ValueError, match="script"):
        snapshot_configuration(content, tmp_path)


@pytest.mark.parametrize(
    "content",
    [
        b"\xff",
        b"[invalid",
        b"",
        b"schema_version=true",
        b"schema_version=2",
        b"schema_version=1\nother=1",
        b"schema_version=1\nservices=1",
        b'schema_version=1\n[services.worktree]\nadapter="acme.test"',
        b'schema_version=1\n[services."bad name"]\nadapter="acme.test"',
        b"schema_version=1\n[services.app]\nvalue=1",
        b'schema_version=1\n[services.app]\nadapter="acme.test"\nadapter_api_major=2',
        b'schema_version=1\n[services.app]\nadapter="acme.test"\nexternal_reference=1',
    ],
)
def test_invalid_configuration_fails_before_snapshot_effects(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_configuration(content)


@pytest.mark.parametrize(
    "setting",
    [
        "inspect=[]",
        'inspect="true"',
        "inspect=[1]",
        'inspect=[""]',
        "timeout=0",
        "timeout=3601",
        "timeout=true",
        "cwd=1",
        'cwd=""',
        'environment="SECRET"',
        "environment=[1]",
        'environment=["SECRET=value"]',
        "unknown=1",
    ],
)
def test_invalid_command_settings_fail_validation(setting: str) -> None:
    commands = {
        "inspect": '["true"]',
        "start": '["true"]',
        "stop": '["true"]',
        "delete": '["true"]',
    }
    key, value = setting.split("=", 1)
    commands[key] = value
    content = (
        'schema_version=1\n[services.app]\nadapter="fangorn.command"\n'
        + "\n".join(f"{name}={value}" for name, value in commands.items())
    ).encode()
    with pytest.raises(ValueError):
        parse_configuration(content)


@pytest.mark.parametrize("kind", ["directory", "missing", "large", "special"])
def test_direct_scripts_require_bounded_regular_files(
    tmp_path: Path, kind: str
) -> None:
    script = tmp_path / "script"
    if kind == "directory":
        script.mkdir()
    elif kind == "large":
        script.write_bytes(b"a" * (1024 * 1024 + 1))
    elif kind == "special":
        script.write_text("exit 0")
        script.chmod(0o4700)
    content = (
        b'schema_version=1\n[services.app]\nadapter="acme.demo"\nscripts=["script"]'
    )
    with pytest.raises((ValueError, GitError)):
        snapshot_configuration(content, tmp_path)


def test_snapshot_covers_script_bytes_and_mode_and_revocation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    script = source / "probe"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o700)
    content = (
        b'schema_version = 1\n[services.app]\nadapter="fangorn.command"\n'
        b'inspect=["./probe"]\nstart=["true"]\nstop=["true"]\ndelete=["true"]\n'
    )
    first = snapshot_configuration(content, source)
    script.write_text("#!/bin/sh\nexit 1\n")
    second = snapshot_configuration(content, source)
    assert first.digest != second.digest
    paths = first.materialize(tmp_path / "snapshots")
    assert paths["./probe"].read_text() == "#!/bin/sh\nexit 0\n"
    store = ConsentStore(tmp_path / "state")
    assert store.list() == ()
    assert not (tmp_path / "state").exists()
    store.grant(first.digest)
    store.require(first.digest)
    with pytest.raises(ValueError, match="consent grant"):
        store.require(second.digest)
    store.revoke(first.digest)
    with pytest.raises(ValueError, match="consent grant"):
        store.require(first.digest)


def test_configuration_snapshot_is_deeply_immutable_and_roundtrips(
    tmp_path: Path,
) -> None:
    snapshot = snapshot_configuration(
        b'schema_version=1\n[services.app]\nadapter="acme.demo"\nvalues=["one"]\n',
        tmp_path,
    )
    services = snapshot.value["services"]
    assert isinstance(services, Mapping)
    service = services["app"]
    assert isinstance(service, Mapping)
    assert service["values"] == ("one",)
    with pytest.raises(TypeError):
        services["other"] = {}  # type: ignore[index]
    assert snapshot.from_record(snapshot.serialize()) == snapshot
    empty = snapshot_configuration(None, tmp_path)
    assert (
        empty.digest
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert empty.content == b""


def test_scripts_reject_symlinks_and_source_escape(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.write_text("exit 0")
    (tmp_path / "link").symlink_to(target)
    for name in ("./link", "../real"):
        content = (
            f'schema_version=1\n[services.app]\nadapter="acme.demo"\n'
            f'scripts=["{name}"]\n'
        ).encode()
        with pytest.raises((ValueError, GitError)):
            snapshot_configuration(content, tmp_path)


def test_consent_rejects_symlink_ancestors_and_does_not_truncate_hardlinks(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    with pytest.raises(RegistryError):
        ConsentStore(tmp_path / "link" / "consent").grant("a" * 64)
    original = tmp_path / "original"
    original.write_text("preserve")
    (real / ("a" * 64)).hardlink_to(original)
    store = ConsentStore(real)
    store.grant("a" * 64)
    assert original.read_text() == "preserve"
    store.require("a" * 64)


def test_script_mode_reader_and_snapshot_read_purity(tmp_path: Path) -> None:
    content = (
        b'schema_version=1\n[services.app]\nadapter="acme.demo"\nscripts=["./probe"]\n'
    )
    first = snapshot_configuration(
        content, tmp_path, script_reader=lambda name: (b"pinned bytes", 0o700)
    )
    second = snapshot_configuration(
        content, tmp_path, script_reader=lambda name: (b"pinned bytes", 0o600)
    )
    assert first.digest != second.digest
    root = tmp_path / "snapshots"
    with pytest.raises(ValueError, match="unavailable"):
        first.paths(root)
    assert not root.exists()
    with ThreadPoolExecutor(max_workers=3) as executor:
        paths = list(executor.map(first.materialize, [root] * 3))
    assert paths[0] == paths[1] == paths[2]
    script = paths[0]["./probe"]
    before = (script.stat().st_mtime_ns, script.parent.stat().st_mtime_ns)
    assert first.paths(root)["./probe"].read_bytes() == b"pinned bytes"
    assert before == (script.stat().st_mtime_ns, script.parent.stat().st_mtime_ns)
    script.chmod(0o600)
    with pytest.raises(ValueError, match="differs"):
        first.paths(root)


@pytest.mark.parametrize("mode", [-1, 0o4700, 0o100700, True])
def test_pinned_script_reader_rejects_invalid_modes(tmp_path: Path, mode: int) -> None:
    content = (
        b'schema_version=1\n[services.app]\nadapter="acme.demo"\nscripts=["probe"]\n'
    )
    with pytest.raises(ValueError, match="mode"):
        snapshot_configuration(
            content, tmp_path, script_reader=lambda name: (b"bytes", mode)
        )


@pytest.mark.parametrize("kind", ["invalid", "symlink", "hardlink", "permissions"])
def test_consent_does_not_trust_unsafe_grant_files(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "consent"
    root.mkdir(mode=0o700)
    grant = root / ("a" * 64)
    original = tmp_path / "original"
    original.write_text("a" * 64 + "\n")
    original.chmod(0o600)
    if kind == "symlink":
        grant.symlink_to(original)
    elif kind == "hardlink":
        grant.hardlink_to(original)
    else:
        grant.write_text("a" * 64 + "\n" if kind == "permissions" else "invalid")
        grant.chmod(0o644 if kind == "permissions" else 0o600)
    store = ConsentStore(root)
    assert store.list() == ()
    with pytest.raises(ValueError, match="consent grant"):
        store.require("a" * 64)
    store.revoke("a" * 64)
    store.revoke("a" * 64)
    assert original.read_text() == "a" * 64 + "\n"
