"""M1 补正重放：隔离项补正、已采信列不可覆盖、修订版本与续跑。"""

from __future__ import annotations

import csv
import io

import pytest

from app import store
from app.modules.m1_intake import amend, service as intake
from app.modules.m6_batches import service as batch_service
from tests.fixtures import expected


def _amendable(schema_id: str) -> dict[str, dict]:
    snapshot = store.load_snapshot(schema_id)
    return {item["item_id"]: item for item in amend.amendable_items(snapshot)}


def _item(target: dict, **overrides) -> dict:
    item = {**target["suggestion"], "item_id": target["item_id"], "action": "amend"}
    item.update(overrides)
    return item


def test_amendable_lists_original_reason_and_suggestion(run):
    """补正页能看到原隔离原因与预填的修订内容。"""
    by_id = _amendable(run["old_schema_id"])
    reasons = {item["reason"] for item in by_id.values()}
    assert reasons == {"LINE_MALFORMED", "DUPLICATE_COLUMN"}
    malformed = next(item for item in by_id.values() if item["reason"] == "LINE_MALFORMED")
    assert malformed["line"] == 7
    assert malformed["suggestion"]["field_id"] == "1005"
    assert malformed["suggestion"]["type"] == "DECIMAL(18,2)"


def test_amend_recovers_quarantined_column(run):
    """补正把被隔离的列救回来，生成同一文件的修订版，原快照不动。"""
    base = store.load_snapshot(run["old_schema_id"])
    target = next(item for item in amend.amendable_items(base)
                  if item["reason"] == "LINE_MALFORMED")
    record = amend.apply(base["schema_id"], [
        _item(target, column="order_amt_legacy", type="decimal(18,2)", required="N"),
    ])
    assert record["status"] == "applied"
    assert record["revision_seq"] == 1
    assert record["summary"]["amended"] == 1

    revised = store.load_snapshot(record["result_schema_id"])
    assert revised["revision_seq"] == 1
    assert revised["base_schema_id"] == base["schema_id"]
    assert revised["root_schema_id"] == base["schema_id"]
    assert revised["schema_id"] != base["schema_id"]
    names = {field["name"] for field in revised["fields"]}
    assert "order_amt_legacy" in names
    assert revised["field_count"] == base["field_count"] + 1
    assert revised["blocked_count"] == base["blocked_count"] - 1

    # 原快照仍在库里，字段与阻断数不变
    untouched = store.load_snapshot(base["schema_id"])
    assert untouched["field_count"] == base["field_count"]
    assert untouched["blocked_count"] == base["blocked_count"]
    assert "order_amt_legacy" not in {field["name"] for field in untouched["fields"]}


def test_conflict_does_not_overwrite_adopted_column(run):
    """补正与已采信列撞名/撞身份时整项退回隔离区，绝不覆盖。"""
    base = store.load_snapshot(run["old_schema_id"])
    target = next(item for item in amend.amendable_items(base)
                  if item["reason"] == "DUPLICATE_COLUMN")
    # 补一个合法类型后提交，仍与第 4 行已采信的 order_no 撞名
    record = amend.apply(base["schema_id"], [
        _item(target, type="varchar(32)", required="N"),
    ])
    assert record["status"] == "rejected"
    assert record["summary"]["amended"] == 0
    assert record["summary"]["rejected"] == 1
    item = record["items"][0]
    assert item["reason"] == "DUPLICATE_COLUMN"
    assert item["status"] == "rejected"
    assert record["result_schema_id"] is None

    fields = store.load_snapshot(base["schema_id"])["fields"]
    order_no = [field for field in fields if field["name"].lower() == "order_no"]
    assert len(order_no) == 1
    assert order_no[0]["field_id"] == "1002"


def test_conflict_with_explicit_field_id_is_rejected(run):
    """身份撞车同样算冲突：已采信的 1002 不能被补正挪用。"""
    base = store.load_snapshot(run["old_schema_id"])
    target = next(item for item in amend.amendable_items(base)
                  if item["reason"] == "DUPLICATE_COLUMN")
    record = amend.apply(base["schema_id"], [
        _item(target, column="order_no_copy", type="varchar(32)", field_id="1002"),
    ])
    assert record["status"] == "rejected"
    assert record["items"][0]["reason"] == "FIELD_ID_DUP"


def test_amendment_retry_does_not_reingest(run):
    """同一份补正重试命中同一张单，快照不增加。"""
    base = store.load_snapshot(run["old_schema_id"])
    target = next(item for item in amend.amendable_items(base)
                  if item["reason"] == "LINE_MALFORMED")
    items = [_item(target, column="order_amt_legacy", type="decimal(18,2)")]
    first = amend.apply(base["schema_id"], items)
    before = len(store.list_snapshots_for_source(base["source_path"]))
    again = amend.apply(base["schema_id"], items)
    after = len(store.list_snapshots_for_source(base["source_path"]))
    assert again["amendment_id"] == first["amendment_id"]
    assert again["reused"] is True
    assert again["result_schema_id"] == first["result_schema_id"]
    assert before == after


