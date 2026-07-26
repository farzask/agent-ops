# Skills

Project skills for AgentOps live in [`.claude/skills/`](../.claude/skills/), not in
this folder. Claude Code only auto-discovers project skills under `.claude/skills/`;
a bare `skills/` directory at the repo root is not on the discovery path.

## Authored project skills (committed, auto-loaded)

These were written for this repo because no off-the-shelf skill exists for the
stack in `TECH_SPEC.md`:

| Skill | Covers |
|---|---|
| [`agentops-conventions`](../.claude/skills/agentops-conventions/SKILL.md) | The no-agent-framework rule, `emit_event()` contract, WS envelope, agent state machine, failure classes |
| [`fastapi-backend`](../.claude/skills/fastapi-backend/SKILL.md) | Async-everywhere rules, DI, lifespan wiring, routers, WS handlers, queue worker, Pydantic v2 |
| [`sqlalchemy-async-db`](../.claude/skills/sqlalchemy-async-db/SKILL.md) | SQLAlchemy 2.0 async + asyncpg, enum mapping, required indexes, eager loading, transaction boundaries |
| [`alembic-migrations`](../.claude/skills/alembic-migrations/SKILL.md) | Async `env.py`, autogenerate caveats, native enum ordering, review checklist |
| [`nextjs-ui`](../.claude/skills/nextjs-ui/SKILL.md) | App Router server/client boundary, `useJobSocket` reconnect/backfill contract, status colors, SVG diagram |

## Installed marketplace plugins (machine-local, not committed)

Installed from the official `anthropics/claude-plugins-official` marketplace at
user scope, so they are **not** part of this repo. Reinstall on a new machine with:

```bash
claude plugin marketplace add anthropics/claude-plugins-official
for p in frontend-design claude-security security-guidance pyright-lsp typescript-lsp redis-development; do
  claude plugin install "$p@claude-plugins-official"
done
```

| Plugin | Why |
|---|---|
| `frontend-design` | Visual design guidance for the dashboard UI |
| `claude-security` | Tiered vulnerability scanning of this codebase |
| `security-guidance` | Hook-based security reminders during editing |
| `pyright-lsp` | Python type checking / code intelligence for the backend |
| `typescript-lsp` | TypeScript code intelligence for the frontend |
| `redis-development` | Redis best practices — relevant to the queue and pub/sub layer |

## Gaps, stated honestly

The official marketplace has **no** generic skill for a Python backend, for
SQLAlchemy/asyncpg, or for Alembic migrations. Every database-related plugin in it
is vendor-locked (Neon, Supabase, Prisma, PlanetScale, AlloyDB, Cloud SQL) and does
not apply to a self-hosted Postgres. That is why the four stack skills above are
hand-written rather than installed.

**Authentication** is intentionally absent. `PRD.md` §4 and §8 place auth out of
scope for v1, and the project owner confirmed skipping it. The only auth options in
the marketplace are vendor plugins (Auth0, WorkOS, Supabase, Firebase, Duende)
requiring third-party accounts, none of which v1 needs.
