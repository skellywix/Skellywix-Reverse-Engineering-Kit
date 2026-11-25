// SPDX-License-Identifier: GPL-2.0-or-later
//
// Tests for the Rust port of the Neverball 1.6.0 SOL loader.
// See NOTICE.md and COPYING.

//! SOL loader tests. Inputs come from the independent writer in
//! `tests/support`, which lays records out from the 1.6.0 `solid_base.c`
//! description rather than from the loader.

mod support;

use neverball::error::Error;
use neverball::sol::{flag, ItemKind, Sol, Version, SOL_MAGIC};
use support::{patch_count, sample_v6, sample_v7, Builder, Mtrl};

fn load(b: &Builder) -> Sol {
    Sol::from_bytes(&b.build()).unwrap_or_else(|e| panic!("sample failed to load: {e}"))
}

fn load_err(b: &Builder) -> Error {
    match Sol::from_bytes(&b.build()) {
        Ok(_) => panic!("expected an error, but the file loaded"),
        Err(e) => e,
    }
}

fn near(a: f32, b: f32) -> bool {
    (a - b).abs() < 1e-5
}

// ---------------------------------------------------------------------------
// Version 7 (the format 1.6.0 writes).

#[test]
fn magic_constant_matches_file_bytes() {
    assert_eq!(SOL_MAGIC, 0x4C4F_53AF);
    assert_eq!(&sample_v7().build()[..4], &[0xAF, b'S', b'O', b'L']);
}

#[test]
fn v7_materials_including_alpha_test_block() {
    let sol = load(&sample_v7());
    assert_eq!(sol.version, Version::Current);
    assert_eq!(sol.materials.len(), 2);

    let glass = &sol.materials[0];
    assert_eq!(glass.texture, "mtrl/glass");
    assert_eq!(glass.flags, flag::MATERIAL_SHADOWED | flag::MATERIAL_ALPHA_TEST);
    assert!(glass.has(flag::MATERIAL_ALPHA_TEST));
    assert_eq!(glass.alpha_func, 516);
    assert!(near(glass.alpha_ref, 0.5));
    assert_eq!(glass.diffuse, [0.8, 0.7, 0.6, 1.0]);
    assert!(near(glass.shininess, 32.0));

    // The second material has no alpha block. If the loader read one anyway
    // everything after it would be misaligned and the file would not load.
    let plain = &sol.materials[1];
    assert_eq!(plain.texture, "mtrl/plain");
    assert_eq!(plain.flags, 0);
    assert_eq!((plain.alpha_func, plain.alpha_ref), (0, 0.0));
}

#[test]
fn texture_name_filling_the_whole_field_is_kept() {
    let mut b = sample_v7();
    let long = "t".repeat(64);
    b.mtrls[1] = Mtrl::new(&long, 0);
    assert_eq!(load(&b).materials[1].texture, long);
}

#[test]
fn v7_vertices_edges_sides_and_texcoords() {
    let sol = load(&sample_v7());
    assert_eq!(sol.vertices.len(), 4);
    assert_eq!(sol.vertices[0], [0.0, -5.0, 0.0]);
    assert_eq!(sol.edges[1].vertices, [1, 2]);
    assert_eq!(sol.sides[1].normal, [1.0, 0.0, 0.0]);
    assert!(near(sol.sides[1].distance, 1.0));
    assert_eq!(sol.tex_coords[1], [1.0, 1.0]);
    assert_eq!(sol.offsets.len(), 3);
    assert_eq!(
        (sol.offsets[2].tex_coord, sol.offsets[2].side, sol.offsets[2].vertex),
        (0, 1, 2)
    );
}

#[test]
fn v7_triangle_corners_combine_offsets() {
    let sol = load(&sample_v7());
    let corners = sol.corners(&sol.geoms[0]);
    assert_eq!(corners[0].position, [0.0, -5.0, 0.0]);
    assert_eq!(corners[0].normal, [0.0, 1.0, 0.0]);
    assert_eq!(corners[0].tex_coord, [0.0, 0.0]);
    assert_eq!(corners[1].position, [1.0, 0.0, 0.0]);
    assert_eq!(corners[1].tex_coord, [1.0, 1.0]);
    // Offset 2 is (texc 0, side 1, vertex 2).
    assert_eq!(corners[2].position, [0.0, 1.0, 0.0]);
    assert_eq!(corners[2].normal, [1.0, 0.0, 0.0]);
    assert_eq!(corners[2].tex_coord, [0.0, 0.0]);
    assert_eq!(sol.geoms[1].material, 1);
    assert_eq!(sol.geoms[1].offsets, [2, 1, 0]);
}

