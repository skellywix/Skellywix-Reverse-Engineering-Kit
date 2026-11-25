// SPDX-License-Identifier: GPL-2.0-or-later
//
// Rust port of the SOL loader from Neverball 1.6.0 (share/solid_base.c,
// share/solid_base.h). Original: Copyright (C) 2003 Robert Kooima and the
// Neverball authors. See NOTICE.md and COPYING.

//! The SOL level file: record types, the loader, and geometry accessors.
//!
//! This is a port of `sol_load_file` and the `sol_load_*` record readers in
//! `share/solid_base.c`, for the two file versions 1.6.0 accepts: version 6
//! (the 1.5 format, [`Version::Legacy`]) and version 7 (the development format
//! 1.6.0 itself writes, [`Version::Current`]).
//!
//! # Differences from the C loader
//!
//! * Every count, range and index is checked while loading. The C loader
//!   trusts the file; this one returns an [`Error`] instead of reading past an
//!   array.
//! * The shared index pool (`iv`) is resolved while loading: each [`Lump`]
//!   owns its vertex, edge, geometry and side index lists, and each [`Body`]
//!   owns its geometry list. The raw pool is not kept.
//! * The string block (`av`) is resolved into [`Attribute`] key/value strings.
//! * Where the C loader treats `-1` as "none" (`pi`, `pj` and `ni` of a body,
//!   the links of a BSP node, a switch path) the field is an [`Option`].
//!
//! Nothing here simulates or renders; it exposes the data those parts need.

use core::ops::Range;

use crate::error::Error;
use crate::reader::Reader;

/// A 3-component vector in level space.
pub type Vec3 = [f32; 3];

/// First word of every SOL file: `0xAF 'S' 'O' 'L'` read little-endian.
pub const SOL_MAGIC: i32 = i32::from_le_bytes([0xAF, b'S', b'O', b'L']);

/// Size in bytes of the fixed texture-name field of a material (`PATHMAX`).
pub const NAME_LEN: usize = 64;

/// Bit values of the flag words stored in SOL records.
pub mod flag {
    /// Material: texture coordinates are clamped in T.
    pub const MATERIAL_CLAMP_T: u32 = 1 << 0;
    /// Material: texture coordinates are clamped in S.
    pub const MATERIAL_CLAMP_S: u32 = 1 << 1;
    /// Material: additive blending.
    pub const MATERIAL_ADDITIVE: u32 = 1 << 2;
    /// Material: drawn without back-face culling.
    pub const MATERIAL_TWO_SIDED: u32 = 1 << 3;
    /// Material: environment-mapped.
    pub const MATERIAL_ENVIRONMENT: u32 = 1 << 4;
    /// Material: decal.
    pub const MATERIAL_DECAL: u32 = 1 << 5;
    /// Material: receives shadows.
    pub const MATERIAL_SHADOWED: u32 = 1 << 6;
    /// Material: alpha-blended.
    pub const MATERIAL_TRANSPARENT: u32 = 1 << 7;
    /// Material: reflective.
    pub const MATERIAL_REFLECTIVE: u32 = 1 << 8;
    /// Material: alpha-tested (version 7 stores a function and reference).
    pub const MATERIAL_ALPHA_TEST: u32 = 1 << 9;
    /// Material: particle.
    pub const MATERIAL_PARTICLE: u32 = 1 << 10;
    /// Billboard: edge-aligned.
    pub const BILLBOARD_EDGE: u32 = 1;
    /// Billboard: flat.
    pub const BILLBOARD_FLAT: u32 = 2;
    /// Billboard: no face.
    pub const BILLBOARD_NOFACE: u32 = 4;
    /// Lump: detail geometry (no collision).
    pub const LUMP_DETAIL: u32 = 1;
    /// Path: the record carries an orientation quaternion.
    pub const PATH_ORIENTED: u32 = 1;
}

/// SOL format version.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Version {
    /// Version 6, the Neverball 1.5 format. Offsets are derived from each
    /// triangle, paths and bodies lack the newer fields, and material flags
    /// use the old bit layout.
    Legacy,
    /// Version 7, the format Neverball 1.6.0 writes.
    Current,
}

impl Version {
    /// The number stored in the file.
    #[must_use]
    pub const fn number(self) -> i32 {
        match self {
            Self::Legacy => 6,
            Self::Current => 7,
        }
    }
}

/// A key/value pair from the file's dictionary (`b_dict`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Attribute {
    /// Attribute name, for example `message` or `time`.
    pub key: String,
    /// Attribute value.
    pub value: String,
}

/// A material (`b_mtrl`).
#[derive(Debug, Clone, PartialEq)]
pub struct Material {
    /// Diffuse color (RGBA).
    pub diffuse: [f32; 4],
    /// Ambient color (RGBA).
    pub ambient: [f32; 4],
    /// Specular color (RGBA).
    pub specular: [f32; 4],
    /// Emission color (RGBA).
    pub emission: [f32; 4],
    /// Specular exponent.
    pub shininess: f32,
    /// Flags, using the current bit layout in [`flag`]. Legacy files are
    /// converted as `sol_load_mtrl` does.
    pub flags: u32,
    /// Texture name, relative to the data directory, without extension.
    pub texture: String,
    /// Alpha comparison function; zero unless [`flag::MATERIAL_ALPHA_TEST`].
    pub alpha_func: i32,
    /// Alpha reference value; zero unless [`flag::MATERIAL_ALPHA_TEST`].
    pub alpha_ref: f32,
}

