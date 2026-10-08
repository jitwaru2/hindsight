"""Attribute merges and their inverse (specification 6.3).

When two keys of one subject name the same attribute (the alignment pass's ``same_as``, the gate's
review, or an operator), ``merge_key`` records the merge on the merged key's catalog row
(``alignment = merged``, ``merged_into``), re-points the aliases that named the merged key, re-keys
its claims onto the target, and writes the ledger entry; the caller then recomputes both keys. A
merge is reversed by ``reverse_merge``, which records the inverse: the aliases go back, the claims
whose ``keyed_as`` is the merged key or one of its aliases return to it, and the key is ``aligned``
as its own key, because an operator or reviewer has now said it is distinct.

``mark_distinct`` resolves a pending key as its own attribute (the alignment pass's ``distinct``).

Each runs in one transaction under the bank's supersession lock, so it never interleaves with a
supersession pass.
"""

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from hindsight_api.engine.schema import fq_table

from . import ledger
from .supersession import LOCK_PREFIX, Key


class MergeRefused(ValueError):
    """The merge or its inverse does not apply to the catalog as it stands."""


@dataclass
class MergeResult:
    subject_id: UUID
    key: str
    into: str
    claim_ids: list[UUID] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    @property
    def keys(self) -> set[Key]:
        return {(self.subject_id, self.key), (self.subject_id, self.into)}


async def _lock(conn: Any, bank_id: str) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LOCK_PREFIX + bank_id)


async def _catalog_rows(conn: Any, bank_id: str, subject_id: UUID, keys: list[str]) -> dict[str, Any]:
    rows = await conn.fetch(
        f"SELECT attribute_key, alignment, merged_into, subject_entity_id FROM {fq_table('attributes')} "
        f"WHERE bank_id = $1 AND subject_entity_id = $2 AND attribute_key = ANY($3::text[])",
        bank_id,
        subject_id,
        keys,
    )
    return {row["attribute_key"]: row for row in rows}


async def merge_key(
    engine: Any,
    bank_id: str,
    subject_id: UUID,
    key: str,
    into: str,
    *,
    actor: str,
    reason: str,
    run_id: UUID | None = None,
    subject_text: str = "",
) -> MergeResult:
    """Merge ``key`` into ``into`` on one subject. Raises ``MergeRefused`` when it does not apply."""
    if key == into:
        raise MergeRefused(f"{key} cannot be merged into itself")
    pool = await engine._get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _lock(conn, bank_id)
        rows = await _catalog_rows(conn, bank_id, subject_id, [key, into])
        if key not in rows or into not in rows:
            raise MergeRefused(f"both {key} and {into} must be keys of subject {subject_id}")
        if rows[key]["alignment"] == "merged":
            raise MergeRefused(f"{key} is already merged into {rows[key]['merged_into']}")
        if rows[into]["alignment"] == "merged":
            raise MergeRefused(f"{into} is itself merged into {rows[into]['merged_into']}; merge into that key")
        aliases = [
            row["attribute_key"]
            for row in await conn.fetch(
                f"UPDATE {fq_table('attributes')} SET merged_into = $4, updated_at = now() "
                f"WHERE bank_id = $1 AND subject_entity_id = $2 AND merged_into = $3 RETURNING attribute_key",
                bank_id,
                subject_id,
                key,
                into,
            )
        ]
        await conn.execute(
            f"UPDATE {fq_table('attributes')} SET alignment = 'merged', merged_into = $4, "
            f"conflict_claim_ids = '{{}}', stale_documents = '{{}}', updated_at = now() "
            f"WHERE bank_id = $1 AND subject_entity_id = $2 AND attribute_key = $3",
            bank_id,
            subject_id,
            key,
            into,
        )
        moved = await conn.fetch(
            f"UPDATE {fq_table('claims')} SET attribute_key = $4, updated_at = now() "
            f"WHERE bank_id = $1 AND subject_entity_id = $2 AND attribute_key = $3 "
            f"RETURNING id, memory_unit_id, subject_text",
            bank_id,
            subject_id,
            key,
            into,
        )
        name = subject_text or (moved[0]["subject_text"] if moved else str(subject_id))
        result = MergeResult(subject_id, key, into, [row["id"] for row in moved], sorted(aliases))
        await ledger.append(
            conn,
            bank_id,
            [
                ledger.Entry(
                    "attribute-merged",
                    reason=f"{name}/{key} merged into {name}/{into}: {reason}",
                    claim_ids=result.claim_ids,
                    memory_unit_ids=list(dict.fromkeys(row["memory_unit_id"] for row in moved)),
                    details={
                        "subject_entity_id": subject_id,
                        "key": key,
                        "into": into,
                        "aliases": result.aliases,
                        "previous_alignment": rows[key]["alignment"],
                    },
                )
            ],
            actor=actor,
            run_id=run_id,
        )
    return result


