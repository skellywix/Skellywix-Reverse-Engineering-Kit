// SPDX-License-Identifier: GPL-2.0-or-later
//
// Test support for the Rust port of the Neverball 1.6.0 level loader.
// See NOTICE.md and COPYING.

//! An independent SOL *writer* for tests.
//!
//! It is written from the record layouts in `share/solid_base.c` of the 1.6.0
//! source, not from the loader under test, so a misreading of the format in
//! the loader does not cancel out here. Every record type is a plain array of
//! words, and `build` lays them out in file order.

#![allow(dead_code)] // each integration test crate uses a different subset of this shared module

/// `0xAF 'S' 'O' 'L'` read little-endian.
pub const MAGIC: i32 = 0x4C4F_53AF;

/// Material flag bits (version 7 layout).
pub const M_SHADOWED: i32 = 1 << 6;
pub const M_ALPHA_TEST: i32 = 1 << 9;

/// Header count names in file order. `offset` is absent from version 6 files.
pub const COUNT_NAMES: [&str; 21] = [
    "strings", "dict", "mtrl", "vert", "edge", "side", "texc", "offs", "geom", "lump", "node",
    "path", "body", "item", "goal", "jump", "swch", "bill", "ball", "view", "pool",
];

#[derive(Clone, Debug)]
pub struct Mtrl {
    pub d: [f32; 4],
    pub a: [f32; 4],
    pub s: [f32; 4],
    pub e: [f32; 4],
    pub h: f32,
    pub fl: i32,
    pub name: String,
    /// Written only to version 7 files whose `fl` has `M_ALPHA_TEST`.
    pub alpha: (i32, f32),
}

impl Mtrl {
    pub fn new(name: &str, fl: i32) -> Self {
        Self {
            d: [0.8, 0.7, 0.6, 1.0],
            a: [0.2, 0.2, 0.2, 1.0],
            s: [0.5, 0.5, 0.5, 1.0],
            e: [0.0, 0.0, 0.0, 1.0],
            h: 32.0,
            fl,
            name: name.to_owned(),
            alpha: (0, 0.0),
        }
    }
}

#[derive(Clone, Debug)]
pub struct PathRec {
    pub p: [f32; 3],
    pub t: f32,
    pub pi: i32,
    pub f: i32,
    pub s: i32,
    /// Version 7 only. Bit 0 (`P_ORIENTED`) adds the quaternion.
    pub fl: i32,
    pub e: [f32; 4],
}

#[derive(Clone, Debug)]
pub struct Swch {
    pub p: [f32; 3],
    pub r: f32,
    pub pi: i32,
    pub t: f32,
    pub f: i32,
    pub i: i32,
}

/// A whole SOL file as lists of records.
#[derive(Clone, Debug)]
pub struct Builder {
    pub version: i32,
    pub attrs: Vec<(String, String)>,
    /// Replaces the string block and dictionary entries when set.
    pub dict_override: Option<(Vec<u8>, Vec<[i32; 2]>)>,
    pub mtrls: Vec<Mtrl>,
    pub verts: Vec<[f32; 3]>,
    pub edges: Vec<[i32; 2]>,
    pub sides: Vec<([f32; 3], f32)>,
    pub texcs: Vec<[f32; 2]>,
    /// Version 7 only.
    pub offs: Vec<[i32; 3]>,
    /// Raw words: `[mi, oi, oj, ok]` for version 7, `[mi, 9 offset words]`
    /// for version 6.
    pub geoms: Vec<Vec<i32>>,
    /// `[fl, v0, vc, e0, ec, g0, gc, s0, sc]`.
    pub lumps: Vec<[i32; 9]>,
    /// `[si, ni, nj, l0, lc]`.
    pub nodes: Vec<[i32; 5]>,
    pub paths: Vec<PathRec>,
    /// `[pi, pj, ni, l0, lc, g0, gc]`; `pj` is dropped from version 6 files.
    pub bodies: Vec<[i32; 7]>,
    /// `(position, type, value)`.
    pub items: Vec<([f32; 3], i32, i32)>,
    pub goals: Vec<([f32; 3], f32)>,
    /// `(position, target, radius)`.
    pub jumps: Vec<([f32; 3], [f32; 3], f32)>,
    pub swchs: Vec<Swch>,
    /// 22 words: `fl, mi, t, d, w[3], h[3], rx[3], ry[3], rz[3], p[3]` with
    /// the integers as words and the floats as raw bit patterns.
    pub bills: Vec<[u32; 22]>,
    pub balls: Vec<([f32; 3], f32)>,
    pub views: Vec<([f32; 3], [f32; 3])>,
    pub pool: Vec<i32>,
}

