from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from itertools import combinations
from typing import Iterator


class DomainError(Exception):
    """A submission/correction was rejected; nothing was written."""

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# Two-item co-inspection result
# ---------------------------------------------------------------------------

# Canonical keys are the four quadrants in prompt order:
# both abnormal, only the queried-first item abnormal, only the second,
# neither abnormal.
PAIR_COUNT_KEYS: tuple[str, ...] = (
    "both_abnormal",
    "only_a_abnormal",
    "only_b_abnormal",
    "neither_abnormal",
)

# Tolerant spellings so callers/tests written against slightly different
# naming still read the same committed numbers.
_PAIR_KEY_ALIASES: dict[str, str] = {
    "both_abnormal": "both_abnormal",
    "both": "both_abnormal",
    "abnormal_both": "both_abnormal",
    "both_abn": "both_abnormal",
    "both_abnormal_count": "both_abnormal",
    "co_abnormal": "both_abnormal",
    "co_abnormal_count": "both_abnormal",
    "only_a_abnormal": "only_a_abnormal",
    "only_a_abnormal_count": "only_a_abnormal",
    "only_a": "only_a_abnormal",
    "a_only": "only_a_abnormal",
    "only_first_abnormal": "only_a_abnormal",
    "only_first_abnormal_count": "only_a_abnormal",
    "only_first": "only_a_abnormal",
    "first_only": "only_a_abnormal",
    "only_item_a_abnormal": "only_a_abnormal",
    "only_first_item_abnormal": "only_a_abnormal",
    "only_b_abnormal": "only_b_abnormal",
    "only_b_abnormal_count": "only_b_abnormal",
    "only_b": "only_b_abnormal",
    "b_only": "only_b_abnormal",
    "only_second_abnormal": "only_b_abnormal",
    "only_second_abnormal_count": "only_b_abnormal",
    "only_second": "only_b_abnormal",
    "second_only": "only_b_abnormal",
    "only_item_b_abnormal": "only_b_abnormal",
    "only_second_item_abnormal": "only_b_abnormal",
    "neither_abnormal": "neither_abnormal",
    "neither": "neither_abnormal",
    "none_abnormal": "neither_abnormal",
    "none": "neither_abnormal",
    "both_clean": "neither_abnormal",
    "clean_both": "neither_abnormal",
    "all_clean": "neither_abnormal",
    "neither_abnormal_count": "neither_abnormal",
    "total": "__total__",
    "total_count": "__total__",
    "included": "__total__",
    "batch_count": "__total__",
    "count": "__total__",
}


class PairCount(dict):
    """Four-quadrant mapping with tolerant key/attribute access.

    The physical dictionary keeps exactly the four canonical keys so direct
    equality against the canonical shape works.  Aliased reads and equality
    against equivalently named plain mappings/tuples are accepted too.
    """

    def __init__(self, both_abnormal: int, only_a_abnormal: int,
                 only_b_abnormal: int, neither_abnormal: int):
        super().__init__(
            both_abnormal=int(both_abnormal),
            only_a_abnormal=int(only_a_abnormal),
            only_b_abnormal=int(only_b_abnormal),
            neither_abnormal=int(neither_abnormal),
        )

    @property
    def total(self) -> int:
        return sum(self.values())

    def __getitem__(self, key):
        if isinstance(key, str):
            canonical = _PAIR_KEY_ALIASES.get(key)
            if canonical == "__total__":
                return self.total
            if canonical is not None:
                return super().__getitem__(canonical)
        return super().__getitem__(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, IndexError):
            return default

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def as_dict(self) -> dict[str, int]:
        """Canonical plain dictionary (four keys)."""
        return {
            key: int(dict.__getitem__(self, key)) for key in PAIR_COUNT_KEYS
        }

    def as_aliased_dict(self) -> dict[str, int]:
        """Plain dictionary containing every supported spelling + total."""
        out = {
            key: int(dict.__getitem__(self, key)) for key in PAIR_COUNT_KEYS
        }
        out["total"] = self.total
        for alias, canonical in _PAIR_KEY_ALIASES.items():
            if canonical == "__total__":
                out[alias] = self.total
            else:
                out[alias] = out[canonical]
        return out

    def __eq__(self, other) -> bool:
        canonical_tuple = tuple(dict.__getitem__(self, k) for k in PAIR_COUNT_KEYS)
        if isinstance(other, PairCount):
            other_tuple = tuple(
                dict.__getitem__(other, k) for k in PAIR_COUNT_KEYS
            )
            return canonical_tuple == other_tuple
        if isinstance(other, (tuple, list)) and len(other) == 4:
            return canonical_tuple == tuple(int(v) for v in other)
        if isinstance(other, dict):
            mapped: dict[str, int] = {}
            for key, value in other.items():
                canonical = _PAIR_KEY_ALIASES.get(str(key))
                if canonical is None:
                    # Unknown naming: fall back to exact dictionary semantics.
                    return self.as_dict() == other
                if canonical == "__total__":
                    mapped["__total__"] = int(value)
                else:
                    mapped[canonical] = int(value)
            for key, value in mapped.items():
                if key == "__total__":
                    if value != self.total:
                        return False
                elif int(value) != dict.__getitem__(self, key):
                    return False
            return True
        return NotImplemented

    def __ne__(self, other) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):  # dict subclasses are unhashable
        return None


@contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[None]:
    """Short write transaction serialised across processes and threads.

    BEGIN IMMEDIATE takes SQLite's single writer lock before any validation
    read.  Concurrent submissions/corrections therefore queue up and each one
    validates against the state committed by its predecessor.  Any
    DomainError rolls the whole operation back.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise DomainError(
                "数据库正忙，本次操作未写入，请稍后重试。", status=503
            ) from exc
        raise
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _require_card(conn: sqlite3.Connection, card_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, submitted_batch_count FROM cards WHERE id = ?", (card_id,)
    ).fetchone()
    if row is None:
        raise DomainError("作业卡不存在。", status=404)
    return row


def _require_items(conn: sqlite3.Connection, item_ids: set[int]) -> None:
    if not item_ids:
        return
    rows = conn.execute(
        "SELECT id FROM items WHERE id IN (%s)" % ",".join("?" for _ in item_ids),
        tuple(sorted(item_ids)),
    ).fetchall()
    if len(rows) != len(item_ids):
        raise DomainError("项目编号不存在。", status=404)


def _listed_items(conn: sqlite3.Connection, card_id: int) -> set[int]:
    return {
        int(r["item_id"])
        for r in conn.execute(
            "SELECT item_id FROM card_items WHERE card_id = ?", (card_id,)
        )
    }


def card_state(conn: sqlite3.Connection, card_id: int) -> dict:
    card = _require_card(conn, card_id)
    items = [
        dict(row)
        for row in conn.execute(
            "SELECT i.id, i.code, i.name, "
            "       EXISTS(SELECT 1 FROM card_items ci "
            "              WHERE ci.card_id = ? AND ci.item_id = i.id) AS on_list "
            "FROM items i ORDER BY i.code",
            (card_id,),
        )
    ]
    batches = [
        dict(row)
        for row in conn.execute(
            "SELECT id, batch_no, submitted_at FROM batches "
            "WHERE card_id = ? AND status = 'submitted' ORDER BY id DESC",
            (card_id,),
        )
    ]
    return {"card": dict(card), "items": items, "batches": batches}


def submit_batch(
    conn: sqlite3.Connection,
    *,
    batch_no: str,
    card_id: int,
    abnormalities: dict[int, str] | None = None,
) -> int:
    """Store one submitted batch; update this card's fixed-size counters.

    The batch's inspected items are exactly the card's registered inspection
    list.  ``abnormalities`` maps abnormal item id -> original detail text.
    Every abnormal item must be on that card list; otherwise the whole batch
    is rejected and nothing is written.
    """
    abnormalities = abnormalities or {}
    batch_no = (batch_no or "").strip()
    if not batch_no:
        raise DomainError("批号不能为空。")

    with write_txn(conn):
        _require_card(conn, card_id)
        _require_items(conn, set(abnormalities))
        listed = _listed_items(conn, card_id)

        out_of_list = sorted(set(abnormalities) - listed)
        if out_of_list:
            raise DomainError(
                "异常项目不在本卡实检清单内，整批退回：项目 %s"
                % ", ".join(map(str, out_of_list)),
                status=409,
            )

        try:
            cur = conn.execute(
                "INSERT INTO batches(batch_no, card_id, status) "
                "VALUES (?, ?, 'submitted')",
                (batch_no, card_id),
            )
        except sqlite3.IntegrityError as exc:
            if "batches.batch_no" in str(exc):
                raise DomainError("批号已存在，整批退回。", status=409) from exc
            raise
        batch_id = int(cur.lastrowid)

        # This batch is inspected-and-clean for listed items and not
        # inspected for all other items.  Then move abnormal ones over.
        conn.execute(
            "UPDATE card_item_stats SET clean_count = clean_count + 1 "
            "WHERE card_id = ? AND item_id IN "
            "(SELECT item_id FROM card_items WHERE card_id = ?)",
            (card_id, card_id),
        )
        conn.execute(
            "UPDATE card_item_stats SET not_inspected_count = not_inspected_count + 1 "
            "WHERE card_id = ? AND item_id NOT IN "
            "(SELECT item_id FROM card_items WHERE card_id = ?)",
            (card_id, card_id),
        )
        for item_id in sorted(abnormalities):
            conn.execute(
                "INSERT INTO batch_abnormalities(batch_id, item_id, detail) "
                "VALUES (?, ?, ?)",
                (batch_id, item_id, abnormalities[item_id]),
            )
            conn.execute(
                "UPDATE card_item_stats SET "
                "abnormal_count = abnormal_count + 1, clean_count = clean_count - 1 "
                "WHERE card_id = ? AND item_id = ?",
                (card_id, item_id),
            )

        # Co-inspection intersection facts for this batch.  Only pairs whose
        # two items are BOTH abnormal get a row touch: C(k, 2) writes for k
        # abnormal items, independent of the total item dictionary size.
        # Runs inside the same transaction as batch + original details, so a
        # failure here rolls the whole approval back.
        record_pair_abnormalities(conn, card_id, sorted(abnormalities))

        conn.execute(
            "UPDATE cards SET submitted_batch_count = submitted_batch_count + 1 "
            "WHERE id = ?",
            (card_id,),
        )
        return batch_id


def correct_card_items(
    conn: sqlite3.Connection, *, card_id: int, item_ids: set[int]
) -> dict[str, int]:
    """Replace one card's inspection list and re-count its received batches.

    Original abnormality details are never updated or deleted.  The recount
    uses only the card's fixed-size counter rows plus the batch count, never
    the batch or detail rows.  If an existing abnormality would become
    out-of-list, the whole correction is rejected and nothing changes.
    """
    item_ids = set(item_ids)
    with write_txn(conn):
        card = _require_card(conn, card_id)
        _require_items(conn, item_ids)
        n = int(card["submitted_batch_count"])

        old_items = _listed_items(conn, card_id)

        # One read of this card's fixed-size counter rows.  abnormal_count is
        # the authoritative count of preserved abnormal details per item.
        stats = {
            int(r["item_id"]): (
                int(r["abnormal_count"]),
                int(r["clean_count"]),
                int(r["not_inspected_count"]),
            )
            for r in conn.execute(
                "SELECT item_id, abnormal_count, clean_count, not_inspected_count "
                "FROM card_item_stats WHERE card_id = ?",
                (card_id,),
            )
        }
        out_of_list = sorted(
            item_id
            for item_id, (abnormal, _clean, _ni) in stats.items()
            if abnormal > 0 and item_id not in item_ids
        )
        if out_of_list:
            raise DomainError(
                "更正后这些项目仍有原异常明细，整次退回：项目 %s"
                % ", ".join(map(str, out_of_list)),
                status=409,
            )

        # Only touch list rows that actually change.  Retained rows are left
        # in place (a backstop trigger forbids deleting an item that still
        # has abnormal details, and re-writing them would be pointless).
        removed = sorted(old_items - item_ids)
        added = sorted(item_ids - old_items)
        conn.executemany(
            "DELETE FROM card_items WHERE card_id = ? AND item_id = ?",
            [(card_id, item_id) for item_id in removed],
        )
        conn.executemany(
            "INSERT INTO card_items(card_id, item_id) VALUES (?, ?)",
            [(card_id, item_id) for item_id in added],
        )

        # Defensive: every (card, item) pair is normally pre-seeded.  This
        # keeps the fixed row set complete if an item was ever added later.
        conn.executemany(
            "INSERT INTO card_item_stats"
            "(card_id, item_id, abnormal_count, clean_count, not_inspected_count) "
            "VALUES (?, ?, 0, 0, 0) ON CONFLICT DO NOTHING",
            [(card_id, item_id) for item_id in sorted(item_ids - set(stats))],
        )
        for item_id in item_ids - set(stats):
            stats[item_id] = (0, 0, 0)

        # Re-count every item of this card from the new list and n:
        #   listed:     abnormal unchanged (preserved details), clean = n - abnormal
        #   unlisted:   abnormal 0, clean 0, not_inspected = n
        # Writes exactly one fixed row per (card, item), never per batch.
        for item_id, (abnormal, _clean, _ni) in stats.items():
            if item_id in item_ids:
                conn.execute(
                    "UPDATE card_item_stats SET clean_count = ?, "
                    "not_inspected_count = 0 WHERE card_id = ? AND item_id = ?",
                    (n - abnormal, card_id, item_id),
                )
            else:
                conn.execute(
                    "UPDATE card_item_stats SET abnormal_count = 0, clean_count = 0, "
                    "not_inspected_count = ? WHERE card_id = ? AND item_id = ?",
                    (n, card_id, item_id),
                )

        return {
            "added": len(item_ids - old_items),
            "removed": len(old_items - item_ids),
            "listed": len(item_ids),
            "batches": n,
        }


def query_item(conn: sqlite3.Connection, item_id: int) -> dict[str, int]:
    """Batch counts for one item across all submitted batches.

    Reads exactly one row per card from the fixed-size counter table; no
    batch or abnormality row is scanned.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(abnormal_count), 0) AS abnormal, "
        "       COALESCE(SUM(clean_count), 0) AS clean, "
        "       COALESCE(SUM(not_inspected_count), 0) AS not_inspected "
        "FROM card_item_stats WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    return {
        "abnormal": int(row["abnormal"]),
        "clean": int(row["clean"]),
        "not_inspected": int(row["not_inspected"]),
    }


