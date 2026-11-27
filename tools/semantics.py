"""Analyze original source, measured behavior, or both, without running a target."""

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import sys
import warnings
from itertools import groupby
from pathlib import Path
from typing import NamedTuple


MAX_BYTES = 1024 * 1024
MAX_ROWS = 1024
LABEL = re.compile(r"[a-z][a-z0-9-]{0,31}")
SHA256 = re.compile(r"[0-9a-f]{64}")
EVENT = {"state", "action", "input", "next_state", "output"}
TRACE_BASES = ("black-box", "source-informed", "unspecified")
Scalar = int | str | None


class Stimulus(NamedTuple):
    state: str
    action: str
    input: Scalar

    def sort_key(self):
        # JSON null, integers and labels have no shared ordering in Python.
        rank = 0 if self.input is None else 1 if isinstance(self.input, int) else 2
        return self.state, self.action, rank, self.input


class Effect(NamedTuple):
    next_state: str
    output: Scalar


class Witness(NamedTuple):
    effect: Effect
    rows: list[int]


class Study(NamedTuple):
    interface: str
    artifact_sha256: str
    trace_sha256: str
    cases: dict[Stimulus, Witness]
    basis: str


def unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("duplicate JSON field")
    return fields


def bounded_integer(text: str) -> int:
    # Includes coefficients derived from signed 32-bit coordinates.
    if len(text) > 21:
        raise ValueError("integer is too large")
    return int(text)


def reject_number(_):
    raise ValueError("fractional or nonfinite JSON number")


