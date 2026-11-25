from __future__ import annotations

import sqlite3
import threading

import pytest

from app import create_app
from app import service


@pytest.fixture()
def app(tmp_path):
    db_path = str(tmp_path / "test_co.sqlite3")
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": db_path,
            "SECRET_KEY": "test",
            "SQLITE_BUSY_TIMEOUT_MS": 300,
        }
    )
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def connect(app) -> sqlite3.Connection:
    conn = sqlite3.connect(
        app.config["DATABASE"], timeout=20, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn


def item_id(conn, code: str) -> int:
    return int(conn.execute("SELECT id FROM items WHERE code = ?", (code,)).fetchone()[0])


def card_id(conn, code: str) -> int:
    return int(conn.execute("SELECT id FROM cards WHERE code = ?", (code,)).fetchone()[0])


def set_list(conn, card: int, codes: list[str]) -> dict[str, int]:
    ids = {item_id(conn, code) for code in codes}
    return service.correct_card_items(conn, card_id=card, item_ids=ids)


def submit(conn, batch_no: str, card: int, abnormalities: dict[str, str] | None = None):
    ids = {
        item_id(conn, code): detail
        for code, detail in (abnormalities or {}).items()
    }
    return service.submit_batch(
        conn, batch_no=batch_no, card_id=card, abnormalities=ids
    )


def pair(conn, code_a: str, code_b: str) -> service.PairCount:
    return service.query_item_pair(
        conn, item_id(conn, code_a), item_id(conn, code_b)
    )


# ---------------------------------------------------------------------------
# Quadrant correctness
# ---------------------------------------------------------------------------

def test_pair_quadrants_match_reference_across_cards(app):
    conn = connect(app)
    c1 = card_id(conn, "C001")
    c2 = card_id(conn, "C002")
    c3 = card_id(conn, "C003")
    set_list(conn, c1, ["I04", "I05", "I06"])
    set_list(conn, c2, ["I04", "I05"])
    set_list(conn, c3, ["I04"])  # missing I05 -> never included

    # C1: 2 both, 3 only I04, 1 only I05, 4 neither
    for i in range(2):
        submit(conn, f"C1-BOTH-{i}", c1, {"I04": "s", "I05": "d"})
    for i in range(3):
        submit(conn, f"C1-A-{i}", c1, {"I04": "s"})
    submit(conn, "C1-B-0", c1, {"I05": "d"})
    for i in range(4):
        submit(conn, f"C1-N-{i}", c1)

    # C2: 1 both, 0 only A, 2 only B, 1 neither
    submit(conn, "C2-BOTH", c2, {"I04": "s", "I05": "d"})
    for i in range(2):
        submit(conn, f"C2-B-{i}", c2, {"I05": "d"})
    submit(conn, "C2-N", c2)

    # C3: excluded even though I04 has abnormalities
    submit(conn, "C3-A", c3, {"I04": "s"})
    submit(conn, "C3-N", c3)

    got = pair(conn, "I04", "I05")
    # C1 (2,3,1,4) + C2 (1,0,2,1); C3 excluded.
    assert got == {"both_abnormal": 3, "only_a_abnormal": 3,
                   "only_b_abnormal": 3, "neither_abnormal": 5}
    assert got.total == 14
    assert sum(got.values()) == 14

    ref = service.reference_pair_counts(
        conn, item_id(conn, "I04"), item_id(conn, "I05")
    )
    assert ref == got

    # Swapping orientation swaps the two "only" cells only.
    swapped = pair(conn, "I05", "I04")
    assert (swapped.both_abnormal, swapped.only_a_abnormal,
            swapped.only_b_abnormal, swapped.neither_abnormal) == (3, 3, 3, 5)
    assert swapped.only_a_abnormal == got.only_b_abnormal
    assert swapped.only_b_abnormal == got.only_a_abnormal

    # Alias spellings and tuple equality all work.
    assert got["both"] == 3
    assert got["only_a"] == 3
    assert got["only_second"] == 3
    assert got["neither"] == 5
    assert got == (3, 3, 3, 5)
    assert got == {"both": 3, "only_first_abnormal": 3,
                   "only_second_abnormal": 3, "neither": 5, "total": 14}

    assert service.audit_counters(conn)["pair_mismatches"] == 0
    conn.close()


def test_inclusion_follows_current_checklist_after_correction(app):
    conn = connect(app)
    c2 = card_id(conn, "C002")
    set_list(conn, c2, ["I04"])  # I05 not inspected yet
    submit(conn, "H-1", c2, {"I04": "色差"})
    submit(conn, "H-2", c2)
    submit(conn, "H-3", c2)

    # Not included while I05 is absent from the current list.
    before = pair(conn, "I04", "I05")
    assert before.total == 0
    assert before == (0, 0, 0, 0)

    # Correct the checklist: add I05.  History enters as I05-clean.
    set_list(conn, c2, ["I04", "I05"])
    entered = pair(conn, "I04", "I05")
    assert entered == {"both_abnormal": 0, "only_a_abnormal": 1,
                       "only_b_abnormal": 0, "neither_abnormal": 2}

    # Remove I05 again (it has no abnormal detail): history exits.
    set_list(conn, c2, ["I04"])
    assert pair(conn, "I04", "I05") == (0, 0, 0, 0)

    # Re-add: same counts return.
    set_list(conn, c2, ["I04", "I05"])
    assert pair(conn, "I04", "I05") == entered
    assert service.audit_counters(conn)["pair_mismatches"] == 0
    conn.close()


def test_same_or_missing_items_rejected(app):
    conn = connect(app)
    i04 = item_id(conn, "I04")
    with pytest.raises(service.DomainError):
        service.query_item_pair(conn, i04, i04)
    with pytest.raises(service.DomainError):
        service.query_item_pair(conn, None, i04)
    conn.close()


def test_pair_rows_are_fixed_size(app):
    conn = connect(app)
    cards = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    rows = conn.execute("SELECT COUNT(*) FROM card_item_pair_stats").fetchone()[0]
    from math import comb
    assert rows == cards * comb(items, 2)
    assert rows == 60  # 4 cards x C(6,2)
    conn.close()


# ---------------------------------------------------------------------------
# Constant read/write work
# ---------------------------------------------------------------------------

def _progress_steps(conn, fn) -> int:
    counter = {"n": 0}

    def progress() -> int:
        counter["n"] += 1
        return 0

    conn.set_progress_handler(progress, 1)
    try:
        fn()
    finally:
        conn.set_progress_handler(None, 0)
    return counter["n"]


def test_pair_query_work_constant_in_batch_count(app):
    conn = connect(app)
    c1 = card_id(conn, "C001")
    i04, i05 = item_id(conn, "I04"), item_id(conn, "I05")
    set_list(conn, c1, ["I04", "I05"])
    submit(conn, "S-1", c1, {"I04": "s", "I05": "d"})
    submit(conn, "S-2", c1, {"I04": "s"})

    agg_small = _progress_steps(conn, lambda: service.query_item_pair(conn, i04, i05))
    state_small = _progress_steps(conn, lambda: service.pair_query_state(conn, i04, i05))

    for i in range(300):
        submit(conn, f"BIG-{i}", c1)

    agg_big = _progress_steps(conn, lambda: service.query_item_pair(conn, i04, i05))
    state_big = _progress_steps(conn, lambda: service.pair_query_state(conn, i04, i05))

    assert agg_small == agg_big, (agg_small, agg_big)
    assert state_small == state_big, (state_small, state_big)
    # And the value is still correct after the growth.
    counts = service.query_item_pair(conn, i04, i05)
    assert counts.total == 302
    assert counts.both_abnormal == 1
    assert service.audit_counters(conn)["pair_mismatches"] == 0
    conn.close()


def test_pair_record_work_independent_of_total_item_count(app):
    conn = connect(app)
    c1 = card_id(conn, "C001")
    ids3 = [item_id(conn, c) for c in ("I01", "I02", "I03")]

    steps_m6 = _progress_steps(
        conn, lambda: service.record_pair_abnormalities(conn, c1, ids3)
    )
    traces6: list[str] = []
    conn.set_trace_callback(lambda sql: traces6.append(sql) if "card_item_pair_stats" in sql else None)
    touched = service.record_pair_abnormalities(conn, c1, ids3)
    conn.set_trace_callback(None)
    assert touched == 3  # C(3,2)
    assert len([s for s in traces6 if s.startswith(("INSERT", "UPDATE"))]) == 3

    # Grow the global item dictionary (and fixed pair rows) substantially.
    top = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM items").fetchone()[0])
    extra = 40
    for j in range(extra):
        conn.execute(
            "INSERT INTO items(id, code, name) VALUES (?, ?, ?)",
            (top + 1 + j, f"X{j:03d}", f"extra {j}"),
        )
    conn.execute(
        "INSERT OR IGNORE INTO card_item_pair_stats"
        "(card_id, item_a, item_b, both_abnormal_count) "
        "SELECT c.id, i.id, j.id, 0 FROM cards c CROSS JOIN items i "
        "JOIN items j ON j.id > i.id"
    )

    steps_m46 = _progress_steps(
        conn, lambda: service.record_pair_abnormalities(conn, c1, ids3)
    )
    assert steps_m6 == steps_m46, (steps_m6, steps_m46)

    # Fixed abnormal count k=2 -> exactly one pair record, regardless of M.
    ids2 = [item_id(conn, "I04"), item_id(conn, "I05")]
    statements: list[str] = []
    conn.set_trace_callback(lambda sql: statements.append(sql) if "card_item_pair_stats" in sql else None)
    assert service.record_pair_abnormalities(conn, c1, ids2) == 1
    conn.set_trace_callback(None)
    assert len([s for s in statements if s.startswith(("INSERT", "UPDATE"))]) == 1
    conn.close()


# ---------------------------------------------------------------------------
# Atomic approval: pair stats commit/rollback with batch + details
# ---------------------------------------------------------------------------

def test_failed_approval_rolls_pair_stats_back(app, monkeypatch):
    conn = connect(app)
    c1 = card_id(conn, "C001")
    i01, i02 = item_id(conn, "I01"), item_id(conn, "I02")
    set_list(conn, c1, ["I01", "I02", "I03"])
    submit(conn, "OK-1", c1, {"I01": "a", "I02": "b"})
    before = service.query_item_pair(conn, i01, i02)
    assert before.both_abnormal == 1
    item_before = service.query_item(conn, i01)

    def boom(*_a, **_k):
        raise RuntimeError("simulated pair-stat failure")

    monkeypatch.setattr(service, "record_pair_abnormalities", boom)
    with pytest.raises(RuntimeError):
        submit(conn, "FAIL-1", c1, {"I01": "a", "I02": "b", "I03": "c"})
    monkeypatch.undo()

    # No half batch, detail, item stat or pair stat survives.
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM batches WHERE batch_no = 'FAIL-1'"
    ).fetchone()[0] == 0
    assert service.query_item_pair(conn, i01, i02) == before
    assert service.query_item(conn, i01) == item_before
    assert service.audit_counters(conn)["pair_mismatches"] == 0
    conn.close()


