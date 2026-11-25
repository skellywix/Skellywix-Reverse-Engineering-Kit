// SPDX-License-Identifier: GPL-2.0-or-later
//
// Rust port of the replay file format of Neverball 1.6.0 (ball/demo.c,
// share/cmd.c, share/cmd.h, share/binary.c). Original: Copyright (C) 2003
// Robert Kooima and the Neverball authors. See NOTICE.md and COPYING.

//! Reading and writing Neverball 1.6.0 replay (`.nbr`) data.
//!
//! A replay is a header (`demo_header_write`) followed by the command stream
//! the game server sent to the game client: each command is one type byte, a
//! 16-bit payload size, and the payload (`cmd_put`). All numbers are
//! little-endian.
//!
//! This module only converts bytes to [`Cmd`] values and back. Every read is
//! bounds-checked, every string is cut at the C buffer size, and every float is
//! checked for finiteness; nothing here touches the disk or the level.
//!
//! # Differences from the C code
//!
//! * A known command whose declared size differs from the bytes it holds is an
//!   error. C ignores the size of a known command, so a damaged size would
//!   silently shift every later command.
//! * A negative size, or a size that runs past the end of the data, is an
//!   error. C seeks by it.
//! * Type `0` is skipped by its declared size like any unknown type. C treats
//!   it as an empty command and does not skip the payload (the writer never
//!   produces it).
//! * Non-finite floats and an updates-per-second outside `1..=1000` are errors;
//!   C divides by the latter and feeds the former to the renderer.
//! * Strings are decoded as lossy UTF-8 with control characters replaced by
//!   `?`; C keeps the raw bytes.

use std::error::Error;
use std::fmt;

/// `DEMO_VERSION`.
pub(crate) const VERSION: i32 = 9;
/// `DEMO_MAGIC`: `0xAF 'N' 'B' 'R'` read as a little-endian integer.
pub(crate) const MAGIC: i32 = i32::from_le_bytes([0xAF, b'N', b'B', b'R']);
/// `MAXSTR`: size of the player name and sound/map name buffers.
pub(crate) const MAXSTR: usize = 256;
/// `PATHMAX`: size of the screenshot and level file name buffers.
pub(crate) const PATHMAX: usize = 64;
/// `DATELEN`: size of the textual date buffer.
const DATELEN: usize = 20;
/// Largest accepted updates-per-second value.
pub(crate) const UPS_MAX: i32 = 1000;

/// Why replay bytes were refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum CodecError {
    /// The data ended inside the named item.
    Truncated(&'static str),
    /// The first word is not the replay magic number.
    BadMagic,
    /// The format version is not the one this game reads.
    BadVersion(i32),
    /// The header records no elapsed time; 1.6.0 refuses such files too.
    NoTime,
    /// A value is out of range or inconsistent.
    Invalid(String),
}

impl fmt::Display for CodecError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Truncated(what) => write!(f, "the replay ends inside {what}"),
            Self::BadMagic => f.write_str("not a Neverball replay (wrong magic number)"),
            Self::BadVersion(v) => {
                write!(f, "replay format version {v} is not supported (this game reads version {VERSION})")
            }
            Self::NoTime => f.write_str("the replay header records no play time (an unfinished recording)"),
            Self::Invalid(why) => write!(f, "corrupt replay: {why}"),
        }
    }
}

impl Error for CodecError {}

fn invalid(why: impl Into<String>) -> CodecError {
    CodecError::Invalid(why.into())
}

// ---------------------------------------------------------------------------
// Header.

/// The replay header (`struct demo` without its path and name).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Header {
    /// Elapsed time of the attempt in hundredths of a second.
    pub timer: i32,
    /// Coins collected.
    pub coins: i32,
    /// `GAME_NONE`, `GAME_TIME`, `GAME_GOAL` or `GAME_FALL`.
    pub status: i32,
    /// `MODE_*` value of `ball/progress.h`.
    pub mode: i32,
    /// Player name.
    pub player: String,
    /// Recording time as seconds since the Unix epoch, if the text parsed.
    pub date: Option<i64>,
    /// Screenshot file of the level.
    pub shot: String,
    /// Level file, relative to the data directory.
    pub file: String,
    /// Level time limit in hundredths of a second (zero: none).
    pub time: i32,
    /// Level coin goal.
    pub goal: i32,
    /// Total coins of the set so far.
    pub score: i32,
    /// Balls left.
    pub balls: i32,
    /// Total time of the set so far.
    pub times: i32,
}

