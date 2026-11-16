"""隔离项补正与重放：在采信层把被隔离的列补回来，生成同一文件的新一版采信结果。

三条硬规矩：
- 已采信列是既成事实，补正不能覆盖；冲突的补正原样退回隔离区。
- 修订只影响显式选用它之后的批次；历史批次保存着自己的快照，不跟着变。
- 补正单落库，重试按 amendment_key 命中同一张单，不重复采信。
"""

from __future__ import annotations

import hashlib
import json

from app import config, errors, reasons, store
from app.modules.m1_intake import ddl, service, sheet

AMENDABLE_REASONS = {"LINE_MALFORMED", "DUPLICATE_COLUMN", "FIELD_ID_DUP"}
CARRY_REASONS = {"BOM_STRIPPED", "ENCODING_FALLBACK", "EMPTY_INPUT", "TABLE_UNPARSED"}
COLUMN_WARNING_REASONS = {"FIELD_ID_DERIVED", "REQUIRED_TOKEN_UNKNOWN", "UNKNOWN_TYPE"}
TERMINAL_STATES = {"amended", "rejected", "skipped"}


class RevisionStale(errors.FieldmapError):
    status = 409
    code = "REVISION_STALE"
    label = "补正基线已过期"


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _normalize_item(raw: dict) -> dict:
    return {
        "item_id": str(raw.get("item_id") or "").strip(),
        "action": str(raw.get("action") or "amend").strip().lower(),
        "table": str(raw.get("table") or "").strip(),
        "column": str(raw.get("column") or raw.get("name") or "").strip(),
        "type": str(raw.get("type") or raw.get("type_raw") or "").strip(),
        "required": raw.get("required"),
        "field_id": raw.get("field_id"),
    }


def canonical_items(items: list[dict]) -> list[dict]:
    normalized = [_normalize_item(item) for item in items]
    seen: set[str] = set()
    for item in normalized:
        if not item["item_id"]:
            raise errors.BadInput("补正项缺少 item_id", detail={"field": "items.item_id"})
        if item["item_id"] in seen:
            raise errors.BadInput("同一隔离项重复出现",
                                  detail={"item_id": item["item_id"]})
        seen.add(item["item_id"])
        if item["action"] not in ("amend", "keep"):
            raise errors.BadInput("补正动作只接受 amend 或 keep",
                                  detail={"item_id": item["item_id"],
                                          "action": item["action"]})
    return sorted(normalized, key=lambda item: item["item_id"])


def amendment_key(base_schema_id: str, items: list[dict]) -> str:
    return _hash({"base": base_schema_id, "items": canonical_items(items)})


def _base_or_404(schema_id: str) -> dict:
    snapshot = store.load_snapshot(schema_id)
    if snapshot is None:
        raise errors.NotFound("没有这个采信快照", detail={"schema_id": schema_id})
    return snapshot


def _quarantine_of(snapshot: dict) -> list[dict]:
    return list(snapshot.get("diagnostics") or [])


def _suggestion(row: dict) -> dict:
    """从隔离行里尽量还原出可编辑的列定义，供页面预填。"""
    table = row.get("table") or ""
    name = row.get("column") or ""
    type_raw = ""
    required = None
    field_id = None
    raw = row.get("raw") or ""
    if raw and not raw.startswith("auto:"):
        line, comment = ddl._split_code_and_comment(raw)
        fid_match = ddl.FID_RE.search(comment) or ddl.FID_RE.search(line)
        field_id = fid_match.group("fid") if fid_match else None
        body = line.strip()
        while body.endswith(","):
            body = body[:-1].rstrip()
        match = ddl.COLUMN_RE.match(body)
        if match:
            name = name or ddl.clean_name(match.group("name"))
            type_raw = " ".join(match.group("type").split())
            required = ddl.required_from_rest(match.group("rest") or "")
    return {
        "table": table,
        "column": name,
        "type": type_raw,
        "required": "" if required is None else ("Y" if required else "N"),
        "field_id": field_id or "",
    }


