---
name: alembic-migrations
description: Alembic migration workflow for the AgentOps async Postgres schema — async env.py setup, autogenerate caveats, native enum create/drop ordering, index naming, and the review checklist before committing a revision. Use whenever a SQLAlchemy model changes, a migration is generated, or a migration fails to apply.
---

# Alembic Migrations (async)

Migrations live in `backend/alembic/versions/`. Config is `backend/alembic.ini`.

## Commands

Run from `backend/`:

```bash
alembic revision --autogenerate -m "short imperative description"
alembic upgrade head
alembic downgrade -1
alembic current
alembic history --verbose
```

In Docker: `docker-compose exec backend alembic upgrade head`.

## Async env.py

Alembic's default template is synchronous. This project's `env.py` uses the async
pattern — `async_engine_from_config` plus `connection.run_sync(do_run_migrations)`.
Do not regenerate `env.py` from the default template; it will break.

`env.py` reads the URL from `Settings` (never a hardcoded URL in `alembic.ini`),
and imports `Base.metadata` from `app.models.db_models` as `target_metadata`.
Every new model **must** be imported in that module's import chain or
autogenerate will silently emit an empty migration.

## Autogenerate is a draft, not an answer

Always read the generated file before committing. Autogenerate reliably misses or
mishandles:

- **Native enum types** — it does not create or drop `CREATE TYPE` reliably, and
  it never emits `ALTER TYPE ... ADD VALUE` for a new enum member
- **Index changes** — renames come out as drop + create, sometimes only one half
- **Server defaults** — needs `compare_server_default=True` in `context.configure`
- **Column type widening** — may emit nothing at all
- **Table/column renames** — always emitted as drop + create, which destroys data.
  Rewrite these by hand as `op.alter_column(..., new_column_name=...)`.

## Native enums — explicit create and drop

For the three enums in this schema (`job_status`, `agent_status`, `log_level`),
create the type explicitly in `upgrade()` before the table that uses it, and drop
it in `downgrade()` after the table:

```python
def upgrade() -> None:
    job_status = postgresql.ENUM(
        "queued", "running", "completed", "failed",
        name="job_status", create_type=False,
    )
    job_status.create(op.get_bind(), checkfirst=True)
    op.create_table("jobs", sa.Column("status", job_status, nullable=False), ...)

def downgrade() -> None:
    op.drop_table("jobs")
    postgresql.ENUM(name="job_status").drop(op.get_bind(), checkfirst=True)
```

`create_type=False` on the column-level type prevents a duplicate `CREATE TYPE`
attempt when the table is created. Omitting it causes
`DuplicateObject: type "job_status" already exists`.

To add a value to an existing enum, hand-write it —
`op.execute("ALTER TYPE job_status ADD VALUE 'paused'")` — and note that in
PostgreSQL this **cannot be reversed**, so `downgrade()` must either raise
`NotImplementedError` or rebuild the type.

## Every migration needs a working downgrade

No `pass` and no `raise NotImplementedError` in `downgrade()` unless it is
genuinely irreversible (enum value addition being the real case), and then say so
in a comment. A migration whose downgrade was never tested is not reviewed.

Test both directions before committing:
```bash
alembic upgrade head && alembic downgrade -1 && alembic upgrade head
```

## Naming

- Revision message: short and imperative — `"add rework_count to agent_runs"`
- Indexes: `ix_<table>_<columns>` matching the model-side `Index()` name exactly.
  A mismatch means autogenerate proposes a spurious drop/create on every run.
- One logical schema change per revision. Do not bundle unrelated changes.

## Never edit an applied migration

Once a revision has run anywhere it is immutable — write a new one. Editing it
leaves other environments with a version row pointing at content that no longer
matches, and `alembic current` will look correct while the schema is wrong.

## Data migrations

Schema and data changes go in separate revisions. Use `op.get_bind()` with
`sa.text()` and bound parameters for data backfills — never import and use the
ORM models inside a migration. Models evolve; the migration must keep working
against the schema as it existed at that revision.

Backfill large tables in batches, not one statement, so the migration does not
hold a long lock.

## Review checklist before committing a revision

1. `down_revision` points at the correct parent (not `None` unless it is the first)
2. Enum types are created before use and dropped after
3. Index names match the model definitions
4. `downgrade()` is implemented and tested
5. No ORM imports
6. Re-running `alembic revision --autogenerate` right after produces an **empty**
   migration — if it does not, the model and migration disagree
