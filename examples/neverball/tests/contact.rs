// SPDX-License-Identifier: GPL-2.0-or-later
// Behavioral boundaries from Neverball 1.6.0, share/solid_all.c and share/geom.h.
use neverball::contact::{JumpContact, goal_at, jump_at, take_item};
use neverball::sol::{Ball, Goal, Item, ItemKind, Jump, Vec3};

fn ball(position: Vec3) -> Ball {
    Ball {
        position,
        radius: 0.25,
    }
}

fn item(position: Vec3, kind: ItemKind, value: i32) -> Item {
    Item {
        position,
        kind,
        value,
    }
}

#[test]
fn pickups_follow_file_order_and_a_taken_coin_cannot_be_taken_again() {
    let center = [2.0, 0.5, -3.0];
    let coin = item(center, ItemKind::Coin, 5);
    let grow = item(center, ItemKind::Grow, 0);
    let mut items = [item(center, ItemKind::None, 10), coin, grow];
    assert_eq!(take_item(ball(center), &mut items), Some((1, coin)));
    assert_eq!(take_item(ball(center), &mut items), Some((2, grow)));
    assert_eq!(take_item(ball(center), &mut items), None);
}

#[test]
fn a_pickup_uses_spherical_distance_and_excludes_exact_tangency() {
    let mut items = [item([0.0; 3], ItemKind::Shrink, 0)];
    let tangent = ball([0.0, 0.25_f32 + 0.15, 0.0]);
    assert_eq!(take_item(tangent, &mut items), None);
    assert_eq!(take_item(ball([0.30, 0.0, 0.30]), &mut items), None);
    assert_eq!(
        take_item(ball([0.0, 0.399, 0.0]), &mut items).map(|(_, i)| i.kind),
        Some(ItemKind::Shrink)
    );
}

#[test]
fn goal_requires_the_whole_radius_strictly_inside_and_an_open_height_interval() {
    let goals = [Goal {
        position: [2.0, 4.0, -1.0],
        radius: 1.0,
    }; 2];
    for p in [
        [2.75, 4.5, -1.0],
        [2.0, 4.0, -1.0],
        [2.0, 5.5, -1.0],
        [2.0, 4.5, -0.24],
    ] {
        assert_eq!(goal_at(ball(p), &goals), None, "{p:?}");
    }
    assert_eq!(goal_at(ball([2.749, 5.499, -1.0]), &goals), Some(0));
}

#[test]
fn teleport_preserves_offset_in_all_axes_and_an_inside_pad_wins_over_an_earlier_touch() {
    let pads = [
        Jump {
            position: [0.0; 3],
            radius: 1.0,
            target: [50.0; 3],
        },
        Jump {
            position: [1.0, 0.25, 0.25],
            radius: 1.0,
            target: [-4.0, 3.0, 8.0],
        },
    ];
    assert_eq!(
        jump_at(ball([1.0, 0.5, 0.0]), &pads),
        JumpContact::Inside {
            index: 1,
            destination: [-4.0, 3.25, 7.75]
        }
    );
}

#[test]
fn jump_includes_both_radial_boundaries_but_excludes_floor_and_ceiling() {
    let pad = [Jump {
        position: [0.0; 3],
        radius: 1.0,
        target: [10.0, 2.0, -3.0],
    }];
    assert_eq!(
        jump_at(ball([0.0, 0.5, -0.75]), &pad),
        JumpContact::Inside {
            index: 0,
            destination: [10.0, 2.5, -3.75]
        }
    );
    assert_eq!(jump_at(ball([1.0, 0.5, 0.0]), &pad), JumpContact::Touch);
    for p in [[1.001, 0.5, 0.0], [0.0, 0.0, 0.0], [0.0, 1.0, 0.0]] {
        assert_eq!(jump_at(ball(p), &pad), JumpContact::Outside, "{p:?}");
    }
}