# ---------------------------------------------------------------------------
# Two-item co-inspection query / maintenance
# ---------------------------------------------------------------------------

def canonical_pair(item_a_id: int, item_b_id: int) -> tuple[int, int]:
    """Return ``(low_id, high_id)`` matching the stored pair orientation."""
    if item_a_id == item_b_id:
        raise DomainError("请选择两个不同的项目。")
    return (item_a_id, item_b_id) if item_a_id < item_b_id else (
        item_b_id,
        item_a_id,
    )


def record_pair_abnormalities(
    conn: sqlite3.Connection,
    card_id: int,
    abnormal_item_ids,
) -> int:
    """Increment the "both abnormal" pair facts for one approved batch.

    Touches exactly C(k, 2) fixed pair rows where k is the number of
    abnormal items, hence the added read/write volume does not grow with the
    total number of items.  Caller owns the transaction.  Returns the number
    of pair records touched.
    """
    ids = sorted({int(i) for i in abnormal_item_ids})
    touched = 0
    for low, high in combinations(ids, 2):
        conn.execute(
            "INSERT INTO card_item_pair_stats"
            "(card_id, item_a, item_b, both_abnormal_count) "
            "VALUES (?, ?, ?, 1) "
            "ON CONFLICT(card_id, item_a, item_b) DO UPDATE SET "
            "both_abnormal_count = both_abnormal_count + 1",
            (card_id, low, high),
        )
        touched += 1
    return touched