def read_bytes(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("input must be a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"input exceeds {limit // MAX_BYTES} MiB")
    return data


def read_document(path: Path, limit: int = MAX_BYTES) -> tuple[dict, str]:
    data = read_bytes(path, limit)
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=unique_fields,
                              parse_int=bounded_integer, parse_float=reject_number, parse_constant=reject_number)
    except UnicodeDecodeError as exc:
        raise ValueError("input must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at line {exc.lineno}, column {exc.colno}") from exc
    except RecursionError as exc:
        raise ValueError("JSON nesting is too deep") from exc
    if not isinstance(document, dict):
        raise ValueError("expected a JSON object")
    return document, hashlib.sha256(data).hexdigest()


def fields(value, names: set[str], where: str) -> None:
    if not isinstance(value, dict) or value.keys() != names:
        raise ValueError(f"{where}: expected exactly {', '.join(sorted(names))}")


def label(value, where: str) -> str:
    if not isinstance(value, str) or not LABEL.fullmatch(value):
        raise ValueError(f"{where}: expected a short lowercase interface label")
    return value


def sha256_field(value) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError("expected a lowercase SHA-256 digest")
    return value


def scalar(value, where: str) -> Scalar:
    if value is None or (type(value) is int and -(2**31) <= value < 2**31):
        return value
    if isinstance(value, str):
        return label(value, where)
    raise ValueError(f"{where}: expected null, a signed 32-bit integer or a short label")


def bounded_rows(value) -> list:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ROWS:
        raise ValueError(f"expected 1 to {MAX_ROWS} observations")
    return value


def event(row, idx: int, names: set[str]) -> tuple[Stimulus, Effect]:
    where = f"row {idx}"
    fields(row, names, where)
    return (Stimulus(label(row["state"], where), label(row["action"], where), scalar(row["input"], where)),
            Effect(label(row["next_state"], where), scalar(row["output"], where)))


def read_trace(path: Path) -> Study:
    doc, digest = read_document(path)
    basis = doc.pop("basis", "unspecified")
    fields(doc, {"format", "interface", "artifact_sha256", "observations"}, "trace")
    if doc["format"] != "behavior-trace-v1" or basis not in TRACE_BASES:
        raise ValueError("expected behavior-trace-v1 with measured observations")
    interface = label(doc["interface"], "interface")
    artifact = sha256_field(doc["artifact_sha256"])
    cases = {}
    for i, row in enumerate(bounded_rows(doc["observations"]), 1):
        stimulus, effect = event(row, i, EVENT)
        previous = cases.get(stimulus)
        if previous is None:
            cases[stimulus] = Witness(effect, [i])
        elif previous.effect != effect:
            raise ValueError(f"rows {previous.rows[0]} and {i} disagree for the same state, action and input")
        else:
            previous.rows.append(i)
    return Study(interface, artifact, digest, cases, basis)


def hypotheses(cases: dict[Stimulus, Witness], ordered: list[Stimulus]) -> list[dict]:
    """Only propose y = m*x + b when the whole state/action group supports it."""
    rules = []
    for (state, action), members in groupby(ordered, key=lambda s: (s.state, s.action)):
        group = list(members)
        if len(group) < 3 or any(type(s.input) is not int or type(cases[s].effect.output) is not int for s in group):
            continue
        first, second = group[:2]
        base = cases[first].effect
        slope, remainder = divmod(cases[second].effect.output - base.output, second.input - first.input)
        if remainder:
            continue
        offset = base.output - slope * first.input
        if all(cases[s].effect == Effect(base.next_state, slope * s.input + offset) for s in group):
            rules.append({"kind": "integer-affine", "state": state, "action": action, "next_state": base.next_state,
                          "multiplier": slope, "offset": offset, "min_input": first.input,
                          "max_input": group[-1].input, "distinct_inputs": len(group)})
    return rules


def make_spec(study: Study) -> dict:
    ordered = sorted(study.cases, key=Stimulus.sort_key)
    return {"format": "behavior-spec-v1", "interface": study.interface,
            "reference": {"basis": study.basis, "artifact_sha256": study.artifact_sha256,
                          "trace_sha256": study.trace_sha256},
            "observed": [s._asdict() | study.cases[s].effect._asdict() | {"rows": study.cases[s].rows} for s in ordered],
            "hypotheses": hypotheses(study.cases, ordered)}


def read_spec(path: Path) -> tuple[Study, str]:
    doc, spec_digest = read_document(path, 4 * MAX_BYTES)
    if doc.get("format") == "source-analysis-v1":
        raise ValueError("source review has no observed cases; supply an observation trace to compare")
    # Source annotations are analyst context, not an oracle. Never consult
    # their return values, code identifiers, or paths when judging a case.
    doc.pop("source_analysis", None)
    fields(doc, {"format", "interface", "reference", "observed", "hypotheses"}, "spec")
    ref = doc["reference"]
    fields(ref, {"basis", "artifact_sha256", "trace_sha256"}, "reference")
    if doc["format"] != "behavior-spec-v1" or ref["basis"] not in TRACE_BASES:
        raise ValueError("expected behavior-spec-v1 with an observation reference")
    study = Study(label(doc["interface"], "interface"), sha256_field(ref["artifact_sha256"]),
                  sha256_field(ref["trace_sha256"]), {}, ref["basis"])
    seen = set()
    for i, row in enumerate(bounded_rows(doc["observed"]), 1):
        stimulus, effect = event(row, i, EVENT | {"rows"})
        if stimulus in study.cases:
            raise ValueError(f"observed case {i}: duplicate stimulus")
        indices = bounded_rows(row["rows"])
        last = 0
        for index in indices:
            if type(index) is not int or not last < index <= MAX_ROWS or index in seen:
                raise ValueError(f"observed case {i}: invalid or reused witness row")
            seen.add(index)
            last = index
        study.cases[stimulus] = Witness(effect, indices)
    if len(seen) != max(seen):
        raise ValueError("missing witness row in handoff")
    expected = hypotheses(study.cases, sorted(study.cases, key=Stimulus.sort_key))
    supplied = doc["hypotheses"]
    # Compare flat, known fields by JSON type: Python considers true equal to 1.
    if not isinstance(supplied, list) or len(supplied) != len(expected) or not all(
        isinstance(got, dict) and got.keys() == rule.keys()
        and all(type(got[k]) is type(v) and got[k] == v for k, v in rule.items())
        for got, rule in zip(supplied, expected)
    ):
        raise ValueError("handoff hypotheses do not follow from its observed cases")
    return study, spec_digest


def compare(reference: Study, candidate: Study, spec_digest: str) -> dict:
    if reference.interface != candidate.interface:
        raise ValueError("reference and candidate interface labels differ")
    matched = 0
    missing, mismatches = [], []
    for stimulus in sorted(reference.cases, key=Stimulus.sort_key):
        observed = candidate.cases.get(stimulus)
        expected = reference.cases[stimulus].effect
        if observed is None:
            missing.append(stimulus._asdict())
        elif observed.effect != expected:
            mismatches.append(stimulus._asdict() | {"expected": expected._asdict(), "actual": observed.effect._asdict()})
        else:
            matched += 1
    return {"format": "behavior-check-v1", "interface": reference.interface,
            "spec_sha256": spec_digest, "reference_artifact_sha256": reference.artifact_sha256,
            "candidate_artifact_sha256": candidate.artifact_sha256, "candidate_trace_sha256": candidate.trace_sha256,
            "same_artifact": reference.artifact_sha256 == candidate.artifact_sha256,
            "reference_basis": reference.basis, "candidate_basis": candidate.basis,
            "status": "mismatch" if mismatches else "incomplete" if missing else "matches-observed",
            "matched": matched, "missing": missing, "mismatches": mismatches,
            "unassessed": len(candidate.cases.keys() - reference.cases.keys())}


def affine(node: ast.AST, depth: int = 0) -> tuple[str | None, int, int] | None:
    """Normalize a one-variable expression under plain integer arithmetic."""
    if depth > 16:
        return None
    match node:
        case ast.Name(id=name):
            return name, 1, 0
        case ast.Constant(value=n) if type(n) is int and n.bit_length() <= 63:
            return None, 0, n
        case ast.UnaryOp(op=(ast.UAdd() | ast.USub()) as op, operand=child):
            term = affine(child, depth + 1)
            if term is None:
                return None
            name, a, b = term
            return (name, -a, -b) if isinstance(op, ast.USub) else term
        case ast.BinOp(left=left, op=(ast.Add() | ast.Sub() | ast.Mult()) as op, right=right):
            lhs, rhs = affine(left, depth + 1), affine(right, depth + 1)
            if lhs is None or rhs is None:
                return None
            x, a, b = lhs
            y, c, d = rhs
            if x and y and (x != y or isinstance(op, ast.Mult)):
                return None
            if isinstance(op, ast.Mult):
                m, k = a * d + b * c, b * d
            elif isinstance(op, ast.Sub):
                m, k = a - c, b - d
            else:
                m, k = a + c, b + d
            if max(m.bit_length(), k.bit_length()) <= 63:
                return x or y, m, k
    return None


def dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


def expression(node: ast.AST | None, depth: int = 0) -> dict:
    if node is None or isinstance(node, ast.Constant) and node.value is None:
        return {"literal": None}
    if isinstance(node, ast.Name):
        return {"name": node.id}
    term = affine(node)
    if term is not None:
        name, multiplier, offset = term
        if name is None:
            return {"integer": offset}
        return {"integer_affine": {"variable": name, "multiplier": multiplier, "offset": offset}}
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return {"literal": node.value}
        if isinstance(node.value, (bytes, str)):
            if len(node.value) <= 64:
                return {"text": node.value} if isinstance(node.value, str) else {"bytes_hex": node.value.hex()}
            return {"literal_type": type(node.value).__name__, "length": len(node.value)}
    if isinstance(node, (ast.Tuple, ast.List)) and depth < 3 and len(node.elts) <= 8:
        return {type(node).__name__.lower(): [expression(item, depth + 1) for item in node.elts]}
    if isinstance(node, ast.Call) and (callee := dotted(node.func)):
        call = {"call": callee}
        if depth < 3 and not node.keywords and len(node.args) <= 4:
            call["arguments"] = [expression(arg, depth + 1) for arg in node.args]
        return call
    return {"unresolved": type(node).__name__}


def predicate(node: ast.AST) -> dict:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return {"not": expression(node.operand)}
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        return {"op": type(node.ops[0]).__name__.lower(), "left": expression(node.left),
                "right": expression(node.comparators[0])}
    return expression(node)


def source_fact(node: ast.AST) -> dict | None:
    if isinstance(node, ast.If):
        return {"kind": "branch", "predicate": predicate(node.test)}
    if isinstance(node, ast.Return):
        return {"kind": "return", "value": expression(node.value)}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        method = node.func.attr
        if method in {"read", "read1", "readline", "write", "flush"}:
            return {"kind": "stream-call", "method": method, "receiver": dotted(node.func.value)}
        if method == "count" and len(node.args) == 1 and not node.keywords:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, bytes) and len(arg.value) == 1:
                return {"kind": "byte-count-call", "byte": arg.value[0], "receiver": dotted(node.func.value)}
    return None


def source_report(path: Path, language: str | None) -> dict:
    data = read_bytes(path, MAX_BYTES)
    python = language == "python" or path.suffix.lower() in {".py", ".pyw", ".pyi"}
    report = {"format": "source-analysis-v1", "basis": "source-review",
              "source_sha256": hashlib.sha256(data).hexdigest(), "language": "python" if python else None}
    if not python:
        return report | {"status": "identity-only", "reason": "No static parser selected for this file."}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(data, filename="<original>")
    except SyntaxError as exc:
        raise ValueError(f"invalid Python source at line {exc.lineno or 1}") from exc
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise ValueError("could not parse Python source") from exc

    facts = []
    size = 0
    stack = [(tree, "<module>", None)]
    while stack:
        node, scope, enclosing = stack.pop()
        fact = source_fact(node)
        if fact is not None:
            fact.update(scope=scope, line=node.lineno)
            if enclosing is not None:
                fact["enclosing_if"] = enclosing
            size += len(json.dumps(fact, ensure_ascii=True))
            if size > MAX_BYTES // 4:
                return report | {"status": "partial", "facts": facts, "reason": "Source fact output limit reached."}
            facts.append(fact)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            local = node.name if scope == "<module>" else f"{scope}.{node.name}"
            stack.extend((child, local, None) for child in reversed(node.body))
        elif isinstance(node, ast.If):
            for arm, body in (("else", node.orelse), ("then", node.body)):
                branch = {"line": node.lineno, "arm": arm}
                stack.extend((child, scope, branch) for child in reversed(body))
            stack.append((node.test, scope, enclosing))
        else:
            stack.extend((child, scope, enclosing) for child in reversed(list(ast.iter_child_nodes(node))))
    return report | {"status": "analyzed", "facts": facts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze", help="analyze source, observations, or both")
    analyze.add_argument("trace", nargs="?", type=Path, help="measured behavior trace")
    analyze.add_argument("--source", type=Path, help="original source file (parsed as data, never imported)")
    analyze.add_argument("--language", choices=["python"], help="select the parser, e.g. for an extensionless file")
    analyze.add_argument("--out", type=Path, help="create a new spec file instead of printing JSON")
    check = commands.add_parser("check", help="check candidate observations against a reviewed spec")
    check.add_argument("spec", type=Path)
    check.add_argument("candidate", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "analyze":
            if args.trace is None and args.source is None:
                raise ValueError("supply a trace, --source FILE, or both")
            if args.language and args.source is None:
                raise ValueError("--language requires --source FILE")
            if args.trace is None:
                spec = source_report(args.source, args.language)
            else:
                spec = make_spec(read_trace(args.trace))
                if args.source is not None:
                    spec["source_analysis"] = source_report(args.source, args.language)
            if args.out is None:
                print(json.dumps(spec, indent=2))
            else:
                with args.out.open("x", encoding="utf-8", newline="\n") as out:
                    json.dump(spec, out, indent=2)
                    out.write("\n")
            return 0
        reference, digest = read_spec(args.spec)
        report = compare(reference, read_trace(args.candidate), digest)
        print(json.dumps(report, indent=2))
        return 0 if report["status"] == "matches-observed" else 1
    except ValueError as exc:
        parser.exit(2, f"semantics: {exc}\n")
    except OSError as exc:
        parser.exit(2, f"semantics: file or stream error: {exc.strerror}\n")


if __name__ == "__main__":
    sys.exit(main())