impl Builder {
    pub fn empty(version: i32) -> Self {
        Self {
            version,
            attrs: Vec::new(),
            dict_override: None,
            mtrls: Vec::new(),
            verts: Vec::new(),
            edges: Vec::new(),
            sides: Vec::new(),
            texcs: Vec::new(),
            offs: Vec::new(),
            geoms: Vec::new(),
            lumps: Vec::new(),
            nodes: Vec::new(),
            paths: Vec::new(),
            bodies: Vec::new(),
            items: Vec::new(),
            goals: Vec::new(),
            jumps: Vec::new(),
            swchs: Vec::new(),
            bills: Vec::new(),
            balls: Vec::new(),
            views: Vec::new(),
            pool: Vec::new(),
        }
    }

    pub fn attr(mut self, key: &str, value: &str) -> Self {
        self.attrs.push((key.to_owned(), value.to_owned()));
        self
    }

    pub fn build(&self) -> Vec<u8> {
        let v7 = self.version >= 7;

        let (strings, dict) = match &self.dict_override {
            Some((s, d)) => (s.clone(), d.clone()),
            None => {
                let mut block = Vec::new();
                let mut entries = Vec::new();
                for (k, v) in &self.attrs {
                    let ai = block.len() as i32;
                    block.extend_from_slice(k.as_bytes());
                    block.push(0);
                    let aj = block.len() as i32;
                    block.extend_from_slice(v.as_bytes());
                    block.push(0);
                    entries.push([ai, aj]);
                }
                (block, entries)
            }
        };

        let mut w = Vec::new();
        i(&mut w, MAGIC);
        i(&mut w, self.version);
        for &n in &[
            strings.len(),
            dict.len(),
            self.mtrls.len(),
            self.verts.len(),
            self.edges.len(),
            self.sides.len(),
            self.texcs.len(),
        ] {
            i(&mut w, n as i32);
        }
        if v7 {
            i(&mut w, self.offs.len() as i32);
        }
        for &n in &[
            self.geoms.len(),
            self.lumps.len(),
            self.nodes.len(),
            self.paths.len(),
            self.bodies.len(),
            self.items.len(),
            self.goals.len(),
            self.jumps.len(),
            self.swchs.len(),
            self.bills.len(),
            self.balls.len(),
            self.views.len(),
            self.pool.len(),
        ] {
            i(&mut w, n as i32);
        }

        w.extend_from_slice(&strings);
        for d in &dict {
            is(&mut w, d);
        }
        for m in &self.mtrls {
            fs(&mut w, &m.d);
            fs(&mut w, &m.a);
            fs(&mut w, &m.s);
            fs(&mut w, &m.e);
            f(&mut w, m.h);
            i(&mut w, m.fl);
            let mut name = [0_u8; 64];
            name[..m.name.len()].copy_from_slice(m.name.as_bytes());
            w.extend_from_slice(&name);
            if v7 && m.fl & M_ALPHA_TEST != 0 {
                i(&mut w, m.alpha.0);
                f(&mut w, m.alpha.1);
            }
        }
        for v in &self.verts {
            fs(&mut w, v);
        }
        for e in &self.edges {
            is(&mut w, e);
        }
        for (n, d) in &self.sides {
            fs(&mut w, n);
            f(&mut w, *d);
        }
        for t in &self.texcs {
            fs(&mut w, t);
        }
        if v7 {
            for o in &self.offs {
                is(&mut w, o);
            }
        }
        for g in &self.geoms {
            is(&mut w, g);
        }
        for l in &self.lumps {
            is(&mut w, l);
        }
        for n in &self.nodes {
            is(&mut w, n);
        }
        for p in &self.paths {
            fs(&mut w, &p.p);
            f(&mut w, p.t);
            i(&mut w, p.pi);
            i(&mut w, p.f);
            i(&mut w, p.s);
            if v7 {
                i(&mut w, p.fl);
                if p.fl & 1 != 0 {
                    fs(&mut w, &p.e);
                }
            }
        }
        for b in &self.bodies {
            if v7 {
                is(&mut w, b);
            } else {
                i(&mut w, b[0]);
                is(&mut w, &b[2..]);
            }
        }
        for (p, t, n) in &self.items {
            fs(&mut w, p);
            i(&mut w, *t);
            i(&mut w, *n);
        }
        for (p, r) in &self.goals {
            fs(&mut w, p);
            f(&mut w, *r);
        }
        for (p, q, r) in &self.jumps {
            fs(&mut w, p);
            fs(&mut w, q);
            f(&mut w, *r);
        }
        for x in &self.swchs {
            fs(&mut w, &x.p);
            f(&mut w, x.r);
            i(&mut w, x.pi);
            f(&mut w, x.t);
            f(&mut w, 0.0); // dummy float
            i(&mut w, x.f);
            i(&mut w, 0); // dummy int
            i(&mut w, x.i);
        }
        for b in &self.bills {
            for word in b {
                w.extend_from_slice(&word.to_le_bytes());
            }
        }
        for (p, r) in &self.balls {
            fs(&mut w, p);
            f(&mut w, *r);
        }
        for (p, q) in &self.views {
            fs(&mut w, p);
            fs(&mut w, q);
        }
        is(&mut w, &self.pool);
        w
    }
}

