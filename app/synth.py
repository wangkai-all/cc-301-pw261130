"""合成数据生成器（确定性种子）。

生成的世界：30 个商户按商户级分层随机分臂（15/15）。结构上分为——
- 20 个"孤立商户"：七成用户是忠诚用户，只逛这一个商户
  → 曝光图中的纯组连通分量；
- 2 个"开放商圈"（各 5 个商户）：三成用户在商圈内逛 1~3 家，
  自然产生跨组曝光 → 混合连通分量（组间干扰的载体）。
另有重复转化、退款（含无法匹配的退款）、身份归并（同一用户的第二设备 id）、
曝光归因修订。全部为合成数据，不宣称任何真实投放效果。
"""
import csv
import os
import random

BASE_DAY = "2026-11-"
N_MERCHANTS = 30
N_ISOLATED = 20
HUB_SIZE = 5
N_USERS = 5000
DEFAULT_SEED = 20261116


def _ts(rng, day):
    return (f"{BASE_DAY}{day + 1:02d}T{rng.randrange(8, 23):02d}:"
            f"{rng.randrange(60):02d}:{rng.randrange(60):02d}")


def generate(out_dir, experiment_id="EXP-SYNTH-01", seed=DEFAULT_SEED):
    rng = random.Random(seed)
    os.makedirs(out_dir, exist_ok=True)

    merchants = [f"M{i:02d}" for i in range(1, N_MERCHANTS + 1)]
    isolated = merchants[:N_ISOLATED]
    hubs = [merchants[N_ISOLATED:N_ISOLATED + HUB_SIZE],
            merchants[N_ISOLATED + HUB_SIZE:]]

    # 商户级分层随机分臂：孤立商户内 10/10，商圈商户内平衡，总体 15/15
    treat = set(rng.sample(isolated, N_ISOLATED // 2))
    hub_flat = [m for h in hubs for m in h]
    treat |= set(rng.sample(hub_flat, N_MERCHANTS // 2 - N_ISOLATED // 2))
    arm_of = {m: ("treatment" if m in treat else "control") for m in merchants}

    users = [f"U{i:04d}" for i in range(1, N_USERS + 1)]
    base_prop = {u: rng.uniform(0.04, 0.16) for u in users}

    seq = [0]

    def eid():
        seq[0] += 1
        return f"E{seq[0]:06d}"

    oseq = [0]

    def oid():
        oseq[0] += 1
        return f"O{oseq[0]:06d}"

    events = []          # (day, row_dict)
    orders = []          # 用于退款/重复转化
    exposure_index = []  # (event_uid, merchant) 供修订抽样
    visited_of = {}      # user -> [merchant]

    for u in users:
        if rng.random() < 0.70:
            visited = [rng.choice(isolated)]
        else:
            hub = hubs[rng.randrange(len(hubs))]
            r = rng.random()
            n_visit = 1 if r < 0.30 else (2 if r < 0.70 else 3)
            visited = rng.sample(hub, n_visit)
        visited_of[u] = visited
        for m in visited:
            lift = 1.25 if arm_of[m] == "treatment" else 1.0
            day0 = rng.randrange(0, 5)
            for _ in range(rng.randint(1, 3)):
                d = min(day0 + rng.randrange(0, 3), 6)
                uid = eid()
                events.append((d, {"event_uid": uid, "event_type": "exposure",
                                   "user_id": u, "merchant_id": m,
                                   "ts": _ts(rng, d), "amount": "",
                                   "order_id": ""}))
                exposure_index.append((uid, m))
            p_conv = min(base_prop[u] * lift, 0.60)
            if rng.random() < p_conv:
                d = min(day0 + rng.randrange(0, 3), 6)
                cents = max(1000, min(50000,
                                      int(rng.lognormvariate(4.0, 0.6) * 10)))
                order = oid()
                events.append((d, {"event_uid": eid(),
                                   "event_type": "conversion",
                                   "user_id": u, "merchant_id": m,
                                   "ts": _ts(rng, d),
                                   "amount": f"{cents / 100:.2f}",
                                   "order_id": order}))
                orders.append({"order_id": order, "user_id": u,
                               "merchant_id": m, "cents": cents, "day": d})

    # 退款：8% 订单，50%-100% 金额，1-3 天后；超出主窗口的进迟到批次
    refunds = []
    late = []
    for o in orders:
        if rng.random() < 0.08:
            d = min(o["day"] + rng.randint(1, 3), 9)
            cents = int(o["cents"] * rng.uniform(0.5, 1.0))
            row = {"event_uid": eid(), "event_type": "refund",
                   "user_id": o["user_id"], "merchant_id": o["merchant_id"],
                   "ts": _ts(rng, min(d, 6)),
                   "amount": f"{cents / 100:.2f}", "order_id": o["order_id"]}
            if d <= 6:
                refunds.append((d, row))
            else:
                late.append(row)

    # 无法匹配的退款（坏 order_id），进批次 7
    for o in rng.sample(orders, max(1, int(len(orders) * 0.005))):
        cents = int(o["cents"] * rng.uniform(0.5, 1.0))
        late.append({"event_uid": eid(), "event_type": "refund",
                     "user_id": o["user_id"], "merchant_id": o["merchant_id"],
                     "ts": _ts(rng, 6),
                     "amount": f"{cents / 100:.2f}",
                     "order_id": o["order_id"] + "-X"})

    # 重复转化：3% 订单在批次 7 以新 event_uid 重发
    for o in rng.sample(orders, max(1, int(len(orders) * 0.03))):
        late.append({"event_uid": eid(), "event_type": "conversion",
                     "user_id": o["user_id"], "merchant_id": o["merchant_id"],
                     "ts": _ts(rng, 6),
                     "amount": f"{o['cents'] / 100:.2f}",
                     "order_id": o["order_id"]})

    # 身份归并：60 个用户有第二设备 id，其部分行为在批次 7 才以别名出现
    merges = []
    merged = rng.sample(users, 60)
    for i, u in enumerate(merged, 1):
        alias = f"A{i:04d}"
        merges.append({"merge_id": f"MG{i:04d}", "primary_user": u,
                       "secondary_user": alias})
        for m in rng.sample(visited_of[u], min(2, len(visited_of[u]))):
            late.append({"event_uid": eid(), "event_type": "exposure",
                         "user_id": alias, "merchant_id": m,
                         "ts": _ts(rng, 6), "amount": "", "order_id": ""})
        if rng.random() < 0.35:
            m = rng.choice(visited_of[u])
            cents = max(1000, int(rng.lognormvariate(4.0, 0.6) * 10))
            late.append({"event_uid": eid(), "event_type": "conversion",
                         "user_id": alias, "merchant_id": m,
                         "ts": _ts(rng, 6),
                         "amount": f"{cents / 100:.2f}", "order_id": oid()})

    # 曝光修订：12 条曝光的商户归因修正
    revisions = []
    for i, (uid, old_m) in enumerate(rng.sample(exposure_index, 12), 1):
        new_m = rng.choice([m for m in merchants if m != old_m])
        revisions.append({"revision_id": f"RV{i:04d}", "event_uid": uid,
                          "new_merchant_id": new_m,
                          "reason": "商户归因修正（合成）"})

    # 迟到普通事件
    for _ in range(120):
        u = rng.choice(users)
        m = rng.choice(visited_of[u])
        late.append({"event_uid": eid(), "event_type": "exposure",
                     "user_id": u, "merchant_id": m,
                     "ts": _ts(rng, 6), "amount": "", "order_id": ""})

    # ---- 写批次文件
    def w(name, fieldnames, rows):
        path = os.path.join(out_dir, name)
        with open(path, "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=fieldnames)
            wr.writeheader()
            wr.writerows(rows)
        return path

    EV = ["event_uid", "event_type", "user_id", "merchant_id", "ts",
          "amount", "order_id"]
    w("batch01_assignments.csv", ["merchant_id", "arm"],
      [{"merchant_id": m, "arm": arm_of[m]} for m in merchants])
    main = sorted(events + refunds, key=lambda x: (x[0], x[1]["event_uid"]))
    w("batch02_events.csv", EV, [r for (d, r) in main if d <= 2])
    w("batch03_events.csv", EV, [r for (d, r) in main if 3 <= d <= 4])
    w("batch04_events.csv", EV, [r for (d, r) in main if 5 <= d <= 6])
    w("batch05_merges.csv", ["merge_id", "primary_user", "secondary_user"],
      merges)
    w("batch06_revisions.csv",
      ["revision_id", "event_uid", "new_merchant_id", "reason"], revisions)
    w("batch07_events.csv", EV, late)

    return {
        "experiment_id": experiment_id,
        "seed": seed,
        "merchants": N_MERCHANTS,
        "treatment_merchants": sum(1 for a in arm_of.values()
                                   if a == "treatment"),
        "isolated_merchants": N_ISOLATED,
        "hub_merchants": N_MERCHANTS - N_ISOLATED,
        "users": N_USERS,
        "events_main": len(events),
        "refunds": len(refunds),
        "late_events": len(late),
        "merges": len(merges),
        "revisions": len(revisions),
        "orders": len(orders),
        "note": "合成数据：处理臂转化概率相对 +25%，仅用于演示，"
                "不宣称真实投放效果。",
    }


if __name__ == "__main__":
    import argparse
    import json
    p = argparse.ArgumentParser(description="生成合成数据批次")
    p.add_argument("--out", default="data/synth")
    p.add_argument("--experiment", default="EXP-SYNTH-01")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = p.parse_args()
    print(json.dumps(generate(args.out, args.experiment, args.seed),
                     ensure_ascii=False, indent=2))
