// SPDX-License-Identifier: GPL-2.0-or-later
// Derived from Neverball 1.6.0, Copyright (C) 2003 Robert Kooima and the Neverball authors.
//! Fixed-step play: camera-relative tilt, ball motion, pickups, teleportation,
//! the clock, and goal, fall, and timeout outcomes. No graphics or I/O.
mod camera;

use crate::contact::{self, JumpContact};
use crate::level::Level;
use crate::motion::Motion;
use crate::sol::{Ball, Item, ItemKind, Vec3};
pub use camera::{Camera, CameraMode};

/// Duration of one simulation update in seconds (`1 / UPS`).
pub const TICK: f32 = 1.0 / 90.0;

/// Tilt requests are in degrees in the plane of the camera, not world axes.
#[derive(Debug, Clone, Copy)]
pub struct Controls {
    /// Requested rotation around the camera's right axis, limited to ±20°.
    pub tilt_x: f32,
    /// Requested rotation around the horizontal camera-back axis, limited to ±20°.
    pub tilt_z: f32,
    /// Response time in seconds; the original uses 0.05 for mouse and 0.25 for stick input.
    pub response: f32,
    /// Camera rotation, in units of 90 degrees per second.
    pub rotation: f32,
    /// Selected camera preset.
    pub camera: CameraMode,
}

impl Default for Controls {
    fn default() -> Self {
        Self {
            tilt_x: 0.0,
            tilt_z: 0.0,
            response: 0.05,
            rotation: 0.0,
            camera: CameraMode::Chase,
        }
    }
}

/// First terminal result of one attempt.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum Outcome {
    /// Ball entered an open goal.
    Goal,
    /// Time limit elapsed.
    Time,
    /// Ball fell below the level's first vertex (or the level had no vertices).
    Fall,
}

/// One-shot changes from one update; a held state does not repeat its event.
#[derive(Debug, Default, Clone, Copy)]
pub struct Events {
    /// Item actually removed from the level, with its coin value or size effect.
    pub pickup: Option<Item>,
    /// The size change actually started, if any.
    pub resize: Option<ItemKind>,
    /// A visible switch was triggered.
    pub switched: bool,
    /// Ball entered a jump pad and started a teleport.
    pub jumped: bool,
    /// The coin requirement was first satisfied this frame.
    pub opened_goal: bool,
    /// Collision energy from the solver (zero without a bounce).
    pub impact: f32,
    /// A new terminal outcome, only on its first update.
    pub outcome: Option<Outcome>,
}

#[derive(Debug)]
struct Growth {
    original: f32,
    size: i8,
    start: f32,
    target: f32,
    elapsed: f32,
    active: bool,
}
impl Growth {
    fn new(original: f32) -> Self {
        Self {
            original,
            size: 0,
            start: original,
            target: original,
            elapsed: 0.0,
            active: false,
        }
    }
    fn pick(&mut self, kind: ItemKind, radius: f32) -> Option<ItemKind> {
        let next = match kind {
            ItemKind::Grow => (self.size + 1).min(1),
            ItemKind::Shrink => (self.size - 1).max(-1),
            _ => self.size,
        };
        let changed = next != self.size;
        if changed {
            self.size = next;
            self.target = self.original * (1.0 + 0.5 * f32::from(next));
            self.active = true;
        }
        if self.active {
            self.start = radius;
            self.elapsed = 0.0;
        }
        changed.then_some(kind)
    }
    fn step(&mut self, ball: &mut Ball) {
        if !self.active {
            return;
        }
        self.elapsed = (self.elapsed + TICK).min(0.5);
        let radius = self.start + (self.target - self.start) * (self.elapsed / 0.5);
        ball.position[1] += radius - ball.radius;
        ball.radius = radius;
        self.active = self.elapsed < 0.5;
    }
}

#[derive(Debug, Clone, Copy)]
struct Jump {
    elapsed: f32,
    destination: Vec3,
    translated: bool,
}

