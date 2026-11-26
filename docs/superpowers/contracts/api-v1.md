# RE Kit workspace API v1 (shared contract)

Binding for the Rust server (`src/api.rs`, `src/events.rs`, `src/domain.rs`) and the TypeScript client (`frontend/src/types.ts`, `api.ts`, `events.ts`). Changes need the lead's ruling. JSON uses `snake_case`; timestamps are RFC 3339 UTC strings; ids are opaque strings; revisions and cursors are unsigned 64-bit integers (cursors exceed no JS-safe range in practice and are sent as JSON numbers).

## Errors

Every non-2xx JSON body: `{"error": "<code>", "message": "<human text, no internals>"}`. Codes: `bad_request` 400, `forbidden_origin` 403, `not_found` 404, `stale_revision` 409 (adds `"current_revision": n`), `illegal_transition` 409, `payload_too_large` 413, `engine_unavailable` 502, `internal` 500. Request bodies are capped (64 KiB JSON on `/api/v1/commands`).

## Enumerations

- `JobStatus`: `queued | starting | running | awaiting_input | blocked | cancelling | cancelled | failed | succeeded | interrupted`
- Legal transitions: `queued→starting`, `starting→running|failed|interrupted|cancelling`, `running→awaiting_input|blocked|cancelling|failed|succeeded|interrupted`, `awaiting_input→running|cancelling|interrupted`, `blocked→running|cancelling|failed|interrupted`, `cancelling→cancelled|failed|interrupted`. `cancelled|failed|succeeded|interrupted` are terminal for the attempt. Retry creates a NEW attempt.
- `ProjectKind`: `reconstruct | inspect | convert`
- `Phase` is a free-form lowercase token owned by the workflow (for example `analyze`, `record`, `replay_original`, `front_door`, `unit_loop`, `replay`, `lift`); status and phase are independent. `outcome` is `null` until terminal, then `verified | unverified | failed`.

## Snapshot

`GET /api/v1/snapshot` (read-only, no mutation) returns:

```json
{
  "schema_version": 1,
  "cursor": 42,
  "generated_at": "2026-11-24T12:00:00Z",
  "projects": [{"id":"p_1","kind":"convert","name":"demo","revision":3,"created_at":"..."}],
  "jobs": [{"id":"j_1","project_id":"p_1","attempt_id":"a_1","attempt":1,"status":"running","phase":"record","outcome":null,"revision":5,"created_at":"...","updated_at":"..."}],
  "engines": {"fourmilab": {"state":"up|down|unknown","detail":"text"}, "inspector": {"state":"up|down|unknown","detail":"text"}},
  "releases": {"active": null, "previous": null, "staged": null}
}
```

`cursor` is the highest committed event id at read time; the client opens SSE with `after=<cursor>`.

## Commands

`POST /api/v1/commands`, headers `Content-Type: application/json`, `Idempotency-Key: <1-128 visible ASCII>` (required). Body:

```json
{"command":"create_project|start_job|cancel_job|retry_job|resume_job|resolve_decision",
 "project_id":"p_1", "job_id":"j_1", "expected_revision":3, "payload":{}}
```

`expected_revision` is the project's (or, for job commands, the job's) revision as last read. Mismatch → 409 `stale_revision`. Success `200`:

```json
{"project_id":"p_1","job_id":"j_1","revision":4,"cursor":43,"replayed":false}
```

Replaying the same Idempotency-Key with the same body returns the ORIGINAL receipt with `"replayed": true` and launches nothing; the same key with a different body → 409 `bad_request`-class `idempotency_conflict`. Each accepted command and its event commit in one transaction.

## Events (SSE)

`GET /api/v1/events?after=<cursor>`; `Last-Event-ID` overrides `after`. Each message: `id: <cursor>`, `event: workspace`, `data: <envelope JSON>`. Cursors are strictly increasing and gap-free per store.

```json
{"schema_version":1,"event_id":43,"project_id":"p_1","job_id":"j_1","attempt_id":"a_1","revision":4,"phase":"record","type":"job.status_changed","at":"...","payload":{"from":"starting","to":"running"}}
```

If `after` is older than retained history, the server sends one `event: reset` with `data: {"cursor": <high-water>}` and the client must reload `/api/v1/snapshot`. A `: heartbeat` comment every 15 s proves transport only. Large output is an artifact reference in `payload`, never inline bytes over ~16 KiB.

Event `type` values for core: `project.created`, `job.queued`, `job.status_changed`, `job.phase_changed`, `job.decision_requested`, `job.decision_resolved`, `artifact.published`, `engine.state_changed`, `release.changed`.

An `engine.state_changed` event reports on the engine, not a project. The server sends `project_id`, `job_id`, `attempt_id`, and `phase` as `null`, with `revision: 0`. Project and job events retain their project ID.

## Static assets and security headers

`/` and frontend routes (`/reconstruct`, `/inspect`, `/convert`, `/runtime`, `/releases` and children) serve `index.html` (`Cache-Control: no-cache`); `/assets/*` immutable-hashed (`public, max-age=31536000, immutable`). Unknown `/api/*` → JSON 404, never `index.html`. Mutating requests require `Origin` matching `Host` (or absent only for non-browser tooling with an explicit server flag). CSP: `default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'`; plus `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.

## Workflow namespaces (wave 2, not part of core)

`/api/v1/reconstruct/*`, `/inspect/*`, `/convert/*`, `/runtime/*`, `/releases/*`. Each module in `src/workflows/` exposes `pub fn router() -> axum::Router<crate::api::AppState>`; `api::router` merges all five.