impl Material {
    /// True if every bit of `mask` is set in [`Material::flags`].
    #[must_use]
    pub const fn has(&self, mask: u32) -> bool {
        self.flags & mask == mask
    }
}

/// An edge (`b_edge`): two vertex indices.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Edge {
    /// Indices into [`Sol::vertices`].
    pub vertices: [usize; 2],
}

/// A side plane (`b_side`): points `p` with `normal . p == distance`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Side {
    /// Plane normal.
    pub normal: Vec3,
    /// Distance from the origin along the normal.
    pub distance: f32,
}

/// A texture-coordinate pair (`b_texc`).
pub type TexCoord = [f32; 2];

/// A triangle corner as three independent indices (`b_offs`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Offset {
    /// Index into [`Sol::tex_coords`].
    pub tex_coord: usize,
    /// Index into [`Sol::sides`]; its normal is the corner normal.
    pub side: usize,
    /// Index into [`Sol::vertices`].
    pub vertex: usize,
}

/// A triangle (`b_geom`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Geom {
    /// Index into [`Sol::materials`].
    pub material: usize,
    /// Three indices into [`Sol::offsets`], one per corner.
    pub offsets: [usize; 3],
}

/// A convex collision lump (`b_lump`) with its index lists resolved.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Lump {
    /// Raw flags; see [`flag::LUMP_DETAIL`].
    pub flags: u32,
    /// Indices into [`Sol::vertices`].
    pub vertices: Vec<usize>,
    /// Indices into [`Sol::edges`].
    pub edges: Vec<usize>,
    /// Indices into [`Sol::geoms`].
    pub geoms: Vec<usize>,
    /// Indices into [`Sol::sides`].
    pub sides: Vec<usize>,
}

impl Lump {
    /// True for detail lumps, which the game draws but never collides with.
    #[must_use]
    pub const fn is_detail(&self) -> bool {
        self.flags & flag::LUMP_DETAIL != 0
    }
}

/// A BSP node (`b_node`). A leaf has no side and no children.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Node {
    /// Splitting plane, an index into [`Sol::sides`] (`si`).
    pub side: Option<usize>,
    /// Child on the front of the plane (`ni`), an index into [`Sol::nodes`].
    pub fore: Option<usize>,
    /// Child on the back of the plane (`nj`), an index into [`Sol::nodes`].
    pub back: Option<usize>,
    /// Lumps stored at this node: a range of [`Sol::lumps`] (`l0`, `lc`).
    pub lumps: Range<usize>,
}

/// A moving-platform waypoint (`b_path`).
#[derive(Debug, Clone, PartialEq)]
pub struct Path {
    /// Position of the waypoint.
    pub position: Vec3,
    /// Orientation quaternion; `[1, 0, 0, 0]` unless [`flag::PATH_ORIENTED`].
    pub orientation: [f32; 4],
    /// Travel time to the next waypoint, in seconds, after millisecond
    /// rounding.
    pub time: f32,
    /// Travel time in whole milliseconds, as the simulation counts it.
    pub time_ms: i32,
    /// Next waypoint, an index into [`Sol::paths`] (`pi`).
    pub next: usize,
    /// Whether the path starts enabled (`f`).
    pub enabled: bool,
    /// Whether motion eases between waypoints (`s`).
    pub smooth: bool,
    /// Raw flags; zero for legacy files.
    pub flags: u32,
}

/// A moving or static body (`b_body`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Body {
    /// First path (`pi`); `None` for a static body.
    pub path: Option<usize>,
    /// Second path (`pj`). Equal to [`Body::path`] when the file has no second
    /// path, as in legacy files.
    pub path_alt: Option<usize>,
    /// Root BSP node (`ni`), if the body has collision geometry.
    pub node: Option<usize>,
    /// Lumps of this body: a range of [`Sol::lumps`] (`l0`, `lc`).
    pub lumps: Range<usize>,
    /// Triangles of this body, indices into [`Sol::geoms`] (`g0`, `gc`).
    pub geoms: Vec<usize>,
}

/// What an item does when collected (`b_item::t`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ItemKind {
    /// `ITEM_NONE`.
    None,
    /// `ITEM_COIN`; the item's value is the coin worth.
    Coin,
    /// `ITEM_GROW`.
    Grow,
    /// `ITEM_SHRINK`.
    Shrink,
    /// A type number 1.6.0 does not define. The C loader keeps these too.
    Unknown(i32),
}

impl ItemKind {
    const fn from_raw(raw: i32) -> Self {
        match raw {
            0 => Self::None,
            1 => Self::Coin,
            2 => Self::Grow,
            3 => Self::Shrink,
            other => Self::Unknown(other),
        }
    }
}

/// A collectible (`b_item`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Item {
    /// Position.
    pub position: Vec3,
    /// Item type.
    pub kind: ItemKind,
    /// Value (`n`); for coins, how many coins it is worth.
    pub value: i32,
}

