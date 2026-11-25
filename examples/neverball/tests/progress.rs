// SPDX-License-Identifier: GPL-2.0-or-later
//
// Tests for the Rust port of the Neverball 1.6.0 score ranking and progress
// rules. See NOTICE.md and COPYING.

//! Integration tests for `neverball::progress`.
//!
//! Every expected value is a literal worked out by hand from `ball/score.c`
//! (ranking), `ball/progress.c` (`progress_stat` and friends) and
//! `ball/set.c` (`set_store_hs`, `set_load_hs_v1`, `set_load_hs_v2`), not
//! produced by the code under test.

mod support;

use std::fmt::Debug;

use neverball::level::{HighScores, LevelInfo, Scores};
use neverball::progress::{
    LoadStatus, Mode, Outcome, Placement, Progress, ProgressError, Rank, RenameTarget, ScoreEntry,
    ScoreTable, SetPlacement,
};
use neverball::set::Set;
use support::TempDir;

fn ok<T, E: Debug>(result: Result<T, E>) -> T {
    match result {
        Ok(value) => value,
        Err(e) => panic!("unexpected error: {e:?}"),
    }
}

fn err<T: Debug, E>(result: Result<T, E>) -> E {
    match result {
        Ok(value) => panic!("unexpected success: {value:?}"),
        Err(e) => e,
    }
}

// ---------------------------------------------------------------------------
// Fixtures.

/// Time tables 900/1500/3000 hundredths, goal table the same with the goal's
/// coins, coin table 20/15/10 coins at the level's time limit.
fn scores_for(goal: i32, time: i32) -> Scores {
    Scores {
        time: HighScores {
            timer: [900, 1500, 3000],
            coins: [0; 3],
        },
        goal: HighScores {
            timer: [900, 1500, 3000],
            coins: [goal; 3],
        },
        coin: HighScores {
            timer: [time; 3],
            coins: [20, 15, 10],
        },
    }
}

fn info(goal: i32, time: i32, bonus: bool, version: i32) -> LevelInfo {
    LevelInfo {
        message: String::new(),
        song: String::new(),
        shot: String::new(),
        author: String::new(),
        version_str: version.to_string(),
        version_num: version,
        goal,
        time,
        is_bonus: bonus,
        scores: scores_for(goal, time),
    }
}

fn set(id: &str, files: &[&str]) -> Set {
    Set {
        file: "set-test.txt".to_owned(),
        name: "Test".to_owned(),
        description: "A test set".to_owned(),
        id: id.to_owned(),
        shot: "shot.jpg".to_owned(),
        time_scores: [3000, 4000, 5000],
        coin_scores: [30, 20, 10],
        levels: files.iter().map(|f| (*f).to_owned()).collect(),
    }
}

/// Five levels, all goal 10 and time limit 3000; level 2 is a bonus level.
fn game_with(version: i32) -> Progress {
    let files = ["a.sol", "b.sol", "x.sol", "c.sol", "d.sol"];
    let infos: Vec<LevelInfo> = [false, false, true, false, false]
        .iter()
        .map(|&bonus| info(10, 3000, bonus, version))
        .collect();
    ok(Progress::new(&set("test-set", &files), &infos))
}

fn game() -> Progress {
    game_with(1)
}

/// A regular level with a goal of 10, then a bonus level that needs no coins.
fn pair() -> Progress {
    let infos = [info(10, 3000, false, 1), info(0, 0, true, 1)];
    ok(Progress::new(&set("pair", &["a.sol", "b.sol"]), &infos))
}

fn entry(player: &str, timer: i32, coins: i32) -> ScoreEntry {
    ScoreEntry {
        player: player.to_owned(),
        timer,
        coins,
    }
}

/// Three score lines: timer `base + k`, coins `k + 1`, player `<name><k>`.
fn block(base: i32, name: &str) -> String {
    (0..3)
        .map(|k| format!("{} {} {name}{k}\n", base + k, k + 1))
        .collect()
}

/// The entry `block` writes for rank index `k`.
fn block_entry(base: i32, name: &str, k: usize) -> ScoreEntry {
    let k = i32::try_from(k).unwrap_or(0);
    entry(&format!("{name}{k}"), base + k, k + 1)
}

/// A version 2 save of set `set_id`: set tables, then one block per
/// `(flags, version, file)`, level `k` using bases `1000 * (k + 1)` (time),
/// `+100` (goal) and `+200` (coin).
fn save_text(set_id: &str, levels: &[(i32, i32, &str)]) -> String {
    let mut text = format!("version 2\nset {set_id}\n");
    text.push_str(&block(100, "ST"));
    text.push_str(&block(200, "SC"));
    for (k, (flags, version, file)) in levels.iter().enumerate() {
        let base = 1000 * (i32::try_from(k).unwrap_or(0) + 1);
        text.push_str(&format!("level {flags} {version} {file}\n"));
        text.push_str(&block(base, "T"));
        text.push_str(&block(base + 100, "G"));
        text.push_str(&block(base + 200, "C"));
    }
    text
}

// ---------------------------------------------------------------------------
// Score ranking.

fn time_table() -> ScoreTable {
    ScoreTable::new([1000, 2000, 3000], [5, 5, 5])
}

fn coin_table() -> ScoreTable {
    ScoreTable::new([100, 200, 300], [9, 6, 3])
}

#[test]
fn a_time_tying_an_entry_exactly_ranks_below_it() {
    let table = time_table();
    assert_eq!(table.time_rank(2000, 5), Some(Rank::Easy));
    assert_eq!(table.time_rank(1000, 5), Some(Rank::Medium));
}

#[test]
fn a_time_tying_the_last_entry_exactly_does_not_place() {
    assert_eq!(time_table().time_rank(3000, 5), None);
}

#[test]
fn an_equal_time_with_more_coins_beats_the_entry() {
    let table = time_table();
    assert_eq!(table.time_rank(2000, 6), Some(Rank::Medium));
    assert_eq!(table.time_rank(1000, 6), Some(Rank::Hard));
    assert_eq!(table.time_rank(3000, 6), Some(Rank::Easy));
}

