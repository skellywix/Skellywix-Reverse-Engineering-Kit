"""Fixed in-sandbox scripts. They are constants: nothing in a request is interpolated into them.

The host sends one tar on stdin: `run.sh` (one of the scripts below), optional helper files, and the project under
`src/`. The only per-run values reach the script as environment variables (`PROJECT_NAME`, already reduced to
`[A-Za-z0-9._-]`). Results leave through file descriptor 3 as a tar of `bin/*` and `toolchain.txt`; everything else the
tools print goes to stderr. Build commands run with descriptor 3 closed and stdin at /dev/null.

The scripts never execute the produced files. Compilers, build scripts, procedural macros, `make` rules and
`./configure` run inside the networkless sandbox; that is what compiling the project means.
"""

BOOTSTRAP = 'mkdir -p /work/in && cd /work/in && tar -xf - && exec sh ./run.sh'

# Shared by every compiling script. `build` is defined by the mode-specific part that follows.
_HEAD = r'''set -eu
umask 022
exec 3>&1 1>&2
SRC=/work/in/src
OUT=/work/out
fail() { printf 'PROJECT-BUILD-ERROR: %s\n' "$*" >&2; exit 64; }
need() {
  for t in "$@"; do
    command -v "$t" >/dev/null 2>&1 || fail "The pinned image has no '$t', which this project needs."
  done
}
need head od tr find sort cut uname
mkdir -p "$OUT/bin" /work/home /work/cargo /work/target /work/obj /work/tmp
'''

# Prints "elf" for a little-endian ELF executable or shared object, "ar" for a static archive, nothing otherwise.
_COLLECT = r'''kind() {
  h=$(head -c 18 "$1" 2>/dev/null | od -An -v -tx1 | tr -d ' \n') || h=
  case "$h" in
    7f454c46????????????????????????0[23]00) echo elf ;;
    213c617263683e0a*) echo ar ;;
  esac
}
collect_from() {
  root=$1
  shift
  find "$root" "$@" -type f -newer /work/stamp ! -path '*/CMakeFiles/*' ! -path '*/.git/*' | sort |
  {
    n=0
    while IFS= read -r f; do
      k=$(kind "$f")
      case "$k:$f" in
        elf:*|ar:*.a) ;;
        *) continue ;;
      esac
      [ "$n" -lt 64 ] || break
      n=$((n + 1))
      rel=${f#"$root"/}
      dest=$(printf '%s' "$rel" | tr -c 'A-Za-z0-9._+-' '_' | cut -c1-100)
      case "$dest" in .*) dest="_$dest" ;; esac
      [ ! -e "$OUT/bin/$dest" ] || dest="$n-$dest"
      cp "$f" "$OUT/bin/$dest"
    done
  }
}
'''

_TAIL = r'''touch /work/stamp
cd "$SRC"
build 3>&- </dev/null
set -- "$OUT"/bin/*
[ -e "$1" ] || fail "The build finished but produced no executable or library files to package."
toolchain 3>&- </dev/null > "$OUT/toolchain.txt"
tar -cf - -C "$OUT" bin toolchain.txt >&3
'''

_VERSION_HELPER = r'''ver() { "$@" 2>&1 | head -n 1; }
'''


def _compose(body, toolchain):
    return _HEAD + _COLLECT + _VERSION_HELPER + body + 'toolchain() {\n' + toolchain + '}\n' + _TAIL


RUSTC = _compose(r'''build() {
  need rustc cc
  [ -f main.rs ] || fail "The sandbox did not receive main.rs."
  rustc --edition 2021 -C opt-level=2 -C debuginfo=0 --crate-name project main.rs -o "$OUT/bin/$PROJECT_NAME" ||
    fail "rustc could not compile main.rs."
}
''', r'''  printf 'arch: %s\n' "$(uname -m)"
  printf 'rustc: %s\n' "$(ver rustc --version)"
  printf 'cc: %s\n' "$(ver cc --version)"
''')

CARGO = _compose(r'''build() {
  need cargo rustc cc
  rm -f rust-toolchain rust-toolchain.toml
  if [ -f Cargo.lock ]; then lock=--frozen; else lock=--offline; fi
  cargo build --release "$lock" --color never ||
    fail "cargo could not build this project offline. Vendored sources and a matching Cargo.lock are required."
  collect_from /work/target/release -maxdepth 1
}
''', r'''  printf 'arch: %s\n' "$(uname -m)"
  printf 'cargo: %s\n' "$(ver cargo --version)"
  printf 'rustc: %s\n' "$(ver rustc --version)"
  printf 'cc: %s\n' "$(ver cc --version)"
''')