def test_duplicate_batch_does_not_touch_pair_stats(app):
    conn = connect(app)
    c1 = card_id(conn, "C001")
    i01, i02 = item_id(conn, "I01"), item_id(conn, "I02")
    set_list(conn, c1, ["I01", "I02"])
    submit(conn, "DUP", c1, {"I01": "a", "I02": "b"})
    before = service.query_item_pair(conn, i01, i02)
    with pytest.raises(service.DomainError):
        submit(conn, "DUP", c1, {"I01": "a", "I02": "b"})
    assert service.query_item_pair(conn, i01, i02) == before
    conn.close()


# ---------------------------------------------------------------------------
# Backfill from original batches + old-database upgrade
# ---------------------------------------------------------------------------

def test_rebuild_pair_stats_from_raw_details(app):
    conn = connect(app)
    c1 = card_id(conn, "C001")
    set_list(conn, c1, ["I04", "I05", "I06"])
    submit(conn, "R-1", c1, {"I04": "s", "I05": "d", "I06": "h"})
    submit(conn, "R-2", c1, {"I04": "s", "I05": "d"})
    submit(conn, "R-3", c1, {"I04": "s"})

    expected = service.query_item_pair(conn, item_id(conn, "I04"), item_id(conn, "I05"))
    assert expected.both_abnormal == 2

    # Wipe materialised pair facts, then rebuild from raw batches.
    conn.execute("UPDATE card_item_pair_stats SET both_abnormal_count = 0")
    assert service.query_item_pair(
        conn, item_id(conn, "I04"), item_id(conn, "I05")
    ).both_abnormal == 0

    result = service.rebuild_pair_stats(conn)
    assert result["both_abnormal_total"] >= 3  # pairs: (4,5)x2 + (4,6) + (5,6)
    rebuilt = service.query_item_pair(conn, item_id(conn, "I04"), item_id(conn, "I05"))
    assert rebuilt == expected
    # Idempotent.
    again = service.rebuild_pair_stats(conn)
    assert again == result
    assert service.audit_counters(conn)["pair_mismatches"] == 0
    conn.close()