#[test]
fn an_equal_time_with_fewer_coins_loses_to_the_entry() {
    let table = time_table();
    assert_eq!(table.time_rank(2000, 4), Some(Rank::Easy));
    assert_eq!(table.time_rank(3000, 4), None);
}

#[test]
fn more_coins_win_and_equal_coins_go_to_the_faster_time() {
    let table = coin_table();
    assert_eq!(table.coin_rank(99, 9), Some(Rank::Hard));
    assert_eq!(table.coin_rank(100, 9), Some(Rank::Medium));
    assert_eq!(table.coin_rank(150, 6), Some(Rank::Medium));
}

#[test]
fn equal_coins_with_equal_or_slower_time_rank_below() {
    let table = coin_table();
    assert_eq!(table.coin_rank(200, 6), Some(Rank::Easy));
    assert_eq!(table.coin_rank(250, 6), Some(Rank::Easy));
    assert_eq!(table.coin_rank(300, 3), None);
    assert_eq!(table.coin_rank(999, 3), None);
}

#[test]
fn inserting_shifts_lower_entries_down_and_drops_the_last() {
    let mut table = coin_table();
    assert_eq!(table.insert_by_coins("P", 150, 6), Some(Rank::Medium));
    let players: Vec<&str> = table.entries().iter().map(|e| e.player.as_str()).collect();
    assert_eq!(players, ["Hard", "P", "Medium"]);
    assert_eq!(table.entry(Rank::Medium), &entry("P", 150, 6));
    assert_eq!(table.entry(Rank::Easy), &entry("Medium", 200, 6));
}

#[test]
fn a_result_that_does_not_place_leaves_the_table_unchanged() {
    let mut table = time_table();
    let before = table.clone();
    assert_eq!(table.insert_by_time("P", 3000, 5), None);
    assert_eq!(table, before);
}

#[test]
fn a_new_table_names_its_ranks_hard_medium_easy() {
    let table = time_table();
    assert_eq!(table.entry(Rank::Hard), &entry("Hard", 1000, 5));
    assert_eq!(table.entry(Rank::Medium), &entry("Medium", 2000, 5));
    assert_eq!(table.entry(Rank::Easy), &entry("Easy", 3000, 5));
}

#[test]
fn rank_queries_do_not_change_the_table() {
    let table = time_table();
    let before = table.clone();
    let _ = table.time_rank(1, 99);
    let _ = table.coin_rank(1, 99);
    assert_eq!(table, before);
}

#[test]
fn player_names_lose_line_breaks_and_leading_blanks_and_are_cut_to_255_bytes() {
    let mut table = time_table();
    table.insert_by_time("  A\nB\r", 1, 0);
    assert_eq!(table.entry(Rank::Hard).player, "AB");
    table.insert_by_time(&"x".repeat(300), 0, 0);
    assert_eq!(table.entry(Rank::Hard).player.len(), 255);
}

// ---------------------------------------------------------------------------
// Unlocking.

#[test]
fn a_new_set_has_only_the_first_level_open() {
    let p = game();
    assert!(p.is_unlocked(0));
    for index in 1..5 {
        assert!(!p.is_unlocked(index), "level {index}");
    }
    assert!(!p.is_completed(0));
}

#[test]
fn a_goal_in_normal_mode_opens_the_next_regular_level_and_skips_a_locked_bonus() {
    let mut p = game();
    p.new_game(Mode::Normal);
    ok(p.play(0));
    let first = ok(p.record_goal(10, 2000, "Ann"));
    assert_eq!(first.next, Some(1));
    assert!(first.needs_save);
    assert!(p.is_completed(0));
    assert!(p.is_unlocked(1));

    ok(p.play_next());
    let second = ok(p.record_goal(10, 2000, "Ann"));
    assert_eq!(second.next, Some(3));
    assert!(p.is_unlocked(3));
    assert!(
        !p.is_unlocked(2),
        "a bonus level stays locked in normal mode"
    );
}

#[test]
fn a_goal_in_challenge_mode_opens_every_bonus_level_it_passes() {
    let mut p = game();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    let first = ok(p.record_goal(10, 2000, "Ann"));
    assert_eq!(first.next, Some(1));
    assert!(
        !p.is_unlocked(2),
        "the bonus level is after level 1, not level 0"
    );

    ok(p.play_next());
    let second = ok(p.record_goal(10, 2000, "Ann"));
    assert_eq!(second.next, Some(3));
    assert!(p.is_unlocked(2));
    assert!(p.is_unlocked(3));
    assert!(second.needs_save);
}

#[test]
fn a_bonus_level_opened_earlier_is_the_next_level_in_normal_mode() {
    let mut challenger = game();
    challenger.new_game(Mode::Challenge);
    ok(challenger.play(0));
    ok(challenger.record_goal(10, 2000, "Ann"));
    ok(challenger.play_next());
    ok(challenger.record_goal(10, 2000, "Ann"));

    let mut p = game();
    ok(p.load_text(&challenger.to_save_text()));
    p.new_game(Mode::Normal);
    ok(p.play(1));
    let r = ok(p.record_goal(10, 2000, "Ann"));
    assert_eq!(r.next, Some(2));
}

#[test]
fn a_fall_or_timeout_unlocks_nothing_completes_nothing_and_costs_a_ball() {
    let fresh = game();
    let mut p = game();
    p.new_game(Mode::Normal);
    ok(p.play(0));
    let fall = ok(p.record_fall(2500));
    assert_eq!(fall.outcome, Outcome::Fall);
    assert_eq!(fall.next, None);
    assert_eq!(fall.ball_delta, -1);
    assert!(!fall.needs_save);
    assert_eq!(fall.placement, Placement::default());
    assert!(!p.is_completed(0));
    assert!(!p.is_unlocked(1));
    assert_eq!((p.balls(), p.score(), p.times()), (1, 0, 500));
    assert_eq!(p.level_scores(0), fresh.level_scores(0));

    ok(p.retry());
    let timeout = ok(p.record_timeout(0));
    assert_eq!(timeout.outcome, Outcome::Timeout);
    assert!(!timeout.needs_save);
    assert!(!p.is_unlocked(1));
    assert_eq!((p.balls(), p.times()), (0, 3500));
}

