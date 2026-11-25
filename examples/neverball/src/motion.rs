// SPDX-License-Identifier: GPL-2.0-or-later
//
// Rust port of the interactive ball motion of Neverball 1.6.0
// (share/solid_sim_sol.c, share/solid_all.c, share/solid_vary.c, share/vec3.c,
// and the way ball/game_server.c drives them). Original: Copyright (C) 2003
// Robert Kooima and the Neverball authors. See NOTICE.md and COPYING.

//! Ball motion: the physics step, moving bodies and switches.
//!
//! [`Motion`] owns everything `struct s_vary` owns for the one ball the game
//! simulates (`uv[0]`): the ball itself, the enabled flag of every path, the
//! movers that carry bodies along their paths, the switch states and the
//! millisecond clock. The level data stays in the borrowed [`Sol`]; nothing
//! here renders or reads a file.
//!
//! Every routine follows the 1.6.0 source operation for operation, in single
//! precision and in the same order, so a given level, gravity and time step
//! produce the same trajectory as the original on a machine that evaluates
//! `float` arithmetic in single precision. The sphere, edge and vertex
//! solvers, the BSP walk, the 16-iteration collision loop, the clamping of a
//! step at path boundaries, the rotating-body trick (testing the ball moving
//! in the body's frame), the bounce factor `1.7` and the ball pendulum are all
//! ported.
//!
//! # Differences from the C code
//!
//! * **Friction branch.** `sol_step` has an optional `m` argument that turns
//!   on a rolling-friction branch; `game_server.c` always passes `NULL`, so
//!   the branch is not ported and [`Motion::step`] has no such argument.
//! * **Commands.** The `cmd_fn` event stream (`CMD_STEP_SIMULATION`,
//!   `CMD_SWCH_TOGGLE`, ...) feeds replays and the client. It is not ported;
//!   the state it describes is readable from [`Motion`] instead.
//! * **`sol_move`.** The server never calls it, so it is not ported.
//! * **Booleans.** The SOL loader stores path and switch flags as `bool`.
//!   The C code keeps the raw integer, so a level whose flag is a nonzero
//!   value other than `1` could differ; the shipped levels use `0` and `1`.
//! * **Defined behavior for malformed levels.** A body without a BSP root is
//!   skipped (C reads `nv[-1]`), and BSP children of a node without a splitting
//!   side are ignored (C reads `sv[-1]`). [`Sol::from_bytes`] accepts both.
//! * **Trigonometry.** `sin`, `cos` and `acos` go through `f64`, as the C
//!   macros do, using Rust's `f64` functions. Their last bit can differ from a
//!   given C runtime's `libm`.
//! * **Stack.** The BSP walk is iterative with one reusable buffer, so a deep
//!   tree cannot overflow the stack and a step does not allocate.
//! * **Clock guard.** The millisecond accumulator stops subtracting when the
//!   subtraction no longer changes it (an infinite or astronomically large
//!   time step); C would spin forever. `dt` must still be finite and small
//!   (a frame) for the result to mean anything.

use crate::sol::{Ball, Sol, Vec3, flag};

/// "No hit" time (`LARGE`).
const LARGE: f32 = 1.0e+5;
/// One millisecond, the unit of the integer clock.
const MILLISECOND: f32 = 0.001;
/// Collision iterations per step before the last one is allowed to miss.
const MAX_ITERATIONS: u32 = 16;
/// `SWCH_HEIGHT` of `share/geom.h`.
const SWITCH_HEIGHT: f32 = 2.0;

// ---------------------------------------------------------------------------
// Vector, quaternion and matrix helpers (share/vec3.h, share/vec3.c).

type Quat = [f32; 4];

fn sqrt32(x: f32) -> f32 {
    f64::from(x).sqrt() as f32
}

fn sin32(x: f32) -> f32 {
    f64::from(x).sin() as f32
}

fn cos32(x: f32) -> f32 {
    f64::from(x).cos() as f32
}

fn acos32(x: f32) -> f32 {
    f64::from(x).acos() as f32
}

fn dot(u: Vec3, v: Vec3) -> f32 {
    u[0] * v[0] + u[1] * v[1] + u[2] * v[2]
}

fn len(u: Vec3) -> f32 {
    sqrt32(dot(u, u))
}

fn add(u: Vec3, v: Vec3) -> Vec3 {
    [u[0] + v[0], u[1] + v[1], u[2] + v[2]]
}

fn sub(u: Vec3, v: Vec3) -> Vec3 {
    [u[0] - v[0], u[1] - v[1], u[2] - v[2]]
}

fn scl(v: Vec3, k: f32) -> Vec3 {
    [v[0] * k, v[1] * k, v[2] * k]
}

/// `v_mad`: `p + v * t`.
fn mad(p: Vec3, v: Vec3, t: f32) -> Vec3 {
    [p[0] + v[0] * t, p[1] + v[1] * t, p[2] + v[2] * t]
}

fn nrm(v: Vec3) -> Vec3 {
    let d = len(v);
    if d == 0.0 {
        [0.0; 3]
    } else {
        [v[0] / d, v[1] / d, v[2] / d]
    }
}

fn crs(v: Vec3, w: Vec3) -> Vec3 {
    [
        v[1] * w[2] - v[2] * w[1],
        v[2] * w[0] - v[0] * w[2],
        v[0] * w[1] - v[1] * w[0],
    ]
}