/// Reads the header at the start of `data` (`demo_header_read`) and returns it
/// with the offset of the first command.
pub(crate) fn read_header(data: &[u8]) -> Result<(Header, usize), CodecError> {
    let mut r = Reader::new(data, 0);

    let magic = r.i32("the magic number")?;
    let version = r.i32("the version")?;
    let timer = r.i32("the play time")?;

    if magic != MAGIC {
        return Err(CodecError::BadMagic);
    }
    if version != VERSION {
        return Err(CodecError::BadVersion(version));
    }
    if timer == 0 {
        return Err(CodecError::NoTime);
    }

    let coins = r.i32("the coin count")?;
    let status = r.i32("the status")?;
    let mode = r.i32("the mode")?;
    let player = r.string(MAXSTR, "the player name")?;
    let date = parse_date(&r.string(DATELEN, "the date")?);
    let shot = r.string(PATHMAX, "the screenshot name")?;
    let file = r.string(PATHMAX, "the level name")?;
    let time = r.i32("the time limit")?;
    let goal = r.i32("the coin goal")?;
    let _unused = r.i32("the unused field")?;
    let score = r.i32("the score")?;
    let balls = r.i32("the ball count")?;
    let times = r.i32("the total time")?;

    let header = Header { timer, coins, status, mode, player, date, shot, file, time, goal, score, balls, times };
    Ok((header, r.pos()))
}

/// Appends the header (`demo_header_write`) to `out`.
pub(crate) fn write_header(out: &mut Vec<u8>, h: &Header) {
    put_i32(out, MAGIC);
    put_i32(out, VERSION);
    put_i32(out, h.timer);
    put_i32(out, h.coins);
    put_i32(out, h.status);
    put_i32(out, h.mode);
    put_string(out, &h.player, MAXSTR);
    put_string(out, &format_date(h.date.unwrap_or(0)), DATELEN);
    put_string(out, &h.shot, PATHMAX);
    put_string(out, &h.file, PATHMAX);
    put_i32(out, h.time);
    put_i32(out, h.goal);
    put_i32(out, 0); // Unused (was the goal-enabled flag).
    put_i32(out, h.score);
    put_i32(out, h.balls);
    put_i32(out, h.times);
}

// ---------------------------------------------------------------------------
// Dates (UTC, proleptic Gregorian; `make_time_from_utc` and `gmtime`).

/// Days from 1970-01-01 to the given civil date.
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = if month > 2 { month - 3 } else { month + 9 };
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// The civil date `days` after 1970-01-01.
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

/// Parses `YYYY-MM-DDTHH:MM:SS` into seconds since the Unix epoch.
pub(crate) fn parse_date(text: &str) -> Option<i64> {
    let (date, time) = text.split_once('T')?;
    let mut d = date.splitn(3, '-');
    let mut t = time.splitn(3, ':');
    let next = |it: &mut std::str::SplitN<'_, char>| it.next()?.trim().parse::<i64>().ok();
    let (year, month, day) = (next(&mut d)?, next(&mut d)?, next(&mut d)?);
    let (hour, minute, second) = (next(&mut t)?, next(&mut t)?, next(&mut t)?);

    let ok = (1970..=9999).contains(&year)
        && (1..=12).contains(&month)
        && (1..=31).contains(&day)
        && (0..24).contains(&hour)
        && (0..60).contains(&minute)
        && (0..60).contains(&second);
    ok.then(|| days_from_civil(year, month, day) * 86_400 + hour * 3600 + minute * 60 + second)
}

/// Formats seconds since the Unix epoch as `YYYY-MM-DDTHH:MM:SS` (UTC). Times
/// outside the years 1970 to 9999 are written as the epoch.
pub(crate) fn format_date(secs: i64) -> String {
    let secs = if (0..253_402_300_800).contains(&secs) { secs } else { 0 };
    let (y, mo, d) = civil_from_days(secs.div_euclid(86_400));
    let rem = secs.rem_euclid(86_400);
    format!("{y:04}-{mo:02}-{d:02}T{:02}:{:02}:{:02}", rem / 3600, rem / 60 % 60, rem % 60)
}

// ---------------------------------------------------------------------------
// Commands.