_OLD_SCHEMA = """
CREATE TABLE items (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL);
CREATE TABLE cards (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  submitted_batch_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE card_items (card_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
  PRIMARY KEY (card_id, item_id)) WITHOUT ROWID;
CREATE TABLE batches (id INTEGER PRIMARY KEY, batch_no TEXT NOT NULL UNIQUE,
  card_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'submitted',
  submitted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE TABLE batch_abnormalities (batch_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '', PRIMARY KEY (batch_id, item_id)) WITHOUT ROWID;
CREATE TABLE card_item_stats (card_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
  abnormal_count INTEGER NOT NULL DEFAULT 0, clean_count INTEGER NOT NULL DEFAULT 0,
  not_inspected_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (card_id, item_id)) WITHOUT ROWID;
"""


def test_old_database_pair_stats_auto_backfill(tmp_path):
    db_path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(str(db_path), isolation_level=None)
    raw.executescript(_OLD_SCHEMA)
    raw.executemany("INSERT INTO items(id, code, name) VALUES (?,?,?)", [
        (1, "I01", "幅宽"), (2, "I02", "克重"), (3, "I03", "纬斜"),
        (4, "I04", "色差"), (5, "I05", "污点"), (6, "I06", "破洞"),
    ])
    raw.executemany("INSERT INTO cards(id, code, name, submitted_batch_count) VALUES (?,?,?,?)", [
        (1, "C001", "A", 3), (2, "C002", "B", 0),
        (3, "C003", "C", 0), (4, "C004", "D", 0),
    ])
    raw.execute("INSERT INTO card_items(card_id,item_id) VALUES (1,4),(1,5)")
    raw.executemany("INSERT INTO batches(id,batch_no,card_id) VALUES (?,?,?)", [
        (1, "OLD-1", 1), (2, "OLD-2", 1), (3, "OLD-3", 1),
    ])
    raw.executemany(
        "INSERT INTO batch_abnormalities(batch_id,item_id,detail) VALUES (?,?,?)",
        [(1, 4, "色差"), (1, 5, "污点"), (2, 4, "色差")],
    )
    # Old materialised single-item stats, as the old app would have them.
    raw.executemany(
        "INSERT INTO card_item_stats(card_id,item_id,abnormal_count,clean_count,"
        "not_inspected_count) VALUES (?,?,?,?,?)",
        [(1, 4, 2, 1, 0), (1, 5, 1, 2, 0)],
    )
    raw.close()

    # Opening with the new app must migrate + one-pass backfill automatically.
    app = create_app({"TESTING": True, "DATABASE": str(db_path), "SECRET_KEY": "t"})
    with app.app_context():
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key='pair_stats_backfilled_v1'"
        ).fetchone()
        assert row is not None
        counts = service.query_item_pair(conn, 4, 5)
        assert counts == {"both_abnormal": 1, "only_a_abnormal": 1,
                          "only_b_abnormal": 0, "neither_abnormal": 1}
        assert counts.total == 3
        assert service.audit_counters(conn)["pair_mismatches"] == 0
        conn.close()