/// A goal zone (`b_goal`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Goal {
    /// Center.
    pub position: Vec3,
    /// Radius.
    pub radius: f32,
}

/// A teleporter (`b_jump`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Jump {
    /// Entry position.
    pub position: Vec3,
    /// Exit position.
    pub target: Vec3,
    /// Radius.
    pub radius: f32,
}

/// A switch (`b_swch`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Switch {
    /// Position.
    pub position: Vec3,
    /// Radius.
    pub radius: f32,
    /// The path the switch drives (`pi`), an index into [`Sol::paths`].
    pub path: Option<usize>,
    /// Default timer in seconds, after millisecond rounding.
    pub timer: f32,
    /// Default timer in whole milliseconds.
    pub timer_ms: i32,
    /// Default state (`f`).
    pub default_state: bool,
    /// Whether the switch is hidden (`i`).
    pub invisible: bool,
}

/// A billboard (`b_bill`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Billboard {
    /// Raw type flags; see the `BILLBOARD_*` constants in [`flag`].
    pub flags: u32,
    /// Index into [`Sol::materials`].
    pub material: usize,
    /// Repeat time interval (`t`).
    pub repeat_time: f32,
    /// Distance (`d`).
    pub distance: f32,
    /// Width coefficients (`w`).
    pub width: Vec3,
    /// Height coefficients (`h`).
    pub height: Vec3,
    /// X rotation coefficients (`rx`).
    pub rot_x: Vec3,
    /// Y rotation coefficients (`ry`).
    pub rot_y: Vec3,
    /// Z rotation coefficients (`rz`).
    pub rot_z: Vec3,
    /// Position.
    pub position: Vec3,
}

/// The ball's start position (`b_ball`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Ball {
    /// Start position.
    pub position: Vec3,
    /// Radius.
    pub radius: f32,
}

/// A camera viewpoint (`b_view`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct View {
    /// Camera position.
    pub position: Vec3,
    /// Look-at target.
    pub target: Vec3,
}

/// One triangle corner with the data a renderer needs.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Corner {
    /// Vertex position.
    pub position: Vec3,
    /// Normal, taken from the corner's side plane.
    pub normal: Vec3,
    /// Texture coordinate.
    pub tex_coord: TexCoord,
}

/// A triangle with its material.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Triangle<'a> {
    /// The triangle's material.
    pub material: &'a Material,
    /// The three corners.
    pub corners: [Corner; 3],
}

/// A loaded SOL level (`struct s_base`).
///
/// All fields are public for reading. The accessor methods index the arrays
/// without re-checking and assume the cross-references established by
/// [`Sol::from_bytes`]; they panic if a caller edits the fields into an
/// inconsistent state.
#[derive(Debug, Clone, PartialEq)]
pub struct Sol {
    /// File format version.
    pub version: Version,
    /// Dictionary attributes, in file order.
    pub attributes: Vec<Attribute>,
    /// Materials.
    pub materials: Vec<Material>,
    /// Vertex positions.
    pub vertices: Vec<Vec3>,
    /// Edges.
    pub edges: Vec<Edge>,
    /// Side planes.
    pub sides: Vec<Side>,
    /// Texture coordinates.
    pub tex_coords: Vec<TexCoord>,
    /// Triangle corner offsets. For legacy files these are derived while
    /// loading geometry.
    pub offsets: Vec<Offset>,
    /// Triangles.
    pub geoms: Vec<Geom>,
    /// Collision lumps.
    pub lumps: Vec<Lump>,
    /// BSP nodes.
    pub nodes: Vec<Node>,
    /// Waypoints.
    pub paths: Vec<Path>,
    /// Bodies. Body 0 is the static world in shipped levels.
    pub bodies: Vec<Body>,
    /// Collectibles.
    pub items: Vec<Item>,
    /// Goal zones.
    pub goals: Vec<Goal>,
    /// Teleporters.
    pub jumps: Vec<Jump>,
    /// Switches.
    pub switches: Vec<Switch>,
    /// Billboards.
    pub billboards: Vec<Billboard>,
    /// Ball start positions. Never empty: a file with none gets one zeroed
    /// ball, as `sol_load_file` does ("magically fix all of our code").
    pub balls: Vec<Ball>,
    /// Viewpoints.
    pub views: Vec<View>,
}

// ---------------------------------------------------------------------------
// Loading.

#[derive(Debug)]
struct Counts {
    strings: usize,
    dict: usize,
    mtrl: usize,
    vert: usize,
    edge: usize,
    side: usize,
    texc: usize,
    offs: usize,
    geom: usize,
    lump: usize,
    node: usize,
    path: usize,
    body: usize,
    item: usize,
    goal: usize,
    jump: usize,
    swch: usize,
    bill: usize,
    ball: usize,
    view: usize,
    pool: usize,
}

fn count(section: &'static str, value: i32) -> Result<usize, Error> {
    usize::try_from(value).map_err(|_| Error::NegativeCount {
        section,
        count: value,
    })
}

