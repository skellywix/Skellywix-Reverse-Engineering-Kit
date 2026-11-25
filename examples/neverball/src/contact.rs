// SPDX-License-Identifier: GPL-2.0-or-later
// Derived from Neverball 1.6.0, Copyright (C) 2003–2010 Neverball authors.

//! Ball contact with collectibles, goal zones, and teleporters. These queries
//! use the current ball position; they do not advance time or unlock a goal.

use crate::sol::{Ball, Goal, Item, ItemKind, Jump, Vec3};

const ITEM_RADIUS: f32 = 0.15;
const GOAL_TRIGGER_HEIGHT: f32 = 1.5;
const JUMP_TRIGGER_HEIGHT: f32 = 1.0;

fn length(x: f32, y: f32, z: f32) -> f32 {
    // vec3.h sums in single precision before taking the double-precision root.
    f64::from(x * x + y * y + z * z).sqrt() as f32
}

fn horizontal_distance(a: Vec3, b: Vec3) -> f32 {
    length(a[0] - b[0], a[2] - b[2], 0.0)
}

fn within_height(y: f32, base: f32, height: f32) -> bool {
    base < y && y < base + height
}

/// Collects the first item in file order within reach of the ball. Returns its
/// original index and value; marks it absent without moving later items.
pub fn take_item(ball: Ball, items: &mut [Item]) -> Option<(usize, Item)> {
    let reach = ball.radius + ITEM_RADIUS;
    for (index, item) in items.iter_mut().enumerate() {
        if item.kind == ItemKind::None {
            continue;
        }
        let [x, y, z] = item.position;
        if length(
            ball.position[0] - x,
            ball.position[1] - y,
            ball.position[2] - z,
        ) < reach
        {
            let taken = *item;
            item.kind = ItemKind::None;
            return Some((index, taken));
        }
    }
    None
}

/// Returns the first goal that contains the ball. A ball tangent to the rim
/// is not inside. Whether the goal is open is a separate gameplay decision.
pub fn goal_at(ball: Ball, goals: &[Goal]) -> Option<usize> {
    goals.iter().position(|goal| {
        within_height(ball.position[1], goal.position[1], GOAL_TRIGGER_HEIGHT)
            && horizontal_distance(ball.position, goal.position) + ball.radius < goal.radius
    })
}

/// Contact with the set of teleporters at one instant.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum JumpContact {
    /// The ball is clear of every pad.
    Outside,
    /// The ball touches a pad but is not fully inside any pad.
    Touch,
    /// The first pad containing the whole ball at trigger height.
    Inside {
        /// Pad index in the original level.
        index: usize,
        /// Exit position, including the ball's offset from the entry center.
        destination: Vec3,
    },
}

/// Tests all pads in file order. A rim contact never masks a later pad that
/// contains the ball; a ball tangent to the inside of the rim can teleport.
pub fn jump_at(ball: Ball, pads: &[Jump]) -> JumpContact {
    let mut contact = JumpContact::Outside;
    for (index, pad) in pads.iter().enumerate() {
        if !within_height(ball.position[1], pad.position[1], JUMP_TRIGGER_HEIGHT) {
            continue;
        }
        let distance = horizontal_distance(ball.position, pad.position) + ball.radius - pad.radius;
        if distance <= ball.radius {
            if distance <= 0.0 {
                return JumpContact::Inside {
                    index,
                    destination: std::array::from_fn(|axis| {
                        pad.target[axis] + (ball.position[axis] - pad.position[axis])
                    }),
                };
            }
            contact = JumpContact::Touch;
        }
    }
    contact
}
