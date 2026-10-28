# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pick one shard of the test files, so CI can run the shards in parallel jobs (.github/workflows/ci.yml).

``python -m tests.support.shard 2/8`` prints the files of shard 2 of 8, one per line. Pass them to
pytest to run just that shard. A shard names its files, rather than deselecting tests after
collection, because every xdist worker imports every file it is given, and under coverage that import
is a large part of a short run.

Files go to the lightest shard so far, heaviest first. ``shard_weights.json`` holds each file's run
time in seconds. A stale or missing entry only unbalances the shards, never skips a test: a file with
no entry weighs what its size predicts. To refresh the weights, with coverage on as in CI:

    uv run pytest -n 4 --cov --cov-report= -q --durations=0 --durations-min=0 \\
      | uv run python -m tests.support.shard --record"""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
WEIGHTS = Path(__file__).with_name("shard_weights.json")


def parse(spec: str) -> tuple[int, int]:
    """``"2/8"`` is shard 2 of 8. Raises ``ValueError`` for anything else."""
    index, slash, total = spec.partition("/")
    if not (slash and index.isdecimal() and total.isdecimal()) or not 1 <= int(index) <= int(total):
        raise ValueError(f"The shard must look like 2/8 (shard, then total), not {spec!r}")
    return int(index), int(total)


def collectable() -> list[Path]:
    """Every file pytest collects tests from, by its default patterns."""
    return sorted({*TESTS.rglob("test_*.py"), *TESTS.rglob("*_test.py")})


def weigh(files: list[Path], known: dict[str, float]) -> dict[Path, float]:
    """Each file's weight in seconds: its recorded time, or else its size at the usual seconds per byte."""
    root = TESTS.parent
    per_byte = [
        known[name] / size for f in files if (name := str(f.relative_to(root))) in known and (size := f.stat().st_size)
    ]
    rate = statistics.median(per_byte) if per_byte else 1.0
    return {f: known.get(str(f.relative_to(root)), f.stat().st_size * rate) for f in files}


def shards(files: list[Path], total: int, known: dict[str, float] | None = None) -> list[list[Path]]:
    """``files`` split into ``total`` groups of about equal weight, the same split every time."""
    weights = weigh(files, json.loads(WEIGHTS.read_text()) if known is None else known)
    groups: list[list[Path]] = [[] for _ in range(total)]
    loads = [0.0] * total
    for file in sorted(files, key=lambda f: (-weights[f], f.name)):
        lightest = loads.index(min(loads))
        groups[lightest].append(file)
        loads[lightest] += weights[file]
    return groups


def record(report: str) -> dict[str, float]:
    """Seconds per test file from the ``--durations=0`` lines of a pytest report."""
    seconds: defaultdict[str, float] = defaultdict(float)
    for line in report.splitlines():
        if match := re.match(r"([\d.]+)s (?:call|setup|teardown)\s+(tests/\S+?)::", line):
            seconds[match[2]] += float(match[1])
    return {name: round(total, 1) for name, total in sorted(seconds.items())}


def main(argv: list[str]) -> int:
    if argv == ["--record"]:
        WEIGHTS.write_text(json.dumps(record(sys.stdin.read()), indent=2) + "\n")
        return 0
    if len(argv) != 1:
        print("usage: python -m tests.support.shard 2/8 | --record", file=sys.stderr)
        return 2
    index, total = parse(argv[0])
    root = TESTS.parent
    print("\n".join(sorted(str(file.relative_to(root)) for file in shards(collectable(), total)[index - 1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
