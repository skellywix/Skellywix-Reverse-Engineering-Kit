// SPDX-License-Identifier: GPL-2.0-or-later
//
// Rust port of the replay recording and playback of Neverball 1.6.0
// (ball/demo.c, ball/game_client.c, ball/game_server.c, share/solid_vary.c,
// share/solid_all.c). Original: Copyright (C) 2003 Robert Kooima and the
// Neverball authors. See NOTICE.md and COPYING.
//! Recording of plays and viewing of Neverball 1.6.0 replay (`.nbr`) files.
//!
//! * [`Capture`] watches a live [`Game`] and writes what the 1.6.0 server
//!   would have sent to its client: one command stream, 90 updates a second.
//!   The result is a standard version 9 replay that the original game reads.
//! * [`Playback`] is the 1.6.0 client: it applies a command stream to its own
//!   copy of the level's varying state and interpolates between updates. It
//!   implements [`Frame`], so the renderer draws a replay exactly as it draws
//!   a live game. It never touches a [`Game`], the progress record, or the
//!   scores.
//! * [`browse`] lists `Replays/*.nbr` under the save folder and plays one.
//!
//! Replay files are untrusted input: every read is bounded, every index is
//! checked against the level, and a damaged file is refused with a message.

use crate::assets::{Assets, Result};
use crate::audio::Audio;
use crate::demo_codec::{self as codec, Cmd, CodecError, Header, Reader};
use crate::frame::Frame;
use crate::render::{Renderer, Scene};
use macroquad::prelude::*;
use neverball::game::{Events, Game, Outcome, TICK};
use neverball::progress::Mode;
use neverball::sol::{flag, Ball, Item, ItemKind, Sol};
use neverball::Level;
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

type CResult<T> = std::result::Result<T, CodecError>;
type V3 = [f32; 3];
type Basis = [V3; 3];

/// Folder of the save directory that holds replay files.
const REPLAY_DIR: &str = "Replays";
const LAST_REPLAY: &str = "Last.nbr";
/// Largest replay file read or written.
const MAX_FILE: u64 = 48 * 1024 * 1024;
/// Most replay files listed.
const MAX_LISTED: usize = 512;
/// Most bytes read to list a replay: its header is never larger.
const HEADER_PEEK: u64 = 4096;
/// Most items a replay may create.
const MAX_ITEMS: usize = 1 << 16;
/// Most balls a replay may create.
const MAX_BALLS: usize = 64;
/// Most updates applied for one rendered frame.
const MAX_UPDATES_PER_FRAME: u32 = 64;
/// Most pieces one simulated step may split into.
const MAX_PIECES: u32 = 64;
/// Most rendered frames one `Capture::frame` call turns into updates.
const MAX_TICKS_PER_FRAME: u32 = 4096;
/// Largest time between recorded frames that is believed.
const MILLISECOND: f32 = 0.001;
/// How far a mirrored platform may stray from the game's before it is fixed.
const DRIFT: f32 = 0.02;
/// Most sound commands held between two frames.
const MAX_SOUNDS: usize = 64;
const IDENTITY: Basis = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];
const CURR: usize = 0;
const PREV: usize = 1;

// ---------------------------------------------------------------------------
// Small vector helpers (share/vec3.h).

fn lerp(a: f32, b: f32, t: f32) -> f32 {
    a + (b - a) * t
}

fn lerp3(a: V3, b: V3, t: f32) -> V3 {
    [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)]
}

fn sub(a: V3, b: V3) -> V3 {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}

fn dot(a: V3, b: V3) -> f32 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}

fn cross(a: V3, b: V3) -> V3 {
    [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
}

fn norm(v: V3) -> V3 {
    let d = dot(v, v).sqrt();
    if d == 0.0 { [0.0; 3] } else { [v[0] / d, v[1] / d, v[2] / d] }
}

/// `e_orthonrm`.
fn orthonormal(e: &mut Basis) {
    e[0] = cross(e[1], e[2]);
    e[2] = cross(e[0], e[1]);
    for v in e.iter_mut() {
        *v = norm(*v);
    }
}

/// `e_lerp`.
fn e_lerp(a: &Basis, b: &Basis, t: f32) -> Basis {
    let mut e = [lerp3(a[0], b[0], t), lerp3(a[1], b[1], t), lerp3(a[2], b[2], t)];
    orthonormal(&mut e);
    e
}

/// `e_lerp` for the two stored vectors of a basis.
fn lerp_pair(a: [V3; 2], b: [V3; 2], t: f32) -> [V3; 2] {
    let e = e_lerp(&[a[0], a[1], cross(a[0], a[1])], &[b[0], b[1], cross(b[0], b[1])], t);
    [e[0], e[1]]
}

/// `q_slerp` for `[w, x, y, z]` quaternions.
fn slerp(a: [f32; 4], b: [f32; 4], t: f32) -> [f32; 4] {
    if t <= 0.0 {
        return a;
    }
    if t >= 1.0 {
        return b;
    }
    let mut c = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3];
    let mut sign = 1.0;
    if c < 0.0 {
        c = -c;
        sign = -1.0;
    }
    let (u, v) = if 1.0 - c < 1e-5 {
        (1.0 - t, t)
    } else {
        let r = c.min(1.0).acos();
        let s = r.sin();
        (((1.0 - t) * r).sin() / s, (t * r).sin() / s * sign)
    };
    let q = [a[0] * u + b[0] * v, a[1] * u + b[1] * v, a[2] * u + b[2] * v, a[3] * u + b[3] * v];
    let d = (q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]).sqrt();
    if d == 0.0 { [1.0, 0.0, 0.0, 0.0] } else { [q[0] / d, q[1] / d, q[2] / d, q[3] / d] }
}

fn smooth_step(t: f32) -> f32 {
    3.0 * t * t - 2.0 * t * t * t
}

/// Inverse of [`smooth_step`] on `0..=1`.
fn inverse_smooth_step(y: f32) -> f32 {
    let (mut lo, mut hi) = (0.0_f32, 1.0_f32);
    for _ in 0..24 {
        let mid = 0.5 * (lo + hi);
        if smooth_step(mid) < y { lo = mid } else { hi = mid }
    }
    0.5 * (lo + hi)
}

fn finite3(v: V3) -> bool {
    v.iter().all(|x| x.is_finite())
}

// ---------------------------------------------------------------------------
// Codes shared with the original.

fn status_code(outcome: Option<Outcome>) -> i32 {
    match outcome {
        None => 0,
        Some(Outcome::Time) => 1,
        Some(Outcome::Goal) => 2,
        Some(Outcome::Fall) => 3,
    }
}

fn status_text(status: i32) -> &'static str {
    match status {
        0 => "Aborted",
        1 => "Time-out",
        2 => "Success",
        3 => "Fall-out",
        _ => "Unknown",
    }
}

fn mode_code(mode: Mode) -> i32 {
    match mode {
        Mode::Challenge => 1,
        Mode::Normal => 2,
        Mode::Standalone => 3,
    }
}

fn kind_code(kind: ItemKind) -> i32 {
    match kind {
        ItemKind::None => 0,
        ItemKind::Coin => 1,
        ItemKind::Grow => 2,
        ItemKind::Shrink => 3,
        ItemKind::Unknown(n) => n,
    }
}

fn kind_from(n: i32) -> ItemKind {
    match n {
        0 => ItemKind::None,
        1 => ItemKind::Coin,
        2 => ItemKind::Grow,
        3 => ItemKind::Shrink,
        other => ItemKind::Unknown(other),
    }
}

/// A level file name a replay may ask for: a relative `.sol` path with no
/// way out of the game's data.
fn level_name_ok(name: &str) -> bool {
    !name.is_empty()
        && name.ends_with(".sol")
        && !name.starts_with('/')
        && !name.contains('\\')
        && !name.contains(':')
        && name.split('/').all(|part| !part.is_empty() && part != "." && part != "..")
}

/// Keeps ASCII letters, digits, `-` and `_`; everything else becomes `_`.
fn sanitize(text: &str) -> String {
    text.chars().map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' }).collect()
}

/// `1.2` to `(1, 2)` (`sscanf(v, "%d.%d")`).
fn parse_version(info: &neverball::LevelInfo) -> (i32, i32) {
    let minor = info
        .version_str
        .split_once('.')
        .map(|(_, rest)| {
            let digits: String = rest.trim().chars().take_while(char::is_ascii_digit).collect();
            digits.parse::<i32>().unwrap_or(0)
        })
        .unwrap_or(0);
    (info.version_num, minor)
}

// ---------------------------------------------------------------------------
// Moving bodies (`struct v_move`, `sol_body_p`, `sol_body_e`).

/// A mover: progress along one path.
#[derive(Clone, Copy, Debug)]
struct Mv {
    path: usize,
    t: f32,
    ms: i32,
}

#[derive(Clone, Debug)]
struct PathInfo {
    position: V3,
    orientation: [f32; 4],
    time: f32,
    time_ms: i32,
    next: usize,
    smooth: bool,
    oriented: bool,
}

/// What a replay needs to know of a level to place its moving bodies.
#[derive(Clone, Debug)]
struct Layout {
    paths: Vec<PathInfo>,
    /// Position and orientation mover of each body.
    bodies: Vec<(Option<usize>, Option<usize>)>,
    /// Starting path of each mover.
    start: Vec<usize>,
}

