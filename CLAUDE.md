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

## `apex_common.auth` — v0.2.0

The whole auth surface, not just the denylist. The two services held ~394 code
lines each, **~94% identical**: `register` / `login` / `refresh` / `logout` were
byte-identical logic differing only in import ORDER. They are two JWT REALMS
over one `users` collection, differing in exactly two settings —
`cookie_prefix` and `jwt_secret_key`.

It is an **optional extra** (`apex-common[auth]`), because
heavy-processing-engine is a private Pub/Sub worker with no authentication and
must not pull fastapi and bcrypt in to format logs.

**This package does not import `fastapi_jwt_auth`.** The consumer passes its
`AuthJWT` class into `install()`. The service keeps ownership of that
dependency's version — it is a git dependency pinned upstream to HEAD, which is
not a risk a shared library should carry on a consumer's behalf.

`install()` registers PROCESS-GLOBAL state on the `AuthJWT` class (its config
loader and denylist loader), because that is the only interface the library
offers. Call it once, from the module that owns a service's auth.

**What stays with the service:** its `AuthConfig`, its collections, and any
dependency that reads its own records — `require_project_member`,
`require_project_owner`. A service's `app/auth/oauth2.py` becomes ~100 lines of
wiring that re-exports the rest, so its routers do not change.

### Three drifts the copies had already grown

Fixed in place before extraction, so neither copy's bug was shipped to both:

| | |
|---|---|
| a stray `print(error)` | in one service's auth exception path, bypassing JSON logging |
| `"User no longer exist"` | one 401 message, two spellings |
| `TWELVE HOURS` | a docstring describing the *other* service's refresh-token life, wrong — both set 43200 minutes |

The third is the shape that matters: a duplicated file whose comment
confidently describes its twin, incorrectly, about how long a leaked
credential lives.

### And one the extraction itself found

The cookie-policy guard — `SameSite=None` without `Secure` — was raised from
**inside** `AuthJWT.load_config`'s callback. That method wraps its callback in
`except Exception: raise TypeError("Config must be pydantic 'BaseSettings' or
list of tuple")`, so the message never reached anyone: a misconfigured
deployment got a complaint about the config object's TYPE, not about the cookie
settings that were wrong. Both services shipped it that way, which defeated the
entire purpose of a guard whose job is to turn a mystifying outage into a
message. `install()` now runs the check **before** registering the loader, and
`tests/test_auth.py` pins that a `RuntimeError` naming `COOKIE_SECURE` is what
escapes.

### Still deliberately NOT here

`auth_preflight.py` — 83 lines, byte-identical in both services, and a clean
candidate. It is about **GCP** credentials (stale local impersonation configs),
not user auth, so it does not belong in this subpackage. Worth moving, under
its own name.

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
