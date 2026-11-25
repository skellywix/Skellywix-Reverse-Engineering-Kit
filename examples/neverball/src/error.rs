// SPDX-License-Identifier: GPL-2.0-or-later
//
// Rust port of the SOL loader from Neverball 1.6.0 (share/solid_base.c).
// Original: Copyright (C) 2003 Robert Kooima and the Neverball authors.
// See NOTICE.md and COPYING.

//! Error types for the SOL reader and for loading level and set files.

use core::fmt;
use std::io;
use std::path::PathBuf;

/// Why a SOL file could not be loaded.
///
/// The original C loader reports every failure as `0` and silently reads
/// garbage past the end of a short file. This crate refuses instead and says
/// where and why.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Error {
    /// The first word is not the SOL magic number.
    BadMagic {
        /// The word found in the file.
        found: i32,
    },
    /// The version word is outside the range the 1.6.0 loader accepts (6 to 7).
    UnsupportedVersion {
        /// The version found in the file.
        found: i32,
    },
    /// The file ended in the middle of a field.
    Truncated {
        /// The section being read.
        section: &'static str,
        /// Byte offset of the field that could not be read.
        offset: usize,
        /// Bytes the field needs.
        needed: usize,
        /// Bytes left in the file at `offset`.
        available: usize,
    },
    /// A header count is negative. The C loader would pass it to `calloc` as a
    /// huge unsigned size.
    NegativeCount {
        /// The section the count belongs to.
        section: &'static str,
        /// The count found in the header.
        count: i32,
    },
    /// The header promises more records than the remaining bytes could hold,
    /// even at each record's minimum size.
    CountExceedsData {
        /// The section whose count pushed the total over the limit.
        section: &'static str,
        /// The count found in the header.
        count: usize,
        /// Minimum bytes needed for this and all earlier sections.
        needed: u64,
        /// Bytes available after the header.
        available: u64,
    },
    /// A dictionary entry points outside the string block, or at a string with
    /// no terminating NUL.
    BadString {
        /// Index of the dictionary entry.
        entry: usize,
        /// Which half of the entry: `key` or `value`.
        part: &'static str,
        /// The offset stored in the entry.
        offset: i32,
    },
    /// A record refers to something that does not exist.
    BadReference {
        /// Record kind, for example `lump`.
        record: &'static str,
        /// Index of the offending record.
        index: usize,
        /// Field name, using this crate's names.
        field: &'static str,
        /// The value found.
        value: i32,
        /// Exclusive upper bound that applied to the value.
        limit: usize,
    },
    /// A start/count pair does not fit inside the array it indexes.
    BadRange {
        /// Record kind.
        record: &'static str,
        /// Index of the offending record.
        index: usize,
        /// Field name.
        field: &'static str,
        /// First index of the range.
        start: i32,
        /// Number of entries in the range.
        count: i32,
        /// Length of the array the range must fit in.
        limit: usize,
    },
    /// A float that must be finite is NaN or infinite.
    NonFinite {
        /// Record kind.
        record: &'static str,
        /// Index of the offending record.
        index: usize,
        /// Field name.
        field: &'static str,
    },
    /// The BSP node graph contains a cycle, so a walk would never end.
    NodeCycle {
        /// A node on the cycle.
        node: usize,
    },
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BadMagic { found } => {
                write!(f, "not a SOL file (magic word {found:#010x})")
            }
            Self::UnsupportedVersion { found } => {
                write!(f, "unsupported SOL version {found} (accepted: 6..=7)")
            }
            Self::Truncated {
                section,
                offset,
                needed,
                available,
            } => write!(
                f,
                "truncated in {section} at byte {offset}: need {needed}, have {available}"
            ),
            Self::NegativeCount { section, count } => {
                write!(f, "negative {section} count {count}")
            }
            Self::CountExceedsData {
                section,
                count,
                needed,
                available,
            } => write!(
                f,
                "{section} count {count} needs at least {needed} bytes, only {available} follow the header"
            ),
            Self::BadString {
                entry,
                part,
                offset,
            } => write!(
                f,
                "dictionary entry {entry} {part} offset {offset} is not a terminated string in the string block"
            ),
            Self::BadReference {
                record,
                index,
                field,
                value,
                limit,
            } => write!(
                f,
                "{record} {index}: {field} = {value} is outside 0..{limit}"
            ),
            Self::BadRange {
                record,
                index,
                field,
                start,
                count,
                limit,
            } => write!(
                f,
                "{record} {index}: {field} range start {start}, count {count} does not fit in 0..{limit}"
            ),
            Self::NonFinite {
                record,
                index,
                field,
            } => write!(f, "{record} {index}: {field} is not finite"),
            Self::NodeCycle { node } => {
                write!(f, "BSP node {node} is part of a cycle")
            }
        }
    }
}

impl std::error::Error for Error {}

/// Why a level or set file could not be loaded from disk.
#[derive(Debug)]
#[non_exhaustive]
pub enum LoadError {
    /// The file could not be read.
    Io {
        /// The file that failed.
        path: PathBuf,
        /// The underlying error.
        source: io::Error,
    },
    /// The file was read but is not a loadable SOL file.
    Sol {
        /// The file that failed.
        path: PathBuf,
        /// What is wrong with it.
        source: Error,
    },
    /// A set description file lacks its five header lines.
    Set {
        /// The file that failed.
        path: PathBuf,
        /// What is wrong with it.
        reason: &'static str,
    },
}

impl fmt::Display for LoadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { path, source } => write!(f, "{}: {source}", path.display()),
            Self::Sol { path, source } => write!(f, "{}: {source}", path.display()),
            Self::Set { path, reason } => write!(f, "{}: {reason}", path.display()),
        }
    }
}

impl std::error::Error for LoadError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            Self::Sol { source, .. } => Some(source),
            Self::Set { .. } => None,
        }
    }
}