def test_revision_is_revalidated_and_still_invalid_stays_quarantined(run):
    """补正后重新校验身份与类型：仍不合法的项继续隔离。"""
    base = store.load_snapshot(run["old_schema_id"])
    malformed = next(item for item in amend.amendable_items(base)
                     if item["reason"] == "LINE_MALFORMED")
    duplicate = next(item for item in amend.amendable_items(base)
                     if item["reason"] == "DUPLICATE_COLUMN")
    record = amend.apply(base["schema_id"], [
        _item(malformed, column="order_amt_legacy", type="decimal(18,2)"),
        # 类型不在类型格内：重校验不通过，继续隔离
        _item(duplicate, column="order_no_copy", type="geometry"),
    ])
    assert record["status"] == "partial"
    statuses = {item["item_id"]: item["status"] for item in record["items"]}
    assert statuses[malformed["item_id"]] == "amended"
    assert statuses[duplicate["item_id"]] == "rejected"
    revised = store.load_snapshot(record["result_schema_id"])
    assert revised["blocked_count"] == 1
    remaining = store.list_quarantine(revised["schema_id"])
    assert [row["reason"] for row in remaining if row["blocking"]] == ["DUPLICATE_COLUMN"]


def test_incomplete_amendment_is_rejected(run):
    base = store.load_snapshot(run["old_schema_id"])
    target = next(item for item in amend.amendable_items(base)
                  if item["reason"] == "LINE_MALFORMED")
    record = amend.apply(base["schema_id"], [_item(target, type="")])
    assert record["status"] == "rejected"
    assert record["items"][0]["reason"] == "AMENDMENT_INCOMPLETE"


def test_stale_revision_submission_rejected(run):
    """并发补正：基于已被落下的版本提交必须 409。"""
    base = store.load_snapshot(run["old_schema_id"])
    targets = amend.amendable_items(base)
    malformed, duplicate = targets[0], targets[1]
    amend.apply(base["schema_id"], [
        _item(malformed, column="order_amt_legacy", type="decimal(18,2)"),
    ])
    with pytest.raises(amend.RevisionStale) as caught:
        amend.apply(base["schema_id"], [
            _item(duplicate, column="order_no_copy", type="varchar(32)"),
        ])
    assert caught.value.detail["reason"] == "REVISION_STALE"

    with pytest.raises(amend.RevisionStale):
        amend.apply(base["schema_id"], [
            _item(duplicate, column="order_no_copy", type="varchar(32)"),
        ], expected_revision_seq=0)


def test_keep_action_leaves_item_quarantined(run):
    base = store.load_snapshot(run["old_schema_id"])
    target = next(item for item in amend.amendable_items(base)
                  if item["reason"] == "LINE_MALFORMED")
    record = amend.apply(base["schema_id"], [{"item_id": target["item_id"],
                                              "action": "keep"}])
    assert record["status"] == "skipped"
    assert record["result_schema_id"] is None
    still = store.load_snapshot(base["schema_id"])
    assert still["blocked_count"] == base["blocked_count"]


def test_amending_revision_survives_missing_base_quarantine(run):
    """修订版的隔离项编号是新发的，不能在修订版上再拿原编号补正。"""
    base = store.load_snapshot(run["old_schema_id"])
    target = next(item for item in amend.amendable_items(base)
                  if item["reason"] == "LINE_MALFORMED")
    record = amend.apply(base["schema_id"], [
        _item(target, column="order_amt_legacy", type="decimal(18,2)"),
    ])
    revised = store.load_snapshot(record["result_schema_id"])
    by_id = _amendable(revised["schema_id"])
    assert target["item_id"] not in by_id

    missing = amend.apply(revised["schema_id"], [
        _item(target, column="order_amt_legacy", type="decimal(18,2)"),
    ])
    assert missing["status"] == "rejected"
    assert missing["items"][0]["reason"] == "AMENDMENT_TARGET_MISSING"


def test_new_batch_uses_revision_only_when_selected(client):
    """新批次显式选用修订版才生效；历史批次与引用账不变。"""
    run = store.latest_run()
    base = store.load_snapshot(run["old_schema_id"])
    target = next(item for item in amend.amendable_items(base)
                  if item["reason"] == "LINE_MALFORMED")
    response = client.post("/api/schemas/amend", json={
        "schema_id": base["schema_id"],
        "items": [{**_item(target, column="order_amt_legacy", type="decimal(18,2)")}],
    })
    assert response.status_code == 200
    revised_id = response.json()["result_schema_id"]
    assert response.json()["summary"]["amended"] == 1

    # 默认行为：仍然按文件重新采信，原批次可幂等复用
    default_run = client.post("/api/runs", json={})
    assert default_run.status_code == 200
    assert default_run.json()["reused"] is True
    assert store.latest_run()["old_schema_id"] == base["schema_id"]

    selected = client.post("/api/runs", json={"old_schema_id": revised_id})
    assert selected.status_code == 200
    assert selected.json()["reused"] is False
    assert selected.json()["schema_refs"]["old"]["schema_id"] == revised_id
    assert selected.json()["schema_refs"]["old"]["revision_seq"] == 1

    again = client.post("/api/runs", json={"old_schema_id": revised_id})
    assert again.json()["reused"] is True
    assert again.json()["run_id"] == selected.json()["run_id"]

    history = {item["version_seq"]: item for item in client.get("/api/batches").json()["runs"]}
    original = history[1]
    assert original["schema_refs"]["old"]["revision_seq"] == 0
    assert original["old_total"] == expected.OLD_TOTAL
    assert original["new_total"] == expected.NEW_TOTAL