impl Layout {
    /// Builds the movers the way `sol_load_vary` does.
    fn new(sol: &Sol) -> Self {
        let n = sol.paths.len();
        let paths = sol
            .paths
            .iter()
            .enumerate()
            .map(|(i, p)| PathInfo {
                position: p.position,
                orientation: p.orientation,
                time: p.time,
                time_ms: p.time_ms,
                next: if p.next < n { p.next } else { i },
                smooth: p.smooth,
                oriented: p.flags & flag::PATH_ORIENTED != 0,
            })
            .collect();

        let mut start = Vec::new();
        let mut bodies = Vec::with_capacity(sol.bodies.len());
        for body in &sol.bodies {
            let mi = body.path.filter(|&p| p < n).map(|p| {
                start.push(p);
                start.len() - 1
            });
            let mj = if body.path_alt == body.path {
                mi
            } else {
                body.path_alt.filter(|&p| p < n).map(|p| {
                    start.push(p);
                    start.len() - 1
                })
            };
            bodies.push((mi, mj));
        }
        Self { paths, bodies, start }
    }

    fn initial(&self) -> Vec<Mv> {
        self.start.iter().map(|&path| Mv { path, t: 0.0, ms: 0 }).collect()
    }

    /// Where a mover sits between its path's two waypoints, `0..=1` (`s`
    /// before smoothing).
    fn phase(&self, m: &Mv) -> f32 {
        let pp = &self.paths[m.path];
        if pp.time > 0.0 { m.t / pp.time } else { 0.0 }
    }

    fn position(&self, movers: &[Mv], body: usize) -> V3 {
        let Some(m) = self.bodies.get(body).and_then(|b| b.0).and_then(|i| movers.get(i)) else {
            return [0.0; 3];
        };
        let pp = &self.paths[m.path];
        let pq = &self.paths[pp.next];
        let s = self.phase(m);
        let k = if pp.smooth { smooth_step(s) } else { s };
        let v = sub(pq.position, pp.position);
        [pp.position[0] + v[0] * k, pp.position[1] + v[1] * k, pp.position[2] + v[2] * k]
    }

    /// Position and `[w, x, y, z]` orientation of body `body`.
    fn pose(&self, movers: &[Mv], body: usize) -> (V3, [f32; 4]) {
        let orientation = self
            .bodies
            .get(body)
            .and_then(|b| b.1)
            .and_then(|i| movers.get(i))
            .map_or([1.0, 0.0, 0.0, 0.0], |m| {
                let pp = &self.paths[m.path];
                let pq = &self.paths[pp.next];
                if pp.oriented || pq.oriented {
                    let s = self.phase(m);
                    slerp(pp.orientation, pq.orientation, if pp.smooth { smooth_step(s) } else { s })
                } else {
                    [1.0, 0.0, 0.0, 0.0]
                }
            });
        (self.position(movers, body), orientation)
    }
}

// ---------------------------------------------------------------------------
// Replay files.

/// What a full read of a replay found.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Summary {
    /// Updates in the file.
    pub updates: u32,
    /// Commands whose index the level lacks, ignored because the replay says
    /// it is for another version of the level.
    pub ignored: u32,
    /// Whether the replay's level version matches the level (`game_compat_map`).
    pub compat: bool,
    /// The file stops in the middle of a command.
    pub cut: bool,
    /// Length of the replay in seconds.
    pub seconds: f32,
}

/// A replay file held in memory: its header and its command stream.
pub(crate) struct Replay {
    header: Header,
    data: Arc<[u8]>,
    body: usize,
}

impl Replay {
    /// Parses the header of `bytes`; the commands are read later.
    pub(crate) fn from_bytes(bytes: Vec<u8>) -> CResult<Self> {
        if bytes.len() as u64 > MAX_FILE {
            return Err(CodecError::Invalid("the file is larger than 48 MiB".into()));
        }
        let (header, body) = codec::read_header(&bytes)?;
        Ok(Self { header, data: bytes.into(), body })
    }

    /// Reads a replay file of at most 48 MiB.
    pub(crate) fn read(path: &Path) -> Result<Self> {
        let shown = path.display();
        let meta = fs::metadata(path).map_err(|e| format!("Open {shown}: {e}"))?;
        if !meta.is_file() {
            return Err(format!("{shown} is not a file").into());
        }
        if meta.len() > MAX_FILE {
            return Err(format!("{shown} is larger than 48 MiB").into());
        }
        let mut data = Vec::with_capacity(meta.len() as usize);
        fs::File::open(path)
            .and_then(|f| f.take(MAX_FILE + 1).read_to_end(&mut data))
            .map_err(|e| format!("Read {shown}: {e}"))?;
        Ok(Self::from_bytes(data).map_err(|e| format!("{shown}: {e}"))?)
    }

    pub(crate) fn header(&self) -> &Header {
        &self.header
    }

    /// Plays the whole replay against `level` without drawing, so that a
    /// damaged file is refused before the viewer opens it.
    pub(crate) fn check(&self, level: &Level) -> CResult<Summary> {
        let mut play = Playback::new(self, level)?;
        while !play.ended {
            play.read_update()?;
        }
        if play.balls.is_empty() {
            return Err(CodecError::Invalid("the replay has no ball".into()));
        }
        Ok(Summary {
            updates: play.updates,
            ignored: play.ignored,
            compat: play.compat,
            cut: play.cut,
            seconds: play.updates as f32 * play.step_dt(),
        })
    }
}

// ---------------------------------------------------------------------------
// Playback (`ball/game_client.c`, `share/solid_vary.c`).

#[derive(Clone, Copy, Debug)]
struct LBall {
    p: V3,
    e: Basis,
    pend: Basis,
    r: f32,
}

#[derive(Clone, Copy, Debug)]
struct LView {
    p: V3,
    c: V3,
    e: Basis,
}

#[derive(Clone, Copy, Debug)]
struct LTilt {
    x: V3,
    z: V3,
    rx: f32,
    rz: f32,
}

/// The 1.6.0 replay client. Feed it time with [`Playback::advance`]; draw it
/// through [`Frame`].
pub(crate) struct Playback {
    data: Arc<[u8]>,
    pos: usize,
    layout: Layout,
    level_version: i32,
    compat: bool,
    ignored: u32,
    cut: bool,
    ended: bool,
    ups: i32,
    acc: f32,
    alpha: f32,
    first_update: bool,
    next_update: bool,
    got_tilt_axes: bool,
    items: Vec<Item>,
    switches: Vec<bool>,
    flags: Vec<bool>,
    balls: Vec<[LBall; 2]>,
    curr_ball: usize,
    movers: Vec<[Mv; 2]>,
    view: [LView; 2],
    tilt: [LTilt; 2],
    goal_open: bool,
    goal_k: [f32; 2],
    jump_active: bool,
    jump_dt: [f32; 2],
    timer: f32,
    status: i32,
    coins: i32,
    updates: u32,
    sounds: Vec<(String, f32)>,
    // What `game_lerp_apply` produced for the current blend factor.
    shown_ball: LBall,
    shown_view: LView,
    shown_tilt: LTilt,
    shown_movers: Vec<Mv>,
    shown_jump: f32,
}

impl Playback {
    /// Starts the replay: loads the level's initial state and applies the
    /// first update (`game_client_init`, then `demo_update_read`).
    pub(crate) fn new(replay: &Replay, level: &Level) -> CResult<Self> {
        let sol = &level.sol;
        let layout = Layout::new(sol);
        let movers: Vec<[Mv; 2]> = layout.initial().into_iter().map(|m| [m, m]).collect();
        let shown_movers = layout.initial();
        let balls: Vec<[LBall; 2]> = sol
            .balls
            .iter()
            .map(|b| {
                let ball = LBall { p: b.position, e: IDENTITY, pend: IDENTITY, r: b.radius };
                [ball, ball]
            })
            .collect();
        let view = LView { p: [0.0, 0.75, 2.0], c: [0.0, 0.25, 0.0], e: IDENTITY };
        let tilt = LTilt { x: [1.0, 0.0, 0.0], z: [0.0, 0.0, 1.0], rx: 0.0, rz: 0.0 };
        let shown_ball = balls.first().map_or(LBall { p: [0.0; 3], e: IDENTITY, pend: IDENTITY, r: 0.0 }, |b| b[CURR]);

        let mut play = Self {
            data: Arc::clone(&replay.data),
            pos: replay.body,
            level_version: level.info.version_num,
            compat: level.info.version_num == 1,
            layout,
            ignored: 0,
            cut: false,
            ended: false,
            ups: 0,
            acc: 0.0,
            alpha: 1.0,
            first_update: true,
            next_update: false,
            got_tilt_axes: false,
            items: sol.items.clone(),
            switches: sol.switches.iter().map(|s| s.default_state).collect(),
            flags: sol.paths.iter().map(|p| p.enabled).collect(),
            balls,
            curr_ball: 0,
            movers,
            view: [view, view],
            tilt: [tilt, tilt],
            goal_open: false,
            goal_k: [0.0; 2],
            jump_active: false,
            jump_dt: [0.0; 2],
            timer: 0.0,
            status: 0,
            coins: 0,
            updates: 0,
            sounds: Vec::new(),
            shown_ball,
            shown_view: view,
            shown_tilt: tilt,
            shown_movers,
            shown_jump: 0.0,
        };
        play.read_update()?;
        play.apply_lerp();
        Ok(play)
    }

