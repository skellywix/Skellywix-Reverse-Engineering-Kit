// SPDX-License-Identifier: GPL-2.0-or-later
//
// Behavioral cases for the ball motion of Neverball 1.6.0
// (share/solid_sim_sol.c, share/solid_all.c, share/solid_vary.c).
// Every expected value is derived by hand from the C source's formulas
// (sphere solvers, bounce factor 1.7, path interpolation, switch rules), not
// copied from the Rust implementation.

//! Integration cases for `neverball::motion`: gravity, floors, walls, edges
//! and vertices, fast balls, the BSP, moving and rotating bodies, and
//! switches. Levels are built in memory with [`Sol`] struct literals.

use std::f32::consts::FRAC_1_SQRT_2;
use std::ops::Range;

use neverball::motion::Motion;
use neverball::sol::{Ball, Body, Edge, Lump, Node, Path, Side, Sol, Switch, Vec3, Version, flag};

const DT: f32 = 1.0 / 60.0;
const NO_GRAVITY: Vec3 = [0.0; 3];
const GRAVITY: Vec3 = [0.0, -9.8, 0.0];
const IDENTITY: [f32; 4] = [1.0, 0.0, 0.0, 0.0];
/// Rotation of 90 degrees about +Y as `[w, x, y, z]`.
const QUARTER_TURN_Y: [f32; 4] = [FRAC_1_SQRT_2, 0.0, FRAC_1_SQRT_2, 0.0];

// ---------------------------------------------------------------------------
// Assertions.

fn near(actual: f32, expected: f32, tolerance: f32, what: &str) {
    assert!(
        (actual - expected).abs() <= tolerance,
        "{what}: got {actual}, expected {expected} +/- {tolerance}"
    );
}

fn near_all(actual: &[f32], expected: &[f32], tolerance: f32, what: &str) {
    assert_eq!(actual.len(), expected.len(), "{what}: length");
    for (index, (a, e)) in actual.iter().zip(expected).enumerate() {
        near(*a, *e, tolerance, &format!("{what}[{index}]"));
    }
}

// ---------------------------------------------------------------------------
// Level construction.

fn empty_sol(ball: Vec3, radius: f32) -> Sol {
    Sol {
        version: Version::Current,
        attributes: Vec::new(),
        materials: Vec::new(),
        vertices: Vec::new(),
        edges: Vec::new(),
        sides: Vec::new(),
        tex_coords: Vec::new(),
        offsets: Vec::new(),
        geoms: Vec::new(),
        lumps: Vec::new(),
        nodes: Vec::new(),
        paths: Vec::new(),
        bodies: Vec::new(),
        items: Vec::new(),
        goals: Vec::new(),
        jumps: Vec::new(),
        switches: Vec::new(),
        billboards: Vec::new(),
        balls: vec![Ball {
            position: ball,
            radius,
        }],
        views: Vec::new(),
    }
}

/// Appends a convex lump and returns its index.
fn add_convex(
    sol: &mut Sol,
    flags: u32,
    vertices: &[Vec3],
    edges: &[[usize; 2]],
    planes: &[(Vec3, f32)],
) -> usize {
    let first_vertex = sol.vertices.len();
    let first_edge = sol.edges.len();
    let first_side = sol.sides.len();

    sol.vertices.extend_from_slice(vertices);
    sol.edges.extend(edges.iter().map(|edge| Edge {
        vertices: [first_vertex + edge[0], first_vertex + edge[1]],
    }));
    sol.sides.extend(
        planes
            .iter()
            .map(|&(normal, distance)| Side { normal, distance }),
    );

    sol.lumps.push(Lump {
        flags,
        vertices: (first_vertex..first_vertex + vertices.len()).collect(),
        edges: (first_edge..first_edge + edges.len()).collect(),
        geoms: Vec::new(),
        sides: (first_side..first_side + planes.len()).collect(),
    });
    sol.lumps.len() - 1
}

/// Appends an axis-aligned box lump `[lo, hi]` and returns its index.
fn add_box(sol: &mut Sol, flags: u32, lo: Vec3, hi: Vec3) -> usize {
    let mut vertices = Vec::new();
    for i in 0..8_usize {
        vertices.push([
            if i & 1 == 0 { lo[0] } else { hi[0] },
            if i & 2 == 0 { lo[1] } else { hi[1] },
            if i & 4 == 0 { lo[2] } else { hi[2] },
        ]);
    }

    // One edge per pair of corners that differ in a single axis.
    let mut edges = Vec::new();
    for i in 0..8_usize {
        for bit in [1_usize, 2, 4] {
            if i & bit == 0 {
                edges.push([i, i | bit]);
            }
        }
    }

    let planes = [
        ([1.0, 0.0, 0.0], hi[0]),
        ([-1.0, 0.0, 0.0], -lo[0]),
        ([0.0, 1.0, 0.0], hi[1]),
        ([0.0, -1.0, 0.0], -lo[1]),
        ([0.0, 0.0, 1.0], hi[2]),
        ([0.0, 0.0, -1.0], -lo[2]),
    ];
    add_convex(sol, flags, &vertices, &edges, &planes)
}