fn q_dot(q: Quat, r: Quat) -> f32 {
    // `q[0] * r[0] + v_dot(q + 1, r + 1)`: the vector part is summed first.
    q[0] * r[0] + (q[1] * r[1] + q[2] * r[2] + q[3] * r[3])
}

fn q_nrm(r: Quat) -> Quat {
    let d = sqrt32(q_dot(r, r));
    if d == 0.0 {
        [1.0, 0.0, 0.0, 0.0]
    } else {
        [r[0] / d, r[1] / d, r[2] / d, r[3] / d]
    }
}

fn q_conj(r: Quat) -> Quat {
    [r[0], -r[1], -r[2], -r[3]]
}

fn q_mul(a: Quat, b: Quat) -> Quat {
    [
        a[0] * b[0] - a[1] * b[1] - a[2] * b[2] - a[3] * b[3],
        a[0] * b[1] + a[1] * b[0] + a[2] * b[3] - a[3] * b[2],
        a[0] * b[2] - a[1] * b[3] + a[2] * b[0] + a[3] * b[1],
        a[0] * b[3] + a[1] * b[2] - a[2] * b[1] + a[3] * b[0],
    ]
}

/// Rotates `w` by the quaternion `r` (`q_rot`).
fn q_rot(r: Quat, w: Vec3) -> Vec3 {
    let b = q_mul(r, [0.0, w[0], w[1], w[2]]);
    let c = q_mul(b, q_conj(r));
    [c[1], c[2], c[3]]
}

/// Spherical linear interpolation (`q_slerp`).
fn q_slerp(a: Quat, b: Quat, t: f32) -> Quat {
    /// `(float) TINY`.
    const TINY: f32 = 1e-5;

    if t <= 0.0 {
        return a;
    }
    if 1.0 <= t {
        return b;
    }

    let mut c = q_dot(a, b);
    let mut sign = 1.0_f32;

    // Ensure the shortest path.
    if c < 0.0 {
        c = -c;
        sign = -1.0;
    }

    let (u, v) = if 1.0 - c < TINY {
        // Nearly equal orientations: normalized lerp.
        (1.0 - t, t)
    } else {
        let r = acos32(c);
        let s = sin32(r);
        (sin32((1.0 - t) * r) / s, sin32(t * r) / s * sign)
    };

    q_nrm([
        a[0] * u + b[0] * v,
        a[1] * u + b[1] * v,
        a[2] * u + b[2] * v,
        a[3] * u + b[3] * v,
    ])
}

/// The rotation by angle `a` about axis `v` as a column-major 4x4 matrix
/// (`m_rot`); only the upper 3x3 block is filled.
fn m_rot(v: Vec3, a: f32) -> [f32; 16] {
    let s = sin32(a);
    let c = cos32(a);
    let u = nrm(v);

    let mut uu = [0.0_f32; 16];
    uu[0] = u[0] * u[0];
    uu[4] = u[0] * u[1];
    uu[8] = u[0] * u[2];
    uu[1] = u[1] * u[0];
    uu[5] = u[1] * u[1];
    uu[9] = u[1] * u[2];
    uu[2] = u[2] * u[0];
    uu[6] = u[2] * u[1];
    uu[10] = u[2] * u[2];

    let mut ss = [0.0_f32; 16];
    ss[4] = -u[2];
    ss[8] = u[1];
    ss[1] = u[2];
    ss[9] = -u[0];
    ss[2] = -u[1];
    ss[6] = u[0];

    let mut m = [0.0_f32; 16];
    m[0] = uu[0] + c * (1.0 - uu[0]) + s * ss[0];
    m[1] = uu[1] + c * (0.0 - uu[1]) + s * ss[1];
    m[2] = uu[2] + c * (0.0 - uu[2]) + s * ss[2];
    m[4] = uu[4] + c * (0.0 - uu[4]) + s * ss[4];
    m[5] = uu[5] + c * (1.0 - uu[5]) + s * ss[5];
    m[6] = uu[6] + c * (0.0 - uu[6]) + s * ss[6];
    m[8] = uu[8] + c * (0.0 - uu[8]) + s * ss[8];
    m[9] = uu[9] + c * (0.0 - uu[9]) + s * ss[9];
    m[10] = uu[10] + c * (1.0 - uu[10]) + s * ss[10];
    m[15] = 1.0;
    m
}

/// Transforms a direction by the upper 3x3 block of `m` (`m_vxfm`).
fn m_vxfm(m: &[f32; 16], w: Vec3) -> Vec3 {
    [
        w[0] * m[0] + w[1] * m[4] + w[2] * m[8],
        w[0] * m[1] + w[1] * m[5] + w[2] * m[9],
        w[0] * m[2] + w[1] * m[6] + w[2] * m[10],
    ]
}

/// Integrates the orientation basis `e` under angular velocity `w` through
/// `dt` and re-orthonormalizes it (`sol_rotate`).
fn rotate_basis(e: &mut [Vec3; 3], w: Vec3, dt: f32) {
    if len(w) > 0.0 {
        let a = nrm(w);
        let m = m_rot(a, len(w) * dt);

        let f = [m_vxfm(&m, e[0]), m_vxfm(&m, e[1]), m_vxfm(&m, e[2])];

        e[2] = crs(f[0], f[1]);
        e[1] = crs(f[2], f[0]);
        e[0] = crs(f[1], f[2]);

        e[0] = nrm(e[0]);
        e[1] = nrm(e[1]);
        e[2] = nrm(e[2]);
    }
}

