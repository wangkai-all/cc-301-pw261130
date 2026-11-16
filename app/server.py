"""Web 界面（纯标准库 http.server，服务端渲染，无外部依赖）。

页面：
- /                      总览（批次 / 快照 / 报告 / 数据质量 / 操作）
- /experiment/<eid>      分析页：按原分配 vs 实际接触、估计单位切换、不确定性比较
- /experiment/<eid>/paths   污染路径
- /experiment/<eid>/group   组贡献下钻（臂 / 聚类）
- /reports               报告列表（含输入版本）与比较入口
- /compare?a=&b=         两份报告并排比较
- /methodology           方法与取舍说明
"""
import html
import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import analysis, ingest, synth, worker
from .db import connect, init_db

DB_PATH = os.environ.get("ADEXP_DB", "data/app.db")
SYNTH_DIR = os.environ.get("ADEXP_SYNTH_DIR", "data/synth")
METHODS = ["itt_user", "merchant", "cluster"]
METHOD_LABELS = {
    "itt_user": "用户级 ITT",
    "merchant": "商户级",
    "cluster": "聚类级",
}
ARM_LABELS = {"control": "对照组", "treatment": "处理组"}


# ---------------------------------------------------------------- 展示工具

def esc(s):
    return html.escape(str(s), quote=True)


def yuan(cents):
    return f"¥{cents / 100:,.2f}"


def syuan(cents):
    sign = "+" if cents >= 0 else "-"
    return f"{sign}¥{abs(cents) / 100:,.2f}"


def pct(x, d=2):
    return f"{x * 100:.{d}f}%"


def pp(x):
    return f"{x * 100:+.2f}pp"


def table(headers, rows, cls=""):
    out = [f'<table class="{cls}"><thead><tr>']
    out += [f"<th>{h}</th>" for h in headers]
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei",
       sans-serif; margin: 0; color: #1d2129; background: #f5f6f7; }
