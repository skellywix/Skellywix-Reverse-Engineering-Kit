// SPDX-License-Identifier: GPL-2.0-or-later
//
// Tests for the Rust port of the Neverball 1.6.0 level loader.
// See NOTICE.md and COPYING.

//! Level attribute tests: the rules of `scan_level_attribs` in
//! `ball/level.c` (defaults, the two-value score lines that fill in the easy
//! rank, truncation to the C buffers) and the on-disk loading path.

mod support;

use neverball::error::{Error, LoadError};
use neverball::level::{Concern, Level, LevelInfo, Summary, DEFAULT_TIMER};
use neverball::sol::Sol;
use support::{sample_v7, Builder, TempDir};

fn info_for(attrs: &[(&str, &str)]) -> LevelInfo {
    let mut b = Builder::empty(7);
    b.attrs = attrs
        .iter()
        .map(|&(k, v)| (k.to_owned(), v.to_owned()))
        .collect();
    let sol = Sol::from_bytes(&b.build()).unwrap_or_else(|e| panic!("sample failed to load: {e}"));
    LevelInfo::from_sol(&sol)
}

#[test]
fn an_empty_dictionary_gives_the_c_defaults() {
    let info = info_for(&[]);
    assert_eq!(info.message, "");
    assert_eq!(info.song, "");
    assert_eq!(info.shot, "");
    assert_eq!(info.author, "");
    assert_eq!(info.version_str, "");
    assert_eq!(info.version_num, 0);
    assert_eq!((info.goal, info.time), (0, 0));
    assert!(!info.is_bonus);
    for table in [info.scores.time, info.scores.goal, info.scores.coin] {
        assert_eq!(table.timer, [DEFAULT_TIMER; 3]);
        assert_eq!(table.coins, [0; 3]);
    }
}

#[test]
fn plain_string_and_number_attributes() {
    let info = info_for(&[
        ("message", "Welcome"),
        ("song", "bgm/title.ogg"),
        ("shot", "shot-easy/01.jpg"),
        ("author", "A. Person"),
        ("goal", "12"),
        ("time", "90"),
        ("bonus", "1"),
        ("unknown key", "ignored"),
    ]);
    assert_eq!(info.message, "Welcome");
    assert_eq!(info.song, "bgm/title.ogg");
    assert_eq!(info.shot, "shot-easy/01.jpg");
    assert_eq!(info.author, "A. Person");
    assert_eq!((info.goal, info.time), (12, 90));
    assert!(info.is_bonus);
}

#[test]
fn a_repeated_key_takes_the_last_value() {
    let info = info_for(&[("time", "10"), ("goal", "3"), ("time", "20")]);
    assert_eq!(info.time, 20);
    assert_eq!(info.goal, 3);
}

#[test]
fn numbers_follow_atoi() {
    // atoi stops at the first non-digit and returns 0 when there is none.
    assert_eq!(info_for(&[("goal", "  7 coins")]).goal, 7);
    assert_eq!(info_for(&[("goal", "-4")]).goal, -4);
    assert_eq!(info_for(&[("goal", "many")]).goal, 0);
    assert!(!info_for(&[("bonus", "yes")]).is_bonus);
    assert!(info_for(&[("bonus", "2")]).is_bonus);
    assert!(!info_for(&[("bonus", "0")]).is_bonus);
}

#[test]
fn version_text_and_number() {
    let info = info_for(&[("version", "2 beta")]);
    assert_eq!(info.version_str, "2 beta");
    assert_eq!(info.version_num, 2);

    // No leading integer: sscanf fails and the number stays 0.
    let info = info_for(&[("version", "abc")]);
    assert_eq!(info.version_str, "abc");
    assert_eq!(info.version_num, 0);

    // A later unparsable version keeps the earlier number, as sscanf does.
    let info = info_for(&[("version", "5"), ("version", "x")]);
    assert_eq!(info.version_str, "x");
    assert_eq!(info.version_num, 5);
}

#[test]
fn strings_are_cut_to_the_c_buffers() {
    let (m, a, s, h, v) = (
        "m".repeat(300),
        "a".repeat(300),
        "s".repeat(100),
        "h".repeat(100),
        "1".repeat(40),
    );
    let info = info_for(&[
        ("message", m.as_str()),
        ("author", a.as_str()),
        ("song", s.as_str()),
        ("shot", h.as_str()),
        ("version", v.as_str()),
    ]);
    // Buffers are 256, 256, 64, 64 and 32 bytes; the last byte is the NUL.
    assert_eq!(info.message.len(), 255);
    assert_eq!(info.author.len(), 255);
    assert_eq!(info.song.len(), 63);
    assert_eq!(info.shot.len(), 63);
    assert_eq!(info.version_str.len(), 31);
}

#[test]
fn truncation_never_splits_a_character() {
    // 30 ASCII bytes then a 3-byte character: byte 31 is mid-character.
    let value = format!("{}\u{20AC}", "x".repeat(30));
    let doubled = format!("{value}{value}");
    let info = info_for(&[("version", doubled.as_str())]);
    assert_eq!(info.version_str, "x".repeat(30));
}

#[test]
fn three_value_score_lines_are_taken_as_written() {
    let info = info_for(&[
        ("time", "45"),
        ("goal", "9"),
        ("time_hs", "1000 2000 3000"),
        ("goal_hs", "1100 2100 3100"),
        ("coin_hs", "40 30 20"),
    ]);
    assert_eq!(info.scores.time.timer, [1000, 2000, 3000]);
    assert_eq!(info.scores.goal.timer, [1100, 2100, 3100]);
    assert_eq!(info.scores.coin.coins, [40, 30, 20]);
}