/// `erp`: the smooth-step used by smooth paths.
fn erp(t: f32) -> f32 {
    3.0 * t * t - 2.0 * t * t * t
}

fn ms_to_time(ms: i32) -> f32 {
    ms as f32 * MILLISECOND
}

/// `ms_step`: accumulates `dt` and returns the whole milliseconds it contains.
fn ms_step(accum: &mut f32, dt: f32) -> i32 {
    let mut ms = 0;
    *accum += dt;
    while *accum >= MILLISECOND {
        let before = *accum;
        *accum -= MILLISECOND;
        if *accum >= before {
            // Too large for single precision to count down; C would spin.
            break;
        }
        ms += 1;
    }
    ms
}

/// `ms_peek`: what [`ms_step`] would return, without keeping the result.
fn ms_peek(accum: f32, dt: f32) -> i32 {
    let mut at = accum;
    ms_step(&mut at, dt)
}

// ---------------------------------------------------------------------------
// Sphere against vertex, edge and plane (share/solid_sim_sol.c).

/// The ball as the solvers see it: `struct v_ball` minus its bases.
#[derive(Clone, Copy, Debug)]
struct Probe {
    position: Vec3,
    velocity: Vec3,
    radius: f32,
}

/// Solves `(p + v * t) . (p + v * t) == r * r` for the smaller `t` (`v_sol`).
fn solve_sphere(p: Vec3, v: Vec3, r: f32) -> f32 {
    let a = dot(v, v);
    let b = dot(v, p) * 2.0;
    let c = dot(p, p) - r * r;
    let d = b * b - 4.0 * a * c;

    // Testing for equality against zero is what the original does.
    if a == 0.0 {
        return LARGE;
    }

    if d < 0.0 {
        LARGE
    } else if d > 0.0 {
        let t0 = 0.5 * (-b - sqrt32(d)) / a;
        let t1 = 0.5 * (-b + sqrt32(d)) / a;
        let t = if t0 < t1 { t0 } else { t1 };

        if t < 0.0 { LARGE } else { t }
    } else {
        -b * 0.5 / a
    }
}

/// Earliest time the ball touches a vertex at `q` (relative to `o`) that moves
/// with velocity `w` (`v_vert`). `hit` receives the contact point when the
/// result is below [`LARGE`].
fn hit_vertex(hit: &mut Vec3, o: Vec3, q: Vec3, w: Vec3, ball: &Probe) -> f32 {
    let origin = add(o, q);
    let p = sub(ball.position, origin);
    let v = sub(ball.velocity, w);
    let mut t = LARGE;

    if dot(p, v) < 0.0 {
        t = solve_sphere(p, v, ball.radius);

        if t < LARGE {
            *hit = mad(origin, w, t);
        }
    }
    t
}

/// Earliest time the ball touches the edge from `q` along `u` (`v_edge`).
fn hit_edge(hit: &mut Vec3, o: Vec3, q: Vec3, u: Vec3, w: Vec3, ball: &Probe) -> f32 {
    let r = ball.radius;
    let d = sub(sub(ball.position, o), q);
    let e = sub(ball.velocity, w);

    // Project the offset and the relative velocity off the edge direction.
    let du = dot(d, u);
    let eu = dot(e, u);
    let uu = dot(u, u);

    let p = mad(d, u, -du / uu);

    if dot(p, p) < r * r {
        // The sphere already intersects the line of the edge.
        if du < 0.0 || du > uu {
            // Behind the endpoints: the vertices come first.
            return LARGE;
        }

        // The sphere already intersects the edge.
        if dot(p, e) >= 0.0 {
            // Moving apart.
            return LARGE;
        }

        *hit = mad(ball.position, nrm(p), -r);
        return 0.0;
    }

    let v = mad(e, u, -eu / uu);

    let t = solve_sphere(p, v, r);
    let s = (du + eu * t) / uu;

    if (0.0..LARGE).contains(&t) && 0.0 < s && s < 1.0 {
        let moved = mad(o, w, t);
        let along = mad(q, u, s);
        *hit = add(along, moved);
        t
    } else {
        LARGE
    }
}

/// Earliest time the ball touches the plane `n . x == d` (offset by `o`) that
/// moves with velocity `w` (`v_side`).
fn hit_side(hit: &mut Vec3, o: Vec3, w: Vec3, n: Vec3, d: f32, ball: &Probe) -> f32 {
    let r = ball.radius;
    let vn = dot(ball.velocity, n);
    let wn = dot(w, n);
    let mut t = LARGE;

    if vn - wn < 0.0 {
        let on = dot(o, n);
        let pn = dot(ball.position, n);

        let u = (r + d + on - pn) / (vn - wn);
        let a = (d + on - pn) / (vn - wn);

        if 0.0 <= u {
            t = u;

            *hit = mad(ball.position, ball.velocity, t);
            *hit = mad(*hit, n, -r);
        } else if 0.0 <= a {
            t = 0.0;

            *hit = mad(ball.position, ball.velocity, t);
            *hit = mad(*hit, n, -r);
        }
    }
    t
}