CMAKE = _compose(r'''build() {
  need cmake make
  cmake -S . -B /work/build -DCMAKE_BUILD_TYPE=Release || fail "cmake could not configure this project offline."
  cmake --build /work/build -j 2 || fail "cmake could not build this project."
  collect_from /work/build
}
''', r'''  printf 'arch: %s\n' "$(uname -m)"
  printf 'cmake: %s\n' "$(ver cmake --version)"
  printf 'make: %s\n' "$(ver make --version)"
''')

MAKE = _compose(r'''build() {
  need make
  make -j 2 || fail "make failed."
  collect_from "$SRC"
}
''', r'''  printf 'arch: %s\n' "$(uname -m)"
  printf 'make: %s\n' "$(ver make --version)"
''')

CONFIGURE = _compose(r'''build() {
  need make sh
  sh ./configure || fail "./configure failed offline."
  make -j 2 || fail "make failed."
  collect_from "$SRC"
}
''', r'''  printf 'arch: %s\n' "$(uname -m)"
  printf 'make: %s\n' "$(ver make --version)"
''')

# Compiles every root-level source (or, if there are none, every source directly under src/) and links one program.
DIRECT = _compose(r'''build() {
  objs=
  count=0
  has_cxx=0
  for dir in . src; do
    for f in "$dir"/*.c "$dir"/*.cc "$dir"/*.cpp "$dir"/*.cxx; do
      [ -f "$f" ] || continue
      count=$((count + 1))
      o=/work/obj/$count.o
      case "$f" in
        *.c)
          need cc
          cc -O2 -pipe -I. -Iinclude -Isrc -c "$f" -o "$o" || fail "cc could not compile $f."
          ;;
        *)
          need c++
          has_cxx=1
          c++ -O2 -pipe -I. -Iinclude -Isrc -c "$f" -o "$o" || fail "c++ could not compile $f."
          ;;
      esac
      objs="$objs $o"
    done
    [ "$count" -eq 0 ] || break
  done
  [ "$count" -gt 0 ] || fail "No C or C++ source files were found at the top level or in src/."
  if [ "$has_cxx" -eq 1 ]; then
    c++ -o "$OUT/bin/$PROJECT_NAME" $objs -lm -lpthread || fail "c++ could not link the program."
  else
    cc -o "$OUT/bin/$PROJECT_NAME" $objs -lm -lpthread || fail "cc could not link the program."
  fi
}
''', r'''  printf 'arch: %s\n' "$(uname -m)"
  printf 'cc: %s\n' "$(ver cc --version)"
  printf 'c++: %s\n' "$(ver c++ --version)"
''')

# Python profile: parse (never import or run) every .py file with the pinned interpreter. Output only on stderr.
PYTHON_RUN = r'''set -eu
exec 3>&1 1>&2
cd /work/in/src
command -v python3 >/dev/null 2>&1 || {
  printf 'PROJECT-BUILD-ERROR: The pinned image has no python3.\n' >&2
  exit 64
}
exec python3 -I -B /work/in/check.py 3>&- </dev/null
'''

PYTHON_CHECK = r'''import ast
import os
import sys

problems = []
count = 0
for base, dirs, files in os.walk('.'):
    dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d != '__pycache__')
    for name in sorted(files):
        if not name.endswith('.py'):
            continue
        count += 1
        path = os.path.join(base, name)[2:]
        try:
            with open(path, 'rb') as handle:
                ast.parse(handle.read(), path)
        except SyntaxError as error:
            problems.append('%s line %s: %s' % (path, error.lineno, error.msg))
        except (ValueError, RecursionError, MemoryError) as error:
            problems.append('%s: %s' % (path, type(error).__name__))
version = '%d.%d.%d' % sys.version_info[:3]
if problems:
    for line in problems[:5]:
        sys.stderr.write('PROJECT-BUILD-ERROR: Python %s cannot parse %s\n' % (version, line))
    sys.exit(65)
sys.stderr.write('PROJECT-CHECK: python=%s files=%d\n' % (version, count))
'''

SCRIPTS = {'rustc': RUSTC, 'cargo': CARGO, 'cmake': CMAKE, 'make': MAKE, 'configure': CONFIGURE, 'direct': DIRECT,
           'python': PYTHON_RUN}
HELPERS = {'python': {'check.py': PYTHON_CHECK}}
