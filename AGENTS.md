# Dispatcharr-EPGShift agent instructions

## Scope

These instructions apply to the entire repository.

This is a private downstream copy of:

https://github.com/Dispatcharr/Dispatcharr

Keep changes small and focused so future upstream updates remain easy to incorporate.

## Required preparation

Before changing files:

1. Read `README.md` and `CONTRIBUTING.md`.
2. Inspect `git status` and the current branch.
3. Examine the relevant existing code, tests, models, serializers, views and frontend components.
4. Explain the current behaviour and propose a minimal implementation plan.
5. Identify the files likely to change and the verification commands.
6. Ask before proceeding if a decision would materially affect behaviour or compatibility.

Do not make application changes directly on `main`. Create a focused branch named `dsh/<short-task-name>`.

## Change rules

- Work only inside this repository.
- Make the smallest change that satisfies the requested goal.
- Preserve existing APIs, data formats and behaviour unless the request requires otherwise.
- Follow the existing Python/Django and React/Vite patterns.
- Prefer the existing plugin or event-hook system when it cleanly supports the requirement.
- Do not perform unrelated refactoring or formatting.
- Do not introduce new dependencies or update lockfiles without approval.
- Do not create database migrations unless a model change genuinely requires one.
- Never edit or expose credentials, tokens, API keys, private keys or production data.
- Do not add real IPTV provider URLs, usernames, passwords or playlist contents.
- Do not remove, weaken or skip tests merely to make them pass.
- Do not leave debug logging, `print()`, `console.log` or commented-out code.
- Never run `git reset --hard`, `git clean`, force-push or delete the repository.
- Do not commit or push unless explicitly requested.
- Do not alter Git remotes or upstream configuration without approval.

## Project architecture

- Backend: Python, Django and Django REST Framework
- Asynchronous tasks: Celery and Redis
- Database: PostgreSQL
- Frontend: React, Vite, Mantine UI and Zustand
- Packaging: `uv` and `pyproject.toml`
- Development deployment: Docker Compose

Treat the current repository configuration as authoritative if it differs from documentation.

## Verification

Choose checks appropriate to the affected area.

For backend changes, run focused tests first and then the broader applicable test suite.

The documented backend test command is:

    python manage.py test

When using the `uv` environment, use:

    uv run python manage.py test

For frontend changes, run the appropriate existing scripts from the `frontend` directory:

    npm run lint
    npm run test
    npm run build

For Docker configuration changes, validate the relevant Compose configuration without starting or replacing production services.

Before installing dependencies, starting containers or running commands that require network access, explain what will happen and request approval.

After making changes:

1. Review `git status`.
2. Review the complete `git diff`.
3. Check for accidental generated files, secrets and unrelated formatting.
4. Report all commands run and their results.
5. Separate pre-existing failures from failures caused by the change.
6. Describe any manual testing that remains necessary.

## Final response

Finish every implementation task with:

- Summary of the implemented behaviour
- Files changed
- Tests and checks performed
- Results and any pre-existing failures
- Remaining risks or manual verification
- Confirmation that nothing was committed or pushed unless requested
