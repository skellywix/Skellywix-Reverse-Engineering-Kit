// SPDX-License-Identifier: GPL-2.0-or-later
//
// Rust port of the score ranking and progress rules of Neverball 1.6.0
// (ball/score.c, ball/level.c, ball/set.c and ball/progress.c). Original:
// Copyright (C) 2003-2007 Robert Kooima and the Neverball authors. See
// NOTICE.md and COPYING.

//! Score ranking, level unlocking, lives and persistent save files for one
//! level set.
//!
//! [`Progress`] owns everything the 1.6.0 game keeps per set: which levels are
//! unlocked and completed, the three score tables of every level, the two
//! set-wide score tables, and the counters of the run in progress (balls,
//! coins, time). It does no simulation and no I/O except in
//! [`Progress::load`], [`Progress::store`] and
//! [`Progress::quarantine_unreadable_save`].
//!
//! # Usage contract for a game shell
//!
//! ```text
//! let mut progress = Progress::new(&set, &level_infos)?;   // levels in set order
//! progress.load(&save_root)?;                              // before the first store
//! progress.new_game(Mode::Normal);                         // progress_init
//! let start = progress.play(index)?;                       // progress_play
//! // ... simulate; at the end of the attempt, exactly one of:
//! let r = progress.record_goal(coins, clock, player)?;     // GAME_GOAL
//! let r = progress.record_fall(clock)?;                    // GAME_FALL
//! let r = progress.record_timeout(clock)?;                 // GAME_TIME
//! if r.needs_save { progress.store(&save_root)?; }         // set_store_hs
//! // then, as the menu allows:
//! progress.play_next()?; progress.retry()?;                // progress_next / progress_same
//! progress.finish_set(player)?;                            // progress_exit (challenge)
//! ```
//!
//! `clock` is the game clock exactly as `curr_clock()` reports it: it counts
//! down from [`Progress::level_time_limit`] when the level has a time limit
//! and counts up from zero when it does not. The elapsed time the score
//! tables use is derived here (`progress_stat`).
//!
//! # Deliberate differences from the C game
//!
//! * A save file that does not parse is an error and changes nothing. The C
//!   loader keeps whatever it had already read. Wrong set id and unsupported
//!   save versions are errors too (the C loader ignores both silently).
//! * Saves are written to a temporary file and renamed into place, so a
//!   failed write cannot truncate the previous save. A save that failed to
//!   load is never overwritten: [`Progress::store`] refuses until the file is
//!   moved aside with [`Progress::quarantine_unreadable_save`].
//! * The file name is `Scores/<set id>.txt` under the caller's save root, and
//!   the set id must be a plain file-name stem (see [`ProgressError::InvalidSetId`]).
//! * Player names lose control characters and leading blanks and are cut to
//!   255 bytes, so every name survives a save and load unchanged.
//!
//! Not ported: cheat mode (`<id>-cheat.txt`, `set_cheat`, playing locked
//! levels), demo replays, and the rule that a goal-reaching tick uses the
//! goal value from the previous step (here the goal counts as reached when
//! `coins` is at least the level goal).

use core::fmt;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use crate::level::LevelInfo;
use crate::set::{DEFAULT_SET_TIMER, MAX_LEVELS, Set};
use crate::text::{read_lines, safe_copy, scan_int};

/// Version number written to and required in a save file (`SCORE_VERSION`).
pub const SCORE_VERSION: i32 = 2;

/// Directory under the save root that holds score files (`Scores/`).
pub const SCORE_DIR: &str = "Scores";

/// Balls a new game starts with (`progress_init`).
pub const START_BALLS: i32 = 2;

/// A ball is awarded each time the running coin total passes a multiple of
/// this (`progress_reward_ball`).
pub const BALL_REWARD_INTERVAL: i32 = 100;

/// Longest accepted set id, in bytes (`PATHMAX`).
const MAX_ID_LEN: usize = 64;
/// Buffer size of a player name (`MAXSTR`); `SAFECPY` keeps one byte less.
const PLAYER_CAPACITY: usize = 256;
/// Save-file flag: the level is locked (`LEVEL_LOCKED`).
const LEVEL_LOCKED: i32 = 0x1;
/// Save-file flag: the level is completed (`LEVEL_COMPLETED`).
const LEVEL_COMPLETED: i32 = 0x2;
/// Quarantine names tried before giving up.
const MAX_QUARANTINE_FILES: u32 = 1000;

const RANKS: usize = 3;
const DEFAULT_PLAYERS: [&str; RANKS] = ["Hard", "Medium", "Easy"];

/// Roman numerals naming bonus levels (`roman[]` in `set_load_levels`).
const ROMAN: [&str; MAX_LEVELS + 1] = [
    "", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV",
    "XV", "XVI", "XVII", "XVIII", "XIX", "XX", "XXI", "XXII", "XXIII", "XXIV", "XXV",
];

// ---------------------------------------------------------------------------
// Score ranking (ball/score.c).

/// A place on a score table. Missing a place is `None` (`RANK_LAST`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Rank {
    /// First place (`RANK_HARD`).
    Hard,
    /// Second place (`RANK_MEDM`).
    Medium,
    /// Third place (`RANK_EASY`).
    Easy,
}

impl Rank {
    /// The three ranks, best first.
    pub const ALL: [Self; RANKS] = [Self::Hard, Self::Medium, Self::Easy];

    /// Position in a table: 0, 1 or 2.
    #[must_use]
    pub const fn index(self) -> usize {
        self as usize
    }

    const fn from_index(index: usize) -> Option<Self> {
        match index {
            0 => Some(Self::Hard),
            1 => Some(Self::Medium),
            2 => Some(Self::Easy),
            _ => None,
        }
    }
}

/// One line of a score table.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ScoreEntry {
    /// Player name (`Hard`, `Medium` and `Easy` in a table nobody has beaten).
    pub player: String,
    /// Elapsed time in hundredths of a second.
    pub timer: i32,
    /// Coins collected.
    pub coins: i32,
}

/// `(timer, coins)` of a candidate or an entry.
type Pair = (i32, i32);

/// `score_time_comp`: less time wins, then more coins.
fn better_by_time(candidate: Pair, entry: Pair) -> bool {
    candidate.0 < entry.0 || (candidate.0 == entry.0 && candidate.1 > entry.1)
}

/// `score_coin_comp`: more coins win, then less time.
fn better_by_coins(candidate: Pair, entry: Pair) -> bool {
    candidate.1 > entry.1 || (candidate.1 == entry.1 && candidate.0 < entry.0)
}

/// A three-place score table (`struct score`).
///
/// An entry that ties a table entry exactly (same time and same coins) ranks
/// below it, as in the original.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScoreTable {
    entries: [ScoreEntry; RANKS],
}

