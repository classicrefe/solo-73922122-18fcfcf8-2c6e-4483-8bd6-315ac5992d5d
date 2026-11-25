"""HTTP demonstration of two-item co-inspection before/after a list correction.

Usage:
    python3 scripts/demo_coinspect.py http://127.0.0.1:8000

It triggers the idempotent web demo seed over HTTP, reads the
色差(I04) x 污点(I05) four quadrants ("before": C004 does not inspect 污点),
then corrects C004's checklist to add 污点 and reads them again ("after":
C004's four historical batches enter inclusion).
"""
from __future__ import annotations

import re
import sys
import urllib.request
from urllib.parse import urlencode


def get(base: str, path: str) -> str:
    with urllib.request.urlopen(base + path) as resp:
        return resp.read().decode()


def post(base: str, path: str, data: dict[str, list[str] | str]) -> str:
    pairs: list[tuple[str, str]] = []
    for key, value in data.items():
        if isinstance(value, list):
            pairs.extend((key, v) for v in value)
        else:
            pairs.append((key, value))
    req = urllib.request.Request(
        base + path, data=urlencode(pairs).encode(), method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode()


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


def pair_counts(html: str) -> dict[str, int]:
    out = {}
    for key in ("both_abnormal", "only_a_abnormal", "only_b_abnormal",
                "neither_abnormal", "total"):
        m = re.search(rf'data-count="{key}">(\d+)<', html)
        if not m:
            raise AssertionError(f"missing {key}")
        out[key] = int(m.group(1))
    return out


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
    cards, items = discover(base)
    c4 = cards["C004"]
    i01, i04, i05 = items["I01"], items["I04"], items["I05"]
    pair_path = f"/query?item_a={i04}&item_b={i05}"

    seeded_html = post(base, "/demo/co-inspection/seed", {})
    before = pair_counts(seeded_html)
    print("更正前（C004 清单不含污点，仅 C003 的 10 批纳入）:")
    print("  两项均异常={both_abnormal} 仅色差异常={only_a_abnormal} "
          "仅污点异常={only_b_abnormal} 均无异常={neither_abnormal} "
          "合计={total}".format(**before))
    assert before == {
        "both_abnormal": 3, "only_a_abnormal": 2, "only_b_abnormal": 1,
        "neither_abnormal": 4, "total": 10,
    }, before

    # Demo C004 list is {I01, I04}; add I05 while preserving the others.
    after_html = post(
        base,
        f"/cards/{c4}/items",
        {"item_ids": [str(i01), str(i04), str(i05)], "next": pair_path},
    )
    after = pair_counts(after_html)
    print("更正后（C004 加入污点，其 4 个历史批按新清单纳入）:")
    print("  两项均异常={both_abnormal} 仅色差异常={only_a_abnormal} "
          "仅污点异常={only_b_abnormal} 均无异常={neither_abnormal} "
          "合计={total}".format(**after))
    assert after == {
        "both_abnormal": 3, "only_a_abnormal": 3, "only_b_abnormal": 1,
        "neither_abnormal": 7, "total": 14,
    }, after

    audit = get(base, "/audit")
    assert 'data-audit="pair_mismatches">0<' in audit
    print("审计页共检不一致数 = 0")
    print("浏览器打开:", base + pair_path)
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
