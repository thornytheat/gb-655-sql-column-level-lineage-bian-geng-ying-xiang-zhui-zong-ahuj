"""批次编排：一次判定 = 采信 → 类型格 → 兼容 → 映射 → 影响面 → 账目。

同一输入（文件摘要 + 生效区间 + 参数）只算一次：命中批次键就复用已有结果。
"""

from __future__ import annotations

from app import config, errors, manifest as manifest_mod, store
from app.modules.m1_intake import service as intake
from app.modules.m2_types import service as types
from app.modules.m3_compat import service as compat
from app.modules.m4_mapping import service as mapping
from app.modules.m5_impact import service as impact
from app.modules.m6_batches import guard, history, window as window_mod
from app.modules.m7_migration import service as migration


def _ref_counts(consumers: list[dict]) -> dict:
    counts: dict[tuple[str, str], int] = {}
    for consumer in consumers:
        for ref in consumer["refs"]:
            table, _, column = ref.rpartition(".")
            key = (table.lower(), column.lower())
            counts[key] = counts.get(key, 0) + 1
    return counts


def _json_safe(resolution: dict) -> dict:
    out = dict(resolution)
    out["by_field"] = {key: sorted(value)
                       for key, value in (resolution.get("by_field") or {}).items()}
    out["ref_counts"] = {f"{table}|{column}": count
                         for (table, column), count
                         in (resolution.get("ref_counts") or {}).items()}
    return out


def _schema_ref(snapshot: dict, rel_name: str | None) -> dict:
    return {
        "schema_id": snapshot["schema_id"],
        "source_path": snapshot.get("source_path"),
        "file": rel_name,
        "version_label": snapshot.get("version_label"),
        "revision_seq": int(snapshot.get("revision_seq") or 0),
        "root_schema_id": snapshot.get("root_schema_id") or snapshot["schema_id"],
        "base_schema_id": snapshot.get("base_schema_id"),
        "amendment_id": snapshot.get("amendment_id"),
    }


def _edges(old_snapshot: dict, new_snapshot: dict, diff: dict,
           base_snapshot: dict | None) -> dict:
    allowed = {row["field_id"] for row in diff["rows"] if row["bucket"] == "renamed"}
    graph = mapping.rename_edges(old_snapshot["fields"], new_snapshot["fields"], allowed)
    if base_snapshot is not None:
        base_diff = compat.diff_snapshots(base_snapshot, old_snapshot)
        allowed_base = {row["field_id"] for row in base_diff["rows"]
                        if row["bucket"] == "renamed"}
        graph = mapping.merge_edges(
            graph,
            mapping.rename_edges(base_snapshot["fields"], old_snapshot["fields"],
                                 allowed_base),
        )
    return graph


def _resolve_snapshot(rel_name: str, schema_id: str | None) -> dict:
    """明确选用某个采信版本时按 schema_id 取；否则按文件重新采信。

    历史迁移单引用的快照原样留在库里，批次只挑自己那一版，互不影响。
    """
    if not schema_id:
        return intake.ingest(rel_name)
    snapshot = store.load_snapshot(str(schema_id))
    if snapshot is None:
        raise errors.NotFound("没有这个采信版本", detail={"schema_id": schema_id})
    path = config.resolve_sample(rel_name) if rel_name else None
    if path is not None and snapshot.get("source_path") != path.name:
        raise errors.BadInput(
            "采信版本与文件名不符",
            detail={"schema_id": str(schema_id), "file": rel_name,
                    "source_path": snapshot.get("source_path")},
        )
    return snapshot