fn add_leaf(sol: &mut Sol, lumps: Range<usize>) -> usize {
    sol.nodes.push(Node {
        side: None,
        fore: None,
        back: None,
        lumps,
    });
    sol.nodes.len() - 1
}

fn add_body(
    sol: &mut Sol,
    node: Option<usize>,
    lumps: Range<usize>,
    path: Option<usize>,
    path_alt: Option<usize>,
) -> usize {
    sol.bodies.push(Body {
        path,
        path_alt,
        node,
        lumps,
        geoms: Vec::new(),
    });
    sol.bodies.len() - 1
}

/// A static world of boxes in one leaf node on body 0.
fn static_world(boxes: &[(Vec3, Vec3)], ball: Vec3, radius: f32) -> Sol {
    let mut sol = empty_sol(ball, radius);
    for &(lo, hi) in boxes {
        add_box(&mut sol, 0, lo, hi);
    }
    let node = add_leaf(&mut sol, 0..boxes.len());
    add_body(&mut sol, Some(node), 0..boxes.len(), None, None);
    sol
}

/// A closed two-waypoint loop; each leg is `(position, orientation, seconds)`
/// for the trip from that waypoint to the other. Returns the first index.
fn add_loop(sol: &mut Sol, legs: [(Vec3, [f32; 4], f32); 2], enabled: bool, smooth: bool) -> usize {
    let first = sol.paths.len();
    for (index, (position, orientation, seconds)) in legs.into_iter().enumerate() {
        sol.paths.push(Path {
            position,
            orientation,
            time: seconds,
            time_ms: (seconds * 1000.0).round() as i32,
            next: first + 1 - index,
            enabled,
            smooth,
            flags: if orientation == IDENTITY {
                0
            } else {
                flag::PATH_ORIENTED
            },
        });
    }
    first
}

/// A platform far from the origin carried along `path`.
fn add_platform(sol: &mut Sol, lo: Vec3, hi: Vec3, path: usize, path_alt: usize) -> usize {
    let lump = add_box(sol, 0, lo, hi);
    let node = add_leaf(sol, lump..lump + 1);
    add_body(sol, Some(node), lump..lump + 1, Some(path), Some(path_alt))
}

fn run(motion: &mut Motion, sol: &Sol, gravity: Vec3, steps: usize, dt: f32) -> f32 {
    let mut peak = 0.0_f32;
    for _ in 0..steps {
        peak = peak.max(motion.step(sol, gravity, dt));
    }
    peak
}

// ---------------------------------------------------------------------------
// Construction.

#[test]
fn a_new_motion_starts_at_rest_at_the_level_start() {
    let mut sol = empty_sol([1.0, 2.0, 3.0], 0.25);
    let looped = add_loop(
        &mut sol,
        [([0.0; 3], IDENTITY, 1.0), ([1.0, 0.0, 0.0], IDENTITY, 1.0)],
        false,
        false,
    );
    add_platform(
        &mut sol,
        [40.0, -1.0, -2.0],
        [44.0, 0.0, 2.0],
        looped,
        looped,
    );
    add_body(&mut sol, None, 0..0, None, None);
    sol.switches.push(Switch {
        position: [0.0; 3],
        radius: 1.0,
        path: Some(looped),
        timer: 0.0,
        timer_ms: 0,
        default_state: true,
        invisible: false,
    });

    let motion = Motion::new(&sol);
    assert_eq!(motion.ball, sol.balls[0]);
    assert_eq!(motion.velocity, [0.0; 3]);
    assert_eq!(motion.angular_velocity, [0.0; 3]);
    assert_eq!(
        motion.basis,
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    );
    assert_eq!(motion.path_enabled(0), Some(false));
    assert_eq!(motion.path_enabled(2), None);
    assert_eq!(motion.switch_on(0), Some(true));
    assert_eq!(motion.switch_on(1), None);

    // A body without a path sits at the origin with the identity orientation.
    assert_eq!(motion.body_pose(&sol, 1), ([0.0; 3], IDENTITY));
}

// ---------------------------------------------------------------------------
// Gravity and static geometry.

