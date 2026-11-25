// SPDX-License-Identifier: GPL-2.0-or-later
//
// Rust port of the level-set file reader from Neverball 1.6.0 (ball/set.c,
// set_load and set_init). Original: Copyright (C) 2003 Robert Kooima and the
// Neverball authors. See NOTICE.md and COPYING.

//! Level sets: `sets.txt` and the `set-*.txt` files it points to.
//!
//! A set file has five header lines (name, description, id, screenshot,
//! scores) followed by one level file name per line, at most [`MAX_LEVELS`].
//! Names are relative to the data directory.

use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

use crate::error::LoadError;
use crate::text::{read_lines, scan_ints};

/// The list of set files, in display order (`SET_FILE`).
pub const SET_LIST_FILE: &str = "sets.txt";

/// Most levels the game reads from one set (`MAXLVL`).
pub const MAX_LEVELS: usize = 25;

/// Default set timer, used where the score line is short (`359999`).
pub const DEFAULT_SET_TIMER: i32 = 359_999;

/// A level set (`struct set`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Set {
    /// The set file this was read from, relative to the data directory.
    pub file: String,
    /// Display name.
    pub name: String,
    /// Description; `\` marks line breaks as in the file.
    pub description: String,
    /// Identifier used for score files.
    pub id: String,
    /// Screenshot path.
    pub shot: String,
    /// Set timer thresholds: hard, medium, easy.
    pub time_scores: [i32; 3],
    /// Set coin thresholds: hard, medium, easy.
    pub coin_scores: [i32; 3],
    /// Level files, relative to the data directory, in play order.
    pub levels: Vec<String>,
}

impl Set {
    /// Parses set file text. `file` names the set in the result and in errors.
    ///
    /// # Errors
    ///
    /// [`LoadError::Set`] if the five header lines are not all present.
    pub fn parse(file: &str, text: &str) -> Result<Self, LoadError> {
        let mut lines = read_lines(text);
        let mut header = || lines.next().map(str::to_owned);
        let (Some(name), Some(description), Some(id), Some(shot), Some(scores)) =
            (header(), header(), header(), header(), header())
        else {
            return Err(LoadError::Set {
                path: file.into(),
                reason: "fewer than five header lines",
            });
        };

        let mut time_scores = [DEFAULT_SET_TIMER; 3];
        let mut coin_scores = [0; 3];
        let (values, parsed) = scan_ints::<6>(&scores);
        for (k, &v) in values[..parsed].iter().enumerate() {
            if k < 3 {
                time_scores[k] = v;
            } else {
                coin_scores[k - 3] = v;
            }
        }

        let levels = lines.take(MAX_LEVELS).map(str::to_owned).collect();

        Ok(Self {
            file: file.to_owned(),
            name,
            description,
            id,
            shot,
            time_scores,
            coin_scores,
            levels,
        })
    }

    /// Reads the set file `file` from `data_dir`.
    ///
    /// # Errors
    ///
    /// [`LoadError::Io`] if the file cannot be read, [`LoadError::Set`] if its
    /// header is incomplete.
    pub fn load(data_dir: &Path, file: &str) -> Result<Self, LoadError> {
        let path = data_dir.join(file);
        let bytes = fs::read(&path).map_err(|source| LoadError::Io {
            path: path.clone(),
            source,
        })?;
        let text = String::from_utf8_lossy(&bytes);
        Self::parse(file, &text).map_err(|e| match e {
            LoadError::Set { reason, .. } => LoadError::Set { path, reason },
            other => other,
        })
    }
}

/// Loads every set the way `set_init` does: first those listed in
/// `sets.txt`, in that order, then any other `set-*.txt` in the data
/// directory, sorted by name.
///
/// Unlike the game, which silently skips a set it cannot load, this fails on
/// the first set that cannot be read, so a damaged data directory is noticed.
/// It also loads `set-misc.txt`, which the game hides unless cheats are on.
///
/// # Errors
///
/// [`LoadError::Io`] if `sets.txt`, the directory, or a set file cannot be
/// read; [`LoadError::Set`] if a set file's header is incomplete.
pub fn load_all(data_dir: &Path) -> Result<Vec<Set>, LoadError> {
    let list_path = data_dir.join(SET_LIST_FILE);
    let list_bytes = fs::read(&list_path).map_err(|source| LoadError::Io {
        path: list_path.clone(),
        source,
    })?;

    let mut sets = Vec::new();
    for name in read_lines(&String::from_utf8_lossy(&list_bytes)) {
        sets.push(Set::load(data_dir, name)?);
    }

    let io_err = |source| LoadError::Io {
        path: data_dir.to_path_buf(),
        source,
    };
    let mut unseen = Vec::new();
    for entry in fs::read_dir(data_dir).map_err(io_err)? {
        let entry = entry.map_err(io_err)?;
        let file_name = entry.file_name();
        let Some(name) = file_name.to_str() else {
            continue;
        };
        if name.starts_with("set-") && name.ends_with(".txt") && !sets.iter().any(|s| s.file == name)
        {
            unseen.push(name.to_owned());
        }
    }
    unseen.sort();
    for name in unseen {
        sets.push(Set::load(data_dir, &name)?);
    }

    Ok(sets)
}

/// Every distinct level file named by `sets`, sorted. A level can appear in
/// more than one set; it is one level.
#[must_use]
pub fn distinct_levels(sets: &[Set]) -> Vec<&str> {
    sets.iter()
        .flat_map(|s| s.levels.iter().map(String::as_str))
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}
