# Fourmilab Base64 1.5: reference reconstruction

This is the earlier, source-assisted Rust reconstruction of [John Walker’s public-domain Base64 1.5](https://www.fourmilab.ch/webtools/base64/). It targets the Linux CLI, not the historical Windows executable. It preserves the observed CLI behavior rather than enforcing a strict Base64 specification. It is not the source used by the workspace’s model builder. To inspect that source, download the package from its verified run.

On Linux, run `cargo test --locked --offline` and `cargo build --release --locked --offline` in this directory. To compare the two programs, run `LC_ALL=C python3 compare.py /absolute/path/to/reference/base64 "$PWD/target/release/base64"`. The harness compares exit status, stdout, stderr, and file effects. It exercises 2,557 cases; agreement does not prove equivalence for every input or platform.

Keep input and output files separate: the reference and the Rust example both truncate the output file. The [compatibility service](../../fourmilab/README.md) checks the original executable against its pinned digest. A different compiler can produce different candidate bytes.