impl ScoreTable {
    /// A table nobody has entered yet (`score_init_hs` followed by the level
    /// or set thresholds): ranks named `Hard`, `Medium` and `Easy` holding
    /// the given thresholds, best first.
    #[must_use]
    pub fn new(timers: [i32; RANKS], coins: [i32; RANKS]) -> Self {
        Self {
            entries: std::array::from_fn(|i| ScoreEntry {
                player: DEFAULT_PLAYERS[i].to_owned(),
                timer: timers[i],
                coins: coins[i],
            }),
        }
    }

    /// The three entries, best first.
    #[must_use]
    pub fn entries(&self) -> &[ScoreEntry; RANKS] {
        &self.entries
    }

    /// The entry at `rank`.
    #[must_use]
    pub fn entry(&self, rank: Rank) -> &ScoreEntry {
        &self.entries[rank.index()]
    }

    /// The rank a result would take in a table ordered by time (`SCORE_TIME`
    /// and `SCORE_GOAL`), without changing the table.
    #[must_use]
    pub fn time_rank(&self, timer: i32, coins: i32) -> Option<Rank> {
        Rank::from_index(self.position(better_by_time, (timer, coins)))
    }

    /// The rank a result would take in a table ordered by coins
    /// (`SCORE_COIN`), without changing the table.
    #[must_use]
    pub fn coin_rank(&self, timer: i32, coins: i32) -> Option<Rank> {
        Rank::from_index(self.position(better_by_coins, (timer, coins)))
    }

    /// `score_time_insert`: enters a result in a time-ordered table. Lower
    /// entries move down and the last one drops off. Returns the place taken,
    /// or `None` when the result is not good enough (the table is unchanged).
    pub fn insert_by_time(&mut self, player: &str, timer: i32, coins: i32) -> Option<Rank> {
        self.insert(better_by_time, player, timer, coins)
    }

    /// `score_coin_insert`: like [`insert_by_time`](Self::insert_by_time) for
    /// a coin-ordered table.
    pub fn insert_by_coins(&mut self, player: &str, timer: i32, coins: i32) -> Option<Rank> {
        self.insert(better_by_coins, player, timer, coins)
    }

    /// Replaces the player name at `rank` (the post-game name entry).
    pub fn rename(&mut self, rank: Rank, player: &str) {
        self.entries[rank.index()].player = clean_player(player);
    }

    /// Walks the candidate up from the bottom while it beats the entry above,
    /// exactly like the swap loop of `score_*_insert`. Returns the index it
    /// stops at; [`RANKS`] means it did not place.
    fn position(&self, better: fn(Pair, Pair) -> bool, candidate: Pair) -> usize {
        let mut position = RANKS;
        while position > 0 {
            let above = &self.entries[position - 1];
            if !better(candidate, (above.timer, above.coins)) {
                break;
            }
            position -= 1;
        }
        position
    }

    fn insert(
        &mut self,
        better: fn(Pair, Pair) -> bool,
        player: &str,
        timer: i32,
        coins: i32,
    ) -> Option<Rank> {
        let position = self.position(better, (timer, coins));
        if position < RANKS {
            self.entries[position..].rotate_right(1);
            self.entries[position] = ScoreEntry {
                player: clean_player(player),
                timer,
                coins,
            };
        }
        Rank::from_index(position)
    }
}

/// C `isspace` in the C locale.
fn is_c_space(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\u{b}' | '\u{c}' | '\r')
}

/// A player name that survives a save file round trip: no control characters
/// (a newline would split the line), no leading blanks (the loader skips
/// them), at most 255 bytes.
fn clean_player(name: &str) -> String {
    let printable: String = name.chars().filter(|c| !c.is_control()).collect();
    safe_copy(printable.trim_start_matches(is_c_space), PLAYER_CAPACITY)
}

/// The three tables of a level (`scores[SCORE_TIME]`, `SCORE_GOAL`,
/// `SCORE_COIN`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LevelScores {
    /// Fastest times; every attempt that reaches the goal enters it.
    pub time: ScoreTable,
    /// Fastest times among attempts that collected at least the goal's coins.
    pub goal: ScoreTable,
    /// Most coins.
    pub coin: ScoreTable,
}

impl LevelScores {
    fn from_info(info: &LevelInfo) -> Self {
        let s = &info.scores;
        Self {
            time: ScoreTable::new(s.time.timer, s.time.coins),
            goal: ScoreTable::new(s.goal.timer, s.goal.coins),
            coin: ScoreTable::new(s.coin.timer, s.coin.coins),
        }
    }

    /// `level_score_update`. The goal table is entered only when `goal_met`.
    fn update(&mut self, player: &str, timer: i32, coins: i32, goal_met: bool) -> Placement {
        let time = self.time.insert_by_time(player, timer, coins);
        let goal = if goal_met {
            self.goal.insert_by_time(player, timer, coins)
        } else {
            None
        };
        let coin = self.coin.insert_by_coins(player, timer, coins);
        Placement { time, goal, coin }
    }
}

/// The two set-wide tables, filled by a finished challenge run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SetScores {
    /// Fastest total times (`time_score`).
    pub time: ScoreTable,
    /// Most total coins (`coin_score`).
    pub coin: ScoreTable,
}

/// Where one attempt landed on the level's tables.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Placement {
    /// Place on the best-time table.
    pub time: Option<Rank>,
    /// Place on the goal table; `None` when the goal's coins were not met.
    pub goal: Option<Rank>,
    /// Place on the most-coins table.
    pub coin: Option<Rank>,
}

impl Placement {
    /// Whether any table was entered (`progress_lvl_high`).
    #[must_use]
    pub fn is_high_score(&self) -> bool {
        self.time.is_some() || self.goal.is_some() || self.coin.is_some()
    }
}

/// Where a finished challenge run landed on the set's tables.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct SetPlacement {
    /// Place on the set's best-total-time table (`times_rank`).
    pub time: Option<Rank>,
    /// Place on the set's most-total-coins table (`score_rank`).
    pub coin: Option<Rank>,
}

impl SetPlacement {
    /// Whether either table was entered (`progress_set_high`).
    #[must_use]
    pub fn is_high_score(&self) -> bool {
        self.time.is_some() || self.coin.is_some()
    }
}

// ---------------------------------------------------------------------------
// Progress state.

/// Game mode (`MODE_CHALLENGE`, `MODE_NORMAL`, `MODE_STANDALONE`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    /// Limited balls; the set must be played through; bonus levels open as the
    /// run passes them.
    Challenge,
    /// Free play of unlocked levels; a bonus level opens only when it was
    /// opened before.
    Normal,
    /// Single level outside a set run; results are kept in memory but
    /// [`Recorded::needs_save`] is never set.
    Standalone,
}