#[test]
fn free_fall_adds_gravity_to_the_velocity_then_moves_the_ball() {
    let sol = empty_sol([0.0, 10.0, 0.0], 0.25);
    let mut motion = Motion::new(&sol);

    let energy = motion.step(&sol, GRAVITY, 0.1);

    // v = g * dt, then p = p + v * dt (semi-implicit Euler).
    assert_eq!(energy, 0.0);
    near_all(&motion.velocity, &[0.0, -0.98, 0.0], 1e-5, "velocity");
    near_all(&motion.ball.position, &[0.0, 9.902, 0.0], 1e-5, "position");
}

#[test]
fn a_floor_bounce_keeps_70_percent_of_the_impact_speed_and_reports_it() {
    let sol = static_world(
        &[([-10.0, -1.0, -10.0], [10.0, 0.0, 10.0])],
        [0.0, 0.75, 0.0],
        0.5,
    );
    let mut motion = Motion::new(&sol);
    motion.velocity = [0.0, -5.0, 0.0];

    let energy = motion.step(&sol, NO_GRAVITY, 0.1);

    // Contact after 0.05 s, with the ball center at the radius; the
    // velocity into the plane becomes -0.7 of what it was (1.7 factor).
    near(energy, 5.0, 1e-4, "energy");
    near_all(&motion.velocity, &[0.0, 3.5, 0.0], 1e-4, "velocity");
    near_all(&motion.ball.position, &[0.0, 0.675, 0.0], 1e-4, "position");
}

#[test]
fn a_ball_already_touching_the_floor_bounces_at_once() {
    let sol = static_world(
        &[([-10.0, -1.0, -10.0], [10.0, 0.0, 10.0])],
        [0.0, 0.5, 0.0],
        0.5,
    );
    let mut motion = Motion::new(&sol);
    motion.velocity = [0.0, -5.0, 0.0];

    let energy = motion.step(&sol, NO_GRAVITY, 0.1);

    near(energy, 5.0, 1e-4, "energy");
    near_all(&motion.velocity, &[0.0, 3.5, 0.0], 1e-4, "velocity");
    near_all(&motion.ball.position, &[0.0, 0.85, 0.0], 1e-4, "position");
}

#[test]
fn a_ball_in_contact_with_a_wall_is_set_back_to_the_surface() {
    // Wall face at x = 1, ball approaching from -x; the glancing component
    // along the wall is untouched.
    let sol = static_world(
        &[([1.0, -5.0, -5.0], [2.0, 5.0, 5.0])],
        [0.0, 0.0, 0.0],
        0.25,
    );
    let mut motion = Motion::new(&sol);
    motion.velocity = [4.0, 1.0, -2.0];

    let energy = motion.step(&sol, NO_GRAVITY, 0.5);

    // Contact at t = (1 - 0.25) / 4 = 0.1875; vx becomes 4 - 1.7 * 4 = -2.8.
    near(energy, 4.0, 1e-4, "energy");
    near_all(&motion.velocity, &[-2.8, 1.0, -2.0], 1e-4, "velocity");
    // 0.3125 s remain after the contact at x = 0.75.
    near_all(
        &motion.ball.position,
        &[0.75 - 2.8 * 0.3125, 0.5, -1.0],
        1e-4,
        "position",
    );
}

#[test]
fn a_very_fast_ball_does_not_tunnel_through_a_thin_floor() {
    let sol = static_world(
        &[([-10.0, -0.01, -10.0], [10.0, 0.0, 10.0])],
        [0.0, 5.0, 0.0],
        0.25,
    );
    let mut motion = Motion::new(&sol);
    motion.velocity = [0.0, -600.0, 0.0]; // 10 units in one frame

    let energy = motion.step(&sol, NO_GRAVITY, DT);

    near(energy, 600.0, 0.05, "energy");
    near(motion.velocity[1], 420.0, 0.05, "rebound velocity");
    // Contact at 4.75 / 600 s, then 0.00875 s of rebound from y = 0.25.
    near(motion.ball.position[1], 3.925, 0.01, "height");
    assert!(motion.ball.position[1] > 0.25);
}

#[test]
fn a_very_fast_ball_does_not_tunnel_through_a_thin_wall() {
    let sol = static_world(
        &[([0.0, -5.0, -5.0], [0.01, 5.0, 5.0])],
        [-3.0, 0.0, 0.0],
        0.25,
    );
    let mut motion = Motion::new(&sol);
    motion.velocity = [900.0, 0.0, 0.0]; // 15 units in one frame

    let energy = motion.step(&sol, NO_GRAVITY, DT);

    near(energy, 900.0, 0.1, "energy");
    near(motion.velocity[0], -630.0, 0.1, "rebound velocity");
    near(motion.ball.position[0], -8.825, 0.01, "position");
    assert!(motion.ball.position[0] < 0.0);
}

