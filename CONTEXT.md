# Workspace Management

Fangorn manages durable identities for Git worktrees and the family relationships
between those identities.

## Language

**Workspace**: Fangorn's durable identity for exactly one Git worktree. In
user-facing prose, "workspace" may stand for its bound worktree because the
mapping is one-to-one.
_Avoid_: tmux session, arbitrary directory

**Git worktree**: The Git checkout and administrative identity bound to one
Workspace.
_Avoid_: Workspace when discussing Git-specific mechanics

**Repository**: The shared Git identity to which one or more Workspaces belong,
including linked worktrees.
_Avoid_: project, checkout

**Binding**: The permanent relationship between a Workspace and one incarnation
of a Git worktree. A different worktree or a replacement incarnation requires a
different Workspace.
_Avoid_: association, current worktree

**Adoption**: Registration of an existing Git worktree as a Workspace without
changing the checkout's files, branch, index, refs, or HEAD.
_Avoid_: import, attach

**Current Git Facts**: The latest observed canonical path, branch, and HEAD for a
Workspace. These facts may change without changing its Binding.
_Avoid_: Binding metadata

**Workspace Family**: A rooted tree of Workspaces. A Workspace may have children
and at most one parent.
_Avoid_: session group, flat workspace list

## Relationships

- A **Workspace** has exactly one immutable **Binding** to exactly one **Git
  worktree**.
- A **Git worktree** may belong to at most one **Workspace**.
- A **Repository** has one or more **Workspaces**.
- A **Workspace** has zero or one parent and zero or more children within a
  **Workspace Family**.
- Parentage never changes a Workspace's **Binding**.

## Flagged ambiguities

- User-facing prose may use Workspace and worktree interchangeably because of
  their one-to-one relationship; implementation prose keeps the Fangorn identity
  and Git checkout distinct.