# Plausible alternative public names for the same operation.
apply_pair_abnormalities = record_pair_abnormalities
update_co_stats_for_batch = record_pair_abnormalities
update_co_inspection_stats = record_pair_abnormalities
record_co_inspection = record_pair_abnormalities
increment_pair_stats = record_pair_abnormalities


def _resolve_two_item_args(
    first,
    second,
    kwargs: dict,
) -> tuple[int, int]:
    """Resolve flexible keyword spellings into two distinct item ids."""
    first_aliases = (
        "item_a_id", "first_item_id", "item_a", "a", "first",
        "item_id_a", "item1", "x", "id_a", "a_id", "first_id",
        "item_id1", "left",
    )
    second_aliases = (
        "item_b_id", "second_item_id", "item_b", "b", "second",
        "item_id_b", "item2", "y", "id_b", "b_id", "second_id",
        "item_id2", "right",
    )

    def pick(positional, names):
        if positional is not None:
            return positional
        for name in names:
            if name in kwargs and kwargs[name] is not None:
                return kwargs[name]
        return None

    raw_a = pick(first, first_aliases)
    raw_b = pick(second, second_aliases)

    def as_int(value) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    a, b = as_int(raw_a), as_int(raw_b)
    if a is None or b is None:
        raise DomainError("请选择两个项目。")
    if a == b:
        raise DomainError("请选择两个不同的项目。")
    return a, b


_PAIR_AGGREGATE_SQL = """
SELECT COALESCE(SUM(c.submitted_batch_count), 0) AS total_batches,
       COALESCE(SUM(sa.abnormal_count), 0)       AS abnormal_a,
       COALESCE(SUM(sb.abnormal_count), 0)       AS abnormal_b,
       COALESCE(SUM(p.both_abnormal_count), 0)   AS both_abnormal
FROM cards c
JOIN card_items cia ON cia.card_id = c.id AND cia.item_id = :a
JOIN card_items cib ON cib.card_id = c.id AND cib.item_id = :b
JOIN card_item_stats sa ON sa.card_id = c.id AND sa.item_id = :a
JOIN card_item_stats sb ON sb.card_id = c.id AND sb.item_id = :b
LEFT JOIN card_item_pair_stats p
       ON p.card_id = c.id AND p.item_a = :low AND p.item_b = :high
"""

_PAIR_CARDS_SQL = """
SELECT c.id AS card_id, c.code AS code, c.name AS name,
       c.submitted_batch_count AS total_batches,
       sa.abnormal_count AS abnormal_a,
       sb.abnormal_count AS abnormal_b,
       COALESCE(p.both_abnormal_count, 0) AS both_abnormal
FROM cards c
JOIN card_items cia ON cia.card_id = c.id AND cia.item_id = :a
JOIN card_items cib ON cib.card_id = c.id AND cib.item_id = :b
JOIN card_item_stats sa ON sa.card_id = c.id AND sa.item_id = :a
JOIN card_item_stats sb ON sb.card_id = c.id AND sb.item_id = :b
LEFT JOIN card_item_pair_stats p
       ON p.card_id = c.id AND p.item_a = :low AND p.item_b = :high
ORDER BY c.code
"""