/// One replay command (`union cmd`). Indices are as stored, not yet checked
/// against a level.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Cmd {
    EndOfUpdate,
    MakeBall,
    MakeItem { p: [f32; 3], t: i32, n: i32 },
    PickItem(i32),
    TiltAngles { x: f32, z: f32 },
    Sound { name: String, gain: f32 },
    Timer(f32),
    Status(i32),
    Coins(i32),
    JumpEnter,
    JumpExit,
    BodyPath { body: i32, path: i32 },
    BodyTime { body: i32, t: f32 },
    GoalOpen,
    SwchEnter(i32),
    SwchToggle(i32),
    SwchExit(i32),
    UpdatesPerSecond(i32),
    BallRadius(f32),
    ClearItems,
    ClearBalls,
    BallPosition([f32; 3]),
    BallBasis([[f32; 3]; 2]),
    BallPendBasis([[f32; 3]; 2]),
    ViewPosition([f32; 3]),
    ViewCenter([f32; 3]),
    ViewBasis([[f32; 3]; 2]),
    CurrentBall(i32),
    PathFlag { path: i32, flag: i32 },
    StepSimulation(f32),
    Map { name: String, major: i32, minor: i32 },
    TiltAxes { x: [f32; 3], z: [f32; 3] },
    MovePath { mover: i32, path: i32 },
    MoveTime { mover: i32, t: f32 },
}

/// `enum cmd_type` of `share/cmd.h`.
mod ty {
    pub(super) const END_OF_UPDATE: u8 = 1;
    pub(super) const MAKE_BALL: u8 = 2;
    pub(super) const MAKE_ITEM: u8 = 3;
    pub(super) const PICK_ITEM: u8 = 4;
    pub(super) const TILT_ANGLES: u8 = 5;
    pub(super) const SOUND: u8 = 6;
    pub(super) const TIMER: u8 = 7;
    pub(super) const STATUS: u8 = 8;
    pub(super) const COINS: u8 = 9;
    pub(super) const JUMP_ENTER: u8 = 10;
    pub(super) const JUMP_EXIT: u8 = 11;
    pub(super) const BODY_PATH: u8 = 12;
    pub(super) const BODY_TIME: u8 = 13;
    pub(super) const GOAL_OPEN: u8 = 14;
    pub(super) const SWCH_ENTER: u8 = 15;
    pub(super) const SWCH_TOGGLE: u8 = 16;
    pub(super) const SWCH_EXIT: u8 = 17;
    pub(super) const UPDATES_PER_SECOND: u8 = 18;
    pub(super) const BALL_RADIUS: u8 = 19;
    pub(super) const CLEAR_ITEMS: u8 = 20;
    pub(super) const CLEAR_BALLS: u8 = 21;
    pub(super) const BALL_POSITION: u8 = 22;
    pub(super) const BALL_BASIS: u8 = 23;
    pub(super) const BALL_PEND_BASIS: u8 = 24;
    pub(super) const VIEW_POSITION: u8 = 25;
    pub(super) const VIEW_CENTER: u8 = 26;
    pub(super) const VIEW_BASIS: u8 = 27;
    pub(super) const CURRENT_BALL: u8 = 28;
    pub(super) const PATH_FLAG: u8 = 29;
    pub(super) const STEP_SIMULATION: u8 = 30;
    pub(super) const MAP: u8 = 31;
    pub(super) const TILT_AXES: u8 = 32;
    pub(super) const MOVE_PATH: u8 = 33;
    pub(super) const MOVE_TIME: u8 = 34;
    /// `CMD_MAX`.
    pub(super) const MAX: u8 = 35;
}

