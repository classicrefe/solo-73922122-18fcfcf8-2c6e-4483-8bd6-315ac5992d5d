from __future__ import annotations

import sqlite3


CARDS = [
    ("C001", "验布作业卡 A"),
    ("C002", "验布作业卡 B"),
    ("C003", "验布作业卡 C"),
    ("C004", "验布作业卡 D"),
]

ITEMS = [
    ("I01", "幅宽"),
    ("I02", "克重"),
    ("I03", "纬斜"),
    ("I04", "色差"),
    ("I05", "污点"),
    ("I06", "破洞"),
]

# Bumped when a new one-time statistics rebuild is required after upgrade.
PAIR_BACKFILL_KEY = "pair_stats_backfilled_v1"


def seed_fixed_dictionary(conn: sqlite3.Connection) -> None:
    """Cards and items are a fixed dictionary; insert idempotently.

    A counter row exists for every (card, item) pair from the start and is
    never added or removed by batch activity, so the table size is fixed at
    cards x items.  Likewise one co-inspection row exists per
    (card, canonical item pair): cards x C(items, 2).

    On an old database that predates the pair table, the missing pair facts
    are rebuilt once from the original batches inside this same transaction.
    """
    from . import service

    conn.execute("BEGIN IMMEDIATE")
    try:
        for code, name in ITEMS:
            conn.execute(
                "INSERT INTO items(code, name) VALUES (?, ?) "
                "ON CONFLICT(code) DO UPDATE SET name = excluded.name",
                (code, name),
            )
        for code, name in CARDS:
            conn.execute(
                "INSERT INTO cards(code, name) VALUES (?, ?) "
                "ON CONFLICT(code) DO UPDATE SET name = excluded.name",
                (code, name),
            )
        conn.execute(
            "INSERT OR IGNORE INTO card_item_stats"
            "(card_id, item_id, abnormal_count, clean_count, not_inspected_count) "
            "SELECT c.id, i.id, 0, 0, 0 FROM cards c CROSS JOIN items i"
        )
        # Fixed-size pair rows for every card and every canonical item pair.
        conn.execute(
            "INSERT OR IGNORE INTO card_item_pair_stats"
            "(card_id, item_a, item_b, both_abnormal_count) "
            "SELECT c.id, i.id, j.id, 0 "
            "FROM cards c CROSS JOIN items i "
            "JOIN items j ON j.id > i.id"
        )

        row = conn.execute(
            "SELECT value FROM app_meta WHERE key = ?",
            (PAIR_BACKFILL_KEY,),
        ).fetchone()
        if row is None:
            # Fresh DB: scans zero detail rows.  Old DB: one-pass补齐 from
            # the preserved original batches.  Runs once, atomically with the
            # dictionary seeding.
            service.rebuild_pair_stats_locked(conn)
            conn.execute(
                "INSERT INTO app_meta(key, value) VALUES(?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value = '1'",
                (PAIR_BACKFILL_KEY,),
            )

        conn.commit()
    except BaseException:
        conn.rollback()
        raise
