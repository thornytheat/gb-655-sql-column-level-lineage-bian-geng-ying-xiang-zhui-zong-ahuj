"""M1 入口：规格单首页、采信接口、隔离区导出、隔离项补正与重放。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app import errors, store, web
from app.modules.m1_intake import amend, service, sheet

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    run = store.latest_run()
    rows = (run or {}).get("diff") or []
    ledger = (run or {}).get("ledger") or {}
    quarantine = (run or {}).get("quarantine") or []
    blocked = sum(1 for row in quarantine if row["blocking"])
    return web.page_or_404(
        request,
        "index.html",
        "index",
        run,
        rows=rows,
        counts=ledger.get("counts") or {},
        quarantine=quarantine,
        blocked=blocked,
        warnings=len(quarantine) - blocked,
    )


@router.get("/intake", response_class=HTMLResponse)
def intake_page(request: Request, schema_id: str | None = None):
    """采信页：按隔离项逐条补正并重放，修订版只影响显式选用它之后的批次。"""
    snapshot = (store.load_snapshot(schema_id) if schema_id
                else service.latest_snapshot())
    latest_map = store.latest_revision_map()
    amendable = amend.amendable_items(snapshot) if snapshot else []
    target = schema_id or ((store.latest_run() or {}).get("new_schema_id"))
    revisions = amend.revision_list(snapshot["source_path"]) if snapshot else []
    return web.page_or_404(
        request,
        "intake.html",
        "intake",
        store.latest_run(),
        snapshot=snapshot,
        schema_id=target,
        amendable=amendable,
        revisions=revisions,
        current_revision=(latest_map.get(snapshot["source_path"]) if snapshot else None),
        amendments=(store.amendments_for_snapshot(schema_id) if schema_id else []),
        recent_amendments=store.list_amendments(8),
        revision_seq=int((snapshot or {}).get("revision_seq") or 0),
        latest_map=latest_map,
    )


@router.post("/api/schemas/import")
def import_schemas(payload: dict | None = None):
    body = payload or {}
    files = body.get("files") or []
    if not isinstance(files, list) or not files:
        raise errors.BadInput("需要给出 files 列表", detail={"field": "files"})
    if len(files) > 6:
        raise errors.BadInput("一次最多采信 6 份", detail={"count": len(files)})
    snapshots = [service.ingest(str(name)) for name in files]
    return {
        "imported": [service.snapshot_summary(item) for item in snapshots],
        "diagnostics": [row for item in snapshots for row in item["diagnostics"]],
    }


@router.get("/api/schemas/quarantine")
def quarantine(schema_id: str | None = None):
    rows = service.quarantine_of(schema_id)
    blocked = sum(1 for row in rows if row["blocking"])
    return {"schema_id": schema_id or (store.latest_run() or {}).get("new_schema_id"),
            "total": len(rows), "blocked": blocked, "warnings": len(rows) - blocked,
            "rows": rows}


@router.get("/api/schemas/quarantine.csv")
def quarantine_csv(schema_id: str | None = None):
    rows = service.quarantine_of(schema_id)
    body = sheet.diagnostic_csv(rows)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="lineageguard-quarantine.csv"'},
    )


def _items_from_body(body: dict) -> list[dict]:
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise errors.BadInput("需要给出 items 列表", detail={"field": "items"})
    if not all(isinstance(item, dict) for item in items):
        raise errors.BadInput("items 里只能放对象", detail={"field": "items"})
    return items


@router.get("/api/schemas/amendable")
def amendable(schema_id: str | None = None):
    snapshot = store.load_snapshot(schema_id) if schema_id else service.latest_snapshot()
    if snapshot is None:
        raise errors.NotFound("没有这个采信快照", detail={"schema_id": schema_id})
    items = amend.amendable_items(snapshot)
    return {
        "schema_id": snapshot["schema_id"],
        "source_path": snapshot.get("source_path"),
        "revision_seq": int(snapshot.get("revision_seq") or 0),
        "root_schema_id": snapshot.get("root_schema_id") or snapshot["schema_id"],
        "total": len(items),
        "items": items,
    }


@router.post("/api/schemas/amend")
def amend_schema(payload: dict | None = None):
    body = payload or {}
    schema_id = str(body.get("schema_id") or "").strip()
    if not schema_id:
        raise errors.BadInput("需要给出 schema_id", detail={"field": "schema_id"})
    record = amend.apply(
        schema_id,
        _items_from_body(body),
        expected_revision_seq=body.get("expected_revision_seq"),
        expected_schema_id=(str(body["expected_schema_id"])
                            if body.get("expected_schema_id") else None),
    )
    snapshot = store.load_snapshot(record["result_schema_id"]) \
        if record.get("result_schema_id") else None
    return {
        "amendment_id": record["amendment_id"],
        "status": record["status"],
        "reused": bool(record.get("reused")),
        "base_schema_id": record["base_schema_id"],
        "base_revision_seq": record["base_revision_seq"],
        "revision_seq": record.get("revision_seq"),
        "result_schema_id": record.get("result_schema_id"),
        "summary": record.get("summary") or {},
        "items": [
            {
                "item_id": item["item_id"],
                "status": item["status"],
                "reason": item.get("reason"),
                "reason_label": item.get("reason_label"),
                "detail": item.get("detail"),
                "original": item.get("original"),
                "input": item.get("input"),
                "result": item.get("result"),
            }
            for item in record.get("items") or []
        ],
        "stats": (snapshot or {}).get("stats") or {},
        "revisions": amend.revision_list(record["source_path"]),
    }


@router.get("/api/schemas/amendments")
def amendments(schema_id: str | None = None):
    rows = (store.amendments_for_snapshot(schema_id) if schema_id
            else store.list_amendments())
    return {
        "total": len(rows),
        "amendments": [
            {
                "amendment_id": item["amendment_id"],
                "base_schema_id": item["base_schema_id"],
                "base_revision_seq": item["base_revision_seq"],
                "revision_seq": item.get("revision_seq"),
                "status": item["status"],
                "result_schema_id": item.get("result_schema_id"),
                "created_at": item["created_at"],
                "updated_at": item["updated_at"],
                "error": item.get("error"),
                "summary": item.get("summary") or {},
                "items": item.get("items") or [],
            }
            for item in rows
        ],
    }


@router.post("/api/schemas/amendments/{amendment_id}/resume")
def resume_amendment(amendment_id: str):
    record = amend.resume(amendment_id)
    return {
        "amendment_id": record["amendment_id"],
        "status": record["status"],
        "reused": bool(record.get("reused")),
        "revision_seq": record.get("revision_seq"),
        "result_schema_id": record.get("result_schema_id"),
        "summary": record.get("summary") or {},
        "items": record.get("items") or [],
    }


@router.get("/api/schemas/revisions")
def revisions(schema_id: str | None = None):
    snapshot = store.load_snapshot(schema_id) if schema_id else None
    if snapshot is None:
        snapshot = service.latest_snapshot()
    if snapshot is None:
        raise errors.NotFound("还没有采信结果", detail={"schema_id": schema_id})
    rows = amend.revision_list(snapshot["source_path"])
    current = store.latest_revision(snapshot["source_path"])
    return {
        "source_path": snapshot["source_path"],
        "current_schema_id": (current or {}).get("schema_id"),
        "revisions": rows,
    }
