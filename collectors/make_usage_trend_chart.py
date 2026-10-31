#!/usr/bin/env python3
"""token 用量趋势图生成器:多系列折线图(2026-10-25 重做,参考 Codex 用量仪表盘)。

用法:
  python3 make_usage_trend_chart.py --days 10                 # 生成 HTML(默认)
  python3 make_usage_trend_chart.py --days 10 --png           # 另出 PNG(Chrome headless)
  python3 make_usage_trend_chart.py --month 2026-10 --png     # 整月(归档)

版式(2026-10-25 定稿,参考 Codex dashboard):
  - 表头单行 flex:左 = `查询起 → 查询止 模型用量统计`,右 = `Token 总用量 <b>数</b>`
  - 右上胶囊分段切换器(仅 HTML 交互):近7天/近30天 × 按模型/按工具
  - 多系列直段折线(2.5px round),无面积填充;悬浮 = 竖参考线 + 白描边圆点
    + 白卡 Tooltip(标题=当日合计,分隔线,色点行 名→值 右对齐)
  - X 轴只标 首/中/尾 三档(≤10 天全标);Y 轴仅水平网格线无轴线
  - 图例居中,11px 圆点,点按切换隐藏;默认显示 Top6 模型(工具全显)

数据来源:
  - 已归档日期: NAS 月归档 agent usage/YYYY-MM.json 的 days[].models / tools
    (canon 口径,含 mini 双机;归档边界 = 昨日 23:59)
  - 今天: 实时扫描各源(与 model_usage_rank.py 同解析逻辑,import 复用)
  - 归档缺失(如 NAS 未挂载)的日期 → 跳过并在 stderr 报告,绝不补 0

说明:
  - HTML 默认透明背景 + app 主题变量(--foreground 等),供桌面 app 内联
    预览;`?theme=light|dark` 触发导出模式(固定配色 + 实底),独立浏览器
    打开/转 PNG 用(否则 fallback 浅灰字在白底上不可读)。
  - PNG 走本机 Chrome headless。
  - 纯 stdlib,只读,不调 LLM。
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import aiusd_config  # noqa: E402

ARCHIVE_DIR = os.path.join(
    aiusd_config.NAS_VOLUME or "/Volumes/UNSET", "Agent", "Program",
    "Agent token usage", "agent usage")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TRAEE = os.path.join(HERE, "trae_usage.py")
# Codex 风格高辨识调色板:蓝/橙/绿/粉/紫/青/金/红 打头(前 8 色相拉满),
# 后接长尾延展色(默认隐藏的系列被图例点开时才用得到)
PALETTE = ["#3D8BFD", "#EE8A3C", "#3E9B4F", "#E8639C", "#8C5CE8",
           "#00A6C8", "#D9A514", "#D64545", "#9AA0A6", "#5E6AD2",
           "#14B8A6", "#A0785C", "#7CB342", "#C247A8", "#64B5F6"]
ZERO_KEYS = {"trae 原生积分"}  # 0-token 计数键,不入图


def load_rank_module():
    """载入 model_usage_rank.py 复用各源解析逻辑(单一事实源,防漂移)。"""
    path = os.path.join(HERE, "model_usage_rank.py")
    spec = importlib.util.spec_from_file_location("model_usage_rank", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_workbuddy_module():
    """载入 workbuddy_usage.py 兄弟模块（projects jsonl 主源，与日报归档链路同源）。"""
    path = os.path.join(HERE, "workbuddy_usage.py")
    if not os.path.exists(path):
        print(f"[WorkBuddy] ⚠ 脚本未找到: {path}", file=sys.stderr)
        return None
    spec = importlib.util.spec_from_file_location("workbuddy_usage", path)
    if spec is None or spec.loader is None:
        print(f"[WorkBuddy] ⚠ 模块加载失败: {path}", file=sys.stderr)
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def live_trae(date_str):
    """Trae 单日采集(rank 扫描路无 Trae 源;归档日由卡片/回填覆盖,这里只管今天)。

    返回 (models, total, calls);失败返回 ({}, 0, 0) 并 stderr 告警,不静默。
    2026-10-26（评审 S3-05）：检查退出码 + 必需字段必须可解析，逗号数千分位
    正确解析——失败/格式漂移绝不能编码成普通 0。
    """
    try:
        r = subprocess.run([sys.executable, TRAEE, "--date", date_str],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f"[Trae] ⚠ 实时采集失败(rc={r.returncode},按 0 计,不静默): "
                  f"{(r.stderr or '').strip()[:200]}", file=sys.stderr)
            return {}, 0, 0
        out = r.stdout
        m_calls = re.search(r"API 轮次:\s*([\d,]+)", out)
        m_total = re.search(r"合计 tokens:\s*([\d,]+)", out)
        if not m_calls or not m_total:
            print("[Trae] ⚠ 实时采集失败(输出格式漂移,按 0 计,不静默)",
                  file=sys.stderr)
            return {}, 0, 0
        calls = int(m_calls.group(1).replace(",", ""))
        total = int(m_total.group(1).replace(",", ""))
        models, cur = {}, None
        for line in out.splitlines():
            mm = re.match(r"模型:\s*(.+)", line.strip())
            if mm:
                cur = mm.group(1).strip(); continue
            ms = re.match(r"小计:\s*([\d,]+)", line.strip())
            if ms and cur:
                models[cur] = int(ms.group(1).replace(",", "")); cur = None
        if total > 0 and not models:
            print("[Trae] ⚠ 今日有总量但无模型小计（partial，图示按工具计入、"
                  "标题总量按模型和会偏低）", file=sys.stderr)
        return {k.casefold(): v for k, v in models.items()}, total, calls
    except Exception as e:
        print(f"[Trae] ⚠ 实时采集失败(按 0 计,不静默): {e}", file=sys.stderr)
        return {}, 0, 0


def live_day(rank, date_str):
    """实时扫描单日(date_str, 本地日)的 (models, tools) 两个 {name: tokens}。

    复用 rank 模块的聚合器与解析函数,把窗口设成该日 00:00→次日 00:00。
    Trae 源 rank 没有,单独补采。
    """
    from datetime import datetime, timedelta
    d0 = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=rank.LOCAL_TZ)
    rank.WIN_START = d0
    rank.WIN_END = d0 + timedelta(days=1)
    rank.AGG.clear()
    rank.RANGE.clear()
    devnull = open(os.devnull, "w")
    real = sys.stdout
    sys.stdout = devnull  # 抑制 parser 的进度打印
    try:
        rank.hermes(); rank.codex(); rank.claude_code(); rank.zcode()
        # rank.workbuddy() 已弃用：traces 源与日报漂移（评审 S3-04），见下方 WorkBuddy 专用路
    finally:
        sys.stdout = real
        devnull.close()
    models, tools = {}, {}
    for model, a in rank.AGG.items():
        key = model.casefold()
        models[key] = models.get(key, 0) + a["total"]
        for tname, tv in a["tools"].items():
            tn = tname
            if tn == "Hermes":
                tn = "Hermes Agent"  # rank 内部工具名与归档口径对齐
            tools[tn] = tools.get(tn, 0) + tv["total"]
    # WorkBuddy 专用路（2026-10-26 评审 S3-04）：rank.workbuddy 扫 traces
    # ——已被日报 0929 切源弃用（trace 可能整份不落盘/记 0，且整段归
    # mtime 日），今日柱会与日报 JSONL 数据源漂移。改走
    # workbuddy_usage.day_model_totals（projects jsonl 逐调用，与归档
    # 同一事实源）。只读纯函数，不写台账。
    wb = load_workbuddy_module()
    if wb is not None:
        try:
            # S4-04：源不存在时 day_model_totals 会静默返回空统计——先查
            # 源可用性，缺失必须显式告警（与 CLI 的「采集失败」契约对齐），
            # 不能与真 0 同形。
            import glob as _glob
            if not _glob.glob(wb.PROJECTS_GLOB, recursive=True):
                print("[WorkBuddy] ⚠ 源不可用（projects jsonl 不存在，"
                      "今日按 0 计，不静默）", file=sys.stderr)
                wb_st = None
            else:
                wb_st = wb.day_model_totals(date_str)
        except Exception as e:
            print(f"[WorkBuddy] ⚠ 实时采集失败(按 0 计,不静默): {e}",
                  file=sys.stderr)
            wb_st = None
        if wb_st and wb_st.get("total"):
            for m, v in (wb_st.get("models") or {}).items():
                key = m.casefold()
                models[key] = models.get(key, 0) + v
            tools["WorkBuddy"] = tools.get("WorkBuddy", 0) + wb_st["total"]
    t_models, t_total, t_calls = live_trae(date_str)
    if t_total:
        for m, v in t_models.items():
            models[m] = models.get(m, 0) + v
        tools["Trae"] = tools.get("Trae", 0) + t_total
    return models, tools


def load_archive(month, dates):
    """从 NAS 月归档取逐日 (models, tools);返回 (data, missing, partial)。

    data[date] = {"models": {...}, "tools": {...}};error 工具条目跳过
    （计入 partial，S3-10：部分失败日不得伪装成完整日）。
    """
    data, missing, partial = {}, [], {}
    fp = os.path.join(ARCHIVE_DIR, f"{month}.json")
    try:
        doc = json.load(open(fp))
    except Exception as e:
        print(f"[归档] 读取失败 {fp}: {e}", file=sys.stderr)
        return data, list(dates), partial
    days = doc.get("days") or {}
    for d in dates:
        if d not in days:
            missing.append(d)
            continue
        m2, t2, errs = {}, {}, []
        for name, t in (days[d].get("models") or {}).items():
            if name.casefold() in ZERO_KEYS and not t:
                continue
            m2[name.casefold()] = m2.get(name.casefold(), 0) + (t or 0)
        for name, t in (days[d].get("tools") or {}).items():
            if not isinstance(t, dict):
                continue
            if "error" in t:
                errs.append(name)
                continue
            t2[name] = t2.get(name, 0) + (t.get("total") or 0)
        data[d] = {"models": m2, "tools": t2}
        if errs:
            partial[d] = errs
    return data, missing, partial


def build_days(month, ndays):
    """组装该月前 ndays 天的逐日数据;今天走实时扫描。

    返回 (dates, data, partial)——partial = {date: [error 工具名]}（S3-10）。
    """
    from datetime import datetime, timedelta
    today = datetime.now().strftime("%Y-%m-%d")
    dates = [(datetime.strptime(f"{month}-01", "%Y-%m-%d") + timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range(ndays)]
    arch_dates = [d for d in dates if d != today]
    data, missing, partial = load_archive(month, arch_dates)
    if today in dates:
        rank = load_rank_module()
        models, tools = live_day(rank, today)
        data[today] = {"models": models, "tools": tools}
    if missing:
        print(f"[警告] 归档缺失 {len(missing)} 天,已跳过: {', '.join(missing)}", file=sys.stderr)
    if partial:
        print(f"[警告] {len(partial)} 天含采集失败工具（按缺失绘制,非完整日）:", file=sys.stderr)
        for d in sorted(partial):
            print(f"  {d}: {', '.join(partial[d])}", file=sys.stderr)
    dates = [d for d in dates if d in data]
    return dates, data, partial


def build_ranges(dates, data, req_end=None):
    """生成查询窗口数据集。

    - "all" = 查询窗口本身（调用方 --month/--days 决定，导出 PNG 的默认口径）
    - "r7"  = 请求窗口末尾按**日历**算的最近 7 个自然日（S3-14/R2-06：
      锚点必须用请求窗口末日，不能用 dates[-1]——那是最后有数据日，尾部
      缺档会把窗口悄悄前移）。可用日为空 → 不提供 r7（不回退历史样本）。
    模型序/工具序按窗口总量降序。
    """
    ranges = {"all": _one_range(dates, data)}
    if len(dates) > 7:
        from datetime import datetime as _dt, timedelta as _td
        end = _dt.strptime(req_end or dates[-1], "%Y-%m-%d")
        win7 = [(end - _td(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
        have = set(dates)
        sel = [d for d in win7 if d in have]
        if sel:
            ranges["r7"] = _one_range(sel, data)
    return ranges


def _one_range(ds, data):
    models = sorted({m for d in ds for m in data[d]["models"]},
                    key=lambda m: -sum(data[d]["models"].get(m, 0) for d in ds))
    tools = sorted({t for d in ds for t in data[d]["tools"]},
                   key=lambda t: -sum(data[d]["tools"].get(t, 0) for d in ds))
    return {
        "dates": ds,
        "models": models,
        "tools": tools,
        "series_model": {m: [data[d]["models"].get(m, 0) for d in ds] for m in models},
        "series_tool": {t: [data[d]["tools"].get(t, 0) for d in ds] for t in tools},
        "totals": [sum(data[d]["models"].values()) for d in ds],
    }


def yi(n):
    return f"{n/1e8:.2f}亿" if n >= 1e8 else (f"{n/1e4:.0f}万" if n >= 1e4 else str(n))


def _jso(obj) -> str:
    """SEC-C-01：嵌入 <script> 的 JSON 必须转义 `</`——json.dumps 不转义
    `/`，模型名含 `</script>` 时 HTML 解析器提前终止脚本块注入任意标记。
    `<\\/` 是合法 JSON 转义且 JS 字符串语义等价。"""
    return json.dumps(obj).replace("</", "<\\/")


def render_html(ranges, default_range, palette_models, palette_tools,
                partial_dates=()):
    r0 = ranges[default_range]
    grand = sum(r0["totals"])

    export_hook = '''<script>
// 独立导出模式(?theme=light|dark):固化配色,不依赖 app 注入的主题变量
(function(){
  const t = new URLSearchParams(location.search).get("theme");
  if (!t) return;
  const LIGHT = {"--foreground":"#1f2328","--muted-foreground":"#5c6672","--border":"#d8dee6","--card":"#ffffff","--accent":"#2f6fed"};
  const DARK  = {"--foreground":"#e6e6e6","--muted-foreground":"#9aa0a6","--border":"#3a3f45","--card":"#1c1f23","--accent":"#63a4ff"};
  const M = t === "dark" ? DARK : LIGHT;
  const r = document.documentElement.style;
  for (const k in M) r.setProperty(k, M[k]);
  document.body.style.background = M["--card"];
  document.body.style.padding = "18px 22px 16px";
  const st = document.createElement("style");
  // 导出字号补偿:PNG 会被缩放显示,基础字号整体上调约一成半
  st.textContent = "body{font-size:15px} h3{font-size:16px} .stat{font-size:16px} " +
    ".stat b{font-size:23px} #legend{font-size:15px} #legend i{width:13px;height:13px}" +
    "text{font-size:13px} .seg{font-size:14px}";
  document.head.appendChild(st);
})();
</script>'''

    return f'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<style>
:root {{ color-scheme: light dark; }}
body {{ margin:0; padding:14px 16px 10px; color:var(--foreground, #ddd); font: 13px/1.5 -apple-system, "PingFang SC", sans-serif; background:transparent; }}
.head {{ display:grid; grid-template-columns:1fr auto 1fr; align-items:center; gap:16px; }}
.head h3 {{ margin:0; font-size:14px; text-align:center; }}
.hz {{ display:flex; }}
.stat {{ justify-self:end; font-size:14px; white-space:nowrap; font-variant-numeric:tabular-nums; }}
.stat b {{ font-size:19px; font-weight:650; }}
.bar {{ transition: opacity .15s; }}
.segrow {{ display:flex; justify-content:flex-end; gap:14px; margin:10px 0 2px; }}
.seg {{ display:inline-flex; background:var(--muted-foreground,#888); border-radius:10px; padding:3px; opacity:.92; }}
.seg button {{ border:0; background:transparent; color:var(--card,#fff); font:inherit; font-size:12px;
              padding:3px 12px; border-radius:8px; cursor:pointer; opacity:.75; }}
.seg button.on {{ background:var(--card,#fff); color:var(--foreground,#111); font-weight:600; opacity:1;
                  box-shadow:0 1px 3px rgba(0,0,0,.12); }}
#tip {{ position:fixed; pointer-events:none; background:var(--card,#fff); border:1px solid var(--border,#e5e7eb);
       border-radius:12px; padding:12px 14px; font-size:12px; display:none; z-index:9; min-width:190px;
       box-shadow:0 8px 24px rgba(16,24,40,.16); color:var(--foreground,#111); }}
#tip .tt-title {{ font-size:14px; font-weight:650; font-variant-numeric:tabular-nums; }}
#tip .tt-date {{ color:var(--muted-foreground,#888); margin-top:2px; }}
#tip .tt-div {{ height:1px; background:var(--border,#ededf2); margin:8px 0; }}
#tip .tt-row {{ display:flex; align-items:center; gap:7px; padding:2.5px 0; }}
#tip .tt-row i {{ width:10px; height:10px; border-radius:50%; flex:0 0 auto; }}
#tip .tt-row .n {{ color:var(--muted-foreground,#555); }}
#tip .tt-row .v {{ margin-left:auto; font-weight:600; font-variant-numeric:tabular-nums; }}
#tip .tt-row .p {{ color:var(--muted-foreground,#999); font-variant-numeric:tabular-nums; width:52px; text-align:right; }}
#legend {{ display:flex; flex-wrap:wrap; gap:8px 28px; margin-top:14px; font-size:13px; }}
#legend span {{ display:inline-flex; align-items:center; gap:6px; cursor:pointer; user-select:none;
                color:var(--muted-foreground,#aaa); transition:color .15s; }}
#legend span.on {{ color:var(--foreground,#ddd); }}
#legend i {{ width:11px; height:11px; border-radius:50%; display:inline-block; }}
#total {{ font-variant-numeric: tabular-nums; }}
line.grid {{ stroke:var(--border,#333); stroke-width:1; opacity:.6; }}
line.ref {{ stroke:var(--muted-foreground,#999); stroke-width:1; opacity:.45; }}
text {{ fill:var(--muted-foreground,#999); font-size:11.5px; font-variant-numeric:tabular-nums; }}
text.tt {{ fill:var(--foreground,#ddd); font-weight:600; }}
polyline.sr {{ fill:none; stroke-width:2.5; stroke-linecap:round; stroke-linejoin:round; }}
#ovl {{ cursor:crosshair; }}
</style>
</head>
<body>
<div class="head"><div class="hz" id="hz-left"></div><h3 id="rng-title">{r0["dates"][0]} → {r0["dates"][-1]} 模型用量统计</h3><span class="stat">Token 总用量 <b id="total">{yi(grand)}</b><span id="pnote" style="color:var(--muted-foreground,#888);font-size:12px"></span></span></div>
<div class="segrow">
  <span class="seg" id="seg-range"></span>
  <span class="seg" id="seg-mode"></span>
</div>
{export_hook}
<svg id="chart"></svg>
<div id="legend"></div>
<div id="tip"></div>
<script>
const RANGES = {_jso(ranges)};
const PARTIAL_DATES = {_jso(sorted(partial_dates))};  // S3-10/R2-05
const COLORS_M = {_jso(palette_models)};
const COLORS_T = {_jso(palette_tools)};
const YI = n => n >= 1e8 ? (n/1e8).toFixed(2)+"亿" : n >= 1e4 ? Math.round(n/1e4)+"万" : String(n);
const YIB = n => n >= 1e8 ? ((n/1e8).toFixed(1).replace(/\\.0$/,""))+"亿" : n >= 1e4 ? Math.round(n/1e4)+"万" : String(n);
const W = 980, H = 340, M = {{t:18, r:18, b:30, l:52}};
const TOP_N = 6;  // 默认显示的系列数(模型模式);其余图例点开,防 0 轴拥挤
// SEC-C-02：模型名进 innerHTML 前必须 HTML 转义（图例/tooltip/导出图例）
const esc = s => String(s).replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;"}})[c]);
const iw = W-M.l-M.r, ih = H-M.t-M.b;
let curRange = {_jso(default_range)}, curMode = "model";
let hidden = new Set(), hoverIdx = -1;

function applyDefaults() {{
  hidden = new Set();
  if (curMode === "model") names().slice(TOP_N).forEach(m => hidden.add(m));
}}

const svg = document.getElementById("chart");
svg.setAttribute("viewBox", `0 0 ${{W}} ${{H}}`);
svg.setAttribute("width", "100%");
const NS = "http://www.w3.org/2000/svg";
function el(tag, attrs) {{
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}}
const R = () => RANGES[curRange];
const series = () => curMode === "model" ? R().series_model : R().series_tool;
const names = () => curMode === "model" ? R().models : R().tools;
const colors = () => curMode === "model" ? COLORS_M : COLORS_T;
const DATES = () => R().dates;
const TOTALS = () => R().totals;
const step = () => iw / Math.max(1, DATES().length - 1);
const xAt = i => DATES().length === 1 ? M.l + iw/2 : M.l + step()*i;
const yMax = () => Math.max(...TOTALS(), 1) * 1.05;
const y = v => M.t + ih - v/yMax()*ih;
// nice 刻度:步长取 1/2/5×10^k,避免 6.0/11.9/17.9 这类碎刻度
const TICK = () => {{ const raw = yMax()/4; const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const nn = raw/mag; return (nn<=1?1:nn<=2?2:nn<=5?5:10)*mag; }};

function fmtY(v) {{ const e = (v/1e8).toFixed(1).replace(/\\.0$/,""); return v >= 1e8 ? e+"亿" : v >= 1e4 ? Math.round(v/1e4)+"万" : String(v); }}

function render() {{
  svg.innerHTML = "";
  hidden = new Set([...hidden].filter(m => names().includes(m)));
  const N = DATES().length;
  // Y 网格(仅水平线,无轴线,nice 步长)
  for (let v=0; v<=yMax()+1e-9; v+=TICK()) {{
    const yy = y(v);
    svg.appendChild(el("line", {{class:"grid", x1:M.l, y1:yy, x2:W-M.r, y2:yy}}));
    const t = el("text", {{x:M.l-8, y:yy+4, "text-anchor":"end"}});
    t.textContent = fmtY(v); svg.appendChild(t);
  }}
  // X 轴标签:≤10 天全标,否则 首/中/尾
  const idxs = N <= 10 ? [...Array(N).keys()] : [0, Math.round((N-1)/2), N-1];
  idxs.forEach(i => {{
    const t = el("text", {{x:xAt(i), y:H-8, "text-anchor": i===0?"start":(i===N-1?"end":"middle")}});
    t.textContent = DATES()[i].slice(5).replace("-", "/"); svg.appendChild(t);
  }});
  // 折线(直段,无填充)
  names().forEach(m => {{
    if (hidden.has(m)) return;
    const vals = series()[m];
    const pts = vals.map((v,i) => `${{xAt(i)}},${{y(v)}}`).join(" ");
    svg.appendChild(el("polyline", {{class:"sr", points:pts, stroke:colors()[m]}}));
    if (N === 1) {{
      // S3-15：单顶点 polyline 不可见——孤立样本画常驻圆点（不依赖 hover）
      const p = pts.split(" ")[0].split(",");
      svg.appendChild(el("circle", {{cx:+p[0], cy:+p[1], r:3.5, fill:colors()[m]}}));
    }}
  }});
  // 悬浮参考线 + 白描边圆点
  if (hoverIdx >= 0 && hoverIdx < N) {{
    svg.appendChild(el("line", {{class:"ref", x1:xAt(hoverIdx), y1:M.t, x2:xAt(hoverIdx), y2:M.t+ih}}));
    names().forEach(m => {{
      if (hidden.has(m)) return;
      const v = series()[m][hoverIdx];
      if (!v) return;
      svg.appendChild(el("circle", {{cx:xAt(hoverIdx), cy:y(v), r:5.5, fill:colors()[m],
        stroke:"var(--card,#fff)", "stroke-width":2.5}}));
    }});
  }}
  // 透明悬浮捕获层
  const ovl = el("rect", {{id:"ovl", x:M.l, y:M.t, width:iw, height:ih, fill:"transparent"}});
  ovl.addEventListener("mousemove", e => {{
    const box = svg.getBoundingClientRect();
    const px = (e.clientX - box.left) * (W / box.width);
    const i = DATES().length === 1 ? 0 : Math.round((px - M.l) / step());
    hoverIdx = Math.max(0, Math.min(DATES().length-1, i));
    render(); showTip(hoverIdx, e);
  }});
  ovl.addEventListener("mouseleave", () => {{ hoverIdx = -1; render(); document.getElementById("tip").style.display = "none"; }});
  svg.appendChild(ovl);
  renderLegend(); renderSegs(); renderHead();
}}

function renderHead() {{
  document.getElementById("rng-title").textContent = `${{DATES()[0]}} → ${{DATES()[DATES().length-1]}} 模型用量统计`;
  document.getElementById("total").textContent = YI(TOTALS().reduce((a,b)=>a+b,0));
  // S3-10/R2-05：不完整日标注按**当前窗口**重算（切 r7/导出都更新）
  const pd = PARTIAL_DATES.filter(d => DATES().includes(d));
  document.getElementById("pnote").textContent =
    pd.length ? `（${{pd.length}} 日不完整）` : "";
}}

function renderSegs() {{
  const sr = document.getElementById("seg-range"), sm = document.getElementById("seg-mode");
  sr.innerHTML = ""; sm.innerHTML = "";
  [["all","全部"],["r7","近7天"]].forEach(([k,label]) => {{
    if (!RANGES[k]) return;
    const b = document.createElement("button");
    b.textContent = label;
    b.className = curRange === k ? "on" : "";
    b.onclick = () => {{ curRange = k; applyDefaults(); hoverIdx = -1; render(); }};
    sr.appendChild(b);
  }});
  if (sr.children.length > 1) sr.style.display = "";
  else sr.style.display = "none";  // 查询窗口 ≤7 天:只有一个口径,切换器隐藏
  [["model","按模型"],["tool","按工具"]].forEach(([k,label]) => {{
    const b = document.createElement("button");
    b.textContent = label;
    b.className = curMode === k ? "on" : "";
    b.onclick = () => {{ curMode = k; applyDefaults(); hoverIdx = -1; render(); }};
    sm.appendChild(b);
  }});
}}

function renderLegend() {{
  const lg = document.getElementById("legend");
  lg.innerHTML = "";
  const order = names().slice().sort((a,b)=>series()[b].reduce((x,y)=>x+y,0)-series()[a].reduce((x,y)=>x+y,0));
  order.forEach(m => {{
    const tot = series()[m].reduce((x,y)=>x+y,0);
    const s = document.createElement("span");
    s.className = hidden.has(m) ? "" : "on";
    s.innerHTML = `<i style="background:${{colors()[m]}}"></i>${{esc(m)}} ${{YI(tot)}}`;
    s.onclick = () => {{ hidden.has(m) ? hidden.delete(m) : hidden.add(m); render(); }};
    lg.appendChild(s);
  }});
}}

const tipEl = document.getElementById("tip");
function showTip(di, e) {{
  const rows = names().filter(m => !hidden.has(m) && series()[m][di] > 0)
    .sort((a,b)=>series()[b][di]-series()[a][di]);
  const tot = TOTALS()[di];
  let html = `<div class="tt-title">${{YI(tot)}} tokens</div>`;
  const wd = "日一二三四五六"[new Date(DATES()[di]+"T12:00:00").getDay()];
  html += `<div class="tt-date">${{DATES()[di]}} 周${{wd}}</div><div class="tt-div"></div>`;
  rows.forEach(m => {{
    const v = series()[m][di];
    html += `<div class="tt-row"><i style="background:${{colors()[m]}}"></i><span class="n">${{esc(m)}}</span>` +
            `<span class="p">${{(v/tot*100).toFixed(1)}}%</span><span class="v">${{YI(v)}}</span></div>`;
  }});
  tipEl.innerHTML = html;
  tipEl.style.display = "block";
  const bw = tipEl.offsetWidth, bh = tipEl.offsetHeight;
  let left = e.clientX + 16;
  if (left + bw > innerWidth - 8) left = e.clientX - bw - 16;
  tipEl.style.left = left + "px";
  tipEl.style.top = Math.max(8, Math.min(e.clientY - bh/2, innerHeight - bh - 8)) + "px";
}}

// ---- 导出模式(?theme=):无交互 → 隐藏切换器,改堆叠柱显示每日总量 ----
function renderBars() {{
  svg.innerHTML = "";
  const N = DATES().length;
  for (let v=0; v<=yMax()+1e-9; v+=TICK()) {{
    const yy = y(v);
    svg.appendChild(el("line", {{class:"grid", x1:M.l, y1:yy, x2:W-M.r, y2:yy}}));
    const t = el("text", {{x:M.l-8, y:yy+4, "text-anchor":"end"}});
    t.textContent = fmtY(v); svg.appendChild(t);
  }}
  const bw = iw / N, barW = Math.min(bw*0.62, 58);
  const lstep = N <= 16 ? 1 : (N <= 26 ? 2 : 3);  // X 轴标签抽稀,防粘连
  DATES().forEach((dl, di) => {{
    let acc = 0;
    names().forEach(m => {{
      const v = series()[m][di]; if (!v) return;
      const y0 = y(acc), y1 = y(acc+v);
      svg.appendChild(el("rect", {{x:M.l + bw*di + (bw-barW)/2, y:y1, width:barW,
        height:Math.max(1, y0-y1-1), fill:colors()[m]}}));
      acc += v;
    }});
    if (di % lstep === 0 || di === N-1) {{
      const tx = el("text", {{x:M.l + bw*di + bw/2, y:H-8, "text-anchor":"middle"}});
      tx.textContent = dl.slice(5).replace("-", "/"); svg.appendChild(tx);
    }}
    const tt = el("text", {{class:"tt", x:M.l + bw*di + bw/2, y:y(TOTALS()[di])-6, "text-anchor":"middle"}});
    tt.textContent = YIB(TOTALS()[di]); svg.appendChild(tt);
  }});
  // 图例(静态,全系列,色块与柱对应)
  const lg = document.getElementById("legend");
  lg.innerHTML = "";
  names().slice().sort((a,b)=>series()[b].reduce((x,y)=>x+y,0)-series()[a].reduce((x,y)=>x+y,0))
    .forEach(m => {{
      const tot = series()[m].reduce((x,y)=>x+y,0);
      const s = document.createElement("span");
      s.className = "on";
      s.innerHTML = `<i style="background:${{colors()[m]}};border-radius:3px"></i>${{esc(m)}} ${{YI(tot)}}`;
      lg.appendChild(s);
    }});
  renderHead();
}}

const EXPORT = new URLSearchParams(location.search).get("theme");
if (EXPORT) {{
  document.body.classList.add("export");
  document.querySelector(".segrow").style.display = "none";
  renderBars();
}} else {{
  applyDefaults();
  render();
}}
</script>
</body>
</html>'''


def export_png(html_path, png_path, height=None):
    """Chrome headless 截图为 PNG(导出模式:实底浅色)。"""
    if not os.path.exists(CHROME):
        print(f"[PNG] 未找到 Chrome,跳过: {CHROME}", file=sys.stderr)
        return False
    url = "file://" + os.path.abspath(html_path) + "?theme=light"
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--force-device-scale-factor=2", "--window-size=1020," + str(height or 620),
           f"--screenshot={os.path.abspath(png_path)}", url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    ok = os.path.exists(png_path) and os.path.getsize(png_path) > 5000
    if not ok:
        print(f"[PNG] 生成失败 rc={r.returncode}: {r.stderr[-300:]}", file=sys.stderr)
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--month", default=None, help="月份 YYYY-MM(默认当月)")
    ap.add_argument("--days", type=int, default=None, help="只取该月前 N 天(默认整月到今天)")
    ap.add_argument("--out", default=None, help="输出 HTML 路径(默认工作目录 <月>token用量趋势.html)")
    ap.add_argument("--png", action="store_true", help="另出 PNG(Chrome headless,导出配色)")
    ap.add_argument("--png-height", type=int, default=545, help="PNG 视口高度(默认 545)")
    ap.add_argument("--png-range", choices=["all", "r7"], default="all",
                    help="PNG 截取的窗口(默认 all=查询窗口本身,与请求口径一致)")
    args = ap.parse_args()

    from datetime import datetime, timedelta
    month = args.month or datetime.now().strftime("%Y-%m")
    if month == datetime.now().strftime("%Y-%m"):
        dim = datetime.now().day
    else:
        import calendar
        y, m = (int(x) for x in month.split("-"))
        dim = calendar.monthrange(y, m)[1]
    ndays = min(args.days or dim, dim)

    # 请求窗口末日（R2-06）：必须是请求口径的末日（含未来空档），不是最后
    # 有数据日——否则尾部缺档会把 r7 悄悄前移
    req_end = (datetime.strptime(f"{month}-01", "%Y-%m-%d")
               + timedelta(days=ndays - 1)).strftime("%Y-%m-%d")
    dates, data, partial = build_days(month, ndays)
    if not dates:
        print("无数据,退出", file=sys.stderr); return 1
    ranges = build_ranges(dates, data, req_end=req_end)
    # 调色板按整月总量降序稳定分配(两个窗口同模型同色)
    all_models = sorted({m for d in dates for m in data[d]["models"]},
                        key=lambda m: -sum(data[d]["models"].get(m, 0) for d in dates))
    all_tools = sorted({t for d in dates for t in data[d]["tools"]},
                       key=lambda t: -sum(data[d]["tools"].get(t, 0) for d in dates))
    palette_models = {m: PALETTE[i % len(PALETTE)] for i, m in enumerate(all_models)}
    palette_tools = {t: PALETTE[i % len(PALETTE)] for i, t in enumerate(all_tools)}
    default_range = args.png_range if args.png_range in ranges else "all"
    html = render_html(ranges, default_range, palette_models, palette_tools,
                       partial_dates=partial.keys())
    out = args.out or os.path.join(os.getcwd(), f"{month}token用量趋势.html")
    open(out, "w").write(html)
    grand = sum(sum(data[d]["models"].values()) for d in dates)
    print(f"HTML: {out}")
    print(f"日期数 {len(dates)} / 模型数 {len(all_models)} / 合计 {grand/1e8:.2f}亿 tokens")
    if args.png:
        png = os.path.splitext(out)[0] + ".png"
        if export_png(out, png, args.png_height):
            print(f"PNG: {png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