/// How an attempt ended (`GAME_GOAL`, `GAME_FALL`, `GAME_TIME`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    /// The ball entered the open goal.
    Goal,
    /// The ball fell off the level.
    Fall,
    /// The time limit ran out.
    Timeout,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Counters {
    balls: i32,
    score: i32,
    times: i32,
}

/// The attempt in progress or just finished (`level`, `next`, `status`, `goal_i`).
#[derive(Debug, Clone, Copy)]
struct Session {
    level: usize,
    next: Option<usize>,
    status: Option<Outcome>,
    goal_initial: i32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct LevelRecord {
    file: String,
    version: i32,
    bonus: bool,
    goal: i32,
    time: i32,
    locked: bool,
    completed: bool,
    scores: LevelScores,
}

/// What survives a relaunch: exactly the contents of the save file.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Persistent {
    set: SetScores,
    levels: Vec<LevelRecord>,
}

/// What [`Progress::play`] tells the simulation to start.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LevelStart {
    /// Index of the level in the set.
    pub index: usize,
    /// Coins needed to open the goal (`level_goal`).
    pub goal: i32,
    /// Time limit in hundredths of a second; zero means none (`level_time`).
    pub time_limit: i32,
    /// Whether the goal is open from the first tick (`goal_e`): the level is
    /// already completed (and not challenge mode, and goals are not locked),
    /// or needs no coins.
    pub goal_open: bool,
}

/// What recording an attempt changed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Recorded {
    /// The outcome that was recorded.
    pub outcome: Outcome,
    /// Place taken on the level's tables; all `None` for a fall or timeout.
    pub placement: Placement,
    /// The level [`Progress::play_next`] would offer (`next`): after a goal,
    /// the level just opened or `None` at the end of the set; after a fall or
    /// timeout, the first later level that is already open.
    pub next: Option<usize>,
    /// Change in balls: coins' ball rewards on a goal, `-1` on a fall or
    /// timeout.
    pub ball_delta: i32,
    /// A challenge run just passed the last level (`done`); call
    /// [`Progress::finish_set`] next.
    pub set_complete: bool,
    /// The persistent state changed and the mode keeps a save (`dirty`):
    /// call [`Progress::store`].
    pub needs_save: bool,
}

/// What [`Progress::finish_set`] changed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SetRecorded {
    /// Places taken on the set's tables.
    pub placement: SetPlacement,
    /// A set table was entered: call [`Progress::store`].
    pub needs_save: bool,
}

/// Which tables [`Progress::rename_player`] renames.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RenameTarget {
    /// Only the set tables entered by [`Progress::finish_set`].
    SetOnly,
    /// The level tables entered by the last attempt, and the set tables too
    /// when the run is complete.
    Level,
}

/// Result of [`Progress::load`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LoadStatus {
    /// There is no save file; progress is unchanged.
    Missing,
    /// The save file was read and applied.
    Loaded,
}

/// Progress through one level set.
///
/// See the [module documentation](self) for the call sequence.
#[derive(Debug, Clone)]
pub struct Progress {
    set_id: String,
    /// Fresh-install state; loading starts from it (`set_load_levels`).
    pristine: Persistent,
    state: Persistent,
    mode: Mode,
    lock_goals: bool,
    curr: Counters,
    prev: Counters,
    session: Option<Session>,
    done: bool,
    set_recorded: bool,
    goal_open: bool,
    reuse_goal_open: bool,
    level_placement: Placement,
    set_placement: SetPlacement,
    unreadable_save: bool,
}

impl Progress {
    /// Starts a fresh set: only the first level open, nothing completed, every
    /// score table at the level's and set's thresholds (`set_load_levels`).
    /// The mode is [`Mode::Normal`] until [`new_game`](Self::new_game).
    ///
    /// `levels[i]` is the attributes of the level file `set.levels[i]`.
    ///
    /// # Errors
    ///
    /// [`ProgressError::InvalidSetId`] if `set.id` cannot be a file-name stem,
    /// [`ProgressError::LevelCountMismatch`] if `levels` and `set.levels`
    /// differ in length, [`ProgressError::TooManyLevels`] past
    /// [`MAX_LEVELS`], and [`ProgressError::InvalidLevelFile`] if a level file
    /// name contains a line break (it could not be saved).
    pub fn new(set: &Set, levels: &[LevelInfo]) -> Result<Self, ProgressError> {
        validate_set_id(&set.id)?;
        if set.levels.len() != levels.len() {
            return Err(ProgressError::LevelCountMismatch {
                set: set.levels.len(),
                supplied: levels.len(),
            });
        }
        if levels.len() > MAX_LEVELS {
            return Err(ProgressError::TooManyLevels {
                count: levels.len(),
            });
        }

        let mut records = Vec::with_capacity(levels.len());
        for (index, (file, info)) in set.levels.iter().zip(levels).enumerate() {
            if file.contains(['\n', '\r']) {
                return Err(ProgressError::InvalidLevelFile { index });
            }
            records.push(LevelRecord {
                file: file.clone(),
                version: info.version_num,
                bonus: info.is_bonus,
                goal: info.goal,
                time: info.time,
                locked: index > 0,
                completed: false,
                scores: LevelScores::from_info(info),
            });
        }

        let pristine = Persistent {
            set: SetScores {
                time: ScoreTable::new(set.time_scores, [0; RANKS]),
                coin: ScoreTable::new([DEFAULT_SET_TIMER; RANKS], set.coin_scores),
            },
            levels: records,
        };
        let mut progress = Self {
            set_id: set.id.clone(),
            state: pristine.clone(),
            pristine,
            mode: Mode::Normal,
            lock_goals: false,
            curr: Counters {
                balls: START_BALLS,
                score: 0,
                times: 0,
            },
            prev: Counters {
                balls: START_BALLS,
                score: 0,
                times: 0,
            },
            session: None,
            done: false,
            set_recorded: false,
            goal_open: false,
            reuse_goal_open: false,
            level_placement: Placement::default(),
            set_placement: SetPlacement::default(),
            unreadable_save: false,
        };
        progress.new_game(Mode::Normal);
        Ok(progress)
    }

    // -- queries -----------------------------------------------------------

    /// The set id the save file is named after.
    #[must_use]
    pub fn set_id(&self) -> &str {
        &self.set_id
    }

    /// Number of levels in the set.
    #[must_use]
    pub fn level_count(&self) -> usize {
        self.state.levels.len()
    }

    /// The level file name at `index`, as the set lists it.
    #[must_use]
    pub fn level_file(&self, index: usize) -> Option<&str> {
        self.state.levels.get(index).map(|l| l.file.as_str())
    }

