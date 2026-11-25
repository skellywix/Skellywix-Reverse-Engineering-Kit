// SPDX-License-Identifier: GPL-2.0-or-later
//
// Check of the Rust port of the Neverball 1.6.0 level loader against the
// original game's data. See NOTICE.md and COPYING.

//! The shipped-data check.
//!
//! This repository does not contain the game's data files (see `NOTICE.md`),
//! so this test is `#[ignore]`d and runs only on request, against a data
//! directory the caller extracted from the original 1.6.0 data package:
//!
//! ```text
//! NEVERBALL_DATA=/path/to/data cargo test --test shipped -- --ignored
//! ```
//!
//! Run that way, a missing variable or directory is a failure, not a skip.
//! The same facts are printed per level by `cargo run --bin levelcheck`.

use std::path::PathBuf;

use neverball::level::{Concern, Level};
use neverball::set::{distinct_levels, load_all};

/// Distinct level files named by the 1.6.0 release's eight sets.
const RELEASE_LEVELS: usize = 183;
const RELEASE_SETS: usize = 8;
/// The set the game hides unless cheats are on; it may hold test levels.
const HIDDEN_SET: &str = "set-misc.txt";

fn data_dir() -> PathBuf {
    let dir = std::env::var_os("NEVERBALL_DATA")
        .map(PathBuf::from)
        .unwrap_or_else(|| panic!("set NEVERBALL_DATA to the extracted Neverball 1.6.0 data directory"));
    assert!(dir.join("sets.txt").is_file(), "{dir:?} has no sets.txt");
    dir
}

#[test]
#[ignore = "needs the original game's data; set NEVERBALL_DATA and pass --ignored"]
fn every_shipped_level_loads_with_usable_geometry_and_gameplay() {
    let dir = data_dir();
    let sets = load_all(&dir).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(sets.len(), RELEASE_SETS, "set files found");

    let names = distinct_levels(&sets);
    assert_eq!(names.len(), RELEASE_LEVELS, "distinct levels in the sets");

    let ordinary: Vec<&str> = sets
        .iter()
        .filter(|s| s.file != HIDDEN_SET)
        .flat_map(|s| s.levels.iter().map(String::as_str))
        .collect();

    let mut failures = Vec::new();
    for name in &names {
        let level = match Level::load(dir.join(name)) {
            Ok(level) => level,
            Err(e) => {
                failures.push(format!("{name}: {e}"));
                continue;
            }
        };
        let sol = &level.sol;
        let mut problems = Vec::new();

        let concerns = level.concerns();
        if concerns.contains(&Concern::NoVertices) {
            problems.push("no vertices".to_owned());
        }
        if concerns.contains(&Concern::NoTriangles) {
            problems.push("no triangles".to_owned());
        }
        if concerns.contains(&Concern::NoGoalZone) && ordinary.contains(name) {
            problems.push("no goal zone in a playable set".to_owned());
        }

        let start = sol.start();
        if !(start.radius.is_finite() && start.radius > 0.0) {
            problems.push(format!("ball radius {}", start.radius));
        }
        if sol.bounds().is_none() {
            problems.push("no bounds".to_owned());
        }

        // Walk every resolved reference a simulation or renderer would follow.
        for (n, body) in sol.bodies.iter().enumerate() {
            for triangle in sol.body_triangles(body) {
                let finite = triangle
                    .corners
                    .iter()
                    .all(|c| c.position.iter().chain(&c.normal).all(|x| x.is_finite()));
                if !finite {
                    problems.push(format!("body {n} has a non-finite corner"));
                    break;
                }
            }
            for lump in sol.body_lumps(body) {
                let resolved = (
                    sol.lump_vertices(lump).count(),
                    sol.lump_edges(lump).count(),
                    sol.lump_sides(lump).count(),
                );
                if resolved != (lump.vertices.len(), lump.edges.len(), lump.sides.len()) {
                    problems.push(format!("body {n} lump references do not resolve"));
                }
            }
        }

        if !problems.is_empty() {
            failures.push(format!("{name}: {}", problems.join("; ")));
        }
    }

    assert!(
        failures.is_empty(),
        "{} of {} levels failed:\n{}",
        failures.len(),
        names.len(),
        failures.join("\n")
    );
}
