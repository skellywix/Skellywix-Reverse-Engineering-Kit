// SPDX-License-Identifier: GPL-2.0-or-later
//
// Rust port of parts of Neverball 1.6.0 (https://neverball.org), release tag
// neverball-1.6.0. Original: Copyright (C) 2003 Robert Kooima and the
// Neverball authors. This program is free software; you can redistribute it
// and/or modify it under the terms of the GNU General Public License as
// published by the Free Software Foundation; either version 2 of the License,
// or (at your option) any later version. It is distributed WITHOUT ANY
// WARRANTY. See COPYING and NOTICE.md.

//! Neverball 1.6.0 level data, fixed-step simulation, and player progress.
//!
//! [`game`] advances one attempt using [`motion`] and [`contact`]; [`progress`]
//! handles scoring and unlocks. Rendering and input belong to the desktop client.
//!
//! * [`sol`]: the binary SOL level format (`share/solid_base.c`), versions 6
//!   and 7: materials, vertices, edges, side planes, texture coordinates,
//!   triangles, collision lumps, the BSP tree, paths, bodies, items, goals,
//!   teleporters, switches, billboards, ball starts, viewpoints and the
//!   attribute dictionary.
//! * [`level`]: a level file plus the gameplay attributes (`time`, `goal`,
//!   score tables, ...) read from its dictionary (`ball/level.c`).
//! * [`set`]: level sets, `sets.txt` and `set-*.txt` (`ball/set.c`).
//! * [`contact`]: pickup, goal, and teleporter contact at a given ball position
//!   (`share/solid_all.c`, `ball/game_server.c`).
//!
//! The game's data files are not part of this crate. Load them from an
//! extracted copy of the original game's data directory.
//!
//! ```no_run
//! use neverball::level::Level;
//!
//! # fn main() -> Result<(), neverball::error::LoadError> {
//! let level = Level::load("data/map-easy/easy.sol")?;
//! println!("{} triangles, goal {} coins", level.sol.geoms.len(), level.info.goal);
//! # Ok(())
//! # }
//! ```

#![forbid(unsafe_code)]
#![deny(missing_docs)]

pub mod contact;
pub mod error;
pub mod game;
pub mod level;
pub mod motion;
pub mod progress;
mod reader;
pub mod set;
pub mod sol;
mod text;

pub use error::{Error, LoadError};
pub use level::{Level, LevelInfo};
pub use sol::Sol;