#[test]
fn after_a_fall_the_next_level_is_the_first_later_one_already_open() {
    let mut p = game();
    ok(p.play(0));
    ok(p.record_goal(10, 2000, "Ann"));
    ok(p.play(0));
    let fall = ok(p.record_fall(2999));
    assert_eq!(fall.next, Some(1));
    assert!(p.next_available());
}

#[test]
fn a_locked_or_missing_level_cannot_be_played_and_changes_nothing() {
    let mut p = game();
    ok(p.play(0));
    assert!(matches!(
        err(p.play(1)),
        ProgressError::LevelLocked { index: 1 }
    ));
    assert!(matches!(
        err(p.play(9)),
        ProgressError::NoSuchLevel { index: 9 }
    ));
    assert_eq!(p.current_level(), Some(0));
}

#[test]
fn standalone_results_are_kept_but_never_ask_for_a_save() {
    let mut p = game();
    p.new_game(Mode::Standalone);
    ok(p.play(0));
    let r = ok(p.record_goal(10, 2000, "Ann"));
    assert!(!r.needs_save);
    assert!(p.is_completed(0));
}

// ---------------------------------------------------------------------------
// Lives and retries.

#[test]
fn a_new_game_starts_with_two_balls() {
    let mut p = game();
    p.new_game(Mode::Challenge);
    assert_eq!((p.balls(), p.score(), p.times()), (2, 0, 0));
}

#[test]
fn a_ball_is_awarded_each_time_the_coin_total_passes_a_hundred() {
    let mut p = game();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    let first = ok(p.record_goal(99, 2900, "Ann"));
    assert_eq!((first.ball_delta, p.balls(), p.score()), (0, 2, 99));

    ok(p.play_next());
    let second = ok(p.record_goal(2, 2900, "Ann"));
    assert_eq!((second.ball_delta, p.balls(), p.score()), (1, 3, 101));

    ok(p.play_next());
    let third = ok(p.record_goal(100, 2900, "Ann"));
    assert_eq!((third.ball_delta, p.balls(), p.score()), (1, 4, 201));
    assert_eq!(p.times(), 300);
}

#[test]
fn one_level_can_award_several_balls() {
    let mut p = game();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    let r = ok(p.record_goal(250, 2900, "Ann"));
    assert_eq!(r.ball_delta, 2);
    assert_eq!(p.balls(), 4);
}

#[test]
fn a_challenge_run_is_dead_after_the_ball_count_goes_below_zero() {
    let mut p = game();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    for expected in [1, 0] {
        ok(p.record_fall(0));
        assert_eq!(p.balls(), expected);
        assert!(!p.is_dead());
        assert!(p.retry_available());
        ok(p.retry());
    }
    ok(p.record_fall(0));
    assert_eq!(p.balls(), -1);
    assert!(p.is_dead());
    assert!(!p.retry_available());
    assert!(!p.next_available());
    assert!(matches!(err(p.retry()), ProgressError::RetryUnavailable));
}

#[test]
fn a_normal_game_never_dies() {
    let mut p = game();
    p.new_game(Mode::Normal);
    ok(p.play(0));
    for _ in 0..3 {
        ok(p.record_fall(0));
        ok(p.retry());
    }
    ok(p.record_fall(0));
    assert_eq!(p.balls(), -2);
    assert!(!p.is_dead());
    assert!(p.retry_available());
}

#[test]
fn retrying_after_a_goal_takes_back_the_balls_coins_and_time_of_that_goal() {
    let mut p = game();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    ok(p.record_goal(250, 2900, "Ann"));
    assert_eq!((p.balls(), p.score(), p.times()), (4, 250, 100));
    let start = ok(p.retry());
    assert_eq!(start.index, 0);
    assert_eq!((p.balls(), p.score(), p.times()), (2, 0, 0));
    assert!(p.is_completed(0), "the completion and the unlock stay");
    assert!(p.is_unlocked(1));
}

#[test]
fn retry_is_not_offered_before_a_challenge_attempt_has_a_result() {
    let mut p = game();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    assert!(!p.retry_available());
    p.new_game(Mode::Normal);
    ok(p.play(0));
    assert!(p.retry_available());
}

#[test]
fn next_is_not_offered_in_challenge_mode_after_a_fall() {
    let mut p = game();
    ok(p.play(0));
    ok(p.record_goal(10, 2000, "Ann"));
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    ok(p.record_fall(0));
    assert_eq!(p.next_level(), Some(1));
    assert!(!p.next_available());
    assert!(matches!(err(p.play_next()), ProgressError::NextUnavailable));
}

#[test]
fn a_goal_is_open_from_the_start_only_on_a_completed_level_in_normal_mode() {
    let mut p = game();
    assert!(!ok(p.play(0)).goal_open);
    ok(p.record_goal(10, 2000, "Ann"));
    assert!(ok(p.play(0)).goal_open);

    p.set_lock_goals(true);
    assert!(!ok(p.play(0)).goal_open);
    p.set_lock_goals(false);

    p.new_game(Mode::Challenge);
    assert!(!ok(p.play(0)).goal_open);
}

#[test]
fn a_level_that_needs_no_coins_starts_with_its_goal_open() {
    let mut p = pair();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    ok(p.record_goal(10, 2000, "Ann"));
    let start = ok(p.play(1));
    assert_eq!(
        (start.goal, start.time_limit, start.goal_open),
        (0, 0, true)
    );
}

