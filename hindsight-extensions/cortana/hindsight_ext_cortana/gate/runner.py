"""The gate (specification 11): one command runs the deterministic suite, the structuring suite, the
acceptance run and the latency assertion, and writes the run's table and summary.

Where things live. The gate's inputs that hold real data (the questions, the answer keys, the
question subjects, the structuring fixtures) are read from the operating folder, never from the fork.
Each run writes ``gate-<timestamp>.md`` (the table) and ``gate-<timestamp>.json`` (the summary, the
``status.GateRun`` shape, with every result) beside the earlier scorer outputs in the operating
folder's ``ranking/`` (``gate_dir``); ``status`` reports the newest of them.

Verdicts. The deterministic suite passes when pytest exits 0. The structuring suite passes when it
meets its precision target. The acceptance run's hard criteria are the current-state read and
reflect (specification 16 item 8): it passes when every one of the ten questions passes the strict
read and the judged reflect, and every generated question passes the read and, where sampled, the
judged reflect. Plain recall is measured on the strict reading and reported beside them; a failure
on it means the rescoring seam (HSIGHT-11) is built before cut-over is called complete. The latency
suite passes within the recorded budget. The run passes only when all four ran and passed; Josh
accepting a specific shortfall is recorded by him, not by the gate.
"""

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SUITES = ("deterministic", "structuring", "acceptance", "latency")
OPERATING = Path.home() / ".cortana-legacy" / "hindsight"


def gate_dir() -> Path:
    """Where gate runs are written and read: $HINDSIGHT_CORTANA_GATE_DIR, else the operating folder's
    ``ranking/``."""
    return Path(os.environ.get("HINDSIGHT_CORTANA_GATE_DIR") or OPERATING / "ranking").expanduser()


def default_inputs() -> dict[str, Path]:
    """The acceptance inputs, each overridable by an environment variable."""
    ranking = gate_dir()
    return {
        "questions": Path(
            os.environ.get("HINDSIGHT_CORTANA_ACCEPTANCE_QUESTIONS")
            or OPERATING / "backfill" / "experiments" / "reader-queries.json"
        ).expanduser(),
        "keys": Path(os.environ.get("HINDSIGHT_CORTANA_ACCEPTANCE_KEYS") or ranking / "markers.json").expanduser(),
        "subjects": Path(
            os.environ.get("HINDSIGHT_CORTANA_ACCEPTANCE_SUBJECTS") or ranking / "acceptance-subjects.json"
        ).expanduser(),
    }


@dataclass
class SuiteResult:
    name: str
    ran: bool
    passed: bool | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    note: str = ""


def package_dir() -> Path:
    """The package's checkout folder (the one holding ``tests/``)."""
    return Path(__file__).resolve().parents[2]


def release() -> str | None:
    """The commit the run tests (the checkout's HEAD, with ``+dirty`` for uncommitted changes)."""
    try:
        head = subprocess.run(
            ["git", "-C", str(package_dir()), "rev-parse", "--short=9", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(package_dir()), "status", "--porcelain", "--", "."],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return head + ("+dirty" if dirty else "")


SUMMARY_LINE = re.compile(r"(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)")


def run_deterministic() -> SuiteResult:
    """Suite 1: the package's tests, no model calls (the engine's mock provider and embedded database)."""
    tests = package_dir() / "tests"
    if not tests.is_dir():
        return SuiteResult("deterministic", ran=False, passed=False, note=f"no tests at {tests}; run from a checkout")
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=--timeout 300", str(tests)],
        cwd=package_dir(),
        capture_output=True,
        text=True,
    )
    tail = [line for line in done.stdout.splitlines() if line.strip()][-1:] or [""]
    counts = {kind: int(n) for n, kind in SUMMARY_LINE.findall(tail[0])}
    failed = [line for line in done.stdout.splitlines() if line.startswith(("FAILED", "ERROR"))]
    return SuiteResult(
        "deterministic",
        ran=True,
        passed=done.returncode == 0,
        summary={"exit": done.returncode, **counts, "failures": failed},
        note=tail[0].strip("= "),
    )


def acceptance_verdict(ten: list[dict], generated: list[dict]) -> dict[str, Any]:
    """Counts and the hard verdict from the acceptance results (``TenResult``/``GeneratedResult`` dicts)."""

    def count(rows: list[dict], test) -> int:
        return sum(1 for row in rows if test(row))

    reflected = [g for g in generated if g.get("reflect") is not None]
    summary = {
        "ten": len(ten),
        "read_strict": count(ten, lambda r: r["read"]["strict"]),
        "read_spec": count(ten, lambda r: r["read"]["spec"]),
        "recall_strict": count(ten, lambda r: bool(r["recall"] and r["recall"]["strict"])),
        "recall_loose": count(ten, lambda r: bool(r["recall"] and r["recall"]["loose"])),
        "recall_filtered_strict": count(ten, lambda r: bool(r["recall_filtered"] and r["recall_filtered"]["strict"])),
        "reflect_correct": count(ten, lambda r: bool(r["reflect"]["verdict"] and r["reflect"]["verdict"]["correct"])),
        "generated": len(generated),
        "generated_read": count(generated, lambda g: g["read"]),
        "generated_recall_strict": count(generated, lambda g: g["recall_strict"]),
        "generated_reflected": len(reflected),
        "generated_reflect_correct": count(
            reflected, lambda g: bool(g["reflect"]["verdict"] and g["reflect"]["verdict"]["correct"])
        ),
    }
    hard = (
        summary["read_strict"] == summary["ten"]
        and summary["reflect_correct"] == summary["ten"]
        and summary["generated_read"] == summary["generated"]
        and summary["generated_reflect_correct"] == summary["generated_reflected"]
        and summary["ten"] > 0
    )
    summary["hard_criteria_pass"] = hard
    summary["recall_all_strict"] = (
        summary["recall_strict"] == summary["ten"] and summary["generated_recall_strict"] == summary["generated"]
    )
    summary["hsight_11_needed"] = not summary["recall_all_strict"]
    return summary


