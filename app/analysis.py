"""分析层：从冻结的输入快照构建分析帧，计算四种口径的估计与可复现的不确定性。

快照隔离原则：compute_report 只读取 batch_id <= through_batch 的行，
身份归并 / 曝光修订 / 新增批次只会产生新快照，绝不会混入旧报告的计算。

估计单位：
- itt_user  用户级（按首次曝光的组归属，受污染用户保留并标记，不删除）
- merchant  商户级（随机化单位本身，独立但样本量小）
- cluster   曝光聚类级（用户-商户二部图连通分量；仅用纯组聚类估计，混合聚类用于量化干扰）
- as_treated 实际接触口径（描述性，非随机化比较，不作因果结论）
"""
import json
import math
import random
from collections import defaultdict

ARMS = ("control", "treatment")


# ---------------------------------------------------------------- 统计工具

def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def var(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = mean(xs)
    return sum((x - m) ** 2 for x in xs) / (n - 1)


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def welch(a, b):
    """Welch 双样本 t；p 值为正态近似（小样本仅作参考，以 bootstrap CI 为准）。"""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return {"diff": (mean(a) - mean(b)) if a and b else 0.0, "se": 0.0,
                "df": 0.0, "p_approx": 1.0}
    va, vb = var(a), var(b)
    se = math.sqrt(va / na + vb / nb)
    diff = mean(a) - mean(b)
    if se == 0:
        return {"diff": diff, "se": 0.0, "df": 0.0, "p_approx": 1.0}
    t = diff / se
    df = ((va / na + vb / nb) ** 2 /
          ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)))
    return {"diff": diff, "se": se, "df": df,
            "p_approx": 2.0 * (1.0 - _phi(abs(t)))}


def boot_ci(t_vals, c_vals, n_boot, rng):
    """双臂分别重抽样的 percentile bootstrap，返回差值的 95% CI。

    确定性：调用方传入固定种子的 rng，且单位列表在传入前已按 id 排序。
    """
    nt, nc = len(t_vals), len(c_vals)
    if nt == 0 or nc == 0 or n_boot <= 0:
        return (0.0, 0.0)
    diffs = []
    for _ in range(n_boot):
        st = sum(t_vals[rng.randrange(nt)] for _ in range(nt)) / nt
        sc = sum(c_vals[rng.randrange(nc)] for _ in range(nc)) / nc
        diffs.append(st - sc)
    diffs.sort()
    lo = diffs[int(0.025 * n_boot)]
    hi = diffs[min(int(0.975 * n_boot), n_boot - 1)]
    return (lo, hi)


class UF:
    """并查集，canonical 取分量内最小 id，保证确定性。"""

    def __init__(self):
        self.p = {}

    def find(self, x):
        p = self.p
        if x not in p:
            p[x] = x
        root = x
        while p[root] != root:
            root = p[root]
        while p[x] != root:
            p[x], x = root, p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if rb < ra:
            ra, rb = rb, ra
        self.p[rb] = ra


# ---------------------------------------------------------------- 输入装载

def load_inputs(conn, experiment_id, through_batch):
    """读取快照的冻结输入视图。只读 batch_id <= through_batch 的行。"""
    # 分组：取每个商户在快照内最新一次指派
    assignments = {}
    for r in conn.execute(
            "SELECT merchant_id, arm FROM assignments"
            " WHERE experiment_id=? AND batch_id<=? ORDER BY batch_id",
            (experiment_id, through_batch)):
        assignments[r["merchant_id"]] = r["arm"]

    # 曝光修订：快照内最新修订覆盖事件商户
    revisions = {}
    n_rev_orphan = 0
    for r in conn.execute(
            "SELECT event_uid, new_merchant_id FROM revisions"
            " WHERE experiment_id=? AND batch_id<=? ORDER BY batch_id, revision_id",
            (experiment_id, through_batch)):
        revisions[r["event_uid"]] = r["new_merchant_id"]

    # 身份归并
    uf = UF()
    n_merges = 0
    for r in conn.execute(
            "SELECT primary_user, secondary_user FROM merges"
            " WHERE experiment_id=? AND batch_id<=? ORDER BY batch_id, merge_id",
            (experiment_id, through_batch)):
        uf.union(r["primary_user"], r["secondary_user"])
        n_merges += 1

    events = []
    known_uids = set()
    for r in conn.execute(
            "SELECT event_uid, event_type, user_id, merchant_id, ts,"
            " amount_cents, order_id FROM events"
            " WHERE experiment_id=? AND batch_id<=?"
            " ORDER BY ts, event_uid",
            (experiment_id, through_batch)):
        uid = r["event_uid"]
        known_uids.add(uid)
        events.append({
            "event_uid": uid,
            "event_type": r["event_type"],
            "user": uf.find(r["user_id"]),
            "merchant": revisions.get(uid, r["merchant_id"]),
            "revised": uid in revisions,
            "ts": r["ts"],
            "amount_cents": r["amount_cents"],
            "order_id": r["order_id"],
        })
    for uid in revisions:
        if uid not in known_uids:
            n_rev_orphan += 1

    return {"assignments": assignments, "events": events,
            "n_merges": n_merges, "n_revisions": len(revisions),
            "n_rev_orphan": n_rev_orphan}


