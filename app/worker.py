"""分析工作进程：轮询 pending 报告并计算。

隔离保证：每个报告只读取自己快照（batch_id <= through_batch）的输入，
身份归并 / 曝光修订 / 新批次只会触发新快照 + 新报告，旧报告结果永不改写。
"""
import argparse
import json
import time
from datetime import datetime, timezone

from .analysis import compute_report, save_sections
from .db import connect, init_db


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_one(conn, row, log=print):
    rid = row["report_id"]
    conn.execute("UPDATE reports SET status='running' WHERE report_id=?", (rid,))
    conn.commit()
    try:
        sections = compute_report(conn, row["experiment_id"],
                                  row["snapshot_id"], row["seed"],
                                  row["n_boot"])
        save_sections(conn, rid, sections)
        conn.execute(
            "UPDATE reports SET status='done', finished_at=? WHERE report_id=?",
            (_utcnow(), rid))
        conn.commit()
        log(f"[worker] report #{rid} done "
            f"(snapshot={row['snapshot_id']}, seed={row['seed']}, "
            f"n_boot={row['n_boot']})")
    except Exception as exc:  # 失败留痕，不影响其他任务
        conn.execute(
            "UPDATE reports SET status='failed', error=? WHERE report_id=?",
            (f"{type(exc).__name__}: {exc}", rid))
        conn.commit()
        log(f"[worker] report #{rid} FAILED: {exc}")


def run_pending(db_path, once=False, interval=2.0, log=print):
    init_db(db_path)
    while True:
        conn = connect(db_path)
        row = conn.execute(
            "SELECT * FROM reports WHERE status='pending'"
            " ORDER BY report_id LIMIT 1").fetchone()
        if row is None:
            conn.close()
            if once:
                return
            time.sleep(interval)
            continue
        run_one(conn, row, log=log)
        conn.close()
        if once:
            return


def drain(db_path, log=print):
    """处理完全部待办报告后返回。"""
    while True:
        conn = connect(db_path)
        n = conn.execute(
            "SELECT COUNT(*) c FROM reports WHERE status='pending'"
        ).fetchone()["c"]
        conn.close()
        if n == 0:
            return
        run_pending(db_path, once=True, log=log)


def create_snapshot(db_path, experiment_id, note=""):
    """以当前最大 batch_id 冻结一个输入版本。"""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(batch_id), 0) AS mb FROM batches"
            " WHERE experiment_id=?", (experiment_id,)).fetchone()
        cur = conn.execute(
            "INSERT INTO snapshots(experiment_id, through_batch, note,"
            " created_at) VALUES(?,?,?,?)",
            (experiment_id, row["mb"], note, _utcnow()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def queue_report(db_path, experiment_id, snapshot_id, seed=42, n_boot=1000):
    from . import CODE_VERSION
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO reports(experiment_id, snapshot_id, seed, n_boot,"
            " code_version, status, created_at)"
            " VALUES(?,?,?,?,?, 'pending', ?)",
            (experiment_id, snapshot_id, seed, n_boot, CODE_VERSION,
             _utcnow()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="分析工作进程")
    p.add_argument("--db", default="data/app.db")
    p.add_argument("--once", action="store_true", help="处理一个待办后退出")
    p.add_argument("--interval", type=float, default=2.0)
    args = p.parse_args()

    def _log(msg):
        print(msg, flush=True)

    _log(f"[worker] db={args.db} once={args.once}")
    run_pending(args.db, once=args.once, interval=args.interval, log=_log)
