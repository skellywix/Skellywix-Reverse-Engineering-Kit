"""The exact dependency archives (`dependencies.tgz`) approved for the Neverball Windows rebuild profile.

Both hold the same 7,009 vendored crate files, verified file for file against the frozen `desktop/Cargo.lock` closure.
They differ only in tar/gzip packaging, which a SHA-256 over the archive bytes depends on:

* PORTABLE_SHA256: the canonical archive `tools/prepare_neverball.py` writes from the updater-installed crates (188,297,268 bytes).
  A clean install uses it; no private file is needed.
* HISTORICAL_SHA256: the archive an earlier candidate release carried. An existing install that still holds it keeps its
  original binding.

A digest is approved only by being listed here, in reviewed source. Nothing a browser sends, nothing generated at install time,
and no tree that merely holds the same files is a substitute. `game_jobs.verify_release` still compares the release's archive
to the single digest the run is bound to, and every run journals and packages that digest.
"""

PORTABLE_SHA256 = '22e12932d194701a8652d4baa28af6513d77289aae4bed2aa06fd9f75c553d1f'
HISTORICAL_SHA256 = '88a960b6c75d5b303ac2b636b77681cfd1e4f4782434c5a498c1c2c9b5ce8b84'
APPROVED = (PORTABLE_SHA256, HISTORICAL_SHA256)