    /// Seconds one update lasts.
    fn step_dt(&self) -> f32 {
        if self.ups > 0 { 1.0 / self.ups as f32 } else { TICK }
    }

    /// Whether every update has been shown.
    pub(crate) fn finished(&self) -> bool {
        self.ended
    }

    /// Whether the file stopped in the middle of a command or update.
    pub(crate) fn cut_short(&self) -> bool {
        self.cut
    }

    /// Whether the replay's level version matched the level's.
    pub(crate) fn compatible(&self) -> bool {
        self.compat
    }

    /// Commands ignored because the level lacks the index they name.
    pub(crate) fn ignored(&self) -> u32 {
        self.ignored
    }

    /// Updates applied so far.
    pub(crate) fn updates(&self) -> u32 {
        self.updates
    }

    /// The clock the player saw, in hundredths of a second (`curr_clock`).
    pub(crate) fn clock_centis(&self) -> i32 {
        (self.timer * 100.0) as i32
    }

    /// Coins collected so far.
    pub(crate) fn coins(&self) -> i32 {
        self.coins
    }

    /// `GAME_NONE`, `GAME_TIME`, `GAME_GOAL` or `GAME_FALL` so far.
    pub(crate) fn status(&self) -> i32 {
        self.status
    }

    /// Takes the sounds the replay asked for since the last call.
    pub(crate) fn take_sounds(&mut self) -> Vec<(String, f32)> {
        std::mem::take(&mut self.sounds)
    }

    /// Moves the replay forward `seconds` of wall time, one update at a time
    /// (`lockstep_run`), and blends toward the next update.
    pub(crate) fn advance(&mut self, seconds: f32) -> CResult<()> {
        if !seconds.is_finite() || seconds <= 0.0 {
            return Ok(());
        }
        let dt = self.step_dt();
        self.acc += seconds;
        let mut done = 0;
        while self.acc >= dt && !self.ended {
            self.acc -= dt;
            self.read_update()?;
            done += 1;
            if done >= MAX_UPDATES_PER_FRAME {
                self.acc = 0.0;
                break;
            }
        }
        self.alpha = if self.ended { 1.0 } else { (self.acc / dt).clamp(0.0, 1.0) };
        self.apply_lerp();
        Ok(())
    }

    /// A command names something the level does not have: corrupt, unless the
    /// replay says it was recorded against another version of the level.
    fn bad(&mut self, what: &str) -> CResult<()> {
        if self.compat {
            Err(CodecError::Invalid(format!("{what} is out of range for this level")))
        } else {
            self.ignored += 1;
            Ok(())
        }
    }

    /// Reads commands up to and including the next end of update
    /// (`demo_update_read`). `Ok(false)` at the end of the file.
    fn read_update(&mut self) -> CResult<bool> {
        let data = Arc::clone(&self.data);
        let mut reader = Reader::new(&data, self.pos);
        loop {
            let cmd = match codec::next_cmd(&mut reader) {
                Ok(Some(cmd)) => cmd,
                Ok(None) => {
                    self.pos = reader.pos();
                    self.ended = true;
                    return Ok(false);
                }
                // A file that stops inside a command (a crash while
                // recording) plays up to that point, like the original.
                Err(CodecError::Truncated(_)) => {
                    self.pos = data.len();
                    self.ended = true;
                    self.cut = true;
                    return Ok(false);
                }
                Err(e) => return Err(e),
            };
            let end = matches!(cmd, Cmd::EndOfUpdate);
            self.apply(cmd)?;
            if end {
                self.pos = reader.pos();
                self.updates += 1;
                return Ok(true);
            }
        }
    }

    /// `game_lerp_copy`.
    fn copy_current(&mut self) {
        for m in &mut self.movers {
            m[PREV] = m[CURR];
        }
        for b in &mut self.balls {
            b[PREV] = b[CURR];
        }
        self.tilt[PREV] = self.tilt[CURR];
        self.view[PREV] = self.view[CURR];
        self.goal_k[PREV] = self.goal_k[CURR];
        self.jump_dt[PREV] = self.jump_dt[CURR];
    }

    /// `game_lerp_apply`.
    fn apply_lerp(&mut self) {
        let a = self.alpha;
        for (shown, m) in self.shown_movers.iter_mut().zip(&self.movers) {
            let t = if m[PREV].path == m[CURR].path { lerp(m[PREV].t, m[CURR].t, a) } else { m[CURR].t * a };
            *shown = Mv { path: m[CURR].path, t, ms: 0 };
        }
        if let Some(b) = self.balls.get(self.curr_ball) {
            self.shown_ball = LBall {
                p: lerp3(b[PREV].p, b[CURR].p, a),
                e: e_lerp(&b[PREV].e, &b[CURR].e, a),
                pend: e_lerp(&b[PREV].pend, &b[CURR].pend, a),
                r: lerp(b[PREV].r, b[CURR].r, a),
            };
        }
        let (v0, v1) = (self.view[PREV], self.view[CURR]);
        self.shown_view = LView { p: lerp3(v0.p, v1.p, a), c: lerp3(v0.c, v1.c, a), e: e_lerp(&v0.e, &v1.e, a) };
        let (t0, t1) = (self.tilt[PREV], self.tilt[CURR]);
        self.shown_tilt = LTilt {
            x: lerp3(t0.x, t1.x, a),
            z: lerp3(t0.z, t1.z, a),
            rx: lerp(t0.rx, t1.rx, a),
            rz: lerp(t0.rz, t1.rz, a),
        };
        self.shown_jump = lerp(self.jump_dt[PREV], self.jump_dt[CURR], a);
    }

    fn index(value: i32, len: usize) -> Option<usize> {
        usize::try_from(value).ok().filter(|&i| i < len)
    }

    /// `game_run_cmd`.
    fn apply(&mut self, cmd: Cmd) -> CResult<()> {
        if self.next_update {
            self.copy_current();
            self.next_update = false;
        }
        match cmd {
            Cmd::EndOfUpdate => {
                self.got_tilt_axes = false;
                self.next_update = true;
                if self.first_update {
                    self.copy_current();
                    self.alpha = 1.0;
                    self.apply_lerp();
                    self.first_update = false;
                } else if self.ups > 0 {
                    // Goal and jump effects advance once per update.
                    let dt = 1.0 / self.ups as f32;
                    if self.goal_open && self.goal_k[CURR] < 1.0 {
                        self.goal_k[CURR] += dt;
                    }
                    if self.jump_active {
                        self.jump_dt[CURR] += dt;
                        if self.jump_dt[PREV] >= 1.0 {
                            self.jump_active = false;
                        }
                    }
                }
            }
            Cmd::MakeBall => {
                if self.balls.len() >= MAX_BALLS {
                    return Err(CodecError::Invalid(format!("more than {MAX_BALLS} balls")));
                }
                let ball = LBall { p: [0.0; 3], e: IDENTITY, pend: IDENTITY, r: 0.0 };
                self.balls.push([ball, ball]);
                self.curr_ball = self.balls.len() - 1;
            }
            Cmd::MakeItem { p, t, n } => {
                if self.items.len() >= MAX_ITEMS {
                    return Err(CodecError::Invalid(format!("more than {MAX_ITEMS} items")));
                }
                self.items.push(Item { position: p, kind: kind_from(t), value: n });
            }
            Cmd::PickItem(i) => match Self::index(i, self.items.len()) {
                Some(i) => self.items[i].kind = ItemKind::None,
                None => self.bad("an item")?,
            },
            Cmd::TiltAngles { x, z } => {
                if !self.got_tilt_axes {
                    // 1.5.1 and earlier tilt around the view vectors.
                    let view = self.view[CURR];
                    self.tilt[CURR].x = view.e[0];
                    self.tilt[CURR].z = view.e[2];
                }
                self.tilt[CURR].rx = x;
                self.tilt[CURR].rz = z;
            }
            Cmd::Sound { name, gain } => {
                if !name.is_empty() && self.sounds.len() < MAX_SOUNDS {
                    self.sounds.push((name, gain));
                }
            }
            Cmd::Timer(t) => self.timer = t,
            Cmd::Status(s) => self.status = s,
            Cmd::Coins(n) => self.coins = n,
            Cmd::JumpEnter => {
                self.jump_active = true;
                self.jump_dt = [0.0; 2];
            }
            Cmd::JumpExit => {}
            Cmd::BodyPath { body, path } => {
                let mover = Self::index(body, self.layout.bodies.len()).and_then(|b| self.layout.bodies[b].0);
                match (mover, Self::index(path, self.layout.paths.len())) {
                    (Some(mi), Some(path)) => self.movers[mi][CURR].path = path,
                    (None, Some(_)) if Self::index(body, self.layout.bodies.len()).is_some() => {}
                    _ => self.bad("a body path")?,
                }
            }
            Cmd::BodyTime { body, t } => {
                let mover = Self::index(body, self.layout.bodies.len()).and_then(|b| self.layout.bodies[b].0);
                match mover {
                    Some(mi) => self.movers[mi][CURR].t = t,
                    None if Self::index(body, self.layout.bodies.len()).is_some() => {}
                    None => self.bad("a body")?,
                }
            }
            Cmd::GoalOpen => {
                if !self.goal_open {
                    self.goal_open = true;
                    self.goal_k[CURR] = if self.first_update { 1.0 } else { 0.0 };
                }
            }
            Cmd::SwchEnter(i) | Cmd::SwchExit(i) => {
                if Self::index(i, self.switches.len()).is_none() {
                    self.bad("a switch")?;
                }
            }
            Cmd::SwchToggle(i) => match Self::index(i, self.switches.len()) {
                Some(i) => self.switches[i] = !self.switches[i],
                None => self.bad("a switch")?,
            },
            Cmd::UpdatesPerSecond(n) => self.ups = n,
            Cmd::BallRadius(r) => match self.balls.get_mut(self.curr_ball) {
                Some(b) => b[CURR].r = r,
                None => return Err(CodecError::Invalid("a ball is set before any ball exists".into())),
            },
            Cmd::ClearItems => self.items.clear(),
            Cmd::ClearBalls => self.balls.clear(),
            Cmd::BallPosition(p) => self.ball_mut()?.p = p,
            Cmd::BallBasis(e) => {
                let ball = self.ball_mut()?;
                ball.e = [e[0], e[1], cross(e[0], e[1])];
            }
            Cmd::BallPendBasis(e) => {
                let ball = self.ball_mut()?;
                ball.pend = [e[0], e[1], cross(e[0], e[1])];
            }
            Cmd::ViewPosition(p) => self.view[CURR].p = p,
            Cmd::ViewCenter(c) => self.view[CURR].c = c,
            Cmd::ViewBasis(e) => self.view[CURR].e = [e[0], e[1], cross(e[0], e[1])],
            Cmd::CurrentBall(i) => match Self::index(i, self.balls.len()) {
                Some(i) => self.curr_ball = i,
                None => self.bad("a ball")?,
            },
            Cmd::PathFlag { path, flag } => match Self::index(path, self.flags.len()) {
                Some(i) => self.flags[i] = flag != 0,
                None => self.bad("a path")?,
            },
            Cmd::StepSimulation(dt) => {
                for m in &mut self.movers {
                    if self.flags[m[CURR].path] {
                        m[CURR].t += dt;
                    }
                }
            }
            Cmd::Map { major, .. } => self.compat = self.level_version == major,
            Cmd::TiltAxes { x, z } => {
                self.got_tilt_axes = true;
                self.tilt[CURR].x = x;
                self.tilt[CURR].z = z;
            }
            Cmd::MovePath { mover, path } => {
                match (Self::index(mover, self.movers.len()), Self::index(path, self.layout.paths.len())) {
                    (Some(m), Some(path)) => self.movers[m][CURR].path = path,
                    _ => self.bad("a mover path")?,
                }
            }
            Cmd::MoveTime { mover, t } => match Self::index(mover, self.movers.len()) {
                Some(m) => self.movers[m][CURR].t = t,
                None => self.bad("a mover")?,
            },
        }
        Ok(())
    }