/// Reads the header counts in the order `sol_load_indx` does.
fn read_counts(r: &mut Reader<'_>, version: Version) -> Result<Counts, Error> {
    r.set_section("header");
    let strings = count("string block", r.i32()?)?;
    let dict = count("dictionary", r.i32()?)?;
    let mtrl = count("material", r.i32()?)?;
    let vert = count("vertex", r.i32()?)?;
    let edge = count("edge", r.i32()?)?;
    let side = count("side", r.i32()?)?;
    let texc = count("texture coordinate", r.i32()?)?;
    let offs = match version {
        Version::Current => count("offset", r.i32()?)?,
        Version::Legacy => 0,
    };
    Ok(Counts {
        strings,
        dict,
        mtrl,
        vert,
        edge,
        side,
        texc,
        offs,
        geom: count("geometry", r.i32()?)?,
        lump: count("lump", r.i32()?)?,
        node: count("node", r.i32()?)?,
        path: count("path", r.i32()?)?,
        body: count("body", r.i32()?)?,
        item: count("item", r.i32()?)?,
        goal: count("goal", r.i32()?)?,
        jump: count("jump", r.i32()?)?,
        swch: count("switch", r.i32()?)?,
        bill: count("billboard", r.i32()?)?,
        ball: count("ball", r.i32()?)?,
        view: count("view", r.i32()?)?,
        pool: count("index pool", r.i32()?)?,
    })
}

/// Rejects headers whose counts could not possibly fit in the file, before
/// anything is allocated for them.
fn check_budget(c: &Counts, version: Version, available: usize) -> Result<(), Error> {
    let (geom, path, body) = match version {
        Version::Legacy => (40_u64, 28_u64, 24_u64),
        Version::Current => (16, 32, 28),
    };
    let table: [(&'static str, usize, u64); 21] = [
        ("string block", c.strings, 1),
        ("dictionary", c.dict, 8),
        ("material", c.mtrl, 136),
        ("vertex", c.vert, 12),
        ("edge", c.edge, 8),
        ("side", c.side, 16),
        ("texture coordinate", c.texc, 8),
        ("offset", c.offs, 12),
        ("geometry", c.geom, geom),
        ("lump", c.lump, 36),
        ("node", c.node, 20),
        ("path", c.path, path),
        ("body", c.body, body),
        ("item", c.item, 20),
        ("goal", c.goal, 16),
        ("jump", c.jump, 28),
        ("switch", c.swch, 40),
        ("billboard", c.bill, 88),
        ("ball", c.ball, 16),
        ("view", c.view, 24),
        ("index pool", c.pool, 4),
    ];
    let available = available as u64;
    let mut needed = 0_u64;
    for (section, n, size) in table {
        needed += n as u64 * size;
        if needed > available {
            return Err(Error::CountExceedsData {
                section,
                count: n,
                needed,
                available,
            });
        }
    }
    Ok(())
}

fn to_index(
    record: &'static str,
    index: usize,
    field: &'static str,
    value: i32,
    limit: usize,
) -> Result<usize, Error> {
    usize::try_from(value)
        .ok()
        .filter(|&v| v < limit)
        .ok_or(Error::BadReference {
            record,
            index,
            field,
            value,
            limit,
        })
}

/// Like [`to_index`], but a negative value means "none".
fn to_option_index(
    record: &'static str,
    index: usize,
    field: &'static str,
    value: i32,
    limit: usize,
) -> Result<Option<usize>, Error> {
    if value < 0 {
        Ok(None)
    } else {
        to_index(record, index, field, value, limit).map(Some)
    }
}

/// Checks that `start..start + count` fits in `0..limit`. An empty range is
/// always fine, whatever its start: the C code never touches it.
fn to_range(
    record: &'static str,
    index: usize,
    field: &'static str,
    start: i32,
    count: i32,
    limit: usize,
) -> Result<Range<usize>, Error> {
    let bad = || Error::BadRange {
        record,
        index,
        field,
        start,
        count,
        limit,
    };
    let n = usize::try_from(count).map_err(|_| bad())?;
    if n == 0 {
        return Ok(0..0);
    }
    let s = usize::try_from(start).map_err(|_| bad())?;
    let end = s.checked_add(n).ok_or_else(bad)?;
    if end > limit {
        return Err(bad());
    }
    Ok(s..end)
}

fn finite(
    record: &'static str,
    index: usize,
    field: &'static str,
    values: &[f32],
) -> Result<(), Error> {
    if values.iter().all(|v| v.is_finite()) {
        Ok(())
    } else {
        Err(Error::NonFinite {
            record,
            index,
            field,
        })
    }
}

/// `TIME_TO_MS`: `(int) (f + 0.5f * SIGN(f))` of `t * 1000`.
fn to_millis(t: f32) -> i32 {
    let f = t * 1000.0;
    let sign = if f < 0.0 {
        -1.0
    } else if f > 0.0 {
        1.0
    } else {
        0.0
    };
    (f + 0.5 * sign) as i32
}

/// `MS_TO_TIME`.
fn from_millis(ms: i32) -> f32 {
    ms as f32 * 0.001
}

fn string_at(
    block: &[u8],
    entry: usize,
    part: &'static str,
    offset: i32,
) -> Result<String, Error> {
    let bad = || Error::BadString {
        entry,
        part,
        offset,
    };
    let start = usize::try_from(offset).map_err(|_| bad())?;
    let tail = block.get(start..).ok_or_else(bad)?;
    let len = tail.iter().position(|&b| b == 0).ok_or_else(bad)?;
    Ok(String::from_utf8_lossy(&tail[..len]).into_owned())
}

/// Version 6 material flag bits and what `sol_load_mtrl` turns them into.
const LEGACY_FLAGS: [(i32, u32); 8] = [
    (1, flag::MATERIAL_SHADOWED),
    (2, flag::MATERIAL_TRANSPARENT),
    (4, flag::MATERIAL_REFLECTIVE | flag::MATERIAL_SHADOWED),
    (8, flag::MATERIAL_ENVIRONMENT),
    (16, flag::MATERIAL_ADDITIVE),
    (32, flag::MATERIAL_CLAMP_S | flag::MATERIAL_CLAMP_T),
    (64, flag::MATERIAL_DECAL | flag::MATERIAL_SHADOWED),
    (128, flag::MATERIAL_TWO_SIDED),
];

/// Converts 1.5 material flags. A zero word is the invisible material: it
/// becomes transparent with zero diffuse alpha.
fn convert_legacy_flags(old: i32, diffuse: &mut [f32; 4]) -> u32 {
    if old == 0 {
        diffuse[3] = 0.0;
        return flag::MATERIAL_TRANSPARENT;
    }
    LEGACY_FLAGS
        .iter()
        .filter(|&&(bit, _)| old & bit != 0)
        .fold(0, |acc, &(_, new)| acc | new)
}

fn read_section<'a, T>(
    r: &mut Reader<'a>,
    section: &'static str,
    n: usize,
    mut read: impl FnMut(&mut Reader<'a>, usize) -> Result<T, Error>,
) -> Result<Vec<T>, Error> {
    r.set_section(section);
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        out.push(read(r, i)?);
    }
    Ok(out)
}