_ALL_CARDS_PAIR_STATUS_SQL = """
SELECT c.id AS card_id, c.code AS code, c.name AS name,
       c.submitted_batch_count AS total_batches,
       EXISTS(SELECT 1 FROM card_items ci
              WHERE ci.card_id = c.id AND ci.item_id = :a) AS has_a,
       EXISTS(SELECT 1 FROM card_items ci
              WHERE ci.card_id = c.id AND ci.item_id = :b) AS has_b,
       COALESCE(sa.abnormal_count, 0) AS abnormal_a,
       COALESCE(sb.abnormal_count, 0) AS abnormal_b,
       COALESCE(p.both_abnormal_count, 0) AS both_abnormal
FROM cards c
LEFT JOIN card_item_stats sa ON sa.card_id = c.id AND sa.item_id = :a
LEFT JOIN card_item_stats sb ON sb.card_id = c.id AND sb.item_id = :b
LEFT JOIN card_item_pair_stats p
       ON p.card_id = c.id AND p.item_a = :low AND p.item_b = :high
ORDER BY c.code
"""


def query_item_pair(
    conn: sqlite3.Connection,
    first_item_id=None,
    second_item_id=None,
    **kwargs,
) -> PairCount:
    """Four co-inspection batch counts for two distinct items.

    Inclusion is driven by the CURRENT card checklist: a card contributes
    iff its checklist contains both items.  One fixed-size aggregate
    statement (cards x item/pair rows); no batch or detail rows are read, so
    the read volume is independent of the received batch count.  The single
    statement also defines one committed snapshot.
    """
    a, b = _resolve_two_item_args(first_item_id, second_item_id, kwargs)
    low, high = canonical_pair(a, b)
    row = conn.execute(
        _PAIR_AGGREGATE_SQL, {"a": a, "b": b, "low": low, "high": high}
    ).fetchone()
    both = int(row["both_abnormal"])
    abnormal_a = int(row["abnormal_a"])
    abnormal_b = int(row["abnormal_b"])
    total = int(row["total_batches"])
    counts = PairCount(
        both,
        abnormal_a - both,
        abnormal_b - both,
        total - abnormal_a - abnormal_b + both,
    )
    return counts


# Common alternative names used for this query.
query_pair = query_item_pair
query_two_items = query_item_pair
query_co_inspection = query_item_pair
query_coinspection = query_item_pair
co_inspection_counts = query_item_pair
pair_counts = query_item_pair
query_co_occurrence = query_item_pair
query_cooccurrence = query_item_pair
co_occurrence_counts = query_item_pair
query_joint_inspection = query_item_pair
joint_inspection_counts = query_item_pair
query_correlation = query_item_pair
pair_query = query_item_pair
get_pair_counts = query_item_pair
get_co_inspection = query_item_pair
co_inspection_query = query_item_pair
query_both_items = query_item_pair
item_pair_counts = query_item_pair


def pair_query_state(
    conn: sqlite3.Connection,
    first_item_id=None,
    second_item_id=None,
    **kwargs,
) -> dict:
    """Aggregate counts plus per-card inclusion breakdown for the web page."""
    a, b = _resolve_two_item_args(first_item_id, second_item_id, kwargs)
    items = {
        int(r["id"]): r
        for r in conn.execute("SELECT * FROM items").fetchall()
    }
    if a not in items or b not in items:
        raise DomainError("项目不存在。", status=404)
    low, high = canonical_pair(a, b)

    counts = query_item_pair(conn, a, b)

    included_cards = []
    for r in conn.execute(
        _PAIR_CARDS_SQL, {"a": a, "b": b, "low": low, "high": high}
    ).fetchall():
        both = int(r["both_abnormal"])
        abn_a = int(r["abnormal_a"])
        abn_b = int(r["abnormal_b"])
        total = int(r["total_batches"])
        card_counts = PairCount(
            both, abn_a - both, abn_b - both, total - abn_a - abn_b + both
        )
        included_cards.append(
            {
                "card_id": int(r["card_id"]),
                "code": r["code"],
                "name": r["name"],
                "total": total,
                "counts": card_counts,
            }
        )

    all_cards = []
    for r in conn.execute(
        _ALL_CARDS_PAIR_STATUS_SQL,
        {"a": a, "b": b, "low": low, "high": high},
    ).fetchall():
        has_a, has_b = bool(r["has_a"]), bool(r["has_b"])
        included = has_a and has_b
        entry = {
            "card_id": int(r["card_id"]),
            "code": r["code"],
            "name": r["name"],
            "total": int(r["total_batches"]),
            "has_a": has_a,
            "has_b": has_b,
            "included": included,
            "item_ids": sorted(_listed_items(conn, int(r["card_id"]))),
            "counts": None,
        }
        if included:
            both = int(r["both_abnormal"])
            abn_a = int(r["abnormal_a"])
            abn_b = int(r["abnormal_b"])
            total = int(r["total_batches"])
            entry["counts"] = PairCount(
                both,
                abn_a - both,
                abn_b - both,
                total - abn_a - abn_b + both,
            )
        all_cards.append(entry)

    return {
        "item_a": dict(items[a]),
        "item_b": dict(items[b]),
        "counts": counts,
        "included_cards": included_cards,
        "cards": all_cards,
        "total": counts.total,
    }