def amendable_items(snapshot: dict) -> list[dict]:
    """可补正的隔离项：带原隔离原因与预填的修订内容。"""
    out: list[dict] = []
    for row in _quarantine_of(snapshot):
        if row.get("reason") not in AMENDABLE_REASONS:
            continue
        suggestion = _suggestion(row)
        out.append({
            "item_id": row.get("item_id"),
            "reason": row["reason"],
            "reason_label": reasons.label(row["reason"]),
            "line": row.get("line"),
            "table": row.get("table"),
            "column": row.get("column"),
            "detail": row.get("detail"),
            "raw": row.get("raw"),
            "blocking": bool(row.get("blocking")),
            "suggestion": suggestion,
        })
    return out


def _find_row(snapshot: dict, item_id: str) -> dict | None:
    for row in _quarantine_of(snapshot):
        if row.get("item_id") == item_id:
            return row
    return None


def _identity_text(raw_value) -> tuple[str | None, str | None]:
    """字段身份一律按字符串保形；浮点身份不接受。返回（身份, 拒绝原因）。"""
    if raw_value is None:
        return "", None
    if isinstance(raw_value, bool):
        return None, "字段身份不能是布尔值"
    if isinstance(raw_value, float):
        if not raw_value.is_integer():
            return None, "字段身份不能是浮点数"
        raw_value = int(raw_value)
    text = str(raw_value).strip()
    if text.lower() in ("none", "null", "nan", "inf", "-inf"):
        return None, "字段身份不是合法字符串"
    return text, None


def _validate_input(item: dict) -> tuple[dict | None, str | None, str | None]:
    """校验修订内容。返回（列定义, 拒绝原因码, 说明）。"""
    if item["action"] == "keep":
        return None, None, None
    if not item["table"] or not item["column"] or not item["type"]:
        return None, "AMENDMENT_INCOMPLETE", "缺少表名、列名或类型"

    field_id, id_error = _identity_text(item["field_id"])
    if id_error:
        return None, "AMENDMENT_INCOMPLETE", id_error
    if not sheet._is_known_type(item["type"]):
        return None, "AMENDMENT_REJECTED", f"类型 {item['type']} 不在类型格内"

    required = ddl.detect_required_token(item["required"])
    return {
        "table": item["table"],
        "name": item["column"],
        "type_raw": item["type"],
        "required": required,
        "field_id": field_id or None,
    }, None, None


def _fold_one(base_fields: list[dict], column: dict, version_label: str,
              source: str) -> tuple[dict | None, list[dict]]:
    folded = sheet.fold_corrections(base_fields, [column], version_label, source)
    fields = folded["fields"]
    return (fields[0] if fields else None), folded["diagnostics"]


def _existing_field(fields: list[dict], column: dict) -> dict | None:
    """续跑时按表名列名/身份找回已经被基线吸收的那一列。"""
    wanted_name = (column.get("table") or "").strip().lower()
    wanted_column = (column.get("name") or "").strip().lower()
    wanted_id = column.get("field_id")
    for field in fields:
        if wanted_id and str(field["field_id"]) == str(wanted_id):
            return field
        if (field["table"].lower() == wanted_name
                and field["name"].lower() == wanted_column):
            return field
    return None