/// A bounds-checked cursor over replay bytes (`get_*` of `share/binary.c`).
pub(crate) struct Reader<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    /// A reader over `data` starting at byte `pos` (clamped to the end).
    pub(crate) fn new(data: &'a [u8], pos: usize) -> Self {
        Self { data, pos: pos.min(data.len()) }
    }

    /// Offset of the next unread byte.
    pub(crate) fn pos(&self) -> usize {
        self.pos
    }

    fn remaining(&self) -> usize {
        self.data.len() - self.pos
    }

    fn take(&mut self, n: usize, what: &'static str) -> Result<&'a [u8], CodecError> {
        if self.remaining() < n {
            return Err(CodecError::Truncated(what));
        }
        let bytes = &self.data[self.pos..self.pos + n];
        self.pos += n;
        Ok(bytes)
    }

    fn u8(&mut self, what: &'static str) -> Result<u8, CodecError> {
        Ok(self.take(1, what)?[0])
    }

    fn i16(&mut self, what: &'static str) -> Result<i16, CodecError> {
        let b = self.take(2, what)?;
        Ok(i16::from_le_bytes([b[0], b[1]]))
    }

    fn i32(&mut self, what: &'static str) -> Result<i32, CodecError> {
        let b = self.take(4, what)?;
        Ok(i32::from_le_bytes([b[0], b[1], b[2], b[3]]))
    }

    fn f32(&mut self, what: &'static str) -> Result<f32, CodecError> {
        let b = self.take(4, what)?;
        let v = f32::from_le_bytes([b[0], b[1], b[2], b[3]]);
        if v.is_finite() { Ok(v) } else { Err(invalid(format!("a non-finite number in {what}"))) }
    }

    fn vec3(&mut self, what: &'static str) -> Result<[f32; 3], CodecError> {
        Ok([self.f32(what)?, self.f32(what)?, self.f32(what)?])
    }

    fn vec3_pair(&mut self, what: &'static str) -> Result<[[f32; 3]; 2], CodecError> {
        Ok([self.vec3(what)?, self.vec3(what)?])
    }

    /// `get_string`: reads up to the terminating NUL, keeps at most `max - 1`
    /// bytes, and refuses data that ends before the NUL.
    fn string(&mut self, max: usize, what: &'static str) -> Result<String, CodecError> {
        let rest = &self.data[self.pos..];
        let end = rest.iter().position(|&b| b == 0).ok_or(CodecError::Truncated(what))?;
        let keep = end.min(max.saturating_sub(1));
        let text = String::from_utf8_lossy(&rest[..keep])
            .chars()
            .map(|c| if c.is_control() { '?' } else { c })
            .collect();
        self.pos += end + 1;
        Ok(text)
    }
}

/// Reads the next command (`cmd_get`). Commands of unknown type are skipped by
/// their declared size. Returns `Ok(None)` when the data ends between commands.
pub(crate) fn next_cmd(r: &mut Reader<'_>) -> Result<Option<Cmd>, CodecError> {
    loop {
        if r.remaining() == 0 {
            return Ok(None);
        }
        let kind = r.u8("a command type")?;
        let size = r.i16("a command size")?;
        let size = usize::try_from(size).map_err(|_| invalid(format!("command {kind} has a negative size")))?;

        if kind == 0 || kind >= ty::MAX {
            r.take(size, "a command of unknown type")?;
            continue;
        }

        let start = r.pos();
        let cmd = read_body(r, kind)?;
        let used = r.pos() - start;
        if used != size {
            return Err(invalid(format!("command {kind} declares {size} bytes but holds {used}")));
        }
        return Ok(Some(cmd));
    }
}

