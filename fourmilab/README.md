# Reconstruct Fourmilab Base64

**Reconstruct** compares a proposed Rust implementation of [Fourmilab Base64 1.5](https://www.fourmilab.ch/webtools/base64/) with the original Linux command-line program. A download requires agreement on 2,557 cases. This optional profile is separate from the general [source builder](../README.md#build-a-game-or-application).

## Prepare the original and tools

Use a non-root account on the [workspace host](../README.md#start-the-workspace). Start the workspace once to install the Python and Rust images, then export any open Inspect evidence before stopping it. Supply your own original. The installer checks its [pinned digest](contract.py), the actual Docker image IDs, and the `runsc` runtime.

From the repository root, substitute the path to your original and the SSH host name the companion will use. If the companion runs on this host, it can use `localhost` with key-based SSH.

```sh
python3 -B tools/prepare_fourmilab.py --reference "$REFERENCE" --companion-host "$SSH_HOST"
```

The installer prints the new private directory and the launch commands. It doesn't start the desk, contact a model, or copy an earlier ledger. Set `D` to the printed directory for the commands below.

## Start the desk and select a release

1. Start the desk in one terminal:

   ```sh
   (cd "$D/app" && python3 -B -m fourmilab.desk serve "$D")
   ```

2. For the first release, use a second terminal to expose the private socket on loopback. Port 48784 must be free.

   ```sh
   systemd-socket-activate -l 127.0.0.1:48784 /usr/lib/systemd/systemd-socket-proxyd "$D/desk.sock"
   ```

3. Open <http://127.0.0.1:48784/>, choose **Stage update**, review the staged receipt, then choose **Promote staged**. Staging resolves the Kit inspector on GitHub and the stable REA release to exact commits and file hashes. It needs network access; promotion is a separate action. Stop the loopback proxy, not the desk.
4. From the repository root, start the workspace and open **Reconstruct**:

   ```sh
   python3 -B tools/workspace.py --fourmilab-socket "$D/desk.sock"
   ```

## Generate and compare a candidate

Start the optional companion yourself. It requires the [pinned OMP version, subscription route, and quota check](builder.py). The companion needs a copy of `connection.json`, `app/fourmilab/builder.py`, and `app/fourmilab/model_turn.ts` in the same layout. With the companion on the workspace host:

```sh
python3 -B "$D/app/fourmilab/builder.py"
```

After **Ready for one Fourmilab run**, start the run in **Reconstruct**. The companion supplies source text for this profile only, has no model tools, and does not answer approval dialogs. No companion, failed comparison, or missing evidence means no verified download. A pass covers the named cases, not every possible input. The private directory is under `/tmp`; don't publish or reuse its ledger, snapshots, or run packages as evidence for another run.
