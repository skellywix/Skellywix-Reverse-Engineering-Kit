// SPDX-License-Identifier: GPL-2.0-or-later
// Derived from Neverball 1.6.0 ball/game_common.c and ball/game_server.c.
use super::TICK;
use crate::sol::Vec3;

/// The three camera presets in the default configuration, in keyboard order.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum CameraMode {
    /// Tracks the ball and steers toward its direction of travel.
    #[default]
    Chase,
    /// Tracks the ball without steering toward its velocity.
    Lazy,
    /// Holds a world heading until the player rotates it.
    Manual,
}

/// Follow camera and its horizontal basis, also used to orient floor tilt.
#[derive(Clone, Copy, Debug)]
pub struct Camera {
    /// Position of the eye in level coordinates.
    pub eye: Vec3,
    /// Position the eye looks at.
    pub center: Vec3,
    /// Horizontal direction toward the right side of the screen.
    pub right: Vec3,
    /// Horizontal direction from the ball toward the camera.
    pub back: Vec3,
    distance_scale: f32,
    active_time: f32,
    fade: f32,
    heading: f32,
}

impl Camera {
    pub(super) fn new(p: Vec3) -> Self {
        Self {
            eye: [p[0], p[1] + 0.75, p[2] + 2.0],
            center: [p[0], p[1] + 0.25, p[2]],
            right: [1.0, 0.0, 0.0],
            back: [0.0, 0.0, 1.0],
            distance_scale: 1.0,
            active_time: 0.0,
            fade: 0.0,
            heading: 0.0,
        }
    }

    pub(super) fn translate(&mut self, from: Vec3, to: Vec3) {
        for i in 0..3 {
            self.eye[i] += to[i] - from[i];
        }
    }

    pub(super) fn update(
        &mut self,
        mode: CameraMode,
        rotation: f32,
        p: Vec3,
        v: Vec3,
        jump: Option<f32>,
    ) {
        let da = rotation.clamp(-10.0, 10.0) * TICK * 90.0;
        if da == 0.0 {
            if self.active_time < 0.0 {
                self.fade = (-self.active_time).clamp(0.2, 1.0);
                self.active_time = 0.0;
            }
            self.active_time += TICK;
        } else {
            if self.active_time > 0.0 {
                self.fade = 0.0;
                self.active_time = 0.0;
            }
            self.active_time -= TICK;
        }
        let (mut bx, mut bz) = (self.back[0], self.back[2]);
        if mode == CameraMode::Manual {
            (bx, bz) = self.heading.sin_cos();
        } else if da == 0.0 {
            let delta: Vec3 = std::array::from_fn(|i| self.eye[i] - p[i]);
            let d = (delta[0] * delta[0] + delta[1] * delta[1] + delta[2] * delta[2]).sqrt();
            if d > 0.0 {
                bx = delta[0] / d;
                bz = delta[2] / d;
            }
            if mode == CameraMode::Chase {
                let fade = if self.fade > 0.0 {
                    (self.active_time / self.fade).min(1.0).powi(3)
                } else {
                    1.0
                };
                let k = (v[0] * v[0] + v[2] * v[2]).sqrt() * 0.25 * fade * TICK;
                bx -= v[0] * k;
                bz -= v[2] * k;
            }
        }
        if da != 0.0 {
            let (s, c) = da.to_radians().sin_cos();
            (bx, bz) = (c * bx + s * bz, -s * bx + c * bz);
        }
        let length = (bx * bx + bz * bz).sqrt();
        if length > 0.0 {
            bx /= length;
            bz /= length;
            self.back = [bx, 0.0, bz];
            self.right = [bz, 0.0, -bx];
        }
        let target = 1.0 - (self.back[0] * v[0] + self.back[2] * v[2]) / 10.0;
        self.distance_scale =
            (self.distance_scale + (target - self.distance_scale) * TICK).max(0.5);
        let dz = 2.0 * self.distance_scale;
        self.eye = [
            p[0] + dz * self.back[0],
            p[1] + 0.75 * self.distance_scale,
            p[2] + dz * self.back[2],
        ];
        self.center = [
            p[0],
            p[1] + 0.25 * jump.map_or(1.0, |t| 2.0 * (t - 0.5).abs()),
            p[2],
        ];
        self.heading = self.back[0].atan2(self.back[2]);
    }
}
