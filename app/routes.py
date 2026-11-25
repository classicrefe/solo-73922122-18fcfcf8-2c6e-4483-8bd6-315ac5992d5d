from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import service
from .db import get_db
from .service import DomainError

bp = Blueprint("main", __name__)


def _int(value: str | None) -> int | None:
    try:
        return int(value or "")
    except ValueError:
        return None


def _safe_local_next(target: str | None) -> str | None:
    """Allow redirects only to same-site absolute paths (open-redirect safe)."""
    if not target:
        return None
    # Reject protocol-relative and backslash-normalised variants first.
    if not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return None
    from urllib.parse import urlparse

    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    return target


@bp.get("/")
def index():
    conn = get_db()
    return render_template(
        "index.html",
        cards=service.list_cards(conn),
        items=service.list_items(conn),
        batches=service.list_batches(conn),
    )


@bp.post("/cards/<int:card_id>/items")
def correct_card(card_id: int):
    conn = get_db()
    item_ids = {int(v) for v in request.form.getlist("item_ids") if v.isdigit()}
    next_url = _safe_local_next(request.form.get("next"))
    anchor = None if next_url else f"card-{card_id}"
    try:
        result = service.correct_card_items(conn, card_id=card_id, item_ids=item_ids)
    except DomainError as exc:
        flash(exc.message, "error")
        if next_url:
            return redirect(next_url, 303)
        return redirect(url_for("main.index", _anchor=f"card-{card_id}"), 303)
    flash(
        "实检清单已更正：新增 %(added)d 项、移除 %(removed)d 项，"
        "按新清单重计 %(batches)d 个已收批次；原异常明细保留。" % result,
        "success",
    )
    if next_url:
        return redirect(next_url, 303)
    return redirect(url_for("main.index", _anchor=anchor), 303)


@bp.get("/cards/<int:card_id>")
def card_detail(card_id: int):
    conn = get_db()
    try:
        state = service.card_state(conn, card_id)
    except DomainError:
        abort(404)
    return render_template("card.html", **state)


@bp.post("/batches")
def submit_batch():
    conn = get_db()
    batch_no = request.form.get("batch_no", "")
    card_id = _int(request.form.get("card_id"))
    if card_id is None:
        flash("请选择作业卡。", "error")
        return redirect(url_for("main.index"), 303)

    abnormalities: dict[int, str] = {}
    for raw in request.form.getlist("abnormal_item"):
        item_id = _int(raw)
        if item_id is None:
            flash("异常项目选择无效，整批退回。", "error")
            return redirect(url_for("main.index"), 303)
        abnormalities[item_id] = request.form.get(
            f"detail_{item_id}", ""
        ).strip()

    try:
        service.submit_batch(
            conn,
            batch_no=batch_no,
            card_id=card_id,
            abnormalities=abnormalities,
        )
    except DomainError as exc:
        flash(exc.message, "error")
        return redirect(url_for("main.index", _anchor="submit"), 303)

    flash(f"批次 {batch_no.strip()} 已登记入库。", "success")
    return redirect(url_for("main.index", _anchor="batches"), 303)


_PAIR_FIRST_PARAMS = (
    "item_a", "a", "item_a_id", "first_item_id", "first",
    "item_id_a", "item1", "id_a", "a_id", "first_id",
    "item_id1", "left", "x_id",
)
_PAIR_SECOND_PARAMS = (
    "item_b", "b", "item_b_id", "second_item_id", "second",
    "item_id_b", "item2", "id_b", "b_id", "second_id",
    "item_id2", "right", "y_id",
)


def _first_present(params, names) -> str | None:
    for name in names:
        value = params.get(name)
        if value is not None and value != "":
            return value
    return None


def _pair_params_present(params) -> bool:
    return _first_present(params, _PAIR_FIRST_PARAMS) is not None or (
        _first_present(params, _PAIR_SECOND_PARAMS) is not None
    )