#[test]
fn a_retry_keeps_the_goal_open_state_the_attempt_started_with() {
    let mut p = game();
    assert!(!ok(p.play(0)).goal_open);
    ok(p.record_goal(10, 2000, "Ann"));
    assert!(!ok(p.retry()).goal_open, "a retry reuses the earlier state");
    ok(p.record_fall(0));
    assert!(
        ok(p.play(0)).goal_open,
        "a fresh play sees the completed level"
    );
}

#[test]
fn goal_remaining_counts_down_and_stops_at_zero() {
    let mut p = game();
    assert_eq!(p.goal_remaining(0), 0, "no level yet");
    ok(p.play(0));
    assert_eq!(p.goal_remaining(0), 10);
    assert_eq!(p.goal_remaining(4), 6);
    assert_eq!(p.goal_remaining(10), 0);
    assert_eq!(p.goal_remaining(15), 0);
}

#[test]
fn bonus_levels_are_labelled_with_roman_numerals_apart_from_regular_levels() {
    let p = game();
    let labels: Vec<String> = (0..5).filter_map(|i| p.level_label(i)).collect();
    assert_eq!(labels, ["01", "02", "I", "03", "04"]);
    assert_eq!(p.level_label(5), None);
}

// ---------------------------------------------------------------------------
// Finishing a set.

#[test]
fn passing_the_last_level_of_a_challenge_run_completes_the_set() {
    let mut p = game();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    for expected_next in [Some(1), Some(3), Some(4)] {
        let r = ok(p.record_goal(10, 2900, "Ann"));
        assert_eq!(r.next, expected_next);
        assert!(!r.set_complete);
        ok(p.play_next());
    }
    let last = ok(p.record_goal(10, 2900, "Ann"));
    assert_eq!(last.next, None);
    assert!(last.set_complete);
    assert!(p.is_done());
    assert!(!p.is_last(), "progress_last is for non-challenge games");
    assert_eq!((p.score(), p.times(), p.balls()), (40, 400, 2));
}

#[test]
fn the_set_tables_take_the_total_coins_and_the_total_time_of_the_run() {
    let mut p = game();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    for _ in 0..3 {
        ok(p.record_goal(10, 2900, "Ann"));
        ok(p.play_next());
    }
    ok(p.record_goal(10, 2900, "Ann"));

    let done = ok(p.finish_set("Zed"));
    assert_eq!(
        done.placement,
        SetPlacement {
            time: Some(Rank::Hard),
            coin: Some(Rank::Hard)
        }
    );
    assert!(done.needs_save);
    assert_eq!(
        p.set_scores().coin.entry(Rank::Hard),
        &entry("Zed", 400, 40)
    );
    assert_eq!(
        p.set_scores().time.entry(Rank::Hard),
        &entry("Zed", 400, 40)
    );
    assert_eq!(
        p.set_scores().coin.entry(Rank::Medium),
        &entry("Hard", 359_999, 30)
    );
    assert_eq!(
        p.set_scores().time.entry(Rank::Medium),
        &entry("Hard", 3000, 0)
    );
    assert_eq!(p.last_set_placement(), done.placement);

    assert!(matches!(
        err(p.finish_set("Zed")),
        ProgressError::SetAlreadyRecorded
    ));
    assert!(ok(p.rename_player("Yan", RenameTarget::SetOnly)));
    assert_eq!(p.set_scores().coin.entry(Rank::Hard).player, "Yan");
    assert_eq!(p.set_scores().time.entry(Rank::Hard).player, "Yan");
}

#[test]
fn a_set_run_cannot_be_recorded_before_it_is_complete() {
    let mut p = game();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    ok(p.record_goal(10, 2900, "Ann"));
    assert!(matches!(
        err(p.finish_set("Ann")),
        ProgressError::SetNotComplete
    ));
}

#[test]
fn a_challenge_set_ending_in_bonus_levels_opens_them_and_completes() {
    let mut p = pair();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    let r = ok(p.record_goal(10, 2000, "Ann"));
    assert_eq!(r.next, None);
    assert!(r.set_complete);
    assert!(p.is_unlocked(1), "the trailing bonus level is opened");
}

#[test]
fn the_last_level_of_a_normal_game_is_last_but_not_a_complete_set() {
    let mut p = pair();
    p.new_game(Mode::Normal);
    ok(p.play(0));
    let r = ok(p.record_goal(10, 2000, "Ann"));
    assert_eq!(r.next, None);
    assert!(!r.set_complete);
    assert!(p.is_last());
    assert!(!p.is_done());
    assert!(!p.is_unlocked(1), "a locked bonus level stays locked");
    assert!(matches!(
        err(p.finish_set("Ann")),
        ProgressError::SetNotComplete
    ));
}

// ---------------------------------------------------------------------------
// Level scores.

#[test]
fn a_goal_enters_the_time_goal_and_coin_tables_of_its_level() {
    let mut p = game();
    ok(p.play(0));
    // 3000 - 2000 = 1000 hundredths elapsed.
    let r = ok(p.record_goal(12, 2000, "Ann"));
    assert_eq!(
        r.placement,
        Placement {
            time: Some(Rank::Medium),
            goal: Some(Rank::Medium),
            coin: Some(Rank::Easy)
        }
    );
    assert!(r.placement.is_high_score());
    assert_eq!(p.last_placement(), r.placement);

    let Some(scores) = p.level_scores(0) else {
        panic!("level 0 has scores");
    };
    assert_eq!(scores.time.entry(Rank::Hard), &entry("Hard", 900, 0));
    assert_eq!(scores.time.entry(Rank::Medium), &entry("Ann", 1000, 12));
    assert_eq!(scores.time.entry(Rank::Easy), &entry("Medium", 1500, 0));
    assert_eq!(scores.goal.entry(Rank::Medium), &entry("Ann", 1000, 12));
    assert_eq!(scores.coin.entry(Rank::Medium), &entry("Medium", 3000, 15));
    assert_eq!(scores.coin.entry(Rank::Easy), &entry("Ann", 1000, 12));
}

