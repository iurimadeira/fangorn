# Workspace Management

Fangorn manages durable Workspace aggregates and their owned external Resources.

## Language

**Workspace**: A durable aggregate created with a complete, structurally
immutable definition and operated through one lifecycle. It owns every Resource
in that definition until deletion.
_Avoid_: worktree, branch, terminal session, arbitrary directory

**Resource**: An external capability exclusively owned by one Workspace and
managed through the Workspace lifecycle.
_Avoid_: binding, instance

**Worktree Resource**: The mandatory Resource providing a Git checkout with its
own administrative identity. A Workspace owns exactly one.
_Avoid_: Workspace, branch, directory

**Terminal Resource**: An optional interactive terminal environment. A
Workspace owns at most one.
_Avoid_: Workspace, tmux session unless naming the concrete adapter

**Service Resource**: A named application service or service bundle. A
Workspace may own zero or more in declared order.
_Avoid_: generic instance, hook

**Repository**: The shared Git identity to which one or more Workspaces belong,
including linked worktrees.
_Avoid_: project, checkout

**Binding**: The schema-1 compatibility record relating a legacy Workspace
identity to one Git worktree incarnation.
_Avoid_: association, current worktree

**Adoption**: The schema-1 compatibility operation registering an existing Git
worktree without changing its files, branch, index, refs, or HEAD.
_Avoid_: import, attach

**Current Git Facts**: The schema-1 compatibility view of the latest observed
canonical path, branch, and HEAD for an adopted worktree.
_Avoid_: Binding metadata

**Parent Workspace**: The optional immutable predecessor from which a Workspace
was created. It records lineage, not Git ancestry or integration direction.
_Avoid_: Git base, blocker, merge target

## Relationships

- A **Workspace** owns exactly one **Worktree Resource**, at most one **Terminal
  Resource**, and zero or more uniquely named, ordered **Service Resources**.
- Every **Resource** has exactly one owning **Workspace**.
- A Workspace belongs to one **Repository** through its Worktree Resource.
- A Workspace has zero or one immutable **Parent Workspace** and zero or more
  children.

## Flagged ambiguities

- Schema-2 prose never uses Workspace and worktree interchangeably. The `info`,
  `list`, and hidden `adopt` compatibility surfaces retain schema-1 terminology
  only for the migration window.