    fn ball_mut(&mut self) -> CResult<&mut LBall> {
        self.balls
            .get_mut(self.curr_ball)
            .map(|b| &mut b[CURR])
            .ok_or_else(|| CodecError::Invalid("a ball is set before any ball exists".into()))
    }
}

impl Frame for Playback {
    fn ball(&self) -> Ball {
        Ball { position: self.shown_ball.p, radius: self.shown_ball.r }
    }
    fn basis(&self) -> [V3; 3] {
        self.shown_ball.e
    }
    fn view(&self) -> (V3, V3) {
        (self.shown_view.p, self.shown_view.c)
    }
    fn tilt(&self) -> ([V3; 2], [f32; 2]) {
        ([self.shown_tilt.x, self.shown_tilt.z], [self.shown_tilt.rx, self.shown_tilt.rz])
    }
    fn items(&self) -> &[Item] {
        &self.items
    }
    fn body_pose(&self, index: usize) -> (V3, [f32; 4]) {
        self.layout.pose(&self.shown_movers, index)
    }
    fn switch_on(&self, index: usize) -> bool {
        self.switches.get(index).copied().unwrap_or(false)
    }
    fn goal_open(&self) -> bool {
        self.goal_open
    }
    fn jump_phase(&self) -> Option<f32> {
        self.jump_active.then_some(self.shown_jump)
    }
}

// ---------------------------------------------------------------------------
// Recording (`ball/game_server.c`).

/// The ball, view and floor at one instant.
#[derive(Clone, Copy, Debug)]
struct Snap {
    position: V3,
    basis: [V3; 2],
    pend: [V3; 2],
    radius: f32,
    eye: V3,
    center: V3,
    view: [V3; 2],
    axes: [V3; 2],
    angles: [f32; 2],
    timer: f32,
}

impl Snap {
    fn of(game: &Game) -> Self {
        let motion = game.motion();
        let camera = game.camera();
        let (axes, angles) = game.floor_tilt();
        Self {
            position: motion.ball.position,
            basis: [motion.basis[0], motion.basis[1]],
            pend: [motion.pendulum_basis[0], motion.pendulum_basis[1]],
            radius: motion.ball.radius,
            eye: camera.eye,
            center: camera.center,
            view: [camera.right, cross(camera.back, camera.right)],
            axes,
            angles,
            // The game keeps whole hundredths; the half keeps `(int)(t * 100)`
            // from rounding down when a client reads the clock back.
            timer: (game.clock_centis() as f32 + 0.5) / 100.0,
        }
    }

    /// Blend between two snapshots; the floor axes are the later ones.
    fn blend(a: &Self, b: &Self, t: f32) -> Self {
        Self {
            position: lerp3(a.position, b.position, t),
            basis: lerp_pair(a.basis, b.basis, t),
            pend: lerp_pair(a.pend, b.pend, t),
            radius: lerp(a.radius, b.radius, t),
            eye: lerp3(a.eye, b.eye, t),
            center: lerp3(a.center, b.center, t),
            view: lerp_pair(a.view, b.view, t),
            axes: b.axes,
            angles: [lerp(a.angles[0], b.angles[0], t), lerp(a.angles[1], b.angles[1], t)],
            timer: lerp(a.timer, b.timer, t),
        }
    }
}

/// `ms_step`: adds `dt` to the accumulator and returns the whole
/// milliseconds in it.
fn ms_step(accum: &mut f32, dt: f32) -> i32 {
    let mut ms = 0;
    *accum += dt;
    while *accum >= MILLISECOND {
        let before = *accum;
        *accum -= MILLISECOND;
        if *accum >= before {
            break;
        }
        ms += 1;
    }
    ms
}

/// The moving bodies, switches and items as a client would have them: what
/// the recording needs to turn a game's state into replay commands, since a
/// [`Game`] does not expose its movers.
struct Mirror {
    layout: Layout,
    movers: Vec<Mv>,
    flags: Vec<bool>,
    switches: Vec<bool>,
    items: Vec<ItemKind>,
    clock: f32,
}

impl Mirror {
    fn new(sol: &Sol) -> Self {
        let layout = Layout::new(sol);
        Self {
            movers: layout.initial(),
            flags: sol.paths.iter().map(|p| p.enabled).collect(),
            switches: sol.switches.iter().map(|s| s.default_state).collect(),
            items: sol.items.iter().map(|i| i.kind).collect(),
            clock: 0.0,
            layout,
        }
    }

    /// `sol_path_time`: `dt` shortened to the next path change.
    fn path_time(&self, dt: f32) -> f32 {
        let mut dt = dt;
        for m in &self.movers {
            if !self.flags[m.path] {
                continue;
            }
            let base = &self.layout.paths[m.path];
            let mut peek = self.clock;
            if m.ms + ms_step(&mut peek, dt) > base.time_ms {
                dt = (base.time_ms - m.ms) as f32 * MILLISECOND;
            }
        }
        dt
    }

    /// `sol_move`: moves the bodies `dt` seconds, writing the commands the
    /// server would send.
    fn step(&mut self, dt: f32, out: &mut Vec<u8>) {
        let mut remaining = dt;
        for _ in 0..MAX_PIECES {
            if remaining <= 0.0 {
                break;
            }
            let piece = self.path_time(remaining);
            codec::put(out, &Cmd::StepSimulation(piece));
            let ms = ms_step(&mut self.clock, piece);
            for (i, m) in self.movers.iter_mut().enumerate() {
                if !self.flags[m.path] {
                    continue;
                }
                let base = &self.layout.paths[m.path];
                m.t += piece;
                m.ms += ms;
                if m.ms >= base.time_ms {
                    m.t = 0.0;
                    m.ms = 0;
                    m.path = base.next;
                    codec::put(out, &Cmd::MoveTime { mover: i as i32, t: 0.0 });
                    codec::put(out, &Cmd::MovePath { mover: i as i32, path: base.next as i32 });
                }
            }
            remaining -= piece;
        }
    }

    /// Where on `path` a body's position `want` lies, as seconds on the path.
    fn solve(&self, path: usize, want: V3) -> Option<f32> {
        let pp = &self.layout.paths[path];
        let pq = &self.layout.paths[pp.next];
        let v = sub(pq.position, pp.position);
        let vv = dot(v, v);
        if vv < 1e-12 || pp.time <= 0.0 {
            return None;
        }
        let s = dot(sub(want, pp.position), v) / vv;
        if !(-0.02..=1.02).contains(&s) {
            return None;
        }
        let s = s.clamp(0.0, 1.0);
        let s = if pp.smooth { inverse_smooth_step(s) } else { s };
        let k = if pp.smooth { smooth_step(s) } else { s };
        let at = lerp3(pp.position, pq.position, k);
        (dot(sub(at, want), sub(at, want)).sqrt() < DRIFT).then_some(s * pp.time)
    }

