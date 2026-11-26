# Neverball 1.6.0 Rust port

The root crate loads Neverball's level sets and simulates play at 90 updates per second. The separate `desktop/` crate adds the Windows game window, menus, input, camera, audio, score storage, and replay recording and playback. It reads the original `data/data-1.6.0.pk3` next to the executable. It does not start the original game or load its game code.

The build profile targets the official [Neverball 1.6.0](https://neverball.org/download) Windows distribution. Building and gameplay verification are separate claims: a finished build is downloadable and labelled **built, not play-tested**; a linked executable is not a verified game. Joystick support, title attract demos, and the full original options menu are not implemented.

On a fresh profile, **Play** opens **Player Name**. Cancel returns to the title. After you enter and save a name, **Play** opens the set list, including after you quit and reopen the game.

Arrow keys tilt the floor with the original 250 ms response, including when a key is released. Moving the mouse switches to the 50 ms mouse response. Leaving it still does not change the keyboard response.

After a goal, fall, or timeout, the desktop game writes the finished attempt to `Replays/Last.nbr` in its own save folder. **Save Replay** lets you keep it under a name you choose; replacing a named replay requires confirmation. The header records elapsed time, not time remaining on the level clock.

License: GPL-2.0-or-later (`COPYING`, `NOTICE.md`). The simulation has no external dependencies. The desktop dependency versions are fixed in `desktop/Cargo.lock`.

## Optional: set up the Windows rebuild profile

The workspace's **Rebuild for Windows** profile compiles `desktop/` into a 32-bit Windows program inside a locally built, offline compiler image. A finished build whose package is intact (its ZIP matches the hash the run journaled and passes the package checks) can be downloaded at once. It is labelled **built, download ready, not play-tested**: nobody has watched it run, and the package's `provenance.json` says so. Independent Windows observations of the original and the rebuilt program are optional and recorded separately; only they can add a **verified** label. The host needs Linux, Docker with the gVisor `runsc` runtime, and `/usr/bin/docker`.

1. **Install the dependency set.** Run `python3 -B tools/update_workspace.py --root DATA bootstrap` (or `update`), or start the workspace once. This builds the `neverball-runner` image from `build/windows-i686.Dockerfile` and vendors the crates in `desktop/Cargo.lock` into `DATA`. Every run uses the immutable image ID that this update recorded for the runner and writes it to the run's journal and package provenance. A run never takes a tag, a registry digest, or an image from a request, and the desk refuses a runner whose recorded recipe is not the one pinned in `build_desk/game_jobs.py`. An update that installs a new runner applies to the next run, not to one in progress.
2. **Get the original Windows release.** Download the unmodified Neverball 1.6.0 Windows ZIP from [neverball.org/download](https://neverball.org/download). It must be 75,802,818 bytes with SHA-256 `865391cc041208e526401a8eb1f14fba44323f17d7e818c5e65e82393ff747fb`. The kit never downloads it. This is the only file you supply; no private bundle or earlier release is needed.
3. **Prepare the release.** Run `python3 -B tools/prepare_neverball.py --root DATA --original /path/to/neverball-1.6.0-win.zip`. The tool copies the source from this checkout, packs the crates the update installed into a canonical, byte-reproducible `dependencies.tgz` (188,297,268 bytes), and writes `DATA/neverball/release/` (change it with `--output`) only after the desk's own release check passes; otherwise it writes nothing. Remove an old release directory first; the tool does not replace one. The archive's SHA-256 must be one of the two exact digests in `build_desk/neverball_archives.py`:
   - `22e12932d194701a8652d4baa28af6513d77289aae4bed2aa06fd9f75c553d1f`, the portable archive that a clean install produces and uses.
   - `88a960b6c75d5b303ac2b636b77681cfd1e4f4782434c5a498c1c2c9b5ce8b84`, the archive an earlier candidate release carried. An existing install that still has it can pass it with `--dependencies-tgz`; its runs keep that original binding.

   Both hold the same 7,009 crate files. Any other archive is refused, even one with the same files: a digest is approved only by being listed in that reviewed source file, never by a browser, at install time, or by a file comparison.
4. **Start the workspace** with `python3 -B tools/workspace.py --root DATA --game-release DATA/neverball/release`, then open **Windows example: Neverball 1.6.0** on the front page. The desk hashes `dependencies.tgz` for each run and records its approved digest in the journal and package provenance.

The inputs are checked as follows. No check is loosened for convenience.

| Input | Pin | Check |
|---|---|---|
| Port source | `desktop/Cargo.lock` `532b4f28…50ac7`, recipe `2c197c64…af6f` (LF-normalized committed bytes) | The release holds exactly the files listed in `source.sha256`; no C or C++ code, binaries, build scripts, links or `target/`. |
| Compiler | The runner image ID in the active dependency receipt | The receipt's recipe hash equals the pin. `docker image inspect` must return that same ID before the run. The run starts with no network, a read-only root and the fixed arguments. |
| Original release | ZIP and `data/data-1.6.0.pk3` SHA-256 | Both are re-hashed on every run; a copy of an original executable is refused. |
| Dependencies | One of the two approved archive digests above | The archive must equal `vendor/` file for file. Each crate must match its `.cargo-checksum.json`, and the crates must be exactly those `Cargo.lock` names. The `miniquad` patch must apply without fuzz. |
| Windows comparison (optional) | Run, program, ZIP and journal binding | Does not gate the download. If a receipt is recorded it must bind this exact run and package, and it is checked as strictly as before; only an accepted receipt makes the run `proven` and reports `verified_sha256`. A fresh receipt that disagrees fails the run, and a failed run has no download. |
| Download | The ZIP hash the journal recorded, the pinned member hashes, the journal's hash chain | A missing, altered, truncated, replaced or structurally invalid ZIP, an unfinished or failed run, and a tampered journal are never served. Every download re-hashes the ZIP; status polling does not, until the file's identity changes. |

The recipe installs exact Debian package versions, so a later Debian archive change can make the runner build fail. The update then reports that failure and installs nothing.

Run states: `queued`, `build`, `package`, then `built` (download ready, not play-tested), then optionally `proven` (accepted independent Windows evidence); `failed` ends a run that did not finish, was damaged, or was contradicted. A run journaled before `built` existed ended in `waiting_windows`; it is read as built, its journal is unchanged, and it can still be proven by a receipt. Status reports `download_sha256` for any intact finished build and `verified_sha256` only for a proven one.

## What it reads

**SOL, versions 6 and 7** (`share/solid_base.c`): header and counts; the string
block and dictionary (attributes); materials, including the version 7 flag
layout and alpha-test fields and the version 6 flag translation; vertices,
edges, side planes, texture coordinates; triangle corner offsets (stored in
version 7, derived for version 6); triangles; collision lumps with their
detail flag; the BSP node tree; paths (waypoint, travel time rounded to
milliseconds, enabled and smooth flags, orientation quaternion and flags for
version 7); bodies with first and second path, root node, lumps and triangles;
items (coin value, grow, shrink; unknown types are kept); goal zones;
teleporters; switches; billboards; ball starts; viewpoints; and the shared
index pool, which is resolved into each lump's and body's own index lists.

**Levels** (`ball/level.c`): the dictionary attributes `message`, `song`,
`shot`, `author`, `version`, `goal`, `time`, `bonus`, `time_hs`, `goal_hs`,
`coin_hs`, with the C defaults, buffer truncation and two-value score-line
rules.

**Sets** (`ball/set.c`): `sets.txt` and `set-*.txt`.

Unlike the C loader, which trusts the file, every count, index and range is
validated: truncated files, negative or impossible counts, dangling references,
bad strings, non-finite floats and cyclic BSP trees are `Error`s, not
out-of-bounds reads.

```rust
use neverball::level::Level;

let level = Level::load("data/map-easy/easy.sol")?;
let sol = &level.sol;
for body in &sol.bodies {
    for triangle in sol.body_triangles(body) {
        // triangle.corners[i].{position, normal, tex_coord}, triangle.material
    }
}
println!("goal {} coins, start {:?}", level.info.goal, sol.start().position);
# Ok::<(), neverball::error::LoadError>(())
```

## Check contact at a ball position

`contact::take_item` removes at most one collectible from the current level
state. It returns the original slot and item, including its coin value.
`contact::goal_at` reports which goal contains the ball, not whether that
goal is unlocked. `contact::jump_at` distinguishes a rim touch from entry
and returns the destination with the ball's offset intact.

## Checking all 183 levels

The data files come from the original game, not from this repository. The
1.6.0 source archive holds only the `.map` sources and the set lists; the
compiled `.sol` files are in the game's released data package. Extract that
package (or the `data/` tree of an installed game) so that `DATA_DIR` contains
`sets.txt`, the eight `set-*.txt` files and the `map-*` directories with the
`.sol` files, then run from this directory:

```text
cargo run --release --bin levelcheck -- DATA_DIR
```

It prints one line per distinct level (triangles, lumps, nodes, bodies and how
many move, paths, coins and their total value, goals, teleporters, switches,
ball starts, viewpoints, `goal` and `time`), concerns such as a goal that needs
more coins than the level contains, and a summary. Exit status 0 means every
distinct level loaded and the count equals 183 (`--expect N` changes the
expected count); 1 means a level failed or the count differs; 2 is a usage or
set-list error. The last line reads `RESULT: OK: all 183 distinct levels
loaded` on success.

The same facts as assertions (every level loads, has vertices, triangles, a
valid ball start, a goal zone in the playable sets, and finite resolved
geometry):

```text
NEVERBALL_DATA=DATA_DIR cargo test --release --test shipped -- --ignored
```

Plain `cargo test` runs the synthetic tests only. They build SOL files from an
independent writer (`tests/support`) and cover truncation at every byte, bad
and negative counts, dangling indices and ranges, the version 6 and 7 layouts,
path flags and millisecond rounding, dictionary string bounds, BSP cycles and
very deep trees, and the attribute rules.

## Level loader limits

- Version 6 and 7 only; other versions are refused, as in 1.6.0.
- The loader reads compiled `.sol` levels. It does not read `.map` sources or
  include the `mapc` compiler.
- `load_all` loads `set-misc.txt`, which the game hides unless cheats are on,
  and fails on a set that cannot be read where the game skips it.
