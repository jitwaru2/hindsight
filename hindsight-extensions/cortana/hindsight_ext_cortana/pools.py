"""Pool scoping for documents that arrive without a pool (specification 7.3; closes Linear COR-18).

Pools are the consolidation scope and an optional recall filter; supersession, the current-state read
and decision capture ignore them (7.3, S7). The plugin's session saves carry their ``pool:`` tag and
observation scope from the client (patch v4 keeps the tag lines), and the decision tool tags its
documents itself. Corrections and ingested documents arrive without one, so ``validate_retain`` gives
each such item a pool from its ``domain:`` tag, or the default pool when the domain names none, and
sets its observation scope to that pool, so its observations are consolidated inside the pool like
every other document's. An item that already carries a ``pool:`` tag is left exactly as it came.

The three pools are the domains that have one; any other domain (``finance``, for example) and an
item with no domain go to ``general``, the default every bank shares, as the plugin's own tag lines
map them.
"""

POOL_PREFIX = "pool:"
DOMAIN_PREFIX = "domain:"
POOLS = frozenset({"work", "recovery", "general"})
DEFAULT_POOL = "general"


def pool_for(tags: list[str] | None) -> str:
    """The pool a document belongs in: its domain's pool, else the default."""
    domains = sorted(tag[len(DOMAIN_PREFIX) :] for tag in tags or [] if tag.startswith(DOMAIN_PREFIX))
    return next((domain for domain in domains if domain in POOLS), DEFAULT_POOL)


def has_pool(tags: list[str] | None) -> bool:
    return any(tag.startswith(POOL_PREFIX) for tag in tags or [])


def scope_item(item: dict) -> dict | None:
    """The item with a pool tag and its observation scope, or None when it already has a pool."""
    tags = list(item.get("tags") or [])
    if has_pool(tags):
        return None
    pool = POOL_PREFIX + pool_for(tags)
    return {**item, "tags": [*tags, pool], "observation_scopes": [[pool]]}


def scope_contents(contents: list[dict]) -> list[dict] | None:
    """Every item scoped to a pool, or None when no item needed one (the retain goes through unchanged)."""
    scoped = [scope_item(item) for item in contents]
    if all(item is None for item in scoped):
        return None
    return [new if new is not None else item for item, new in zip(contents, scoped, strict=True)]