fn read_offset(r: &mut Reader<'_>, index: usize, c: &Counts) -> Result<Offset, Error> {
    let ti = r.i32()?;
    let si = r.i32()?;
    let vi = r.i32()?;
    Ok(Offset {
        tex_coord: to_index("offset", index, "tex_coord", ti, c.texc)?,
        side: to_index("offset", index, "side", si, c.side)?,
        vertex: to_index("offset", index, "vertex", vi, c.vert)?,
    })
}

/// The version 6 half of `sol_load_geom`: three inline offset triples, each
/// matched against the offsets known so far.
///
/// All three are looked up before any is appended, so a triple repeated within
/// one triangle is appended twice, exactly as the C code does.
fn read_legacy_offsets(
    r: &mut Reader<'_>,
    c: &Counts,
    offsets: &mut Vec<Offset>,
) -> Result<[usize; 3], Error> {
    let known = offsets.len();
    let candidates = [
        read_offset(r, known, c)?,
        read_offset(r, known, c)?,
        read_offset(r, known, c)?,
    ];
    let found = candidates.map(|cand| offsets[..known].iter().position(|o| *o == cand));
    let mut ids = [0_usize; 3];
    for ((slot, cand), hit) in ids.iter_mut().zip(candidates).zip(found) {
        *slot = match hit {
            Some(j) => j,
            None => {
                offsets.push(cand);
                offsets.len() - 1
            }
        };
    }
    Ok(ids)
}

struct RawLump {
    flags: u32,
    vertices: Range<usize>,
    edges: Range<usize>,
    geoms: Range<usize>,
    sides: Range<usize>,
}

fn resolve(
    pool: &[i32],
    range: &Range<usize>,
    record: &'static str,
    index: usize,
    field: &'static str,
    limit: usize,
) -> Result<Vec<usize>, Error> {
    pool[range.clone()]
        .iter()
        .map(|&v| to_index(record, index, field, v, limit))
        .collect()
}

/// Rejects node graphs with a cycle. Iterative, so a hostile file cannot
/// overflow the stack.
fn check_node_cycles(nodes: &[Node]) -> Result<(), Error> {
    const NEW: u8 = 0;
    const OPEN: u8 = 1;
    const DONE: u8 = 2;
    let mut state = vec![NEW; nodes.len()];
    let mut stack: Vec<(usize, u8)> = Vec::new();
    for root in 0..nodes.len() {
        if state[root] != NEW {
            continue;
        }
        state[root] = OPEN;
        stack.push((root, 0));
        while let Some(top) = stack.last_mut() {
            let (node, slot) = *top;
            let child = match slot {
                0 => nodes[node].fore,
                1 => nodes[node].back,
                _ => {
                    state[node] = DONE;
                    stack.pop();
                    continue;
                }
            };
            top.1 += 1;
            if let Some(next) = child {
                match state[next] {
                    OPEN => return Err(Error::NodeCycle { node: next }),
                    NEW => {
                        state[next] = OPEN;
                        stack.push((next, 0));
                    }
                    _ => {}
                }
            }
        }
    }
    Ok(())
}

