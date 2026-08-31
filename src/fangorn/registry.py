from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fangorn.git import WorktreeObservation

BUSY_TIMEOUT_SECONDS = 2.0
SCHEMA_VERSION = 1


class RegistryError(RuntimeError):
    """Registry operation failed without weakening Workspace invariants."""


@dataclass(frozen=True)
class WorkspaceRecord:
    id: str
    repository_id: str
    repository_common_dir: str
    git_dir: str
    git_dir_generation: str
    path: str
    branch: str | None
    head: str
    adopted_head: str
    created_at: str
    last_observed_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "repository_id": self.repository_id,
            "repository_common_dir": self.repository_common_dir,
            "git_dir": self.git_dir,
            "git_dir_generation": self.git_dir_generation,
            "path": self.path,
            "branch": self.branch,
            "head": self.head,
            "adopted_head": self.adopted_head,
            "created_at": self.created_at,
            "last_observed_at": self.last_observed_at,
        }


MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE repositories (
                id TEXT PRIMARY KEY NOT NULL,
                git_common_dir TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE workspaces (
                id TEXT PRIMARY KEY NOT NULL,
                repository_id TEXT NOT NULL REFERENCES repositories(id),
                git_dir TEXT NOT NULL UNIQUE,
                git_dir_generation TEXT NOT NULL,
                path TEXT NOT NULL,
                branch TEXT,
                head TEXT NOT NULL,
                adopted_head TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL
            )
            """,
            """
            CREATE TRIGGER repositories_immutable_identity
            BEFORE UPDATE OF id, git_common_dir ON repositories
            FOR EACH ROW
            WHEN NEW.id IS NOT OLD.id
                OR NEW.git_common_dir IS NOT OLD.git_common_dir
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
)


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def from_environment(cls) -> Registry:
        state_home = os.environ.get("XDG_STATE_HOME")
        if state_home:
            root = Path(state_home).expanduser()
        else:
            root = Path.home() / ".local" / "state"
        return cls(root / "fangorn" / "registry.sqlite3")

    def adopt(self, observation: WorktreeObservation) -> tuple[WorkspaceRecord, bool]:
        with self._connection() as connection:
            self._migrate(connection)
            created_at = _timestamp()
            try:
                connection.execute("BEGIN IMMEDIATE")
                repository = connection.execute(
                    "SELECT id FROM repositories WHERE git_common_dir = ?",
                    (str(observation.repository_common_dir),),
                ).fetchone()
                if repository is None:
                    repository_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO repositories (id, git_common_dir, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (
                            repository_id,
                            str(observation.repository_common_dir),
                            created_at,
                        ),
                    )
                else:
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
                            last_observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        SET path = ?, branch = ?, head = ?, last_observed_at = ?
                        WHERE id = ? AND last_observed_at < ?
                        """,
                        (
                            str(observation.path),
                            observation.branch,
                            observation.head,
                            observation.observed_at,
                            workspace_id,
                            observation.observed_at,
                        ),
                    )
                connection.commit()
            except (sqlite3.Error, RegistryError) as error:
                connection.rollback()
                if isinstance(error, RegistryError):
                    raise
                raise _registry_error(error) from error

            row = connection.execute(
                """
                SELECT workspaces.*, repositories.git_common_dir
                FROM workspaces
                JOIN repositories ON repositories.id = workspaces.repository_id
                WHERE workspaces.id = ?
                """,
                (workspace_id,),
            ).fetchone()
            if row is None:
                raise RegistryError("Adopted Workspace disappeared from the registry")
            return _workspace_from_row(row), created

    def get_by_worktree(self, observation: WorktreeObservation) -> WorkspaceRecord:
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT workspaces.*, repositories.git_common_dir
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
                _validate_worktree_binding(
                    row,
                    repository_id=str(row["repository_id"]),
                    observation=observation,
                )
                connection.execute(
                    """
                    UPDATE workspaces
                    SET path = ?, branch = ?, head = ?, last_observed_at = ?
                    WHERE id = ? AND last_observed_at < ?
                    """,
                    (
                        str(observation.path),
                        observation.branch,
                        observation.head,
                        observation.observed_at,
                        str(row["id"]),
                        observation.observed_at,
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
                SELECT workspaces.*, repositories.git_common_dir
                FROM workspaces
                JOIN repositories ON repositories.id = workspaces.repository_id
                WHERE workspaces.id = ?
                """,
                (str(row["id"]),),
            ).fetchone()
            if refreshed is None:
                raise RegistryError("Workspace disappeared from the registry")
            return _workspace_from_row(refreshed)

    def list_workspaces(self) -> list[WorkspaceRecord]:
        with self._connection() as connection:
            self._migrate(connection)
            try:
                rows = connection.execute(
                    """
                    SELECT workspaces.*, repositories.git_common_dir
                    FROM workspaces
                    JOIN repositories ON repositories.id = workspaces.repository_id
                    ORDER BY workspaces.path, workspaces.id
                    """
                ).fetchall()
            except sqlite3.Error as error:
                raise _registry_error(error) from error
            return [_workspace_from_row(row) for row in rows]

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=BUSY_TIMEOUT_SECONDS,
                isolation_level=None,
            )
        except sqlite3.Error as error:
            raise _registry_error(error) from error
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}"
            )
            yield connection
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
        git_dir=str(row["git_dir"]),
        git_dir_generation=str(row["git_dir_generation"]),
        path=str(row["path"]),
        branch=str(row["branch"]) if row["branch"] is not None else None,
        head=str(row["head"]),
        adopted_head=str(row["adopted_head"]),
        created_at=str(row["created_at"]),
        last_observed_at=str(row["last_observed_at"]),
    )


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
    if str(row["git_dir_generation"]) != observation.git_dir_generation:
        raise RegistryError(
            "Git administrative directory generation changed; Workspace binding "
            "is ambiguous"
        )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _registry_error(error: sqlite3.Error) -> RegistryError:
    if "locked" in str(error).lower() or "busy" in str(error).lower():
        return RegistryError(
            f"Registry remained busy for {BUSY_TIMEOUT_SECONDS:g} seconds"
        )
    return RegistryError(f"Registry operation failed: {error}")
