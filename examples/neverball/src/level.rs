// SPDX-License-Identifier: GPL-2.0-or-later
//
// Rust port of the level attribute scanner from Neverball 1.6.0
// (ball/level.c, scan_level_attribs). Original: Copyright (C) 2003 Robert
// Kooima and the Neverball authors. See NOTICE.md and COPYING.

//! A level: the SOL file plus the gameplay attributes stored in its
//! dictionary.

use core::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use crate::error::LoadError;
use crate::sol::{ItemKind, Sol};
use crate::text::{atoi, safe_copy, scan_int, scan_ints};

/// Index of the hardest rank in the three-entry score arrays.
pub const RANK_HARD: usize = 0;
/// Index of the medium rank.
pub const RANK_MEDM: usize = 1;
/// Index of the easiest rank.
pub const RANK_EASY: usize = 2;

/// Default timer value of an unset high score (`score_init_hs`).
pub const DEFAULT_TIMER: i32 = 59999;

// Buffer sizes of `struct level` in ball/level.h; SAFECPY keeps one byte less.
const MAXSTR: usize = 256;
const PATHMAX: usize = 64;
const VERSION_STR: usize = 32;

/// One score table: a timer and a coin count per rank, in the order
/// [`RANK_HARD`], [`RANK_MEDM`], [`RANK_EASY`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HighScores {
    /// Timer thresholds, in hundredths of a second as the game stores them.
    pub timer: [i32; 3],
    /// Coin thresholds.
    pub coins: [i32; 3],
}

impl HighScores {
    const DEFAULT: Self = Self {
        timer: [DEFAULT_TIMER; 3],
        coins: [0; 3],
    };
}

/// The three score tables of a level (`scores[SCORE_TIME]`, `SCORE_GOAL`,
/// `SCORE_COIN`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Scores {
    /// Best-time table (`time_hs`).
    pub time: HighScores,
    /// Most-coins-to-goal table (`goal_hs`).
    pub goal: HighScores,
    /// Most-coins table (`coin_hs`).
    pub coin: HighScores,
}

/// Gameplay attributes read from a level's dictionary.
///
/// Fields keep the C game's names and defaults: strings are truncated to the
/// C buffers, `goal` and `time` default to zero, and scores default to
/// [`DEFAULT_TIMER`] and zero coins.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LevelInfo {
    /// Message shown when the level starts (`message`).
    pub message: String,
    /// Music file (`song`).
    pub song: String,
    /// Screenshot file (`shot`).
    pub shot: String,
    /// Author (`author`).
    pub author: String,
    /// Version text (`version`).
    pub version_str: String,
    /// Leading integer of the version text; zero if it has none.
    pub version_num: i32,
    /// Coins needed to open the goal (`goal`).
    pub goal: i32,
    /// Time limit in hundredths of a second (`time`); zero means no limit.
    pub time: i32,
    /// Whether this is a bonus level (`bonus`).
    pub is_bonus: bool,
    /// Score tables.
    pub scores: Scores,
}

impl LevelInfo {
    /// Reads the dictionary attributes, applying `scan_level_attribs`'s rules,
    /// including the defaults derived from `goal` and `time`.
    #[must_use]
    pub fn from_sol(sol: &Sol) -> Self {
        let mut info = Self {
            message: String::new(),
            song: String::new(),
            shot: String::new(),
            author: String::new(),
            version_str: String::new(),
            version_num: 0,
            goal: 0,
            time: 0,
            is_bonus: false,
            scores: Scores {
                time: HighScores::DEFAULT,
                goal: HighScores::DEFAULT,
                coin: HighScores::DEFAULT,
            },
        };

        let mut have_goal = false;
        let mut have_time = false;
        let mut need_time_easy = false;
        let mut need_goal_easy = false;
        let mut need_coin_easy = false;

        for attr in &sol.attributes {
            let v = attr.value.as_str();
            match attr.key.as_str() {
                "message" => info.message = safe_copy(v, MAXSTR),
                "song" => info.song = safe_copy(v, PATHMAX),
                "shot" => info.shot = safe_copy(v, PATHMAX),
                "goal" => {
                    info.goal = atoi(v);
                    have_goal = true;
                }
                "time" => {
                    info.time = atoi(v);
                    have_time = true;
                }
                "time_hs" => {
                    need_time_easy |= scan_rank_triple(v, &mut info.scores.time.timer);
                }
                "goal_hs" => {
                    need_goal_easy |= scan_rank_triple(v, &mut info.scores.goal.timer);
                }
                "coin_hs" => {
                    need_coin_easy |= scan_rank_triple(v, &mut info.scores.coin.coins);
                }
                "version" => {
                    info.version_str = safe_copy(v, VERSION_STR);
                    // sscanf leaves the old number alone when no integer parses.
                    if let Some((n, _)) = scan_int(v) {
                        info.version_num = n;
                    }
                }
                "author" => info.author = safe_copy(v, MAXSTR),
                "bonus" => info.is_bonus = atoi(v) != 0,
                _ => {}
            }
        }

        if have_goal {
            if need_coin_easy {
                info.scores.coin.coins[RANK_EASY] = info.goal;
            }
            info.scores.goal.coins = [info.goal; 3];
        }

        if have_time {
            if need_time_easy {
                info.scores.time.timer[RANK_EASY] = info.time;
            }
            if need_goal_easy {
                info.scores.goal.timer[RANK_EASY] = info.time;
            }
            info.scores.coin.timer = [info.time; 3];
        }

        info
    }
}