// ---------------------------------------------------------------------------
// Lumps and the BSP (share/solid_sim_sol.c).

/// A node still to examine during the iterative BSP walk.
#[derive(Clone, Copy, Debug)]
enum Frame {
    /// Test the node's lumps, then queue its fore and back subtrees.
    Visit(usize),
    /// The back-side test of a node, run once its fore subtree is done.
    Back(usize),
}

/// `sol_test_fore`: may the ball be in front of the plane during `dt`?
fn test_fore(dt: f32, ball: &Probe, normal: Vec3, distance: f32, o: Vec3, w: Vec3) -> bool {
    // If the ball is not behind the plane, the test passes.
    let q = sub(ball.position, o);
    let mut d = distance;

    if dot(q, normal) - d + ball.radius >= 0.0 {
        return true;
    }

    // If it's not behind the plane after DT seconds, the test passes.
    let q = mad(q, ball.velocity, dt);
    d += dot(w, normal) * dt;

    dot(q, normal) - d + ball.radius >= 0.0
}

/// `sol_test_back`: may the ball be behind the plane during `dt`?
fn test_back(dt: f32, ball: &Probe, normal: Vec3, distance: f32, o: Vec3, w: Vec3) -> bool {
    // If the ball is not in front of the plane, the test passes.
    let q = sub(ball.position, o);
    let mut d = distance;

    if dot(q, normal) - d - ball.radius <= 0.0 {
        return true;
    }

    // If it's not in front of the plane after DT seconds, the test passes.
    let q = mad(q, ball.velocity, dt);
    d += dot(w, normal) * dt;

    dot(q, normal) - d - ball.radius <= 0.0
}

/// `sol_test_lump`: improves `(best, point)` with the earliest hit on a lump.
fn test_lump(
    sol: &Sol,
    ball: &Probe,
    lump: &crate::sol::Lump,
    o: Vec3,
    w: Vec3,
    best: &mut f32,
    point: &mut Vec3,
) {
    // Short circuit a non-solid lump.
    if lump.is_detail() {
        return;
    }

    let mut scratch = [0.0_f32; 3];

    if ball.radius > 0.0 {
        for &vi in &lump.vertices {
            let u = hit_vertex(&mut scratch, o, sol.vertices[vi], w, ball);
            if u < *best {
                *point = scratch;
                *best = u;
            }
        }

        for &ei in &lump.edges {
            let [vi, vj] = sol.edges[ei].vertices;
            let q = sol.vertices[vi];
            let u = sub(sol.vertices[vj], sol.vertices[vi]);

            let t = hit_edge(&mut scratch, o, q, u, w, ball);
            if t < *best {
                *point = scratch;
                *best = t;
            }
        }
    }

    for &si in &lump.sides {
        // `sol_test_side`: a plane hit counts only if the contact point lies
        // behind every other plane of the lump.
        let sp = &sol.sides[si];
        let mut t = hit_side(&mut scratch, o, w, sp.normal, sp.distance, ball);

        if t < *best {
            for &other in &lump.sides {
                if other != si {
                    let sq = &sol.sides[other];
                    let behind =
                        dot(scratch, sq.normal) - dot(o, sq.normal) - dot(w, sq.normal) * t;
                    if behind > sq.distance {
                        t = LARGE;
                        break;
                    }
                }
            }
        }

        if t < *best {
            *point = scratch;
            *best = t;
        }
    }
}

/// `sol_test_node`: the earliest hit below `dt` in the BSP rooted at `root`.
///
/// The C routine recurses and keeps the best time in each frame, but a child
/// starts from its parent's best and only lowers it, so one running best
/// visited in the same order gives the same answer.
fn test_node(
    sol: &Sol,
    stack: &mut Vec<Frame>,
    ball: &Probe,
    root: usize,
    o: Vec3,
    w: Vec3,
    dt: f32,
) -> (f32, Vec3) {
    let mut best = dt;
    let mut point = [0.0_f32; 3];

    stack.clear();
    stack.push(Frame::Visit(root));

    while let Some(frame) = stack.pop() {
        match frame {
            Frame::Visit(index) => {
                let node = &sol.nodes[index];

                for lump in &sol.lumps[node.lumps.clone()] {
                    test_lump(sol, ball, lump, o, w, &mut best, &mut point);
                }

                // Children without a splitting plane cannot be tested.
                let Some(si) = node.side else { continue };

                if node.back.is_some() {
                    stack.push(Frame::Back(index));
                }
                if let Some(fore) = node.fore {
                    let sp = &sol.sides[si];
                    if test_fore(best, ball, sp.normal, sp.distance, o, w) {
                        stack.push(Frame::Visit(fore));
                    }
                }
            }
            Frame::Back(index) => {
                let node = &sol.nodes[index];
                if let (Some(si), Some(back)) = (node.side, node.back) {
                    let sp = &sol.sides[si];
                    if test_back(best, ball, sp.normal, sp.distance, o, w) {
                        stack.push(Frame::Visit(back));
                    }
                }
            }
        }
    }
    (best, point)
}