def _result_diagnostics(base: dict, outcomes: list[dict], adopted_diags: list[dict],
                        new_schema_id: str) -> list[dict]:
    """修订版的诊断：原有说明行照带，列级提示照带，补正结果逐项落位。"""
    amended_ids = {outcome["item_id"] for outcome in outcomes
                   if outcome["status"] == "amended"}
    handled_ids = {outcome["item_id"] for outcome in outcomes}
    rows: list[dict] = []
    for row in _quarantine_of(base):
        item_id = row.get("item_id")
        if item_id and item_id in amended_ids:
            continue
        if item_id and item_id in handled_ids:
            # 被拒绝 / 保留的项按补正结果重新落一条，见下面
            continue
        if row.get("reason") in CARRY_REASONS:
            rows.append(dict(row))
        elif row.get("blocking"):
            # 没被点名的隔离项原样留在隔离区
            rows.append(dict(row))
        elif row.get("reason") in COLUMN_WARNING_REASONS:
            rows.append(dict(row))
    rows.extend(dict(row) for row in adopted_diags if not row.get("blocking"))

    for outcome in outcomes:
        if outcome["status"] == "amended":
            continue
        rows.append({
            "line": outcome["original"].get("line"),
            "table": outcome["original"].get("table") or "",
            "column": outcome["original"].get("column") or "",
            "reason": outcome["original"].get("reason"),
            "blocking": True,
            "detail": outcome["detail"],
            "raw": outcome["original"].get("raw") or "",
            "amendment": {
                "amendment_id": outcome.get("amendment_id"),
                "item_id": outcome["item_id"],
                "status": outcome["status"],
                "reason": outcome.get("reason"),
            },
        })
    rows.append({
        "line": 0,
        "table": "",
        "column": "",
        "reason": "AMENDMENT_APPLIED",
        "blocking": False,
        "detail": outcomes[0].get("amendment_id") if outcomes else "",
        "raw": "",
    })
    rows = sheet.attach_item_ids(new_schema_id, rows)
    for row in rows:
        row["schema_id"] = new_schema_id
    return rows


def _build_snapshot(base: dict, adoptions: list[dict], outcomes: list[dict],
                   amendment_id: str) -> dict | None:
    if not adoptions:
        return None
    base_fields = list(base.get("fields") or [])
    adopted_fields = [item["field"] for item in adoptions]
    fields = sorted(
        base_fields + adopted_fields,
        key=lambda field: (int(field.get("line") or 0), field.get("table") or "",
                           field.get("field_id") or ""),
    )
    version_label = base.get("version_label") or ""
    source = base.get("source_path") or ""
    new_schema_id = service._schema_id(version_label, fields)
    if new_schema_id == (base.get("schema_id") or ""):
        return None

    adopted_diags = [diag for item in adoptions for diag in item["diagnostics"]]
    diagnostics = _result_diagnostics(base, outcomes, adopted_diags, new_schema_id)
    blocked = sum(1 for row in diagnostics if row.get("blocking"))
    accepted = len(fields)
    parsed = accepted + blocked - sum(
        1 for row in diagnostics
        if row.get("blocking") and row.get("reason") == "LINE_MALFORMED"
    )
    stats = service._stats(diagnostics, max(parsed, 0), accepted,
                           len({field["table"] for field in fields}))
    stats["adopted"] = len(adopted_fields)

    base_revision = int(base.get("revision_seq") or 0)
    root_schema_id = base.get("root_schema_id") or base["schema_id"]
    return {
        "schema_id": new_schema_id,
        "version_label": version_label,
        "source_path": source,
        "source_sha256": base.get("source_sha256"),
        "encoding": base.get("encoding"),
        "bom": bool(base.get("bom")),
        "fallback": bool(base.get("fallback")),
        "bytes": base.get("bytes"),
        "line_count": base.get("line_count"),
        "created_at": config.now_utc().isoformat(),
        "fields": fields,
        "diagnostics": diagnostics,
        "tables": base.get("tables") or [],
        "stats": stats,
        "field_count": len(fields),
        "blocked_count": blocked,
        "root_schema_id": root_schema_id,
        "base_schema_id": base["schema_id"],
        "base_revision_seq": base_revision,
        "revision_seq": base_revision + 1,
        "amendment_id": amendment_id,
        "amended_items": sorted(item["item_id"] for item in adoptions),
    }


def _requested_items(schema_id: str, raw_items: list[dict]) -> list[dict]:
    items = canonical_items(raw_items)
    if not items:
        raise errors.BadInput("补正单至少要有一个隔离项", detail={"field": "items"})
    if len(items) > config.MAX_AMENDMENT_ITEMS:
        raise errors.BadInput("一次补正项过多",
                              detail={"count": len(items),
                                      "max": config.MAX_AMENDMENT_ITEMS})
    return items