# ---------------------------------------------------------------- 分析帧

def build_frames(inputs):
    """把输入整理成用户帧 / 商户帧 / 聚类帧 + 数据质量计数。"""
    assignments = inputs["assignments"]
    users = {}      # canonical user -> frame
    merchants = {}  # merchant -> frame
    dq = defaultdict(int)

    def uframe(u):
        return users.setdefault(u, {
            "user": u, "exposures": [], "arms": set(),
            "n_conv": 0, "gross": 0, "refund": 0, "net": 0})

    def mframe(m):
        return merchants.setdefault(m, {
            "merchant": m, "arm": assignments.get(m), "users": set(),
            "n_exp": 0, "n_conv": 0, "gross": 0, "refund": 0, "net": 0})

    orders = {}  # order_id -> conversion record（用于重复转化去重与退款匹配）

    for ev in inputs["events"]:
        m = ev["merchant"]
        arm = assignments.get(m)
        if arm is None:
            dq["events_unassigned_merchant"] += 1
            continue
        if ev["event_type"] == "exposure":
            u = uframe(ev["user"])
            mf = mframe(m)
            u["exposures"].append((ev["ts"], ev["event_uid"], m, arm))
            u["arms"].add(arm)
            mf["users"].add(ev["user"])
            mf["n_exp"] += 1
        elif ev["event_type"] == "conversion":
            key = ev["order_id"] or ("NOORDER:" + ev["event_uid"])
            if not ev["order_id"]:
                dq["conversion_missing_order_id"] += 1
            if key in orders:
                dq["duplicate_order_id"] += 1
                dq["duplicate_order_amount_cents"] += ev["amount_cents"]
                continue  # 重复转化：保留首条，计入 DQ
            u = uframe(ev["user"])
            mf = mframe(m)
            orders[key] = {"user": ev["user"], "merchant": m, "arm": arm,
                           "amount": ev["amount_cents"], "refund": 0}
            u["n_conv"] += 1
            u["gross"] += ev["amount_cents"]
            mf["n_conv"] += 1
            mf["gross"] += ev["amount_cents"]
        else:  # refund：先匹配订单，匹配不上只留痕，不建帧
            rec = orders.get(ev["order_id"])
            if rec is None:
                dq["unmatched_refund"] += 1
                dq["unmatched_refund_amount_cents"] += ev["amount_cents"]
                continue
            rec["refund"] += ev["amount_cents"]

    # 退款回填到用户/商户
    for rec in orders.values():
        if rec["refund"]:
            users[rec["user"]]["refund"] += rec["refund"]
            merchants[rec["merchant"]]["refund"] += rec["refund"]
    for u in users.values():
        u["net"] = u["gross"] - u["refund"]
        u["converted"] = 1 if u["n_conv"] > 0 else 0
        u["exposures"].sort(key=lambda x: (x[0], x[1]))
        u["first_arm"] = u["exposures"][0][3] if u["exposures"] else None
        u["contaminated"] = 1 if len(u["arms"]) > 1 else 0
    for mf in merchants.values():
        mf["net"] = mf["gross"] - mf["refund"]
        mf["n_users"] = len(mf["users"])

    # 曝光二部图的连通分量（聚类）
    adj_u = defaultdict(set)
    adj_m = defaultdict(set)
    for u in users.values():
        for (_, _, m, _) in u["exposures"]:
            adj_u[u["user"]].add(m)
            adj_m[m].add(u["user"])
    comp_id = {}
    comps = []
    for start in sorted(adj_u):
        if start in comp_id:
            continue
        cid = len(comps)
        stack = [("u", start)]
        members_u, members_m = [], []
        while stack:
            kind, x = stack.pop()
            if x in comp_id:
                continue
            comp_id[x] = cid
            if kind == "u":
                members_u.append(x)
                for m in sorted(adj_u[x]):
                    if m not in comp_id:
                        stack.append(("m", m))
            else:
                members_m.append(x)
                for uu in sorted(adj_m[x]):
                    if uu not in comp_id:
                        stack.append(("u", uu))
        arms_in = sorted({assignments.get(m) for m in members_m
                          if assignments.get(m)})
        comps.append({
            "cid": cid,
            "users": sorted(members_u),
            "merchants": sorted(members_m),
            "arms": arms_in,
            "pure": len(arms_in) == 1,
        })
    for u in users.values():
        u["cid"] = comp_id.get(u["user"])

    return {"users": users, "merchants": merchants, "components": comps,
            "dq": dict(dq), "n_orders": len(orders)}