fn read_body(r: &mut Reader<'_>, kind: u8) -> Result<Cmd, CodecError> {
    Ok(match kind {
        ty::END_OF_UPDATE => Cmd::EndOfUpdate,
        ty::MAKE_BALL => Cmd::MakeBall,
        ty::MAKE_ITEM => {
            let p = r.vec3("an item position")?;
            let t = r.i32("an item type")?;
            let n = r.i32("an item value")?;
            Cmd::MakeItem { p, t, n }
        }
        ty::PICK_ITEM => Cmd::PickItem(r.i32("an item index")?),
        ty::TILT_ANGLES => {
            let x = r.f32("tilt angles")?;
            let z = r.f32("tilt angles")?;
            Cmd::TiltAngles { x, z }
        }
        ty::SOUND => {
            let name = r.string(MAXSTR, "a sound name")?;
            let gain = r.f32("a sound gain")?;
            Cmd::Sound { name, gain }
        }
        ty::TIMER => Cmd::Timer(r.f32("the clock")?),
        ty::STATUS => Cmd::Status(r.i32("the status")?),
        ty::COINS => Cmd::Coins(r.i32("the coin count")?),
        ty::JUMP_ENTER => Cmd::JumpEnter,
        ty::JUMP_EXIT => Cmd::JumpExit,
        ty::BODY_PATH => {
            let body = r.i32("a body index")?;
            let path = r.i32("a path index")?;
            Cmd::BodyPath { body, path }
        }
        ty::BODY_TIME => {
            let body = r.i32("a body index")?;
            let t = r.f32("a body time")?;
            Cmd::BodyTime { body, t }
        }
        ty::GOAL_OPEN => Cmd::GoalOpen,
        ty::SWCH_ENTER => Cmd::SwchEnter(r.i32("a switch index")?),
        ty::SWCH_TOGGLE => Cmd::SwchToggle(r.i32("a switch index")?),
        ty::SWCH_EXIT => Cmd::SwchExit(r.i32("a switch index")?),
        ty::UPDATES_PER_SECOND => {
            let n = r.i32("the update rate")?;
            if !(1..=UPS_MAX).contains(&n) {
                return Err(invalid(format!("update rate {n} is outside 1..={UPS_MAX}")));
            }
            Cmd::UpdatesPerSecond(n)
        }
        ty::BALL_RADIUS => Cmd::BallRadius(r.f32("a ball radius")?),
        ty::CLEAR_ITEMS => Cmd::ClearItems,
        ty::CLEAR_BALLS => Cmd::ClearBalls,
        ty::BALL_POSITION => Cmd::BallPosition(r.vec3("a ball position")?),
        ty::BALL_BASIS => Cmd::BallBasis(r.vec3_pair("a ball basis")?),
        ty::BALL_PEND_BASIS => Cmd::BallPendBasis(r.vec3_pair("a ball pendulum basis")?),
        ty::VIEW_POSITION => Cmd::ViewPosition(r.vec3("a view position")?),
        ty::VIEW_CENTER => Cmd::ViewCenter(r.vec3("a view center")?),
        ty::VIEW_BASIS => Cmd::ViewBasis(r.vec3_pair("a view basis")?),
        ty::CURRENT_BALL => Cmd::CurrentBall(r.i32("a ball index")?),
        ty::PATH_FLAG => {
            let path = r.i32("a path index")?;
            let flag = r.i32("a path flag")?;
            Cmd::PathFlag { path, flag }
        }
        ty::STEP_SIMULATION => Cmd::StepSimulation(r.f32("a time step")?),
        ty::MAP => {
            let name = r.string(MAXSTR, "a map name")?;
            let major = r.i32("a map version")?;
            let minor = r.i32("a map version")?;
            Cmd::Map { name, major, minor }
        }
        ty::TILT_AXES => {
            let x = r.vec3("tilt axes")?;
            let z = r.vec3("tilt axes")?;
            Cmd::TiltAxes { x, z }
        }
        ty::MOVE_PATH => {
            let mover = r.i32("a mover index")?;
            let path = r.i32("a path index")?;
            Cmd::MovePath { mover, path }
        }
        ty::MOVE_TIME => {
            let mover = r.i32("a mover index")?;
            let t = r.f32("a mover time")?;
            Cmd::MoveTime { mover, t }
        }
        other => return Err(invalid(format!("command type {other} is not known"))),
    })
}

// ---------------------------------------------------------------------------
// Writing.

fn put_i32(out: &mut Vec<u8>, v: i32) {
    out.extend_from_slice(&v.to_le_bytes());
}

fn put_f32(out: &mut Vec<u8>, v: f32) {
    out.extend_from_slice(&v.to_le_bytes());
}

fn put_vec3(out: &mut Vec<u8>, v: [f32; 3]) {
    for x in v {
        put_f32(out, x);
    }
}

/// `put_string` with the C buffer limit: at most `max - 1` bytes, cut on a
/// character boundary, with any NUL inside replaced.
fn put_string(out: &mut Vec<u8>, s: &str, max: usize) {
    let mut kept = 0;
    for c in s.chars() {
        let c = if c.is_control() { '?' } else { c };
        if kept + c.len_utf8() >= max {
            break;
        }
        kept += c.len_utf8();
        let mut buf = [0_u8; 4];
        out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
    }
    out.push(0);
}

