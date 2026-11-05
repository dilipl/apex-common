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

## `gcp_preflight` — v0.3.0

The last duplicated module: byte-identical in **all three** services, which
each called `verify_credentials()` at startup (hpe from its worker too).

**Renamed on the way in.** It was `app/services/auth_preflight.py`, and it is
about GOOGLE CLOUD credentials — a stale local `impersonated_service_account`
snapshot — not about authenticating a user. Sitting beside the JWT code under
an "auth" name is what kept that confusion alive, and it is why this did not
go into `apex_common.auth`.

Pure stdlib, so it is **core, not an extra**. `_FIX_SCRIPT` names a path inside
task-service-agent on purpose: the script exists only there, and one run of it
repairs the shared ADC snapshot every service reads.

hpe was the only repo with a test for it (`test_auth_preflight.py`); that is
now `tests/test_gcp_preflight.py` here, run once.

## `apex_common.email` — v0.4.0, attachments in v0.5.0

The transport, not the message. ingestor-service emails a note or a transcript
a user asked for; task-service-agent emails a digest it generated. How mail
leaves the building is infrastructure; subject lines and layout are domain and
stay in each service.

**No vendor, and no dependency.** Every hosted provider speaks SMTP and
`smtplib` is stdlib, so this module has no dependency and therefore **no
optional extra** — unlike `auth`. A provider's REST client later is what would
need one, and the `Mailer` protocol is what makes that a new class rather than
a change at the call sites.

**It raises; it does not swallow.** `send` raises `EmailError` and each caller
decides what that means, because the right answer differs: a user waiting for
"email me this entry" must see a failure as a failure, while a digest loop must
log one bad address and continue. A library that logged-and-returned makes the
first case impossible — and this codebase has already paid for a
degrade-silently default, when output bodies written only to object storage set
`body_key = ""` on failure while the row still said `completed`, and two
reports were lost without a trace.

**Unconfigured is `None`, not a mailer that fails.** `get_mailer` returns None
when `MAIL_PROVIDER` is blank, so a service MOUNTS its "email this" route only
when it can actually send — the same principle the agent catalogue applies, and
it means the frontend discovers what exists instead of offering a button that
500s. A MISSPELLED provider, by contrast, **raises**: falling back to disabled
would deploy a service that quietly never emails anybody.

Misconfiguration raises from the CONSTRUCTOR, so a bad deployment fails at
startup in front of an operator rather than at 3am on the first digest — the
same reasoning as `auth`'s cookie-policy check.

`console` logs the message instead of sending it, for local work on a template
without SMTP credentials. It applies the same header rules as SMTP, so an
injection cannot pass in the environment where it is hardest to notice.

**Attachments (v0.5.0).** `Attachment(filename, content, content_type)`, because
"email me this output" means the file — the PDF renderer already produces one
for every kind. The filename is **validated, not sanitised**: it reaches a
header AND the recipient's filesystem, so a path separator or a newline is
refused rather than quietly rewritten, and an empty attachment is refused
because a zero-byte PDF means the render produced nothing and sending it tells
the user their output is empty. The size cap (`max_bytes`, 10 MB) is checked on
the **ENCODED** message, since base64 inflates a file by about a third — capping
the raw bytes would let through a message the provider accepts and then bounces,
which is a failure the person who clicked never sees.

**Header injection is refused, not stripped.** A subject is often built from
user text (an entry title, a filename); a newline in a header value appends
headers of the caller's choosing. Rejected with `EmailError` rather than
silently rewritten, and bodies are unrestricted — a diary entry is full of
newlines and may well contain the word `Bcc`.

## `doc_status` gained `stored` — v0.4.0

Written by **ingestor**, not hpe, and only where `VECTORIZE_ENABLED=false`: no
queue, no worker, no vectors. It maps to **READY**, because the reader's
question is unchanged — can I use this yet — and the answer is yes immediately.

A new value rather than reusing `completed` on purpose: `completed` means "has
vectors, retrievable by semantic search", so writing it with none would make
the badge a lie and mislead the next person to read the collection. The
distinct value keeps WHY recoverable from the row itself.

`DOC_STATUS_CONTRACT` moved to `2026-11-05`. Nothing in the three services pins
that constant, so the bump is contained here — but it is what tells you which
version of the rule a consumer's STORED data was written under.

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