def test_backfill_cli_command(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["backfill-co-stats"])
    assert result.exit_code == 0, result.output
    assert "rebuilt" in result.output


# ---------------------------------------------------------------------------
# HTTP / web behaviour
# ---------------------------------------------------------------------------

def test_web_pair_query_renders_counts(client, app):
    conn = connect(app)
    c1 = card_id(conn, "C001")
    i04, i05 = item_id(conn, "I04"), item_id(conn, "I05")
    set_list(conn, c1, ["I04", "I05"])
    submit(conn, "W-1", c1, {"I04": "s", "I05": "d"})
    submit(conn, "W-2", c1, {"I04": "s"})
    submit(conn, "W-3", c1)
    conn.close()

    resp = client.get(f"/query?item_a={i04}&item_b={i05}")
    assert resp.status_code == 200
    body = resp.data
    assert 'data-count="both_abnormal">1<'.encode() in body
    assert 'data-count="only_a_abnormal">1<'.encode() in body
    assert 'data-count="only_b_abnormal">0<'.encode() in body
    assert 'data-count="neither_abnormal">1<'.encode() in body
    assert 'data-count="total">3<'.encode() in body
    assert "两项均异常".encode() in body
    assert "仅前项".encode() in body
    assert "仅后项".encode() in body
    assert "两项均无异常".encode() in body
    # Alias markers for alternate naming.
    assert b'data-count="both">1<' in body
    assert b'data-count="neither">1<' in body


