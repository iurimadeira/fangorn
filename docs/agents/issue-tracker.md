# Issue Tracker Policy

Ownership: Personal
Tracker: GitHub Issues
Repository: https://github.com/iurimadeira/fangorn

External pull requests are not a request surface. Proposed work starts in an
Issue; a pull request implements an accepted Issue and must reference it.

## Working conventions

- GitHub Issues are the source of truth for requests and implementation units.
- Ticket graphs require explicit approval before publication.
- Publish blocker-first.
- Record dependencies under `## Blocked by` when no native dependency link is
  available.
- Opening a pull request does not by itself accept new scope.
- Pull requests target `main`; direct pushes, force-pushes, and branch deletion
  are prohibited.

## Commands

- List: `gh issue list --repo iurimadeira/fangorn`
- Read: `gh issue view <number> --repo iurimadeira/fangorn`
- Create: `gh issue create --repo iurimadeira/fangorn`

## Public repository safety

- Treat every Issue, comment, branch, commit, and pull request as public.
- Never publish credentials, tokens, private paths, account-routing details,
  personal logs, tailnet/lab infrastructure, or recovery data.
- Keep `acc`, `aco`, and private dotfiles behavior outside Fangorn.