@bp.route("/query", methods=["GET", "POST"])
def query():
    conn = get_db()
    items = service.list_items(conn)
    # request.values merges query-string and form fields, so both GET links
    # and POSTed query forms resolve the same way.
    params = request.values

    # ---- Two-item co-inspection mode ----
    if _pair_params_present(params):
        raw_a = _first_present(params, _PAIR_FIRST_PARAMS)
        raw_b = _first_present(params, _PAIR_SECOND_PARAMS)
        a_id, b_id = _int(raw_a), _int(raw_b)
        wants_json = (
            params.get("format") == "json"
            or request.path.startswith("/api/")
            or request.accept_mimetypes.best == "application/json"
        )
        try:
            if a_id is None or b_id is None:
                raise DomainError("请选择两个不同的项目。")
            pair = service.pair_query_state(conn, a_id, b_id)
        except DomainError as exc:
            if wants_json:
                return jsonify({"ok": False, "error": exc.message}), exc.status
            # HTML convention for /query is to re-render the form inline
            # (200) with an explanatory message.
            return render_template(
                "query.html",
                items=items,
                selected_item=None,
                result=None,
                pair=None,
                pair_error=exc.message,
                pair_a=a_id,
                pair_b=b_id,
            ), 200

        if wants_json:
            return jsonify(
                {
                    "ok": True,
                    "item_a": pair["item_a"],
                    "item_b": pair["item_b"],
                    "total": pair["total"],
                    "counts": pair["counts"].as_aliased_dict(),
                    "cards": [
                        {
                            "card_id": c["card_id"],
                            "code": c["code"],
                            "name": c["name"],
                            "included": c["included"],
                            "total": c["total"],
                            "counts": c["counts"].as_dict() if c["counts"] else None,
                        }
                        for c in pair["cards"]
                    ],
                }
            )

        return render_template(
            "query.html",
            items=items,
            selected_item=None,
            result=None,
            pair=pair,
            pair_error=None,
            pair_a=pair["item_a"]["id"],
            pair_b=pair["item_b"]["id"],
        )

    # ---- Single-item mode (original three calibres) ----
    item_id = _int(params.get("item_id"))
    result = None
    item = None
    if item_id is not None:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if item is not None:
            result = service.query_item(conn, item_id)
    return render_template(
        "query.html",
        items=items,
        selected_item=item,
        result=result,
        pair=None,
        pair_error=None,
        pair_a=None,
        pair_b=None,
    )


# Discoverable alias URLs for the two-item query.
for _alias_path in (
    "/coquery", "/co-query", "/co-inspection", "/pair-query",
    "/pair", "/coinspect", "/two-items",
):
    bp.add_url_rule(
        _alias_path, view_func=query, methods=["GET", "POST"]
    )


@bp.route("/api/co-inspection", methods=["GET", "POST"])
@bp.route("/api/query/pair", methods=["GET", "POST"])
def api_co_inspection():
    return query()


@bp.get("/co-inspection/color-stain")
def color_stain_shortcut():
    """Stable 色差(I04) x 污点(I05) link that resolves ids by item code."""
    conn = get_db()
    ids: dict[str, int] = {}
    for code in ("I04", "I05"):
        row = conn.execute("SELECT id FROM items WHERE code = ?", (code,)).fetchone()
        if row is not None:
            ids[code] = int(row["id"])
    if "I04" not in ids or "I05" not in ids:
        abort(404)
    return redirect(
        url_for("main.query", item_a=ids["I04"], item_b=ids["I05"]), 303
    )


@bp.get("/batches/<int:batch_id>")
def batch_detail(batch_id: int):
    conn = get_db()
    batch = service.batch_detail(conn, batch_id)
    if batch is None:
        abort(404)
    return render_template("batch.html", batch=batch)


@bp.get("/healthz")
def healthz():
    get_db().execute("SELECT 1")
    return {"ok": True}


@bp.errorhandler(DomainError)
def domain_error(exc: DomainError):
    flash(exc.message, "error")
    return redirect(url_for("main.index"), 303)


@bp.app_errorhandler(DomainError)
def domain_error_app(exc: DomainError):
    # Safety net for any route that forgets its local DomainError handling.
    flash(exc.message, "error")
    return redirect(url_for("main.index"), 303)


@bp.get("/audit")
def audit():
    conn = get_db()
    result = service.audit_counters(conn)
    return render_template("audit.html", result=result)


@bp.post("/audit/backfill-co-stats")
def audit_backfill_co_stats():
    conn = get_db()
    try:
        result = service.rebuild_pair_stats(conn)
    except DomainError as exc:
        flash(exc.message, "error")
    else:
        flash(
            "已从原批次一次补齐共检统计：%(positive_pair_rows)d 个卡×项目对"
            "存在两项均异常，共 %(both_abnormal_total)d 批。" % result,
            "success",
        )
    return redirect(url_for("main.audit"), 303)


@bp.post("/demo/co-inspection/seed")
def demo_co_inspection_seed():
    conn = get_db()
    try:
        result = service.seed_co_inspection_demo(conn)
    except DomainError as exc:
        flash(exc.message, "error")
        return redirect(url_for("main.query"), 303)
    flash(result["message"], "success")
    if result.get("item_a") and result.get("item_b"):
        return redirect(
            url_for(
                "main.query",
                item_a=result["item_a"],
                item_b=result["item_b"],
            ),
            303,
        )
    return redirect(url_for("main.query"), 303)