/// Appends `cmd` in the on-disk form (`cmd_put`).
pub(crate) fn put(out: &mut Vec<u8>, cmd: &Cmd) {
    let kind = match cmd {
        Cmd::EndOfUpdate => ty::END_OF_UPDATE,
        Cmd::MakeBall => ty::MAKE_BALL,
        Cmd::MakeItem { .. } => ty::MAKE_ITEM,
        Cmd::PickItem(_) => ty::PICK_ITEM,
        Cmd::TiltAngles { .. } => ty::TILT_ANGLES,
        Cmd::Sound { .. } => ty::SOUND,
        Cmd::Timer(_) => ty::TIMER,
        Cmd::Status(_) => ty::STATUS,
        Cmd::Coins(_) => ty::COINS,
        Cmd::JumpEnter => ty::JUMP_ENTER,
        Cmd::JumpExit => ty::JUMP_EXIT,
        Cmd::BodyPath { .. } => ty::BODY_PATH,
        Cmd::BodyTime { .. } => ty::BODY_TIME,
        Cmd::GoalOpen => ty::GOAL_OPEN,
        Cmd::SwchEnter(_) => ty::SWCH_ENTER,
        Cmd::SwchToggle(_) => ty::SWCH_TOGGLE,
        Cmd::SwchExit(_) => ty::SWCH_EXIT,
        Cmd::UpdatesPerSecond(_) => ty::UPDATES_PER_SECOND,
        Cmd::BallRadius(_) => ty::BALL_RADIUS,
        Cmd::ClearItems => ty::CLEAR_ITEMS,
        Cmd::ClearBalls => ty::CLEAR_BALLS,
        Cmd::BallPosition(_) => ty::BALL_POSITION,
        Cmd::BallBasis(_) => ty::BALL_BASIS,
        Cmd::BallPendBasis(_) => ty::BALL_PEND_BASIS,
        Cmd::ViewPosition(_) => ty::VIEW_POSITION,
        Cmd::ViewCenter(_) => ty::VIEW_CENTER,
        Cmd::ViewBasis(_) => ty::VIEW_BASIS,
        Cmd::CurrentBall(_) => ty::CURRENT_BALL,
        Cmd::PathFlag { .. } => ty::PATH_FLAG,
        Cmd::StepSimulation(_) => ty::STEP_SIMULATION,
        Cmd::Map { .. } => ty::MAP,
        Cmd::TiltAxes { .. } => ty::TILT_AXES,
        Cmd::MovePath { .. } => ty::MOVE_PATH,
        Cmd::MoveTime { .. } => ty::MOVE_TIME,
    };

    out.push(kind);
    let size_at = out.len();
    out.extend_from_slice(&[0, 0]);

    match cmd {
        Cmd::EndOfUpdate
        | Cmd::MakeBall
        | Cmd::JumpEnter
        | Cmd::JumpExit
        | Cmd::GoalOpen
        | Cmd::ClearItems
        | Cmd::ClearBalls => {}
        Cmd::MakeItem { p, t, n } => {
            put_vec3(out, *p);
            put_i32(out, *t);
            put_i32(out, *n);
        }
        Cmd::PickItem(v)
        | Cmd::Status(v)
        | Cmd::Coins(v)
        | Cmd::SwchEnter(v)
        | Cmd::SwchToggle(v)
        | Cmd::SwchExit(v)
        | Cmd::UpdatesPerSecond(v)
        | Cmd::CurrentBall(v) => put_i32(out, *v),
        Cmd::TiltAngles { x, z } => {
            put_f32(out, *x);
            put_f32(out, *z);
        }
        Cmd::Sound { name, gain } => {
            put_string(out, name, MAXSTR);
            put_f32(out, *gain);
        }
        Cmd::Timer(v) | Cmd::BallRadius(v) | Cmd::StepSimulation(v) => put_f32(out, *v),
        Cmd::BodyPath { body: a, path: b } | Cmd::MovePath { mover: a, path: b } | Cmd::PathFlag { path: a, flag: b } => {
            put_i32(out, *a);
            put_i32(out, *b);
        }
        Cmd::BodyTime { body: a, t } | Cmd::MoveTime { mover: a, t } => {
            put_i32(out, *a);
            put_f32(out, *t);
        }
        Cmd::BallPosition(v) | Cmd::ViewPosition(v) | Cmd::ViewCenter(v) => put_vec3(out, *v),
        Cmd::BallBasis(e) | Cmd::BallPendBasis(e) | Cmd::ViewBasis(e) => {
            put_vec3(out, e[0]);
            put_vec3(out, e[1]);
        }
        Cmd::Map { name, major, minor } => {
            put_string(out, name, MAXSTR);
            put_i32(out, *major);
            put_i32(out, *minor);
        }
        Cmd::TiltAxes { x, z } => {
            put_vec3(out, *x);
            put_vec3(out, *z);
        }
    }

    let size = i16::try_from(out.len() - size_at - 2).unwrap_or(i16::MAX);
    out[size_at..size_at + 2].copy_from_slice(&size.to_le_bytes());
}
