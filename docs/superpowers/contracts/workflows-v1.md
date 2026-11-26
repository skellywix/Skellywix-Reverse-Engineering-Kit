# RE Kit workflow API v1

**Status:** binding for the Reconstruct and Inspect Rust routes in this change.  
**Base path:** `/api/v1`. JSON names are `snake_case`. The shared error, origin, revision, event, and security-header rules in [`api-v1.md`](api-v1.md) apply.

## Common engine projection

Workflow state reads return HTTP 200 even when their legacy engine is unavailable. They expose the failure instead of returning empty success data:

```json
{"engine":{"state":"up|down","detail":"available|legacy engine unreachable|legacy engine timed out"}}
```

When `state` is `down`, workflow data and `legacy_evidence` are `null`. Mutations against a down engine return `502` with `error: "engine_unavailable"`.

The server polls each engine on the configured interval. A changed availability or legacy state appends `engine.state_changed`; a changed legacy run/case transition also appends the matching project/job event. The normalized state and its event commit together. The event payload points to the retained raw `legacy_evidence`; uploaded specimen bytes are never evidence and are never retained by Rust.

## Reconstruct

The only profile is the fixed Fourmilab Base64 1.5 compatibility contract. Its `required_cases` is always `2557`; these routes do not accept executable, profile, model, effort, image, coordinator, verifier, builder, or release-pin overrides.

### Read state

`GET /api/v1/reconstruct/state?run=<32 lowercase hex>`

`run` is optional. The response is:

```json
{
  "schema_version": 1,
  "engine": {"state":"up","detail":"available"},
  "profile": {
    "id": "fourmilab_base64_1_5",
    "name": "Fourmilab Base64 1.5",
    "fixed": true,
    "required_cases": 2557
  },
  "runs": [
    {"id":"<32 hex>","attempt":1,"status":"verified","stage":"package"}
  ],
  "selected_run": {
    "id": "<32 hex>",
    "attempt": 1,
    "status": "verified",
    "stage": "package",
    "stages": [
      {"name":"intake","status":"completed","completed":null,"required":null,"detail":{}},
      {"name":"analyze","status":"completed","completed":2557,"required":2557,"detail":{}},
      {"name":"rebuild","status":"completed","completed":null,"required":null,"detail":{}},
      {"name":"verify","status":"completed","completed":2557,"required":2557,"detail":{}},
      {"name":"package","status":"verified","completed":2557,"required":2557,"detail":{}}
    ],
    "proof": {"matched":2557,"required":2557,"verified":true},
    "model": {"requested":"<pinned model>","actual":"<receipt model or null>"},
    "package": {"available":true,"sha256":"<64 lowercase hex or null>"}
  },
  "releases": {"active":null,"previous":null,"staged":null,"items":[]},
  "catalog": [],
  "busy": false,
  "running": null,
  "update_error": null,
  "builder_connected": true,
  "legacy_evidence": {}
}
```

`selected_run` is `null` when no run is selected. The five stage rows are always ordered `intake`, `analyze`, `rebuild`, `verify`, `package`; an unseen stage has `status: "pending"` and null counts. Rows reflect only the selected run's current attempt. `proof.verified` and `package.available` are true only when the legacy package event is `verified`, its receipt belongs to the current run and attempt, and `matched == required == 2557`. `model.actual` comes from the legacy model receipt; it is never copied from `model.requested` as a fallback. `legacy_evidence` is the unmodified `/state` object.

### Run actions

All bodies use `Content-Type: application/json` and are exactly the shown object; extra fields are rejected.

- `POST /api/v1/reconstruct/start` with `{}`.
- `POST /api/v1/reconstruct/runs/<run>/cancel` with `{}`.
- `POST /api/v1/reconstruct/runs/<run>/retry` with `{}`.

Success preserves the legacy receipt:

```json
{"ok":true,"run":"<new or retried 32-hex run, when supplied by the engine>"}
```

### Release actions

- `POST /api/v1/reconstruct/releases/stage` with `{}`.
- `POST /api/v1/reconstruct/releases/promote` with `{"target_digest":"<64 lowercase hex>","confirm_target":"<same digest>"}`.
- `POST /api/v1/reconstruct/releases/rollback` with `{"target_digest":"<64 lowercase hex>","confirm_target":"<same digest>"}`.

Promote requires both fields to match. Rollback additionally reads current engine state and requires `target_digest` to equal `releases.previous` immediately before invoking the legacy `rollback` action. A missing or mismatched confirmation returns `400 bad_request`; a changed or unavailable previous target returns `409 illegal_transition`. Rust never edits the legacy selector or SQLite database.

### Verified package

`GET /api/v1/reconstruct/runs/<run>/package`

Rust first reads that exact run and applies the verification predicate above. If it is not satisfied, the response is `409 illegal_transition` and no package request reaches the legacy engine. A verified response streams the legacy body without buffering it in Rust:

- `Content-Type: application/zip`
- `Content-Disposition: attachment; filename="fourmilab-base64-verified.zip"`

A mid-stream legacy failure terminates the stream; it is never converted into a successful empty package.

## Inspect

The inspector remains passive and revisioned. Valid tool names and argument objects are exactly:

```json
{"tool":"identify_file","arguments":{"slot":"a|b"}}
{"tool":"compare_builds","arguments":{}}
{"tool":"verify_checksum","arguments":{"slot":"a|b","sha256":"<64 hex>"}}
```

### Read state

`GET /api/v1/inspect/state`

```json
{
  "schema_version": 1,
  "engine": {"state":"up","detail":"available"},
  "state": {
    "schema":"yuzu.report/1",
    "revision":1,
    "slots":{},
    "events":[],
    "earlier_events_omitted":0,
    "connection":"off|ready|connected|expired|spent",
    "remaining":0,
    "advisor":{}
  },
  "legacy_evidence": {}
}
```

`legacy_evidence` is the unmodified `/api/state` object. `state` is the same object projected for the workspace client. Both are `null` when the engine is down; stale prior answers are not returned.

### Upload specimens

- `POST /api/v1/inspect/samples/a`
- `POST /api/v1/inspect/samples/b`

Required headers:

- `Content-Type: application/octet-stream`
- `Content-Length: 0..16777216`
- `X-Workspace-Revision: <unsigned integer>`

The body is streamed once into the adapter. Rust rejects a missing/invalid length or a length above 16 MiB before opening the legacy socket. Exactly 16 MiB is accepted. B before A is rejected by the inspector. Replacing A retains the inspector's behavior: it clears B, evidence history, advisor state, and the case key. Success returns the updated inspector state directly.

### Run and reset

- `POST /api/v1/inspect/run` with `{"revision":1,"tool":"identify_file|compare_builds|verify_checksum","arguments":{}}`; returns `{"result":{},"state":{}}`.
- `POST /api/v1/inspect/reset` with `{"revision":1}`; returns the updated state.

A stale revision returns `409 stale_revision` with the legacy message and `current_revision` from the newest observed inspector state. Reset never restarts the inspector process.

### Report, review, and critic

- `GET /api/v1/inspect/report` returns the current `yuzu.report/1` object with `Content-Disposition: attachment; filename="skellywix-re-kit-evidence.json"`.
- `POST /api/v1/inspect/review` with `{"revision":1,"reference":null|"<64 hex>","critique":false}` returns the updated state.
- `GET /api/v1/inspect/critic` returns `{"revision":1,"loaded":true,"status":"idle|not_requested|pending|reviewing|ready|failed|expired|cancelled|stale"}`.
- `POST /api/v1/inspect/critic/claim` with `{"revision":1}` returns `{"brief":{},"ticket":"<opaque>"}`.
- `POST /api/v1/inspect/critic/finish` with `{"revision":1,"ticket":"<opaque>","note":null|{"text":"...","cites":[1]}}` returns critic status.
- `POST /api/v1/inspect/critic/cancel` with `{"revision":1,"end":1}` returns the updated state.

Rust forwards only these closed objects. It does not expand a critic brief, add work, or expose sample bytes, paths, hashes, case keys, or model credentials.

For a non-null critic note, Rust requires exactly `text` and `cites`: nonblank text of at most 320 Unicode scalar values with no control characters, and one to three distinct positive integer finding numbers. Invalid notes return `400` before Rust contacts the inspector. The inspector still checks printability, the ticket, revision, and whether the cited findings remain in the case.

### Case key

- `POST /api/v1/inspect/pair` with `{"revision":1}` returns `{"token":"<opaque>","state":{}}`.
- `POST /api/v1/inspect/disconnect` with `{"revision":1}` returns the updated state.

The token is returned only by `pair`; Rust does not log or persist it. Expiry, 32-call exhaustion, session rules, and revocation remain inspector-owned.

## Adapter and deployment settings

The binary accepts these flags, with equivalent environment variables:

| Flag | Environment variable | Default |
|---|---|---|
| `--fourmilab-socket` | `RE_KIT_FOURMILAB_SOCKET` | required |
| `--inspector-desk-socket` | `RE_KIT_INSPECTOR_DESK_SOCKET` | required |
| `--inspector-agent-socket` | `RE_KIT_INSPECTOR_AGENT_SOCKET` | required |
| `--legacy-poll-interval-ms` | `RE_KIT_LEGACY_POLL_INTERVAL_MS` | `2200` |
| `--legacy-connect-timeout-ms` | `RE_KIT_LEGACY_CONNECT_TIMEOUT_MS` | `2000` |
| `--legacy-read-timeout-ms` | `RE_KIT_LEGACY_READ_TIMEOUT_MS` | `30000` |

All durations must be positive bounded integers. The server never writes a legacy SQLite file and never starts, restarts, stops, or signals a legacy engine.