    /// Pulls every mover back onto the game's own platform positions and
    /// writes the commands that tell a client. Returns false when a platform
    /// could not be matched.
    fn resync(&mut self, sol: &Sol, game: &Game, out: &mut Vec<u8>) -> bool {
        let motion = game.motion();
        let mut matched = true;
        for body in 0..self.layout.bodies.len() {
            let Some(mi) = self.layout.bodies[body].0 else { continue };
            let want = motion.body_pose(sol, body).0;
            let have = self.layout.position(&self.movers, body);
            if sub(want, have).iter().all(|d| d.abs() <= DRIFT) {
                continue;
            }
            let mut path = self.movers[mi].path;
            let mut found = None;
            for _ in 0..3 {
                if let Some(t) = self.solve(path, want) {
                    found = Some((path, t));
                    break;
                }
                path = self.layout.paths[path].next;
            }
            match found {
                Some((path, t)) => {
                    let base = &self.layout.paths[path];
                    self.movers[mi] = Mv { path, t, ms: ((t * 1000.0).round() as i32).min(base.time_ms) };
                    codec::put(out, &Cmd::MoveTime { mover: mi as i32, t });
                    codec::put(out, &Cmd::MovePath { mover: mi as i32, path: path as i32 });
                }
                None => matched = false,
            }
        }
        matched
    }
}

/// What the recording knows of the play around the level (`demo_play_init`'s
/// arguments).
#[derive(Clone, Debug)]
struct Context {
    mode: Mode,
    balls: i32,
    score: i32,
    times: i32,
}

/// A recording of a live [`Game`], made in the 1.6.0 replay format.
///
/// Call [`Capture::frame`] once per rendered frame with the same `dt` that
/// went into `Game::advance`, and [`Capture::events`] for each event the game
/// reports. A recording is a separate object: it never changes the game, and
/// a game that is not being recorded costs nothing.
pub(crate) struct Capture {
    level_path: String,
    context: Context,
    body: Vec<u8>,
    updates: u32,
    overflow: bool,
    acc: f32,
    paused: bool,
    last: Snap,
    mirror: Mirror,
    sol_paths: usize,
    sent_radius: f32,
    original_radius: f32,
    status_sent: i32,
    coins_sent: i32,
    goal_sent: bool,
    jump_before: Option<f32>,
    jumped: bool,
    sounds: Vec<(String, f32)>,
    drifted: bool,
}

impl Capture {
    /// Starts recording `game`, which is at the start of the level read from
    /// `level_path` (the name the level was loaded under, such as
    /// `map-easy/easy.sol`). The first update holds the whole starting state.
    pub(crate) fn new(level_path: &str, game: &Game) -> Self {
        let level = game.level();
        let snap = Snap::of(game);
        let mut body = Vec::with_capacity(1 << 20);
        let (major, minor) = parse_version(&level.info);
        let timer = snap.timer;
        let goal_open = game.goal_open();

        codec::put(&mut body, &Cmd::Map { name: level_path.to_owned(), major, minor });
        codec::put(&mut body, &Cmd::UpdatesPerSecond((1.0 / TICK).round() as i32));
        codec::put(&mut body, &Cmd::Timer(timer));
        if goal_open {
            codec::put(&mut body, &Cmd::GoalOpen);
        }
        codec::put(&mut body, &Cmd::ClearBalls);
        codec::put(&mut body, &Cmd::MakeBall);
        put_ball(&mut body, &snap);
        codec::put(&mut body, &Cmd::BallRadius(snap.radius));
        codec::put(&mut body, &Cmd::ClearItems);
        for item in &level.sol.items {
            codec::put(&mut body, &Cmd::MakeItem { p: item.position, t: kind_code(item.kind), n: item.value });
        }
        put_view(&mut body, &snap);
        codec::put(&mut body, &Cmd::EndOfUpdate);

        Self {
            level_path: level_path.to_owned(),
            context: Context { mode: Mode::Standalone, balls: 0, score: 0, times: 0 },
            body,
            updates: 1,
            overflow: false,
            acc: 0.0,
            paused: false,
            last: snap,
            mirror: Mirror::new(&level.sol),
            sol_paths: level.sol.paths.len(),
            sent_radius: snap.radius,
            original_radius: snap.radius,
            status_sent: status_code(game.outcome()),
            coins_sent: game.coins(),
            goal_sent: goal_open,
            jump_before: game.jump_phase(),
            jumped: false,
            sounds: Vec::new(),
            drifted: false,
        }
    }

    /// Records the mode, remaining balls, set score, and total time at the start of the attempt.
    pub(crate) fn with_context(mut self, mode: Mode, balls: i32, score: i32, times: i32) -> Self {
        self.context = Context { mode, balls, score, times };
        self
    }

    /// Stops recording time while the game is paused, as `Game::advance` does.
    pub(crate) fn set_paused(&mut self, paused: bool) {
        self.paused = paused;
    }

    /// Whether the moving platforms of the recording stayed on the game's.
    /// When false, a platform may sit slightly off in the replay.
    pub(crate) fn platforms_in_sync(&self) -> bool {
        !self.drifted
    }

    /// Updates recorded so far.
    pub(crate) fn updates(&self) -> u32 {
        self.updates
    }

    /// Notes one event from `Game::advance` for the next recorded update; it
    /// becomes the sounds the 1.6.0 server sends.
    pub(crate) fn events(&mut self, events: &Events) {
        if events.impact > 0.5 {
            let gain = (events.impact - 0.5) * 2.0;
            let name = if self.last.radius > self.original_radius {
                "snd/bumpbig.ogg"
            } else if self.last.radius < self.original_radius {
                "snd/bumplil.ogg"
            } else {
                "snd/bump.ogg"
            };
            self.sound(name, gain);
        }
        if events.pickup.is_some() {
            // The original plays the coin sound for every item.
            self.sound("snd/coin.ogg", 1.0);
        }
        match events.resize {
            Some(ItemKind::Grow) => self.sound("snd/grow.ogg", 1.0),
            Some(ItemKind::Shrink) => self.sound("snd/shrink.ogg", 1.0),
            _ => {}
        }
        if events.switched {
            self.sound("snd/switch.ogg", 1.0);
        }
        if events.jumped {
            self.sound("snd/jump.ogg", 1.0);
            self.jumped = true;
        }
        match events.outcome {
            Some(Outcome::Goal) => self.sound("snd/goal.ogg", 1.0),
            Some(Outcome::Time) => self.sound("snd/time.ogg", 1.0),
            Some(Outcome::Fall) => self.sound("snd/fall.ogg", 1.0),
            None => {}
        }
    }

    fn sound(&mut self, name: &str, gain: f32) {
        if self.sounds.len() < MAX_SOUNDS {
            self.sounds.push((name.to_owned(), gain));
        }
    }

    /// Records the updates that `Game::advance(dt, ..)` just ran. Updates
    /// inside a frame are blended between the previous and the current state;
    /// one-time changes (pickups, status, sounds) go into the frame's last.
    pub(crate) fn frame(&mut self, game: &Game, dt: f32) {
        if self.paused || self.overflow || !dt.is_finite() || dt <= 0.0 {
            return;
        }
        // The same accumulator `Game::advance` runs.
        self.acc += dt;
        let mut ticks = 0_u32;
        while self.acc >= TICK {
            self.acc -= TICK;
            ticks += 1;
            if ticks >= MAX_TICKS_PER_FRAME {
                self.acc = 0.0;
                break;
            }
        }
        if ticks == 0 {
            return;
        }

        let end = Snap::of(game);
        let end_phase = game.jump_phase();
        // The game freezes its platforms while a ball is teleporting.
        let mut jump = self.jump_before;
        let start_tick = match (self.jump_before, end_phase) {
            (None, Some(x)) => Some(i64::from(ticks) - (x / TICK).round() as i64),
            _ => None,
        };
        for tick in 1..=ticks {
            let last = tick == ticks;
            let step = jump.is_none();
            match jump {
                Some(e) => {
                    let e = e + TICK;
                    jump = if e >= 1.0 { None } else { Some(e) };
                }
                None if Some(i64::from(tick)) == start_tick => jump = Some(0.0),
                None => {}
            }
            let snap = if last { end } else { Snap::blend(&self.last, &end, tick as f32 / ticks as f32) };
            self.update(game, &snap, step, last, end_phase);
        }
        self.last = end;
        self.jump_before = end_phase;
        self.jumped = false;
    }

