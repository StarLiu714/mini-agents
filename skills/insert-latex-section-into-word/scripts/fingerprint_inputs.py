#!/usr/bin/env python3
"""Snapshot input files and detect concurrent edits before DOCX delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record(path: Path) -> dict:
    resolved = path.resolve()
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256(resolved),
    }


def word_lock(path: Path) -> Path | None:
    if path.suffix.casefold() != ".docx" or len(path.name) < 3:
        return None
    candidate = path.with_name("~$" + path.name[2:])
    return candidate if candidate.exists() else None


def snapshot(output: Path, inputs: list[Path], allow_locks: bool) -> None:
    if not inputs:
        raise ValueError("Provide at least one input file")
    output_resolved = output.resolve()
    input_paths = {path.resolve() for path in inputs}
    if output_resolved in input_paths:
        raise RuntimeError(
            f"Manifest output must not overwrite an input file: {output_resolved}"
        )
    locks = [lock for path in inputs if (lock := word_lock(path)) is not None]
    if locks and not allow_locks:
        raise RuntimeError(
            "Word lock files indicate active editing: " + ", ".join(str(lock) for lock in locks)
        )
    data = {"files": [record(path) for path in inputs]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {output.resolve()} ({len(inputs)} files)")


def check(manifest: Path, allow_locks: bool) -> None:
    data = json.loads(manifest.read_text())
    failures = []
    for expected in data.get("files", []):
        path = Path(expected["path"])
        lock = word_lock(path)
        if lock is not None and not allow_locks:
            failures.append(f"Word lock file present: {lock}")
        if not path.exists():
            failures.append(f"missing: {path}")
            continue
        actual = record(path)
        differences = [
            key for key in ("size", "mtime_ns", "sha256")
            if actual[key] != expected.get(key)
        ]
        if differences:
            failures.append(f"changed ({', '.join(differences)}): {path}")
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        raise SystemExit(1)
    print(f"PASS: {len(data.get('files', []))} input fingerprints match")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path, help="Write a new JSON fingerprint manifest")
    mode.add_argument("--check", type=Path, help="Check an existing fingerprint manifest")
    parser.add_argument("inputs", nargs="*", type=Path)
    parser.add_argument(
        "--allow-lock-files",
        action="store_true",
        help="Proceed despite Word lock files (unsafe in a shared editing workflow)",
    )
    args = parser.parse_args()
    try:
        if args.output:
            snapshot(args.output, args.inputs, args.allow_lock_files)
        else:
            if args.inputs:
                parser.error("Do not pass input paths with --check")
            check(args.check, args.allow_lock_files)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
