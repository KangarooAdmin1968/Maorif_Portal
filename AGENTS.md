# Maorif Portal — Agent Safety Rules

This Django project manages live school data (grades, journals, teacher
allocations). Tool-level permissions are enforced in `.devin/config.json`;
the rules below govern how the agent works.

## Before making changes

- Inspect the existing architecture and relevant code paths first.
- Preserve all existing functionality; do not change unrelated code.
- Before adding a model, field, setting, or helper, check whether an existing
  source of truth already exists. Never introduce duplicate storage or logic.

## Database & migrations

- Creating migrations (`makemigrations` without `--check`/`--dry-run`) and
  applying migrations (`migrate`) always require explicit approval.
- Never delete, bulk-change, or rewrite existing database data. Prefer
  additive, reversible changes.
- Do not modify grading formulas, ranking logic, daily/monthly journals,
  assessment methodology, offline queue, school eligibility, or
  parent/student portal behavior unless the task explicitly requires it.
- Do not touch `db.sqlite3` or other database files directly.

## Secrets & production

- Never change `.env`, environment variables, API keys, passwords, or tokens.
- Never print, copy, or transmit secrets.
- No deployment or server-side production actions without explicit approval
  (`deploy_to_server.py`, `deploy_hotfix.py`, `run_remote_*.py`, …).
- Do not change CI/CD configuration (`.github/`, `deploy.yml`) or
  `maorif_portal/settings.py` without explicit approval.

## Git

- Do not commit unless explicitly instructed. The project owner commits and
  pushes via GitHub Desktop — never run `git push` from the terminal.
- Never run `git reset --hard`, force-push, or history-rewriting commands.

## After completing a task

- Run focused tests for the changed area.
- Run `python manage.py test portal` (full suite) when practical.
- Run `python manage.py check` and `python manage.py makemigrations --check`.
- Report: files changed, whether a migration was created or is required,
  test results, and check results.
- Stop before any destructive, deployment, migration-apply, or git-push
  action.

## Ambiguity

- If an action is ambiguous or could lose data, stop and ask for approval
  instead of guessing.
