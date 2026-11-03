# apex-common

Infrastructure shared by the three apex Python services. A library, not a
service: no `Dockerfile`, no FastAPI app, no `.env`, nothing to deploy.

## Why it exists

`observability.py`, `pagination.py`, `title_norm.py` and `doc_status.py` were
duplicated across `ingestor-service`, `heavy-processing-engine` and
`task-service-agent`, each copy pinned by an identical hand-written fixture
table — the only mechanism available to services that share no library.

That mechanism had already failed. `normalize_title` reached **three** copies,
and the third (the eval harness's) collapsed every non-alphanumeric character
rather than only `\s_-.`, while its docstring claimed it mirrored the ingestor.
The two disagree on any filename with an apostrophe, and one in the live corpus
did. The bug was latent rather than broken only because the harness folded both
sides of its own comparison.

The decision record for the wider "one engine, many apps" question is
`task-service-agent/base_engine_configuration.txt`.

## What belongs here

Two rules, and the first is the one that matters:

1. **It depends on nothing of ours.** No `app.config`, no `app.database`, no
   service import — stdlib and pydantic only. Anything needing configuration
   takes it as an **argument**. The moment this package imports from a service,
   it stops being common and becomes a cycle.
2. **It is already byte-identical in two or more repos.** That is the admission
   test — not "this looks reusable", which is how a shared-utils grab-bag
   starts.

### Deliberately NOT here

`token_denylist.py` (JWT revocation) is code-identical in `ingestor-service` and
`task-service-agent`, so it looks like the obvious fifth module. It fails rule
1: it reaches into `app.database.get_database()` and
`app.config.get_settings().refresh_token_expires_in`, both lazily inside
functions. Admitting it needs those injected first — a `configure(...)` call at
startup, or both passed per call.

That refactor is deliberately **not** bundled with the extraction: it changes
the shape of a security-sensitive module that both services import, so both
would have to move together, and a logout path that behaves differently in two
services mid-rollout is the wrong thing to have in flight. Do it as its own
change, with both suites green before and after.

## Consuming it

```toml
"apex-common @ git+https://github.com/dilipl/apex-common.git@v0.1.0",
```

A **tag**, never a branch. Each service upgrades on its own schedule — that is
what stops one app's deploy from dragging another's, and it is the same reason
each app pins its own service image tag.

Services import the modules directly (`from apex_common.title_norm import
normalize_title`). There are no re-export shims in the services, so there is
exactly one place a name can come from, and each service keeps a **repo guard**
(`tests/test_shared_modules.py`) asserting it defines none of these itself —
re-adding a local copy is the precise failure this package exists to prevent.

`__init__.py` exports nothing but `__version__` on purpose: a flat re-export
surface would make every consumer import pydantic and the logging machinery to
use one regex, and it hides which module owns a name.

## Tests

`pytest` — the library's own behaviour: the contract tables, the contextvar
isolation under concurrency, the paging arithmetic. A service's wiring of a
module (its middleware, its routes) is tested in that service's suite, against
its own app.

Field names in `observability` and `pagination` are a **cross-service
contract** — one trace, and one admin screen, span all three services. Renaming
one breaks every cross-service query silently; the contract tests make it break
a test instead.