/// An attempt owns its level, so its mutable items and platform state cannot be confused with another level.
#[derive(Debug)]
pub struct Game {
    level: Level,
    motion: Motion,
    camera: Camera,
    growth: Growth,
    jump: Option<Jump>,
    jump_enabled: bool,
    tilt: [f32; 2],
    tilt_axes: [Vec3; 2],
    timer: f32,
    countdown: bool,
    coins: i32,
    goal_open: bool,
    outcome: Option<Outcome>,
    paused: bool,
    accumulator: f32,
}
impl Game {
    /// Starts an attempt. `previously_open` permits an unlocked goal on a completed level in normal mode.
    pub fn new(level: Level, previously_open: bool) -> Self {
        let motion = Motion::new(&level.sol);
        let camera = Camera::new(motion.ball.position);
        let growth = Growth::new(motion.ball.radius);
        let timer = level.info.time as f32 / 100.0;
        let goal_open = previously_open || level.info.goal == 0;
        Self {
            level,
            motion,
            camera,
            growth,
            jump: None,
            jump_enabled: true,
            tilt: [0.0; 2],
            tilt_axes: [camera.right, camera.back],
            timer,
            countdown: timer > 0.0,
            coins: 0,
            goal_open,
            outcome: None,
            paused: false,
            accumulator: 0.0,
        }
    }
    /// Current level, with collected items marked absent.
    pub fn level(&self) -> &Level {
        &self.level
    }
    /// Current ball position and radius.
    pub fn ball(&self) -> Ball {
        self.motion.ball
    }
    /// Moving bodies and rolling orientation for the renderer.
    pub fn motion(&self) -> &Motion {
        &self.motion
    }
    /// Current camera and its horizontal axes.
    pub fn camera(&self) -> &Camera {
        &self.camera
    }
    /// Floor rotation axes and angles in degrees at the most recent tick.
    pub fn floor_tilt(&self) -> ([Vec3; 2], [f32; 2]) {
        (self.tilt_axes, self.tilt)
    }
    /// Whether the goal currently accepts the ball.
    pub fn goal_open(&self) -> bool {
        self.goal_open
    }
    /// Seconds into the one-second teleport animation, if active.
    pub fn jump_phase(&self) -> Option<f32> {
        self.jump.map(|jump| jump.elapsed)
    }
    /// Sum of collected coin values.
    pub fn coins(&self) -> i32 {
        self.coins
    }
    /// Time displayed by the original client, truncated to hundredths.
    pub fn clock_centis(&self) -> i32 {
        (self.timer * 100.0) as i32
    }
    /// Time used in score ranking, distinct from remaining time.
    pub fn elapsed_centis(&self) -> i32 {
        if self.countdown {
            self.level.info.time - self.clock_centis()
        } else {
            self.clock_centis()
        }
    }
    /// Outcome, once reached; never replaced by a later fall or timeout.
    pub fn outcome(&self) -> Option<Outcome> {
        self.outcome
    }
    /// Pauses or resumes without accumulating paused wall time.
    pub fn set_paused(&mut self, paused: bool) {
        self.paused = paused;
    }
    /// Runs exactly one update and applies the end-of-frame goal unlock.
    pub fn tick(&mut self, input: &Controls) -> Events {
        self.step(input, true)
    }
    /// Advances a rendered frame and emits events for each 90 Hz update. Goal eligibility is updated after the frame's last physics step.
    pub fn advance(&mut self, seconds: f32, input: &Controls, mut events: impl FnMut(Events)) {
        if self.paused || !seconds.is_finite() || seconds <= 0.0 {
            return;
        }
        self.accumulator += seconds;
        while self.accumulator >= TICK {
            self.accumulator -= TICK;
            events(self.step(input, self.accumulator < TICK));
        }
    }
    fn step(&mut self, input: &Controls, end_frame: bool) -> Events {
        let mut e = Events::default();
        if self.paused || self.outcome == Some(Outcome::Time) {
            return e;
        }
        let active = self.outcome.is_none();
        let factor = TICK / input.response.max(TICK);
        self.tilt[0] += (input.tilt_x.clamp(-20.0, 20.0) - self.tilt[0]) * factor;
        self.tilt[1] += (input.tilt_z.clamp(-20.0, 20.0) - self.tilt[1]) * factor;
        self.tilt_axes = [self.camera.right, self.camera.back];
        self.growth.step(&mut self.motion.ball);
        if let Some(j) = self.jump.as_mut() {
            j.elapsed += TICK;
            if j.elapsed >= 0.5 {
                if !j.translated {
                    self.camera
                        .translate(self.motion.ball.position, j.destination);
                    j.translated = true;
                }
                self.motion.ball.position = j.destination;
            }
            if j.elapsed >= 1.0 {
                self.jump = None;
            }
        } else {
            // Apply X then Z in camera space, the order in game_tilt_grav.
            let g = if self.outcome == Some(Outcome::Goal) {
                9.8
            } else {
                -9.8
            };
            let (sx, cx) = self.tilt[0].to_radians().sin_cos();
            let (sz, cz) = self.tilt[1].to_radians().sin_cos();
            let right = -g * cx * sz;
            let back = g * sx;
            let [x, z] = self.tilt_axes;
            let gravity = [
                right * x[0] + back * z[0],
                g * cx * cz,
                right * x[2] + back * z[2],
            ];
            e.impact = self.motion.step(&self.level.sol, gravity, TICK);
        }
        let ball = self.motion.ball;
        self.camera.update(
            input.camera,
            input.rotation,
            ball.position,
            self.motion.velocity,
            self.jump.map(|j| j.elapsed),
        );
        if active {
            if self.countdown {
                if self.timer < 600.0 {
                    self.timer = (self.timer - TICK).max(0.0);
                }
            } else {
                self.timer += TICK;
            }
            if let Some((_, item)) = contact::take_item(ball, &mut self.level.sol.items) {
                e.pickup = Some(item);
                e.resize = self.growth.pick(item.kind, ball.radius);
                if item.kind == ItemKind::Coin {
                    self.coins = self.coins.saturating_add(item.value);
                }
            }
        }
        e.switched = self.motion.touch_switches(&self.level.sol);
        if self.jump.is_none() {
            match contact::jump_at(ball, &self.level.sol.jumps) {
                JumpContact::Inside { destination, .. } if self.jump_enabled => {
                    self.jump = Some(Jump {
                        elapsed: 0.0,
                        destination,
                        translated: false,
                    });
                    self.jump_enabled = false;
                    e.jumped = true;
                }
                JumpContact::Outside => self.jump_enabled = true,
                _ => {}
            }
        }
        if active {
            let sol = &self.level.sol;
            self.outcome = if self.goal_open && contact::goal_at(ball, &sol.goals).is_some() {
                Some(Outcome::Goal)
            } else if self.countdown && self.timer <= 0.0 {
                Some(Outcome::Time)
            } else if sol.vertices.first().is_none_or(|p| ball.position[1] < p[1]) {
                Some(Outcome::Fall)
            } else {
                None
            };
            e.outcome = self.outcome;
            if end_frame
                && self.outcome.is_none()
                && !self.goal_open
                && self.level.info.goal > 0
                && self.coins >= self.level.info.goal
            {
                self.goal_open = true;
                e.opened_goal = true;
            }
        }
        e
    }
}
