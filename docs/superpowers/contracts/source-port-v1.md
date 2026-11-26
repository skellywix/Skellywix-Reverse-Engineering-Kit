# Reviewed-source conversion v1

This contract defines the workspace’s [Convert workflow](../../../docs/design.md#product-surfaces); it isn't a deployment record. The [Rust export check](../../../src/artifacts.rs) evaluates already-trusted inputs. A project is a reviewed, operator-installed source profile, not a repository URL, executable, or command. The model may propose Rust source changes only. Only the operator can approve a proposal, outside this API. Neither the browser nor the model can register a profile, approve a proposal, write verifier evidence, or change a release selector through these endpoints.

## Sources of authority

- The protected registry supplies the project ID, display name, original source, public proposal brief, fixed recipe and toolchain identities, and comparison corpus. The brief excludes held-out cases and credentials. The operator can give this packet to a model with no tools; only the returned source text enters an attempt. The packet is not sent to an external service by opening a page.
- A separate operator approval names one exact target. A verifier checks it before compiling. It executes only the profile's fixed offline build and comparisons, never commands in a packet. A new attempt cannot reuse an approval or result. Only the verifier can write a result record; neither candidate code nor the web server can write the registry, approvals, or results.
- The control plane owns attempt IDs, selected proposals, revisions, and the build queue. It authorizes export again when serving the bytes, not just when showing a link. Keep raw candidate bytes immutable; never reopen another file after checking its digest.

The server freezes the candidate file before committing its selection in SQLite. A crash or ambiguous commit can leave an unselected candidate directory. Without a committed comparison and its matching `job.json`, the directory is not a job or an export. Keep it for offline reconciliation against the database; do not delete an attempt during recovery merely because the caller didn't receive a response.

## Export gate

`Target` contains `project_id`, `attempt_id`, `source_sha256`, `candidate_sha256`, `corpus_sha256`, `recipe_sha256`, and `toolchain_sha256`. IDs are nonempty; SHA-256 values on disk and on the wire are exactly 64 lowercase hexadecimal characters. The target binds the current proposal to the installed profile. Approval and result each independently name the complete target. Digests are over exact bytes without line-ending conversion.

The protected profile lists a nonempty set of named cases, sorted by ID, and includes at least one held-out case. Each expected row has `id`, `held_out`, and `expected_sha256`. A comparison result has `target`, `build_exit` (integer or null), and `cases`. Each result row has `id`, `original_sha256`, `replay_sha256`, `candidate_output_sha256`, and `used_fallback`. A missing run produces no successful observation. The recipe defines byte framing for each observable and pins it with the corpus. For a CLI, compare exit status and complete stdout/stderr bytes; for an HTTP response, compare the specified status, headers, and body. An output limit or timeout is a failed measurement, not a matching prefix.

Require all three targets (current, approved, result) to match; build exit zero; exactly one result in canonical order for every protected case; matching expected, original, replay, and candidate outputs; no fallback; and a recomputed digest matching the actual artifact being served. Never infer success from counts alone, a phase name, or a submitted `verified` flag. A pass certifies only the frozen suite.

## Operator-facing operations

The shared [API rules](api-v1.md) apply. All workflow reads are side-effect free. Every mutation uses an expected revision and an idempotency key. Failures include a reason; unavailable or incomplete data never appears as a completed build.

`revision` counts accepted control-plane changes, not observations. Operator approval and verifier results can change without a new proposal or comparison request. A fresh read at the same revision must preserve the frozen attempt identity and bytes, but can change its approval, phase, counts, blocker, and download eligibility. Neither a refresh nor an idempotent retry may select an old attempt. An exact retry after its attempt was superseded receives `409 stale_revision`.

| Operation | Behavior |
| --- | --- |
| `GET /api/v1/convert/projects` | List registered profiles and their source identities. An empty registry has no selectable projects. |
| `GET /api/v1/convert/projects/{project}` | Return the selected project's current attempt, revision, phase, proposal identity and text (if any), measured matched/required counts, blocker, and export eligibility. |
| `GET /api/v1/convert/projects/{project}/packet` | Download the exact original source and public proposal brief for this profile. No held-out values, command interface, or implicit provider call. |
| `POST /api/v1/convert/projects/{project}/proposals` | Accept only `expected_revision`, `source_sha256`, and `rust_source` (bounded UTF-8 source). Freeze a new attempt; do not approve or execute the proposal. |
| `POST /api/v1/convert/projects/{project}/compare` | Accept only `expected_revision`, `attempt_id`, and `candidate_sha256`. Queue the exact candidate only if a separate protected approval exists. A request is not evidence that a build ran. |
| `GET /api/v1/convert/projects/{project}/attempts/{attempt}/source` | Serve the exact checked Rust source as a download only for the current passing attempt. Otherwise return 409, or 404 for an unknown project/attempt. |
| `GET /api/v1/runtime/state` | Report which fixed verifier recipes are configured and which have actual check results. No start, exec, install, or relay route. |

Runtime and Convert never call Fourmilab or Inspect. A missing verifier, unstable baseline, unreviewed candidate, unavailable behavior, or mismatching case blocks export. The deployed desks and their data do not change when a local profile or proposal is tested.

### JSON shapes

- Project list: `{"schema_version":1,"projects":[{"id":"line-count","name":"Line count","source_sha256":"…"}]}`.
- Project detail: `{"schema_version":1,"project":{"id":"line-count","name":"Line count","source_sha256":"…"},"revision":0,"phase":"registered","attempt":null,"matched":null,"required":3,"blocker":null,"export_available":false}`. The `project` object matches its list row. When present, `attempt` is `{"id":"…","candidate_sha256":"…","rust_source":"…","approved":false}`. `matched` is null without measurements. `required` is the registered number of cases, not an estimate. `blocker` is null or plain text. Phases: `registered`, `awaiting_review`, `approved`, `queued`, `comparing`, `mismatch`, `blocked`, and `verified`. Only `verified` can offer an export. Both POSTs return the resulting project detail. A repeated idempotent POST does not enqueue a second comparison.
- Proposal packet: `{"schema_version":1,"project_id":"line-count","source_sha256":"…","language":"python","source":"…","brief":"…"}`. Render code and brief as plain text.
- Runtime: `{"schema_version":1,"recipes":[{"project_id":"line-count","name":"Line count","recipe_sha256":"…","toolchain_sha256":"…","state":"configured","detail":"No build recorded."}]}`. State is `configured`, `exercised`, `unavailable`, or `failed`. Only trusted verifier observations can yield `exercised`.
- A source download has `Content-Type: text/plain; charset=utf-8`, `Content-Disposition: attachment; filename="port.rs"`, and `Cache-Control: no-store`. Errors use the shared JSON error shape. Ellipses above stand for real source text or a full digest, never values to return.