#[test]
fn an_edge_hit_bounces_along_the_line_from_the_edge_to_the_ball() {
    // The ball passes above the box's front face, so the face plane is
    // rejected (the contact point is above the top plane) and the edge along
    // z at (x = 0, y = 0) is what it hits.
    let sol = static_world(
        &[([0.0, -1.0, -1.0], [1.0, 0.0, 1.0])],
        [-1.0, 0.1, 0.0],
        0.25,
    );
    let mut motion = Motion::new(&sol);
    motion.velocity = [10.0, 0.0, 0.0];

    let energy = motion.step(&sol, NO_GRAVITY, 0.2);

    // (10 t - 1)^2 + 0.01 = 0.0625 gives the contact at t = 0.0770871 with
    // the ball at x = -0.2291288; the normal is (-0.9165151, 0.4, 0).
    let nx = -0.916_515_1_f32;
    let ny = 0.4_f32;
    let speed_in = 10.0 * nx.abs();
    near(energy, speed_in, 1e-3, "energy");
    near_all(
        &motion.velocity,
        &[10.0 + nx * 1.7 * speed_in, ny * 1.7 * speed_in, 0.0],
        2e-3,
        "velocity",
    );
    // 0.1229129 s of flight remain after the bounce.
    near_all(
        &motion.ball.position,
        &[-0.755_19, 0.866_04, 0.0],
        5e-3,
        "position",
    );
}

#[test]
fn a_vertex_hit_bounces_along_the_line_from_the_vertex_to_the_ball() {
    // Flying along +z over the top edge of the box's front corner at
    // (0, 0, 0): the face and every edge are rejected, the vertex is hit.
    let sol = static_world(
        &[([0.0, -1.0, 0.0], [1.0, 0.0, 1.0])],
        [0.0, 0.1, -2.0],
        0.25,
    );
    let mut motion = Motion::new(&sol);
    motion.velocity = [0.0, 0.0, 8.0];

    let energy = motion.step(&sol, NO_GRAVITY, 0.5);

    // |(0, 0.1, -2 + 8 t)| = 0.25 gives t = 0.221359 at z = -0.2291288.
    let nz = -0.916_515_1_f32;
    let ny = 0.4_f32;
    let speed_in = 8.0 * nz.abs();
    near(energy, speed_in, 1e-3, "energy");
    near_all(
        &motion.velocity,
        &[0.0, ny * 1.7 * speed_in, 8.0 - nz.abs() * 1.7 * speed_in],
        2e-3,
        "velocity",
    );
    // 0.278641 s of flight remain.
    near_all(
        &motion.ball.position,
        &[0.0, 1.489_26, -1.183_19],
        5e-3,
        "position",
    );
}

#[test]
fn detail_lumps_are_never_collided_with() {
    let mut sol = empty_sol([0.0, 0.75, 0.0], 0.5);
    let floor = add_box(
        &mut sol,
        flag::LUMP_DETAIL,
        [-10.0, -1.0, -10.0],
        [10.0, 0.0, 10.0],
    );
    let node = add_leaf(&mut sol, floor..floor + 1);
    add_body(&mut sol, Some(node), floor..floor + 1, None, None);

    let mut motion = Motion::new(&sol);
    motion.velocity = [0.0, -5.0, 0.0];
    let energy = motion.step(&sol, NO_GRAVITY, 0.5);

    assert_eq!(energy, 0.0);
    near_all(&motion.ball.position, &[0.0, -1.75, 0.0], 1e-4, "position");
    near_all(&motion.velocity, &[0.0, -5.0, 0.0], 1e-6, "velocity");
}