    /// The level's name in the set menu: `01`, `02`, ... for regular levels and
    /// `I`, `II`, ... for bonus levels, counted separately (`l->name`).
    #[must_use]
    pub fn level_label(&self, index: usize) -> Option<String> {
        let record = self.state.levels.get(index)?;
        let before = &self.state.levels[..index];
        if record.bonus {
            let n = before.iter().filter(|l| l.bonus).count() + 1;
            ROMAN.get(n).map(|s| (*s).to_owned())
        } else {
            Some(format!(
                "{:02}",
                before.iter().filter(|l| !l.bonus).count() + 1
            ))
        }
    }

    /// Whether the level is open for play (`level_opened`). False for an index
    /// past the end.
    #[must_use]
    pub fn is_unlocked(&self, index: usize) -> bool {
        self.state.levels.get(index).is_some_and(|l| !l.locked)
    }

    /// Whether the level has been completed (`level_completed`).
    #[must_use]
    pub fn is_completed(&self, index: usize) -> bool {
        self.state.levels.get(index).is_some_and(|l| l.completed)
    }

    /// Whether the level is a bonus level.
    #[must_use]
    pub fn is_bonus(&self, index: usize) -> bool {
        self.state.levels.get(index).is_some_and(|l| l.bonus)
    }

    /// The level's time limit in hundredths of a second, zero for none.
    #[must_use]
    pub fn level_time_limit(&self, index: usize) -> Option<i32> {
        self.state.levels.get(index).map(|l| l.time)
    }

    /// The level's three score tables.
    #[must_use]
    pub fn level_scores(&self, index: usize) -> Option<&LevelScores> {
        self.state.levels.get(index).map(|l| &l.scores)
    }

    /// The set's two score tables.
    #[must_use]
    pub fn set_scores(&self) -> &SetScores {
        &self.state.set
    }

    /// The mode of the current game.
    #[must_use]
    pub fn mode(&self) -> Mode {
        self.mode
    }

    /// Balls left (`curr_balls`). Negative once a challenge run is dead.
    #[must_use]
    pub fn balls(&self) -> i32 {
        self.curr.balls
    }

    /// Coins collected in this run (`curr_score`).
    #[must_use]
    pub fn score(&self) -> i32 {
        self.curr.score
    }

    /// Elapsed time of this run in hundredths of a second (`curr.times`).
    #[must_use]
    pub fn times(&self) -> i32 {
        self.curr.times
    }

    /// Challenge mode with no balls left (`progress_dead`).
    #[must_use]
    pub fn is_dead(&self) -> bool {
        self.mode == Mode::Challenge && self.curr.balls < 0
    }

    /// A challenge run has passed the last level (`progress_done`).
    #[must_use]
    pub fn is_done(&self) -> bool {
        self.done
    }

    /// The attempt just recorded was a goal on the last level of a non-challenge
    /// game (`progress_last`).
    #[must_use]
    pub fn is_last(&self) -> bool {
        self.mode != Mode::Challenge
            && self
                .session
                .is_some_and(|s| s.status == Some(Outcome::Goal) && s.next.is_none())
    }

    /// The level being played or just played.
    #[must_use]
    pub fn current_level(&self) -> Option<usize> {
        self.session.map(|s| s.level)
    }

    /// The level [`play_next`](Self::play_next) would start, if the attempt has
    /// ended (`next`).
    #[must_use]
    pub fn next_level(&self) -> Option<usize> {
        self.session.and_then(|s| s.next)
    }

    /// Coins still needed to open the goal after `coins` have been collected
    /// (`curr_goal`): never below zero for a positive goal. Zero outside a
    /// level.
    #[must_use]
    pub fn goal_remaining(&self, coins: i32) -> i32 {
        match self.session {
            Some(s) if s.goal_initial > 0 => s.goal_initial.saturating_sub(coins).max(0),
            Some(s) => s.goal_initial,
            None => 0,
        }
    }

    /// Places the last attempt took on its level's tables (`progress_*_rank`).
    #[must_use]
    pub fn last_placement(&self) -> Placement {
        self.level_placement
    }

    /// Places the finished run took on the set's tables.
    #[must_use]
    pub fn last_set_placement(&self) -> SetPlacement {
        self.set_placement
    }

    /// Whether the next [`play_next`](Self::play_next) is offered
    /// (`progress_next_avail`).
    #[must_use]
    pub fn next_available(&self) -> bool {
        let Some(session) = self.session else {
            return false;
        };
        let Some(next) = session.next else {
            return false;
        };
        if self.mode == Mode::Challenge {
            session.status == Some(Outcome::Goal)
        } else {
            self.is_unlocked(next)
        }
    }

    /// Whether [`retry`](Self::retry) is offered (`progress_same_avail`).
    #[must_use]
    pub fn retry_available(&self) -> bool {
        match self.session {
            None => false,
            Some(session) => match session.status {
                None => self.mode != Mode::Challenge,
                Some(_) if self.mode == Mode::Challenge => !self.is_dead(),
                Some(_) => true,
            },
        }
    }

    // -- game flow ---------------------------------------------------------

    /// Whether levels already completed still start with a closed goal
    /// (`lock_goals` option, off by default). Affects later
    /// [`play`](Self::play) calls.
    pub fn set_lock_goals(&mut self, lock_goals: bool) {
        self.lock_goals = lock_goals;
    }

    /// Starts a run: two balls, no coins, no time (`progress_init`). Locks and
    /// completions are not touched.
    pub fn new_game(&mut self, mode: Mode) {
        self.mode = mode;
        self.curr = Counters {
            balls: START_BALLS,
            score: 0,
            times: 0,
        };
        self.prev = self.curr;
        self.set_placement = SetPlacement::default();
        self.level_placement = Placement::default();
        self.done = false;
        self.set_recorded = false;
        self.reuse_goal_open = false;
        self.session = None;
    }

    /// Begins an attempt at level `index` (`progress_play`).
    ///
    /// # Errors
    ///
    /// [`ProgressError::NoSuchLevel`] past the end of the set and
    /// [`ProgressError::LevelLocked`] for a locked level; nothing changes.
    pub fn play(&mut self, index: usize) -> Result<LevelStart, ProgressError> {
        let record = self
            .state
            .levels
            .get(index)
            .ok_or(ProgressError::NoSuchLevel { index })?;
        if record.locked {
            return Err(ProgressError::LevelLocked { index });
        }
        let (goal, time_limit, completed) = (record.goal, record.time, record.completed);

        if self.reuse_goal_open {
            self.reuse_goal_open = false;
        } else {
            self.goal_open =
                (self.mode != Mode::Challenge && completed && !self.lock_goals) || goal == 0;
        }
        self.prev = self.curr;
        self.level_placement = Placement::default();
        self.session = Some(Session {
            level: index,
            next: None,
            status: None,
            goal_initial: goal,
        });
        Ok(LevelStart {
            index,
            goal,
            time_limit,
            goal_open: self.goal_open,
        })
    }

