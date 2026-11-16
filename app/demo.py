"""一键演示流水线：合成数据 → 两阶段入库 → 两个输入版本 → 各出一份报告。

两阶段入库模拟真实时序：批次 1-4 先到达并冻结快照；身份归并、曝光修订、
迟到事件随后到达，形成第二个输入版本。两份报告各自保持自己的输入版本。
"""
import argparse
import os

from . import ingest, synth, worker
from .db import init_db


def run(db_path, synth_dir, experiment_id="EXP-SYNTH-01", seed=20261116,
        n_boot=1000, log=print):
    stats = synth.generate(synth_dir, experiment_id, seed)
    log(f"合成数据已生成: {stats['orders']} 订单, "
        f"{stats['events_main']} 主事件, 种子 {seed}")
    init_db(db_path)
    files = sorted(f for f in os.listdir(synth_dir) if f.endswith(".csv"))
    for f in files:
        if int(f[5:7]) <= 4:
            r = ingest.ingest_file(db_path, os.path.join(synth_dir, f),
                                   experiment_id)
            log(f"入库 {f}: {r.get('status')} accepted={r.get('accepted')}")
    snap1 = worker.create_snapshot(db_path, experiment_id,
                                   "归并/修订前（批次≤4）")
    for f in files:
        if int(f[5:7]) > 4:
            r = ingest.ingest_file(db_path, os.path.join(synth_dir, f),
                                   experiment_id)
            log(f"入库 {f}: {r.get('status')} accepted={r.get('accepted')}")
    snap2 = worker.create_snapshot(db_path, experiment_id,
                                   "全部批次（含归并/修订/迟到）")
    worker.queue_report(db_path, experiment_id, snap1, seed=42, n_boot=n_boot)
    worker.queue_report(db_path, experiment_id, snap2, seed=42, n_boot=n_boot)
    worker.drain(db_path, log=log)
    log(f"完成：snapshot #{snap1} / #{snap2} 各一份报告。"
        f"启动网页：python3 -m app.server --db {db_path}")
    return snap1, snap2


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="一键演示流水线")
    p.add_argument("--db", default="data/app.db")
    p.add_argument("--out", default="data/synth")
    p.add_argument("--experiment", default="EXP-SYNTH-01")
    p.add_argument("--seed", type=int, default=20261116)
    p.add_argument("--n-boot", type=int, default=1000)
    args = p.parse_args()
    run(args.db, args.out, args.experiment, args.seed, args.n_boot)