#[test]
fn two_value_score_lines_fill_the_easy_rank_from_time_and_goal() {
    let info = info_for(&[
        ("time", "45"),
        ("goal", "9"),
        ("time_hs", "1000 2000"),
        ("goal_hs", "1100 2100"),
        ("coin_hs", "40 30"),
    ]);
    assert_eq!(info.scores.time.timer, [1000, 2000, 45]);
    assert_eq!(info.scores.goal.timer, [1100, 2100, 45]);
    assert_eq!(info.scores.coin.coins, [40, 30, 9]);
}

#[test]
fn two_value_lines_leave_the_easy_default_without_time_or_goal() {
    let info = info_for(&[
        ("time_hs", "1000 2000"),
        ("goal_hs", "1100 2100"),
        ("coin_hs", "40 30"),
    ]);
    assert_eq!(info.scores.time.timer, [1000, 2000, DEFAULT_TIMER]);
    assert_eq!(info.scores.goal.timer, [1100, 2100, DEFAULT_TIMER]);
    assert_eq!(info.scores.coin.coins, [40, 30, 0]);
}

#[test]
fn a_one_value_or_unparsable_score_line_changes_little() {
    // Exactly one value: only the first rank is set and no easy fill happens.
    let info = info_for(&[("time", "45"), ("time_hs", "1000")]);
    assert_eq!(info.scores.time.timer, [1000, DEFAULT_TIMER, DEFAULT_TIMER]);

    let info = info_for(&[("time", "45"), ("time_hs", "fast")]);
    assert_eq!(info.scores.time.timer, [DEFAULT_TIMER; 3]);
}

#[test]
fn goal_and_time_derive_the_other_score_tables() {
    let info = info_for(&[("time", "45"), ("goal", "9")]);
    // The goal table's coins are the goal; the coin table's timers are the time.
    assert_eq!(info.scores.goal.coins, [9; 3]);
    assert_eq!(info.scores.coin.timer, [45; 3]);
    // The tables a level does not describe keep their defaults.
    assert_eq!(info.scores.time.timer, [DEFAULT_TIMER; 3]);
    assert_eq!(info.scores.goal.timer, [DEFAULT_TIMER; 3]);
    assert_eq!(info.scores.coin.coins, [0; 3]);

    // A zero is still "present": the tables follow it.
    let info = info_for(&[("time", "0"), ("goal", "0")]);
    assert_eq!(info.scores.coin.timer, [0; 3]);
    assert_eq!(info.scores.goal.coins, [0; 3]);
}

// ---------------------------------------------------------------------------
// Whole levels.

#[test]
fn a_level_exposes_its_gameplay_entities() {
    let level = Level::from_bytes("sample.sol", &sample_v7().build())
        .unwrap_or_else(|e| panic!("sample failed to load: {e}"));
    assert_eq!(level.path.to_str(), Some("sample.sol"));
    assert_eq!((level.info.goal, level.info.time), (10, 30));
    assert_eq!(level.info.message, "Hello");

    assert_eq!(
        level.summary(),
        Summary {
            materials: 2,
            vertices: 4,
            triangles: 2,
            lumps: 2,
            detail_lumps: 1,
            nodes: 3,
            paths: 2,
            bodies: 3,
            moving_bodies: 2,
            coins: 2,
            coin_value: 6,
            grows: 1,
            shrinks: 1,
            goals: 1,
            jumps: 1,
            switches: 1,
            billboards: 1,
            balls: 1,
            views: 1,
        }
    );
}

#[test]
fn concerns_flag_an_unreachable_goal_but_do_not_fail_the_load() {
    // The sample wants 10 coins and offers 6.
    let level = Level::from_bytes("sample.sol", &sample_v7().build())
        .unwrap_or_else(|e| panic!("sample failed to load: {e}"));
    assert_eq!(
        level.concerns(),
        [Concern::GoalExceedsCoins {
            required: 10,
            available: 6
        }]
    );

    let mut b = sample_v7();
    b.attrs = vec![("goal".into(), "6".into())];
    let level = Level::from_bytes("ok.sol", &b.build()).unwrap_or_else(|e| panic!("{e}"));
    assert!(level.concerns().is_empty());
}

#[test]
fn an_empty_level_loads_and_reports_every_structural_concern() {
    let level = Level::from_bytes("empty.sol", &Builder::empty(7).build())
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        level.concerns(),
        [Concern::NoVertices, Concern::NoTriangles, Concern::NoGoalZone]
    );
    // The game always has one ball start; an absent record becomes the origin.
    assert_eq!(level.sol.balls.len(), 1);
}

#[test]
fn concerns_read_as_sentences() {
    let text = Concern::GoalExceedsCoins {
        required: 10,
        available: 6,
    }
    .to_string();
    assert!(text.contains("10") && text.contains('6'), "{text}");
}

#[test]
fn load_reads_a_file_and_names_it_in_errors() {
    let dir = TempDir::new("level-load");
    let good = dir.write("map/good.sol", &sample_v7().build());
    let level = Level::load(&good).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(level.path, good);
    assert_eq!(level.sol.balls.len(), 1);

    let mut truncated = sample_v7().build();
    truncated.truncate(truncated.len() - 3);
    let bad = dir.write("map/bad.sol", &truncated);
    match Level::load(&bad) {
        Err(LoadError::Sol {
            path,
            source: Error::Truncated { .. } | Error::CountExceedsData { .. },
        }) => assert_eq!(path, bad),
        other => panic!("expected a truncation error naming the file, got {other:?}"),
    }

    let missing = dir.0.join("map/absent.sol");
    match Level::load(&missing) {
        Err(LoadError::Io { path, .. }) => assert_eq!(path, missing),
        other => panic!("expected an I/O error, got {other:?}"),
    }
}