    /// Starts the level offered by the last attempt (`progress_next`).
    ///
    /// # Errors
    ///
    /// [`ProgressError::NextUnavailable`] unless
    /// [`next_available`](Self::next_available); then see [`play`](Self::play).
    pub fn play_next(&mut self) -> Result<LevelStart, ProgressError> {
        if !self.next_available() {
            return Err(ProgressError::NextUnavailable);
        }
        let next = self
            .session
            .and_then(|s| s.next)
            .ok_or(ProgressError::NextUnavailable)?;
        self.play(next)
    }

    /// Plays the same level again (`progress_same`). After a goal the run's
    /// balls, coins and time go back to what they were when the level began,
    /// and the goal keeps its open or closed state.
    ///
    /// # Errors
    ///
    /// [`ProgressError::RetryUnavailable`] unless
    /// [`retry_available`](Self::retry_available);
    /// [`ProgressError::LevelLocked`] if a load locked the level meanwhile.
    pub fn retry(&mut self) -> Result<LevelStart, ProgressError> {
        if !self.retry_available() {
            return Err(ProgressError::RetryUnavailable);
        }
        let session = self.session.ok_or(ProgressError::RetryUnavailable)?;
        if !self.is_unlocked(session.level) {
            return Err(ProgressError::LevelLocked {
                index: session.level,
            });
        }
        if session.status == Some(Outcome::Goal) {
            self.curr = self.prev;
        }
        self.reuse_goal_open = true;
        self.play(session.level)
    }

    /// Records a goal (`progress_stat(GAME_GOAL)`): collects `coins`, adds the
    /// elapsed time, enters the level's tables as `player`, completes the
    /// level and opens the next one.
    ///
    /// Rewards one ball per multiple of [`BALL_REWARD_INTERVAL`] the coin
    /// total passes. In challenge mode every bonus level after this one opens
    /// and the next regular level is offered; otherwise the next level is the
    /// first later one that is regular or already open. With no such level
    /// the set is complete (challenge mode only; see [`Recorded::set_complete`]).
    /// The goal table is entered only when `coins` meets the level's goal.
    ///
    /// # Errors
    ///
    /// [`ProgressError::NoActiveLevel`] before [`play`](Self::play),
    /// [`ProgressError::AlreadyRecorded`] if the attempt already ended, and
    /// [`ProgressError::InvalidResult`] for negative `coins` or `clock`, or a
    /// `clock` above the level's time limit. Nothing changes on error.
    pub fn record_goal(
        &mut self,
        coins: i32,
        clock: i32,
        player: &str,
    ) -> Result<Recorded, ProgressError> {
        self.record(Outcome::Goal, coins, clock, player)
    }

    /// Records a fall (`progress_stat(GAME_FALL)`): the elapsed time is added,
    /// one ball is lost, and the level's state and tables stay as they were.
    ///
    /// # Errors
    ///
    /// As [`record_goal`](Self::record_goal).
    pub fn record_fall(&mut self, clock: i32) -> Result<Recorded, ProgressError> {
        self.record(Outcome::Fall, 0, clock, "")
    }

    /// Records a timeout (`progress_stat(GAME_TIME)`); the same effects as
    /// [`record_fall`](Self::record_fall).
    ///
    /// # Errors
    ///
    /// As [`record_goal`](Self::record_goal).
    pub fn record_timeout(&mut self, clock: i32) -> Result<Recorded, ProgressError> {
        self.record(Outcome::Timeout, 0, clock, "")
    }

    fn record(
        &mut self,
        outcome: Outcome,
        coins: i32,
        clock: i32,
        player: &str,
    ) -> Result<Recorded, ProgressError> {
        let session = self.session.ok_or(ProgressError::NoActiveLevel)?;
        if session.status.is_some() {
            return Err(ProgressError::AlreadyRecorded);
        }
        let index = session.level;
        let record = self
            .state
            .levels
            .get(index)
            .ok_or(ProgressError::NoSuchLevel { index })?;
        if coins < 0 {
            return Err(ProgressError::InvalidResult {
                reason: "coin count is negative",
            });
        }
        if clock < 0 {
            return Err(ProgressError::InvalidResult {
                reason: "clock is negative",
            });
        }
        if record.time > 0 && clock > record.time {
            return Err(ProgressError::InvalidResult {
                reason: "clock is above the level's time limit",
            });
        }
        // `timer` in progress_stat: elapsed time, whichever way the clock runs.
        let timer = if record.time == 0 {
            clock
        } else {
            record.time.saturating_sub(clock)
        };

        let balls_before = self.curr.balls;
        let mut placement = Placement::default();
        let mut dirty = false;
        let levels = &mut self.state.levels;

        let next = match outcome {
            Outcome::Goal => {
                let score_before = self.curr.score;
                let score_after = score_before.saturating_add(coins);
                let rewards =
                    score_after / BALL_REWARD_INTERVAL - score_before / BALL_REWARD_INTERVAL;
                self.curr.balls = self.curr.balls.saturating_add(rewards);
                self.curr.score = score_after;
                self.curr.times = self.curr.times.saturating_add(timer);

                // The C game tracks `goal` per step and clamps it to zero; a
                // negative goal never reaches zero, so it never enters the
                // goal table.
                let goal_met = session.goal_initial == 0
                    || (session.goal_initial > 0 && coins >= session.goal_initial);
                let level = &mut levels[index];
                placement = level.scores.update(player, timer, coins, goal_met);
                dirty = placement.is_high_score();
                if !level.completed {
                    level.completed = true;
                    dirty = true;
                }

                let mut cursor = index + 1;
                if self.mode == Mode::Challenge {
                    while cursor < levels.len() && levels[cursor].bonus {
                        if levels[cursor].locked {
                            levels[cursor].locked = false;
                            dirty = true;
                        }
                        cursor += 1;
                    }
                } else {
                    while cursor < levels.len() && levels[cursor].bonus && levels[cursor].locked {
                        cursor += 1;
                    }
                }
                if cursor < levels.len() {
                    levels[cursor].locked = false;
                    dirty = true;
                    Some(cursor)
                } else {
                    self.done = self.mode == Mode::Challenge;
                    None
                }
            }
            Outcome::Fall | Outcome::Timeout => {
                self.curr.times = self.curr.times.saturating_add(timer);
                self.curr.balls = self.curr.balls.saturating_sub(1);
                (index + 1..levels.len()).find(|&i| !levels[i].locked)
            }
        };

        self.session = Some(Session {
            next,
            status: Some(outcome),
            ..session
        });
        self.level_placement = placement;
        Ok(Recorded {
            outcome,
            placement,
            next,
            ball_delta: self.curr.balls.saturating_sub(balls_before),
            set_complete: outcome == Outcome::Goal && next.is_none() && self.done,
            needs_save: dirty && self.mode != Mode::Standalone,
        })
    }