fn i(w: &mut Vec<u8>, v: i32) {
    w.extend_from_slice(&v.to_le_bytes());
}

fn is(w: &mut Vec<u8>, v: &[i32]) {
    for &x in v {
        i(w, x);
    }
}

fn f(w: &mut Vec<u8>, v: f32) {
    w.extend_from_slice(&v.to_le_bytes());
}

fn fs(w: &mut Vec<u8>, v: &[f32]) {
    for &x in v {
        f(w, x);
    }
}

/// Byte offset of the header count `name` (see [`COUNT_NAMES`]).
pub fn count_offset(name: &str, version: i32) -> usize {
    let mut slot = 0;
    for &n in &COUNT_NAMES {
        if n == name {
            return 8 + 4 * slot;
        }
        if n != "offs" || version >= 7 {
            slot += 1;
        }
    }
    panic!("no header count named {name}");
}

/// Overwrites a header count in built bytes.
pub fn patch_count(bytes: &mut [u8], name: &str, version: i32, value: i32) {
    let at = count_offset(name, version);
    bytes[at..at + 4].copy_from_slice(&value.to_le_bytes());
}

/// A bill record from readable parts.
pub fn bill(fl: i32, mi: i32, t: f32, d: f32, vecs: [[f32; 3]; 6]) -> [u32; 22] {
    let mut out = [0_u32; 22];
    out[0] = fl as u32;
    out[1] = mi as u32;
    out[2] = t.to_bits();
    out[3] = d.to_bits();
    for (k, v) in vecs.iter().enumerate() {
        for (j, x) in v.iter().enumerate() {
            out[4 + 3 * k + j] = x.to_bits();
        }
    }
    out
}

