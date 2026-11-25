"""End-to-end HTTP smoke test against a running server.

Usage: python3 scripts/http_smoke.py http://127.0.0.1:8099 [--concurrency 8]

Uses only HTTP: register an inspection list, submit batches, read per-item
counts before and after a correction, read the two-item co-inspection four
quadrants and verify inclusion follows the current checklist, confirm
out-of-list submissions and detail-dropping corrections are rejected whole,
race duplicate batch numbers, then check the audit page reports zero counter
mismatches (single-item and pair).

Run against a fresh database (the script assumes E2E-* batch numbers are new).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode


def get(base: str, path: str) -> str:
    with urllib.request.urlopen(base + path) as resp:
        return resp.read().decode()


def get_json(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path) as resp:
        return json.loads(resp.read().decode())


def post(base: str, path: str, data: dict[str, list[str] | str]) -> str:
    pairs: list[tuple[str, str]] = []
    for key, value in data.items():
        if isinstance(value, list):
            pairs.extend((key, item) for item in value)
        else:
            pairs.append((key, value))
    req = urllib.request.Request(
        base + path, data=urlencode(pairs).encode(), method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode()


def counts(base: str, item_id: int) -> dict[str, int]:
    html = get(base, f"/query?item_id={item_id}")
    out = {}
    for key in ("abnormal", "clean", "not_inspected"):
        match = re.search(rf'data-count="{key}">(\d+)<', html)
        if not match:
            raise AssertionError(f"count {key} missing from query page")
        out[key] = int(match.group(1))
    return out


def pair_counts(base: str, a: int, b: int) -> dict[str, int]:
    html = get(base, f"/query?item_a={a}&item_b={b}")
    out = {}
    for key in ("both_abnormal", "only_a_abnormal", "only_b_abnormal",
                "neither_abnormal", "total"):
        match = re.search(rf'data-count="{key}">(\d+)<', html)
        if not match:
            raise AssertionError(f"pair count {key} missing:\n{html[:500]}")
        out[key] = int(match.group(1))
    return out


def discover(base: str) -> tuple[dict[str, int], dict[str, int]]:
    index = get(base, "/")
    cards = {
        label: int(cid)
        for cid, label in re.findall(
            r'<option value="(\d+)"[^>]*>\s*(C\d+)', index
        )
    }
    query = get(base, "/query")
    items = {
        label: int(iid)
        for iid, label in re.findall(
            r'<option value="(\d+)"[^>]*>\s*(I\d+)', query
        )
    }
    return cards, items


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    base = args.base.rstrip("/")

    cards, items = discover(base)
    if not cards or not items:
        print("could not discover ids", cards, items)
        return 1
    card = cards["C001"]
    i01, i02, i03 = items["I01"], items["I02"], items["I03"]

    post(base, f"/cards/{card}/items", {"item_ids": [str(i01), str(i02)]})
    post(base, "/batches", {"batch_no": "E2E-1", "card_id": str(card),
                            "abnormal_item": [str(i01)],
                            f"detail_{i01}": "原异常明细"})
    post(base, "/batches", {"batch_no": "E2E-2", "card_id": str(card)})

    before = {code: counts(base, iid)
              for code, iid in (("I01", i01), ("I02", i02), ("I03", i03))}
    print("before correction:", before)
    assert before["I01"] == {"abnormal": 1, "clean": 1, "not_inspected": 0}
    assert before["I03"] == {"abnormal": 0, "clean": 0, "not_inspected": 2}

    # Two-item co-inspection while C001 lists both I01 and I02:
    # E2E-1 only-I01 abnormal, E2E-2 neither abnormal.
    pair_before = pair_counts(base, i01, i02)
    print("pair I01xI02 before:", pair_before)
    assert pair_before == {
        "both_abnormal": 0, "only_a_abnormal": 1, "only_b_abnormal": 0,
        "neither_abnormal": 1, "total": 2,
    }, pair_before
    # Orientation swap swaps the two only-cells (here both are 1/0 -> 0/1).
    swapped = pair_counts(base, i02, i01)
    assert swapped["only_a_abnormal"] == 0 and swapped["only_b_abnormal"] == 1

    post(base, f"/cards/{card}/items", {"item_ids": [str(i01), str(i03)]})
    after = {code: counts(base, iid)
             for code, iid in (("I01", i01), ("I02", i02), ("I03", i03))}
    print("after correction: ", after)
    assert after["I01"] == {"abnormal": 1, "clean": 1, "not_inspected": 0}
    assert after["I02"] == {"abnormal": 0, "clean": 0, "not_inspected": 2}
    assert after["I03"] == {"abnormal": 0, "clean": 2, "not_inspected": 0}

    # I02 left the current list -> the I01xI02 pair now includes ZERO
    # batches; I01xI03 includes both history batches (I01-only + neither).
    pair_out = pair_counts(base, i01, i02)
    print("pair I01xI02 after I02 removed:", pair_out)
    assert pair_out["total"] == 0 and pair_out["both_abnormal"] == 0, pair_out
    pair_in = pair_counts(base, i01, i03)
    print("pair I01xI03 after I03 added:", pair_in)
    assert pair_in == {
        "both_abnormal": 0, "only_a_abnormal": 1, "only_b_abnormal": 0,
        "neither_abnormal": 1, "total": 2,
    }, pair_in

    # JSON API agrees with the HTML page.
    payload = get_json(base, f"/api/co-inspection?item_a={i01}&item_b={i03}")
    assert payload["ok"] and payload["counts"]["only_a_abnormal"] == 1

    # Choosing the same item twice yields a friendly inline error, not 5xx.
    same = get(base, f"/query?item_a={i01}&item_b={i01}")
    assert 'data-role="pair-error"' in same and "两个不同" in same

    # Original detail survives the correction.
    assert "原异常明细" in get(base, "/batches/1")

    # Out-of-list abnormality (I02 is no longer listed) rejected whole.
    post(base, "/batches", {"batch_no": "E2E-BAD", "card_id": str(card),
                            "abnormal_item": [str(i02)]})
    assert counts(base, i02) == {"abnormal": 0, "clean": 0, "not_inspected": 2}
    assert "整批退回" in get(base, "/")

    # Correction that would drop the item with an abnormality rejected whole.
    post(base, f"/cards/{card}/items", {"item_ids": [str(i03)]})
    assert counts(base, i01) == {"abnormal": 1, "clean": 1, "not_inspected": 0}
    assert "整次退回" in get(base, "/")

    # Concurrent duplicate submissions: exactly one may commit.
    def attempt(_i: int) -> None:
        post(base, "/batches", {"batch_no": "E2E-RACE", "card_id": str(card)})

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(attempt, range(args.concurrency)))
    index = get(base, "/")
    committed = len(re.findall(r">E2E-RACE<", index))
    print("E2E-RACE committed rows:", committed)
    assert committed == 1, committed

    audit = get(base, "/audit")
    assert "计数不一致的 (卡, 项目) 数</td><td>0</td>" in audit, "counter mismatch"
    assert 'data-audit="pair_mismatches">0<' in audit, "pair counter mismatch"
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