    /// Enters the finished challenge run in the set's tables as `player`
    /// (`progress_exit`): total coins ranked by coins, total time ranked by time.
    ///
    /// # Errors
    ///
    /// [`ProgressError::SetNotComplete`] unless [`is_done`](Self::is_done), and
    /// [`ProgressError::SetAlreadyRecorded`] on a second call for the same run.
    pub fn finish_set(&mut self, player: &str) -> Result<SetRecorded, ProgressError> {
        if !self.done {
            return Err(ProgressError::SetNotComplete);
        }
        if self.set_recorded {
            return Err(ProgressError::SetAlreadyRecorded);
        }
        let (times, score) = (self.curr.times, self.curr.score);
        let coin = self.state.set.coin.insert_by_coins(player, times, score);
        let time = self.state.set.time.insert_by_time(player, times, score);
        let placement = SetPlacement { time, coin };
        self.set_recorded = true;
        self.set_placement = placement;
        Ok(SetRecorded {
            placement,
            needs_save: placement.is_high_score(),
        })
    }

    /// Renames the player on the entries the last results created
    /// (`progress_rename`). Returns whether [`store`](Self::store) is due.
    ///
    /// In [`Mode::Standalone`] only the level tables are renamed and nothing is
    /// due, whatever `target` says.
    ///
    /// # Errors
    ///
    /// [`ProgressError::NoActiveLevel`] when a level table is to be renamed
    /// and no level has been played.
    pub fn rename_player(
        &mut self,
        player: &str,
        target: RenameTarget,
    ) -> Result<bool, ProgressError> {
        if self.mode == Mode::Standalone {
            self.rename_level_entries(player)?;
            return Ok(false);
        }
        match target {
            RenameTarget::SetOnly => self.rename_set_entries(player),
            RenameTarget::Level => {
                self.rename_level_entries(player)?;
                if self.done {
                    self.rename_set_entries(player);
                }
            }
        }
        Ok(true)
    }

    fn rename_level_entries(&mut self, player: &str) -> Result<(), ProgressError> {
        let session = self.session.ok_or(ProgressError::NoActiveLevel)?;
        let placement = self.level_placement;
        let level = self
            .state
            .levels
            .get_mut(session.level)
            .ok_or(ProgressError::NoSuchLevel {
                index: session.level,
            })?;
        if let Some(rank) = placement.time {
            level.scores.time.rename(rank, player);
        }
        if let Some(rank) = placement.goal {
            level.scores.goal.rename(rank, player);
        }
        if let Some(rank) = placement.coin {
            level.scores.coin.rename(rank, player);
        }
        Ok(())
    }

    fn rename_set_entries(&mut self, player: &str) {
        if let Some(rank) = self.set_placement.coin {
            self.state.set.coin.rename(rank, player);
        }
        if let Some(rank) = self.set_placement.time {
            self.state.set.time.rename(rank, player);
        }
    }

    // -- persistence -------------------------------------------------------

    /// Where this set's save lives: `<root>/Scores/<set id>.txt`. The set id was
    /// validated by [`new`](Self::new), so the result is always a direct child
    /// of `<root>/Scores`.
    #[must_use]
    pub fn save_path(&self, root: &Path) -> PathBuf {
        root.join(SCORE_DIR).join(format!("{}.txt", self.set_id))
    }

    /// The save file as text (`set_store_hs`, version 2).
    #[must_use]
    pub fn to_save_text(&self) -> String {
        let mut out = format!("version {SCORE_VERSION}\nset {}\n", self.set_id);
        push_table(&mut out, &self.state.set.time);
        push_table(&mut out, &self.state.set.coin);
        for level in &self.state.levels {
            let mut flags = 0;
            if level.locked {
                flags |= LEVEL_LOCKED;
            }
            if level.completed {
                flags |= LEVEL_COMPLETED;
            }
            out.push_str(&format!("level {flags} {} {}\n", level.version, level.file));
            push_table(&mut out, &level.scores.time);
            push_table(&mut out, &level.scores.goal);
            push_table(&mut out, &level.scores.coin);
        }
        out
    }

    /// Replaces the persistent state with the contents of save text, starting
    /// from a fresh set exactly as the C loader does.
    ///
    /// Accepts version 2 (`version 2`, `set <id>`, set tables, then
    /// `level <flags> <version> <file>` blocks) and the version-less legacy
    /// format (a line of `L`ocked, `C`ompleted or other level states, then
    /// tables). Rules of the version 2 reader:
    ///
    /// * the `locked` flag of a level is always taken from the file;
    /// * its `completed` flag and tables are taken only when the file's level
    ///   version equals the level's own version;
    /// * the set tables are taken only when the file has them and every level
    ///   line named a level of this set with a matching version;
    /// * unknown lines are skipped.
    ///
    /// Run state (balls, coins, the attempt in progress) is untouched.
    ///
    /// # Errors
    ///
    /// [`ProgressError::Save`] for an empty file, an unreadable or cut-short
    /// score table; [`ProgressError::UnsupportedVersion`] for a version other
    /// than 2; [`ProgressError::SetMismatch`] if the `set` line names another
    /// set. On error the previous state is kept exactly.
    pub fn load_text(&mut self, text: &str) -> Result<(), ProgressError> {
        let staged = parse_save(&self.set_id, &self.pristine, text)?;
        self.state = staged;
        Ok(())
    }

