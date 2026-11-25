# Skellywix RE Kit

File inspection is the part of Skellywix RE Kit that examines files without running them. The standalone inspector prints the byte count, SHA-256, and a signature label as one JSON record. It recognizes a few common byte prefixes; it does not parse, extract, disassemble, or assess the safety of a file.

## Start the local desk

On your own **single-user Linux workstation**, use a non-root account with access to Docker Engine, Bash, curl, and the systemd socket tools. From the repository root:

```sh
bash static-inspection/start.sh
```

Open the loopback URL the launcher prints. Try two ordinary text files in **COMPARE**. The UI is prebuilt; you don’t need Node.js, an SSH server, a model, or an API key. The first launch downloads the pinned worker image if missing. Later launches can run offline. Press Control-C to stop the desk and discard the case. To use another pair of adjacent local ports, pass the first port, for example `bash static-inspection/start.sh 48900`.

Don’t start the desk on a shared machine or publish its ports. For a separate Docker host, see [remote access](#remote-access). The standalone CLI below doesn’t need Docker. All commands below this point run from `static-inspection/`.

## Inspect a file

Use Linux with procfs and Python 3.12 or later. No installation, third-party packages, service, model credentials, network connection, or administrator privileges are needed. Keep `inspect_file.py` in a trusted directory separate from the samples.

From the directory containing `inspect_file.py`, try a three-byte fixture:

```sh
inspector="$(pwd -P)/inspect_file.py"
intake="$(mktemp -d)"
printf abc > "$intake/sample.bin"
(cd "$intake" && python3 -I "$inspector" sample.bin ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad)
```

```json
{"version":1,"size":3,"sha256":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad","signature":"unknown"}
```

For another file, start in its directory and pass its basename to the trusted script. The optional second argument is an expected lowercase SHA-256. A digest mismatch fails rather than producing a report. `pdf`, `zip`, `elf`, `mz`, and `png` describe prefixes only; `unknown` is a valid result.

The inspector refuses links, directories, special files, unsafe names, files larger than 2 GiB, and visible changes during the read. Exit `0` means a report was produced; `1` means inspection was refused or the input could not be read; `2` means invalid invocation. No partial report is printed on an input failure. See [SECURITY.md](SECURITY.md) for the trust boundary.

## Test

```sh
python3 -IB -m unittest discover -s tests -v
```

## File inspection

**INSPECT** has three tools: **IDENTIFY** reports file metadata, **COMPARE** shows differences between A and B, and **CHECK HASH** checks a SHA-256 against an independent reference. The worker accepts up to two 16 MiB files, retains only their size, SHA-256, format, and architecture, and never executes them. The standalone CLI above is unchanged.

On wide screens, file intake sits beside the selected tool. On phones, files, the check result, the evidence ledger, and optional agent access appear in that order. Each comparison keeps A (reference) and B (comparison) together for each field. “Differs” marks a difference; it isn’t a safety verdict.

In a paired workspace, **BUILD** and **INSPECT** open in the same tab. The legacy root opens BUILD; `/index.html` opens the file tools. A standalone inspector has no BUILD link. Neither view reads the other service’s API.

The mascot and all art are local. The counter tracks findings, not visitors. Tap Skellywix to pet her or drag her to a corner. Under **Skellywix options**, recap the case, offer a treat, let her nap, or minimize her. Care and recaps never start a tool or change the case. Escape closes the message or menu.

The [lossless sprite sheet](workbench/ui/public/skelly.webp) has four transparent, hand-inked-style anime cels: tablet and delighted above, treat and nap below. [Dialogue and descriptions](workbench/ui/skelly-dialogue.json) are local. The [browser regression](tests/skelly-browser.cjs) exercises the pet without changing case data. With the desk on its default port and Playwright installed in your test environment, run `node tests/skelly-browser.cjs`.

### Remote access

Run the worker on a trusted, single-user Linux Docker host. The checked-in UI is ready to use. If you change its source, rebuild with `(cd workbench/ui && npm ci --ignore-scripts && npm run build)`. See [third-party notices](THIRD-PARTY-NOTICES.txt) for the bundled client.

```sh
docker pull "$(cat workbench/worker-image.txt)"
ipc=$(mktemp -d)
worker=$(bash workbench/run.sh "$ipc")
printf 'IPC directory: %s\nWorker: %s\n' "$ipc" "$worker"
```

For a paired installation, use `bash workbench/run.sh "$ipc" --paired` instead of the worker command above. Use a fresh private IPC directory; don’t stop a worker holding an open case. Forward both inspector sockets as shown below and start the [Neverball build desk](../README.md#windows-game-profile-neverball-160) on its fixed loopback address. The worker adds navigation only to the HTML it serves, not to the standalone bundle.

From the operator's computer, forward two adjacent **loopback-only** ports to the printed directory. Replace `HOST` with the SSH host and `IPC` with that absolute directory in both socket paths. Keep SSH open:

```sh
ssh -NT -o ExitOnForwardFailure=yes -L 127.0.0.1:48730:IPC/desk.sock -L 127.0.0.1:48731:IPC/agent.sock HOST
```

Open `http://127.0.0.1:48730/`. Choose file A and select **Load A**; load B to compare the files. For one coordinated pass, open **Run the case with Skellywix**, optionally enter an independently obtained SHA-256 for A, then select **Start case review**. Skellywix identifies each file, compares the two if present, and checks A only when you supply a reference. The pass stops after at most four local metadata checks. It doesn't loop or dispatch arbitrary agents.

The **Evidence ledger** distinguishes your entries, Skellywix's checks, and paired-agent calls. It retains details for the latest 64 findings and totals for the whole revision, including earlier checksum mismatches. A later match doesn't erase a mismatch. Loading a file starts a new revision. Export the evidence before replacing a file, resetting, or stopping the worker.

### Attach an independent critic

Model review is optional and off by default. Use a trusted OMP session on the operator's computer with subscription-only model routes and the desk's loopback forward open. The adapter checks reported weekly subscription limits immediately before claiming a request. If any available subscription is at 90% or its usage is unknown, it declines rather than guessing which route OMP will use. Load the specimens first. In Python Eval, from the kit directory, run these **as separate cells**:

```python
%load workbench/omp_critic.py
```

```python
review_next(completion)
```

Select **Request a model critique**, then **Start case review** within two minutes, or request the critique first and attach the adapter while it's pending. If a previous critique finished or expired, start the next review before attaching again. The adapter waits for that loaded revision only. It makes at most one stateless inference call with no tools or conversation history. The model sees only check names, actors, slot letters, outcome labels, counts, and event numbers. No file, path, digest, key, or case socket goes to the model. Don't pair this critic as an MCP agent.

A model note appears with links to the entries it cites. It isn't a finding and isn't included in the evidence export. A new finding invalidates it; a changed case, cancellation, timeout, or failed validation cannot install a late note. **Cancel critique** applies only to the review displayed in that tab. Cancelling after dispatch can't recall information already sent. The adapter stops after one note or error. If no critic connects, no model note appears.

### Pair a metadata agent

**Pair agent** under **Agent access** issues a key for one case. The desk distinguishes an issued key from a paired client. Configure the client outside the worker. Give it only the four case tools, not shell, file, or network tools. The agent listener accepts only `/mcp`; it can't load files, start a case review, pair itself, or run programs.

Reports download as `skellywix-re-kit-evidence.json`. The report format identifier (`yuzu.report/1`) and revision header (`X-Yuzu-Revision`) remain unchanged for existing clients.

Pairing does not approve tool calls. If a client requires approval, approve its requests in that client. A connected client hasn't necessarily used a tool: check the **Evidence ledger** for a paired-agent entry.

**Revoke agent key**, a file change, 15 minutes, or 32 tool calls ends access. For the local quickstart, stop the launcher with Control-C. For the manual remote setup, stop the SSH forward, then run this in the host shell that created the worker:

```sh
docker stop "$worker"
rm -f -- "$ipc/desk.sock" "$ipc/agent.sock"
rmdir -- "$ipc"
```

This workbench does not install a service or modify an existing deployment.