/// The earliest contact of a step.
#[derive(Clone, Copy, Debug)]
struct Hit {
    /// Time of the contact; the time tested when there is none.
    time: f32,
    /// World-space contact point (`T`).
    point: Vec3,
    /// World-space velocity of the thing touched (`V`).
    velocity: Vec3,
}

impl Hit {
    const fn miss(time: f32) -> Self {
        Self {
            time,
            point: [0.0; 3],
            velocity: [0.0; 3],
        }
    }
}

// ---------------------------------------------------------------------------
// Varying state.

/// A mover (`struct v_move`): a body's progress along its current path.
#[derive(Clone, Copy, Debug)]
struct Mover {
    /// Seconds on the current path.
    time: f32,
    /// The same, in whole milliseconds.
    millis: i32,
    /// Current path, an index into [`Sol::paths`].
    path: usize,
}

/// The movers of one body (`mi`, `mj` of `struct v_body`).
#[derive(Clone, Copy, Debug)]
struct BodyMovers {
    /// Drives the body's position.
    position: Option<usize>,
    /// Drives the body's orientation; the same mover as `position` unless the
    /// level gives the body a second path.
    orientation: Option<usize>,
}

/// A switch (`struct v_swch`).
#[derive(Clone, Copy, Debug)]
struct SwitchState {
    /// Seconds of the running timer.
    time: f32,
    /// The same, in whole milliseconds.
    millis: i32,
    /// Current state.
    on: bool,
    /// Whether the ball is inside it.
    inside: bool,
}

fn push_mover(movers: &mut Vec<Mover>, path: usize) -> usize {
    movers.push(Mover {
        time: 0.0,
        millis: 0,
        path,
    });
    movers.len() - 1
}

/// The state of the simulation: the ball, the moving bodies and the switches.
///
/// Build it with [`Motion::new`] from a level and pass the same [`Sol`] to
/// every later call; passing a different level is a programming error and may
/// panic.
#[derive(Clone, Debug)]
pub struct Motion {
    /// The ball: position and radius. Set the position or radius directly for
    /// teleports and size changes, as the game server does.
    pub ball: Ball,
    /// Linear velocity of the ball (`v`).
    pub velocity: Vec3,
    /// Angular velocity of the ball's rolling (`w`).
    pub angular_velocity: Vec3,
    /// Orientation of the rolling ball (`e`): its three basis vectors. The
    /// renderer rotates the ball model by this basis.
    pub basis: [Vec3; 3],
    /// Basis of the ball's pendulum (`E`), which orients the ball's marking.
    pub pendulum_basis: [Vec3; 3],
    /// Angular velocity of the pendulum (`W`).
    pub pendulum_velocity: Vec3,
    /// Enable flag of each path (`v_path::f`).
    paths: Vec<bool>,
    movers: Vec<Mover>,
    bodies: Vec<BodyMovers>,
    switches: Vec<SwitchState>,
    /// Millisecond accumulator (`ms_accum`).
    clock: f32,
    /// Reusable BSP walk buffer.
    stack: Vec<Frame>,
}

const IDENTITY_BASIS: [Vec3; 3] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];

impl Motion {
    /// The simulation at the start of `sol` (`sol_load_vary`, `sol_init_sim`):
    /// the ball at the first start position, at rest, with identity bases;
    /// every path with its default enable flag; every switch in its default
    /// state with its default timer; every body at its first path.
    #[must_use]
    pub fn new(sol: &Sol) -> Self {
        let mut movers = Vec::new();
        let mut bodies = Vec::with_capacity(sol.bodies.len());

        for body in &sol.bodies {
            let position = body.path.map(|path| push_mover(&mut movers, path));
            let orientation = if body.path_alt == body.path {
                position
            } else {
                body.path_alt.map(|path| push_mover(&mut movers, path))
            };
            bodies.push(BodyMovers {
                position,
                orientation,
            });
        }

        Self {
            ball: *sol.start(),
            velocity: [0.0; 3],
            angular_velocity: [0.0; 3],
            basis: IDENTITY_BASIS,
            pendulum_basis: IDENTITY_BASIS,
            pendulum_velocity: [0.0; 3],
            paths: sol.paths.iter().map(|path| path.enabled).collect(),
            movers,
            bodies,
            switches: sol
                .switches
                .iter()
                .map(|switch| SwitchState {
                    time: switch.timer,
                    millis: switch.timer_ms,
                    on: switch.default_state,
                    inside: false,
                })
                .collect(),
            clock: 0.0,
            stack: Vec::new(),
        }
    }

    /// Whether path `path` is currently enabled (`v_path::f`), or `None` if
    /// the level has no such path.
    #[must_use]
    pub fn path_enabled(&self, path: usize) -> Option<bool> {
        self.paths.get(path).copied()
    }

    /// The current state of switch `switch` (`v_swch::f`), or `None` if the
    /// level has no such switch.
    #[must_use]
    pub fn switch_on(&self, switch: usize) -> Option<bool> {
        self.switches.get(switch).map(|state| state.on)
    }