def _record(base: dict, items: list[dict], expected) -> dict:
    key = amendment_key(base["schema_id"], items)
    now = config.now_utc().isoformat()
    return {
        "amendment_id": "amd-" + key[:12],
        "amendment_key": key,
        "root_schema_id": base.get("root_schema_id") or base["schema_id"],
        "base_schema_id": base["schema_id"],
        "base_revision_seq": int(base.get("revision_seq") or 0),
        "revision_seq": None,
        "source_path": base.get("source_path"),
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "applied_at": None,
        "result_schema_id": None,
        "error": None,
        "expected_base_schema_id": expected,
        "items": [],
        "summary": {},
        "reused": False,
    }


def _item_record(item: dict, original: dict | None) -> dict:
    return {
        "item_id": item["item_id"],
        "action": item["action"],
        "original": {
            "line": (original or {}).get("line"),
            "table": (original or {}).get("table"),
            "column": (original or {}).get("column"),
            "reason": (original or {}).get("reason"),
            "reason_label": reasons.label((original or {}).get("reason"))
            if original else None,
            "detail": (original or {}).get("detail"),
            "raw": (original or {}).get("raw"),
        },
        "input": {key: item[key] for key in
                  ("table", "column", "type", "required", "field_id", "action")},
        "status": "pending",
        "reason": None,
        "reason_label": None,
        "detail": "",
        "field_id": None,
    }


def _terminal_items(record: dict) -> dict[str, dict]:
    return {item["item_id"]: item for item in record.get("items") or []
            if item.get("status") in TERMINAL_STATES}