#[test]
fn v7_lumps_resolve_the_shared_index_pool() {
    let sol = load(&sample_v7());
    let l0 = &sol.lumps[0];
    assert!(!l0.is_detail());
    assert_eq!(l0.vertices, [0, 1, 2]);
    assert_eq!(l0.edges, [0, 1]);
    assert_eq!(l0.geoms, [0]);
    assert_eq!(l0.sides, [0, 1]);

    // Lump 1 is detail geometry and shares pool entries with lump 0.
    let l1 = &sol.lumps[1];
    assert!(l1.is_detail());
    assert_eq!(l1.vertices, [1, 2]);
    assert!(l1.edges.is_empty());
    assert_eq!(l1.sides, [0]);

    let seen: Vec<_> = sol.lump_vertices(l1).copied().collect();
    assert_eq!(seen, [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]);
    assert_eq!(sol.lump_sides(l0).count(), 2);
    assert_eq!(sol.lump_edges(l0).count(), 2);
}

#[test]
fn v7_bsp_nodes_distinguish_leaves() {
    let sol = load(&sample_v7());
    let root = &sol.nodes[0];
    assert_eq!((root.side, root.fore, root.back), (Some(0), Some(1), Some(2)));
    assert!(root.lumps.is_empty());
    let leaf = &sol.nodes[1];
    assert_eq!((leaf.side, leaf.fore, leaf.back), (None, None, None));
    assert_eq!(leaf.lumps, 0..1);
    assert_eq!(sol.nodes[2].lumps, 1..2);
}

#[test]
fn v7_paths_round_times_and_read_the_orientation_flag() {
    let sol = load(&sample_v7());
    let p0 = &sol.paths[0];
    assert_eq!(p0.time_ms, 2600);
    assert!(near(p0.time, 2.6));
    assert_eq!(p0.next, 1);
    assert!(p0.enabled && p0.smooth);
    assert_eq!(p0.flags, 0);
    // No orientation in the file: the identity quaternion.
    assert_eq!(p0.orientation, [1.0, 0.0, 0.0, 0.0]);

    let p1 = &sol.paths[1];
    // Negative times round away from zero at the half: -1000.6 -> -1001.
    assert_eq!(p1.time_ms, -1001);
    assert!(near(p1.time, -1.001));
    assert!(!p1.enabled && !p1.smooth);
    assert_eq!(p1.flags, flag::PATH_ORIENTED);
    assert_eq!(p1.orientation, [0.0, 0.0, 1.0, 0.0]);
    assert_eq!(p1.position, [5.0, 0.0, 0.0]);
}

#[test]
fn v7_bodies_second_path_defaults_to_the_first() {
    let sol = load(&sample_v7());
    let world = &sol.bodies[0];
    assert_eq!((world.path, world.path_alt, world.node), (None, None, Some(0)));
    assert_eq!(world.lumps, 0..2);
    assert_eq!(world.geoms, [0]);
    assert_eq!(sol.body_lumps(world).len(), 2);

    // pj is -1 in the file; the loader substitutes pi.
    let mover = &sol.bodies[1];
    assert_eq!((mover.path, mover.path_alt, mover.node), (Some(0), Some(0), None));
    assert_eq!(mover.geoms, [1]);

    // An explicit pj is kept.
    let two = &sol.bodies[2];
    assert_eq!((two.path, two.path_alt), (Some(0), Some(1)));
    assert!(two.geoms.is_empty());

    let tris: Vec<_> = sol.body_triangles(mover).collect();
    assert_eq!(tris.len(), 1);
    assert_eq!(tris[0].material.texture, "mtrl/plain");
    assert_eq!(tris[0].corners[0].position, [0.0, 1.0, 0.0]);
}