    /// Position and orientation of body `body_index` in level space, as the
    /// renderer places it (`sol_body_p` and `sol_body_e` at time zero). The
    /// orientation is a quaternion `[w, x, y, z]`; a body without movers sits
    /// at the origin with the identity orientation.
    ///
    /// # Panics
    ///
    /// Panics if `body_index` is not a body of `sol`.
    #[must_use]
    pub fn body_pose(&self, sol: &Sol, body_index: usize) -> (Vec3, [f32; 4]) {
        let movers = self.bodies[body_index];
        (
            self.body_position(sol, movers, 0.0),
            self.body_orientation(sol, movers, 0.0),
        )
    }

    /// Advances the simulation by `dt` seconds under the gravity acceleration
    /// `gravity` (`sol_step` with the friction branch off), moving the bodies,
    /// running the switch timers and bouncing the ball off whatever it hits.
    ///
    /// Returns the strongest impact of the step: the speed of the ball into
    /// the surface at the hardest bounce, or `0.0` when it hit nothing. The
    /// game turns anything above `0.5` into a bump sound.
    ///
    /// The step splits itself at every path change and at every collision (at
    /// most 16 times; the last iteration moves unobstructed), so a fast ball
    /// does not pass through thin geometry. `dt` must be finite.
    pub fn step(&mut self, sol: &Sol, gravity: Vec3, dt: f32) -> f32 {
        let before = self.velocity;
        let mut remaining = dt;
        let mut energy = 0.0_f32;

        // Without the friction branch the velocity just gains gravity.
        self.velocity = mad(before, gravity, remaining);

        let mut stack = std::mem::take(&mut self.stack);

        for iteration in (1..=MAX_ITERATIONS).rev() {
            if remaining.is_nan() || remaining <= 0.0 {
                break;
            }

            // Avoid stepping across path changes.
            let until = self.path_time(sol, remaining);

            // Miss collisions if we reach the iteration limit.
            let hit = if iteration > 1 {
                let ball = self.probe();
                self.test_bodies(sol, &mut stack, &ball, until)
            } else {
                Hit::miss(remaining)
            };

            self.advance(sol, hit.time);

            if hit.time < until {
                let impact = self.bounce(hit.point, hit.velocity);
                if energy < impact {
                    energy = impact;
                }
            }

            remaining -= hit.time;
        }

        self.stack = stack;

        let acceleration = sub(self.velocity, before);
        self.pendulum(acceleration, gravity, dt);

        energy
    }

    /// Tests the ball against all switches at its current position
    /// (`sol_swch_test`), toggling every switch it enters and starting its
    /// timer, and enabling or disabling the switch's paths. Returns `true`
    /// when the ball entered a visible switch, which is when the game plays
    /// the switch sound.
    pub fn touch_switches(&mut self, sol: &Sol) -> bool {
        let position = self.ball.position;
        let radius = self.ball.radius;
        let mut entered = false;

        for (index, base) in sol.switches.iter().enumerate() {
            let state = self.switches[index];

            // Timed switches only react while in their default state
            // (`xp->base->t == 0 || xp->f == xp->base->f`).
            if base.timer != 0.0 && state.on != base.default_state {
                continue;
            }

            let horizontal = [
                position[0] - base.position[0],
                position[2] - base.position[2],
                0.0,
            ];

            // Distance of the far side from the edge of the halo; the touch
            // distance is the ball's diameter.
            let d = len(horizontal) + radius - base.radius;

            if d <= radius * 2.0
                && position[1] > base.position[1]
                && position[1] < base.position[1] + SWITCH_HEIGHT / 2.0
            {
                if !state.inside && d <= 0.0 {
                    // The ball enters; toggle the state and update the path.
                    let mut next = state;
                    if base.timer_ms == 0 {
                        next.inside = true;
                    }
                    next.on = !next.on;

                    // It toggled to non-default state: start the timer.
                    if next.on != base.default_state {
                        next.time = 0.0;
                        next.millis = 0;
                    }
                    self.switches[index] = next;

                    self.set_path_loop(sol, base.path, next.on);

                    if !base.invisible {
                        entered = true;
                    }
                }
            } else if state.inside {
                // The ball exits.
                self.switches[index].inside = false;
            }
        }
        entered
    }

    // -- Bodies (share/solid_all.c) ----------------------------------------

    /// The path parameter `s` of `mover`: its progress along the current path
    /// of duration `duration`. Looking ahead by `dt` only counts while the
    /// path is enabled.
    fn phase(&self, mover: &Mover, duration: f32, dt: f32) -> f32 {
        if self.paths[mover.path] {
            (mover.time + dt) / duration
        } else {
            mover.time / duration
        }
    }

    /// `sol_body_p`: position of a body `dt` seconds from now.
    fn body_position(&self, sol: &Sol, movers: BodyMovers, dt: f32) -> Vec3 {
        if let Some(mi) = movers.position {
            let mover = &self.movers[mi];
            let pp = &sol.paths[mover.path];
            let pq = &sol.paths[pp.next];

            let s = self.phase(mover, pp.time, dt);
            let v = sub(pq.position, pp.position);

            return mad(pp.position, v, if pp.smooth { erp(s) } else { s });
        }
        [0.0; 3]
    }

    /// `sol_body_v`: velocity of a body over the next `dt` seconds.
    fn body_velocity(&self, sol: &Sol, movers: BodyMovers, dt: f32) -> Vec3 {
        if let Some(mi) = movers.position {
            if self.paths[self.movers[mi].path] {
                let p = self.body_position(sol, movers, 0.0);
                let q = self.body_position(sol, movers, dt);
                let v = sub(q, p);

                return [v[0] / dt, v[1] / dt, v[2] / dt];
            }
        }
        [0.0; 3]
    }

