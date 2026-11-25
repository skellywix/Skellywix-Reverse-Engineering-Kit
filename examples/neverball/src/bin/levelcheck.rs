// SPDX-License-Identifier: GPL-2.0-or-later
//
// Diagnostic for the Rust port of the Neverball 1.6.0 level loader. Not the
// game. Copyright notices and license: see NOTICE.md and COPYING.

//! `levelcheck`: loads every level named by an extracted Neverball data
//! directory's sets and prints what it found.
//!
//! ```text
//! levelcheck DATA_DIR [--expect N]
//! ```
//!
//! `DATA_DIR` holds `sets.txt`, the `set-*.txt` files and the `map-*`
//! directories with compiled `.sol` files, extracted from the original game's
//! data package. Exit status: 0 if every distinct level loaded and the count
//! equals `--expect` (default 183, the 1.6.0 release); 1 if a level failed or
//! the count differs; 2 on a usage error or an unreadable set list.

use std::path::PathBuf;
use std::process::ExitCode;

use neverball::level::{Level, Summary};
use neverball::set::{distinct_levels, load_all};

/// Distinct levels in the Neverball 1.6.0 release data.
const EXPECTED_LEVELS: usize = 183;

const USAGE: &str = "usage: levelcheck DATA_DIR [--expect N]\n\
\n\
Loads every level listed by DATA_DIR/sets.txt and DATA_DIR/set-*.txt and\n\
prints one line per distinct level, then a summary.\n\
\n\
  --expect N   required number of distinct levels (default 183)\n";

struct Args {
    data_dir: PathBuf,
    expect: usize,
}

enum Parsed {
    Run(Args),
    Help,
}

fn parse_args() -> Result<Parsed, String> {
    let mut data_dir = None;
    let mut expect = EXPECTED_LEVELS;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-h" | "--help" => return Ok(Parsed::Help),
            "--expect" => {
                let value = args.next().ok_or("--expect needs a number")?;
                expect = value
                    .parse()
                    .map_err(|_| format!("--expect value {value:?} is not a number"))?;
            }
            flag if flag.starts_with("--") => return Err(format!("unknown option {flag}")),
            _ => {
                if data_dir.replace(PathBuf::from(&arg)).is_some() {
                    return Err("more than one DATA_DIR given".to_owned());
                }
            }
        }
    }
    let data_dir = data_dir.ok_or("missing DATA_DIR")?;
    Ok(Parsed::Run(Args { data_dir, expect }))
}

fn describe(level: &Level) -> String {
    let Summary {
        triangles,
        lumps,
        nodes,
        paths,
        bodies,
        moving_bodies,
        coins,
        coin_value,
        goals,
        jumps,
        switches,
        balls,
        views,
        ..
    } = level.summary();
    format!(
        "v{} tris={triangles} lumps={lumps} nodes={nodes} bodies={bodies}({moving_bodies} moving) \
         paths={paths} coins={coins}(worth {coin_value}) goals={goals} jumps={jumps} \
         switches={switches} balls={balls} views={views} goal={} time={:.2}s",
        level.sol.version.number(),
        level.info.goal,
        f64::from(level.info.time) / 100.0,
    )
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(Parsed::Run(args)) => args,
        Ok(Parsed::Help) => {
            print!("{USAGE}");
            return ExitCode::SUCCESS;
        }
        Err(message) => {
            eprintln!("levelcheck: {message}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    };

    let sets = match load_all(&args.data_dir) {
        Ok(sets) => sets,
        Err(error) => {
            eprintln!("levelcheck: cannot read the set list: {error}");
            return ExitCode::from(2);
        }
    };
    let listed: usize = sets.iter().map(|s| s.levels.len()).sum();
    let levels = distinct_levels(&sets);

    let mut loaded = 0_usize;
    let mut failed = 0_usize;
    let mut flagged = 0_usize;
    let mut totals = Summary::default();

    for name in &levels {
        match Level::load(args.data_dir.join(name)) {
            Ok(level) => {
                loaded += 1;
                println!("ok    {name}  {}", describe(&level));
                let concerns = level.concerns();
                if !concerns.is_empty() {
                    flagged += 1;
                    for concern in concerns {
                        println!("      concern: {concern}");
                    }
                }
                let s = level.summary();
                totals.triangles += s.triangles;
                totals.lumps += s.lumps;
                totals.bodies += s.bodies;
                totals.paths += s.paths;
                totals.coins += s.coins;
                totals.goals += s.goals;
                totals.jumps += s.jumps;
                totals.switches += s.switches;
            }
            Err(error) => {
                failed += 1;
                println!("FAIL  {name}  {error}");
            }
        }
    }

    println!();
    println!("sets: {}", sets.len());
    for set in &sets {
        println!("  {} ({}): {} levels", set.file, set.name, set.levels.len());
    }
    println!("level entries in sets: {listed}");
    println!("distinct levels: {} (expected {})", levels.len(), args.expect);
    println!("loaded: {loaded}  failed: {failed}  with concerns: {flagged}");
    println!(
        "totals over loaded levels: tris={} lumps={} bodies={} paths={} coins={} goals={} \
         jumps={} switches={}",
        totals.triangles,
        totals.lumps,
        totals.bodies,
        totals.paths,
        totals.coins,
        totals.goals,
        totals.jumps,
        totals.switches,
    );

    let count_ok = levels.len() == args.expect;
    if !count_ok {
        println!(
            "RESULT: FAIL: found {} distinct levels, expected {}",
            levels.len(),
            args.expect
        );
    }
    if failed > 0 {
        println!("RESULT: FAIL: {failed} level(s) did not load");
    }
    if count_ok && failed == 0 {
        println!("RESULT: OK: all {loaded} distinct levels loaded");
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