def test_web_pair_same_item_shows_inline_error(client, app):
    conn = connect(app)
    i04 = item_id(conn, "I04")
    conn.close()
    resp = client.get(f"/query?item_a={i04}&item_b={i04}")
    assert resp.status_code == 200
    assert b'data-role="pair-error"' in resp.data
    assert "两个不同".encode() in resp.data

    resp = client.get(f"/api/co-inspection?item_a={i04}&item_b={i04}")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_web_json_api(client, app):
    conn = connect(app)
    c1 = card_id(conn, "C001")
    i04, i05 = item_id(conn, "I04"), item_id(conn, "I05")
    set_list(conn, c1, ["I04", "I05"])
    submit(conn, "J-1", c1, {"I04": "s", "I05": "d"})
    conn.close()

    resp = client.get(f"/api/co-inspection?item_a={i04}&item_b={i05}")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["counts"]["both_abnormal"] == 1
    assert payload["counts"]["both"] == 1
    assert payload["total"] == 1

    resp = client.get(f"/query?format=json&a={i04}&b={i05}")
    assert resp.status_code == 200
    assert resp.get_json()["counts"]["both_abnormal"] == 1


def test_web_correction_with_next_moves_inclusion(client, app):
    conn = connect(app)
    c2 = card_id(conn, "C002")
    i04, i05 = item_id(conn, "I04"), item_id(conn, "I05")
    set_list(conn, c2, ["I04"])
    submit(conn, "NEXT-1", c2, {"I04": "s"})
    submit(conn, "NEXT-2", c2)
    conn.close()

    url = f"/query?item_a={i04}&item_b={i05}"
    resp = client.get(url)
    assert 'data-count="total">0<'.encode() in resp.data

    # Correct C002 through the inline form and return to the pair page.
    resp = client.post(
        f"/cards/{c2}/items",
        data={
            "item_ids": [str(i04), str(i05)],
            "next": url,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["Location"].endswith(url)

    resp = client.get(url)
    assert resp.status_code == 200
    assert 'data-count="only_a_abnormal">1<'.encode() in resp.data
    assert 'data-count="neither_abnormal">1<'.encode() in resp.data
    assert 'data-count="total">2<'.encode() in resp.data


def test_demo_seed_before_and_after_card_correction(client, app):
    conn = connect(app)
    c4 = card_id(conn, "C004")
    i01 = item_id(conn, "I01")
    i04, i05 = item_id(conn, "I04"), item_id(conn, "I05")
    conn.close()

    resp = client.post("/demo/co-inspection/seed", follow_redirects=True)
    assert resp.status_code == 200
    # Before: only C001's 10 batches are included (3/2/1/4).
    assert 'data-count="both_abnormal">3<'.encode() in resp.data
    assert 'data-count="only_a_abnormal">2<'.encode() in resp.data
    assert 'data-count="only_b_abnormal">1<'.encode() in resp.data
    assert 'data-count="neither_abnormal">4<'.encode() in resp.data
    assert 'data-count="total">10<'.encode() in resp.data

    # Idempotent: a second seed does not duplicate batches.
    client.post("/demo/co-inspection/seed", follow_redirects=True)
    conn = connect(app)
    n_demo = conn.execute(
        "SELECT COUNT(*) FROM batches WHERE batch_no LIKE 'GYS-DEMO-%'"
    ).fetchone()[0]
    assert n_demo == 14
    conn.close()

    # Correct C004 (demo list was I04+I01) to also inspect I05.
    pair_url = f"/query?item_a={i04}&item_b={i05}"
    resp = client.post(
        f"/cards/{c4}/items",
        data={
            "item_ids": [str(i01), str(i04), str(i05)],
            "next": pair_url,
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # C002 contributes 1 only-first + 3 neither.
    assert 'data-count="only_a_abnormal">3<'.encode() in resp.data
    assert 'data-count="neither_abnormal">7<'.encode() in resp.data
    assert 'data-count="total">14<'.encode() in resp.data


def test_concurrent_pair_submissions_keep_stats_consistent(app):
    conn = connect(app)
    c1 = card_id(conn, "C001")
    set_list(conn, c1, ["I01", "I02"])
    conn.close()

    barrier = threading.Barrier(4)
    errors: list[str] = []

    def worker(idx: int) -> None:
        c = connect(app)
        try:
            barrier.wait()
            service.submit_batch(
                c,
                batch_no=f"PAR-{idx}",
                card_id=c1,
                abnormalities={
                    item_id(c, "I01"): "a",
                    item_id(c, "I02"): "b",
                },
            )
        except service.DomainError as exc:
            errors.append(f"rejected:{exc.status}")
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check = connect(app)
    assert errors == []
    assert check.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 4
    counts = pair(check, "I01", "I02")
    assert counts.both_abnormal == 4
    assert counts.total == 4
    assert service.audit_counters(check)["pair_mismatches"] == 0
    check.close()
