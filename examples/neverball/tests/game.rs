// SPDX-License-Identifier: GPL-2.0-or-later
// Expectations from Neverball 1.6.0 ball/game_server.c, game_common.c and progress.c.
mod support;

use neverball::Level;
use neverball::game::{CameraMode, Controls, Game, Outcome, TICK};
use support::Builder;

fn stage(time: &str, goal: &str) -> Builder {
    let mut b = Builder::empty(7).attr("time", time).attr("goal", goal);
    b.verts.push([0.0, -100.0, 0.0]);
    b.balls.push(([0.0, 0.6, 0.0], 0.25));
    b
}

fn start(b: Builder) -> Game {
    let level = Level::from_bytes("test.sol", &b.build())
        .unwrap_or_else(|error| panic!("Invalid game test fixture: {error}"));
    Game::new(level, false)
}

#[test]
fn a_pickup_opens_the_goal_then_the_next_update_can_finish_the_run() {
    let mut b = stage("9000", "10");
    b.items.push(([0.0, 0.6, 0.0], 1, 10));
    b.goals.push(([0.0; 3], 1.0));
    let mut g = start(b);
    assert!(!g.goal_open());
    let first = g.tick(&Controls::default());
    assert_eq!(first.pickup.map(|i| i.value), Some(10));
    assert!(first.opened_goal);
    assert_eq!(g.coins(), 10);
    assert_eq!(g.outcome(), None);
    let second = g.tick(&Controls::default());
    assert_eq!(second.outcome, Some(Outcome::Goal));
    assert_eq!(second.pickup, None);
    assert_eq!(g.tick(&Controls::default()).outcome, None);
}

#[test]
fn goal_precedes_timeout_and_timeout_precedes_a_fall_on_the_same_tick() {
    let mut b = stage("1", "0");
    b.verts[0][1] = 1.0;
    b.goals.push(([0.0; 3], 1.0));
    let g = &mut start(b.clone());
    assert_eq!(g.tick(&Controls::default()).outcome, Some(Outcome::Goal));
    b.goals.clear();
    assert_eq!(
        start(b.clone()).tick(&Controls::default()).outcome,
        Some(Outcome::Time)
    );
    b.attrs.clear();
    assert_eq!(
        start(b).tick(&Controls::default()).outcome,
        Some(Outcome::Fall)
    );
}

#[test]
fn paused_wall_time_is_not_charged_on_resume_and_zero_limit_counts_up() {
    let mut g = start(stage("0", "0"));
    let input = Controls::default();
    g.advance(TICK / 2.0, &input, |_| {});
    g.set_paused(true);
    let before = g.ball();
    g.advance(120.0, &input, |_| {
        panic!("a paused game emitted a physics tick")
    });
    assert_eq!(g.clock_centis(), 0);
    assert_eq!(g.ball(), before);
    g.set_paused(false);
    let mut ticks = 0;
    g.advance(TICK / 2.0, &input, |_| ticks += 1);
    assert_eq!((ticks, g.clock_centis()), (1, 1));
    assert!(g.ball().position[1] < before.position[1]);
    let mut long = start(stage("60000", "1"));
    for _ in 0..12 {
        long.tick(&input);
    }
    assert_eq!(long.clock_centis(), 60000);
}

#[test]
fn teleport_holds_at_entry_then_moves_the_saved_offset_once() {
    let mut b = stage("0", "0");
    b.jumps.push(([0.0; 3], [5.0, 3.0, -2.0], 1.0));
    let mut g = start(b);
    let input = Controls::default();
    assert!(g.tick(&input).jumped);
    let entry = g.ball().position;
    for _ in 0..44 {
        assert!(!g.tick(&input).jumped);
    }
    assert_eq!(g.ball().position, entry);
    for _ in 0..3 {
        g.tick(&input);
    }
    assert_eq!(g.ball().position, [5.0, 3.0 + entry[1], -2.0]);
    let exit = g.ball().position;
    for _ in 0..40 {
        assert!(!g.tick(&input).jumped);
    }
    assert_eq!(g.ball().position, exit);
}

#[test]
fn camera_rotation_changes_the_world_direction_of_the_same_forward_tilt() {
    let mut g = start(stage("0", "0"));
    let controls = Controls {
        camera: CameraMode::Manual,
        rotation: 1.0,
        ..Controls::default()
    };
    for _ in 0..90 {
        g.tick(&controls);
    }
    let cam = g.camera();
    assert!(
        cam.back[0] > 0.99 && cam.back[2].abs() < 0.01,
        "{:?}",
        cam.back
    );
    let previous = g.ball().position;
    let push = Controls {
        camera: CameraMode::Manual,
        tilt_x: 20.0,
        ..Controls::default()
    };
    for _ in 0..45 {
        g.tick(&push);
    }
    let now = g.ball().position;
    assert!(now[0] < previous[0] - 0.2 && (now[2] - previous[2]).abs() < 0.02);
}