@dataclass
class GateReport:
    ran_at: datetime
    release: str | None
    bank: str | None
    url: str | None
    suites: dict[str, SuiteResult]
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(self.suites.get(name) and self.suites[name].ran and self.suites[name].passed for name in SUITES)


def write(report: GateReport, out_dir: Path) -> tuple[Path, Path]:
    """Write the table and the summary; return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.ran_at.astimezone().strftime("%Y-%m-%dT%H%M%S")
    table = out_dir / f"gate-{stamp}.md"
    summary = out_dir / f"gate-{stamp}.json"
    table.write_text(render_table(report))
    payload = {
        "ran_at": report.ran_at.isoformat(),
        "release": report.release,
        "passed": report.passed,
        "table_path": str(table),
        "bank": report.bank,
        "url": report.url,
        "suites": {name: asdict(result) for name, result in report.suites.items()},
        "details": report.details,
    }
    summary.write_text(json.dumps(payload, indent=1, default=str))
    return table, summary


def _mark(value: bool | None) -> str:
    return "pass" if value else ("not run" if value is None else "FAIL")


def _cell(text: str | None, limit: int = 160) -> str:
    text = " ".join((text or "").split()).replace("|", "/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_table(report: GateReport) -> str:
    lines = [
        f"# Gate run {report.ran_at.astimezone():%Y-%m-%d %H:%M %Z}",
        "",
        f"Release `{report.release}`; bank `{report.bank}` at {report.url}. Overall: "
        f"**{'pass' if report.passed else 'FAIL'}** (every suite must run and pass).",
        "",
        "| suite | result | detail |",
        "|---|---|---|",
    ]
    for name in SUITES:
        result = report.suites.get(name)
        if result is None or not result.ran:
            lines.append(f"| {name} | not run | {_cell(result.note if result else '')} |")
            continue
        lines.append(f"| {name} | {_mark(result.passed)} | {_cell(result.note, 400)} |")
    ten = report.details.get("ten") or []
    if ten:
        lines += [
            "",
            "## The ten questions",
            "",
            "Current-state read: strict (a standing claim states the current position and none states an old one); "
            "the specification's reading (a current one stands) in brackets. Plain recall: strict, then loose "
            "(first result current), then the first labels. Reflect: the judge's verdict.",
            "",
            "| question | read | recall strict | recall loose | labels | filtered strict | reflect | notes |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for row in ten:
            recall, filtered, verdict = row["recall"], row["recall_filtered"], row["reflect"]["verdict"]
            read = row["read"]
            notes = []
            if read["old"]:
                notes.append("old standing: " + "; ".join(_cell(i["text"], 120) for i in read["old"][:2]))
            if recall and not recall["strict"]:
                notes.append(f"recall first: {_cell(recall['first_text'], 140)}")
            if verdict and not verdict["correct"]:
                notes.append(f"judge: {_cell(verdict['reason'], 160)}")
            if row["reflect"]["error"]:
                notes.append(row["reflect"]["error"])
            notes += row.get("errors") or []
            lines.append(
                f"| {row['id']} | {_mark(read['strict'])} ({_mark(read['spec'])}; C {len(read['current'])}, "
                f"O {len(read['old'])} of {read['standing']}) | {_mark(recall and recall['strict'])} | "
                f"{_mark(recall and recall['loose'])} | `{(recall or {}).get('labels', '')}` | "
                f"{_mark(filtered and filtered['strict']) if filtered else 'n/a'} | "
                f"{_mark(verdict and verdict['correct']) if verdict else 'error'} | {_cell(' / '.join(notes), 600)} |"
            )
    generated = report.details.get("generated") or []
    if generated:
        lines += [
            "",
            "## Generated questions",
            "",
            "| id | subject / attribute | expected | read | recall first | reflect | notes |",
            "|---|---|---|---|---|---|---|",
        ]
        for g in generated:
            verdict = g["reflect"]["verdict"] if g.get("reflect") else None
            reflect = (
                "not sampled"
                if g.get("reflect") is None
                else (_mark(verdict and verdict["correct"]) if verdict else "error")
            )
            notes = [] if g["read"] else [g["read_detail"]]
            if not g["recall_strict"]:
                notes.append(g["recall_detail"])
            if verdict and not verdict["correct"]:
                notes.append(f"judge: {verdict['reason']}")
            lines.append(
                f"| {g['id']} | {_cell(g['subject'], 60)} / {g['attribute']} | {_cell(g['expected'], 80)} | "
                f"{_mark(g['read'])} | {g['recall_first'] or '-'} | {reflect} | {_cell(' / '.join(notes), 300)} |"
            )
    latency = report.details.get("latency")
    if latency:
        lines += ["", "## Latency", "", "```", json.dumps(latency, indent=1, default=str)[:4000], "```"]
    return "\n".join(lines) + "\n"


def newest_run(directory: Path | None = None) -> dict[str, Any] | None:
    """The newest gate run's summary in the gate folder, or None."""
    folder = directory or gate_dir()
    if not folder.is_dir():
        return None
    runs = sorted(folder.glob("gate-*.json"))
    for path in reversed(runs):
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return None


def now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "GateReport",
    "SUITES",
    "SuiteResult",
    "acceptance_verdict",
    "default_inputs",
    "gate_dir",
    "newest_run",
    "release",
    "render_table",
    "run_deterministic",
    "write",
]
