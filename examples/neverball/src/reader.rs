// SPDX-License-Identifier: GPL-2.0-or-later
//
// Rust port of the SOL loader from Neverball 1.6.0 (share/binary.c).
// Original: Copyright (C) 2003 Robert Kooima and the Neverball authors.
// See NOTICE.md and COPYING.

//! Bounds-checked little-endian cursor.
//!
//! Neverball's `get_index` and `get_float` (`share/binary.c`) read a 32-bit
//! word in little-endian order and return garbage once the file ends. Every
//! read here either yields a value or reports exactly where the data ran out.

use crate::error::Error;

/// A cursor over the bytes of a SOL file.
#[derive(Debug)]
pub(crate) struct Reader<'a> {
    data: &'a [u8],
    pos: usize,
    section: &'static str,
}

impl<'a> Reader<'a> {
    /// Starts at the first byte of `data`, in the `header` section.
    pub(crate) fn new(data: &'a [u8]) -> Self {
        Self {
            data,
            pos: 0,
            section: "header",
        }
    }

    /// Names the section that following errors are reported against.
    pub(crate) fn set_section(&mut self, section: &'static str) {
        self.section = section;
    }

    /// Bytes not yet consumed.
    pub(crate) fn remaining(&self) -> usize {
        self.data.len() - self.pos
    }

    /// Takes the next `n` bytes.
    pub(crate) fn bytes(&mut self, n: usize) -> Result<&'a [u8], Error> {
        let available = self.remaining();
        if n > available {
            return Err(Error::Truncated {
                section: self.section,
                offset: self.pos,
                needed: n,
                available,
            });
        }
        let out = &self.data[self.pos..self.pos + n];
        self.pos += n;
        Ok(out)
    }

    fn word(&mut self) -> Result<[u8; 4], Error> {
        let b = self.bytes(4)?;
        Ok([b[0], b[1], b[2], b[3]])
    }

    /// A signed 32-bit integer (`get_index`).
    pub(crate) fn i32(&mut self) -> Result<i32, Error> {
        Ok(i32::from_le_bytes(self.word()?))
    }

    /// A 32-bit float (`get_float`).
    pub(crate) fn f32(&mut self) -> Result<f32, Error> {
        Ok(f32::from_le_bytes(self.word()?))
    }

    /// `N` consecutive floats (`get_array`).
    pub(crate) fn floats<const N: usize>(&mut self) -> Result<[f32; N], Error> {
        let mut out = [0.0; N];
        for slot in &mut out {
            *slot = self.f32()?;
        }
        Ok(out)
    }
}