def test_history_migration_export_keeps_original_snapshot(client):
    """修订落库后，历史迁移单与引用账仍按生成时的快照导出。"""
    run = store.latest_run()
    before = client.get("/api/export/evidence.csv").text
    before_reconcile = client.get("/api/reconcile").json()
    base = store.load_snapshot(run["old_schema_id"])
    target = next(item for item in amend.amendable_items(base)
                  if item["reason"] == "LINE_MALFORMED")
    client.post("/api/schemas/amend", json={
        "schema_id": base["schema_id"],
        "items": [_item(target, column="order_amt_legacy", type="decimal(18,2)")],
    })
    after = client.get("/api/export/evidence.csv").text
    after_reconcile = client.get("/api/reconcile").json()
    assert before == after
    assert after_reconcile["fields"]["old_total"] == before_reconcile["fields"]["old_total"]
    assert after_reconcile["refs"] == before_reconcile["refs"]


def test_resume_only_processes_unfinished_items(run, monkeypatch):
    """处理失败后可续跑未完成项，已完成的项不重复采信。"""
    base = store.load_snapshot(run["old_schema_id"])
    targets = amend.amendable_items(base)
    malformed = next(item for item in targets if item["reason"] == "LINE_MALFORMED")
    duplicate = next(item for item in targets if item["reason"] == "DUPLICATE_COLUMN")

    calls = {"n": 0}
    real_build = amend._build_snapshot

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("磁盘一时写不进去")
        return real_build(*args, **kwargs)

    monkeypatch.setattr(amend, "_build_snapshot", flaky)
    with pytest.raises(RuntimeError):
        amend.apply(base["schema_id"], [
            _item(malformed, column="order_amt_legacy", type="decimal(18,2)"),
            _item(duplicate, column="order_no_copy", type="varchar(32)"),
        ])
    failed = store.list_amendments(1)[0]
    assert failed["status"] == "failed"
    assert failed["error"].startswith("RuntimeError")

    monkeypatch.setattr(amend, "_build_snapshot", real_build)
    resumed = amend.resume(failed["amendment_id"])
    assert resumed["status"] == "applied"
    assert resumed["summary"]["amended"] == 2
    assert resumed["amendment_id"] == failed["amendment_id"]

    snapshots = store.list_snapshots_for_source(base["source_path"])
    assert len(snapshots) == 2


def test_amendable_api_and_page_render(client):
    run = store.latest_run()
    schema_id = run["old_schema_id"]
    payload = client.get(f"/api/schemas/amendable?schema_id={schema_id}").json()
    assert payload["total"] == 2
    item_id = payload["items"][0]["item_id"]
    assert item_id

    page = client.get(f"/intake?schema_id={schema_id}")
    assert page.status_code == 200
    assert "采信与补正" in page.text
    assert item_id in page.text
    assert "LINE_MALFORMED" in page.text or "列定义不完整" in page.text

    revisions = client.get(f"/api/schemas/revisions?schema_id={schema_id}").json()
    assert revisions["revisions"][0]["revision_seq"] == 0
    assert revisions["current_schema_id"] == schema_id


def test_quarantine_csv_exposes_item_ids(client):
    run = store.latest_run()
    rows = list(csv.DictReader(io.StringIO(
        client.get(f"/api/schemas/quarantine.csv?schema_id={run['old_schema_id']}").text)))
    assert rows
    assert all(row["item_id"].startswith("q-") for row in rows)


def test_amendments_endpoint_reports_history(client):
    run = store.latest_run()
    base = store.load_snapshot(run["old_schema_id"])
    target = next(item for item in amend.amendable_items(base)
                  if item["reason"] == "LINE_MALFORMED")
    client.post("/api/schemas/amend", json={
        "schema_id": base["schema_id"],
        "items": [_item(target, column="order_amt_legacy", type="decimal(18,2)")],
    })
    payload = client.get(f"/api/schemas/amendments?schema_id={base['schema_id']}").json()
    assert payload["total"] == 1
    record = payload["amendments"][0]
    assert record["status"] == "applied"
    assert record["items"][0]["original"]["reason"] == "LINE_MALFORMED"
    assert record["items"][0]["result"]["column"] == "order_amt_legacy"