    /// Loads [`save_path`](Self::save_path). A missing file is not an error.
    ///
    /// Call this once after [`new`](Self::new) and before the first
    /// [`store`](Self::store).
    ///
    /// # Errors
    ///
    /// [`ProgressError::Io`] if the file exists but cannot be read, and
    /// everything [`load_text`](Self::load_text) reports, plus
    /// [`ProgressError::Save`] for bytes that are not UTF-8. On error the
    /// progress is unchanged, the file is untouched, and
    /// [`store`](Self::store) refuses until the file is moved aside.
    pub fn load(&mut self, root: &Path) -> Result<LoadStatus, ProgressError> {
        let path = self.save_path(root);
        let bytes = match fs::read(&path) {
            Ok(bytes) => bytes,
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                self.unreadable_save = false;
                return Ok(LoadStatus::Missing);
            }
            Err(source) => {
                self.unreadable_save = true;
                return Err(ProgressError::Io { path, source });
            }
        };
        let result = std::str::from_utf8(&bytes)
            .map_err(|_| ProgressError::Save {
                line: 0,
                reason: "save file is not valid UTF-8",
            })
            .and_then(|text| self.load_text(text));
        self.unreadable_save = result.is_err();
        result.map(|()| LoadStatus::Loaded)
    }

    /// Whether the last [`load`](Self::load) failed on an existing file.
    #[must_use]
    pub fn has_unreadable_save(&self) -> bool {
        self.unreadable_save
    }

    /// Writes [`to_save_text`](Self::to_save_text) to
    /// [`save_path`](Self::save_path), creating `Scores/` as needed. The text
    /// goes to a temporary file in the same directory which is synced and
    /// renamed over the save, so an interrupted store leaves the old save.
    /// Returns the path written.
    ///
    /// # Errors
    ///
    /// [`ProgressError::UnreadableSave`] if the last load failed on an
    /// existing file; [`ProgressError::Io`] if writing fails.
    pub fn store(&self, root: &Path) -> Result<PathBuf, ProgressError> {
        if self.unreadable_save {
            return Err(ProgressError::UnreadableSave);
        }
        let dir = root.join(SCORE_DIR);
        fs::create_dir_all(&dir).map_err(|source| ProgressError::Io {
            path: dir.clone(),
            source,
        })?;
        let path = self.save_path(root);
        let temp = dir.join(format!("{}.txt.tmp", self.set_id));
        let written = write_synced(&temp, self.to_save_text().as_bytes())
            .and_then(|()| fs::rename(&temp, &path));
        if let Err(source) = written {
            // The write already failed; a leftover temp file is the only harm.
            let _ = fs::remove_file(&temp);
            return Err(ProgressError::Io { path, source });
        }
        Ok(path)
    }

    /// Renames an existing save to the first free `<set id>.txt.bad<N>` next
    /// to it, so [`store`](Self::store) may write a fresh one. Returns the new
    /// path, or `None` when there was no save.
    ///
    /// # Errors
    ///
    /// [`ProgressError::Io`] if the rename fails or all 1000 names are taken.
    pub fn quarantine_unreadable_save(
        &mut self,
        root: &Path,
    ) -> Result<Option<PathBuf>, ProgressError> {
        let path = self.save_path(root);
        if !path.exists() {
            self.unreadable_save = false;
            return Ok(None);
        }
        let dir = root.join(SCORE_DIR);
        for n in 0..MAX_QUARANTINE_FILES {
            let candidate = dir.join(format!("{}.txt.bad{n}", self.set_id));
            if candidate.exists() {
                continue;
            }
            fs::rename(&path, &candidate).map_err(|source| ProgressError::Io { path, source })?;
            self.unreadable_save = false;
            return Ok(Some(candidate));
        }
        Err(ProgressError::Io {
            path,
            source: io::Error::new(io::ErrorKind::AlreadyExists, "no free quarantine name"),
        })
    }
}

fn write_synced(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut file = fs::File::create(path)?;
    file.write_all(bytes)?;
    file.sync_all()
}

fn push_table(out: &mut String, table: &ScoreTable) {
    for e in table.entries() {
        out.push_str(&format!("{} {} {}\n", e.timer, e.coins, e.player));
    }
}

// ---------------------------------------------------------------------------
// Save file reader (set_load_hs, set_load_hs_v1, set_load_hs_v2, get_score).

/// Parses `text` onto a copy of `pristine`. Pure: the caller commits the result.
fn parse_save(
    set_id: &str,
    pristine: &Persistent,
    text: &str,
) -> Result<Persistent, ProgressError> {
    let lines: Vec<&str> = read_lines(text).collect();
    let Some(first) = lines.first() else {
        return Err(ProgressError::Save {
            line: 1,
            reason: "save file is empty",
        });
    };
    let mut staged = pristine.clone();
    match save_version(first) {
        Some(SCORE_VERSION) => parse_v2(&mut staged, &lines, set_id)?,
        Some(found) => return Err(ProgressError::UnsupportedVersion { found }),
        None => parse_v1(&mut staged, &lines)?,
    }
    Ok(staged)
}

/// `sscanf(line, "version %d")`.
fn save_version(line: &str) -> Option<i32> {
    line.strip_prefix("version")
        .and_then(scan_int)
        .map(|(version, _)| version)
}

fn parse_v2(staged: &mut Persistent, lines: &[&str], set_id: &str) -> Result<(), ProgressError> {
    let mut cursor = 1;
    let mut set_scores = None;
    let mut set_match = true;

    while cursor < lines.len() {
        let line = lines[cursor];
        cursor += 1;

        if let Some(id) = line.strip_prefix("set ") {
            if id != set_id {
                return Err(ProgressError::SetMismatch {
                    expected: set_id.to_owned(),
                    found: id.to_owned(),
                });
            }
            let time = read_table(lines, &mut cursor)?;
            let coin = read_table(lines, &mut cursor)?;
            set_scores = Some(SetScores { time, coin });
        } else if let Some((flags, version, file)) = parse_level_line(line) {
            match staged.levels.iter_mut().find(|l| l.file == file) {
                Some(level) => {
                    // The locked flag is always taken from the file; the
                    // completed flag and the tables only on a version match.
                    level.locked = flags & LEVEL_LOCKED != 0;
                    if version == level.version {
                        level.completed = flags & LEVEL_COMPLETED != 0;
                        let time = read_table(lines, &mut cursor)?;
                        let goal = read_table(lines, &mut cursor)?;
                        let coin = read_table(lines, &mut cursor)?;
                        level.scores = LevelScores { time, goal, coin };
                    } else {
                        set_match = false;
                    }
                }
                None => set_match = false,
            }
        }
    }

    if set_match {
        if let Some(scores) = set_scores {
            staged.set = scores;
        }
    }
    Ok(())
}

fn parse_v1(staged: &mut Persistent, lines: &[&str]) -> Result<(), ProgressError> {
    // First line: one state letter per level, `L` locked, `C` completed.
    let states = lines.first().map_or(&[][..], |l| l.as_bytes());
    let count = states.len().min(staged.levels.len());
    for (level, &state) in staged.levels.iter_mut().zip(states) {
        level.locked = state == b'L';
        level.completed = state == b'C';
    }

    let mut cursor = 1;
    staged.set.time = read_table(lines, &mut cursor)?;
    staged.set.coin = read_table(lines, &mut cursor)?;
    for level in staged.levels.iter_mut().take(count) {
        let time = read_table(lines, &mut cursor)?;
        let goal = read_table(lines, &mut cursor)?;
        let coin = read_table(lines, &mut cursor)?;
        level.scores = LevelScores { time, goal, coin };
    }
    Ok(())
}

/// `sscanf(line, "level %d %d %n", ...)`: flags, version and the file name.
fn parse_level_line(line: &str) -> Option<(i32, i32, &str)> {
    let rest = line.strip_prefix("level")?;
    let (flags, rest) = scan_int(rest)?;
    let (version, rest) = scan_int(rest)?;
    Some((flags, version, rest.trim_start_matches(is_c_space)))
}

