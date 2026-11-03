# apex-common

Infrastructure shared by the apex services — `ingestor-service`,
`heavy-processing-engine` and `task-service-agent`.

Each of these modules was previously **copied into every service** and held in
place by a hand-written, identical fixture table per repo, because the services
shared no library. One of them had already drifted silently (see
`title_norm.py`). They now share this package.

| module | what it is |
|---|---|
| `observability` | one JSON log line per record, plus the `request_id` / `document_id` contextvars that let a document's life be traced across all three services and the queue |
| `pagination` | the five paging fields every list endpoint returns, a measured (never inferred) `has_more`, and bounded counting |
| `title_norm` | the filename-folding rule `file_name_norm` is stored and searched by |
| `doc_status` | maps raw processing rows to the readiness a user sees — "can I query this document yet" |

## Install

```toml
# pyproject.toml of a consuming service
"apex-common @ git+https://github.com/dilipl/apex-common.git@v0.1.0",
```

Pin a **tag, never a branch**: each service upgrades on its own schedule, so one
app's deploy cannot drag another's.

Local development against an unreleased change:

```bash
pip install -e ../apex-common          # inside the service's venv
```

## Test

```bash
pip install -e '.[dev]'
pytest
```

The tests here are the **library's behaviour** — the contract tables, the
concurrency isolation, the paging arithmetic. How a given service wires a module
into its routes is tested in that service's own suite.

## Versioning

Semver on the module APIs. A change to `title_norm`'s folding rule is **not** a
version bump alone — it is a data migration in every service that has stored
`file_name_norm`. See that module's docstring.
