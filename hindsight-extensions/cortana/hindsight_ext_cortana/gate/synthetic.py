"""Synthetic text for the latency measurement's banks: facts and multi-chunk documents about
invented projects and people, generated from a seed so every run sees the same text.

No real data is used (the fork is public). The text is varied enough that recall finds many
related candidates per query (shared projects, people and technologies), so the reranker scores
a full candidate set, as it does on a real bank. Hand-written because the package depends only on
the engine's dependencies (specification 13.1), which include no fake-data library.
"""

import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

PROJECTS = (
    "Kestrel Heron Osprey Plover Merlin Avocet Bittern Curlew Dunlin Egret Fulmar Gannet "
    "Harrier Ibis Jacana Kittiwake Lapwing Mallard Nightjar Oriole Petrel Quail Redshank Siskin"
).split()
PEOPLE = (
    "Ada Bram Cleo Dov Esme Finn Gale Hugo Iris Jude Kai Lena Milo Nia Otis Pia Quinn Rafe Sana "
    "Theo Uma Vik Wren Xena Yara Zed Arlo Bea Cyd Dara"
).split()
COMPONENTS = (
    "storage layer",
    "billing service",
    "search index",
    "ingest pipeline",
    "auth gateway",
    "mobile client",
    "reporting dashboard",
    "deploy tooling",
    "event bus",
    "cache tier",
    "notification service",
    "admin console",
    "data warehouse",
    "rate limiter",
    "scheduler",
    "export job",
    "feature flags",
    "audit trail",
    "onboarding flow",
    "pricing engine",
)
TECHS = (
    "PostgreSQL",
    "SQLite",
    "Redis",
    "Kafka",
    "RabbitMQ",
    "Elasticsearch",
    "OpenSearch",
    "ClickHouse",
    "DuckDB",
    "S3",
    "MinIO",
    "Terraform",
    "Pulumi",
    "Kubernetes",
    "Nomad",
    "FastAPI",
    "Django",
    "Rails",
    "Go",
    "Rust",
    "TypeScript",
    "React",
    "SvelteKit",
    "Flutter",
    "gRPC",
    "GraphQL",
    "NATS",
    "Temporal",
    "Celery",
    "Airflow",
)
REASONS = (
    "the cost review",
    "a latency regression",
    "the security audit",
    "a vendor price change",
    "the on-call load",
    "a scaling incident",
    "the hiring plan",
    "a customer escalation",
    "the compliance deadline",
    "a failed load test",
    "the migration budget",
    "a licensing change",
)
MONTHS = "January February March April May June July August September October November December".split()
METRICS = (
    ("p95 latency", "ms"),
    ("error rate", "percent"),
    ("monthly cost", "dollars"),
    ("uptime", "percent"),
    ("weekly active users", "users"),
    ("build time", "minutes"),
    ("queue depth", "messages"),
)

TEMPLATES = (
    "{p} set the {c} of {proj} to {t} after the {m} review.",
    "The {proj} team moved its {c} from {t} to {t2} because of {r}.",
    "{p} owns the {c} work on {proj} and reports to {p2} on it every {m}.",
    "{proj}'s {metric} target for the {c} is {n} {unit} this quarter.",
    "{p} prefers {t} for {proj}'s {c}, citing {r}.",
    "{p} and {p2} agreed that {proj} keeps {t} for its {c} until {r} is resolved.",
    "During {m}, {proj}'s {c} ran on {t} and its {metric} was {n} {unit}.",
    "{p2} proposed replacing {t} in the {proj} {c}, but {p} asked to wait for {r}.",
)


def _fill(rng: random.Random, template: str) -> str:
    p, p2 = rng.sample(PEOPLE, 2)
    t, t2 = rng.sample(TECHS, 2)
    metric, unit = rng.choice(METRICS)
    return template.format(
        p=p,
        p2=p2,
        proj=rng.choice(PROJECTS),
        c=rng.choice(COMPONENTS),
        t=t,
        t2=t2,
        r=rng.choice(REASONS),
        m=rng.choice(MONTHS),
        metric=metric,
        unit=unit,
        n=rng.randint(2, 950),
    )


def facts(count: int, *, seed: int) -> Iterator[tuple[str, list[str], datetime]]:
    """``count`` distinct one-sentence facts with their entity names and an event date in the year
    before 2026-10-01."""
    rng = random.Random(seed)
    seen: set[str] = set()
    origin = datetime(2026, 10, 1, tzinfo=UTC)
    while len(seen) < count:
        text = _fill(rng, rng.choice(TEMPLATES))
        if text in seen:
            continue
        seen.add(text)
        names = [name for name in (*PROJECTS, *PEOPLE) if name in text.replace("'s", " ").split()]
        yield text, names, origin - timedelta(days=rng.randint(0, 364), minutes=rng.randint(0, 1439))


def document(chunks: int, *, seed: int, chunk_chars: int = 3000) -> str:
    """A markdown document of project notes about ``chunks`` times ``chunk_chars`` long, so the
    engine splits it into about that many extraction chunks."""
    rng = random.Random(seed)
    parts = [f"# Project notes, batch {seed}\n"]
    size = len(parts[0])
    while size < chunks * chunk_chars:
        heading = f"\n## {rng.choice(PROJECTS)} {rng.choice(COMPONENTS)}\n\n"
        paragraph = " ".join(_fill(rng, rng.choice(TEMPLATES)) for _ in range(rng.randint(4, 7))) + "\n"
        parts.append(heading + paragraph)
        size += len(heading) + len(paragraph)
    return "".join(parts)