/// `get_score`: three lines of `timer coins player`.
fn read_table(lines: &[&str], cursor: &mut usize) -> Result<ScoreTable, ProgressError> {
    let mut entries: [ScoreEntry; RANKS] = Default::default();
    for entry in &mut entries {
        let line = lines.get(*cursor).ok_or(ProgressError::Save {
            line: lines.len() + 1,
            reason: "score table is cut short",
        })?;
        *entry = parse_score_line(line).ok_or(ProgressError::Save {
            line: *cursor + 1,
            reason: "malformed score line",
        })?;
        *cursor += 1;
    }
    Ok(ScoreTable { entries })
}

/// `sscanf(line, "%d %d %n")` then the rest of the line as the player.
fn parse_score_line(line: &str) -> Option<ScoreEntry> {
    let (timer, rest) = scan_int(line)?;
    let (coins, rest) = scan_int(rest)?;
    Some(ScoreEntry {
        player: safe_copy(rest.trim_start_matches(is_c_space), PLAYER_CAPACITY),
        timer,
        coins,
    })
}

// ---------------------------------------------------------------------------
// Set id validation.

/// A set id becomes the stem of a file name, so it must be one plain,
/// portable path component.
fn validate_set_id(id: &str) -> Result<(), ProgressError> {
    let invalid = || ProgressError::InvalidSetId { id: id.to_owned() };
    let Some(first) = id.bytes().next() else {
        return Err(invalid());
    };
    let portable = id
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'));
    if id.len() > MAX_ID_LEN
        || !portable
        || !(first.is_ascii_alphanumeric() || first == b'_')
        || id.ends_with('.')
        || id.contains("..")
    {
        return Err(invalid());
    }
    let stem = id.split('.').next().unwrap_or(id);
    if is_reserved_device_name(stem) {
        return Err(invalid());
    }
    Ok(())
}

/// Windows device names that stay reserved with any extension.
fn is_reserved_device_name(stem: &str) -> bool {
    if ["con", "prn", "aux", "nul"]
        .iter()
        .any(|name| stem.eq_ignore_ascii_case(name))
    {
        return true;
    }
    let lower = stem.to_ascii_lowercase();
    lower
        .strip_prefix("com")
        .or_else(|| lower.strip_prefix("lpt"))
        .is_some_and(|digits| matches!(digits.as_bytes(), [b'1'..=b'9']))
}

// ---------------------------------------------------------------------------
// Errors.

/// Why progress could not be built, advanced, loaded or stored.
#[derive(Debug)]
#[non_exhaustive]
pub enum ProgressError {
    /// The set id is not a plain file-name stem: 1 to 64 characters from
    /// `A-Z a-z 0-9 _ - .`, not starting with `.` or `-`, not ending with `.`,
    /// with no `..`, and not a Windows device name.
    InvalidSetId {
        /// The rejected id.
        id: String,
    },
    /// The level list and the set's level files differ in length.
    LevelCountMismatch {
        /// Levels the set names.
        set: usize,
        /// Level attribute records supplied.
        supplied: usize,
    },
    /// More levels than the game reads from a set.
    TooManyLevels {
        /// Levels supplied.
        count: usize,
    },
    /// A level file name contains a line break and cannot be saved.
    InvalidLevelFile {
        /// Index of the level.
        index: usize,
    },
    /// No level has this index.
    NoSuchLevel {
        /// The index asked for.
        index: usize,
    },
    /// The level is still locked.
    LevelLocked {
        /// The index asked for.
        index: usize,
    },
    /// No attempt has been started with [`Progress::play`].
    NoActiveLevel,
    /// The attempt already has a recorded result.
    AlreadyRecorded,
    /// The result values are impossible.
    InvalidResult {
        /// What is wrong.
        reason: &'static str,
    },
    /// [`Progress::play_next`] is not offered now.
    NextUnavailable,
    /// [`Progress::retry`] is not offered now.
    RetryUnavailable,
    /// The challenge run has not passed the last level.
    SetNotComplete,
    /// The finished run is already in the set tables.
    SetAlreadyRecorded,
    /// The last load failed on an existing save file, which must not be
    /// overwritten; see [`Progress::quarantine_unreadable_save`].
    UnreadableSave,
    /// The save file belongs to another set.
    SetMismatch {
        /// This set's id.
        expected: String,
        /// The id in the file.
        found: String,
    },
    /// The save file has a version this port does not read.
    UnsupportedVersion {
        /// The version in the file.
        found: i32,
    },
    /// The save file is malformed.
    Save {
        /// 1-based line number, 0 for a whole-file problem.
        line: usize,
        /// What is wrong.
        reason: &'static str,
    },
    /// A file operation failed.
    Io {
        /// The file or directory involved.
        path: PathBuf,
        /// The operating system's error.
        source: io::Error,
    },
}

impl fmt::Display for ProgressError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidSetId { id } => write!(f, "set id {id:?} is not a plain file name"),
            Self::LevelCountMismatch { set, supplied } => {
                write!(f, "the set lists {set} levels but {supplied} were supplied")
            }
            Self::TooManyLevels { count } => {
                write!(f, "{count} levels exceed the limit of {MAX_LEVELS}")
            }
            Self::InvalidLevelFile { index } => {
                write!(f, "level {index} has a line break in its file name")
            }
            Self::NoSuchLevel { index } => write!(f, "there is no level {index}"),
            Self::LevelLocked { index } => write!(f, "level {index} is locked"),
            Self::NoActiveLevel => f.write_str("no level is being played"),
            Self::AlreadyRecorded => f.write_str("this attempt already has a result"),
            Self::InvalidResult { reason } => write!(f, "invalid result: {reason}"),
            Self::NextUnavailable => f.write_str("there is no next level to play"),
            Self::RetryUnavailable => f.write_str("the level cannot be retried now"),
            Self::SetNotComplete => f.write_str("the set run is not complete"),
            Self::SetAlreadyRecorded => f.write_str("the set run is already recorded"),
            Self::UnreadableSave => {
                f.write_str("the existing save file could not be read and is kept")
            }
            Self::SetMismatch { expected, found } => {
                write!(f, "save file is for set {found:?}, not {expected:?}")
            }
            Self::UnsupportedVersion { found } => {
                write!(f, "save file version {found} is not supported")
            }
            Self::Save { line, reason } => {
                if *line == 0 {
                    write!(f, "save file: {reason}")
                } else {
                    write!(f, "save file line {line}: {reason}")
                }
            }
            Self::Io { path, source } => write!(f, "{}: {source}", path.display()),
        }
    }
}

impl std::error::Error for ProgressError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            _ => None,
        }
    }
}