#[test]
fn the_goal_table_is_skipped_when_the_goals_coins_were_not_collected() {
    let mut p = game();
    ok(p.play(0));
    ok(p.record_goal(12, 2000, "Ann"));
    let goal_before = p.level_scores(0).map(|s| s.goal.clone());

    // The level is complete, so its goal starts open: 3 coins reach it.
    assert!(ok(p.play(0)).goal_open);
    let r = ok(p.record_goal(3, 2999, "Bob")); // 3000 - 2999 = 1 hundredth elapsed
    assert_eq!(
        r.placement,
        Placement {
            time: Some(Rank::Hard),
            goal: None,
            coin: None
        }
    );
    assert_eq!(p.level_scores(0).map(|s| s.goal.clone()), goal_before);
}

#[test]
fn an_untimed_level_scores_the_clock_as_the_elapsed_time() {
    let mut p = pair();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    ok(p.record_goal(10, 2000, "Ann"));
    ok(p.play(1));
    // Goal 0 and time 0: the clock counts up, so the timer is the clock.
    let r = ok(p.record_goal(0, 400, "Ann"));
    assert_eq!(r.placement.time, Some(Rank::Hard));
    assert_eq!(p.times(), 1000 + 400);
}

#[test]
fn renaming_changes_the_entries_the_last_attempt_created() {
    let mut p = game();
    ok(p.play(0));
    ok(p.record_goal(12, 2000, "Ann"));
    assert!(ok(p.rename_player("Bea", RenameTarget::Level)));
    let Some(scores) = p.level_scores(0) else {
        panic!("level 0 has scores");
    };
    assert_eq!(scores.time.entry(Rank::Medium).player, "Bea");
    assert_eq!(scores.goal.entry(Rank::Medium).player, "Bea");
    assert_eq!(scores.coin.entry(Rank::Easy).player, "Bea");
    assert_eq!(scores.time.entry(Rank::Hard).player, "Hard");
}

#[test]
fn renaming_a_standalone_result_asks_for_no_save() {
    let mut p = game();
    p.new_game(Mode::Standalone);
    ok(p.play(0));
    ok(p.record_goal(12, 2000, "Ann"));
    assert!(!ok(p.rename_player("Bea", RenameTarget::Level)));
    assert_eq!(
        p.level_scores(0)
            .map(|s| s.time.entry(Rank::Medium).player.as_str()),
        Some("Bea")
    );
}

#[test]
fn impossible_results_are_refused_and_change_nothing() {
    let mut p = game();
    assert!(matches!(
        err(p.record_goal(1, 1, "A")),
        ProgressError::NoActiveLevel
    ));
    ok(p.play(0));
    let before = p.to_save_text();
    for (coins, clock) in [(-1, 100), (5, -1), (5, 3001)] {
        assert!(
            matches!(
                err(p.record_goal(coins, clock, "A")),
                ProgressError::InvalidResult { .. }
            ),
            "coins {coins} clock {clock}"
        );
    }
    assert_eq!(p.to_save_text(), before);
    assert_eq!((p.balls(), p.score(), p.times()), (2, 0, 0));
}

#[test]
fn an_attempt_has_only_one_result() {
    let mut p = game();
    ok(p.play(0));
    ok(p.record_fall(0));
    assert!(matches!(
        err(p.record_goal(10, 0, "A")),
        ProgressError::AlreadyRecorded
    ));
    assert!(matches!(
        err(p.record_timeout(0)),
        ProgressError::AlreadyRecorded
    ));
    assert_eq!(p.balls(), 1);
}

// ---------------------------------------------------------------------------
// The save format.

fn tiny() -> Progress {
    let mut a = info(5, 0, false, 3);
    a.scores = Scores {
        time: HighScores {
            timer: [100, 200, 300],
            coins: [0; 3],
        },
        goal: HighScores {
            timer: [100, 200, 300],
            coins: [5; 3],
        },
        coin: HighScores {
            timer: [60; 3],
            coins: [50, 40, 30],
        },
    };
    let mut b = a.clone();
    b.version_num = 4;
    ok(Progress::new(&set("tiny", &["l1.sol", "l2.sol"]), &[a, b]))
}

const TINY_FRESH: &str = "version 2\n\
    set tiny\n\
    3000 0 Hard\n\
    4000 0 Medium\n\
    5000 0 Easy\n\
    359999 30 Hard\n\
    359999 20 Medium\n\
    359999 10 Easy\n\
    level 0 3 l1.sol\n\
    100 0 Hard\n\
    200 0 Medium\n\
    300 0 Easy\n\
    100 5 Hard\n\
    200 5 Medium\n\
    300 5 Easy\n\
    60 50 Hard\n\
    60 40 Medium\n\
    60 30 Easy\n\
    level 1 4 l2.sol\n\
    100 0 Hard\n\
    200 0 Medium\n\
    300 0 Easy\n\
    100 5 Hard\n\
    200 5 Medium\n\
    300 5 Easy\n\
    60 50 Hard\n\
    60 40 Medium\n\
    60 30 Easy\n";

#[test]
fn a_fresh_save_is_written_in_the_original_version_2_format() {
    assert_eq!(tiny().to_save_text(), TINY_FRESH);
}

#[test]
fn a_result_changes_the_flags_and_tables_in_the_written_save() {
    let mut p = tiny();
    ok(p.play(0));
    // No time limit: the clock is the elapsed time, 150.
    ok(p.record_goal(5, 150, "Ann"));
    let expected = TINY_FRESH
        .replacen("level 0 3 l1.sol\n100 0 Hard\n200 0 Medium\n300 0 Easy\n100 5 Hard\n200 5 Medium\n300 5 Easy\n",
            "level 2 3 l1.sol\n100 0 Hard\n150 5 Ann\n200 0 Medium\n100 5 Hard\n150 5 Ann\n200 5 Medium\n", 1)
        .replacen("level 1 4 l2.sol", "level 0 4 l2.sol", 1);
    assert_eq!(p.to_save_text(), expected);
}