    fn update(&mut self, game: &Game, snap: &Snap, step: bool, last: bool, end_phase: Option<f32>) {
        let mut out = Vec::with_capacity(512);
        codec::put(&mut out, &Cmd::TiltAxes { x: snap.axes[0], z: snap.axes[1] });
        codec::put(&mut out, &Cmd::TiltAngles { x: snap.angles[0], z: snap.angles[1] });
        if snap.radius != self.sent_radius {
            codec::put(&mut out, &Cmd::BallRadius(snap.radius));
            self.sent_radius = snap.radius;
        }
        if last {
            for (name, gain) in std::mem::take(&mut self.sounds) {
                codec::put(&mut out, &Cmd::Sound { name, gain });
            }
        }
        if step {
            self.mirror.step(TICK, &mut out);
        }
        if last {
            let motion = game.motion();
            if !self.mirror.resync(&game.level().sol, game, &mut out) {
                self.drifted = true;
            }
            for (i, on) in self.mirror.flags.iter_mut().enumerate() {
                if let Some(now) = motion.path_enabled(i) {
                    if now != *on {
                        *on = now;
                        codec::put(&mut out, &Cmd::PathFlag { path: i as i32, flag: i32::from(now) });
                    }
                }
            }
            for (i, on) in self.mirror.switches.iter_mut().enumerate() {
                if let Some(now) = motion.switch_on(i) {
                    if now != *on {
                        *on = now;
                        codec::put(&mut out, &Cmd::SwchToggle(i as i32));
                    }
                }
            }
        }
        put_ball(&mut out, snap);
        put_view(&mut out, snap);
        codec::put(&mut out, &Cmd::Timer(snap.timer));
        if last {
            for (i, item) in game.level().sol.items.iter().enumerate() {
                if let Some(known) = self.mirror.items.get_mut(i) {
                    if *known != ItemKind::None && item.kind == ItemKind::None {
                        *known = ItemKind::None;
                        codec::put(&mut out, &Cmd::PickItem(i as i32));
                    }
                }
            }
            if game.coins() != self.coins_sent {
                self.coins_sent = game.coins();
                codec::put(&mut out, &Cmd::Coins(self.coins_sent));
            }
            if game.goal_open() && !self.goal_sent {
                self.goal_sent = true;
                codec::put(&mut out, &Cmd::GoalOpen);
            }
            if self.jumped {
                codec::put(&mut out, &Cmd::JumpEnter);
            } else if self.jump_before.is_some() && end_phase.is_none() {
                codec::put(&mut out, &Cmd::JumpExit);
            }
            let status = status_code(game.outcome());
            if status != self.status_sent {
                self.status_sent = status;
                codec::put(&mut out, &Cmd::Status(status));
            }
        }
        codec::put(&mut out, &Cmd::EndOfUpdate);

        if (self.body.len() + out.len()) as u64 + 1024 > MAX_FILE {
            self.overflow = true;
            return;
        }
        self.body.extend_from_slice(&out);
        self.updates += 1;
    }

    /// Publishes the finished attempt as `Replays/Last.nbr`. A write failure
    /// leaves the previous completed recording intact.
    pub(crate) fn save(&self, save_root: &Path, player: &str, game: &Game) -> Result<()> {
        if self.overflow {
            return Err("The recording grew past 48 MiB and was not saved.".into());
        }
        if self.updates < 2 {
            return Err("Nothing was recorded.".into());
        }
        if self.level_path.len() >= codec::PATHMAX {
            return Err(format!("The level name {} is too long to record.", self.level_path).into());
        }
        let info = &game.level().info;
        let header = Header {
            timer: game.elapsed_centis(),
            coins: game.coins(),
            status: status_code(game.outcome()),
            mode: mode_code(self.context.mode),
            player: player.to_owned(),
            date: SystemTime::now().duration_since(UNIX_EPOCH).ok().and_then(|d| i64::try_from(d.as_secs()).ok()),
            shot: info.shot.clone(),
            file: self.level_path.clone(),
            time: info.time,
            goal: info.goal,
            score: self.context.score,
            balls: self.context.balls,
            times: self.context.times,
        };
        if header.timer <= 0 {
            return Err("No elapsed play time to record.".into());
        }
        let mut bytes = Vec::with_capacity(512);
        codec::write_header(&mut bytes, &header);
        let dir = save_root.join(REPLAY_DIR);
        fs::create_dir_all(&dir)?;
        let stamp = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
        let part = dir.join(format!(".Last-{}-{stamp}.tmp", std::process::id()));
        let mut file = fs::File::create_new(&part)?;
        let written = file.write_all(&bytes).and_then(|()| file.write_all(&self.body)).and_then(|()| file.sync_all());
        drop(file);
        let published = written.and_then(|()| fs::rename(&part, dir.join(LAST_REPLAY)));
        if published.is_err() {
            let _ = fs::remove_file(&part); // Only the private file created by this call.
        }
        Ok(published?)
    }
}

/// The original default is `%s-%l_NN`: use the displayed level number, not its map filename.
pub(crate) fn suggested_name(save_root: &Path, set: &str, level: &str) -> Result<String> {
    let dir = save_root.join(REPLAY_DIR);
    let base = format!("{}-{}", sanitize(set), sanitize(level));
    for n in 1..99 {
        let name = format!("{base}_{n:02}");
        if !dir.join(format!("{name}.nbr")).try_exists()? {
            return Ok(name);
        }
    }
    Ok(format!("{base}_99")) // Still requires confirmation if the last suggested name is in use.
}