header { background: #1f2329; color: #fff; padding: 10px 20px; }
header a { color: #9fd0ff; text-decoration: none; margin-right: 14px; }
header .title { font-weight: 700; margin-right: 24px; color: #fff; }
main { max-width: 1180px; margin: 18px auto; padding: 0 16px; }
.card { background: #fff; border: 1px solid #e5e6eb; border-radius: 8px;
        padding: 14px 18px; margin-bottom: 16px; }
.card h2 { margin: 2px 0 10px; font-size: 17px; }
.card h3 { margin: 12px 0 6px; font-size: 15px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { border: 1px solid #e5e6eb; padding: 5px 8px; text-align: right; }
th { background: #f2f3f5; text-align: center; }
td:first-child, th:first-child { text-align: left; }
.tabs a { display: inline-block; padding: 6px 14px; margin-right: 6px;
          border: 1px solid #c9cdd4; border-radius: 6px; color: #1d2129;
          text-decoration: none; background: #fff; }
.tabs a.on { background: #1f2329; color: #fff; border-color: #1f2329; }
.banner { padding: 8px 12px; border-radius: 6px; margin-bottom: 10px;
          font-size: 13px; }
.warn { background: #fff7e6; border: 1px solid #ffd591; }
.info { background: #e8f3ff; border: 1px solid #94c1ff; }
.bad  { background: #ffece8; border: 1px solid #fdaca5; }
.ok   { background: #e8ffea; border: 1px solid #7be188; }
.muted { color: #86909c; font-size: 12px; }
.cibar-wrap { position: relative; height: 16px; background: #f2f3f5;
              border-radius: 3px; min-width: 220px; }
.cibar { position: absolute; top: 6px; height: 4px; background: #4080ff;
         border-radius: 2px; }
.cidot { position: absolute; top: 3px; width: 4px; height: 10px;
         background: #1d2129; border-radius: 2px; }
.zline { position: absolute; top: 0; width: 1px; height: 16px;
         background: #f53f3f; }
form.inline { display: inline-block; margin: 2px 6px 2px 0; }
input, select { padding: 4px 6px; border: 1px solid #c9cdd4; border-radius: 4px;
                font-size: 13px; }
button { padding: 5px 12px; border: 1px solid #1f2329; background: #1f2329;
         color: #fff; border-radius: 4px; cursor: pointer; font-size: 13px; }
button.light { background: #fff; color: #1f2329; }
.kv { display: inline-block; margin: 0 18px 6px 0; font-size: 13px; }
.kv b { font-size: 16px; }
ul.caveats { margin: 6px 0; padding-left: 20px; font-size: 13px; }
ul.caveats li { margin-bottom: 4px; }
.path { font-family: ui-monospace, monospace; font-size: 12px; }
"""


def page(title, body):
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{esc(title)} · 广告实验干扰分析</title><style>{CSS}</style></head>
<body><header>
<span class="title">广告实验干扰分析（合成数据）</span>
<a href="/">总览</a><a href="/reports">报告</a>
<a href="/methodology">方法说明</a>
</header><main>{body}</main></body></html>"""


def ci_bar(lo, hi, mid, vmin, vmax):
    span = (vmax - vmin) or 1e-12

    def pos(x):
        return max(0.0, min(100.0, (x - vmin) / span * 100))
    left, right = pos(lo), pos(hi)
    zero = ""
    if vmin < 0 < vmax:
        zero = f'<div class="zline" style="left:{pos(0):.2f}%"></div>'
    return (f'<div class="cibar-wrap">'
            f'<div class="cibar" style="left:{left:.2f}%;'
            f'width:{max(right - left, 0.4):.2f}%"></div>'
            f'<div class="cidot" style="left:{pos(mid):.2f}%"></div>'
            f'{zero}</div>')


# ---------------------------------------------------------------- 数据取用

def get_conn():
    return connect(DB_PATH)


def latest_done_report(conn, eid):
    return conn.execute(
        "SELECT * FROM reports WHERE experiment_id=? AND status='done'"
        " ORDER BY report_id DESC LIMIT 1", (eid,)).fetchone()


def report_context(conn, eid, report_id):
    if report_id:
        rep = conn.execute(
            "SELECT * FROM reports WHERE report_id=? AND experiment_id=?",
            (report_id, eid)).fetchone()
    else:
        rep = latest_done_report(conn, eid)
    if rep is None:
        return None, None, None
    sections = analysis.load_sections(conn, rep["report_id"])
    snap = conn.execute("SELECT * FROM snapshots WHERE snapshot_id=?",
                        (rep["snapshot_id"],)).fetchone()
    return rep, sections, snap


def report_banner(conn, rep, snap, eid):
    newest_snap = conn.execute(
        "SELECT * FROM snapshots WHERE experiment_id=?"
        " ORDER BY snapshot_id DESC LIMIT 1", (eid,)).fetchone()
    stale = ""
    if newest_snap and newest_snap["snapshot_id"] != rep["snapshot_id"]:
        stale = (
            f'<div class="banner warn">已有更新的输入版本 '
            f'snapshot #{newest_snap["snapshot_id"]}'
            f'（批次 ≤ {newest_snap["through_batch"]}）。'
            f'本报告保持原输入版本不变；如需按新输入重算，请创建新报告——'
            f'历史报告不会被覆盖。</div>')
    return (
        f'<div class="banner info">报告 #{rep["report_id"]} · '
        f'输入版本 snapshot #{rep["snapshot_id"]}'
        f'（批次 ≤ {snap["through_batch"]}）· seed={rep["seed"]} · '
        f'n_boot={rep["n_boot"]} · 代码 v{esc(rep["code_version"])} · '
        f'完成于 {esc(rep["finished_at"] or "-")}</div>') + stale


def arm_tables(sections):
    """按原分配 vs 实际接触，两张描述表并排。"""
    out = ['<div style="display:flex;gap:16px;flex-wrap:wrap">']
    for key, title in (("as_assigned", "按原分配统计（首次曝光归属）"),
                       ("as_treated", "实际接触统计（可重复计入两臂）")):
        blk = sections[key]
        rows = []
        for a in ("control", "treatment"):
            b = blk[a]
            rows.append([
                ARM_LABELS[a], f'{b["users"]:,}', f'{b["converted"]:,}',
                pct(b["conv_rate"]), yuan(b["gross_cents"]),
                yuan(b["refund_cents"]), yuan(b["net_cents"]),
                yuan(b["net_per_user_cents"]), pct(b["refund_rate"], 1),
                f'{b["contaminated_users"]:,}'])
        caveat = ""
        if key == "as_treated":
            caveat = f'<div class="muted">{esc(blk["_caveat"])}</div>'
        out.append(
            f'<div style="flex:1;min-width:480px"><h3>{title}</h3>'
            + table(["臂", "用户数", "转化人数", "转化率", "毛收入", "退款",
                     "净收入", "人均净收入", "退款率", "其中受污染"], rows)
            + caveat + "</div>")
    out.append("</div>")
    return "".join(out)


def method_view(sections, method):
    sec = sections[f"method:{method}"]
    eff = sec["effects"]
    rows = []
    for metric, label, fmt in (("conv_rate", "转化率差", pp),
                               ("net_per_user", "人均净收入差", syuan)):
        e = eff[metric]
        rows.append([label, fmt(e["diff"]), fmt(e["ci_lo"]),
                     fmt(e["ci_hi"]), fmt(e["se"]),
                     f'{e["p_approx"]:.4f}', f'{e["n_t"]}+{e["n_c"]}'])
    html_out = [f"<h3>{esc(sec['label'])}</h3>"]
    html_out.append(
        table(["指标（处理 − 对照）", "点估计", "CI 下界", "CI 上界", "SE",
               "p(近似)", "单位数(处+对)"], rows))
    if "interference" in sec:
        itf = sec["interference"]
        html_out.append(
            f'<div class="banner warn">组间干扰：混合聚类 '
            f'{itf["mixed_components"]} 个，覆盖 {itf["mixed_users"]:,} 用户'
            f'（{pct(itf["mixed_user_share"])}），'
            f'其中转化 {itf["mixed_conversions"]:,} 人次、'
            f'净收入 {yuan(itf["mixed_net_cents"])}。'
            f'这些单位被排除在聚类估计之外。</div>')
    html_out.append('<ul class="caveats">')
    for c in sec["caveats"]:
        html_out.append(f"<li>{esc(c)}</li>")
    html_out.append("</ul>")
    if method == "itt_user" and "sensitivity_excl_contaminated" in sec:
        s = sec["sensitivity_excl_contaminated"]
        html_out.append(
            f'<div class="banner bad">敏感性参考（非随机化比较）：'
            f'剔除受污染用户后转化率差 = {pp(s["conv_rate_diff"])}。'
            f'{esc(s["note"])}</div>')
    return "".join(html_out)


def uncertainty_compare(sections):
    """三种口径的不确定性并排比较（同一快照、同一 seed 族，可复现）。"""
    out = []
    for metric, label, fmt in (("conv_rate", "转化率差（处理−对照）", pp),
                               ("net_per_user", "人均净收入差（处理−对照）",
                                syuan)):
        entries = []
        for m in METHODS:
            e = sections[f"method:{m}"]["effects"][metric]
            entries.append((m, e))
        vmin = min(e["ci_lo"] for _, e in entries)
        vmax = max(e["ci_hi"] for _, e in entries)
        pad = (vmax - vmin) * 0.08 or 1e-9
        vmin, vmax = vmin - pad, vmax + pad
        rows = []
        for m, e in entries:
            rows.append([METHOD_LABELS[m],
                         f'{e["n_t"]}+{e["n_c"]}',
                         fmt(e["diff"]),
                         f'{fmt(e["ci_lo"])} ~ {fmt(e["ci_hi"])}',
                         ci_bar(e["ci_lo"], e["ci_hi"], e["diff"],
                                vmin, vmax)])
        out.append(f"<h3>{label}</h3>" + table(
            ["估计单位", "单位数(处+对)", "点估计", "95% CI", "图示"], rows))
    out.append('<div class="muted">同一输入快照、seed 固定：'
               '相同参数重算将得到逐位相同的结果（可复现）。'
               '用户级 CI 最窄但独立性最弱；聚类级独立性最强但样本量最小。</div>')
    return "".join(out)


def contamination_card(sections, eid, report_id):
    s = sections["summary"]
    p = sections["paths"]
    rows = [[esc(x["pattern"]), f'{x["n"]:,}'] for x in p["patterns"][:8]]
    return (
        f'<div class="kv">受污染用户 <b>{s["n_contaminated_users"]:,}</b>'
        f' / {s["n_users"]:,}（{pct(s["contamination_share"])}）</div>'
        f'<div class="kv">污染用户贡献转化 '
        f'<b>{pct(s["contaminated_conv_share"])}</b></div>'
        f'<div class="kv">污染用户贡献净收入 '
        f'<b>{pct(s["contaminated_net_share"])}</b></div>'
        f'<div class="kv">聚类：纯 {s["n_components_pure"]} / 混合 '
        f'{s["n_components_mixed"]} / 共 {s["n_components"]}</div>'
        f'<h3>常见跨组路径（C=对照 T=处理）</h3>'
        + table(["路径模式", "用户数"], rows)
        + f'<p><a href="/experiment/{esc(eid)}/paths?report_id={report_id}">'
          f'查看全部污染路径 →</a></p>')


# ---------------------------------------------------------------- 页面

def page_overview():
    conn = get_conn()
    body = ["<h1>总览</h1>"]
    exps = conn.execute("SELECT * FROM experiments ORDER BY created_at").fetchall()
    rows = []
    for e in exps:
        eid = e["experiment_id"]
        nb = conn.execute("SELECT COUNT(*) c FROM batches WHERE experiment_id=?",
                          (eid,)).fetchone()["c"]
        ns = conn.execute("SELECT COUNT(*) c FROM snapshots WHERE experiment_id=?",
                          (eid,)).fetchone()["c"]
        nr = conn.execute("SELECT COUNT(*) c FROM reports WHERE experiment_id=?",
                          (eid,)).fetchone()["c"]
        rows.append([f'<a href="/experiment/{esc(eid)}">{esc(eid)}</a>',
                     esc(e["name"]), nb, ns, nr])
    body.append('<div class="card"><h2>实验</h2>'
                + table(["实验 ID", "名称", "批次数", "快照数", "报告数"], rows)
                + "</div>")

    batches = conn.execute(
        "SELECT * FROM batches ORDER BY batch_id DESC LIMIT 20").fetchall()
    rows = []
    for b in batches:
        det = json.loads(b["detail"] or "{}")
        det_s = ", ".join(f"{k}={v}" for k, v in det.items() if v) or "-"
        rows.append([b["batch_id"], esc(b["experiment_id"]), esc(b["kind"]),
                     esc(b["source"]), b["n_rows"], b["n_accepted"],
                     b["n_rejected"], esc(det_s), esc(b["received_at"])])
    body.append('<div class="card"><h2>最近批次（追加式，不可变）</h2>'
                + table(["批次", "实验", "类型", "来源", "行数", "接受",
                         "拒绝", "数据质量", "入库时间"], rows) + "</div>")

    snaps = conn.execute(
        "SELECT * FROM snapshots ORDER BY snapshot_id DESC LIMIT 20").fetchall()
    rows = [[s["snapshot_id"], esc(s["experiment_id"]), s["through_batch"],
             esc(s["note"]), esc(s["created_at"])] for s in snaps]
    body.append('<div class="card"><h2>输入版本（快照）</h2>'
                + table(["快照", "实验", "批次 ≤", "备注", "创建时间"], rows)
                + "</div>")

    reps = conn.execute(
        "SELECT * FROM reports ORDER BY report_id DESC LIMIT 20").fetchall()
    rows = []
    for r in reps:
        rows.append([r["report_id"], esc(r["experiment_id"]), r["snapshot_id"],
                     r["seed"], r["n_boot"], esc(r["code_version"]),
                     esc(r["status"]), esc(r["error"] or "-"),
                     esc(r["created_at"])])
    body.append('<div class="card"><h2>最近报告</h2>'
                + table(["报告", "实验", "快照", "seed", "n_boot", "代码",
                         "状态", "错误", "创建时间"], rows)
                + '<p class="muted">后台工作进程：'
                  '<code>python -m app.worker --db ' + esc(DB_PATH) + '</code>'
                  '（或点下方“内联运行待办任务”）</p></div>')

    body.append(f"""
<div class="card"><h2>操作</h2>
<form class="inline" method="post" action="/action/demo">
  <button>一键演示：生成合成数据 → 入库 → 快照 → 报告 → 计算</button>
</form>
<form class="inline" method="post" action="/action/generate_synth">
  实验 <input name="experiment_id" value="EXP-SYNTH-01" size="12">
  种子 <input name="seed" value="20261116" size="9">
  <button class="light">生成合成数据并入库</button>
</form>
<form class="inline" method="post" action="/action/ingest">
  实验 <input name="experiment_id" size="12">
  文件 <input name="path" size="28" placeholder="data/synth/batch02_events.csv">
  <button class="light">入库批次文件</button>
</form>
<form class="inline" method="post" action="/action/snapshot">
  实验 <input name="experiment_id" size="12">
  备注 <input name="note" size="16">
  <button class="light">冻结输入版本（快照）</button>
</form>
<form class="inline" method="post" action="/action/report">
  实验 <input name="experiment_id" size="12">
  快照 <input name="snapshot_id" size="4">
  seed <input name="seed" value="42" size="4">
  n_boot <input name="n_boot" value="1000" size="5">
  <button class="light">创建报告（后台重算）</button>
</form>
<form class="inline" method="post" action="/action/run_jobs">
  <button class="light">内联运行待办任务</button>
</form>
</div>""")
    conn.close()
    return page("总览", "".join(body))


def page_experiment(eid, query):
    conn = get_conn()
    method = query.get("method", ["itt_user"])[0]
    if method not in METHODS:
        method = "itt_user"
    report_id = query.get("report_id", [None])[0]
    rep, sections, snap = report_context(conn, eid, report_id)
    if rep is None:
        body = (f"<h1>{esc(eid)}</h1><div class='card'>尚无已完成的报告。"
                f"请先在<a href='/'>总览</a>生成数据、冻结快照并创建报告。</div>")
        conn.close()
        return page(eid, body)

    rid = rep["report_id"]
    s = sections["summary"]
    dq = sections["dq"]
    body = [f"<h1>实验 {esc(eid)}</h1>"]
    body.append(report_banner(conn, rep, snap, eid))

    # 报告切换
    done = conn.execute(
        "SELECT report_id, snapshot_id FROM reports WHERE experiment_id=?"
        " AND status='done' ORDER BY report_id DESC", (eid,)).fetchall()
    links = " ".join(
        f'<a href="/experiment/{esc(eid)}?report_id={r["report_id"]}"'
        f' class="{"on" if r["report_id"] == rid else ""}"'
        f' style="padding:2px 8px;border:1px solid #c9cdd4;border-radius:4px;'
        f' margin-right:4px;text-decoration:none;'
        f'{"background:#1f2329;color:#fff;" if r["report_id"] == rid else ""}">'
        f'#{r["report_id"]}(s{r["snapshot_id"]})</a>' for r in done)
    body.append(f'<div class="card"><h2>报告版本</h2>{links} '
                f'<span class="muted">历史报告保持各自输入版本，不会被重算覆盖；'
                f'可去<a href="/reports">报告页</a>并排比较。</span></div>')

    # 数据质量
    dq_rows = [[k, f"{v:,}"] for k, v in sorted(dq.items()) if v]
    body.append('<div class="card"><h2>数据质量（本输入版本）</h2>'
                + (table(["项", "值"], dq_rows) if dq_rows else "无异常")
                + '<div class="muted">重复转化按 order_id 去重后计入；'
                  '无法匹配的退款不计入净收入，仅在此留痕。</div></div>')

    # 按原分配 vs 实际接触
    body.append('<div class="card"><h2>按原分配 vs 实际接触（描述性）</h2>'
                + arm_tables(sections) + "</div>")

    # 估计单位切换
    tabs = '<div class="tabs">' + "".join(
        f'<a class="{"on" if m == method else ""}" '
        f'href="/experiment/{esc(eid)}?report_id={rid}&method={m}">'
        f'{METHOD_LABELS[m]}</a>' for m in METHODS) + "</div>"
    body.append(f'<div class="card"><h2>效应估计（选择估计单位）</h2>{tabs}'
                + method_view(sections, method) + "</div>")

    # 不确定性比较
    body.append('<div class="card"><h2>不确定性比较（可复现）</h2>'
                + uncertainty_compare(sections) + "</div>")

    # 污染概览 + 下钻入口
    body.append(f'<div class="card"><h2>组间干扰</h2>'
                + contamination_card(sections, eid, rid))
    arm_links = " ".join(
        f'<a href="/experiment/{esc(eid)}/group?report_id={rid}&kind=arm&key={a}">'
        f'{ARM_LABELS[a]}贡献</a>' for a in ("control", "treatment"))
    body.append(f'<p>下钻：{arm_links} ｜ '
                f'<a href="/experiment/{esc(eid)}/group?report_id={rid}'
                f'&kind=cluster&key=list">聚类列表</a></p></div>')

    conn.close()
    return page(f"实验 {eid}", "".join(body))


def page_paths(eid, query):
    conn = get_conn()
    rep, sections, snap = report_context(conn, eid,
                                         query.get("report_id", [None])[0])
    if rep is None:
        conn.close()
        return page("污染路径", "<div class='card'>尚无报告。</div>")
    p = sections["paths"]
    body = [f"<h1>污染路径 · {esc(eid)}</h1>",
            report_banner(conn, rep, snap, eid)]
    rows = [[esc(x["pattern"]), f'{x["n"]:,}'] for x in p["patterns"]]
    body.append('<div class="card"><h2>路径模式（曝光臂序列，C=对照 T=处理）</h2>'
                + table(["模式", "用户数"], rows) + "</div>")
    rows = []
    for u in p["top_users"]:
        rows.append([esc(u["user"]), u["n_exposures"],
                     f'<span class="path">{esc(" → ".join(u["merchants"]))}</span>',
                     u["n_conv"], yuan(u["net_cents"])])
    body.append(
        f'<div class="card"><h2>受污染用户明细（按转化排序，前 '
        f'{len(p["top_users"])} / {p["n_contaminated"]:,}）</h2>'
        + table(["用户", "曝光数", "商户路径", "转化次数", "净收入"], rows)
        + "</div>")
    conn.close()
    return page("污染路径", "".join(body))


def page_group(eid, query):
    conn = get_conn()
    rep, sections, snap = report_context(conn, eid,
                                         query.get("report_id", [None])[0])
    if rep is None:
        conn.close()
        return page("组贡献", "<div class='card'>尚无报告。</div>")
    kind = query.get("kind", ["arm"])[0]
    key = query.get("key", ["treatment"])[0]
    rid = rep["report_id"]
    g = sections["groups"]
    body = [f"<h1>组贡献 · {esc(eid)}</h1>", report_banner(conn, rep, snap, eid)]

    if kind == "arm":
        rows_data = g["arms"].get(key, [])
        tot_net = sum(r["net_cents"] for r in rows_data) or 1
        tot_users = sum(r["n_users"] for r in rows_data) or 1
        rows = []
        for r in sorted(rows_data, key=lambda x: -x["net_cents"]):
            rows.append([esc(r["merchant"]), f'{r["n_users"]:,}',
                         f'{r["n_exp"]:,}', f'{r["n_conv"]:,}',
                         pct(r["conv_rate"]), yuan(r["gross_cents"]),
                         yuan(r["refund_cents"]), yuan(r["net_cents"]),
                         yuan(r["net_per_user_cents"]),
                         pct(r["net_cents"] / tot_net, 1),
                         pct(r["n_users"] / tot_users, 1)])
        body.append(
            f'<div class="card"><h2>{ARM_LABELS.get(key, key)} · '
            f'商户级贡献明细</h2>'
            + table(["商户", "用户", "曝光", "转化", "转化率", "毛收入", "退款",
                     "净收入", "人均净收入", "净收入占比", "用户占比"], rows)
            + '<div class="muted">商户级口径下每个商户等权；'
              '占比展示该商户对本臂总量指标的贡献。</div></div>')
    else:
        if key == "list":
            rows = []
            for c in g["clusters"]:
                rows.append([
                    f'<a href="/experiment/{esc(eid)}/group?report_id={rid}'
                    f'&kind=cluster&key={c["cid"]}">#{c["cid"]}</a>',
                    "纯" if c["pure"] else "<b>混合</b>",
                    esc(",".join(c["arms"])),
                    len(c["merchants"]), f'{c["n_users"]:,}',
                    f'{c["n_conv"]:,}', pct(c["conv_rate"]),
                    yuan(c["net_cents"])])
            body.append('<div class="card"><h2>曝光聚类列表</h2>'
                        + table(["聚类", "类型", "臂", "商户数", "用户", "转化",
                                 "转化率", "净收入"], rows)
                        + '<div class="muted">仅纯组聚类参与聚类级效应估计；'
                          '混合聚类即组间干扰的载体。</div></div>')
        else:
            cid = int(key)
            c = next((x for x in g["clusters"] if x["cid"] == cid), None)
            if c is None:
                body.append("<div class='card'>聚类不存在。</div>")
            else:
                mlinks = []
                for c2 in g["clusters"]:
                    mlinks.append(
                        f'<a href="/experiment/{esc(eid)}/group?report_id={rid}'
                        f'&kind=cluster&key={c2["cid"]}">#{c2["cid"]}</a>')
                body.append(
                    f'<div class="card"><h2>聚类 #{cid} '
                    f'（{"纯" if c["pure"] else "混合"} · '
                    f'{esc("/".join(c["arms"]))}）</h2>'
                    f'<div class="kv">商户 <b>{esc(", ".join(c["merchants"]))}</b></div>'
                    f'<div class="kv">用户 <b>{c["n_users"]:,}</b></div>'
                    f'<div class="kv">转化 <b>{c["n_conv"]:,}</b></div>'
                    f'<div class="kv">转化率 <b>{pct(c["conv_rate"])}</b></div>'
                    f'<div class="kv">净收入 <b>{yuan(c["net_cents"])}</b></div>'
                    f'<div class="kv">人均净收入 '
                    f'<b>{yuan(c["net_per_user_cents"])}</b></div>'
                    f'<p class="muted">切换聚类：{" ".join(mlinks)}</p>')
                rows = [[esc(u["user"]), u["n_conv"], yuan(u["net_cents"]),
                         "是" if u["contaminated"] else "否"]
                        for u in c["top_users"]]
                body.append("<h3>头部用户（按 |净收入|，前 10）</h3>"
                            + table(["用户", "转化次数", "净收入", "受污染"],
                                    rows) + "</div>")
    conn.close()
    return page("组贡献", "".join(body))


def page_reports():
    conn = get_conn()
    reps = conn.execute("SELECT * FROM reports ORDER BY report_id DESC").fetchall()
    rows = []
    for r in reps:
        link = (f'<a href="/experiment/{esc(r["experiment_id"])}'
                f'?report_id={r["report_id"]}">查看</a>'
                if r["status"] == "done" else "-")
        rows.append([r["report_id"], esc(r["experiment_id"]), r["snapshot_id"],
                     r["seed"], r["n_boot"], esc(r["code_version"]),
                     esc(r["status"]), esc(r["finished_at"] or "-"), link])
    snaps = conn.execute("SELECT * FROM snapshots ORDER BY snapshot_id DESC"
                         " LIMIT 50").fetchall()
    snap_opts = "".join(
        f'<option value="{s["snapshot_id"]}">'
        f'#{s["snapshot_id"]} · {esc(s["experiment_id"])} · '
        f'批次≤{s["through_batch"]}</option>' for s in snaps)
    rep_opts = "".join(
        f'<option value="{r["report_id"]}">#{r["report_id"]} · '
        f'{esc(r["experiment_id"])} · s{r["snapshot_id"]} · '
        f'{esc(r["status"])}</option>' for r in reps if r["status"] == "done")
    body = ["<h1>报告</h1>",
            '<div class="card"><h2>全部报告（历史报告保持输入版本，永不改写）</h2>'
            + table(["报告", "实验", "快照", "seed", "n_boot", "代码", "状态",
                     "完成时间", ""], rows) + "</div>",
            f'''<div class="card"><h2>创建报告（后台重算，不动旧报告）</h2>
<form method="post" action="/action/report">
实验 <input name="experiment_id" size="12">
快照 <select name="snapshot_id">{snap_opts}</select>
seed <input name="seed" value="42" size="4">
n_boot <input name="n_boot" value="1000" size="5">
<button>创建</button></form>
<form method="post" action="/action/run_jobs" style="margin-top:6px">
<button class="light">内联运行待办任务</button>
<span class="muted">或由后台进程 python -m app.worker 持续处理</span></form></div>
<div class="card"><h2>比较两份报告</h2>
<form method="get" action="/compare">
A <select name="a">{rep_opts}</select>
B <select name="b">{rep_opts}</select>
<button>比较</button></form></div>''']
    conn.close()
    return page("报告", "".join(body))


def page_compare(query):
    a_id = query.get("a", [""])[0]
    b_id = query.get("b", [""])[0]
    conn = get_conn()
    ra = conn.execute("SELECT * FROM reports WHERE report_id=?", (a_id,)).fetchone()
    rb = conn.execute("SELECT * FROM reports WHERE report_id=?", (b_id,)).fetchone()
    if not ra or not rb:
        conn.close()
        return page("比较", "<div class='card'>请选择两份报告。</div>")
    sa = analysis.load_sections(conn, ra["report_id"])
    sb = analysis.load_sections(conn, rb["report_id"])
    body = ["<h1>报告比较</h1>"]
    meta_rows = []
    for label, r in (("A", ra), ("B", rb)):
        meta_rows.append([label, r["report_id"], esc(r["experiment_id"]),
                          r["snapshot_id"], r["seed"], r["n_boot"],
                          esc(r["code_version"]), esc(r["finished_at"] or "-")])
    body.append('<div class="card"><h2>版本信息</h2>'
                + table(["", "报告", "实验", "快照", "seed", "n_boot", "代码",
                         "完成时间"], meta_rows))
    if ra["snapshot_id"] != rb["snapshot_id"]:
        body.append('<div class="banner warn">两份报告的<b>输入版本不同</b>'
                    f'（snapshot #{ra["snapshot_id"]} vs #{rb["snapshot_id"]}）：'
                    '数值差异可能来自身份归并 / 曝光修订 / 新增批次，'
                    '而非方法或参数。比较结论时请先对齐输入版本。</div>')
    body.append("</div>")

    for m in METHODS:
        ka, kb = f"method:{m}", f"method:{m}"
        if ka not in sa or kb not in sb:
            continue
        rows = []
        for metric, label, fmt in (("conv_rate", "转化率差", pp),
                                   ("net_per_user", "人均净收入差", syuan)):
            ea = sa[ka]["effects"][metric]
            eb = sb[kb]["effects"][metric]
            rows.append([label, fmt(ea["diff"]),
                         f'{fmt(ea["ci_lo"])} ~ {fmt(ea["ci_hi"])}',
                         fmt(eb["diff"]),
                         f'{fmt(eb["ci_lo"])} ~ {fmt(eb["ci_hi"])}',
                         fmt(eb["diff"] - ea["diff"])])
        body.append(f'<div class="card"><h2>{METHOD_LABELS[m]}</h2>'
                    + table(["指标", "A 点估计", "A 95% CI", "B 点估计",
                             "B 95% CI", "B−A"], rows) + "</div>")

    sum_rows = []
    for key, label in (("n_users", "用户数"),
                       ("n_contaminated_users", "受污染用户"),
                       ("n_orders_valid", "有效订单"),
                       ("n_components", "聚类数"),
                       ("n_components_mixed", "混合聚类")):
        sum_rows.append([label, f'{sa["summary"][key]:,}',
                         f'{sb["summary"][key]:,}',
                         f'{sb["summary"][key] - sa["summary"][key]:+,}'])
    body.append('<div class="card"><h2>输入规模差异</h2>'
                + table(["项", "A", "B", "B−A"], sum_rows) + "</div>")
    conn.close()
    return page("比较", "".join(body))


def page_methodology():
    body = """
<h1>方法说明</h1>
<div class="card"><h2>问题</h2>
<p>广告实验按<b>商户</b>随机分臂，但用户会跨商户访问：同一用户可能既看到处理组
商户的广告、又看到对照组商户的广告（组间干扰 / 污染）。曝光、转化、退款分批
到达，还会出现重复转化、身份归并、曝光归因修订。在这种数据上，
<b>删掉受污染用户再宣称"随机实验结论"是不成立的</b>——删除依赖于用户
试验后的行为，破坏了随机化给出的可比性。</p></div>

<div class="card"><h2>估计单位的取舍（样本量 vs 独立性）</h2>
<table><thead><tr><th>口径</th><th>单位</th><th>独立性</th><th>样本量</th>
<th>解读</th></tr></thead><tbody>
<tr><td>用户级 ITT</td><td>用户（按首次曝光归属）</td>
<td>弱：同商户用户相关、跨组用户同时接触两臂</td><td>最大（数千）</td>
<td>"按分配意图"的效果；跨组接触使其向零衰减；受污染用户保留并标记</td></tr>
<tr><td>商户级</td><td>商户（随机化单位）</td>
<td>强：商户间无分配相关</td><td>小（数十）</td>
<td>检验效能低、CI 宽，但无群内相关偏倚</td></tr>
<tr><td>聚类级</td><td>曝光图连通分量</td>
<td>最强：分量间无曝光边</td><td>最小（十余），且仅纯组聚类参与估计</td>
<td>只外推到无干扰子总体；混合聚类的体量即干扰规模</td></tr>
<tr><td>实际接触</td><td>用户（按实际曝光）</td>
<td>—（非随机化）</td><td>大</td>
<td>描述性；自选偏差，不作因果结论</td></tr>
</tbody></table>
<p>分析页同时给出四种口径与并排的不确定性区间：用户级 CI 最窄但独立性假设
最弱，聚类级相反。没有唯一的"正确"口径——报告展示的是取舍本身。</p></div>

<div class="card"><h2>版本与可复现</h2>
<ul class="caveats">
<li>输入追加式入库：批次带 sha256，重复文件拒绝；事件按 event_uid 幂等。</li>
<li>快照 = 输入版本（批次 ≤ K）。身份归并、曝光修订、新批次只产生新快照。</li>
<li>报告绑定 (快照, seed, n_boot, 代码版本)；后台 worker 只读自己快照的输入，
新旧分组不会混入同一报告；历史报告永不改写。</li>
<li>不确定性：双臂分别重抽样的 percentile bootstrap；随机数由
seed+快照+口径 派生（sha512），同参数重算逐位一致。</li>
<li>重复转化按 order_id 去重（保留首条）；无法匹配的退款不计入净收入，
仅在数据质量留痕。</li></ul></div>

<div class="card"><h2>免责声明</h2>
<p>全部数据为合成生成（处理臂转化概率相对 +25% 的构造），仅用于演示分析
方法，<b>不宣称任何真实投放效果</b>。</p></div>
"""
    return page("方法说明", body)


# ---------------------------------------------------------------- 动作

def act(form):
    action = form.pop("_action")
    if action == "demo":
        eid = form.get("experiment_id", "EXP-SYNTH-01")
        seed = int(form.get("seed", "20261116"))
        from . import demo
        demo.run(DB_PATH, SYNTH_DIR, eid, seed, log=lambda *a: None)
        return f"/experiment/{eid}"
    if action == "generate_synth":
        eid = form.get("experiment_id", "EXP-SYNTH-01")
        seed = int(form.get("seed", "20261116"))
        synth.generate(SYNTH_DIR, eid, seed)
        init_db(DB_PATH)
        ingest.ingest_dir(DB_PATH, SYNTH_DIR, eid)
        return "/"
    if action == "ingest":
        init_db(DB_PATH)
        path = form["path"]
        if os.path.isdir(path):
            ingest.ingest_dir(DB_PATH, path, form["experiment_id"])
        else:
            ingest.ingest_file(DB_PATH, path, form["experiment_id"])
        return "/"
    if action == "snapshot":
        init_db(DB_PATH)
        worker.create_snapshot(DB_PATH, form["experiment_id"],
                               form.get("note", ""))
        return "/"
    if action == "report":
        init_db(DB_PATH)
        worker.queue_report(DB_PATH, form["experiment_id"],
                            int(form["snapshot_id"]),
                            seed=int(form.get("seed", "42")),
                            n_boot=int(form.get("n_boot", "1000")))
        return "/reports"
    if action == "run_jobs":
        init_db(DB_PATH)
        worker.drain(DB_PATH, log=lambda *a: None)
        return "/reports"
    return "/"


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "AdExpHTTP/1.0"

    def _send(self, body, status=200, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, loc):
        self.send_response(303)
        self.send_header("Location", loc)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        try:
            url = urllib.parse.urlsplit(self.path)
            path = url.path
            q = urllib.parse.parse_qs(url.query)
            if path == "/":
                return self._send(page_overview())
            if path == "/reports":
                return self._send(page_reports())
            if path == "/compare":
                return self._send(page_compare(q))
            if path == "/methodology":
                return self._send(page_methodology())
            m = re.fullmatch(r"/experiment/([^/]+)", path)
            if m:
                return self._send(page_experiment(
                    urllib.parse.unquote(m.group(1)), q))
            m = re.fullmatch(r"/experiment/([^/]+)/paths", path)
            if m:
                return self._send(page_paths(
                    urllib.parse.unquote(m.group(1)), q))
            m = re.fullmatch(r"/experiment/([^/]+)/group", path)
            if m:
                return self._send(page_group(
                    urllib.parse.unquote(m.group(1)), q))
            self._send(page("404", "<div class='card'>404</div>"), 404)
        except Exception as exc:
            self._send(page("错误", f"<div class='card'><pre>{esc(exc)}"
                                    f"</pre></div>"), 500)

    def do_POST(self):
        try:
            m = re.fullmatch(r"/action/(\w+)", urllib.parse.urlsplit(self.path).path)
            if not m:
                return self._send(page("404", "<div class='card'>404</div>"), 404)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8")
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            form["_action"] = m.group(1)
            loc = act(form)
            self._redirect(loc)
        except Exception as exc:
            self._send(page("错误", f"<div class='card'><pre>{esc(exc)}"
                                    f"</pre></div>"), 500)


def main():
    global DB_PATH
    import argparse
    p = argparse.ArgumentParser(description="Web 服务")
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    DB_PATH = args.db
    init_db(DB_PATH)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"监听 http://{args.host}:{args.port}  db={DB_PATH}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