impl Sol {
    /// Parses and checks a complete SOL file held in memory.
    ///
    /// Bytes after the last record are ignored, as the C loader ignores them.
    ///
    /// # Errors
    ///
    /// Returns an [`Error`] for a wrong magic number or version, a file that
    /// ends early, a count that cannot fit in the file, an index or range that
    /// points outside its array, a non-finite coordinate, or a BSP cycle.
    pub fn from_bytes(data: &[u8]) -> Result<Self, Error> {
        let mut r = Reader::new(data);

        let magic = r.i32()?;
        if magic != SOL_MAGIC {
            return Err(Error::BadMagic { found: magic });
        }
        let version = match r.i32()? {
            6 => Version::Legacy,
            7 => Version::Current,
            found => return Err(Error::UnsupportedVersion { found }),
        };

        let c = read_counts(&mut r, version)?;
        check_budget(&c, version, r.remaining())?;

        r.set_section("string block");
        let strings = r.bytes(c.strings)?;

        let attributes = read_section(&mut r, "dictionary", c.dict, |r, i| {
            let ai = r.i32()?;
            let aj = r.i32()?;
            Ok(Attribute {
                key: string_at(strings, i, "key", ai)?,
                value: string_at(strings, i, "value", aj)?,
            })
        })?;

        let materials = read_section(&mut r, "material", c.mtrl, |r, _| {
            let mut diffuse = r.floats::<4>()?;
            let ambient = r.floats::<4>()?;
            let specular = r.floats::<4>()?;
            let emission = r.floats::<4>()?;
            let [shininess] = r.floats::<1>()?;
            let raw_flags = r.i32()?;
            let name = r.bytes(NAME_LEN)?;
            let len = name.iter().position(|&b| b == 0).unwrap_or(name.len());
            let texture = String::from_utf8_lossy(&name[..len]).into_owned();

            let (alpha_func, alpha_ref) = if version == Version::Current
                && (raw_flags as u32) & flag::MATERIAL_ALPHA_TEST != 0
            {
                (r.i32()?, r.f32()?)
            } else {
                (0, 0.0)
            };
            let flags = match version {
                Version::Legacy => convert_legacy_flags(raw_flags, &mut diffuse),
                Version::Current => raw_flags as u32,
            };
            Ok(Material {
                diffuse,
                ambient,
                specular,
                emission,
                shininess,
                flags,
                texture,
                alpha_func,
                alpha_ref,
            })
        })?;

        let vertices = read_section(&mut r, "vertex", c.vert, |r, i| {
            let p = r.floats::<3>()?;
            finite("vertex", i, "position", &p)?;
            Ok(p)
        })?;

        let edges = read_section(&mut r, "edge", c.edge, |r, i| {
            let vi = r.i32()?;
            let vj = r.i32()?;
            Ok(Edge {
                vertices: [
                    to_index("edge", i, "vertex_a", vi, c.vert)?,
                    to_index("edge", i, "vertex_b", vj, c.vert)?,
                ],
            })
        })?;

        let sides = read_section(&mut r, "side", c.side, |r, i| {
            let normal = r.floats::<3>()?;
            let distance = r.f32()?;
            finite("side", i, "normal", &normal)?;
            finite("side", i, "distance", &[distance])?;
            Ok(Side { normal, distance })
        })?;

        let tex_coords = read_section(&mut r, "texture coordinate", c.texc, |r, _| r.floats::<2>())?;

        let mut offsets = match version {
            Version::Current => read_section(&mut r, "offset", c.offs, |r, i| read_offset(r, i, &c))?,
            Version::Legacy => Vec::new(),
        };

        r.set_section("geometry");
        let mut geoms = Vec::with_capacity(c.geom);
        for i in 0..c.geom {
            let mi = r.i32()?;
            let material = to_index("geometry", i, "material", mi, c.mtrl)?;
            let ids = match version {
                Version::Current => {
                    let oi = r.i32()?;
                    let oj = r.i32()?;
                    let ok = r.i32()?;
                    let limit = offsets.len();
                    [
                        to_index("geometry", i, "offset_i", oi, limit)?,
                        to_index("geometry", i, "offset_j", oj, limit)?,
                        to_index("geometry", i, "offset_k", ok, limit)?,
                    ]
                }
                Version::Legacy => read_legacy_offsets(&mut r, &c, &mut offsets)?,
            };
            geoms.push(Geom {
                material,
                offsets: ids,
            });
        }

        let raw_lumps = read_section(&mut r, "lump", c.lump, |r, i| {
            let fl = r.i32()?;
            let v0 = r.i32()?;
            let vc = r.i32()?;
            let e0 = r.i32()?;
            let ec = r.i32()?;
            let g0 = r.i32()?;
            let gc = r.i32()?;
            let s0 = r.i32()?;
            let sc = r.i32()?;
            Ok(RawLump {
                flags: fl as u32,
                vertices: to_range("lump", i, "vertices", v0, vc, c.pool)?,
                edges: to_range("lump", i, "edges", e0, ec, c.pool)?,
                geoms: to_range("lump", i, "geoms", g0, gc, c.pool)?,
                sides: to_range("lump", i, "sides", s0, sc, c.pool)?,
            })
        })?;

        let nodes = read_section(&mut r, "node", c.node, |r, i| {
            let si = r.i32()?;
            let ni = r.i32()?;
            let nj = r.i32()?;
            let l0 = r.i32()?;
            let lc = r.i32()?;
            Ok(Node {
                side: to_option_index("node", i, "side", si, c.side)?,
                fore: to_option_index("node", i, "fore", ni, c.node)?,
                back: to_option_index("node", i, "back", nj, c.node)?,
                lumps: to_range("node", i, "lumps", l0, lc, c.lump)?,
            })
        })?;

        let paths = read_section(&mut r, "path", c.path, |r, i| {
            let position = r.floats::<3>()?;
            let t = r.f32()?;
            let pi = r.i32()?;
            let f = r.i32()?;
            let s = r.i32()?;
            let flags = match version {
                Version::Current => r.i32()? as u32,
                Version::Legacy => 0,
            };
            let mut orientation = [1.0, 0.0, 0.0, 0.0];
            if flags & flag::PATH_ORIENTED != 0 {
                orientation = r.floats::<4>()?;
            }
            finite("path", i, "position", &position)?;
            finite("path", i, "orientation", &orientation)?;
            finite("path", i, "time", &[t])?;
            let time_ms = to_millis(t);
            Ok(Path {
                position,
                orientation,
                time: from_millis(time_ms),
                time_ms,
                next: to_index("path", i, "next", pi, c.path)?,
                enabled: f != 0,
                smooth: s != 0,
                flags,
            })
        })?;

        let raw_bodies = read_section(&mut r, "body", c.body, |r, i| {
            let pi = r.i32()?;
            let pj = match version {
                Version::Current => {
                    let pj = r.i32()?;
                    if pj < 0 { pi } else { pj }
                }
                Version::Legacy => pi,
            };
            let ni = r.i32()?;
            let l0 = r.i32()?;
            let lc = r.i32()?;
            let g0 = r.i32()?;
            let gc = r.i32()?;
            let body = Body {
                path: to_option_index("body", i, "path", pi, c.path)?,
                path_alt: to_option_index("body", i, "path_alt", pj, c.path)?,
                node: to_option_index("body", i, "node", ni, c.node)?,
                lumps: to_range("body", i, "lumps", l0, lc, c.lump)?,
                geoms: Vec::new(),
            };
            Ok((body, to_range("body", i, "geoms", g0, gc, c.pool)?))
        })?;

        let items = read_section(&mut r, "item", c.item, |r, i| {
            let position = r.floats::<3>()?;
            let t = r.i32()?;
            let value = r.i32()?;
            finite("item", i, "position", &position)?;
            Ok(Item {
                position,
                kind: ItemKind::from_raw(t),
                value,
            })
        })?;

        let goals = read_section(&mut r, "goal", c.goal, |r, i| {
            let position = r.floats::<3>()?;
            let radius = r.f32()?;
            finite("goal", i, "position", &position)?;
            finite("goal", i, "radius", &[radius])?;
            Ok(Goal { position, radius })
        })?;

        let jumps = read_section(&mut r, "jump", c.jump, |r, i| {
            let position = r.floats::<3>()?;
            let target = r.floats::<3>()?;
            let radius = r.f32()?;
            finite("jump", i, "position", &position)?;
            finite("jump", i, "target", &target)?;
            finite("jump", i, "radius", &[radius])?;
            Ok(Jump {
                position,
                target,
                radius,
            })
        })?;

        let switches = read_section(&mut r, "switch", c.swch, |r, i| {
            let position = r.floats::<3>()?;
            let radius = r.f32()?;
            let pi = r.i32()?;
            let t = r.f32()?;
            let _unused_float = r.f32()?;
            let f = r.i32()?;
            let _unused_int = r.i32()?;
            let invisible = r.i32()?;
            finite("switch", i, "position", &position)?;
            finite("switch", i, "radius", &[radius])?;
            finite("switch", i, "timer", &[t])?;
            let timer_ms = to_millis(t);
            Ok(Switch {
                position,
                radius,
                path: to_option_index("switch", i, "path", pi, c.path)?,
                timer: from_millis(timer_ms),
                timer_ms,
                default_state: f != 0,
                invisible: invisible != 0,
            })
        })?;

        let billboards = read_section(&mut r, "billboard", c.bill, |r, i| {
            let fl = r.i32()?;
            let mi = r.i32()?;
            let repeat_time = r.f32()?;
            let distance = r.f32()?;
            let width = r.floats::<3>()?;
            let height = r.floats::<3>()?;
            let rot_x = r.floats::<3>()?;
            let rot_y = r.floats::<3>()?;
            let rot_z = r.floats::<3>()?;
            let position = r.floats::<3>()?;
            Ok(Billboard {
                flags: fl as u32,
                material: to_index("billboard", i, "material", mi, c.mtrl)?,
                repeat_time,
                distance,
                width,
                height,
                rot_x,
                rot_y,
                rot_z,
                position,
            })
        })?;

        let mut balls = read_section(&mut r, "ball", c.ball, |r, i| {
            let position = r.floats::<3>()?;
            let radius = r.f32()?;
            finite("ball", i, "position", &position)?;
            finite("ball", i, "radius", &[radius])?;
            Ok(Ball { position, radius })
        })?;

        let views = read_section(&mut r, "view", c.view, |r, i| {
            let position = r.floats::<3>()?;
            let target = r.floats::<3>()?;
            finite("view", i, "position", &position)?;
            finite("view", i, "target", &target)?;
            Ok(View { position, target })
        })?;

        let pool = read_section(&mut r, "index pool", c.pool, |r, _| r.i32())?;

        // Resolve the shared index pool, now that its contents are known.
        let lumps = raw_lumps
            .into_iter()
            .enumerate()
            .map(|(i, raw)| -> Result<Lump, Error> {
                Ok(Lump {
                    flags: raw.flags,
                    vertices: resolve(&pool, &raw.vertices, "lump", i, "vertex", c.vert)?,
                    edges: resolve(&pool, &raw.edges, "lump", i, "edge", c.edge)?,
                    geoms: resolve(&pool, &raw.geoms, "lump", i, "geom", c.geom)?,
                    sides: resolve(&pool, &raw.sides, "lump", i, "side", c.side)?,
                })
            })
            .collect::<Result<Vec<_>, Error>>()?;

        let bodies = raw_bodies
            .into_iter()
            .enumerate()
            .map(|(i, (mut body, range))| -> Result<Body, Error> {
                body.geoms = resolve(&pool, &range, "body", i, "geom", c.geom)?;
                Ok(body)
            })
            .collect::<Result<Vec<_>, Error>>()?;

        check_node_cycles(&nodes)?;

        if balls.is_empty() {
            balls.push(Ball {
                position: [0.0; 3],
                radius: 0.0,
            });
        }

        Ok(Self {
            version,
            attributes,
            materials,
            vertices,
            edges,
            sides,
            tex_coords,
            offsets,
            geoms,
            lumps,
            nodes,
            paths,
            bodies,
            items,
            goals,
            jumps,
            switches,
            billboards,
            balls,
            views,
        })
    }

