"""接入层：把分批到达的 CSV 文件追加进库。

- 每个文件算 sha256，重复文件拒绝（批次幂等）。
- 事件按 event_uid 幂等去重；同一 order_id 的重复转化不在此删除，
  而是记入数据质量，由分析层按快照口径去重（保留原始日志）。
- 身份归并、曝光修订都是"新输入"，绝不回写旧事件。
"""
import csv
import hashlib
import json
import os
from datetime import datetime, timezone

from .db import connect, ensure_experiment

EVENT_TYPES = {"exposure", "conversion", "refund"}


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _cents(s):
    s = (s or "").strip()
    if not s:
        return 0
    return int(round(float(s) * 100))


def detect_kind(header):
    cols = {c.strip() for c in header}
    if {"event_uid", "event_type", "user_id", "merchant_id", "ts"} <= cols:
        return "events"
    if {"merchant_id", "arm"} <= cols:
        return "assignments"
    if {"merge_id", "primary_user", "secondary_user"} <= cols:
        return "merges"
    if {"revision_id", "event_uid", "new_merchant_id"} <= cols:
        return "revisions"
    return None


def ingest_file(db_path, path, experiment_id):
    """入库一个批次文件，返回批次信息 dict。重复文件返回 {'status': 'duplicate'}。"""
    digest = _sha256(path)
    conn = connect(db_path)
    try:
        ensure_experiment(conn, experiment_id)
        dup = conn.execute("SELECT batch_id FROM batches WHERE sha256=?",
                           (digest,)).fetchone()
        if dup:
            return {"status": "duplicate", "batch_id": dup["batch_id"],
                    "source": os.path.basename(path)}

        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            kind = detect_kind(reader.fieldnames or [])
            if kind is None:
                return {"status": "error",
                        "error": f"无法识别的文件表头: {reader.fieldnames}"}
            rows = list(reader)

        cur = conn.execute(
            "INSERT INTO batches(experiment_id, kind, source, sha256, n_rows,"
            " received_at) VALUES(?,?,?,?,?,?)",
            (experiment_id, kind, os.path.basename(path), digest, len(rows),
             _utcnow()))
        batch_id = cur.lastrowid

        detail = {}
        if kind == "events":
            accepted, detail = _ingest_events(conn, batch_id, experiment_id, rows)
        elif kind == "assignments":
            accepted, detail = _ingest_assignments(conn, batch_id, experiment_id, rows)
        elif kind == "merges":
            accepted, detail = _ingest_merges(conn, batch_id, experiment_id, rows)
        else:
            accepted, detail = _ingest_revisions(conn, batch_id, experiment_id, rows)

        rejected = len(rows) - accepted
        conn.execute(
            "UPDATE batches SET n_accepted=?, n_rejected=?, detail=? WHERE batch_id=?",
            (accepted, rejected, json.dumps(detail, ensure_ascii=False), batch_id))
        conn.commit()
        return {"status": "ok", "batch_id": batch_id, "kind": kind,
                "n_rows": len(rows), "accepted": accepted, "rejected": rejected,
                "detail": detail, "source": os.path.basename(path)}
    finally:
        conn.close()


def _ingest_events(conn, batch_id, experiment_id, rows):
    accepted = 0
    detail = {"duplicate_event_uid": 0, "bad_rows": 0}
    for r in rows:
        uid = (r.get("event_uid") or "").strip()
        etype = (r.get("event_type") or "").strip()
        user = (r.get("user_id") or "").strip()
        merch = (r.get("merchant_id") or "").strip()
        ts = (r.get("ts") or "").strip()
        order = (r.get("order_id") or "").strip()
        try:
            amount = _cents(r.get("amount"))
        except ValueError:
            amount = -1
        if (not uid or etype not in EVENT_TYPES or not user or not merch
                or not ts or amount < 0
                or (etype in ("conversion", "refund") and (not order or amount == 0))):
            detail["bad_rows"] += 1
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO events(event_uid, batch_id, experiment_id,"
            " event_type, user_id, merchant_id, ts, amount_cents, order_id)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (uid, batch_id, experiment_id, etype, user, merch, ts, amount, order))
        if cur.rowcount == 0:
            detail["duplicate_event_uid"] += 1
        else:
            accepted += 1
    return accepted, detail


def _ingest_assignments(conn, batch_id, experiment_id, rows):
    accepted = 0
    detail = {"bad_rows": 0}
    for r in rows:
        merch = (r.get("merchant_id") or "").strip()
        arm = (r.get("arm") or "").strip()
        if not merch or arm not in ("control", "treatment"):
            detail["bad_rows"] += 1
            continue
        conn.execute(
            "INSERT INTO assignments(experiment_id, merchant_id, arm, batch_id)"
            " VALUES(?,?,?,?)",
            (experiment_id, merch, arm, batch_id))
        accepted += 1
    return accepted, detail


def _ingest_merges(conn, batch_id, experiment_id, rows):
    accepted = 0
    detail = {"bad_rows": 0, "duplicate_merge_id": 0}
    for r in rows:
        mid = (r.get("merge_id") or "").strip()
        pri = (r.get("primary_user") or "").strip()
        sec = (r.get("secondary_user") or "").strip()
        if not mid or not pri or not sec or pri == sec:
            detail["bad_rows"] += 1
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO merges(merge_id, batch_id, experiment_id,"
            " primary_user, secondary_user) VALUES(?,?,?,?,?)",
            (mid, batch_id, experiment_id, pri, sec))
        if cur.rowcount == 0:
            detail["duplicate_merge_id"] += 1
        else:
            accepted += 1
    return accepted, detail


def _ingest_revisions(conn, batch_id, experiment_id, rows):
    accepted = 0
    detail = {"bad_rows": 0, "duplicate_revision_id": 0}
    for r in rows:
        rid = (r.get("revision_id") or "").strip()
        uid = (r.get("event_uid") or "").strip()
        merch = (r.get("new_merchant_id") or "").strip()
        reason = (r.get("reason") or "").strip()
        if not rid or not uid or not merch:
            detail["bad_rows"] += 1
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO revisions(revision_id, batch_id, experiment_id,"
            " event_uid, new_merchant_id, reason) VALUES(?,?,?,?,?,?)",
            (rid, batch_id, experiment_id, uid, merch, reason))
        if cur.rowcount == 0:
            detail["duplicate_revision_id"] += 1
        else:
            accepted += 1
    return accepted, detail


def ingest_dir(db_path, dir_path, experiment_id):
    """按文件名顺序入库一个目录下的所有 CSV（合成数据演示用）。"""
    results = []
    for name in sorted(os.listdir(dir_path)):
        if name.endswith(".csv"):
            results.append(ingest_file(db_path, os.path.join(dir_path, name),
                                       experiment_id))
    return results


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="入库批次文件")
    p.add_argument("--db", default="data/app.db")
    p.add_argument("--experiment", required=True)
    p.add_argument("paths", nargs="+", help="CSV 文件或目录")
    args = p.parse_args()
    from .db import init_db
    init_db(args.db)
    for path in args.paths:
        if os.path.isdir(path):
            for res in ingest_dir(args.db, path, args.experiment):
                print(res)
        else:
            print(ingest_file(args.db, path, args.experiment))