    /// `sol_body_e`: orientation of a body `dt` seconds from now.
    fn body_orientation(&self, sol: &Sol, movers: BodyMovers, dt: f32) -> Quat {
        if let Some(mj) = movers.orientation {
            let mover = &self.movers[mj];
            let pp = &sol.paths[mover.path];
            let pq = &sol.paths[pp.next];

            if oriented(pp) || oriented(pq) {
                let s = self.phase(mover, pp.time, dt);
                return q_slerp(
                    pp.orientation,
                    pq.orientation,
                    if pp.smooth { erp(s) } else { s },
                );
            }
        }
        [1.0, 0.0, 0.0, 0.0]
    }

    /// `sol_body_w`: whether the body might be rotating.
    fn body_rotates(&self, sol: &Sol, movers: BodyMovers) -> bool {
        if let Some(mj) = movers.orientation {
            let mover = &self.movers[mj];
            if self.paths[mover.path] {
                let pp = &sol.paths[mover.path];
                let pq = &sol.paths[pp.next];
                return oriented(pp) || oriented(pq);
            }
        }
        false
    }

    // -- Collision (share/solid_sim_sol.c) ---------------------------------

    fn probe(&self) -> Probe {
        Probe {
            position: self.ball.position,
            velocity: self.velocity,
            radius: self.ball.radius,
        }
    }

    /// `sol_test_body`: the earliest hit on body `index` below `dt`.
    fn test_body(
        &self,
        sol: &Sol,
        stack: &mut Vec<Frame>,
        ball: &Probe,
        dt: f32,
        index: usize,
    ) -> Hit {
        let Some(root) = sol.bodies[index].node else {
            return Hit::miss(dt);
        };
        let movers = self.bodies[index];

        let origin = self.body_position(sol, movers, 0.0);
        let velocity = self.body_velocity(sol, movers, dt);
        let orientation = self.body_orientation(sol, movers, 0.0);

        // For rotating bodies, rather than rotate every normal and vertex of
        // the body, pretend the ball is rotating and moving about a static
        // body.
        if orientation[0] != 1.0 || self.body_rotates(sol, movers) {
            // Position of the ball in the body's frame at the start and end of
            // the interval.
            let relative = sub(ball.position, origin);
            let start = q_rot(q_conj(orientation), relative);

            let end = mad(mad(relative, ball.velocity, dt), velocity, -dt);
            let end = q_rot(q_conj(self.body_orientation(sol, movers, dt)), end);

            // The ball relative to the body, with the velocity implied by the
            // two positions.
            let local = Probe {
                position: start,
                velocity: scl(sub(end, start), 1.0 / dt),
                radius: ball.radius,
            };

            let (u, point) = test_node(sol, stack, &local, root, [0.0; 3], [0.0; 3], dt);
            if u < dt {
                // The final orientation, to return world-space coordinates.
                let e = self.body_orientation(sol, movers, u);

                return Hit {
                    time: u,
                    point: mad(add(origin, q_rot(e, point)), velocity, u),
                    // The "non-ball" velocity.
                    velocity: sub(ball.velocity, q_rot(e, local.velocity)),
                };
            }
        } else {
            let (u, point) = test_node(sol, stack, ball, root, origin, velocity, dt);
            if u < dt {
                return Hit {
                    time: u,
                    point,
                    velocity,
                };
            }
        }
        Hit::miss(dt)
    }

    /// `sol_test_file`: the earliest hit on any body below `dt`.
    fn test_bodies(&self, sol: &Sol, stack: &mut Vec<Frame>, ball: &Probe, dt: f32) -> Hit {
        let mut best = Hit::miss(dt);

        for index in 0..sol.bodies.len() {
            let hit = self.test_body(sol, stack, ball, best.time, index);
            if hit.time < best.time {
                best = hit;
            }
        }
        best
    }

    /// `sol_bounce`: the new velocities and position of the ball after an
    /// impact at `q` with something moving at `w`. Returns the impact "energy".
    fn bounce(&mut self, q: Vec3, w: Vec3) -> f32 {
        let p = self.ball.position;
        let v = self.velocity;
        let radius = self.ball.radius;

        // Find the normal of the impact.
        let r = sub(p, q);
        let d = sub(v, w);
        let n = nrm(r);

        // Find the new angular velocity.
        self.angular_velocity = scl(crs(d, r), -1.0 / (radius * radius));

        // Find the new linear velocity.
        let vn = dot(v, n);
        let wn = dot(w, n);

        self.velocity = mad(v, n, 1.7 * (wn - vn));
        self.ball.position = mad(q, n, radius);

        // The "energy" of the impact determines the sound amplitude.
        dot(n, d).abs()
    }

    // -- Time (share/solid_sim_sol.c, share/solid_all.c) -------------------

    /// `sol_path_time`: `dt`, shortened to the next path change.
    fn path_time(&self, sol: &Sol, dt: f32) -> f32 {
        let mut dt = dt;

        for mover in &self.movers {
            if !self.paths[mover.path] {
                continue;
            }

            let base = &sol.paths[mover.path];
            if mover.millis + ms_peek(self.clock, dt) > base.time_ms {
                dt = ms_to_time(base.time_ms - mover.millis);
            }
        }
        dt
    }

