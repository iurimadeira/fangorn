from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from fangorn.git import GitError, WorktreeObservation, observe_worktree

BUSY_TIMEOUT_SECONDS = 2.0
ADOPTION_TIMEOUT_SECONDS = 5.0
ADOPTION_RETRY_DELAY_SECONDS = 0.01
READ_INITIALIZATION_TIMEOUT_SECONDS = 1.0
READ_INITIALIZATION_RETRY_DELAY_SECONDS = 0.01
SCHEMA_VERSION = 2


class RegistryError(RuntimeError):
    """Registry operation failed without weakening Workspace invariants."""


class CreateAlreadyCompleted(RegistryError):
    """Equivalent create completed before this invocation acquired its lease."""


@dataclass(frozen=True)
class WorkspaceRecord:
    id: str
    repository_id: str
    repository_common_dir: str
    git_common_dir_generation: str
    git_dir: str
    git_dir_generation: str
    path: str
    branch: str | None
    head: str | None
    adopted_head: str | None
    created_at: str
    last_observed_at: str
    last_observation_token: int

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "repository_id": self.repository_id,
            "repository_common_dir": self.repository_common_dir,
            "git_common_dir_generation": self.git_common_dir_generation,
            "git_dir": self.git_dir,
            "git_dir_generation": self.git_dir_generation,
            "path": self.path,
            "branch": self.branch,
            "head": self.head,
            "adopted_head": self.adopted_head,
            "created_at": self.created_at,
            "last_observed_at": self.last_observed_at,
        }


@dataclass(frozen=True)
class ProcessIdentity:
    process_instance_id: str
    boot_identity: str
    pid: int
    process_start_identity: str


@dataclass(frozen=True)
class CreateIntentRecord:
    operation_id: str
    workspace_id: str
    request_key: str
    request_json: str
    request_id: str | None
    target_path: str
    resolved_sha: str | None
    resolved_json: str | None
    status: str


@dataclass(frozen=True)
class LeaseRecord:
    scope_kind: str
    scope_key: str
    operation_id: str
    owner: ProcessIdentity
    epoch: int
    aggregate_version: int
    active: bool


MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE repositories (
                id TEXT PRIMARY KEY NOT NULL,
                git_common_dir TEXT NOT NULL UNIQUE,
                git_common_dir_generation TEXT NOT NULL CHECK (
                    length(git_common_dir_generation) = 64
                    AND git_common_dir_generation NOT GLOB '*[^0-9a-f]*'
                ),
                created_observation_token INTEGER NOT NULL CHECK (
                    created_observation_token > 0
                ),
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE observation_clock (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                current_token INTEGER NOT NULL CHECK (current_token >= 0)
            )
            """,
            """
            INSERT INTO observation_clock (singleton, current_token)
            VALUES (1, 0)
            """,
            """
            CREATE TABLE workspaces (
                id TEXT PRIMARY KEY NOT NULL,
                repository_id TEXT NOT NULL REFERENCES repositories(id),
                git_dir TEXT NOT NULL UNIQUE,
                git_dir_generation TEXT NOT NULL CHECK (
                    length(git_dir_generation) = 64
                    AND git_dir_generation NOT GLOB '*[^0-9a-f]*'
                ),
                path TEXT NOT NULL,
                branch TEXT,
                head TEXT,
                adopted_head TEXT,
                created_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                last_observation_token INTEGER NOT NULL CHECK (
                    last_observation_token > 0
                )
            )
            """,
            """
            CREATE TRIGGER repositories_immutable_identity
            BEFORE UPDATE OF id, git_common_dir, git_common_dir_generation,
                created_observation_token
            ON repositories
            FOR EACH ROW
            WHEN NEW.id IS NOT OLD.id
                OR NEW.git_common_dir IS NOT OLD.git_common_dir
                OR NEW.git_common_dir_generation
                    IS NOT OLD.git_common_dir_generation
                OR NEW.created_observation_token
                    IS NOT OLD.created_observation_token
            BEGIN
                SELECT RAISE(ABORT, 'repository identity is immutable');
            END
            """,
            """
            CREATE TRIGGER workspaces_immutable_binding
            BEFORE UPDATE OF id, repository_id, git_dir, git_dir_generation
            ON workspaces
            FOR EACH ROW
            WHEN NEW.id IS NOT OLD.id
                OR NEW.repository_id IS NOT OLD.repository_id
                OR NEW.git_dir IS NOT OLD.git_dir
                OR NEW.git_dir_generation IS NOT OLD.git_dir_generation
            BEGIN
                SELECT RAISE(ABORT, 'workspace binding is immutable');
            END
            """,
        ),
    ),
    (
        2,
        (
            """
            ALTER TABLE workspaces
            ADD COLUMN parent_id TEXT REFERENCES workspaces(id)
            """,
            "ALTER TABLE workspaces ADD COLUMN created_from_sha TEXT",
            "ALTER TABLE workspaces ADD COLUMN configuration BLOB",
            "ALTER TABLE workspaces ADD COLUMN configuration_json TEXT",
            "ALTER TABLE workspaces ADD COLUMN configuration_digest TEXT",
            "ALTER TABLE workspaces ADD COLUMN lifecycle_state TEXT",
            """
            ALTER TABLE workspaces
            ADD COLUMN aggregate_version INTEGER NOT NULL DEFAULT 0
            """,
            "ALTER TABLE workspaces ADD COLUMN origin TEXT",
            "ALTER TABLE workspaces ADD COLUMN completed_operation_id TEXT",
            """
            CREATE TABLE workspace_create_intents (
                operation_id TEXT PRIMARY KEY NOT NULL,
                workspace_id TEXT NOT NULL UNIQUE,
                request_key TEXT NOT NULL UNIQUE,
                request_id TEXT UNIQUE,
                request_json TEXT NOT NULL,
                target_path TEXT NOT NULL UNIQUE,
                resolved_sha TEXT,
                resolved_json TEXT,
                aggregate_version INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE operations (
                id TEXT PRIMARY KEY NOT NULL,
                workspace_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE operation_steps (
                operation_id TEXT NOT NULL REFERENCES operations(id),
                position INTEGER NOT NULL,
                action TEXT NOT NULL,
                resource_name TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                PRIMARY KEY (operation_id, position)
            )
            """,
            """
            CREATE TABLE workspace_resources (
                workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                position INTEGER NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                adapter_api_major INTEGER NOT NULL,
                configuration_json TEXT NOT NULL,
                external_reference TEXT,
                locator TEXT NOT NULL UNIQUE,
                ownership_token TEXT NOT NULL UNIQUE,
                provisioning_status TEXT NOT NULL,
                PRIMARY KEY (workspace_id, name),
                UNIQUE (workspace_id, position)
            )
            """,
            """
            CREATE TABLE repository_cache_entries (
                normalized_source TEXT PRIMARY KEY NOT NULL,
                path TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                repository_generation TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE mutation_leases (
                scope_kind TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                process_instance_id TEXT NOT NULL,
                boot_identity TEXT NOT NULL,
                pid INTEGER NOT NULL,
                process_start_identity TEXT NOT NULL,
                epoch INTEGER NOT NULL CHECK (epoch > 0),
                aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
                active INTEGER NOT NULL CHECK (active IN (0, 1)),
                PRIMARY KEY (scope_kind, scope_key)
            )
            """,
            """
            CREATE TABLE workspace_aggregates (
                workspace_id TEXT PRIMARY KEY NOT NULL
                    REFERENCES workspace_create_intents(workspace_id),
                definition_json TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL,
                aggregate_version INTEGER NOT NULL DEFAULT 0,
                completed_operation_id TEXT REFERENCES operations(id)
            )
            """,
            """
            CREATE TRIGGER workspace_aggregate_definition_immutable
            BEFORE UPDATE OF parent_id, repository_id, created_from_sha,
                configuration, configuration_json, configuration_digest, origin
            ON workspaces
            FOR EACH ROW
            WHEN NEW.parent_id IS NOT OLD.parent_id
                OR NEW.repository_id IS NOT OLD.repository_id
                OR NEW.created_from_sha IS NOT OLD.created_from_sha
                OR NEW.configuration IS NOT OLD.configuration
                OR NEW.configuration_json IS NOT OLD.configuration_json
                OR NEW.configuration_digest IS NOT OLD.configuration_digest
                OR NEW.origin IS NOT OLD.origin
            BEGIN
                SELECT RAISE(ABORT, 'workspace definition is immutable');
            END
            """,
            """
            CREATE TRIGGER workspace_resource_definition_immutable
            BEFORE UPDATE OF workspace_id, position, name, kind, adapter_id,
                adapter_api_major, configuration_json, external_reference,
                locator, ownership_token
            ON workspace_resources
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'workspace resource definition is immutable');
            END
            """,
            """
            CREATE TRIGGER workspace_resource_membership_immutable
            BEFORE DELETE ON workspace_resources
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'workspace resource membership is immutable');
            END
            """,
            """
            CREATE TRIGGER workspace_resource_membership_sealed
            BEFORE INSERT ON workspace_resources
            FOR EACH ROW
            WHEN EXISTS (
                SELECT 1 FROM workspace_aggregates
                WHERE workspace_id = NEW.workspace_id
                    AND completed_operation_id IS NOT NULL
            )
            BEGIN
                SELECT RAISE(ABORT, 'workspace resource membership is immutable');
            END
            """,
            """
            CREATE TRIGGER workspace_create_requires_one_worktree_on_insert
            BEFORE INSERT ON operations
            FOR EACH ROW
            WHEN NEW.kind = 'create' AND NEW.status = 'completed'
                AND (
                    SELECT COUNT(*) FROM workspace_resources
                    WHERE workspace_id = NEW.workspace_id AND kind = 'worktree'
                ) != 1
            BEGIN
                SELECT RAISE(ABORT, 'created workspace requires one worktree');
            END
            """,
            """
            CREATE TRIGGER workspace_create_requires_one_worktree
            BEFORE UPDATE OF status, kind, workspace_id ON operations
            FOR EACH ROW
            WHEN NEW.kind = 'create' AND NEW.status = 'completed'
                AND (
                    SELECT COUNT(*) FROM workspace_resources
                    WHERE workspace_id = NEW.workspace_id AND kind = 'worktree'
                ) != 1
            BEGIN
                SELECT RAISE(ABORT, 'created workspace requires one worktree');
            END
            """,
            """
            CREATE TRIGGER workspace_create_intent_identity_immutable
            BEFORE UPDATE OF operation_id, workspace_id, request_key, request_id,
                request_json, target_path
            ON workspace_create_intents
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'workspace create intent is immutable');
            END
            """,
            """
            CREATE TRIGGER workspace_create_intent_resolution_immutable
            BEFORE UPDATE OF resolved_json ON workspace_create_intents
            FOR EACH ROW
            WHEN OLD.resolved_json IS NOT NULL
                AND NEW.resolved_json IS NOT OLD.resolved_json
            BEGIN
                SELECT RAISE(ABORT, 'workspace create intent is immutable');
            END
            """,
            """
            CREATE TRIGGER workspace_create_intent_sha_immutable
            BEFORE UPDATE OF resolved_sha ON workspace_create_intents
            FOR EACH ROW
            WHEN OLD.resolved_sha IS NOT NULL
                AND NEW.resolved_sha IS NOT OLD.resolved_sha
            BEGIN
                SELECT RAISE(ABORT, 'workspace create intent is immutable');
            END
            """,
            """
            CREATE TRIGGER workspace_aggregate_definition_json_immutable
            BEFORE UPDATE OF workspace_id, definition_json ON workspace_aggregates
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'workspace definition is immutable');
            END
            """,
            """
            CREATE TRIGGER workspace_aggregate_completion_immutable
            BEFORE UPDATE OF completed_operation_id ON workspace_aggregates
            FOR EACH ROW
            WHEN OLD.completed_operation_id IS NOT NULL
                AND NEW.completed_operation_id IS NOT OLD.completed_operation_id
            BEGIN
                SELECT RAISE(ABORT, 'workspace completion is immutable');
            END
            """,
            """
            CREATE TRIGGER workspace_aggregate_completion_valid
            BEFORE UPDATE OF completed_operation_id ON workspace_aggregates
            FOR EACH ROW
            WHEN NEW.completed_operation_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM operations
                WHERE id = NEW.completed_operation_id
                    AND workspace_id = NEW.workspace_id
                    AND kind = 'create' AND status = 'completed'
            )
            BEGIN
                SELECT RAISE(ABORT, 'workspace completion is invalid');
            END
            """,
            """
            CREATE TRIGGER workspace_completion_immutable
            BEFORE UPDATE OF completed_operation_id ON workspaces
            FOR EACH ROW
            WHEN OLD.completed_operation_id IS NOT NULL
                AND NEW.completed_operation_id IS NOT OLD.completed_operation_id
            BEGIN
                SELECT RAISE(ABORT, 'workspace completion is immutable');
            END
            """,
            """
            CREATE TRIGGER workspace_completion_valid
            BEFORE UPDATE OF completed_operation_id ON workspaces
            FOR EACH ROW
            WHEN NEW.completed_operation_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM operations
                WHERE id = NEW.completed_operation_id
                    AND workspace_id = NEW.id
                    AND kind = 'create' AND status = 'completed'
            )
            BEGIN
                SELECT RAISE(ABORT, 'workspace completion is invalid');
            END
            """,
            """
            CREATE TRIGGER completed_create_operation_immutable
            BEFORE UPDATE OF workspace_id, kind, status ON operations
            FOR EACH ROW
            WHEN EXISTS (
                SELECT 1 FROM workspace_aggregates
                WHERE completed_operation_id = OLD.id
            )
            BEGIN
                SELECT RAISE(ABORT, 'workspace completion is immutable');
            END
            """,
            """
            CREATE TRIGGER completed_create_operation_delete_immutable
            BEFORE DELETE ON operations
            FOR EACH ROW
            WHEN EXISTS (
                SELECT 1 FROM workspace_aggregates
                WHERE completed_operation_id = OLD.id
            )
            BEGIN
                SELECT RAISE(ABORT, 'workspace completion is immutable');
            END
            """,
        ),
    ),
)


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def from_environment(cls) -> Registry:
        state_home = os.environ.get("XDG_STATE_HOME")
        if state_home and Path(state_home).is_absolute():
            root = Path(state_home)
        else:
            home_value = os.environ.get("HOME")
            if not home_value:
                raise RegistryError(
                    "Registry state directory unavailable: HOME is unset"
                )
            home = Path(home_value)
            if not home.is_absolute():
                raise RegistryError("HOME must be an absolute path")
            root = home / ".local" / "state"
        return cls(root / "fangorn" / "registry.sqlite3")

    def adopt(
        self,
        observation: WorktreeObservation,
        *,
        reobserve: Callable[[Callable[[], int]], WorktreeObservation] | None = None,
    ) -> tuple[WorkspaceRecord, bool]:
        deadline = time.monotonic() + ADOPTION_TIMEOUT_SECONDS
        last_busy_cause: sqlite3.Error | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return self._adopt_once(
                    observation,
                    reobserve=reobserve,
                )
            except RegistryError as error:
                busy_cause = _sqlite_contention_cause(error)
                if busy_cause is None:
                    raise
                last_busy_cause = busy_cause

            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(ADOPTION_RETRY_DELAY_SECONDS, remaining))

        raise RegistryError(
            "Registry remained busy for "
            f"{ADOPTION_TIMEOUT_SECONDS:g} seconds during adoption"
        ) from last_busy_cause

    def _adopt_once(
        self,
        observation: WorktreeObservation,
        *,
        reobserve: Callable[[Callable[[], int]], WorktreeObservation] | None,
    ) -> tuple[WorkspaceRecord, bool]:
        requested_path = observation.path
        with self._connection(timeout=0.0) as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                reserved_token: int | None = None

                def reserve_observation() -> int:
                    nonlocal reserved_token
                    reserved_token = _reserve_observation(connection)
                    return reserved_token

                observation = (
                    reobserve(reserve_observation)
                    if reobserve is not None
                    else observe_worktree(
                        requested_path,
                        reserve_observation=reserve_observation,
                    )
                )
                observation_token = _observation_token(observation)
                if observation_token != reserved_token:
                    raise RegistryError(
                        "Final Git observation token was not reserved by the "
                        "adoption transaction"
                    )
                if observation.git_common_dir_generation is None:
                    raise RegistryError(
                        "Fangorn repository generation marker is missing; "
                        "Repository identity drifted"
                    )
                if observation.git_dir_generation is None:
                    raise RegistryError(
                        "Fangorn worktree generation marker is missing; "
                        "Workspace identity drifted"
                    )
                created_at = _timestamp()
                repository = connection.execute(
                    """
                    SELECT id, git_common_dir_generation
                    FROM repositories WHERE git_common_dir = ?
                    """,
                    (str(observation.repository_common_dir),),
                ).fetchone()
                if repository is None:
                    repository_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO repositories (
                            id, git_common_dir, git_common_dir_generation,
                            created_observation_token, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            repository_id,
                            str(observation.repository_common_dir),
                            observation.git_common_dir_generation,
                            observation_token,
                            created_at,
                        ),
                    )
                else:
                    _validate_repository_binding(repository, observation)
                    repository_id = str(repository["id"])

                workspace = connection.execute(
                    "SELECT * FROM workspaces WHERE git_dir = ?",
                    (str(observation.git_dir),),
                ).fetchone()
                created = workspace is None
                if workspace is None:
                    workspace_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO workspaces (
                            id, repository_id, git_dir, git_dir_generation,
                            path, branch, head, adopted_head, created_at,
                            last_observed_at, last_observation_token
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            workspace_id,
                            repository_id,
                            str(observation.git_dir),
                            observation.git_dir_generation,
                            str(observation.path),
                            observation.branch,
                            observation.head,
                            observation.head,
                            created_at,
                            observation.observed_at,
                            observation_token,
                        ),
                    )
                else:
                    _validate_worktree_binding(
                        workspace,
                        repository_id=repository_id,
                        observation=observation,
                    )
                    workspace_id = str(workspace["id"])
                    connection.execute(
                        """
                        UPDATE workspaces
                        SET path = ?, branch = ?, head = ?, last_observed_at = ?,
                            last_observation_token = ?
                        WHERE id = ? AND last_observation_token < ?
                        """,
                        (
                            str(observation.path),
                            observation.branch,
                            observation.head,
                            observation.observed_at,
                            observation_token,
                            workspace_id,
                            observation_token,
                        ),
                    )
                row = connection.execute(
                    """
                    SELECT workspaces.*, repositories.git_common_dir,
                        repositories.git_common_dir_generation
                    FROM workspaces
                    JOIN repositories
                        ON repositories.id = workspaces.repository_id
                    WHERE workspaces.id = ?
                    """,
                    (workspace_id,),
                ).fetchone()
                if row is None:
                    raise RegistryError(
                        "Adopted Workspace disappeared from the registry"
                    )
                record = _workspace_from_row(row)
                connection.commit()
                return record, created
            except (sqlite3.Error, GitError, RegistryError) as error:
                connection.rollback()
                if isinstance(error, (GitError, RegistryError)):
                    raise
                raise _registry_error(error) from error

    def reserve_observation(self) -> int:
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                token = _reserve_observation(connection)
                connection.commit()
                return token
            except (sqlite3.Error, RegistryError) as error:
                connection.rollback()
                if isinstance(error, RegistryError):
                    raise
                raise _registry_error(error) from error

    def marker_creation_requirements(
        self,
        observation: WorktreeObservation,
        *,
        markerless_reobserved: bool = False,
    ) -> tuple[bool, bool] | None:
        _observation_token(observation)
        with self._connection() as connection:
            self._migrate(connection)
            try:
                repository = connection.execute(
                    """
                    SELECT id, git_common_dir_generation
                    FROM repositories WHERE git_common_dir = ?
                    """,
                    (str(observation.repository_common_dir),),
                ).fetchone()
                workspace = connection.execute(
                    "SELECT * FROM workspaces WHERE git_dir = ?",
                    (str(observation.git_dir),),
                ).fetchone()
            except sqlite3.Error as error:
                raise _registry_error(error) from error
            if repository is not None:
                if (
                    observation.git_common_dir_generation is None
                    and not markerless_reobserved
                ):
                    return None
                _validate_repository_binding(repository, observation)
            if workspace is not None:
                if observation.git_dir_generation is None and not markerless_reobserved:
                    return None
                repository_id = (
                    str(repository["id"])
                    if repository is not None
                    else str(workspace["repository_id"])
                )
                _validate_worktree_binding(
                    workspace,
                    repository_id=repository_id,
                    observation=observation,
                )
            return (
                repository is None and observation.git_common_dir_generation is None,
                workspace is None and observation.git_dir_generation is None,
            )

    def begin_create_intent(
        self,
        *,
        request_key: str,
        request_id: str | None,
        request_json: str,
        target_path: str,
        workspace_id: str,
        operation_id: str,
        prepare_cache: bool,
    ) -> tuple[CreateIntentRecord, bool]:
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = None
                if request_id is not None:
                    row = connection.execute(
                        "SELECT * FROM workspace_create_intents WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                    if row is not None and str(row["request_json"]) != request_json:
                        raise RegistryError(
                            "Create idempotency key belongs to a different request"
                        )
                if row is None:
                    row = connection.execute(
                        "SELECT * FROM workspace_create_intents WHERE request_key = ?",
                        (request_key,),
                    ).fetchone()
                if row is not None:
                    connection.commit()
                    return _create_intent_from_row(row), False
                collision = connection.execute(
                    "SELECT request_key FROM workspace_create_intents "
                    "WHERE target_path = ?",
                    (target_path,),
                ).fetchone()
                if collision is not None:
                    raise RegistryError(
                        "Target path belongs to a different Workspace create request"
                    )
                now = _timestamp()
                connection.execute(
                    """
                    INSERT INTO workspace_create_intents (
                        operation_id, workspace_id, request_key, request_id,
                        request_json, target_path, resolved_sha, resolved_json, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 'preparing', ?, ?)
                    """,
                    (
                        operation_id,
                        workspace_id,
                        request_key,
                        request_id,
                        request_json,
                        target_path,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO operations (
                        id, workspace_id, kind, status, error, created_at, updated_at
                    ) VALUES (?, ?, 'create', 'running', NULL, ?, ?)
                    """,
                    (operation_id, workspace_id, now, now),
                )
                if prepare_cache:
                    connection.execute(
                        """
                        INSERT INTO operation_steps (
                            operation_id, position, action, resource_name, status
                        ) VALUES (?, 0, 'prepare', 'repository-cache', 'pending')
                        """,
                        (operation_id,),
                    )
                connection.commit()
                return CreateIntentRecord(
                    operation_id=operation_id,
                    workspace_id=workspace_id,
                    request_key=request_key,
                    request_json=request_json,
                    request_id=request_id,
                    target_path=target_path,
                    resolved_sha=None,
                    resolved_json=None,
                    status="preparing",
                ), True
            except (sqlite3.Error, RegistryError) as error:
                connection.rollback()
                if isinstance(error, RegistryError):
                    raise
                raise _registry_error(error) from error

    def persist_resolved_sha(self, operation_id: str, sha: str) -> str:
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT resolved_sha FROM workspace_create_intents "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise RegistryError("Workspace create intent is unavailable")
                if row["resolved_sha"] is not None:
                    connection.commit()
                    return str(row["resolved_sha"])
                connection.execute(
                    "UPDATE workspace_create_intents "
                    "SET resolved_sha = ?, updated_at = ? WHERE operation_id = ?",
                    (sha, _timestamp(), operation_id),
                )
                connection.commit()
                return sha
            except (sqlite3.Error, RegistryError) as error:
                connection.rollback()
                if isinstance(error, RegistryError):
                    raise
                raise _registry_error(error) from error

    def repository_id_for_common_dir(self, common_dir: str) -> str:
        with self._connection() as connection:
            self._migrate(connection)
            row = connection.execute(
                "SELECT id FROM repositories WHERE git_common_dir = ?", (common_dir,)
            ).fetchone()
            if row is not None:
                return str(row["id"])
            return str(uuid5(NAMESPACE_URL, f"fangorn-repository:{common_dir}"))

    def record_workspace_definition(
        self,
        *,
        workspace_id: str,
        definition: dict[str, object],
    ) -> None:
        encoded = json.dumps(definition, sort_keys=True, separators=(",", ":"))
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT definition_json FROM workspace_aggregates "
                    "WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO workspace_aggregates "
                        "(workspace_id, definition_json, lifecycle_state) "
                        "VALUES (?, ?, 'creating')",
                        (workspace_id, encoded),
                    )
                elif str(row["definition_json"]) != encoded:
                    raise RegistryError("Workspace definition is immutable")
                connection.commit()
            except (sqlite3.Error, RegistryError) as error:
                connection.rollback()
                if isinstance(error, RegistryError):
                    raise
                raise _registry_error(error) from error

    def enrich_create_intent(
        self,
        operation_id: str,
        *,
        resolved: dict[str, object],
        steps: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM workspace_create_intents WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise RegistryError("Workspace create intent is unavailable")
                if row["resolved_json"] is not None:
                    existing = json.loads(str(row["resolved_json"]))
                    if not isinstance(existing, dict):
                        raise RegistryError("Stored create intent is malformed")
                    connection.commit()
                    return existing
                resolved_sha = str(resolved["created_from_sha"])
                if row["resolved_sha"] is None:
                    connection.execute(
                        "UPDATE workspace_create_intents SET resolved_sha = ? "
                        "WHERE operation_id = ?",
                        (resolved_sha, operation_id),
                    )
                elif str(row["resolved_sha"]) != resolved_sha:
                    raise RegistryError(
                        "Resolved Workspace create SHA does not match its intent"
                    )
                encoded = json.dumps(resolved, sort_keys=True, separators=(",", ":"))
                now = _timestamp()
                connection.execute(
                    """
                    UPDATE workspace_create_intents
                    SET resolved_json = ?, status = 'creating', updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (encoded, now, operation_id),
                )
                start_position = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(position), -1) + 1 FROM operation_steps "
                        "WHERE operation_id = ?",
                        (operation_id,),
                    ).fetchone()[0]
                )
                connection.executemany(
                    """
                    INSERT INTO operation_steps (
                        operation_id, position, action, resource_name, status
                    ) VALUES (?, ?, ?, ?, 'pending')
                    """,
                    (
                        (operation_id, start_position + index, action, resource)
                        for index, (action, resource) in enumerate(steps)
                    ),
                )
                connection.commit()
                return resolved
            except (sqlite3.Error, RegistryError, ValueError) as error:
                connection.rollback()
                if isinstance(error, RegistryError):
                    raise
                if isinstance(error, ValueError):
                    raise RegistryError("Stored create intent is malformed") from error
                raise _registry_error(error) from error

    def acquire_lease(
        self,
        *,
        scope_kind: str,
        scope_key: str,
        operation_id: str,
        owner: ProcessIdentity,
        owner_status: Callable[[ProcessIdentity], str],
    ) -> int:
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if scope_kind == "workspace":
                    terminal = connection.execute(
                        "SELECT status FROM workspace_create_intents "
                        "WHERE workspace_id = ? AND operation_id = ?",
                        (scope_key, operation_id),
                    ).fetchone()
                    if terminal is not None and terminal["status"] == "completed":
                        raise CreateAlreadyCompleted(
                            "Workspace create operation already completed"
                        )
                row = connection.execute(
                    "SELECT * FROM mutation_leases "
                    "WHERE scope_kind = ? AND scope_key = ?",
                    (scope_kind, scope_key),
                ).fetchone()
                aggregate_version = self._aggregate_version(
                    connection, scope_kind=scope_kind, scope_key=scope_key
                )
                if row is None:
                    epoch = 1
                    connection.execute(
                        """
                        INSERT INTO mutation_leases (
                            scope_kind, scope_key, operation_id,
                            process_instance_id, boot_identity, pid,
                            process_start_identity, epoch, aggregate_version, active
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            scope_kind,
                            scope_key,
                            operation_id,
                            owner.process_instance_id,
                            owner.boot_identity,
                            owner.pid,
                            owner.process_start_identity,
                            epoch,
                            aggregate_version,
                        ),
                    )
                else:
                    previous = _lease_from_row(row)
                    same_owner = previous.owner == owner
                    if previous.active and not (
                        same_owner and previous.operation_id == operation_id
                    ):
                        status = owner_status(previous.owner)
                        if status != "dead":
                            raise RegistryError(
                                f"{scope_kind.capitalize()} mutation is busy"
                            )
                        connection.execute(
                            """
                            UPDATE operation_steps SET status = 'unknown'
                            WHERE operation_id = ? AND status = 'running'
                            """,
                            (previous.operation_id,),
                        )
                    if previous.active and same_owner:
                        epoch = previous.epoch
                    else:
                        epoch = previous.epoch + 1
                        connection.execute(
                            """
                            UPDATE mutation_leases
                            SET operation_id = ?, process_instance_id = ?,
                                boot_identity = ?, pid = ?,
                                process_start_identity = ?, epoch = ?,
                                aggregate_version = ?, active = 1
                            WHERE scope_kind = ? AND scope_key = ?
                            """,
                            (
                                operation_id,
                                owner.process_instance_id,
                                owner.boot_identity,
                                owner.pid,
                                owner.process_start_identity,
                                epoch,
                                aggregate_version,
                                scope_kind,
                                scope_key,
                            ),
                        )
                if scope_kind == "workspace":
                    now = _timestamp()
                    connection.execute(
                        "UPDATE operations SET status = 'running', error = NULL, "
                        "updated_at = ? WHERE id = ?",
                        (now, operation_id),
                    )
                    connection.execute(
                        "UPDATE workspace_create_intents "
                        "SET status = 'creating', updated_at = ? "
                        "WHERE operation_id = ?",
                        (now, operation_id),
                    )
                    connection.execute(
                        "UPDATE workspace_aggregates SET lifecycle_state = 'creating' "
                        "WHERE workspace_id = ? AND completed_operation_id IS NULL",
                        (scope_key,),
                    )
                connection.commit()
                return epoch
            except (sqlite3.Error, RegistryError) as error:
                connection.rollback()
                if isinstance(error, RegistryError):
                    raise
                raise _registry_error(error) from error

    def start_operation_step(
        self,
        operation_id: str,
        *,
        position: int,
        scope_kind: str,
        scope_key: str,
        lease_epoch: int,
    ) -> str:
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_lease(
                    connection,
                    operation_id=operation_id,
                    scope_kind=scope_kind,
                    scope_key=scope_key,
                    lease_epoch=lease_epoch,
                )
                row = connection.execute(
                    "SELECT status FROM operation_steps "
                    "WHERE operation_id = ? AND position = ?",
                    (operation_id, position),
                ).fetchone()
                if row is None:
                    raise RegistryError("Workspace operation step is unavailable")
                status = str(row["status"])
                if status != "completed":
                    connection.execute(
                        "UPDATE operation_steps SET status = 'running' "
                        "WHERE operation_id = ? AND position = ?",
                        (operation_id, position),
                    )
                connection.commit()
                return status
            except (sqlite3.Error, RegistryError) as error:
                connection.rollback()
                if isinstance(error, RegistryError):
                    raise
                raise _registry_error(error) from error

    def finish_operation_step(
        self,
        operation_id: str,
        *,
        position: int,
        scope_kind: str,
        scope_key: str,
        lease_epoch: int,
        result: dict[str, object],
    ) -> None:
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_lease(
                    connection,
                    operation_id=operation_id,
                    scope_kind=scope_kind,
                    scope_key=scope_key,
                    lease_epoch=lease_epoch,
                )
                changed = connection.execute(
                    """
                    UPDATE operation_steps
                    SET status = 'completed', result_json = ?
                    WHERE operation_id = ? AND position = ?
                    """,
                    (
                        json.dumps(result, sort_keys=True, separators=(",", ":")),
                        operation_id,
                        position,
                    ),
                ).rowcount
                if changed != 1:
                    raise RegistryError("Workspace operation step is unavailable")
                connection.commit()
            except (sqlite3.Error, RegistryError) as error:
                connection.rollback()
                if isinstance(error, RegistryError):
                    raise
                raise _registry_error(error) from error

    def release_lease(
        self,
        *,
        scope_kind: str,
        scope_key: str,
        operation_id: str,
        lease_epoch: int,
    ) -> None:
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_lease(
                    connection,
                    operation_id=operation_id,
                    scope_kind=scope_kind,
                    scope_key=scope_key,
                    lease_epoch=lease_epoch,
                )
                connection.execute(
                    "UPDATE mutation_leases SET active = 0 "
                    "WHERE scope_kind = ? AND scope_key = ?",
                    (scope_kind, scope_key),
                )
                connection.commit()
            except (sqlite3.Error, RegistryError) as error:
                connection.rollback()
                if isinstance(error, RegistryError):
                    raise
                raise _registry_error(error) from error

    def fail_create_operation(
        self,
        *,
        operation_id: str,
        workspace_id: str,
        lease_epoch: int,
        error: str,
    ) -> None:
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_lease(
                    connection,
                    operation_id=operation_id,
                    scope_kind="workspace",
                    scope_key=workspace_id,
                    lease_epoch=lease_epoch,
                )
                now = _timestamp()
                result_json = json.dumps(
                    {"error": error, "outcome": "unknown"},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                connection.execute(
                    "UPDATE operation_steps SET status = 'unknown', result_json = ? "
                    "WHERE operation_id = ? AND status = 'running'",
                    (result_json, operation_id),
                )
                connection.execute(
                    "UPDATE operations SET status = 'failed', error = ?, "
                    "updated_at = ? "
                    "WHERE id = ?",
                    (error, now, operation_id),
                )
                connection.execute(
                    "UPDATE workspace_create_intents "
                    "SET status = 'create_failed', updated_at = ? "
                    "WHERE operation_id = ?",
                    (now, operation_id),
                )
                connection.execute(
                    "UPDATE workspaces SET lifecycle_state = 'create_failed' "
                    "WHERE id = ? AND origin = 'created'",
                    (workspace_id,),
                )
                connection.execute(
                    "UPDATE workspace_aggregates "
                    "SET lifecycle_state = 'create_failed' WHERE workspace_id = ?",
                    (workspace_id,),
                )
                connection.execute(
                    "UPDATE mutation_leases SET active = 0 "
                    "WHERE scope_kind = 'workspace' AND scope_key = ?",
                    (workspace_id,),
                )
                connection.commit()
            except (sqlite3.Error, RegistryError) as failure:
                connection.rollback()
                if isinstance(failure, RegistryError):
                    raise
                raise _registry_error(failure) from failure

    def save_cache_entry(
        self,
        normalized_source: str,
        *,
        path: str,
        repository_generation: str | None,
        operation_id: str,
        lease_epoch: int,
    ) -> None:
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_lease(
                    connection,
                    operation_id=operation_id,
                    scope_kind="repository",
                    scope_key=normalized_source,
                    lease_epoch=lease_epoch,
                )
                connection.execute(
                    """
                    INSERT INTO repository_cache_entries (
                        normalized_source, path, status,
                        repository_generation, updated_at
                    ) VALUES (?, ?, 'ready', ?, ?)
                    ON CONFLICT(normalized_source) DO UPDATE SET
                        path = excluded.path,
                        status = excluded.status,
                        repository_generation = excluded.repository_generation,
                        updated_at = excluded.updated_at
                    """,
                    (normalized_source, path, repository_generation, _timestamp()),
                )
                connection.commit()
            except sqlite3.Error as error:
                connection.rollback()
                raise _registry_error(error) from error

    def cache_entry(self, normalized_source: str) -> tuple[str, str | None] | None:
        with self._connection() as connection:
            self._migrate(connection)
            row = connection.execute(
                "SELECT path, repository_generation FROM repository_cache_entries "
                "WHERE normalized_source = ? AND status = 'ready'",
                (normalized_source,),
            ).fetchone()
            if row is None:
                return None
            return str(row["path"]), (
                str(row["repository_generation"])
                if row["repository_generation"] is not None
                else None
            )

    def complete_workspace_create(
        self,
        *,
        intent: CreateIntentRecord,
        observation: WorktreeObservation,
        created_from_sha: str,
        configuration: bytes,
        configuration_json: str,
        configuration_digest: str,
        repository_id: str,
        state: str,
        lease_epoch: int,
    ) -> None:
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_lease(
                    connection,
                    operation_id=intent.operation_id,
                    scope_kind="workspace",
                    scope_key=intent.workspace_id,
                    lease_epoch=lease_epoch,
                )
                if observation.git_common_dir_generation is None:
                    raise RegistryError("Repository generation marker is unavailable")
                if observation.git_dir_generation is None:
                    raise RegistryError("Worktree generation marker is unavailable")
                aggregate = connection.execute(
                    "SELECT definition_json FROM workspace_aggregates "
                    "WHERE workspace_id = ?",
                    (intent.workspace_id,),
                ).fetchone()
                if aggregate is None:
                    raise RegistryError("Workspace definition is unavailable")
                definition = json.loads(str(aggregate["definition_json"]))
                resources = (
                    definition.get("resources")
                    if isinstance(definition, dict)
                    else None
                )
                resource = (
                    resources[0] if isinstance(resources, list) and resources else None
                )
                if (
                    not isinstance(resource, dict)
                    or definition.get("repository_id") != repository_id
                    or definition.get("created_from_sha") != created_from_sha
                    or definition.get("configuration") != configuration.hex()
                    or definition.get("configuration_digest") != configuration_digest
                    or resource.get("locator") != str(observation.path)
                    or resource.get("ownership_token") != observation.git_dir_generation
                ):
                    raise RegistryError(
                        "Workspace completion does not match its immutable definition"
                    )
                token = _reserve_observation(connection)
                now = _timestamp()
                repository = connection.execute(
                    "SELECT * FROM repositories WHERE git_common_dir = ?",
                    (str(observation.repository_common_dir),),
                ).fetchone()
                if repository is None:
                    connection.execute(
                        """
                        INSERT INTO repositories (
                            id, git_common_dir, git_common_dir_generation,
                            created_observation_token, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            repository_id,
                            str(observation.repository_common_dir),
                            observation.git_common_dir_generation,
                            token,
                            now,
                        ),
                    )
                else:
                    _validate_repository_binding(repository, observation)
                    if str(repository["id"]) != repository_id:
                        raise RegistryError(
                            "Repository identity changed during Workspace creation"
                        )
                connection.execute(
                    """
                    INSERT INTO workspaces (
                        id, repository_id, git_dir, git_dir_generation,
                        path, branch, head, adopted_head, created_at,
                        last_observed_at, last_observation_token, parent_id,
                        created_from_sha, configuration, configuration_json,
                        configuration_digest, lifecycle_state,
                        aggregate_version, origin, completed_operation_id
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL,
                        ?, ?, ?, ?, ?, 1, 'created', NULL
                    )
                    """,
                    (
                        intent.workspace_id,
                        repository_id,
                        str(observation.git_dir),
                        observation.git_dir_generation,
                        str(observation.path),
                        observation.branch,
                        observation.head,
                        now,
                        observation.observed_at,
                        token,
                        created_from_sha,
                        configuration,
                        configuration_json,
                        configuration_digest,
                        state,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO workspace_resources (
                        workspace_id, position, name, kind, adapter_id,
                        adapter_api_major, configuration_json,
                        external_reference, locator, ownership_token,
                        provisioning_status
                    ) VALUES (?, 0, 'worktree', 'worktree',
                        'fangorn.git-worktree', 1, '{}', NULL, ?, ?, 'created')
                    """,
                    (
                        intent.workspace_id,
                        str(observation.path),
                        observation.git_dir_generation,
                    ),
                )
                connection.execute(
                    "UPDATE operations SET status = 'completed', updated_at = ? "
                    "WHERE id = ?",
                    (now, intent.operation_id),
                )
                aggregate_changed = connection.execute(
                    "UPDATE workspace_aggregates SET lifecycle_state = ?, "
                    "aggregate_version = 1, completed_operation_id = ? "
                    "WHERE workspace_id = ? AND completed_operation_id IS NULL",
                    (state, intent.operation_id, intent.workspace_id),
                ).rowcount
                if aggregate_changed != 1:
                    raise RegistryError("Workspace completion is already recorded")
                connection.execute(
                    "UPDATE workspaces SET completed_operation_id = ? WHERE id = ?",
                    (intent.operation_id, intent.workspace_id),
                )
                connection.execute(
                    "UPDATE workspace_create_intents "
                    "SET status = 'completed', updated_at = ? WHERE operation_id = ?",
                    (now, intent.operation_id),
                )
                connection.execute(
                    "UPDATE mutation_leases SET active = 0 "
                    "WHERE scope_kind = 'workspace' AND scope_key = ?",
                    (intent.workspace_id,),
                )
                connection.commit()
            except (sqlite3.Error, RegistryError) as error:
                connection.rollback()
                if isinstance(error, RegistryError):
                    raise
                raise _registry_error(error) from error

    def load_created_workspace(
        self, workspace_id: str
    ) -> tuple[sqlite3.Row, list[sqlite3.Row], sqlite3.Row]:
        with self._read_connection() as connection:
            if connection is None:
                raise RegistryError("Created Workspace is unavailable")
            workspace = connection.execute(
                "SELECT * FROM workspaces WHERE id = ? AND origin = 'created'",
                (workspace_id,),
            ).fetchone()
            if workspace is None:
                raise RegistryError("Created Workspace is unavailable")
            resources = connection.execute(
                "SELECT * FROM workspace_resources WHERE workspace_id = ? "
                "ORDER BY position",
                (workspace_id,),
            ).fetchall()
            operation = connection.execute(
                "SELECT * FROM operations WHERE id = ?",
                (str(workspace["completed_operation_id"]),),
            ).fetchone()
            if operation is None:
                raise RegistryError("Completed create operation is unavailable")
            return workspace, resources, operation

    @staticmethod
    def _aggregate_version(
        connection: sqlite3.Connection, *, scope_kind: str, scope_key: str
    ) -> int:
        if scope_kind != "workspace":
            return 0
        row = connection.execute(
            "SELECT aggregate_version FROM workspaces WHERE id = ?",
            (scope_key,),
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT aggregate_version FROM workspace_create_intents "
                "WHERE workspace_id = ?",
                (scope_key,),
            ).fetchone()
        return int(row["aggregate_version"]) if row is not None else 0

    @staticmethod
    def _require_lease(
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        scope_kind: str,
        scope_key: str,
        lease_epoch: int,
    ) -> None:
        row = connection.execute(
            """
            SELECT operation_id, epoch, aggregate_version, active
            FROM mutation_leases
            WHERE scope_kind = ? AND scope_key = ?
            """,
            (scope_kind, scope_key),
        ).fetchone()
        if (
            row is None
            or not bool(row["active"])
            or str(row["operation_id"]) != operation_id
            or int(row["epoch"]) != lease_epoch
        ):
            raise RegistryError("Stale operation result rejected by lease fence")
        current_version = Registry._aggregate_version(
            connection, scope_kind=scope_kind, scope_key=scope_key
        )
        if int(row["aggregate_version"]) != current_version:
            raise RegistryError("Stale operation result rejected by aggregate version")

    def get_by_worktree(self, observation: WorktreeObservation) -> WorkspaceRecord:
        observation_token = _observation_token(observation)
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT workspaces.*, repositories.git_common_dir,
                        repositories.git_common_dir_generation
                    FROM workspaces
                    JOIN repositories ON repositories.id = workspaces.repository_id
                    WHERE workspaces.git_dir = ?
                    """,
                    (str(observation.git_dir),),
                ).fetchone()
                if row is None:
                    raise RegistryError(f"Worktree is not adopted: {observation.path}")
                if str(row["git_common_dir"]) != str(observation.repository_common_dir):
                    raise RegistryError(
                        "Git identity is ambiguous; refusing to change the binding"
                    )
                _validate_repository_binding(row, observation)
                _validate_worktree_binding(
                    row,
                    repository_id=str(row["repository_id"]),
                    observation=observation,
                )
                connection.execute(
                    """
                    UPDATE workspaces
                    SET path = ?, branch = ?, head = ?, last_observed_at = ?,
                        last_observation_token = ?
                    WHERE id = ? AND last_observation_token < ?
                    """,
                    (
                        str(observation.path),
                        observation.branch,
                        observation.head,
                        observation.observed_at,
                        observation_token,
                        str(row["id"]),
                        observation_token,
                    ),
                )
                connection.commit()
            except (sqlite3.Error, RegistryError) as error:
                connection.rollback()
                if isinstance(error, RegistryError):
                    raise
                raise _registry_error(error) from error

            refreshed = connection.execute(
                """
                SELECT workspaces.*, repositories.git_common_dir,
                    repositories.git_common_dir_generation
                FROM workspaces
                JOIN repositories ON repositories.id = workspaces.repository_id
                WHERE workspaces.id = ?
                """,
                (str(row["id"]),),
            ).fetchone()
            if refreshed is None:
                raise RegistryError("Workspace disappeared from the registry")
            return _workspace_from_row(refreshed)

    def inspect_worktree(self, observation: WorktreeObservation) -> WorkspaceRecord:
        with self._read_connection() as connection:
            if connection is None:
                raise RegistryError(f"Worktree is not adopted: {observation.path}")
            try:
                row = connection.execute(
                    """
                    SELECT workspaces.*, repositories.git_common_dir,
                        repositories.git_common_dir_generation
                    FROM workspaces
                    JOIN repositories ON repositories.id = workspaces.repository_id
                    WHERE workspaces.git_dir = ?
                    """,
                    (str(observation.git_dir),),
                ).fetchone()
            except sqlite3.Error as error:
                raise _registry_error(error) from error
            if row is None:
                raise RegistryError(f"Worktree is not adopted: {observation.path}")
            if str(row["git_common_dir"]) != str(observation.repository_common_dir):
                raise RegistryError(
                    "Git identity is ambiguous; refusing to change the binding"
                )
            _validate_repository_binding(row, observation)
            _validate_worktree_binding(
                row,
                repository_id=str(row["repository_id"]),
                observation=observation,
            )
            return replace(
                _workspace_from_row(row),
                path=str(observation.path),
                branch=observation.branch,
                head=observation.head,
                last_observed_at=observation.observed_at,
            )

    def list_workspaces(self) -> list[WorkspaceRecord]:
        with self._read_connection() as connection:
            if connection is None:
                return []
            try:
                rows = connection.execute(
                    """
                    SELECT workspaces.*, repositories.git_common_dir,
                        repositories.git_common_dir_generation
                    FROM workspaces
                    JOIN repositories ON repositories.id = workspaces.repository_id
                    ORDER BY workspaces.path, workspaces.id
                    """
                ).fetchall()
            except sqlite3.Error as error:
                raise _registry_error(error) from error
            return [_workspace_from_row(row) for row in rows]

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection | None]:
        if not _state_directory_exists(self.path.parent):
            yield None
            return
        if not _database_file_exists(self.path):
            yield None
            return
        deadline = time.monotonic() + READ_INITIALIZATION_TIMEOUT_SECONDS
        connection: sqlite3.Connection
        while True:
            try:
                connection = sqlite3.connect(
                    f"{self.path.resolve(strict=True).as_uri()}?mode=ro",
                    uri=True,
                    timeout=BUSY_TIMEOUT_SECONDS,
                    isolation_level=None,
                )
            except (OSError, sqlite3.Error) as error:
                raise RegistryError(
                    f"Registry database unavailable: {error}"
                ) from error
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                self._require_supported_schema(connection)
            except sqlite3.Error as error:
                connection.close()
                if _schema_initialization_in_progress(error):
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(
                            min(READ_INITIALIZATION_RETRY_DELAY_SECONDS, remaining)
                        )
                        continue
                raise _registry_error(error) from error
            except RegistryError:
                connection.close()
                raise
            break
        try:
            yield connection
        except sqlite3.Error as error:
            raise _registry_error(error) from error
        finally:
            connection.close()

    def _require_supported_schema(self, connection: sqlite3.Connection) -> None:
        applied = {
            int(row["version"])
            for row in connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        unknown = {version for version in applied if version > SCHEMA_VERSION}
        if unknown:
            versions = ", ".join(str(version) for version in sorted(unknown))
            raise RegistryError(
                f"Registry schema is newer than this Fangorn version: {versions}"
            )

    @contextmanager
    def _connection(
        self, *, timeout: float | None = None
    ) -> Iterator[sqlite3.Connection]:
        _prepare_state_directory(self.path.parent)
        _prepare_database_file(self.path)
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=BUSY_TIMEOUT_SECONDS if timeout is None else timeout,
                isolation_level=None,
            )
        except sqlite3.Error as error:
            raise RegistryError(f"Registry database unavailable: {error}") from error
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        except sqlite3.Error as error:
            raise _registry_error(error) from error
        finally:
            connection.close()

    def _migrate(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            unknown = {version for version in applied if version > SCHEMA_VERSION}
            if unknown:
                versions = ", ".join(str(version) for version in sorted(unknown))
                raise RegistryError(
                    f"Registry schema is newer than this Fangorn version: {versions}"
                )
            for version, statements in MIGRATIONS:
                if version in applied:
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, _timestamp()),
                )
            connection.commit()
        except (sqlite3.Error, RegistryError) as error:
            connection.rollback()
            if isinstance(error, RegistryError):
                raise
            raise _registry_error(error) from error


def _workspace_from_row(row: sqlite3.Row) -> WorkspaceRecord:
    return WorkspaceRecord(
        id=str(row["id"]),
        repository_id=str(row["repository_id"]),
        repository_common_dir=str(row["git_common_dir"]),
        git_common_dir_generation=str(row["git_common_dir_generation"]),
        git_dir=str(row["git_dir"]),
        git_dir_generation=str(row["git_dir_generation"]),
        path=str(row["path"]),
        branch=str(row["branch"]) if row["branch"] is not None else None,
        head=str(row["head"]) if row["head"] is not None else None,
        adopted_head=(
            str(row["adopted_head"]) if row["adopted_head"] is not None else None
        ),
        created_at=str(row["created_at"]),
        last_observed_at=str(row["last_observed_at"]),
        last_observation_token=int(row["last_observation_token"]),
    )


def _create_intent_from_row(row: sqlite3.Row) -> CreateIntentRecord:
    return CreateIntentRecord(
        operation_id=str(row["operation_id"]),
        workspace_id=str(row["workspace_id"]),
        request_key=str(row["request_key"]),
        request_json=str(row["request_json"]),
        request_id=str(row["request_id"]) if row["request_id"] is not None else None,
        target_path=str(row["target_path"]),
        resolved_sha=(
            str(row["resolved_sha"]) if row["resolved_sha"] is not None else None
        ),
        resolved_json=(
            str(row["resolved_json"]) if row["resolved_json"] is not None else None
        ),
        status=str(row["status"]),
    )


def _lease_from_row(row: sqlite3.Row) -> LeaseRecord:
    return LeaseRecord(
        scope_kind=str(row["scope_kind"]),
        scope_key=str(row["scope_key"]),
        operation_id=str(row["operation_id"]),
        owner=ProcessIdentity(
            process_instance_id=str(row["process_instance_id"]),
            boot_identity=str(row["boot_identity"]),
            pid=int(row["pid"]),
            process_start_identity=str(row["process_start_identity"]),
        ),
        epoch=int(row["epoch"]),
        aggregate_version=int(row["aggregate_version"]),
        active=bool(row["active"]),
    )


def _observation_token(observation: WorktreeObservation) -> int:
    token = observation.observation_token
    if token is None or token <= 0:
        raise RegistryError("Registry observation token is missing or invalid")
    return token


def _schema_initialization_in_progress(error: sqlite3.Error) -> bool:
    return (
        isinstance(error, sqlite3.OperationalError)
        and str(error) == "no such table: schema_migrations"
    )


def _reserve_observation(connection: sqlite3.Connection) -> int:
    connection.execute(
        """
        UPDATE observation_clock
        SET current_token = current_token + 1
        WHERE singleton = 1
        """
    )
    row = connection.execute(
        """
        SELECT current_token FROM observation_clock
        WHERE singleton = 1
        """
    ).fetchone()
    if row is None:
        raise RegistryError("Registry observation clock is unavailable")
    return int(row["current_token"])


def _validate_worktree_binding(
    row: sqlite3.Row,
    *,
    repository_id: str,
    observation: WorktreeObservation,
) -> None:
    if str(row["repository_id"]) != repository_id:
        raise RegistryError(
            "Git administrative directory is already bound to another repository"
        )
    if observation.git_dir_generation is None:
        raise RegistryError(
            "Fangorn worktree generation marker is missing; Workspace identity drifted"
        )
    if str(row["git_dir_generation"]) != observation.git_dir_generation:
        raise RegistryError(
            "Fangorn worktree generation marker changed; Workspace identity drifted"
        )


def _validate_repository_binding(
    row: sqlite3.Row, observation: WorktreeObservation
) -> None:
    if observation.git_common_dir_generation is None:
        raise RegistryError(
            "Fangorn repository generation marker is missing; "
            "Repository identity drifted"
        )
    if str(row["git_common_dir_generation"]) != observation.git_common_dir_generation:
        raise RegistryError(
            "Fangorn repository generation marker changed; Repository identity drifted"
        )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _prepare_state_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RegistryError(
                f"Registry state directory unavailable: symlink is not allowed: {path}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise RegistryError(
                f"Registry state directory unavailable: not a directory: {path}"
            )
        if metadata.st_uid != os.geteuid():
            raise RegistryError(
                "Registry state directory unavailable: not owned by current user: "
                f"{path}"
            )
        path.chmod(0o700)
    except RegistryError:
        raise
    except OSError as error:
        detail = error.strerror or str(error)
        raise RegistryError(
            f"Registry state directory unavailable: {path}: {detail}"
        ) from error


def _state_directory_exists(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        detail = error.strerror or str(error)
        raise RegistryError(
            f"Registry state directory unavailable: {path}: {detail}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode):
        raise RegistryError(
            f"Registry state directory unavailable: symlink is not allowed: {path}"
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise RegistryError(
            f"Registry state directory unavailable: not a directory: {path}"
        )
    if metadata.st_uid != os.geteuid():
        raise RegistryError(
            f"Registry state directory unavailable: not owned by current user: {path}"
        )
    return True


def _prepare_database_file(path: Path) -> None:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RegistryError(
                f"Registry database unavailable: not a regular file: {path}"
            )
        if metadata.st_uid != os.geteuid():
            raise RegistryError(
                f"Registry database unavailable: not owned by current user: {path}"
            )
        os.fchmod(descriptor, 0o600)
    except RegistryError:
        raise
    except OSError as error:
        detail = error.strerror or str(error)
        raise RegistryError(
            f"Registry database unavailable: {path}: {detail}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _database_file_exists(path: Path) -> bool:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RegistryError(
                f"Registry database unavailable: not a regular file: {path}"
            )
        if metadata.st_uid != os.geteuid():
            raise RegistryError(
                f"Registry database unavailable: not owned by current user: {path}"
            )
    except FileNotFoundError:
        return False
    except RegistryError:
        raise
    except OSError as error:
        detail = error.strerror or str(error)
        raise RegistryError(
            f"Registry database unavailable: {path}: {detail}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return True


def _registry_error(error: sqlite3.Error) -> RegistryError:
    if _is_sqlite_contention(error):
        return RegistryError(
            f"Registry remained busy for {BUSY_TIMEOUT_SECONDS:g} seconds"
        )
    return RegistryError(f"Registry operation failed: {error}")


def _sqlite_contention_cause(error: RegistryError) -> sqlite3.Error | None:
    cause = error.__cause__
    while cause is not None:
        if isinstance(cause, sqlite3.Error) and _is_sqlite_contention(cause):
            return cause
        cause = cause.__cause__
    return None


def _is_sqlite_contention(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and code & 0xFF in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }
