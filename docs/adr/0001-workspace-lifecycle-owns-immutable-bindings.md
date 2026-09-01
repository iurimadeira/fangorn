# Workspace lifecycle owns immutable worktree bindings

Fangorn permanently binds each Workspace to one incarnation of one Git worktree;
using another worktree creates another Workspace instead of rebinding the old
one. This preserves identity and history at the cost of creating an additional
Workspace when the worktree changes.

The Workspaces module owns the complete lifecycle decision. Git observation is
read-only, generation storage only reads or establishes identity when authorized,
and Registry persistence cannot be orchestrated directly by the CLI. This favors
one deep lifecycle interface over caller-configurable assembly of Git, filesystem,
and SQLite operations.