async def reverse_merge(
    engine: Any,
    bank_id: str,
    subject_id: UUID,
    key: str,
    *,
    actor: str,
    reason: str,
    run_id: UUID | None = None,
) -> MergeResult:
    """Record the inverse of the merge of ``key``: the claims and aliases it moved go back."""
    pool = await engine._get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _lock(conn, bank_id)
        rows = await _catalog_rows(conn, bank_id, subject_id, [key])
        if key not in rows or rows[key]["alignment"] != "merged" or not rows[key]["merged_into"]:
            raise MergeRefused(f"{key} of subject {subject_id} is not merged")
        into = rows[key]["merged_into"]
        merge = await conn.fetchrow(
            f"""
            SELECT details FROM {fq_table("ledger")}
            WHERE bank_id = $1 AND event = 'attribute-merged'
              AND details->>'subject_entity_id' = $2 AND details->>'key' = $3
            ORDER BY id DESC LIMIT 1
            """,
            bank_id,
            str(subject_id),
            key,
        )
        details = merge["details"] if merge else {}
        if isinstance(details, str):
            details = json.loads(details)
        aliases = list(details.get("aliases") or [])
        if aliases:
            await conn.execute(
                f"UPDATE {fq_table('attributes')} SET merged_into = $3, updated_at = now() "
                f"WHERE bank_id = $1 AND subject_entity_id = $2 AND attribute_key = ANY($4::text[]) AND merged_into = $5",
                bank_id,
                subject_id,
                key,
                aliases,
                into,
            )
        await conn.execute(
            f"UPDATE {fq_table('attributes')} SET alignment = 'aligned', merged_into = NULL, updated_at = now() "
            f"WHERE bank_id = $1 AND subject_entity_id = $2 AND attribute_key = $3",
            bank_id,
            subject_id,
            key,
        )
        moved = await conn.fetch(
            f"UPDATE {fq_table('claims')} SET attribute_key = $3, updated_at = now() "
            f"WHERE bank_id = $1 AND subject_entity_id = $2 AND attribute_key = $4 AND keyed_as = ANY($5::text[]) "
            f"RETURNING id, memory_unit_id, subject_text",
            bank_id,
            subject_id,
            key,
            into,
            [key, *aliases],
        )
        name = moved[0]["subject_text"] if moved else str(subject_id)
        result = MergeResult(subject_id, key, into, [row["id"] for row in moved], aliases)
        await ledger.append(
            conn,
            bank_id,
            [
                ledger.Entry(
                    "attribute-merge-reversed",
                    reason=f"{name}/{key} split back out of {name}/{into}: {reason}",
                    claim_ids=result.claim_ids,
                    memory_unit_ids=list(dict.fromkeys(row["memory_unit_id"] for row in moved)),
                    details={"subject_entity_id": subject_id, "key": key, "into": into, "aliases": aliases},
                )
            ],
            actor=actor,
            run_id=run_id,
        )
    return result


async def mark_distinct(
    engine: Any,
    bank_id: str,
    subject_id: UUID,
    key: str,
    *,
    actor: str,
    reason: str,
    run_id: UUID | None = None,
    subject_text: str = "",
) -> bool:
    """Resolve a pending key as its own attribute. Returns False when it was not pending."""
    pool = await engine._get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _lock(conn, bank_id)
        changed = await conn.fetchval(
            f"UPDATE {fq_table('attributes')} SET alignment = 'aligned', updated_at = now() "
            f"WHERE bank_id = $1 AND subject_entity_id = $2 AND attribute_key = $3 AND alignment = 'pending' "
            f"RETURNING attribute_key",
            bank_id,
            subject_id,
            key,
        )
        if changed is None:
            return False
        await ledger.append(
            conn,
            bank_id,
            [
                ledger.Entry(
                    "attribute-aligned",
                    reason=f"{subject_text or subject_id}/{key} is its own attribute: {reason}",
                    details={"subject_entity_id": subject_id, "key": key},
                )
            ],
            actor=actor,
            run_id=run_id,
        )
    return True