#[test]
fn a_ball_rolls_down_a_slope_without_entering_it() {
    // The solid region is x + y <= 0 inside a box-shaped wedge; the surface
    // normal is (1, 1, 0) / sqrt 2 and downhill is toward +x.
    let s = FRAC_1_SQRT_2;
    let radius = 0.25;
    let mut sol = empty_sol([0.1768, 0.2068, 0.0], radius);
    let vertices = [
        [-10.0, -10.0, -5.0],
        [-10.0, -10.0, 5.0],
        [-10.0, 10.0, -5.0],
        [-10.0, 10.0, 5.0],
        [10.0, -10.0, -5.0],
        [10.0, -10.0, 5.0],
    ];
    let edges = [
        [0, 2],
        [2, 4],
        [4, 0],
        [1, 3],
        [3, 5],
        [5, 1],
        [0, 1],
        [2, 3],
        [4, 5],
    ];
    let planes = [
        ([s, s, 0.0], 0.0),
        ([-1.0, 0.0, 0.0], 10.0),
        ([0.0, -1.0, 0.0], 10.0),
        ([0.0, 0.0, 1.0], 5.0),
        ([0.0, 0.0, -1.0], 5.0),
    ];
    let lump = add_convex(&mut sol, 0, &vertices, &edges, &planes);
    let node = add_leaf(&mut sol, lump..lump + 1);
    add_body(&mut sol, Some(node), lump..lump + 1, None, None);

    let mut motion = Motion::new(&sol);
    for _ in 0..90 {
        motion.step(&sol, GRAVITY, DT);

        let [x, y, z] = motion.ball.position;
        let height = s * (x + y);
        assert!(
            height >= radius - 1e-3,
            "ball sank into the slope: {height}"
        );
        assert!(z.abs() < 1e-3, "ball left the slope plane: z = {z}");
    }

    // Frictionless sliding: x grows as g * sin(45 deg) * cos(45 deg) * t^2 / 2
    // = 2.45 t^2, i.e. about 5.5 after 1.5 s.
    let [x, y, _] = motion.ball.position;
    assert!(x > 4.0 && x < 7.0, "x = {x}");
    assert!(
        s * (x + y) < radius + 0.05,
        "ball left the slope: {}",
        s * (x + y)
    );

    // Rolling turns the ball: the basis is no longer the identity and is
    // still orthonormal.
    assert_ne!(motion.basis[0], [1.0, 0.0, 0.0]);
    for (i, a) in motion.basis.iter().enumerate() {
        near(
            a.iter().map(|c| c * c).sum::<f32>(),
            1.0,
            1e-3,
            "basis length",
        );
        for b in &motion.basis[i + 1..] {
            let dot: f32 = a.iter().zip(b).map(|(p, q)| p * q).sum();
            near(dot, 0.0, 1e-3, "basis dot");
        }
    }
}

// ---------------------------------------------------------------------------
// The BSP.

/// Root node splitting at the plane x = 0 with one box in each child.
fn bsp_scene(fore: (Vec3, Vec3), back: (Vec3, Vec3), ball: Vec3) -> Sol {
    let mut sol = empty_sol(ball, 0.5);
    sol.sides.push(Side {
        normal: [1.0, 0.0, 0.0],
        distance: 0.0,
    });
    let fore_lump = add_box(&mut sol, 0, fore.0, fore.1);
    let back_lump = add_box(&mut sol, 0, back.0, back.1);

    sol.nodes.push(Node {
        side: Some(0),
        fore: Some(1),
        back: Some(2),
        lumps: 0..0,
    });
    add_leaf(&mut sol, fore_lump..fore_lump + 1);
    add_leaf(&mut sol, back_lump..back_lump + 1);
    add_body(&mut sol, Some(0), 0..2, None, None);
    sol
}

fn drop_ball(sol: &Sol) -> (Motion, f32) {
    let mut motion = Motion::new(sol);
    motion.velocity = [0.0, -5.0, 0.0];
    let energy = motion.step(sol, NO_GRAVITY, 0.1);
    (motion, energy)
}

const FORE_FLOOR: (Vec3, Vec3) = ([1.0, -1.0, -5.0], [10.0, 0.0, 5.0]);
const BACK_FLOOR: (Vec3, Vec3) = ([-10.0, -1.0, -5.0], [-1.0, 0.0, 5.0]);

#[test]
fn the_bsp_finds_lumps_in_the_fore_child() {
    let sol = bsp_scene(FORE_FLOOR, BACK_FLOOR, [2.0, 0.75, 0.0]);
    let (motion, energy) = drop_ball(&sol);
    near(energy, 5.0, 1e-4, "energy");
    near(motion.velocity[1], 3.5, 1e-4, "velocity");
}

#[test]
fn the_bsp_finds_lumps_in_the_back_child() {
    let sol = bsp_scene(FORE_FLOOR, BACK_FLOOR, [-2.0, 0.75, 0.0]);
    let (motion, energy) = drop_ball(&sol);
    near(energy, 5.0, 1e-4, "energy");
    near(motion.velocity[1], 3.5, 1e-4, "velocity");
}

#[test]
fn a_ball_straddling_the_splitting_plane_tests_both_children() {
    // The floor in the back child reaches past the plane under the ball,
    // which overlaps the plane by its radius.
    let sol = bsp_scene(
        ([20.0, -1.0, -5.0], [30.0, 0.0, 5.0]),
        ([-10.0, -1.0, -5.0], [0.5, 0.0, 5.0]),
        [0.1, 0.75, 0.0],
    );
    let (motion, energy) = drop_ball(&sol);
    near(energy, 5.0, 1e-4, "energy");
    near(motion.velocity[1], 3.5, 1e-4, "velocity");
}