/// A single portable file stem. Never interpret a typed name as a path or a Windows device.
fn valid_replay_name(name: &str) -> bool {
    if name.is_empty() || name.len() > 251 || name.ends_with([' ', '.']) || name.eq_ignore_ascii_case("Last")
        || name.chars().any(|c| c.is_control() || "<>:\"/\\|?*".contains(c)) {
        return false;
    }
    let first = name.split('.').next().unwrap_or_default().trim_end().to_ascii_uppercase();
    !matches!(first.as_str(), "CON" | "PRN" | "AUX" | "NUL" | "CONIN$" | "CONOUT$")
        && !first.strip_prefix("COM").or_else(|| first.strip_prefix("LPT"))
            .is_some_and(|digit| matches!(digit, "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "¹" | "²" | "³"))
}

/// Move the current replay only. `AlreadyExists` means to ask before replacing a named replay.
pub(crate) fn name_last(save_root: &Path, name: &str, replace: bool) -> std::io::Result<()> {
    use std::io::{Error, ErrorKind};
    if !valid_replay_name(name) {
        return Err(Error::new(ErrorKind::InvalidInput, "Choose a valid replay name other than Last."));
    }
    let dir = save_root.join(REPLAY_DIR);
    let dest = dir.join(format!("{name}.nbr"));
    let source = dir.join(LAST_REPLAY);
    if replace {
        return fs::rename(source, dest);
    }
    // Reserve the name atomically. A concurrent save can't turn a new name into an unconfirmed overwrite.
    drop(fs::File::create_new(&dest)?);
    let moved = fs::rename(source, &dest);
    if moved.is_err() {
        fs::remove_file(dest)?;
    }
    moved
}

fn put_ball(out: &mut Vec<u8>, snap: &Snap) {
    codec::put(out, &Cmd::BallPosition(snap.position));
    codec::put(out, &Cmd::BallBasis(snap.basis));
    codec::put(out, &Cmd::BallPendBasis(snap.pend));
}

fn put_view(out: &mut Vec<u8>, snap: &Snap) {
    codec::put(out, &Cmd::ViewPosition(snap.eye));
    codec::put(out, &Cmd::ViewCenter(snap.center));
    codec::put(out, &Cmd::ViewBasis(snap.view));
}

/// Creates `dir/<stem>_NN.nbr` with the first free `NN` of 01 to 99 and
/// writes `bytes` to it (`demo_path`). A partly written file is removed.
fn write_new(dir: &Path, stem: &str, bytes: &[u8]) -> Result<PathBuf> {
    if bytes.len() as u64 > MAX_FILE {
        return Err("The replay is larger than 48 MiB.".into());
    }
    fs::create_dir_all(dir).map_err(|e| format!("Create {}: {e}", dir.display()))?;
    let stem: String = if stem.is_empty() { "replay".into() } else { stem.chars().take(56).collect() };
    for n in 1..=99 {
        let path = dir.join(format!("{stem}_{n:02}.nbr"));
        match fs::OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut file) => {
                if let Err(e) = file.write_all(bytes).and_then(|()| file.flush()) {
                    drop(file);
                    // Best effort: the file is the one just created above.
                    let _ = fs::remove_file(&path);
                    return Err(format!("Write {}: {e}", path.display()).into());
                }
                return Ok(path);
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(format!("Create {}: {e}", path.display()).into()),
        }
    }
    Err(format!("All 99 replay names starting with {stem} are in use.").into())
}

/// Copies a replay made elsewhere (for example by the original game) into the
/// replay folder after checking its header, under a name that is not in use.
pub(crate) fn import(save_root: &Path, source: &Path) -> Result<PathBuf> {
    let replay = Replay::read(source)?;
    let stem = source.file_stem().and_then(|s| s.to_str()).map(sanitize).unwrap_or_default();
    write_new(&save_root.join(REPLAY_DIR), &stem, &replay.data)
}

// ---------------------------------------------------------------------------
// Replay browser.

struct Entry {
    path: PathBuf,
    name: String,
    header: std::result::Result<Header, String>,
}

/// Lists `dir/*.nbr`, newest first. An unreadable folder is reported, not
/// hidden; a folder that does not exist yet is an empty list.
fn list(dir: &Path) -> (Vec<Entry>, Option<String>) {
    let reader = match fs::read_dir(dir) {
        Ok(reader) => reader,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return (Vec::new(), None),
        Err(e) => return (Vec::new(), Some(format!("Couldn't read {}: {e}", dir.display()))),
    };
    let mut entries = Vec::new();
    let mut problem = None;
    for item in reader {
        let item = match item {
            Ok(item) => item,
            Err(e) => {
                problem = Some(format!("Couldn't read part of {}: {e}", dir.display()));
                continue;
            }
        };
        let path = item.path();
        let is_replay = path.extension().and_then(|e| e.to_str()).is_some_and(|e| e.eq_ignore_ascii_case("nbr"));
        // A link could point anywhere; only plain files of the folder count.
        if !is_replay || !item.file_type().is_ok_and(|t| t.is_file()) {
            continue;
        }
        if entries.len() >= MAX_LISTED {
            problem = Some(format!("Only the first {MAX_LISTED} replays are listed."));
            break;
        }
        let name = path.file_stem().and_then(|s| s.to_str()).unwrap_or("?").to_owned();
        let header = peek_header(&path);
        entries.push(Entry { path, name, header });
    }
    entries.sort_by(|a, b| {
        let key = |e: &Entry| e.header.as_ref().ok().and_then(|h| h.date);
        key(b).cmp(&key(a)).then_with(|| a.name.cmp(&b.name))
    });
    (entries, problem)
}

fn peek_header(path: &Path) -> std::result::Result<Header, String> {
    let mut data = Vec::new();
    fs::File::open(path)
        .and_then(|f| f.take(HEADER_PEEK).read_to_end(&mut data))
        .map_err(|e| e.to_string())?;
    codec::read_header(&data).map(|(h, _)| h).map_err(|e| e.to_string())
}

/// A replay checked and ready to watch.
struct Opened {
    replay: Replay,
    level: Level,
    scene: Scene,
    summary: Summary,
}

/// Reads, checks and prepares one replay for viewing.
fn open(assets: &mut Assets, path: &Path) -> Result<Opened> {
    let replay = Replay::read(path)?;
    let name = replay.header().file.clone();
    if !level_name_ok(&name) {
        return Err(format!("This replay names a level file ({name}) the game cannot use.").into());
    }
    if !assets.contains(&name) {
        return Err(format!("This replay is for the level {name}, which is not part of this game.").into());
    }
    let level = assets.level(&name)?;
    let summary = replay.check(&level).map_err(|e| format!("{}: {e}", path.display()))?;
    let scene = Scene::load(assets, &level)?;
    Ok(Opened { replay, level, scene, summary })
}

fn clock_text(centis: i32) -> String {
    let c = centis.max(0);
    format!("{}:{:02}.{:02}", c / 6000, c / 100 % 60, c % 100)
}

fn date_text(secs: Option<i64>) -> String {
    secs.map_or_else(|| "unknown date".to_owned(), |s| format!("{} UTC", &codec::format_date(s)[..16].replace('T', " ")))
}

fn draw_label(font: &Font, text: &str, x: f32, y: f32, size: u16, color: Color) {
    draw_text_ex(text, x, y, TextParams { font: Some(font), font_size: size, color, ..Default::default() });
}

fn text_width(font: &Font, text: &str, size: u16) -> f32 {
    measure_text(text, Some(font), size, 1.0).width
}

/// Shortens `text` with `...` until it is at most `max` pixels wide.
fn fit(font: &Font, text: &str, size: u16, max: f32) -> String {
    if text_width(font, text, size) <= max {
        return text.to_owned();
    }
    let mut cut: String = text.chars().take(200).collect();
    while !cut.is_empty() && text_width(font, &format!("{cut}..."), size) > max {
        cut.pop();
    }
    format!("{cut}...")
}

/// A button drawn and tested in one call; true on the frame it is clicked.
fn button(font: &Font, label: &str, x: f32, y: f32, w: f32, h: f32) -> bool {
    let (mx, my) = mouse_position();
    let hover = mx >= x && mx < x + w && my >= y && my < y + h;
    draw_rectangle(x, y, w, h, if hover { Color::new(1.0, 0.8, 0.2, 0.45) } else { Color::new(1.0, 1.0, 1.0, 0.16) });
    let width = text_width(font, label, 22);
    draw_label(font, label, x + (w - width) * 0.5, y + h * 0.5 + 7.0, 22, WHITE);
    hover && is_mouse_button_pressed(MouseButton::Left)
}

const ROW: f32 = 36.0;
const LIST_TOP: f32 = 100.0;
const LIST_BOTTOM: f32 = 72.0;

/// Lists the recorded replays under `save_root` and plays the one chosen.
///
/// Keys: arrows, Page Up/Down, Home/End and the mouse wheel move through the
/// list; Enter or a second click opens a replay; Escape leaves. While
/// watching: Space pauses, Left/Right change the speed (1/8x to 8x), R
/// restarts, Escape returns to the list. Only replays dropped into the folder
/// are listed: the game's data holds no demonstration replays.
pub(crate) async fn browse(
    assets: &mut Assets,
    save_root: &Path,
    renderer: &Renderer,
    audio: &mut Audio,
    font: &Font,
) -> Result<()> {
    let dir = save_root.join(REPLAY_DIR);
    let (mut entries, mut problem) = list(&dir);
    let mut selected = 0_usize;
    let mut top = 0_usize;
    let mut message: Option<String> = None;
    let mut last_click = (usize::MAX, f64::MIN);

    loop {
        let rows = (((screen_height() - LIST_TOP - LIST_BOTTOM) / ROW).floor() as usize).max(1);
        let count = entries.len();
        let mut open_it = false;
        let mut follow = false;

        if is_key_pressed(KeyCode::Escape) {
            return Ok(());
        }
        if count > 0 {
            if is_key_pressed(KeyCode::Down) {
                selected = (selected + 1).min(count - 1);
                follow = true;
            }
            if is_key_pressed(KeyCode::Up) {
                selected = selected.saturating_sub(1);
                follow = true;
            }
            if is_key_pressed(KeyCode::PageDown) {
                selected = (selected + rows).min(count - 1);
                follow = true;
            }
            if is_key_pressed(KeyCode::PageUp) {
                selected = selected.saturating_sub(rows);
                follow = true;
            }
            if is_key_pressed(KeyCode::Home) {
                selected = 0;
                follow = true;
            }
            if is_key_pressed(KeyCode::End) {
                selected = count - 1;
                follow = true;
            }
            if is_key_pressed(KeyCode::Enter) || is_key_pressed(KeyCode::KpEnter) {
                open_it = true;
            }
            let wheel = mouse_wheel().1;
            if wheel != 0.0 {
                top = if wheel > 0.0 { top.saturating_sub(1) } else { top + 1 };
            }
            if follow {
                if selected < top {
                    top = selected;
                } else if selected >= top + rows {
                    top = selected + 1 - rows;
                }
            }
            top = top.min(count.saturating_sub(rows));

            let (mx, my) = mouse_position();
            if is_mouse_button_pressed(MouseButton::Left) && my >= LIST_TOP && my < LIST_TOP + rows as f32 * ROW {
                let index = top + ((my - LIST_TOP) / ROW) as usize;
                if index < count && mx < screen_width() {
                    let now = get_time();
                    if index == selected && last_click.0 == index && now - last_click.1 < 0.6 {
                        open_it = true;
                    }
                    selected = index;
                    last_click = (index, now);
                }
            }
        }

        clear_background(Color::new(0.05, 0.07, 0.15, 1.0));
        draw_label(font, "Replays", 24.0, 52.0, 40, WHITE);
        draw_label(font, &fit(font, &dir.display().to_string(), 18, screen_width() - 48.0), 24.0, 80.0, 18, LIGHTGRAY);

        if count == 0 {
            draw_label(font, "No replays yet.", 24.0, LIST_TOP + 40.0, 28, WHITE);
            draw_label(font, "Finish or abandon a level to record one, or copy .nbr files from", 24.0, LIST_TOP + 74.0, 20, LIGHTGRAY);
            draw_label(font, "the original Neverball 1.6.0 into the folder above.", 24.0, LIST_TOP + 98.0, 20, LIGHTGRAY);
        }
        let (mx, my) = mouse_position();
        for (row, entry) in entries.iter().enumerate().skip(top).take(rows) {
            let y = LIST_TOP + (row - top) as f32 * ROW;
            let hover = my >= y && my < y + ROW && mx >= 0.0 && mx < screen_width();
            if row == selected {
                draw_rectangle(12.0, y, screen_width() - 24.0, ROW - 2.0, Color::new(1.0, 0.8, 0.2, 0.28));
            } else if hover {
                draw_rectangle(12.0, y, screen_width() - 24.0, ROW - 2.0, Color::new(1.0, 1.0, 1.0, 0.08));
            }
            let base = y + 24.0;
            match &entry.header {
                Ok(h) => {
                    let right = format!(
                        "{}   {}   {} coins   {}",
                        date_text(h.date),
                        status_text(h.status),
                        h.coins,
                        clock_text(h.timer)
                    );
                    let right_w = text_width(font, &right, 20);
                    draw_label(font, &right, screen_width() - 24.0 - right_w, base, 20, LIGHTGRAY);
                    let left = format!("{}   {}", h.file.rsplit('/').next().unwrap_or(&h.file), h.player);
                    draw_label(font, &fit(font, &left, 22, (screen_width() - right_w - 72.0).max(80.0)), 24.0, base, 22, WHITE);
                }
                Err(why) => {
                    let text = format!("{}   (unreadable: {why})", entry.name);
                    draw_label(font, &fit(font, &text, 20, screen_width() - 48.0), 24.0, base, 20, Color::new(1.0, 0.5, 0.45, 1.0));
                }
            }
        }

        if let Some(text) = message.as_ref().or(problem.as_ref()) {
            draw_label(font, &fit(font, text, 20, screen_width() - 48.0), 24.0, screen_height() - 48.0, 20, Color::new(1.0, 0.5, 0.45, 1.0));
        } else if count > 0 {
            draw_label(font, "Up/Down choose   Enter or double-click watch   Esc back", 24.0, screen_height() - 48.0, 20, LIGHTGRAY);
        }
        if button(font, "Back", 24.0, screen_height() - 40.0, 110.0, 32.0) {
            return Ok(());
        }
        if count > 0 && button(font, "Watch", screen_width() - 134.0, screen_height() - 40.0, 110.0, 32.0) {
            open_it = true;
        }

        if open_it && selected < count {
            match open(assets, &entries[selected].path) {
                Ok(opened) => {
                    message = None;
                    watch(assets, renderer, audio, font, &opened).await?;
                    (entries, problem) = list(&dir);
                    selected = selected.min(entries.len().saturating_sub(1));
                }
                Err(e) => message = Some(e.to_string()),
            }
        }
        next_frame().await;
    }
}

const SPEEDS: [f32; 8] = [0.0, 0.125, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0];
const NORMAL_SPEED: usize = 4;

fn speed_text(index: usize) -> String {
    match SPEEDS[index] {
        s if s == 0.0 => "Paused".to_owned(),
        s if s < 1.0 => format!("1/{}x", (1.0 / s).round() as i32),
        s => format!("{}x", s as i32),
    }
}

async fn watch(assets: &mut Assets, renderer: &Renderer, audio: &mut Audio, font: &Font, opened: &Opened) -> Result<()> {
    let Opened { replay, level, scene, summary } = opened;
    let header = replay.header();
    let mut play = Playback::new(replay, level)?;
    let mut notice = String::new();
    if let Err(e) = audio.music(assets, &level.info.song).await {
        notice = format!("Music unavailable: {e}");
    }
    let mut speed = NORMAL_SPEED;
    let mut resume = NORMAL_SPEED;
    let mut failure: Option<String> = None;

    loop {
        if is_key_pressed(KeyCode::Escape) {
            break;
        }
        if is_key_pressed(KeyCode::Space) {
            if speed == 0 {
                speed = resume;
            } else {
                resume = speed;
                speed = 0;
            }
        }
        if is_key_pressed(KeyCode::Right) || is_key_pressed(KeyCode::Equal) || is_key_pressed(KeyCode::KpAdd) {
            speed = (speed + 1).min(SPEEDS.len() - 1).max(1);
        }
        if is_key_pressed(KeyCode::Left) || is_key_pressed(KeyCode::Minus) || is_key_pressed(KeyCode::KpSubtract) {
            speed = speed.saturating_sub(1).max(1);
        }
        let mut restart = is_key_pressed(KeyCode::R);

        if failure.is_none() {
            if let Err(e) = play.advance(get_frame_time().min(0.1) * SPEEDS[speed]) {
                failure = Some(e.to_string());
            }
        }
        for (played, (name, gain)) in play.take_sounds().into_iter().enumerate() {
            if played < 6 && SPEEDS[speed] > 0.0 {
                audio.cue(&name, gain);
            }
        }

        renderer.draw(scene, &play, None, get_time() as f32);

        // Title and result.
        let w = screen_width();
        draw_rectangle(0.0, 0.0, w, 64.0, Color::new(0.0, 0.0, 0.0, 0.5));
        let title = format!("{}   {}   {}", header.file.rsplit('/').next().unwrap_or(&header.file), header.player, date_text(header.date));
        draw_label(font, &fit(font, &title, 24, w - 40.0), 20.0, 28.0, 24, WHITE);
        let mut note = format!("Recorded: {} - {} coins - {}", status_text(header.status), header.coins, clock_text(header.timer));
        if !summary.compat {
            note.push_str("   Recorded on another version of this level.");
        }
        if summary.ignored > 0 {
            note.push_str(&format!("   {} commands skipped.", summary.ignored));
        }
        draw_label(font, &fit(font, &note, 18, w - 40.0), 20.0, 52.0, 18, LIGHTGRAY);

        // Clock, coins, state.
        let hud = format!("{}   {} coins   {}", clock_text(play.clock_centis()), play.coins(), status_text(play.status()));
        draw_label(font, &hud, 20.0, 96.0, 28, WHITE);
        if let Some(why) = &failure {
            draw_label(font, &fit(font, &format!("Replay stopped: {why}"), 22, w - 40.0), 20.0, 128.0, 22, Color::new(1.0, 0.5, 0.45, 1.0));
        } else if play.finished() {
            let text = if play.cut_short() { "The replay ends early (the file is cut short)." } else { "End of replay." };
            draw_label(font, text, 20.0, 128.0, 22, Color::new(1.0, 0.85, 0.3, 1.0));
        } else if !notice.is_empty() {
            draw_label(font, &fit(font, &notice, 18, w - 40.0), 20.0, 128.0, 18, LIGHTGRAY);
        }

        // Controls.
        let h = screen_height();
        draw_rectangle(0.0, h - 84.0, w, 84.0, Color::new(0.0, 0.0, 0.0, 0.5));
        let done = play.updates().min(summary.updates);
        let fraction = if summary.updates > 0 { done as f32 / summary.updates as f32 } else { 0.0 };
        draw_rectangle(20.0, h - 78.0, w - 40.0, 6.0, Color::new(1.0, 1.0, 1.0, 0.2));
        draw_rectangle(20.0, h - 78.0, (w - 40.0) * fraction, 6.0, Color::new(1.0, 0.8, 0.2, 1.0));
        let y = h - 62.0;
        let mut x = 20.0;
        if button(font, "Slower", x, y, 92.0, 34.0) {
            speed = speed.saturating_sub(1).max(1);
        }
        x += 100.0;
        if button(font, if speed == 0 { "Play" } else { "Pause" }, x, y, 92.0, 34.0) {
            if speed == 0 {
                speed = resume;
            } else {
                resume = speed;
                speed = 0;
            }
        }
        x += 100.0;
        if button(font, "Faster", x, y, 92.0, 34.0) {
            speed = (speed + 1).min(SPEEDS.len() - 1).max(1);
        }
        x += 100.0;
        if button(font, "Restart", x, y, 92.0, 34.0) {
            restart = true;
        }
        draw_label(font, &speed_text(speed), x + 108.0, y + 25.0, 22, WHITE);
        if button(font, "Back", w - 112.0, y, 92.0, 34.0) {
            break;
        }

        if restart {
            play = Playback::new(replay, level)?;
            failure = None;
        }
        next_frame().await;
    }
    // The replay's song is not the menu's: stop it.
    audio.music(assets, "").await?;
    Ok(())
}

#[cfg(test)]
#[path = "../../tests/support/mod.rs"]
mod replay_fixture;

#[cfg(test)]
mod recording_tests {
    use super::*;
    use neverball::game::Controls;

    #[test]
    fn timed_out_attempt_publishes_last_with_elapsed_time_and_collected_coins() -> Result<()> {
        let mut level = replay_fixture::Builder::empty(7).attr("time", "5").attr("goal", "10");
        level.verts.push([0.0, -100.0, 0.0]);
        level.balls.push(([0.0, 0.6, 0.0], 0.25));
        level.items.push(([0.0, 0.6, 0.0], 1, 1));
        let mut game = Game::new(Level::from_bytes("map-easy/test.sol", &level.build())?, false);
        let mut capture = Capture::new("map-easy/test.sol", &game);
        for _ in 0..10 {
            let event = game.tick(&Controls::default());
            capture.events(&event);
            capture.frame(&game, TICK);
            if event.outcome.is_some() {
                break;
            }
        }
        assert_eq!(game.outcome(), Some(Outcome::Time));
        let root = std::env::temp_dir().join(format!("nb-recording-{}-{}", std::process::id(), SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos()));
        fs::create_dir(&root)?;
        let result = (|| -> Result<()> {
            capture.save(&root, "Player", &game)?;
            let last = fs::read(root.join("Replays/Last.nbr"))?;
            let (header, _) = codec::read_header(&last)?;
            assert_eq!((header.status, header.timer, header.coins), (1, 5, 1));
            assert_eq!(header.file, "map-easy/test.sol");
            let dir = root.join("Replays");
            fs::write(dir.join("easy-01_01.nbr"), b"earlier run")?;
            assert_eq!(suggested_name(&root, "easy", "01")?, "easy-01_02");
            assert_eq!(name_last(&root, "easy-01_01", false).unwrap_err().kind(), std::io::ErrorKind::AlreadyExists);
            assert_eq!(fs::read(dir.join("easy-01_01.nbr"))?, b"earlier run");
            assert_eq!(name_last(&root, "../outside", false).unwrap_err().kind(), std::io::ErrorKind::InvalidInput);
            assert!(dir.join("Last.nbr").is_file());
            name_last(&root, "easy-01_01", true)?;
            assert_eq!(fs::read(dir.join("easy-01_01.nbr"))?, last);
            assert!(!dir.join("Last.nbr").try_exists()?);
            capture.save(&root, "Next", &game)?;
            name_last(&root, "easy-01_02", false)?;
            let (next, _) = codec::read_header(&fs::read(dir.join("easy-01_02.nbr"))?)?;
            assert_eq!(next.player, "Next");
            Ok(())
        })();
        fs::remove_dir_all(&root)?;
        result
    }
}