#[test]
fn v7_gameplay_entities() {
    let sol = load(&sample_v7());

    let kinds: Vec<_> = sol.items.iter().map(|i| i.kind).collect();
    assert_eq!(
        kinds,
        [
            ItemKind::Coin,
            ItemKind::Coin,
            ItemKind::Grow,
            ItemKind::Shrink,
            ItemKind::Unknown(9)
        ]
    );
    assert_eq!(sol.items[1].value, 5);
    assert_eq!(sol.items[4].value, 7);
    assert_eq!(sol.coin_value(), 6);

    assert_eq!(sol.goals.len(), 1);
    assert_eq!(sol.goals[0].position, [0.0, 0.0, 2.0]);
    assert!(near(sol.goals[0].radius, 0.75));

    assert_eq!(sol.jumps[0].position, [1.0, 0.0, 1.0]);
    assert_eq!(sol.jumps[0].target, [9.0, 0.0, 9.0]);
    assert!(near(sol.jumps[0].radius, 0.5));

    let sw = &sol.switches[0];
    assert_eq!(sw.position, [1.0, 2.0, 3.0]);
    assert_eq!(sw.path, Some(1));
    // 0.0004 s is 0.4 ms, which rounds to zero.
    assert_eq!(sw.timer_ms, 0);
    assert!(sw.default_state && sw.invisible);

    let bill = &sol.billboards[0];
    assert_eq!(bill.flags, flag::BILLBOARD_FLAT);
    assert_eq!(bill.material, 1);
    assert!(near(bill.repeat_time, 0.5));
    assert!(near(bill.distance, 10.0));
    assert_eq!(bill.width, [1.0, 2.0, 3.0]);
    assert_eq!(bill.height, [4.0, 5.0, 6.0]);
    assert_eq!(bill.rot_x, [7.0, 8.0, 9.0]);
    assert_eq!(bill.rot_y, [10.0, 11.0, 12.0]);
    assert_eq!(bill.rot_z, [13.0, 14.0, 15.0]);
    assert_eq!(bill.position, [16.0, 17.0, 18.0]);

    assert_eq!(sol.start().position, [0.0, 1.0, 0.0]);
    assert!(near(sol.start().radius, 0.25));
    assert_eq!(sol.views[0].position, [0.0, 2.0, 5.0]);
    assert_eq!(sol.views[0].target, [0.0, 1.0, 0.0]);
}

#[test]
fn v7_dictionary_and_derived_queries() {
    let sol = load(&sample_v7());
    assert_eq!(sol.attribute("message"), Some("Hello"));
    assert_eq!(sol.attribute("time"), Some("30"));
    assert_eq!(sol.attribute("nope"), None);
    assert_eq!(sol.fall_height(), Some(-5.0));
    assert_eq!(sol.bounds(), Some(([0.0, -5.0, 0.0], [1.0, 1.0, 1.0])));
}

#[test]
fn repeated_attribute_keys_last_one_wins() {
    let b = sample_v7().attr("time", "45");
    assert_eq!(load(&b).attribute("time"), Some("45"));
}

#[test]
fn trailing_bytes_after_the_last_record_are_ignored() {
    let mut bytes = sample_v7().build();
    bytes.extend_from_slice(&[0xDE, 0xAD, 0xBE, 0xEF, 1, 2, 3]);
    assert_eq!(Sol::from_bytes(&bytes).ok(), Some(load(&sample_v7())));
}

#[test]
fn unused_pool_entries_are_never_checked() {
    // The C loader only follows pool entries that a lump or body points at.
    let mut b = sample_v7();
    b.pool.push(-123);
    b.pool.push(9999);
    assert!(Sol::from_bytes(&b.build()).is_ok());
}

#[test]
fn a_file_without_a_ball_gets_one_zeroed_ball() {
    let mut b = sample_v7();
    b.balls.clear();
    let sol = load(&b);
    assert_eq!(sol.balls.len(), 1);
    assert_eq!(sol.start().position, [0.0; 3]);
    assert_eq!(sol.start().radius, 0.0);
}

#[test]
fn an_empty_level_loads() {
    let sol = load(&Builder::empty(7));
    assert!(sol.vertices.is_empty());
    assert_eq!(sol.fall_height(), None);
    assert_eq!(sol.bounds(), None);
    assert_eq!(sol.balls.len(), 1);
}

#[test]
fn non_utf8_attribute_bytes_do_not_fail_the_load() {
    let mut b = sample_v7();
    b.dict_override = Some((b"author\0Ren\xE9\0".to_vec(), vec![[0, 7]]));
    let sol = load(&b);
    assert!(sol.attribute("author").is_some_and(|a| a.starts_with("Ren")));
}

// ---------------------------------------------------------------------------
// Version 6 (Neverball 1.5).