#[test]
fn a_lump_in_a_child_the_ball_cannot_reach_is_culled() {
    // Misplaced geometry (the back child holds a floor entirely in front of
    // the plane) shows the walk obeys sol_test_back: the ball is wholly in
    // front for the whole step, so the back child is never entered.
    let sol = bsp_scene(
        ([20.0, -1.0, -5.0], [30.0, 0.0, 5.0]),
        ([1.0, -1.0, -5.0], [3.0, 0.0, 5.0]),
        [2.0, 0.75, 0.0],
    );
    let (motion, energy) = drop_ball(&sol);
    assert_eq!(energy, 0.0);
    near(motion.ball.position[1], 0.25, 1e-4, "height");
}

// ---------------------------------------------------------------------------
// Moving and rotating bodies.

#[test]
fn a_rising_platform_carries_the_ball_and_never_lets_it_sink() {
    let mut sol = empty_sol([0.0, 0.3, 0.0], 0.25);
    let up = add_loop(
        &mut sol,
        [([0.0; 3], IDENTITY, 2.0), ([0.0, 4.0, 0.0], IDENTITY, 2.0)],
        true,
        false,
    );
    let body = add_platform(&mut sol, [-2.0, -1.0, -2.0], [2.0, 0.0, 2.0], up, up);

    let mut motion = Motion::new(&sol);
    for step in 0..60 {
        motion.step(&sol, GRAVITY, DT);

        // The platform's top face is its origin's height (box top y = 0).
        let (position, _) = motion.body_pose(&sol, body);
        let top = position[1];
        let y = motion.ball.position[1];
        assert!(
            y >= top + 0.25 - 1e-3,
            "step {step}: ball {y} under top {top}"
        );
        assert!(
            y <= top + 0.25 + 0.3,
            "step {step}: ball {y} thrown off top {top}"
        );
    }

    // 2 units per second for one second.
    let (position, orientation) = motion.body_pose(&sol, body);
    near_all(&position, &[0.0, 2.0, 0.0], 0.02, "platform position");
    assert_eq!(orientation, IDENTITY);
    assert!(motion.ball.position[1] > 2.2);
}

#[test]
fn a_disabled_path_leaves_the_body_where_it_is() {
    let mut sol = empty_sol([0.0, 100.0, 0.0], 0.25);
    let lift = add_loop(
        &mut sol,
        [([0.0; 3], IDENTITY, 1.0), ([0.0, 4.0, 0.0], IDENTITY, 1.0)],
        false,
        false,
    );
    let body = add_platform(&mut sol, [40.0, -1.0, -2.0], [44.0, 0.0, 2.0], lift, lift);

    let mut motion = Motion::new(&sol);
    run(&mut motion, &sol, NO_GRAVITY, 120, DT);
    assert_eq!(motion.body_pose(&sol, body).0, [0.0; 3]);
}

#[test]
fn a_body_keeps_moving_across_path_boundaries_within_one_large_step() {
    // Out in 1 s to x = 10, back in 1 s: halfway back after 1.5 s.
    let mut sol = empty_sol([0.0, 100.0, 0.0], 0.25);
    let there_and_back = add_loop(
        &mut sol,
        [([0.0; 3], IDENTITY, 1.0), ([10.0, 0.0, 0.0], IDENTITY, 1.0)],
        true,
        false,
    );
    let body = add_platform(
        &mut sol,
        [40.0, -1.0, -2.0],
        [44.0, 0.0, 2.0],
        there_and_back,
        there_and_back,
    );

    let mut whole = Motion::new(&sol);
    whole.step(&sol, NO_GRAVITY, 1.5);
    near(
        whole.body_pose(&sol, body).0[0],
        5.0,
        0.05,
        "one 1.5 s step",
    );

    let mut frames = Motion::new(&sol);
    run(&mut frames, &sol, NO_GRAVITY, 90, DT);
    near(frames.body_pose(&sol, body).0[0], 5.0, 0.1, "ninety frames");
}

#[test]
fn a_smooth_path_eases_with_the_smoothstep_curve() {
    // erp(0.1) = 3 * 0.01 - 2 * 0.001 = 0.028 of 10 units after 1 s of 10.
    let mut sol = empty_sol([0.0, 100.0, 0.0], 0.25);
    let ease = add_loop(
        &mut sol,
        [
            ([0.0; 3], IDENTITY, 10.0),
            ([10.0, 0.0, 0.0], IDENTITY, 10.0),
        ],
        true,
        true,
    );
    let body = add_platform(&mut sol, [40.0, -1.0, -2.0], [44.0, 0.0, 2.0], ease, ease);

    let mut motion = Motion::new(&sol);
    run(&mut motion, &sol, NO_GRAVITY, 60, DT);
    near(
        motion.body_pose(&sol, body).0[0],
        0.28,
        0.01,
        "eased position",
    );
}