    /// The value of attribute `key`. When a key repeats, the last entry wins,
    /// matching the C level scanner, which overwrites as it goes.
    #[must_use]
    pub fn attribute(&self, key: &str) -> Option<&str> {
        self.attributes
            .iter()
            .rev()
            .find(|a| a.key == key)
            .map(|a| a.value.as_str())
    }

    /// The renderable data of one triangle corner.
    #[must_use]
    pub fn corner(&self, offset: usize) -> Corner {
        let o = &self.offsets[offset];
        Corner {
            position: self.vertices[o.vertex],
            normal: self.sides[o.side].normal,
            tex_coord: self.tex_coords[o.tex_coord],
        }
    }

    /// The three corners of a triangle.
    #[must_use]
    pub fn corners(&self, geom: &Geom) -> [Corner; 3] {
        geom.offsets.map(|o| self.corner(o))
    }

    /// The lumps of `body`.
    #[must_use]
    pub fn body_lumps(&self, body: &Body) -> &[Lump] {
        &self.lumps[body.lumps.clone()]
    }

    /// The triangles of `body`, each with its material.
    pub fn body_triangles<'a>(
        &'a self,
        body: &'a Body,
    ) -> impl Iterator<Item = Triangle<'a>> + 'a {
        body.geoms.iter().map(move |&g| {
            let geom = &self.geoms[g];
            Triangle {
                material: &self.materials[geom.material],
                corners: self.corners(geom),
            }
        })
    }

    /// The vertices of `lump`.
    pub fn lump_vertices<'a>(&'a self, lump: &'a Lump) -> impl Iterator<Item = &'a Vec3> + 'a {
        lump.vertices.iter().map(move |&v| &self.vertices[v])
    }

    /// The edges of `lump`.
    pub fn lump_edges<'a>(&'a self, lump: &'a Lump) -> impl Iterator<Item = &'a Edge> + 'a {
        lump.edges.iter().map(move |&e| &self.edges[e])
    }

    /// The side planes of `lump`.
    pub fn lump_sides<'a>(&'a self, lump: &'a Lump) -> impl Iterator<Item = &'a Side> + 'a {
        lump.sides.iter().map(move |&s| &self.sides[s])
    }

    /// The first ball start. Always present; see [`Sol::balls`].
    #[must_use]
    pub fn start(&self) -> &Ball {
        &self.balls[0]
    }

    /// Axis-aligned bounds of all vertices, or `None` if there are none.
    #[must_use]
    pub fn bounds(&self) -> Option<(Vec3, Vec3)> {
        let first = *self.vertices.first()?;
        Some(self.vertices.iter().fold((first, first), |(lo, hi), v| {
            (
                [lo[0].min(v[0]), lo[1].min(v[1]), lo[2].min(v[2])],
                [hi[0].max(v[0]), hi[1].max(v[1]), hi[2].max(v[2])],
            )
        }))
    }

    /// Height below which the ball has fallen out of the level: the Y of the
    /// first vertex (`game_server.c` compares the ball's Y with `vv[0].p[1]`).
    /// `None` when the level has no vertices; the game then treats the ball as
    /// fallen immediately.
    #[must_use]
    pub fn fall_height(&self) -> Option<f32> {
        self.vertices.first().map(|v| v[1])
    }

    /// Total coin value of all coin items (`n` of each `ITEM_COIN`).
    #[must_use]
    pub fn coin_value(&self) -> i64 {
        self.items
            .iter()
            .filter(|i| i.kind == ItemKind::Coin)
            .map(|i| i64::from(i.value))
            .sum()
    }
}