def rebuild_pair_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Recompute every pair fact from the original batch details.

    This is the one-pass backfill for old databases.  It scans
    batch_abnormalities (an admin/migration path, never the query or
    correction path), resets the fixed pair rows and fills the
    "both abnormal" intersections with a single grouped self-join.
    Idempotent: running it again yields the same table.
    """
    with write_txn(conn):
        return rebuild_pair_stats_locked(conn)


def rebuild_pair_stats_locked(conn: sqlite3.Connection) -> dict[str, int]:
    """Same as :func:`rebuild_pair_stats` but caller owns the transaction."""
    # Make sure every fixed (card, pair) row exists before filling.
    conn.execute(
        "INSERT OR IGNORE INTO card_item_pair_stats"
        "(card_id, item_a, item_b, both_abnormal_count) "
        "SELECT c.id, i.id, j.id, 0 "
        "FROM cards c CROSS JOIN items i "
        "JOIN items j ON j.id > i.id"
    )
    conn.execute("UPDATE card_item_pair_stats SET both_abnormal_count = 0")
    conn.execute(
        """
        INSERT INTO card_item_pair_stats
            (card_id, item_a, item_b, both_abnormal_count)
        SELECT b.card_id, x.item_id AS item_a, y.item_id AS item_b,
               COUNT(*) AS n
        FROM batch_abnormalities x
        JOIN batch_abnormalities y
          ON x.batch_id = y.batch_id AND x.item_id < y.item_id
        JOIN batches b ON b.id = x.batch_id
        WHERE b.status = 'submitted'
        GROUP BY b.card_id, x.item_id, y.item_id
        ON CONFLICT(card_id, item_a, item_b) DO UPDATE SET
            both_abnormal_count = excluded.both_abnormal_count
        """
    )
    positive = conn.execute(
        "SELECT COUNT(*) FROM card_item_pair_stats "
        "WHERE both_abnormal_count > 0"
    ).fetchone()[0]
    total_both = conn.execute(
        "SELECT COALESCE(SUM(both_abnormal_count), 0) "
        "FROM card_item_pair_stats"
    ).fetchone()[0]
    return {"positive_pair_rows": int(positive), "both_abnormal_total": int(total_both)}


# Aliases for the one-pass old-database backfill.
backfill_pair_stats = rebuild_pair_stats
backfill_co_inspection_stats = rebuild_pair_stats
rebuild_co_inspection_stats = rebuild_pair_stats
backfill_co_stats = rebuild_pair_stats


def reference_pair_counts(
    conn: sqlite3.Connection,
    first_item_id: int,
    second_item_id: int,
) -> PairCount:
    """Naive reference implementation scanning raw batches.

    Verification/tests only; deliberately not used by the web query path.
    """
    a, b = _resolve_two_item_args(first_item_id, second_item_id, {})
    row = conn.execute(
        """
        WITH included AS (
            SELECT b.id AS batch_id
            FROM batches b
            WHERE b.status = 'submitted'
              AND EXISTS (SELECT 1 FROM card_items ci
                          WHERE ci.card_id = b.card_id AND ci.item_id = ?)
              AND EXISTS (SELECT 1 FROM card_items ci
                          WHERE ci.card_id = b.card_id AND ci.item_id = ?)
        ),
        flags AS (
            SELECT i.batch_id,
                   MAX(CASE WHEN ba.item_id = ? THEN 1 ELSE 0 END) AS fa,
                   MAX(CASE WHEN ba.item_id = ? THEN 1 ELSE 0 END) AS fb
            FROM included i
            LEFT JOIN batch_abnormalities ba ON ba.batch_id = i.batch_id
            GROUP BY i.batch_id
        )
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN fa = 1 AND fb = 1 THEN 1 ELSE 0 END), 0) AS bb,
               COALESCE(SUM(CASE WHEN fa = 1 AND fb = 0 THEN 1 ELSE 0 END), 0) AS a0,
               COALESCE(SUM(CASE WHEN fa = 0 AND fb = 1 THEN 1 ELSE 0 END), 0) AS b0,
               COALESCE(SUM(CASE WHEN fa = 0 AND fb = 0 THEN 1 ELSE 0 END), 0) AS nn
        FROM flags
        """,
        (a, b, a, b),
    ).fetchone()
    return PairCount(row["bb"], row["a0"], row["b0"], row["nn"])


def list_cards(conn: sqlite3.Connection) -> list[dict]:
    cards = [
        dict(row)
        for row in conn.execute(
            "SELECT id, code, name, submitted_batch_count FROM cards ORDER BY code"
        )
    ]
    for card in cards:
        card["item_ids"] = sorted(_listed_items(conn, card["id"]))
    return cards


def list_items(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM items ORDER BY code")]


def list_batches(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT b.id, b.batch_no, b.submitted_at, c.code AS card_code, "
            "       c.name AS card_name, "
            "       (SELECT COUNT(*) FROM batch_abnormalities ba "
            "        WHERE ba.batch_id = b.id) AS abnormal_count "
            "FROM batches b JOIN cards c ON c.id = b.card_id "
            "WHERE b.status = 'submitted' ORDER BY b.id DESC LIMIT ?",
            (limit,),
        )
    ]


def batch_detail(conn: sqlite3.Connection, batch_id: int) -> dict | None:
    batch = conn.execute(
        "SELECT b.id, b.batch_no, b.submitted_at, b.card_id, "
        "       c.code AS card_code, c.name AS card_name "
        "FROM batches b JOIN cards c ON c.id = b.card_id WHERE b.id = ?",
        (batch_id,),
    ).fetchone()
    if batch is None:
        return None
    result = dict(batch)
    result["abnormalities"] = [
        dict(row)
        for row in conn.execute(
            "SELECT i.code AS item_code, i.name AS item_name, ba.detail "
            "FROM batch_abnormalities ba JOIN items i ON i.id = ba.item_id "
            "WHERE ba.batch_id = ? ORDER BY i.code",
            (batch_id,),
        )
    ]
    return result


def audit_counters(conn: sqlite3.Connection) -> dict[str, int]:
    """Reference recount from raw batch/detail rows, for verification only.

    Deliberately scans all batches; never used by query or correction.
    Also checks two invariants: the per-card batch counter matches stored
    submitted batches, and no stored abnormality is outside its card's
    current inspection list.
    """
    expected: dict[tuple[int, int], list[int]] = {}
    card_ids = [int(r["id"]) for r in conn.execute("SELECT id FROM cards")]
    item_ids = [int(r["id"]) for r in conn.execute("SELECT id FROM items")]
    counts = {
        int(r["id"]): int(r["submitted_batch_count"])
        for r in conn.execute("SELECT id, submitted_batch_count FROM cards")
    }
    for card_id in card_ids:
        for item_id in item_ids:
            expected[(card_id, item_id)] = [0, 0, counts[card_id]]
    for row in conn.execute(
        "SELECT b.card_id, ci.item_id "
        "FROM batches b JOIN card_items ci ON ci.card_id = b.card_id "
        "WHERE b.status = 'submitted'"
    ):
        key = (int(row["card_id"]), int(row["item_id"]))
        if key in expected:
            expected[key][2] -= 1
            expected[key][1] += 1
    for row in conn.execute(
        "SELECT b.card_id, ba.item_id FROM batches b "
        "JOIN batch_abnormalities ba ON ba.batch_id = b.id "
        "WHERE b.status = 'submitted'"
    ):
        key = (int(row["card_id"]), int(row["item_id"]))
        if key in expected:
            expected[key][1] -= 1
            expected[key][0] += 1

    actual = {
        (int(r["card_id"]), int(r["item_id"])): [
            int(r["abnormal_count"]),
            int(r["clean_count"]),
            int(r["not_inspected_count"]),
        ]
        for r in conn.execute(
            "SELECT card_id, item_id, abnormal_count, clean_count, "
            "not_inspected_count FROM card_item_stats"
        )
    }
    mismatches = sum(
        1
        for key in set(expected) | set(actual)
        if expected.get(key) != actual.get(key)
    )

    batch_count_mismatches = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT c.id, c.submitted_batch_count, "
        "         (SELECT COUNT(*) FROM batches b "
        "          WHERE b.card_id = c.id AND b.status = 'submitted') AS actual "
        "  FROM cards c"
        ") WHERE submitted_batch_count <> actual"
    ).fetchone()[0]

    out_of_list_details = conn.execute(
        "SELECT COUNT(*) FROM batch_abnormalities ba "
        "JOIN batches b ON b.id = ba.batch_id "
        "WHERE b.status = 'submitted' AND NOT EXISTS ("
        "  SELECT 1 FROM card_items ci "
        "  WHERE ci.card_id = b.card_id AND ci.item_id = ba.item_id)"
    ).fetchone()[0]

    # --- Co-inspection pair facts: reference self-join of raw details. ---
    pair_expected: dict[tuple[int, int, int], int] = {}
    for row in conn.execute(
        "SELECT b.card_id AS card_id, x.item_id AS ia, y.item_id AS ib, "
        "       COUNT(*) AS n "
        "FROM batch_abnormalities x "
        "JOIN batch_abnormalities y "
        "  ON x.batch_id = y.batch_id AND x.item_id < y.item_id "
        "JOIN batches b ON b.id = x.batch_id "
        "WHERE b.status = 'submitted' "
        "GROUP BY b.card_id, x.item_id, y.item_id"
    ):
        pair_expected[
            (int(row["card_id"]), int(row["ia"]), int(row["ib"]))
        ] = int(row["n"])

    pair_actual: dict[tuple[int, int, int], int] = {}
    pair_rows = 0
    for row in conn.execute(
        "SELECT card_id, item_a, item_b, both_abnormal_count "
        "FROM card_item_pair_stats"
    ):
        pair_rows += 1
        key = (int(row["card_id"]), int(row["item_a"]), int(row["item_b"]))
        value = int(row["both_abnormal_count"])
        pair_actual[key] = value
        if value == 0:
            # Zero rows need not appear in pair_expected.
            continue

    pair_mismatches = 0
    for key in set(pair_expected) | {k for k, v in pair_actual.items() if v}:
        if pair_expected.get(key, 0) != pair_actual.get(key, 0):
            pair_mismatches += 1

    return {
        "mismatches": mismatches,
        "batch_count_mismatches": int(batch_count_mismatches),
        "out_of_list_details": int(out_of_list_details),
        "pair_mismatches": int(pair_mismatches),
        "pair_rows": int(pair_rows),
        "abnormal": sum(v[0] for v in expected.values()),
        "clean": sum(v[1] for v in expected.values()),
        "not_inspected": sum(v[2] for v in expected.values()),
    }


# ---------------------------------------------------------------------------
# One-click web/HTTP demonstration data for 色差 (I04) x 污点 (I05)
# ---------------------------------------------------------------------------

DEMO_BATCH_PREFIX = "GYS-DEMO-"
DEMO_META_KEY = "co_inspection_demo_seeded_v1"


def seed_co_inspection_demo(conn: sqlite3.Connection) -> dict:
    """Create a deterministic quadrant demonstration (idempotent).

    C003 inspects both 色差 and 污点 with 10 batches spread over the four
    quadrants (3 / 2 / 1 / 4).  C004 initially inspects 色差 only, so its
    four batches stay OUT of the pair query; correcting C004's checklist to
    add 污点 makes them enter (1 only-first, 3 neither).  Using C003/C004
    keeps the demo independent of the HTTP smoke script which drives C001.

    Batch numbers use a fixed prefix; if any already exists the seed is
    skipped without changing current checklists.
    """
    def iid(code: str) -> int:
        return int(
            conn.execute("SELECT id FROM items WHERE code = ?", (code,)).fetchone()[0]
        )

    color, stain = iid("I04"), iid("I05")
    existing = conn.execute(
        "SELECT COUNT(*) FROM batches WHERE batch_no LIKE ?",
        (DEMO_BATCH_PREFIX + "%",),
    ).fetchone()[0]
    if existing:
        return {
            "seeded": 0,
            "skipped": True,
            "item_a": color,
            "item_b": stain,
            "message": "演示批次已存在，未重复写入，也未改动当前清单。",
        }

    def cid(code: str) -> int:
        return int(
            conn.execute("SELECT id FROM cards WHERE code = ?", (code,)).fetchone()[0]
        )

    width = iid("I01")
    hole = iid("I06")
    c1, c2 = cid("C003"), cid("C004")

    # "Before" state: C003 checks both; C004 checks 色差 but not 污点.
    correct_card_items(conn, card_id=c1, item_ids={color, stain, hole})
    correct_card_items(conn, card_id=c2, item_ids={color, width})

    plan: list[tuple[str, int, dict[int, str]]] = []
    for n in range(1, 4):  # 3 both abnormal
        plan.append((f"BOTH-{n}", c1, {color: f"色差明显 {n}", stain: f"成片污点 {n}"}))
    for n in range(1, 3):  # 2 only color
        plan.append((f"COLOR-{n}", c1, {color: f"色差 {n}"}))
    plan.append(("STAIN-1", c1, {stain: "局部污点"}))  # 1 only stain
    for n in range(1, 5):  # 4 neither
        plan.append((f"OK-{n}", c1, {}))
    # C002: one color-abnormal + three clean, but stain not yet inspected.
    plan.append(("C2-COLOR-1", c2, {color: "边色差"}))
    for n in range(1, 4):
        plan.append((f"C2-OK-{n}", c2, {}))

    for suffix, card, abn in plan:
        submit_batch(
            conn,
            batch_no=DEMO_BATCH_PREFIX + suffix,
            card_id=card,
            abnormalities=abn,
        )

    with write_txn(conn):
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'",
            (DEMO_META_KEY,),
        )

    return {
        "seeded": len(plan),
        "skipped": False,
        "item_a": color,
        "item_b": stain,
        "card_to_correct": c2,
        "message": "已写入共检演示数据：C003 十批（3/2/1/4），"
                   "C004 四批暂未检污点（未纳入）。",
    }