/// A small version 7 level that touches every record type.
///
/// Pool layout (`ic` = 9): `[0 1 2 | 0 1 | 0 | 0 1 | 1]`, i.e. lump 0's
/// vertices at 0..3, edges at 3..5, geom at 5, sides at 6..8; lump 1 reuses
/// pool entries 1..3 as its vertices and 6 as its only side; body 1's
/// triangle is at 8.
pub fn sample_v7() -> Builder {
    let mut b = Builder::empty(7);
    b.attrs = vec![
        ("message".into(), "Hello".into()),
        ("time".into(), "30".into()),
        ("goal".into(), "10".into()),
    ];
    let mut alpha = Mtrl::new("mtrl/glass", M_SHADOWED | M_ALPHA_TEST);
    alpha.alpha = (516, 0.5);
    b.mtrls = vec![alpha, Mtrl::new("mtrl/plain", 0)];
    b.verts = vec![
        [0.0, -5.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ];
    b.edges = vec![[0, 1], [1, 2]];
    b.sides = vec![([0.0, 1.0, 0.0], 0.0), ([1.0, 0.0, 0.0], 1.0)];
    b.texcs = vec![[0.0, 0.0], [1.0, 1.0]];
    b.offs = vec![[0, 0, 0], [1, 0, 1], [0, 1, 2]];
    b.geoms = vec![vec![0, 0, 1, 2], vec![1, 2, 1, 0]];
    b.lumps = vec![
        [0, 0, 3, 3, 2, 5, 1, 6, 2],
        [1, 1, 2, 0, 0, 0, 0, 6, 1],
    ];
    b.nodes = vec![
        [0, 1, 2, 0, 0],
        [-1, -1, -1, 0, 1],
        [-1, -1, -1, 1, 1],
    ];
    b.paths = vec![
        PathRec {
            p: [0.0, 0.0, 0.0],
            t: 2.6,
            pi: 1,
            f: 1,
            s: 1,
            fl: 0,
            e: [0.0; 4],
        },
        PathRec {
            p: [5.0, 0.0, 0.0],
            t: -1.0006,
            pi: 0,
            f: 0,
            s: 0,
            fl: 1,
            e: [0.0, 0.0, 1.0, 0.0],
        },
    ];
    b.bodies = vec![
        [-1, -1, 0, 0, 2, 5, 1],
        [0, -1, -1, 0, 0, 8, 1],
        [0, 1, -1, 0, 0, 0, 0],
    ];
    b.items = vec![
        ([1.0, 1.0, 1.0], 1, 1),
        ([2.0, 1.0, 1.0], 1, 5),
        ([3.0, 1.0, 1.0], 2, 0),
        ([4.0, 1.0, 1.0], 3, 0),
        ([5.0, 1.0, 1.0], 9, 7),
    ];
    b.goals = vec![([0.0, 0.0, 2.0], 0.75)];
    b.jumps = vec![([1.0, 0.0, 1.0], [9.0, 0.0, 9.0], 0.5)];
    b.swchs = vec![Swch {
        p: [1.0, 2.0, 3.0],
        r: 0.5,
        pi: 1,
        t: 0.0004,
        f: 1,
        i: 1,
    }];
    b.bills = vec![bill(
        2,
        1,
        0.5,
        10.0,
        [
            [1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0],
            [7.0, 8.0, 9.0],
            [10.0, 11.0, 12.0],
            [13.0, 14.0, 15.0],
            [16.0, 17.0, 18.0],
        ],
    )];
    b.balls = vec![([0.0, 1.0, 0.0], 0.25)];
    b.views = vec![([0.0, 2.0, 5.0], [0.0, 1.0, 0.0])];
    b.pool = vec![0, 1, 2, 0, 1, 0, 0, 1, 1];
    b
}

/// A small version 6 (Neverball 1.5) level.
///
/// Triangle 0's three offset triples are `A B A`; triangle 1's are `A C B`.
pub fn sample_v6() -> Builder {
    let mut b = Builder::empty(6);
    b.mtrls = vec![
        Mtrl::new("mtrl/old-a", 1 | 4),
        Mtrl::new("mtrl/invisible", 0),
        Mtrl::new("mtrl/clamped", 32 | 128),
    ];
    b.verts = vec![[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]];
    b.sides = vec![([0.0, 1.0, 0.0], 0.0), ([0.0, 0.0, 1.0], 2.0)];
    b.texcs = vec![[0.0, 0.0], [0.5, 0.5], [1.0, 1.0]];
    // A = (ti 0, si 0, vi 0), B = (1, 0, 1), C = (2, 1, 2).
    b.geoms = vec![
        vec![0, 0, 0, 0, 1, 0, 1, 0, 0, 0],
        vec![1, 0, 0, 0, 2, 1, 2, 1, 0, 1],
    ];
    b.lumps = vec![[0, 0, 3, 0, 0, 0, 2, 0, 2]];
    b.nodes = vec![[-1, -1, -1, 0, 1]];
    b.paths = vec![PathRec {
        p: [1.0, 2.0, 3.0],
        t: 1.0,
        pi: 0,
        f: 1,
        s: 0,
        // Ignored by version 6, which has no flags field.
        fl: 1,
        e: [9.0; 4],
    }];
    b.bodies = vec![[-1, 99, 0, 0, 1, 0, 2], [0, 99, -1, 0, 0, 0, 0]];
    b.balls = vec![([0.0, 1.0, 0.0], 0.25)];
    b.pool = vec![0, 1, 2, 0, 1];
    b
}

/// A scratch directory removed on drop. The name includes the process id and
/// a per-test tag so tests running in parallel never share one.
pub struct TempDir(pub std::path::PathBuf);

impl TempDir {
    pub fn new(tag: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("neverball-test-{}-{tag}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap_or_else(|e| panic!("cannot create {dir:?}: {e}"));
        Self(dir)
    }

    /// Writes `bytes` to `name` (a relative path, parents created).
    pub fn write(&self, name: &str, bytes: &[u8]) -> std::path::PathBuf {
        let path = self.0.join(name);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).unwrap_or_else(|e| panic!("mkdir {parent:?}: {e}"));
        }
        std::fs::write(&path, bytes).unwrap_or_else(|e| panic!("write {path:?}: {e}"));
        path
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
