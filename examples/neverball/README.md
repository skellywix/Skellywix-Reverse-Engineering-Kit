# Neverball 1.6.0 Rust port

The root crate loads Neverball's level sets and simulates play at 90 updates per second. The separate `desktop/` crate adds the Windows game window, menus, input, camera, audio, score storage, and replay recording and playback. It reads the original `data/data-1.6.0.pk3` next to the executable. It does not start the original game or load its game code.

The build profile targets the official [Neverball 1.6.0](https://neverball.org/download) Windows distribution. Build and gameplay verification are separate: a linked executable is not a verified game. Joystick support, title attract demos, and the full original options menu are not implemented.

On a fresh profile, **Play** opens **Player Name**. Cancel returns to the title. After you enter and save a name, **Play** opens the set list, including after you quit and reopen the game.

Arrow keys tilt the floor with the original 250 ms response, including when a key is released. Moving the mouse switches to the 50 ms mouse response. Leaving it still does not change the keyboard response.

After a goal, fall, or timeout, the desktop game writes the finished attempt to `Replays/Last.nbr` in its own save folder. **Save Replay** lets you keep it under a name you choose; replacing a named replay requires confirmation. The header records elapsed time, not time remaining on the level clock.

License: GPL-2.0-or-later (`COPYING`, `NOTICE.md`). The simulation has no external dependencies. The desktop dependency versions are fixed in `desktop/Cargo.lock`.

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