#[test]
fn an_oriented_path_interpolates_the_orientation_by_slerp() {
    // 90 degrees about Y over 2 s: after 1 s it is 45 degrees,
    // (cos 22.5, 0, sin 22.5, 0) = (0.9238795, 0, 0.3826834, 0).
    let mut sol = empty_sol([0.0, 100.0, 0.0], 0.25);
    let turn = add_loop(
        &mut sol,
        [([0.0; 3], IDENTITY, 2.0), ([0.0; 3], QUARTER_TURN_Y, 2.0)],
        true,
        false,
    );
    let body = add_platform(&mut sol, [40.0, -1.0, -2.0], [44.0, 0.0, 2.0], turn, turn);

    let mut motion = Motion::new(&sol);
    run(&mut motion, &sol, NO_GRAVITY, 60, DT);
    let (position, orientation) = motion.body_pose(&sol, body);
    near_all(&position, &[0.0; 3], 1e-6, "position");
    near_all(
        &orientation,
        &[0.923_879_5, 0.0, 0.382_683_4, 0.0],
        2e-3,
        "orientation",
    );
}

#[test]
fn a_second_path_drives_the_orientation_independently() {
    // Position: 10 units in 1 s. Orientation: 90 degrees in 2 s. After 0.5 s
    // the body is at x = 5 turned by 22.5 degrees: (cos 11.25, 0, sin 11.25, 0).
    let mut sol = empty_sol([0.0, 100.0, 0.0], 0.25);
    let position_path = add_loop(
        &mut sol,
        [([0.0; 3], IDENTITY, 1.0), ([10.0, 0.0, 0.0], IDENTITY, 1.0)],
        true,
        false,
    );
    let turn_path = add_loop(
        &mut sol,
        [([0.0; 3], IDENTITY, 2.0), ([0.0; 3], QUARTER_TURN_Y, 2.0)],
        true,
        false,
    );
    let body = add_platform(
        &mut sol,
        [40.0, -1.0, -2.0],
        [44.0, 0.0, 2.0],
        position_path,
        turn_path,
    );

    let mut motion = Motion::new(&sol);
    run(&mut motion, &sol, NO_GRAVITY, 30, DT);
    let (position, orientation) = motion.body_pose(&sol, body);
    near_all(&position, &[5.0, 0.0, 0.0], 0.02, "position");
    near_all(
        &orientation,
        &[0.980_785_3, 0.0, 0.195_090_3, 0.0],
        2e-3,
        "orientation",
    );
}

#[test]
fn a_rotating_platform_carries_a_resting_ball_without_pushing_it_sideways() {
    // The platform turns about the vertical axis through the ball.
    let mut sol = empty_sol([0.0, 0.3, 0.0], 0.25);
    let spin = add_loop(
        &mut sol,
        [([0.0; 3], IDENTITY, 4.0), ([0.0; 3], QUARTER_TURN_Y, 4.0)],
        true,
        false,
    );
    let body = add_platform(&mut sol, [-2.0, -0.5, -2.0], [2.0, 0.0, 2.0], spin, spin);

    let mut motion = Motion::new(&sol);
    for step in 0..120 {
        motion.step(&sol, GRAVITY, DT);

        let [x, y, z] = motion.ball.position;
        assert!(y >= 0.25 - 1e-3, "step {step}: ball sank to {y}");
        assert!(y <= 0.25 + 0.1, "step {step}: ball rose to {y}");
        assert!(
            x.abs() < 0.05 && z.abs() < 0.05,
            "step {step}: drift ({x}, {z})"
        );
    }

    // Halfway through the 90 degree turn.
    let (_, orientation) = motion.body_pose(&sol, body);
    near_all(
        &orientation,
        &[0.923_879_5, 0.0, 0.382_683_4, 0.0],
        2e-3,
        "orientation",
    );
}

// ---------------------------------------------------------------------------
// Switches.

/// A switch at the origin (radius 1) driving a platform's loop path that
/// starts disabled. The ball starts inside the switch's volume.
fn switch_scene(timer_seconds: f32, invisible: bool) -> Sol {
    let mut sol = empty_sol([0.0, 0.5, 0.0], 0.25);
    let lift = add_loop(
        &mut sol,
        [([0.0; 3], IDENTITY, 4.0), ([0.0, 8.0, 0.0], IDENTITY, 4.0)],
        false,
        false,
    );
    add_platform(&mut sol, [40.0, -1.0, -2.0], [44.0, 0.0, 2.0], lift, lift);
    sol.switches.push(Switch {
        position: [0.0; 3],
        radius: 1.0,
        path: Some(lift),
        timer: timer_seconds,
        timer_ms: (timer_seconds * 1000.0).round() as i32,
        default_state: false,
        invisible,
    });
    sol
}