#[test]
fn the_exact_text_the_game_writes_loads_back_unchanged() {
    let mut p = tiny();
    ok(p.load_text(TINY_FRESH));
    assert_eq!(p.to_save_text(), TINY_FRESH);
}

#[test]
fn progress_survives_a_relaunch_through_the_save_file() {
    let root = TempDir::new("progress-relaunch");
    let mut p = game();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    ok(p.record_goal(12, 2000, "Ann"));
    ok(p.play_next());
    ok(p.record_goal(10, 2500, "Ann"));
    let path = ok(p.store(&root.0));
    assert_eq!(path, root.0.join("Scores").join("test-set.txt"));

    let mut relaunched = game();
    assert_eq!(ok(relaunched.load(&root.0)), LoadStatus::Loaded);
    for index in 0..5 {
        assert_eq!(
            relaunched.is_unlocked(index),
            p.is_unlocked(index),
            "unlocked {index}"
        );
        assert_eq!(
            relaunched.is_completed(index),
            p.is_completed(index),
            "completed {index}"
        );
        assert_eq!(
            relaunched.level_scores(index),
            p.level_scores(index),
            "scores {index}"
        );
    }
    assert_eq!(relaunched.set_scores(), p.set_scores());
    assert!(relaunched.is_completed(0));
    assert!(relaunched.is_completed(1));
    assert!(relaunched.is_unlocked(3));
    assert_eq!(
        relaunched
            .level_scores(0)
            .map(|s| s.time.entry(Rank::Medium).clone()),
        Some(entry("Ann", 1000, 12))
    );
}

#[test]
fn a_missing_save_file_is_not_an_error_and_changes_nothing() {
    let root = TempDir::new("progress-missing");
    let mut p = game();
    let before = p.to_save_text();
    assert_eq!(ok(p.load(&root.0)), LoadStatus::Missing);
    assert_eq!(p.to_save_text(), before);
    assert!(!p.has_unreadable_save());
}

#[test]
fn storing_twice_replaces_the_save_and_leaves_no_temporary_file() {
    let root = TempDir::new("progress-store-twice");
    let mut p = game();
    ok(p.store(&root.0));
    ok(p.play(0));
    ok(p.record_goal(10, 2000, "Ann"));
    let path = ok(p.store(&root.0));
    let Ok(text) = std::fs::read_to_string(&path) else {
        panic!("cannot read {path:?}");
    };
    assert_eq!(text, p.to_save_text());
    let Ok(listing) = std::fs::read_dir(root.0.join("Scores")) else {
        panic!("cannot list Scores");
    };
    let names: Vec<String> = listing
        .filter_map(Result::ok)
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(names, ["test-set.txt"]);
}

#[test]
fn player_names_with_line_breaks_cannot_corrupt_a_save() {
    let mut p = game();
    ok(p.play(0));
    ok(p.record_goal(12, 2000, "A\nlevel 0 1 a.sol\nB"));
    let text = p.to_save_text();
    let mut q = game();
    ok(q.load_text(&text));
    assert_eq!(q.to_save_text(), text);
    assert_eq!(
        q.level_scores(0)
            .map(|s| s.time.entry(Rank::Medium).player.as_str()),
        Some("Alevel 0 1 a.solB")
    );
}

// ---------------------------------------------------------------------------
// Loading rules of set_load_hs_v2 and set_load_hs_v1.

#[test]
fn a_version_2_save_with_matching_levels_restores_levels_and_set_tables() {
    let mut p = pair();
    ok(p.load_text(&save_text("pair", &[(2, 1, "a.sol"), (0, 1, "b.sol")])));
    assert!(p.is_unlocked(0));
    assert!(p.is_completed(0));
    assert!(p.is_unlocked(1));
    assert!(!p.is_completed(1));
    let Some(a) = p.level_scores(0) else {
        panic!("level 0 has scores");
    };
    assert_eq!(a.time.entry(Rank::Hard), &block_entry(1000, "T", 0));
    assert_eq!(a.goal.entry(Rank::Medium), &block_entry(1100, "G", 1));
    assert_eq!(a.coin.entry(Rank::Easy), &block_entry(1200, "C", 2));
    assert_eq!(
        p.set_scores().time.entry(Rank::Hard),
        &block_entry(100, "ST", 0)
    );
    assert_eq!(
        p.set_scores().coin.entry(Rank::Medium),
        &block_entry(200, "SC", 1)
    );
}

#[test]
fn a_level_whose_version_changed_keeps_its_lock_flag_but_loses_completion_and_scores() {
    let fresh = pair();
    let mut p = pair();
    // File version 2, level version 1: flags 3 = locked and completed.
    ok(p.load_text(&save_text("pair", &[(2, 1, "a.sol"), (3, 2, "b.sol")])));
    assert!(p.is_completed(0), "the matching level is restored");
    assert!(!p.is_unlocked(1), "the locked flag is taken from the file");
    assert!(!p.is_completed(1), "completion needs a version match");
    assert_eq!(p.level_scores(1), fresh.level_scores(1));
    assert_eq!(
        p.set_scores(),
        fresh.set_scores(),
        "a mismatch voids the set tables"
    );
}

#[test]
fn the_lock_flag_is_taken_even_when_the_level_version_differs() {
    let mut p = pair();
    ok(p.load_text(&save_text("pair", &[(2, 1, "a.sol"), (0, 9, "b.sol")])));
    assert!(
        p.is_unlocked(1),
        "flag 0 unlocks level 1 despite the version"
    );
    assert!(!p.is_completed(1));
}

#[test]
fn a_level_the_set_does_not_have_voids_the_set_tables_but_not_the_other_levels() {
    let fresh = pair();
    let mut p = pair();
    ok(p.load_text(&save_text("pair", &[(2, 1, "a.sol"), (0, 1, "zzz.sol")])));
    assert!(p.is_completed(0));
    assert_eq!(p.set_scores(), fresh.set_scores());
    assert!(!p.is_unlocked(1));
}