# ---------------------------------------------------------------- 口径计算

def _arm_block(user_list):
    n = len(user_list)
    gross = sum(u["gross"] for u in user_list)
    refund = sum(u["refund"] for u in user_list)
    conv = sum(u["converted"] for u in user_list)
    net = gross - refund
    return {
        "users": n,
        "converted": conv,
        "conv_rate": (conv / n) if n else 0.0,
        "gross_cents": gross,
        "refund_cents": refund,
        "net_cents": net,
        "net_per_user_cents": (net / n) if n else 0.0,
        "refund_rate": (refund / gross) if gross else 0.0,
        "contaminated_users": sum(u["contaminated"] for u in user_list),
    }


def _effect(t_vals, c_vals, n_boot, rng):
    w = welch(t_vals, c_vals)
    lo, hi = boot_ci(t_vals, c_vals, n_boot, rng)
    return {"diff": w["diff"], "se": w["se"], "ci_lo": lo, "ci_hi": hi,
            "p_approx": w["p_approx"], "n_t": len(t_vals), "n_c": len(c_vals)}


def _rng(seed, snapshot_id, tag):
    # random.Random(str) 走 sha512，跨进程/跨机器确定性一致
    return random.Random(f"{seed}:{snapshot_id}:{tag}")


def compute_report(conn, experiment_id, snapshot_id, seed, n_boot):
    """对一个快照计算全部口径，返回 {section: payload}。纯函数式：不改写任何旧数据。"""
    snap = conn.execute(
        "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if snap is None:
        raise ValueError(f"snapshot {snapshot_id} 不存在")
    through_batch = snap["through_batch"]

    inputs = load_inputs(conn, experiment_id, through_batch)
    frames = build_frames(inputs)
    users = frames["users"]
    merchants = frames["merchants"]
    comps = frames["components"]
    dq = dict(frames["dq"])
    dq["merge_rows_applied"] = inputs["n_merges"]
    dq["revision_rows_applied"] = inputs["n_revisions"]
    dq["revision_orphan_event"] = inputs["n_rev_orphan"]

    all_users = sorted(users)
    contaminated = [u for u in all_users if users[u]["contaminated"]]
    total_conv = sum(users[u]["converted"] for u in all_users)
    total_net = sum(users[u]["net"] for u in all_users)
    cont_conv = sum(users[u]["converted"] for u in contaminated)
    cont_net = sum(users[u]["net"] for u in contaminated)

    summary = {
        "experiment_id": experiment_id,
        "snapshot_id": snapshot_id,
        "through_batch": through_batch,
        "seed": seed,
        "n_boot": n_boot,
        "n_merchants": len(merchants),
        "n_users": len(all_users),
        "n_exposures": sum(len(users[u]["exposures"]) for u in all_users),
        "n_orders_valid": frames["n_orders"],
        "n_contaminated_users": len(contaminated),
        "contamination_share": (len(contaminated) / len(all_users)) if all_users else 0.0,
        "contaminated_conv_share": (cont_conv / total_conv) if total_conv else 0.0,
        "contaminated_net_share": (cont_net / total_net) if total_net else 0.0,
        "n_components": len(comps),
        "n_components_pure": sum(1 for c in comps if c["pure"]),
        "n_components_mixed": sum(1 for c in comps if not c["pure"]),
        "assignments": dict(sorted(inputs["assignments"].items())),
    }

    sections = {"summary": summary, "dq": dq}

    # ---- 按原分配（ITT 归属：首次曝光组）与实际接触，两种描述口径始终同时给出
    by_first = {a: [users[u] for u in all_users if users[u]["first_arm"] == a]
                for a in ARMS}
    by_exposure = {a: [users[u] for u in all_users if a in users[u]["arms"]]
                   for a in ARMS}
    sections["as_assigned"] = {a: _arm_block(by_first[a]) for a in ARMS}
    sections["as_treated"] = {a: _arm_block(by_exposure[a]) for a in ARMS}
    sections["as_treated"]["_caveat"] = (
        "实际接触口径：用户可同时出现在两臂，存在自选偏差，"
        "仅为描述性统计，不构成随机化比较。")

    # ---- 口径一：用户级 ITT（受污染用户保留并标记；剔除会破坏随机化可比性）
    t_vals = sorted(u for u in all_users if users[u]["first_arm"] == "treatment")
    c_vals = sorted(u for u in all_users if users[u]["first_arm"] == "control")
    sections["method:itt_user"] = {
        "unit": "user",
        "label": "用户级 ITT（按首次曝光归属，受污染用户保留）",
        "arms": {a: _arm_block(by_first[a]) for a in ARMS},
        "effects": {
            "conv_rate": _effect([users[u]["converted"] for u in t_vals],
                                 [users[u]["converted"] for u in c_vals],
                                 n_boot, _rng(seed, snapshot_id, "itt:conv")),
            "net_per_user": _effect([users[u]["net"] for u in t_vals],
                                    [users[u]["net"] for u in c_vals],
                                    n_boot, _rng(seed, snapshot_id, "itt:net")),
        },
        "caveats": [
            "随机化单位是商户而估计单位是用户：同一商户内用户结果相关，"
            "用户级 SE 偏乐观，CI 偏窄。",
            f"受污染用户 {len(contaminated)} 人保留在分析中并标记；"
            "删除他们会破坏随机化可比性，本报告不这么做。",
            "跨组接触会稀释 ITT 效应（向零衰减），解读为‘按分配意图’的效果。",
        ],
        "sensitivity_excl_contaminated": {
            "note": "仅作敏感性参考：剔除受污染用户后不再是随机化比较，不得据此宣称实验结论。",
            "conv_rate_diff": _safe_rate_diff(by_first),
        },
    }

    # ---- 口径二：商户级（随机化单位，独立，但样本量小）
    m_sorted = sorted(merchants)
    m_t = [m for m in m_sorted if merchants[m]["arm"] == "treatment"]
    m_c = [m for m in m_sorted if merchants[m]["arm"] == "control"]

    def m_rate(m):
        mf = merchants[m]
        return mf["n_conv"] / mf["n_users"] if mf["n_users"] else 0.0

    def m_net(m):
        mf = merchants[m]
        return mf["net"] / mf["n_users"] if mf["n_users"] else 0.0

    sections["method:merchant"] = {
        "unit": "merchant",
        "label": "商户级（随机化单位本身）",
        "n_units": {"treatment": len(m_t), "control": len(m_c)},
        "effects": {
            "conv_rate": _effect([m_rate(m) for m in m_t],
                                 [m_rate(m) for m in m_c],
                                 n_boot, _rng(seed, snapshot_id, "mer:conv")),
            "net_per_user": _effect([m_net(m) for m in m_t],
                                    [m_net(m) for m in m_c],
                                    n_boot, _rng(seed, snapshot_id, "mer:net")),
        },
        "caveats": [
            "商户是随机化单位，商户间相互独立（用户跨商户不影响商户级总值的独立性假设"
            "以外的结论需结合聚类口径）。",
            f"样本量仅 {len(m_t)}+{len(m_c)} 个商户，检验效能低、CI 宽——"
            "这是独立性换来的代价。",
            "各商户等权；与用户级口径的估计量权重不同，数值不可直接对齐。",
        ],
    }

    # ---- 口径三：曝光聚类级（仅用纯组聚类估计；混合聚类量化干扰）
    pure_t = sorted(c["cid"] for c in comps
                    if c["pure"] and c["arms"] == ["treatment"])
    pure_c = sorted(c["cid"] for c in comps
                    if c["pure"] and c["arms"] == ["control"])
    mixed = [c for c in comps if not c["pure"]]

    def comp_stats(cid):
        us = [users[u] for u in comps[cid]["users"]]
        n = len(us)
        conv = sum(u["converted"] for u in us)
        net = sum(u["net"] for u in us)
        return (conv / n if n else 0.0), (net / n if n else 0.0), n

    ct_rate = [comp_stats(c)[0] for c in pure_t]
    cc_rate = [comp_stats(c)[0] for c in pure_c]
    ct_net = [comp_stats(c)[1] for c in pure_t]
    cc_net = [comp_stats(c)[1] for c in pure_c]
    mixed_users = sum(len(c["users"]) for c in mixed)
    mixed_conv = sum(users[u]["converted"] for c in mixed for u in c["users"])
    mixed_net = sum(users[u]["net"] for c in mixed for u in c["users"])

    sections["method:cluster"] = {
        "unit": "cluster",
        "label": "曝光聚类级（仅纯组聚类参与估计）",
        "n_units": {"treatment": len(pure_t), "control": len(pure_c),
                    "mixed_excluded": len(mixed)},
        "effects": {
            "conv_rate": _effect(ct_rate, cc_rate, n_boot,
                                 _rng(seed, snapshot_id, "clu:conv")),
            "net_per_user": _effect(ct_net, cc_net, n_boot,
                                    _rng(seed, snapshot_id, "clu:net")),
        },
        "interference": {
            "mixed_components": len(mixed),
            "mixed_users": mixed_users,
            "mixed_user_share": (mixed_users / len(all_users)) if all_users else 0.0,
            "mixed_conversions": mixed_conv,
            "mixed_net_cents": mixed_net,
        },
        "caveats": [
            "聚类 = 用户-商户曝光二部图的连通分量；分量之间无曝光边，独立性更可信。",
            "仅纯组聚类进入效应估计：结论只外推到‘无跨组接触’的子总体，"
            "不代表全量用户。",
            f"混合聚类 {len(mixed)} 个（覆盖 {mixed_users} 用户）被排除在估计外，"
            "其体量即组间干扰的规模，见污染路径页。",
            "聚类数少时 CI 很宽；聚类级与用户级估计的权重不同。",
        ],
    }

    # ---- 污染路径
    patterns = defaultdict(int)
    path_rows = []
    for u in contaminated:
        uf_ = users[u]
        seq = [arm for (_, _, _, arm) in uf_["exposures"]]
        comp = [seq[0]] if seq else []
        for a in seq[1:]:
            if a != comp[-1]:
                comp.append(a)
        patterns["→".join("T" if a == "treatment" else "C" for a in comp)] += 1
        path_rows.append({
            "user": u,
            "n_exposures": len(uf_["exposures"]),
            "merchants": [f"{m}({'T' if a == 'treatment' else 'C'})"
                          for (_, _, m, a) in uf_["exposures"]],
            "n_conv": uf_["n_conv"],
            "net_cents": uf_["net"],
        })
    path_rows.sort(key=lambda r: (-r["n_conv"], -abs(r["net_cents"]), r["user"]))
    sections["paths"] = {
        "patterns": sorted(({"pattern": k, "n": v}
                            for k, v in patterns.items()),
                           key=lambda x: -x["n"]),
        "top_users": path_rows[:200],
        "n_contaminated": len(contaminated),
    }

    # ---- 组贡献明细（供下钻页）
    arm_groups = {}
    for a in ARMS:
        rows = []
        for m in m_sorted:
            mf = merchants[m]
            if mf["arm"] != a:
                continue
            rows.append({
                "merchant": m,
                "n_users": mf["n_users"],
                "n_exp": mf["n_exp"],
                "n_conv": mf["n_conv"],
                "conv_rate": m_rate(m),
                "gross_cents": mf["gross"],
                "refund_cents": mf["refund"],
                "net_cents": mf["net"],
                "net_per_user_cents": m_net(m),
            })
        arm_groups[a] = rows
    cluster_groups = []
    for c in comps:
        rate, netpu, n = comp_stats(c["cid"])
        top = sorted((users[u] for u in c["users"]),
                     key=lambda u: (-abs(u["net"]), u["user"]))[:10]
        cluster_groups.append({
            "cid": c["cid"],
            "pure": c["pure"],
            "arms": c["arms"],
            "merchants": c["merchants"],
            "n_users": n,
            "conv_rate": rate,
            "net_per_user_cents": netpu,
            "n_conv": sum(users[u]["converted"] for u in c["users"]),
            "net_cents": sum(users[u]["net"] for u in c["users"]),
            "top_users": [{"user": t["user"], "net_cents": t["net"],
                           "n_conv": t["n_conv"],
                           "contaminated": t["contaminated"]} for t in top],
        })
    sections["groups"] = {"arms": arm_groups, "clusters": cluster_groups}

    return sections


def _safe_rate_diff(by_first):
    """剔除受污染用户后的转化率差（仅敏感性参考，非随机化比较）。"""
    rows = []
    for a in ARMS:
        us = [u for u in by_first[a] if not u["contaminated"]]
        n = len(us)
        rows.append((sum(u["converted"] for u in us) / n) if n else 0.0)
    return rows[1] - rows[0]


# ---------------------------------------------------------------- 报告存取

def save_sections(conn, report_id, sections):
    for name, payload in sections.items():
        conn.execute(
            "INSERT OR REPLACE INTO sections(report_id, section, json)"
            " VALUES(?,?,?)",
            (report_id, name, json.dumps(payload, ensure_ascii=False)))


def load_sections(conn, report_id):
    return {r["section"]: json.loads(r["json"]) for r in conn.execute(
        "SELECT section, json FROM sections WHERE report_id=?", (report_id,))}
