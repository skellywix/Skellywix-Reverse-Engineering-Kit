# Analyze behavior for a rewrite

Use the original source when you have it. Use observations when you don’t, or use both to check whether the source explains the behavior you measured. `tools/semantics.py` runs locally with the Python standard library. It reads the files you name; it doesn't import, compile to an executable, or run the target, contact a model, or access the verifier registry.

From the repository root, try the [dial example](../examples/semantic-dial/). Each command works independently except the final comparison, which uses the spec created in step 2.

1. **Read original source.** No trace, license declaration, or second implementation is required.

   ```sh
   python -IB tools/semantics.py analyze --source examples/semantic-dial/reference.py
   ```

2. **Describe what was observed.** This creates a file for an implementer who doesn’t need the original. `--out` creates a new file; omit it to print JSON.

   ```sh
   python -IB tools/semantics.py analyze examples/semantic-dial/reference.json --out dial-spec.json
   ```

3. **Study source alongside measurements.** `source_analysis` comes from the selected source file; `observed` comes from the trace. Supplying both associates them for review. It doesn't prove a binary was built from that source.

   ```sh
   python -IB tools/semantics.py analyze examples/semantic-dial/reference.json --source examples/semantic-dial/reference.py
   ```

4. **Check a candidate.** The checker needs only the spec and candidate observations. It also accepts a combined spec, but never treats source annotations or inferred rules as test results.

   ```sh
   python -IB tools/semantics.py check dial-spec.json examples/semantic-dial/candidate.json
   ```

The example has seven reference observations and six distinct transitions. In the ready state, inputs −2, 0, and 3 to `scale` suggest `output = 3 × input + 2`. The candidate matches all six observed transitions; its result for input 4 is **unassessed**. Measuring the reference at 4 gives 14, while the candidate gives 11. Adding that reference row turns the same comparison into a mismatch. Neither report establishes equivalence beyond its observations.

## Source analysis

A Python source report identifies the exact source bytes by SHA-256. It lists `if` predicates, return values, and calls named like stream operations or single-byte counts, with enclosing scopes and line numbers. Simple integer expressions such as `x + x + 1` become a multiplier and offset. The expression refers to the variable **at that site**, assuming ordinary integer operations; it is not a statement about all callers, Python overloads, or reachability. `enclosing_if` identifies the nearest lexical `if` arm, not a complete execution path. `unresolved` means the analyzer didn't interpret that expression.

`.py`, `.pyw`, and `.pyi` select the Python parser. Use `--language python` for a different filename. Other source files can be attached and hashed: `identity-only` means no static semantics were extracted. Observation analysis is language independent. Source files and traces are limited to 1 MiB each. A source report marked `partial` reached its fact output limit.

Source review can include original identifiers and short literal values. It omits comments and whole function bodies, but **it is source-informed, not a source-blind handoff**. The existing [Convert packet](superpowers/contracts/source-port-v1.md#operator-facing-operations) remains available and includes the original source.

## Record and compare measurements

Record one reproducible action per row. The state and action names are labels you choose; `next_state` is the state you observed after the action. For a stateless interface, use the same label on both sides. Keep the original capture, artifact, environment, and the mapping from labels to public actions in the analyst’s records. Keep held-out tests separate.

| Trace field | Meaning |
|---|---|
| `format` | `behavior-trace-v1`. See the [full example](../examples/semantic-dial/reference.json). |
| `interface` | The same interface label in both captures. |
| `artifact_sha256` | Digest of the artifact the operator measured. The tool hashes the trace itself; it doesn't authenticate the operator’s artifact or measurements. |
| `basis` | Optional: `black-box` for externally chosen probes or `source-informed` if reading source helped design them. Omit it for `unspecified`. In all three cases, rows are reported **measurements**, not predictions. |
| `observations` | 1–1,024 rows, each with `state`, `action`, `input`, `next_state`, and `output`. Labels start with a lowercase ASCII letter and use at most 32 lowercase letters, digits, or hyphens. Inputs and outputs take a label, a signed 32-bit integer, or JSON `null`. |

Repeated observations retain their one-based row numbers. If the same state, action, and input produce different effects, the tool reports the conflicting rows rather than choosing one. Check for missing state or nondeterminism. Integer hypotheses require at least three distinct inputs, one next state, and an exact fit across the entire state/action group. They are not extra test cases, even inside the sampled range.

`check` exits **0** when every reference case matches, **1** for a mismatch or missing reference case, and **2** for invalid input or an I/O error. Differences include the stimulus and both effects. Additional candidate stimuli are counted as `unassessed`; repeats don't inflate the count. Rechecking the same artifact is allowed and labeled `same_artifact`. The checker hashes the spec and candidate capture; it neither issues a Convert receipt nor changes a download gate.

## Source access and licensing

If you own the source or its license permits the intended reuse, use it directly under those terms. An independent rewrite is an option, not a requirement imposed by the Kit. If you choose a clean-room process, the analyst can retain the source and raw evidence, review what may be shared, and give the implementer just the behavior spec. Review labels and examples too: the tool can't tell whether someone copied them.

U.S. copyright distinguishes program expression from functional ideas and methods of operation ([17 U.S.C. § 102(b)](https://www.law.cornell.edu/uscode/text/17/102), [Copyright Office Circular 61, p. 1](https://www.copyright.gov/circs/circ61.pdf)). These reports don't grant rights, verify permission, remove license or notice requirements, or clear bundled assets. Keep the applicable license and notices with your records and review the intended distribution separately.