def execute(old_file: str = config.DEFAULT_OLD, new_file: str = config.DEFAULT_NEW,
            refs_file: str = config.DEFAULT_REFS, base_file: str | None = config.BASE_OLD,
            base_version_seq=None, effective_from: str | None = None,
            effective_to: str | None = None, params: dict | None = None,
            old_schema_id: str | None = None, new_schema_id: str | None = None,
            base_schema_id: str | None = None) -> dict:
    params = dict(params or {})
    old_path = config.resolve_sample(old_file)
    new_path = config.resolve_sample(new_file)
    refs_path = config.resolve_sample(refs_file)
    base_path = None
    if base_file:
        try:
            base_path = config.resolve_sample(base_file)
        except (errors.NotFound, errors.BadInput):
            base_path = None

    old_snapshot = types.prepare(_resolve_snapshot(old_file, old_schema_id))
    new_snapshot = types.prepare(_resolve_snapshot(new_file, new_schema_id))
    base_snapshot = (types.prepare(_resolve_snapshot(base_file, base_schema_id))
                     if base_path else None)
    refs = mapping.load_refs(refs_file)

    window = window_mod.normalize(effective_from, effective_to,
                                 new_snapshot["version_label"])

    paths = {"old": old_path, "new": new_path, "refs": refs_path}
    names = {"old": old_file, "new": new_file, "refs": refs_file}
    if base_path is not None:
        paths["base"] = base_path
        names["base"] = base_file
    digests = {role: manifest_mod.sha256_file(path) for role, path in sorted(paths.items())}
    window_key = f"{window['from']}..{window['to'] or ''}"
    # 选中的采信版本进批次键：修订版与原版是两次不同的判定
    schema_key = {role: value for role, value in
                  (("old", old_schema_id), ("new", new_schema_id),
                   ("base", base_schema_id)) if value}
    key = manifest_mod.batch_key(
        digests,
        {"window": window_key, "schemas": schema_key, **params},
        config.RULESET_VERSION,
    )

    # 陈旧提交先拒：基线落后时，连复用都不该给
    guard.check_stale(base_version_seq, store.current_version_seq())

    existing = store.find_run_by_key(key)
    if existing is not None:
        reused = dict(existing)
        reused["reused"] = True
        return reused

    ref_counts = _ref_counts(refs["consumers"])
    diff = compat.diff_snapshots(old_snapshot, new_snapshot, ref_counts)
    edges = _edges(old_snapshot, new_snapshot, diff, base_snapshot)
    resolution = mapping.resolve(refs["consumers"], old_snapshot["fields"],
                                 new_snapshot["fields"], edges)
    mapping_table = mapping.build_mapping(old_snapshot["fields"], new_snapshot["fields"], diff)
    impact_payload = impact.build(diff, refs["consumers"], resolution)
    ledger = migration.build_ledger(diff, refs["consumers"], resolution)

    warnings = guard.overlap_warnings(window, store.list_windows())
    created_at = config.now_utc().isoformat()
    input_sha = manifest_mod.sha256_bytes(
        "|".join(f"{role}:{digest}" for role, digest in sorted(digests.items())).encode()
    )

    run = {
        "run_id": manifest_mod.run_id_for(key),
        "batch_key": key,
        "old_schema_id": old_snapshot["schema_id"],
        "new_schema_id": new_snapshot["schema_id"],
        "version_label_old": old_snapshot["version_label"],
        "version_label_new": new_snapshot["version_label"],
        "version_seq": store.next_version_seq(),
        "input_sha256": input_sha,
        "digests": digests,
        "input_names": names,
        "params": params,
        "ruleset": config.RULESET_VERSION,
        "created_at": created_at,
        "effective_from": window["from"],
        "effective_to": window["to"],
        "window": {k: v for k, v in window.items() if not k.endswith("_dt")},
        "fields_old": old_snapshot["fields"],
        "fields_new": new_snapshot["fields"],
        "type_stats_old": old_snapshot["type_stats"],
        "type_stats_new": new_snapshot["type_stats"],
        "schema_refs": {
            "old": _schema_ref(old_snapshot, old_file),
            "new": _schema_ref(new_snapshot, new_file),
            **({"base": _schema_ref(base_snapshot, base_file)}
               if base_snapshot is not None else {}),
        },
        "diff": diff["rows"],
        "buckets": diff["buckets"],
        "changes": diff["changes"],
        "verdicts": diff["verdicts"],
        "counts": diff["counts"],
        "name_clashes": diff["name_clashes"],
        "mapping": mapping_table,
        "refs": {"source": refs["source"], "sha256": refs["sha256"],
                 "registered_at": refs["registered_at"], "ref_total": refs["ref_total"],
                 "consumers": refs["consumers"]},
        "resolution": _json_safe(resolution),
        "impact": impact_payload,
        "ledger": ledger,
        "quarantine": ([dict(row, side="old") for row in old_snapshot["diagnostics"]]
                       + [dict(row, side="new") for row in new_snapshot["diagnostics"]]),
        "quarantine_old": old_snapshot["diagnostics"],
        "quarantine_new": new_snapshot["diagnostics"],
        "warnings": warnings,
        "reused": False,
    }
    run["summary"] = history.summary_of(run)
    store.save_run(run)
    return run


def ensure_seeded() -> dict | None:
    """启动时把样例跑一遍，页面打开就有数据；已有批次则不重复。"""
    if store.count_runs() > 0:
        return store.latest_run()
    run = execute()
    store.set_kv("seeded", {"run_id": run["run_id"], "version_seq": run["version_seq"]})
    return run


def current_run() -> dict | None:
    return store.latest_run()


REVISION_ROLES = (("old", config.DEFAULT_OLD), ("new", config.DEFAULT_NEW),
                  ("base", config.BASE_OLD))


def revision_options() -> dict[str, list[dict]]:
    """给批次页列出每个角色可选用的采信版本，标出最新修订与当前批次所用版本。"""
    run = store.latest_run() or {}
    refs = run.get("schema_refs") or {}
    out: dict[str, list[dict]] = {}
    for role, default_file in REVISION_ROLES:
        rows: list[dict] = []
        default_path = None
        try:
            default_path = config.resolve_sample(default_file).name
        except (errors.NotFound, errors.BadInput):
            default_path = None
        if default_path:
            stored = store.list_snapshots_for_source(default_path)
            current = store.latest_revision(default_path)
            used = (refs.get(role) or {}).get("schema_id")
            for snapshot in stored:
                rows.append({
                    "schema_id": snapshot["schema_id"],
                    "revision_seq": int(snapshot.get("revision_seq") or 0),
                    "file": default_file,
                    "source_path": snapshot.get("source_path"),
                    "version_label": snapshot.get("version_label"),
                    "field_count": snapshot.get("field_count"),
                    "blocked_count": snapshot.get("blocked_count"),
                    "amendment_id": snapshot.get("amendment_id"),
                    "is_current": bool(current
                                       and current["schema_id"] == snapshot["schema_id"]),
                    "is_selected": used == snapshot["schema_id"],
                })
        out[role] = sorted(rows, key=lambda item: (item["revision_seq"],
                                                   item["schema_id"]))
    return out


def reconcile(run: dict) -> dict:
    return history.reconcile(run)