#[test]
fn v6_material_flags_are_converted() {
    let sol = load(&sample_v6());
    assert_eq!(sol.version, Version::Legacy);

    // 1 | 4: shadowed, and reflective which also implies shadowed.
    let old = &sol.materials[0];
    assert_eq!(old.flags, flag::MATERIAL_SHADOWED | flag::MATERIAL_REFLECTIVE);

    // 0 is "mtrl/invisible": transparent, with zero diffuse alpha.
    let invisible = &sol.materials[1];
    assert_eq!(invisible.flags, flag::MATERIAL_TRANSPARENT);
    assert_eq!(invisible.diffuse[3], 0.0);
    assert_eq!(invisible.diffuse[0], 0.8);

    // 32 | 128: clamp S and T, two-sided.
    let clamped = &sol.materials[2];
    assert_eq!(
        clamped.flags,
        flag::MATERIAL_CLAMP_S | flag::MATERIAL_CLAMP_T | flag::MATERIAL_TWO_SIDED
    );
}

#[test]
fn v6_has_no_alpha_block_even_when_bit_nine_is_set() {
    // Bit 9 means nothing in a 1.5 file, so no alpha function is stored.
    let mut b = sample_v6();
    b.mtrls[0] = Mtrl::new("mtrl/odd", 1 << 9);
    let sol = load(&b);
    assert_eq!(sol.materials[0].alpha_func, 0);
    // No legacy bit matches, so the converted flags are empty.
    assert_eq!(sol.materials[0].flags, 0);
}

#[test]
fn v6_offsets_are_derived_from_triangles() {
    // Triangle 0 holds offset triples A B A. All three are looked up before
    // any is added, so A is appended twice. Triangle 1 holds A C B: A and B
    // match the first copies, C is new.
    let sol = load(&sample_v6());
    assert_eq!(sol.offsets.len(), 4);
    let key = |i: usize| {
        let o = &sol.offsets[i];
        (o.tex_coord, o.side, o.vertex)
    };
    assert_eq!(key(0), (0, 0, 0));
    assert_eq!(key(1), (1, 0, 1));
    assert_eq!(key(2), (0, 0, 0));
    assert_eq!(key(3), (2, 1, 2));
    assert_eq!(sol.geoms[0].offsets, [0, 1, 2]);
    assert_eq!(sol.geoms[1].offsets, [0, 3, 1]);
    assert_eq!(sol.geoms[0].material, 0);
    assert_eq!(sol.geoms[1].material, 1);
}

#[test]
fn v6_paths_and_bodies_lack_the_newer_fields() {
    let sol = load(&sample_v6());
    let p = &sol.paths[0];
    assert_eq!(p.flags, 0);
    // The builder was told fl = 1 but a 1.5 file has no such field, so no
    // quaternion is read either.
    assert_eq!(p.orientation, [1.0, 0.0, 0.0, 0.0]);
    assert_eq!(p.time_ms, 1000);

    assert_eq!((sol.bodies[0].path, sol.bodies[0].path_alt), (None, None));
    assert_eq!((sol.bodies[1].path, sol.bodies[1].path_alt), (Some(0), Some(0)));
    assert_eq!(sol.bodies[0].geoms, [0, 1]);
}

#[test]
fn v6_header_has_no_offset_count() {
    let mut bytes = sample_v6().build();
    // With the 1.5 layout the geometry count sits where 1.6 keeps the offset
    // count, so patching it by name must hit the right word.
    patch_count(&mut bytes, "geom", 6, 3);
    assert!(Sol::from_bytes(&bytes).is_err());
    assert_eq!(support::count_offset("geom", 6) + 4, support::count_offset("geom", 7));
}

#[test]
fn v6_bad_inline_offset_is_rejected() {
    let mut b = sample_v6();
    b.geoms[0][1] = 9; // ti out of range
    assert!(matches!(
        load_err(&b),
        Error::BadReference {
            record: "offset",
            field: "tex_coord",
            value: 9,
            limit: 3,
            ..
        }
    ));
}

// ---------------------------------------------------------------------------
// Rejection: framing.

#[test]
fn wrong_magic_is_rejected() {
    let mut bytes = sample_v7().build();
    bytes[0] = 0;
    assert!(matches!(Sol::from_bytes(&bytes), Err(Error::BadMagic { .. })));
}

