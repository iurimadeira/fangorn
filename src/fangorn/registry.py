from __future__ import annotations

import os
import sqlite3
import stat
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

    def adopt(self, observation: WorktreeObservation) -> tuple[WorkspaceRecord, bool]:
        observation_token = _observation_token(observation)
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
        with self._connection() as connection:
            self._migrate(connection)
            created_at = _timestamp()
            try:
                connection.execute("BEGIN IMMEDIATE")
                repository = connection.execute(
                    """
                    SELECT id, git_common_dir_generation,
                        created_observation_token
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
                connection.commit()
            except (sqlite3.Error, RegistryError) as error:
                connection.rollback()
                if isinstance(error, RegistryError):
                    raise
                raise _registry_error(error) from error

            row = connection.execute(
                """
                SELECT workspaces.*, repositories.git_common_dir,
                    repositories.git_common_dir_generation
                FROM workspaces
                JOIN repositories ON repositories.id = workspaces.repository_id
                WHERE workspaces.id = ?
                """,
                (workspace_id,),
            ).fetchone()
            if row is None:
                raise RegistryError("Adopted Workspace disappeared from the registry")
            return _workspace_from_row(row), created

    def reserve_observation(self) -> int:
        with self._connection() as connection:
            self._migrate(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
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
                token = int(row["current_token"])
                connection.commit()
                return token
            except (sqlite3.Error, RegistryError) as error:
                connection.rollback()
                if isinstance(error, RegistryError):
                    raise
                raise _registry_error(error) from error

    def marker_creation_requirements(
        self, observation: WorktreeObservation
    ) -> tuple[bool, bool] | None:
        observation_token = _observation_token(observation)
        with self._connection() as connection:
            self._migrate(connection)
            try:
                repository = connection.execute(
                    """
                    SELECT id, git_common_dir_generation,
                        created_observation_token
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
                    and int(repository["created_observation_token"]) > observation_token
                ):
                    return None
                _validate_repository_binding(repository, observation)
            if workspace is not None:
                if (
                    observation.git_dir_generation is None
                    and int(workspace["last_observation_token"]) > observation_token
                ):
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

    def list_workspaces(self) -> list[WorkspaceRecord]:
        with self._connection() as connection:
            self._migrate(connection)
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
    def _connection(self) -> Iterator[sqlite3.Connection]:
        _prepare_state_directory(self.path.parent)
        _prepare_database_file(self.path)
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=BUSY_TIMEOUT_SECONDS,
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


def _observation_token(observation: WorktreeObservation) -> int:
    token = observation.observation_token
    if token is None or token <= 0:
        raise RegistryError("Registry observation token is missing or invalid")
    return token


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


def _registry_error(error: sqlite3.Error) -> RegistryError:
    if "locked" in str(error).lower() or "busy" in str(error).lower():
        return RegistryError(
            f"Registry remained busy for {BUSY_TIMEOUT_SECONDS:g} seconds"
        )
    return RegistryError(f"Registry operation failed: {error}")