#[test]
fn a_switch_toggles_once_per_entry_and_enables_its_whole_path_loop() {
    let sol = switch_scene(0.0, false);
    let mut motion = Motion::new(&sol);
    assert_eq!(motion.path_enabled(0), Some(false));
    assert_eq!(motion.path_enabled(1), Some(false));

    assert!(motion.touch_switches(&sol), "entering a visible switch");
    assert_eq!(motion.switch_on(0), Some(true));
    assert_eq!(motion.path_enabled(0), Some(true));
    assert_eq!(motion.path_enabled(1), Some(true));

    // Edge-triggered: staying inside does nothing.
    assert!(!motion.touch_switches(&sol));
    assert_eq!(motion.switch_on(0), Some(true));

    // Leaving re-arms it; entering again toggles it back off.
    motion.ball.position = [5.0, 0.5, 0.0];
    assert!(!motion.touch_switches(&sol));
    assert_eq!(motion.switch_on(0), Some(true));

    motion.ball.position = [0.0, 0.5, 0.0];
    assert!(motion.touch_switches(&sol));
    assert_eq!(motion.switch_on(0), Some(false));
    assert_eq!(motion.path_enabled(0), Some(false));
    assert_eq!(motion.path_enabled(1), Some(false));
}

#[test]
fn a_switch_needs_the_ball_in_the_halo_and_inside_the_height_band() {
    let sol = switch_scene(0.0, false);
    let mut motion = Motion::new(&sol);

    // Below, at and above the band (0 < y < SWCH_HEIGHT / 2 = 1).
    for y in [-0.1, 0.0, 1.0, 1.5] {
        motion.ball.position = [0.0, y, 0.0];
        assert!(!motion.touch_switches(&sol), "y = {y}");
        assert_eq!(motion.switch_on(0), Some(false), "y = {y}");
    }

    // d = |x| + r - R: 0.8 + 0.25 - 1 > 0 stays outside; 0.7 gives -0.05.
    motion.ball.position = [0.8, 0.5, 0.0];
    assert!(!motion.touch_switches(&sol));
    assert_eq!(motion.switch_on(0), Some(false));

    motion.ball.position = [0.7, 0.5, 0.0];
    assert!(motion.touch_switches(&sol));
    assert_eq!(motion.switch_on(0), Some(true));
}

#[test]
fn an_invisible_switch_toggles_without_reporting_the_entry() {
    let sol = switch_scene(0.0, true);
    let mut motion = Motion::new(&sol);

    assert!(!motion.touch_switches(&sol));
    assert_eq!(motion.switch_on(0), Some(true));
    assert_eq!(motion.path_enabled(0), Some(true));
}

#[test]
fn a_timed_switch_returns_to_its_default_after_its_timer_and_retriggers() {
    // One second: still on after 58 frames (about 967 ms), off after 62
    // frames (about 1033 ms).
    let sol = switch_scene(1.0, false);
    let mut motion = Motion::new(&sol);

    assert!(motion.touch_switches(&sol));
    assert_eq!(motion.switch_on(0), Some(true));
    assert_eq!(motion.path_enabled(0), Some(true));

    for frame in 0..58 {
        assert_eq!(motion.step(&sol, NO_GRAVITY, DT), 0.0);
        assert_eq!(motion.switch_on(0), Some(true), "frame {frame}");
        // A timed switch in its non-default state ignores the ball.
        assert!(!motion.touch_switches(&sol), "frame {frame}");
    }
    assert_eq!(motion.path_enabled(1), Some(true));

    for _ in 0..4 {
        motion.step(&sol, NO_GRAVITY, DT);
    }
    assert_eq!(motion.switch_on(0), Some(false));
    assert_eq!(motion.path_enabled(0), Some(false));
    assert_eq!(motion.path_enabled(1), Some(false));

    // The ball never left, yet a timed switch has no enter/exit state: it
    // triggers again at once.
    assert!(motion.touch_switches(&sol));
    assert_eq!(motion.switch_on(0), Some(true));
    assert_eq!(motion.path_enabled(0), Some(true));
}

#[test]
fn a_switch_sets_a_stopped_platform_moving() {
    let sol = switch_scene(0.0, false);
    let mut motion = Motion::new(&sol);

    // The loop is disabled: one second of frames moves nothing.
    run(&mut motion, &sol, NO_GRAVITY, 60, DT);
    assert_eq!(motion.body_pose(&sol, 0).0, [0.0; 3]);

    assert!(motion.touch_switches(&sol));

    // 8 units in 4 s: 2 units after one more second.
    run(&mut motion, &sol, NO_GRAVITY, 60, DT);
    near_all(
        &motion.body_pose(&sol, 0).0,
        &[0.0, 2.0, 0.0],
        0.02,
        "platform",
    );
}