#[test]
fn versions_outside_six_and_seven_are_rejected() {
    for version in [-1, 0, 5, 8, 100] {
        let mut bytes = sample_v7().build();
        bytes[4..8].copy_from_slice(&i32::to_le_bytes(version));
        assert_eq!(
            Sol::from_bytes(&bytes).err(),
            Some(Error::UnsupportedVersion { found: version })
        );
    }
}

#[test]
fn every_truncation_of_a_valid_file_is_an_error() {
    for builder in [sample_v7(), sample_v6()] {
        let bytes = builder.build();
        for len in 0..bytes.len() {
            let result = Sol::from_bytes(&bytes[..len]);
            assert!(
                matches!(
                    result,
                    Err(Error::Truncated { .. } | Error::CountExceedsData { .. })
                ),
                "{len} of {} bytes of a v{} file gave {result:?}",
                bytes.len(),
                builder.version
            );
        }
        assert!(Sol::from_bytes(&bytes).is_ok());
    }
}

#[test]
fn truncation_names_the_section_and_offset() {
    let bytes = sample_v7().build();
    // Cut inside the vertex records: after the header, strings, dictionary
    // and materials, but one byte short of the end of the pool the tail is
    // far away, so cut by section instead: drop only the last byte.
    let err = Sol::from_bytes(&bytes[..bytes.len() - 1]).err();
    match err {
        Some(Error::Truncated {
            section,
            needed,
            available,
            ..
        }) => {
            assert_eq!(section, "index pool");
            assert_eq!((needed, available), (4, 3));
        }
        other => panic!("unexpected {other:?}"),
    }
}

#[test]
fn counts_that_cannot_fit_are_rejected_before_allocating() {
    let good = sample_v7().build();
    for (name, section) in [
        ("vert", "vertex"),
        ("mtrl", "material"),
        ("geom", "geometry"),
        ("bill", "billboard"),
        ("pool", "index pool"),
        ("strings", "string block"),
    ] {
        let mut bytes = good.clone();
        patch_count(&mut bytes, name, 7, i32::MAX);
        match Sol::from_bytes(&bytes) {
            Err(Error::CountExceedsData {
                section: got,
                count,
                needed,
                available,
            }) => {
                assert_eq!(got, section);
                assert_eq!(count, i32::MAX as usize);
                assert!(needed > available);
            }
            other => panic!("{name}: expected CountExceedsData, got {other:?}"),
        }
    }
}

#[test]
fn negative_counts_are_rejected() {
    let good = sample_v7().build();
    for (name, section) in [("vert", "vertex"), ("path", "path"), ("lump", "lump")] {
        let mut bytes = good.clone();
        patch_count(&mut bytes, name, 7, -1);
        assert_eq!(
            Sol::from_bytes(&bytes).err(),
            Some(Error::NegativeCount { section, count: -1 })
        );
    }
}

#[test]
fn a_count_one_too_high_is_never_accepted() {
    // One extra vertex consumes 12 bytes of the following sections, so the
    // end of the file arrives 12 bytes early.
    let mut bytes = sample_v7().build();
    patch_count(&mut bytes, "vert", 7, 5);
    assert!(Sol::from_bytes(&bytes).is_err());
}