#[test]
fn levels_are_matched_by_file_name_not_by_position() {
    let mut p = pair();
    ok(p.load_text(&save_text("pair", &[(0, 1, "b.sol"), (2, 1, "a.sol")])));
    assert!(p.is_completed(0));
    assert!(p.is_unlocked(1));
    let Some(a) = p.level_scores(0) else {
        panic!("level 0 has scores");
    };
    assert_eq!(a.time.entry(Rank::Hard), &block_entry(2000, "T", 0));
}

#[test]
fn a_save_for_a_different_set_is_refused() {
    let mut p = pair();
    let before = p.to_save_text();
    let e = err(p.load_text(&save_text("other", &[(2, 1, "a.sol")])));
    assert!(matches!(e, ProgressError::SetMismatch { .. }));
    assert_eq!(p.to_save_text(), before);
}

#[test]
fn a_legacy_save_restores_level_states_scores_and_set_tables() {
    let mut text = String::from("CU\n");
    text.push_str(&block(100, "ST"));
    text.push_str(&block(200, "SC"));
    for base in [1000, 2000] {
        text.push_str(&block(base, "T"));
        text.push_str(&block(base + 100, "G"));
        text.push_str(&block(base + 200, "C"));
    }
    let mut p = pair();
    ok(p.load_text(&text));
    assert!(p.is_unlocked(0));
    assert!(p.is_completed(0), "C means completed");
    assert!(p.is_unlocked(1), "any letter but L means open");
    assert!(!p.is_completed(1));
    assert_eq!(
        p.set_scores().coin.entry(Rank::Hard),
        &block_entry(200, "SC", 0)
    );
    let Some(b) = p.level_scores(1) else {
        panic!("level 1 has scores");
    };
    assert_eq!(b.coin.entry(Rank::Medium), &block_entry(2200, "C", 1));
}

#[test]
fn a_legacy_state_line_shorter_than_the_set_leaves_the_rest_at_their_defaults() {
    let fresh = pair();
    let mut text = String::from("C\n");
    text.push_str(&block(100, "ST"));
    text.push_str(&block(200, "SC"));
    text.push_str(&block(1000, "T"));
    text.push_str(&block(1100, "G"));
    text.push_str(&block(1200, "C"));
    let mut p = pair();
    ok(p.load_text(&text));
    assert!(p.is_completed(0));
    assert!(!p.is_unlocked(1));
    assert_eq!(p.level_scores(1), fresh.level_scores(1));
}

#[test]
fn loading_starts_from_a_fresh_set_not_from_the_current_state() {
    let mut p = pair();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    ok(p.record_goal(10, 2000, "Ann"));
    assert!(p.is_completed(0));
    ok(p.load_text(&save_text("pair", &[(1, 1, "b.sol")])));
    assert!(!p.is_completed(0), "level a is not in the file");
    assert!(p.is_unlocked(0));
}

// ---------------------------------------------------------------------------
// Malformed input.

fn malformed_inputs() -> Vec<(&'static str, String)> {
    let complete = save_text("pair", &[(2, 1, "a.sol"), (0, 1, "b.sol")]);
    let lines: Vec<&str> = complete.lines().collect();
    let truncated_last_block = format!("{}\n", lines[..lines.len() - 2].join("\n"));
    vec![
        ("empty file", String::new()),
        ("unsupported version", "version 3\nset pair\n".to_owned()),
        ("another set", save_text("other", &[(2, 1, "a.sol")])),
        (
            "set table cut short",
            "version 2\nset pair\n1 2 Hard\n".to_owned(),
        ),
        (
            "garbage in a score line",
            "version 2\nset pair\nx y z\n".to_owned(),
        ),
        (
            "level table cut short",
            "version 2\nlevel 2 1 a.sol\n1 1 P\n".to_owned(),
        ),
        ("last level block truncated", truncated_last_block),
        ("legacy tables cut short", "CC\n1 2 P\n".to_owned()),
        ("legacy garbage", "CC\nnot a score\n".to_owned()),
    ]
}

#[test]
fn malformed_save_text_is_an_error_that_changes_nothing() {
    let mut p = pair();
    p.new_game(Mode::Challenge);
    ok(p.play(0));
    ok(p.record_goal(12, 2000, "Ann"));
    let committed = p.to_save_text();
    for (what, text) in malformed_inputs() {
        let e = err(p.load_text(&text));
        assert!(
            matches!(
                e,
                ProgressError::Save { .. }
                    | ProgressError::UnsupportedVersion { .. }
                    | ProgressError::SetMismatch { .. }
            ),
            "{what}: {e}"
        );
        assert_eq!(
            p.to_save_text(),
            committed,
            "{what} changed committed progress"
        );
        assert!(p.is_completed(0), "{what}");
    }
}

#[test]
fn a_save_that_fails_halfway_applies_none_of_its_levels() {
    let mut p = pair();
    let fresh = p.to_save_text();
    let complete = save_text("pair", &[(2, 1, "a.sol"), (0, 1, "b.sol")]);
    let lines: Vec<&str> = complete.lines().collect();
    // Level a's block is complete and valid; level b's block is cut short.
    let text = format!("{}\n", lines[..lines.len() - 4].join("\n"));
    assert!(matches!(
        err(p.load_text(&text)),
        ProgressError::Save { .. }
    ));
    assert_eq!(p.to_save_text(), fresh);
}

#[test]
fn an_unreadable_save_file_is_reported_kept_and_never_overwritten() {
    let root = TempDir::new("progress-corrupt");
    let corrupt = b"version 2\nset pair\ngarbage\n";
    let path = root.write("Scores/pair.txt", corrupt);

    let mut p = pair();
    let before = p.to_save_text();
    assert!(matches!(err(p.load(&root.0)), ProgressError::Save { .. }));
    assert!(p.has_unreadable_save());
    assert_eq!(p.to_save_text(), before);

    assert!(matches!(
        err(p.store(&root.0)),
        ProgressError::UnreadableSave
    ));
    assert_eq!(std::fs::read(&path).ok().as_deref(), Some(&corrupt[..]));
}