def apply(schema_id: str, raw_items: list[dict], expected_revision_seq=None,
          expected_schema_id: str | None = None) -> dict:
    """落一张补正单并重放；同一份内容重试命中同一张单，不重复采信。"""
    base = _base_or_404(schema_id)
    items = _requested_items(schema_id, raw_items)
    key = amendment_key(schema_id, items)
    latest = store.latest_revision(base.get("source_path") or "")

    # 重试先看命中：同一张单重放原样返回，不再采信第二次
    existing = store.find_amendment_by_key(key)
    if existing is not None and existing.get("status") in ("applied", "partial") \
            and existing.get("result_schema_id"):
        reused = dict(existing)
        reused["reused"] = True
        return reused

    if expected_schema_id and latest and latest["schema_id"] != expected_schema_id:
        raise RevisionStale(
            "补正提交基于已过期的采信版本",
            detail={"expected_schema_id": expected_schema_id,
                    "current_schema_id": latest["schema_id"],
                    "reason": "REVISION_STALE"},
        )
    if expected_revision_seq is not None:
        try:
            wanted = int(expected_revision_seq)
        except (TypeError, ValueError):
            raise errors.BadInput("expected_revision_seq 必须是整数",
                                  detail={"value": repr(expected_revision_seq)})
        current = int(base.get("revision_seq") or 0)
        if wanted != current:
            raise RevisionStale(
                "补正提交基于已过期的修订序号",
                detail={"expected_revision_seq": wanted,
                        "current_revision_seq": current,
                        "reason": "REVISION_STALE"},
            )
    if latest and latest["schema_id"] != base["schema_id"]:
        raise RevisionStale(
            "同一份文件已有更新的采信版本",
            detail={"base_schema_id": base["schema_id"],
                    "current_schema_id": latest["schema_id"],
                    "current_revision_seq": int(latest.get("revision_seq") or 0),
                    "reason": "REVISION_STALE"},
        )

    record = existing or _record(base, items, expected_schema_id)
    record["items"] = [_item_record(item, None) for item in items]
    try:
        return _replay(base, record, items)
    except store.RevisionConflict as exc:
        raise RevisionStale(
            "补正提交时基线已被别的修订落下",
            detail={"base_schema_id": base["schema_id"],
                    "current_schema_id": exc.current_schema_id,
                    "reason": "REVISION_STALE"},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 失败单要能续跑
        failed = dict(record)
        failed["status"] = "failed"
        failed["error"] = f"{type(exc).__name__}: {exc}"
        failed["updated_at"] = config.now_utc().isoformat()
        try:
            store.save_amendment(failed)
        except Exception:  # noqa: BLE001
            pass
        raise


def _replay(base: dict, record: dict, items: list[dict]) -> dict:
    """把补正项逐条重放到基线上，并提交修订版。"""
    done = _terminal_items(record)
    by_id = {row.get("item_id"): row for row in _quarantine_of(base)}

    adoptions: list[dict] = []
    outcomes: list[dict] = []
    accepted = [dict(field) for field in base.get("fields") or []]
    version_label = base.get("version_label") or ""
    source = base.get("source_path") or ""

    for item in items:
        outcome = done.get(item["item_id"])
        if outcome is not None:
            outcomes.append(dict(outcome))
            if outcome["status"] == "amended":
                # 续跑时把已采纳的列接回，保证整版结果完整；基线里已经有的不重复加
                column, _, _ = _validate_input(item)
                if column is not None:
                    field, found = _fold_one(accepted, column, version_label, source)
                    if field is not None:
                        accepted.append(field)
                        adoptions.append({"item_id": item["item_id"], "field": field,
                                          "diagnostics": found})
                    elif found and found[0].get("reason") in ("DUPLICATE_COLUMN",
                                                              "FIELD_ID_DUP"):
                        existing_field = _existing_field(accepted, column)
                        if existing_field is not None:
                            adoptions.append({"item_id": item["item_id"],
                                              "field": existing_field,
                                              "diagnostics": []})
            continue

        original = by_id.get(item["item_id"])
        entry = _item_record(item, original)
        if original is None:
            entry["status"] = "rejected"
            entry["reason"] = "AMENDMENT_TARGET_MISSING"
            entry["reason_label"] = reasons.label("AMENDMENT_TARGET_MISSING")
            entry["detail"] = "补正目标不在该快照的隔离区"
            outcomes.append(entry)
            continue
        if original.get("reason") not in AMENDABLE_REASONS:
            entry["status"] = "rejected"
            entry["reason"] = "AMENDMENT_TARGET_MISSING"
            entry["reason_label"] = reasons.label("AMENDMENT_TARGET_MISSING")
            entry["detail"] = "该隔离项不是列定义问题，不能补正"
            outcomes.append(entry)
            continue
        if item["action"] == "keep":
            entry["status"] = "skipped"
            entry["detail"] = "保留原隔离，不重放"
            outcomes.append(entry)
            continue

        column, reason_code, detail = _validate_input(item)
        if column is None:
            entry["status"] = "rejected"
            entry["reason"] = reason_code
            entry["reason_label"] = reasons.label(reason_code)
            entry["detail"] = detail or "补正未采纳"
            outcomes.append(entry)
            continue

        column["line"] = original.get("line") or 0
        column["version"] = version_label
        column["source"] = source
        field, found = _fold_one(accepted, column, version_label, source)
        if field is None:
            conflict = found[0] if found else {}
            code = conflict.get("reason") or "AMENDMENT_REJECTED"
            entry["status"] = "rejected"
            entry["reason"] = code
            entry["reason_label"] = reasons.label(code)
            entry["detail"] = "补正与已采信列冲突：" + (conflict.get("detail") or "")
            entry["conflict"] = conflict
            outcomes.append(entry)
            continue

        entry["status"] = "amended"
        entry["field_id"] = field["field_id"]
        entry["detail"] = f"已采纳为 {field['table']}.{field['name']} {field['type_raw']}"
        entry["result"] = {
            "field_id": field["field_id"],
            "table": field["table"],
            "column": field["name"],
            "type": field["type_raw"],
            "required": bool(field["required"]),
            "line": field["line"],
        }
        outcomes.append(entry)
        accepted.append(field)
        adoptions.append({"item_id": item["item_id"], "field": field,
                          "diagnostics": found})

    amendment_id = record["amendment_id"]
    for outcome in outcomes:
        outcome["amendment_id"] = amendment_id
    outcomes.sort(key=lambda entry: entry["item_id"])

    snapshot = _build_snapshot(base, adoptions, outcomes, amendment_id)
    amended = sum(1 for entry in outcomes if entry["status"] == "amended")
    rejected = sum(1 for entry in outcomes if entry["status"] == "rejected")
    skipped = sum(1 for entry in outcomes if entry["status"] == "skipped")
    pending = len(items) - amended - rejected - skipped

    record["items"] = outcomes
    record["updated_at"] = config.now_utc().isoformat()
    if pending:
        record["status"] = "pending"
    elif amended == 0:
        record["status"] = "skipped" if skipped and not rejected else "rejected"
    elif rejected:
        record["status"] = "partial"
    else:
        record["status"] = "applied"
    record["summary"] = {
        "requested": len(items),
        "amended": amended,
        "rejected": rejected,
        "skipped": skipped,
        "pending": pending,
        "base_schema_id": base["schema_id"],
        "base_revision_seq": int(base.get("revision_seq") or 0),
    }

    committed = store.commit_amendment(record, snapshot, base["schema_id"])
    committed["reused"] = False
    return committed


def _revision_base(record: dict, available: dict[str, dict],
                   latest: dict | None) -> dict | None:
    """给续跑挑一个可用的基线。

    原来那张基线可能已经被后续修订落下；这种情况下退回同一条修订链上
    仍可用的最近一版（优先当前最新版），避免整张单卡死。
    """
    original = available.get(record["base_schema_id"])
    if original is not None:
        return original
    if latest is None:
        return None
    current = latest["schema_id"]
    chain = [current]
    seen = {current}
    while chain[-1] in available:
        node = available[chain[-1]]
        parent = node.get("base_schema_id")
        if not parent or parent in seen:
            break
        chain.append(parent)
        seen.add(parent)
    for schema_id in chain:
        if schema_id in available:
            return available[schema_id]
    return None


def resume(amendment_id: str) -> dict:
    """续跑一张没做完的补正单；已终结的项不重复处理。"""
    record = store.load_amendment(amendment_id)
    if record is None:
        raise errors.NotFound("没有这张补正单", detail={"amendment_id": amendment_id})
    if record.get("status") in ("applied", "partial") and record.get("result_schema_id"):
        reused = dict(record)
        reused["reused"] = True
        return reused
    latest = store.latest_revision(record.get("source_path") or "")
    available: dict[str, dict] = {}
    for snapshot in store.list_snapshots_for_source(record.get("source_path") or ""):
        available[snapshot["schema_id"]] = snapshot
    base = _revision_base(record, available, latest)
    if base is None:
        raise errors.NotFound("补正单的基线快照已不存在",
                              detail={"amendment_id": amendment_id,
                                      "base_schema_id": record.get("base_schema_id")})
    items = [
        {
            "item_id": item["item_id"],
            "action": item["action"],
            "table": item["input"].get("table"),
            "column": item["input"].get("column"),
            "type": item["input"].get("type"),
            "required": item["input"].get("required"),
            "field_id": item["input"].get("field_id"),
        }
        for item in record.get("items") or []
    ]
    return apply(base["schema_id"], items)


def pending_amendments() -> list[dict]:
    return [item for item in store.list_amendments()
            if item.get("status") in ("pending", "failed")]


def revision_list(source_path: str) -> list[dict]:
    snapshots = store.list_snapshots_for_source(source_path)
    current = store.latest_revision(source_path)
    out: list[dict] = []
    for snapshot in snapshots:
        out.append({
            "schema_id": snapshot["schema_id"],
            "revision_seq": int(snapshot.get("revision_seq") or 0),
            "base_schema_id": snapshot.get("base_schema_id"),
            "amendment_id": snapshot.get("amendment_id"),
            "field_count": snapshot.get("field_count"),
            "blocked_count": snapshot.get("blocked_count"),
            "created_at": snapshot.get("created_at"),
            "is_current": bool(current and current["schema_id"] == snapshot["schema_id"]),
        })
    return sorted(out, key=lambda item: (item["revision_seq"], item["created_at"] or ""))


