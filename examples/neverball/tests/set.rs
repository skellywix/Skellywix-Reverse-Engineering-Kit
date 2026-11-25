// SPDX-License-Identifier: GPL-2.0-or-later
//
// Tests for the Rust port of the Neverball 1.6.0 level-set reader.
// See NOTICE.md and COPYING.

//! Set file tests: the header and level list rules of `set_load` and the
//! discovery order of `set_init` in `ball/set.c`.

mod support;

use neverball::error::LoadError;
use neverball::set::{distinct_levels, load_all, Set, DEFAULT_SET_TIMER, MAX_LEVELS};
use support::TempDir;

const HEADER: &str = "Easy\nThe first\\steps\nset-easy\nshot-easy/set.jpg\n";

fn set_text(scores: &str, levels: &[&str]) -> String {
    let mut text = format!("{HEADER}{scores}\n");
    for level in levels {
        text.push_str(level);
        text.push('\n');
    }
    text
}

fn parse_ok(text: &str) -> Set {
    Set::parse("s.txt", text).unwrap_or_else(|e| panic!("{e}"))
}

#[test]
fn a_set_file_is_five_header_lines_then_levels() {
    let text = set_text(
        "1000 2000 3000 10 20 30",
        &["map-easy/a.sol", "map-easy/b.sol"],
    );
    let set = Set::parse("set-easy.txt", &text).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(set.file, "set-easy.txt");
    assert_eq!(set.name, "Easy");
    // The backslash is a line-break marker for the menu, kept as written.
    assert_eq!(set.description, "The first\\steps");
    assert_eq!(set.id, "set-easy");
    assert_eq!(set.shot, "shot-easy/set.jpg");
    assert_eq!(set.time_scores, [1000, 2000, 3000]);
    assert_eq!(set.coin_scores, [10, 20, 30]);
    assert_eq!(set.levels, ["map-easy/a.sol", "map-easy/b.sol"]);
}

#[test]
fn a_short_score_line_keeps_the_defaults_for_what_is_missing() {
    let set = parse_ok(&set_text("1000 2000", &["a.sol"]));
    assert_eq!(set.time_scores, [1000, 2000, DEFAULT_SET_TIMER]);
    assert_eq!(set.coin_scores, [0, 0, 0]);

    let set = parse_ok(&set_text("1000 2000 3000 7", &["a.sol"]));
    assert_eq!(set.time_scores, [1000, 2000, 3000]);
    assert_eq!(set.coin_scores, [7, 0, 0]);

    let set = parse_ok(&set_text("", &["a.sol"]));
    assert_eq!(set.time_scores, [DEFAULT_SET_TIMER; 3]);
}

#[test]
fn a_set_is_capped_at_the_games_level_limit() {
    let names: Vec<String> = (0..MAX_LEVELS + 5).map(|n| format!("map/{n}.sol")).collect();
    let refs: Vec<&str> = names.iter().map(String::as_str).collect();
    let set = parse_ok(&set_text("1 2 3 4 5 6", &refs));
    assert_eq!(MAX_LEVELS, 25);
    assert_eq!(set.levels.len(), MAX_LEVELS);
    assert_eq!(set.levels[MAX_LEVELS - 1], format!("map/{}.sol", MAX_LEVELS - 1));
}

#[test]
fn windows_line_endings_are_stripped() {
    let text = "Name\r\nDesc\r\nid\r\nshot.jpg\r\n1 2 3 4 5 6\r\nmap/a.sol\r\nmap/b.sol\r\n";
    let set = parse_ok(text);
    assert_eq!(set.name, "Name");
    assert_eq!(set.shot, "shot.jpg");
    assert_eq!(set.levels, ["map/a.sol", "map/b.sol"]);
}

#[test]
fn a_final_newline_does_not_add_a_level_and_a_missing_one_does_not_drop_it() {
    let with = parse_ok(&set_text("1 2 3 4 5 6", &["a.sol"]));
    assert_eq!(with.levels, ["a.sol"]);

    let mut text = set_text("1 2 3 4 5 6", &["a.sol", "b.sol"]);
    text.pop();
    let without = parse_ok(&text);
    assert_eq!(without.levels, ["a.sol", "b.sol"]);
}

#[test]
fn a_set_with_no_levels_is_valid() {
    let set = parse_ok(&set_text("1 2 3 4 5 6", &[]));
    assert!(set.levels.is_empty());
}

#[test]
fn incomplete_headers_are_rejected() {
    for lines in 0..5 {
        let text: String = (0..lines).map(|n| format!("line {n}\n")).collect();
        match Set::parse("s.txt", &text) {
            Err(LoadError::Set { path, .. }) => assert_eq!(path.to_str(), Some("s.txt")),
            other => panic!("{lines} header lines: expected a set error, got {other:?}"),
        }
    }
}

// ---------------------------------------------------------------------------
// Directories.

fn write_set(dir: &TempDir, file: &str, id: &str, levels: &[&str]) {
    let text = format!("{id}\nd\n{id}\nshot.jpg\n1 2 3 4 5 6\n{}\n", levels.join("\n"));
    dir.write(file, text.as_bytes());
}

#[test]
fn load_all_orders_listed_sets_first_then_the_rest_by_name() {
    let dir = TempDir::new("set-order");
    dir.write("sets.txt", b"set-hard.txt\nset-easy.txt\n");
    write_set(&dir, "set-hard.txt", "hard", &["map/h1.sol", "map/shared.sol"]);
    write_set(&dir, "set-easy.txt", "easy", &["map/e1.sol", "map/shared.sol"]);
    // Not listed in sets.txt: found by directory scan, sorted by name.
    write_set(&dir, "set-zeta.txt", "zeta", &["map/z1.sol"]);
    write_set(&dir, "set-alpha.txt", "alpha", &["map/a1.sol"]);
    // Neither listed nor named like a set file: ignored.
    dir.write("notes.txt", b"not a set");
    dir.write("set-readme.md", b"not a set");

    let sets = load_all(&dir.0).unwrap_or_else(|e| panic!("{e}"));
    let files: Vec<&str> = sets.iter().map(|s| s.file.as_str()).collect();
    assert_eq!(
        files,
        ["set-hard.txt", "set-easy.txt", "set-alpha.txt", "set-zeta.txt"]
    );

    // A level in two sets counts once.
    assert_eq!(
        distinct_levels(&sets),
        [
            "map/a1.sol",
            "map/e1.sol",
            "map/h1.sol",
            "map/shared.sol",
            "map/z1.sol"
        ]
    );
    assert_eq!(sets.iter().map(|s| s.levels.len()).sum::<usize>(), 6);
}

#[test]
fn load_all_fails_on_a_missing_or_broken_set() {
    let dir = TempDir::new("set-missing");
    dir.write("sets.txt", b"set-gone.txt\n");
    match load_all(&dir.0) {
        Err(LoadError::Io { path, .. }) => assert!(path.ends_with("set-gone.txt"), "{path:?}"),
        other => panic!("expected an I/O error for the missing set, got {other:?}"),
    }

    let dir = TempDir::new("set-broken");
    dir.write("sets.txt", b"set-short.txt\n");
    dir.write("set-short.txt", b"only\ntwo\n");
    match load_all(&dir.0) {
        Err(LoadError::Set { path, .. }) => assert!(path.ends_with("set-short.txt"), "{path:?}"),
        other => panic!("expected a set error, got {other:?}"),
    }

    let dir = TempDir::new("set-nolist");
    assert!(matches!(load_all(&dir.0), Err(LoadError::Io { .. })));
}