#[test]
fn a_save_that_is_not_utf8_is_unreadable_not_replaced() {
    let root = TempDir::new("progress-binary");
    let bytes: &[u8] = b"\xff\xfe\x00 not text";
    let path = root.write("Scores/pair.txt", bytes);
    let mut p = pair();
    assert!(matches!(
        err(p.load(&root.0)),
        ProgressError::Save { line: 0, .. }
    ));
    assert!(matches!(
        err(p.store(&root.0)),
        ProgressError::UnreadableSave
    ));
    assert_eq!(std::fs::read(&path).ok().as_deref(), Some(bytes));
}

#[test]
fn quarantining_moves_the_bad_save_aside_and_lets_a_fresh_one_be_stored() {
    let root = TempDir::new("progress-quarantine");
    let corrupt = b"version 2\nset pair\ngarbage\n";
    let path = root.write("Scores/pair.txt", corrupt);
    let mut p = pair();
    let _ = err(p.load(&root.0));

    let moved = ok(p.quarantine_unreadable_save(&root.0));
    assert_eq!(moved, Some(root.0.join("Scores").join("pair.txt.bad0")));
    assert!(!p.has_unreadable_save());
    assert_eq!(
        std::fs::read(root.0.join("Scores").join("pair.txt.bad0"))
            .ok()
            .as_deref(),
        Some(&corrupt[..])
    );
    assert!(!path.exists());

    ok(p.store(&root.0));
    let mut q = pair();
    assert_eq!(ok(q.load(&root.0)), LoadStatus::Loaded);
}

#[test]
fn a_second_quarantine_does_not_overwrite_the_first() {
    let root = TempDir::new("progress-quarantine-twice");
    let mut p = pair();
    root.write("Scores/pair.txt", b"one");
    let _ = err(p.load(&root.0));
    ok(p.quarantine_unreadable_save(&root.0));
    root.write("Scores/pair.txt", b"two");
    let _ = err(p.load(&root.0));
    let moved = ok(p.quarantine_unreadable_save(&root.0));
    assert_eq!(moved, Some(root.0.join("Scores").join("pair.txt.bad1")));
    assert_eq!(
        std::fs::read(root.0.join("Scores").join("pair.txt.bad0"))
            .ok()
            .as_deref(),
        Some(&b"one"[..])
    );
}

#[test]
fn a_successful_load_after_a_failed_one_allows_storing_again() {
    let root = TempDir::new("progress-recover");
    let path = root.write("Scores/pair.txt", b"garbage that is not a save");
    let mut p = pair();
    let _ = err(p.load(&root.0));
    assert!(p.has_unreadable_save());
    assert!(
        std::fs::write(
            &path,
            save_text("pair", &[(2, 1, "a.sol"), (0, 1, "b.sol")])
        )
        .is_ok()
    );
    assert_eq!(ok(p.load(&root.0)), LoadStatus::Loaded);
    assert!(!p.has_unreadable_save());
    ok(p.store(&root.0));
}

// ---------------------------------------------------------------------------
// Construction and path safety.

#[test]
fn set_ids_that_could_leave_the_scores_directory_are_refused() {
    let long = "x".repeat(65);
    let bad = [
        "",
        ".",
        "..",
        "../x",
        "..\\x",
        "a/b",
        "a\\b",
        "C:evil",
        "a b",
        "a\0b",
        ".hidden",
        "-flag",
        "trail.",
        "a..b",
        "con",
        "NUL",
        "Com1",
        "lpt9.extra",
        "caf\u{e9}",
        long.as_str(),
    ];
    for id in bad {
        let e = err(Progress::new(&set(id, &["a.sol"]), &[info(1, 1, false, 1)]));
        assert!(matches!(e, ProgressError::InvalidSetId { .. }), "id {id:?}");
    }
}

#[test]
fn ordinary_set_ids_are_accepted_and_stay_inside_the_scores_directory() {
    let root = TempDir::new("progress-ids");
    for id in ["easy", "set-easy", "mym2", "Set_1.5", "console", "com10"] {
        let p = ok(Progress::new(&set(id, &["a.sol"]), &[info(1, 1, false, 1)]));
        let path = p.save_path(&root.0);
        assert_eq!(
            path,
            root.0.join("Scores").join(format!("{id}.txt")),
            "id {id}"
        );
        assert_eq!(
            path.parent(),
            Some(root.0.join("Scores").as_path()),
            "id {id}"
        );
    }
}

#[test]
fn the_level_list_must_match_the_set() {
    let e = err(Progress::new(
        &set("s", &["a.sol", "b.sol"]),
        &[info(1, 1, false, 1)],
    ));
    assert!(matches!(
        e,
        ProgressError::LevelCountMismatch {
            set: 2,
            supplied: 1
        }
    ));
}

#[test]
fn a_level_file_name_with_a_line_break_is_refused() {
    let e = err(Progress::new(
        &set("s", &["a\nb.sol"]),
        &[info(1, 1, false, 1)],
    ));
    assert!(matches!(e, ProgressError::InvalidLevelFile { index: 0 }));
}

#[test]
fn a_set_of_more_than_the_game_limit_is_refused() {
    let files: Vec<String> = (0..26).map(|i| format!("l{i}.sol")).collect();
    let refs: Vec<&str> = files.iter().map(String::as_str).collect();
    let infos: Vec<LevelInfo> = (0..26).map(|_| info(1, 1, false, 1)).collect();
    let e = err(Progress::new(&set("s", &refs), &infos));
    assert!(matches!(e, ProgressError::TooManyLevels { count: 26 }));
}

#[test]
fn a_set_with_no_levels_is_valid() {
    let p = ok(Progress::new(&set("empty", &[]), &[]));
    assert_eq!(p.level_count(), 0);
    assert!(!p.is_unlocked(0));
}