    /// `sol_move_once`: moves the bodies, the switch timers and the ball
    /// forward `dt` seconds.
    fn advance(&mut self, sol: &Sol, dt: f32) {
        let ms = ms_step(&mut self.clock, dt);

        self.move_step(sol, dt, ms);
        self.switch_step(sol, dt, ms);
        self.ball_step(dt);
    }

    /// `sol_move_step`: the movers after `dt` seconds (`ms` milliseconds).
    fn move_step(&mut self, sol: &Sol, dt: f32, ms: i32) {
        for mover in &mut self.movers {
            if self.paths[mover.path] {
                let base = &sol.paths[mover.path];

                mover.time += dt;
                mover.millis += ms;

                if mover.millis >= base.time_ms {
                    mover.time = 0.0;
                    mover.millis = 0;
                    mover.path = base.next;
                }
            }
        }
    }

    /// `sol_swch_step`: the switch timers after `dt` seconds (`ms`
    /// milliseconds); an expired timer returns the switch to its default.
    fn switch_step(&mut self, sol: &Sol, dt: f32, ms: i32) {
        for (index, base) in sol.switches.iter().enumerate() {
            let state = self.switches[index];

            if state.millis < base.timer_ms {
                let millis = state.millis + ms;

                self.switches[index].time += dt;
                self.switches[index].millis = millis;

                if millis >= base.timer_ms {
                    self.set_path_loop(sol, base.path, base.default_state);
                    self.switches[index].on = base.default_state;
                }
            }
        }
    }

    /// `sol_ball_step`: the ball after `dt` seconds of its own velocity.
    fn ball_step(&mut self, dt: f32) {
        self.ball.position = mad(self.ball.position, self.velocity, dt);
        rotate_basis(&mut self.basis, self.angular_velocity, dt);
    }

    /// `sol_path_flag`: enables or disables one path.
    fn set_path_flag(&mut self, path: usize, enabled: bool) {
        if let Some(flag) = self.paths.get_mut(path) {
            *flag = enabled;
        }
    }

    /// `sol_path_loop`: enables or disables the path `first` and everything
    /// reachable from it (the tail leading to a loop and the loop itself).
    fn set_path_loop(&mut self, sol: &Sol, first: Option<usize>, enabled: bool) {
        let Some(first) = first else { return };
        if first >= self.paths.len() {
            return;
        }
        let next = |path: usize| sol.paths[path].next;

        let mut pi = first;
        let mut pj = first;

        // Tortoise and hare cycle traverser.
        loop {
            self.set_path_flag(pi, enabled);

            pi = next(pi);
            pj = next(pj);
            pj = next(pj);
            if pi == pj {
                break;
            }
        }

        // The indices now point into the loop; walk the remaining nodes of
        // that loop.
        pj = first;
        let start = pi;

        loop {
            self.set_path_flag(pi, enabled);

            pi = next(pi);
            pj = next(pj);
            if pi == pj || pi == start {
                break;
            }
        }
    }

    /// `sol_pendulum`: the new angular velocity and orientation of the ball
    /// pendulum. `a` is the acceleration of the ball, `g` the gravity.
    fn pendulum(&mut self, a: Vec3, g: Vec3, dt: f32) {
        const MASS: f32 = 5.000;
        const KA: f32 = 0.500;
        const KD: f32 = 0.995;

        // Find the total force over DT.
        let total = mad(scl(a, KA), g, -dt);

        // Find the force.
        let force = scl(total, MASS / dt);

        // Find the position of the pendulum.
        let r = scl(self.pendulum_basis[1], -self.ball.radius);

        // Find the torque on the pendulum.
        let mut torque = [0.0_f32; 3];
        if dot(r, force).abs() > 0.0 {
            torque = crs(force, r);
        }

        // Apply the torque and dampen the angular velocity.
        self.pendulum_velocity = mad(self.pendulum_velocity, torque, dt);
        self.pendulum_velocity = scl(self.pendulum_velocity, KD);

        // Apply the angular velocity to the pendulum basis.
        rotate_basis(&mut self.pendulum_basis, self.pendulum_velocity, dt);

        // Apply a torque turning the pendulum toward the ball velocity.
        let e1 = self.pendulum_basis[1];
        let v = mad(self.velocity, e1, dot(self.velocity, e1));
        let mut y = crs(v, self.pendulum_basis[2]);

        // The original is `v_scl(Y, E[1], 2 * v_dot(Y, E[1]))`, a macro that
        // re-evaluates the dot product for every component while Y is being
        // overwritten in place. The later components therefore see the
        // already-scaled earlier ones; this reproduces that.
        y[0] = e1[0] * (2.0 * dot(y, e1));
        y[1] = e1[1] * (2.0 * dot(y, e1));
        y[2] = e1[2] * (2.0 * dot(y, e1));

        rotate_basis(&mut self.pendulum_basis, y, dt);
    }
}

/// Whether a path waypoint carries an orientation (`P_ORIENTED`).
fn oriented(path: &crate::sol::Path) -> bool {
    path.flags & flag::PATH_ORIENTED != 0
}