/// `sscanf(v, "%d %d %d", ...)` into `dest`; returns true when exactly two
/// values parsed, which asks the caller to fill in the easy rank.
fn scan_rank_triple(v: &str, dest: &mut [i32; 3]) -> bool {
    let (values, parsed) = scan_ints::<3>(v);
    dest[..parsed].copy_from_slice(&values[..parsed]);
    parsed == 2
}

/// Counts of what a level contains, for reports and sanity checks.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Summary {
    /// Materials.
    pub materials: usize,
    /// Vertices.
    pub vertices: usize,
    /// Triangles.
    pub triangles: usize,
    /// Collision lumps, detail lumps included.
    pub lumps: usize,
    /// Detail lumps.
    pub detail_lumps: usize,
    /// BSP nodes.
    pub nodes: usize,
    /// Waypoints.
    pub paths: usize,
    /// Bodies.
    pub bodies: usize,
    /// Bodies that follow a path.
    pub moving_bodies: usize,
    /// Coin items.
    pub coins: usize,
    /// Sum of the values of all coin items.
    pub coin_value: i64,
    /// Grow items.
    pub grows: usize,
    /// Shrink items.
    pub shrinks: usize,
    /// Goal zones.
    pub goals: usize,
    /// Teleporters.
    pub jumps: usize,
    /// Switches.
    pub switches: usize,
    /// Billboards.
    pub billboards: usize,
    /// Ball starts.
    pub balls: usize,
    /// Viewpoints.
    pub views: usize,
}

/// Something about a loaded level that looks wrong but that 1.6.0 itself
/// loads without complaint. Reported, never fatal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum Concern {
    /// No vertices: the game treats the ball as fallen at once.
    NoVertices,
    /// No triangles to draw.
    NoTriangles,
    /// No goal zone: the level cannot be completed.
    NoGoalZone,
    /// The goal needs more coins than the level's coins are worth.
    GoalExceedsCoins {
        /// Coins required (`goal`).
        required: i32,
        /// Total value of all coin items.
        available: i64,
    },
}

/// A level loaded from disk: file contents and attributes.
#[derive(Debug, Clone, PartialEq)]
pub struct Level {
    /// Where the level was read from.
    pub path: PathBuf,
    /// Gameplay attributes from the dictionary.
    pub info: LevelInfo,
    /// The parsed geometry and gameplay entities.
    pub sol: Sol,
}

impl Level {
    /// Reads and parses the SOL file at `path`.
    ///
    /// # Errors
    ///
    /// [`LoadError::Io`] if the file cannot be read, [`LoadError::Sol`] if its
    /// contents are not a loadable SOL file.
    pub fn load(path: impl AsRef<Path>) -> Result<Self, LoadError> {
        let path = path.as_ref();
        let bytes = fs::read(path).map_err(|source| LoadError::Io {
            path: path.to_path_buf(),
            source,
        })?;
        Self::from_bytes(path, &bytes)
    }

    /// Parses SOL bytes already in memory; `path` only labels the result and
    /// any error.
    ///
    /// # Errors
    ///
    /// [`LoadError::Sol`] if the bytes are not a loadable SOL file.
    pub fn from_bytes(path: impl AsRef<Path>, bytes: &[u8]) -> Result<Self, LoadError> {
        let path = path.as_ref();
        let sol = Sol::from_bytes(bytes).map_err(|source| LoadError::Sol {
            path: path.to_path_buf(),
            source,
        })?;
        Ok(Self {
            path: path.to_path_buf(),
            info: LevelInfo::from_sol(&sol),
            sol,
        })
    }

    /// Counts of the level's contents.
    #[must_use]
    pub fn summary(&self) -> Summary {
        let sol = &self.sol;
        let count_kind = |kind: ItemKind| sol.items.iter().filter(|i| i.kind == kind).count();
        Summary {
            materials: sol.materials.len(),
            vertices: sol.vertices.len(),
            triangles: sol.geoms.len(),
            lumps: sol.lumps.len(),
            detail_lumps: sol.lumps.iter().filter(|l| l.is_detail()).count(),
            nodes: sol.nodes.len(),
            paths: sol.paths.len(),
            bodies: sol.bodies.len(),
            moving_bodies: sol.bodies.iter().filter(|b| b.path.is_some()).count(),
            coins: count_kind(ItemKind::Coin),
            coin_value: sol.coin_value(),
            grows: count_kind(ItemKind::Grow),
            shrinks: count_kind(ItemKind::Shrink),
            goals: sol.goals.len(),
            jumps: sol.jumps.len(),
            switches: sol.switches.len(),
            billboards: sol.billboards.len(),
            balls: sol.balls.len(),
            views: sol.views.len(),
        }
    }

    /// Things worth a second look. An empty list means none found.
    #[must_use]
    pub fn concerns(&self) -> Vec<Concern> {
        let mut out = Vec::new();
        if self.sol.vertices.is_empty() {
            out.push(Concern::NoVertices);
        }
        if self.sol.geoms.is_empty() {
            out.push(Concern::NoTriangles);
        }
        if self.sol.goals.is_empty() {
            out.push(Concern::NoGoalZone);
        }
        let available = self.sol.coin_value();
        if i64::from(self.info.goal) > available {
            out.push(Concern::GoalExceedsCoins {
                required: self.info.goal,
                available,
            });
        }
        out
    }
}

impl fmt::Display for Concern {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoVertices => f.write_str("no vertices (the ball counts as fallen at once)"),
            Self::NoTriangles => f.write_str("no triangles to draw"),
            Self::NoGoalZone => f.write_str("no goal zone"),
            Self::GoalExceedsCoins {
                required,
                available,
            } => write!(
                f,
                "goal needs {required} coins but the coins are worth only {available}"
            ),
        }
    }
}
