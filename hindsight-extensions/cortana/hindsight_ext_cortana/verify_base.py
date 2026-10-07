"""Check that a checkout's package directory is byte-identical to an installed wheel.

Behind ``hindsight-cortana verify-base``. It confirms the fork's base: every file the
installed wheel's RECORD lists under the package (for example ``hindsight_api/``) must
exist in the checkout's package directory with the same hash, and the checkout must hold
no package file the RECORD lacks. ``__pycache__`` is ignored on both sides because
bytecode is a local by-product, not part of the release.

It uses only the standard library, so it also runs as a script under any Python 3.9 or
later, without the engine installed:

    python3 -I verify_base.py <dist-info>/RECORD <checkout>/hindsight-api-slim/hindsight_api

Exit status: 0 when everything matches, 1 on any mismatch, missing or extra file,
2 on a usage error.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
from dataclasses import dataclass
from importlib.metadata import PathDistribution
from pathlib import Path
from typing import Optional, Sequence

PYCACHE = "__pycache__"


@dataclass(frozen=True)
class BaseVerification:
    """Outcome of comparing a package directory against a RECORD; paths in RECORD form."""

    package: str
    expected: int
    matched: int
    mismatched: tuple[str, ...]
    missing: tuple[str, ...]
    extra: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.expected > 0 and not (self.mismatched or self.missing or self.extra)


def _record_digest(path: Path, algorithm: str) -> str:
    """Hash a file the way RECORD stores it: urlsafe base64 without padding (PEP 376/427)."""
    digest = hashlib.new(algorithm, path.read_bytes()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_base(record: Path, package_dir: Path) -> BaseVerification:
    """Compare ``package_dir`` against the rows of ``record`` under ``package_dir.name``."""
    if record.name != "RECORD" or not record.is_file():
        raise ValueError(f"not a RECORD file: {record}")
    if not package_dir.is_dir():
        raise ValueError(f"not a directory: {package_dir}")

    package = package_dir.name
    rows = [
        row
        for row in PathDistribution(record.parent).files or []
        if row.parts and row.parts[0] == package and PYCACHE not in row.parts
    ]

    matched = 0
    mismatched: list[str] = []
    missing: list[str] = []
    for row in rows:
        path = package_dir.joinpath(*row.parts[1:])
        if not path.is_file():
            missing.append(str(row))
        elif row.hash is None or _record_digest(path, row.hash.mode) != row.hash.value:
            mismatched.append(str(row))
        else:
            matched += 1

    recorded = {str(row) for row in rows}
    on_disk = {
        f"{package}/{path.relative_to(package_dir).as_posix()}"
        for path in package_dir.rglob("*")
        if path.is_file() and PYCACHE not in path.relative_to(package_dir).parts
    }

    return BaseVerification(
        package=package,
        expected=len(rows),
        matched=matched,
        mismatched=tuple(sorted(mismatched)),
        missing=tuple(sorted(missing)),
        extra=tuple(sorted(on_disk - recorded)),
    )


def format_report(result: BaseVerification, record: Path, package_dir: Path) -> str:
    lines = [
        f"verify-base: {result.package}",
        f"record:   {record}",
        f"checkout: {package_dir}",
        f"matching:   {result.matched} of {result.expected}",
        f"mismatched: {len(result.mismatched)}",
        f"missing:    {len(result.missing)}",
        f"extra:      {len(result.extra)}",
    ]
    for label, paths in (
        ("mismatched", result.mismatched),
        ("missing", result.missing),
        ("extra", result.extra),
    ):
        lines.extend(f"{label}: {path}" for path in paths)
    lines.append("result: OK" if result.ok else "result: DIFFERS")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify-base",
        description="Check a checkout's package directory against an installed wheel's RECORD.",
    )
    parser.add_argument("record", type=Path, help="path to <dist>.dist-info/RECORD of the installed wheel")
    parser.add_argument(
        "package_dir", type=Path, help="the checkout's package directory, e.g. hindsight-api-slim/hindsight_api"
    )
    args = parser.parse_args(argv)

    try:
        result = verify_base(args.record, args.package_dir)
    except ValueError as error:
        parser.error(str(error))
    print(format_report(result, args.record, args.package_dir))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