#[test]
fn flipping_any_single_byte_never_panics() {
    for builder in [sample_v7(), sample_v6()] {
        let bytes = builder.build();
        for at in 0..bytes.len() {
            for value in [0x00, 0x80, 0xFF] {
                let mut mutated = bytes.clone();
                mutated[at] = value;
                // Ok or Err are both fine; a panic fails the test.
                let _ = Sol::from_bytes(&mutated);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Rejection: references.

#[test]
fn out_of_range_references_are_rejected() {
    type Mutation = fn(&mut Builder);
    let cases: Vec<(&str, Mutation, Error)> = vec![
        (
            "edge vertex",
            |b| b.edges[1] = [1, 99],
            Error::BadReference {
                record: "edge",
                index: 1,
                field: "vertex_b",
                value: 99,
                limit: 4,
            },
        ),
        (
            "geometry material",
            |b| b.geoms[0][0] = 7,
            Error::BadReference {
                record: "geometry",
                index: 0,
                field: "material",
                value: 7,
                limit: 2,
            },
        ),
        (
            "geometry offset",
            |b| b.geoms[0][2] = 3,
            Error::BadReference {
                record: "geometry",
                index: 0,
                field: "offset_j",
                value: 3,
                limit: 3,
            },
        ),
        (
            "offset side",
            |b| b.offs[1][1] = 5,
            Error::BadReference {
                record: "offset",
                index: 1,
                field: "side",
                value: 5,
                limit: 2,
            },
        ),
        (
            "node front child",
            |b| b.nodes[0][1] = 9,
            Error::BadReference {
                record: "node",
                index: 0,
                field: "fore",
                value: 9,
                limit: 3,
            },
        ),
        (
            "node side",
            |b| b.nodes[0][0] = 2,
            Error::BadReference {
                record: "node",
                index: 0,
                field: "side",
                value: 2,
                limit: 2,
            },
        ),
        (
            "path next",
            |b| b.paths[0].pi = 2,
            Error::BadReference {
                record: "path",
                index: 0,
                field: "next",
                value: 2,
                limit: 2,
            },
        ),
        (
            "path next negative",
            |b| b.paths[1].pi = -1,
            Error::BadReference {
                record: "path",
                index: 1,
                field: "next",
                value: -1,
                limit: 2,
            },
        ),
        (
            "body path",
            |b| b.bodies[1][0] = 2,
            Error::BadReference {
                record: "body",
                index: 1,
                field: "path",
                value: 2,
                limit: 2,
            },
        ),
        (
            "body node",
            |b| b.bodies[0][2] = 3,
            Error::BadReference {
                record: "body",
                index: 0,
                field: "node",
                value: 3,
                limit: 3,
            },
        ),
        (
            "switch path",
            |b| b.swchs[0].pi = 5,
            Error::BadReference {
                record: "switch",
                index: 0,
                field: "path",
                value: 5,
                limit: 2,
            },
        ),
        (
            "billboard material",
            |b| b.bills[0][1] = 2,
            Error::BadReference {
                record: "billboard",
                index: 0,
                field: "material",
                value: 2,
                limit: 2,
            },
        ),
        (
            "pool entry used as a lump vertex",
            |b| b.pool[1] = 4,
            Error::BadReference {
                record: "lump",
                index: 0,
                field: "vertex",
                value: 4,
                limit: 4,
            },
        ),
        (
            "pool entry used as a body triangle",
            |b| b.pool[8] = -1,
            Error::BadReference {
                record: "body",
                index: 1,
                field: "geom",
                value: -1,
                limit: 2,
            },
        ),
    ];
    for (name, mutate, expected) in cases {
        let mut b = sample_v7();
        mutate(&mut b);
        assert_eq!(load_err(&b), expected, "case: {name}");
    }
}

#[test]
fn ranges_that_leave_their_array_are_rejected() {
    type Mutation = fn(&mut Builder);
    let cases: Vec<(&str, Mutation, Error)> = vec![
        (
            "lump vertices past the pool",
            |b| {
                b.lumps[0][1] = 7;
                b.lumps[0][2] = 3;
            },
            Error::BadRange {
                record: "lump",
                index: 0,
                field: "vertices",
                start: 7,
                count: 3,
                limit: 9,
            },
        ),
        (
            "negative lump count",
            |b| b.lumps[1][4] = -1,
            Error::BadRange {
                record: "lump",
                index: 1,
                field: "edges",
                start: 0,
                count: -1,
                limit: 9,
            },
        ),
        (
            "negative lump start",
            |b| b.lumps[1][7] = -2,
            Error::BadRange {
                record: "lump",
                index: 1,
                field: "sides",
                start: -2,
                count: 1,
                limit: 9,
            },
        ),
        (
            "node lumps",
            |b| {
                b.nodes[2][3] = 1;
                b.nodes[2][4] = 5;
            },
            Error::BadRange {
                record: "node",
                index: 2,
                field: "lumps",
                start: 1,
                count: 5,
                limit: 2,
            },
        ),
        (
            "body lumps",
            |b| {
                b.bodies[0][3] = 1;
                b.bodies[0][4] = 2;
            },
            Error::BadRange {
                record: "body",
                index: 0,
                field: "lumps",
                start: 1,
                count: 2,
                limit: 2,
            },
        ),
        (
            "body triangles",
            |b| {
                b.bodies[1][5] = 8;
                b.bodies[1][6] = 2;
            },
            Error::BadRange {
                record: "body",
                index: 1,
                field: "geoms",
                start: 8,
                count: 2,
                limit: 9,
            },
        ),
        (
            "start plus count overflows",
            |b| {
                b.lumps[0][1] = i32::MAX;
                b.lumps[0][2] = i32::MAX;
            },
            Error::BadRange {
                record: "lump",
                index: 0,
                field: "vertices",
                start: i32::MAX,
                count: i32::MAX,
                limit: 9,
            },
        ),
    ];
    for (name, mutate, expected) in cases {
        let mut b = sample_v7();
        mutate(&mut b);
        assert_eq!(load_err(&b), expected, "case: {name}");
    }
}

#[test]
fn an_empty_range_may_start_anywhere() {
    // The C code never touches an empty range, so neither does this port.
    let mut b = sample_v7();
    b.lumps[1][3] = 12345; // e0 of a lump whose ec is 0
    b.bodies[2][5] = 99; // g0 of a body whose gc is 0
    assert!(Sol::from_bytes(&b.build()).is_ok());
}

#[test]
fn minus_one_means_none_for_body_paths_and_switch_paths() {
    let mut b = sample_v7();
    b.swchs[0].pi = -1;
    b.bodies[2][0] = -1;
    let sol = load(&b);
    assert_eq!(sol.switches[0].path, None);
    // pj = 1 with pi = -1 stays as written.
    assert_eq!((sol.bodies[2].path, sol.bodies[2].path_alt), (None, Some(1)));
}

#[test]
fn non_finite_coordinates_are_rejected() {
    let mut b = sample_v7();
    b.verts[2][1] = f32::NAN;
    assert_eq!(
        load_err(&b),
        Error::NonFinite {
            record: "vertex",
            index: 2,
            field: "position"
        }
    );

    let mut b = sample_v7();
    b.sides[0].1 = f32::INFINITY;
    assert!(matches!(
        load_err(&b),
        Error::NonFinite {
            record: "side",
            index: 0,
            ..
        }
    ));

    let mut b = sample_v7();
    b.paths[0].t = f32::NEG_INFINITY;
    assert!(matches!(
        load_err(&b),
        Error::NonFinite {
            record: "path",
            ..
        }
    ));
}

#[test]
fn bad_dictionary_strings_are_rejected() {
    let cases: Vec<(&str, Vec<u8>, [i32; 2], Error)> = vec![
        (
            "key offset past the block",
            b"k\0v\0".to_vec(),
            [9, 2],
            Error::BadString {
                entry: 0,
                part: "key",
                offset: 9,
            },
        ),
        (
            "negative value offset",
            b"k\0v\0".to_vec(),
            [0, -1],
            Error::BadString {
                entry: 0,
                part: "value",
                offset: -1,
            },
        ),
        (
            "value without a terminator",
            b"k\0v".to_vec(),
            [0, 2],
            Error::BadString {
                entry: 0,
                part: "value",
                offset: 2,
            },
        ),
        (
            "offset at the very end of the block",
            b"k\0".to_vec(),
            [0, 2],
            Error::BadString {
                entry: 0,
                part: "value",
                offset: 2,
            },
        ),
    ];
    for (name, block, entry, expected) in cases {
        let mut b = Builder::empty(7);
        b.dict_override = Some((block, vec![entry]));
        assert_eq!(load_err(&b), expected, "case: {name}");
    }
}

#[test]
fn bsp_cycles_are_rejected_but_shared_children_are_not() {
    // node 0 -> node 1 -> node 0
    let mut b = sample_v7();
    b.nodes[1] = [0, 0, -1, 0, 1];
    assert_eq!(load_err(&b), Error::NodeCycle { node: 0 });

    // node 2 is its own child
    let mut b = sample_v7();
    b.nodes[2] = [0, -1, 2, 1, 1];
    assert_eq!(load_err(&b), Error::NodeCycle { node: 2 });

    // A diamond: both children of the root are the same leaf. Not a cycle.
    let mut b = sample_v7();
    b.nodes[0] = [0, 1, 1, 0, 0];
    assert!(Sol::from_bytes(&b.build()).is_ok());
}

#[test]
fn a_very_deep_node_chain_does_not_overflow_the_stack() {
    let mut b = Builder::empty(7);
    b.sides = vec![([0.0, 1.0, 0.0], 0.0)];
    let depth = 200_000_i32;
    b.nodes = (0..depth)
        .map(|n| {
            let next = if n + 1 < depth { n + 1 } else { -1 };
            [0, next, -1, 0, 0]
        })
        .collect();
    let sol = load(&b);
    assert_eq!(sol.nodes.len(), depth as usize);
}
