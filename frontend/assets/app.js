/* 三角洲行动 · 子弹物价监控 —— 前端逻辑
 *
 * 设计取舍（对应开发文档 8.3 与 README）
 * ------------------------------------
 * 1. **零构建、零 CDN**：没有打包器、没有任何外部资源。图表是手写 SVG。
 *    理由：这是本机自用看板，断网也必须能看；引一个 1MB 的图表库只为画一条折线不值得。
 * 2. **指标一律由后端算**：前端不做任何数值计算，只负责展示与格式化。
 *    理由：同一个公式在前端和后端各写一遍，早晚会不一致，而且这种 bug 极难发现。
 * 3. **红涨绿跌**：沿用国内行情习惯（见 style.css 注释）。
 * 4. **数值缺失一律显示 "—"**，绝不显示 0。显示 0 会被误读成"价格是 0"或"涨跌为 0"。
 */

"use strict";

// ── 常量 ────────────────────────────────────────────────────────────────
const TIERS = ["白", "绿", "蓝", "紫", "金", "红"];
const POLL_MS = 30000;
const RANGE_OPTIONS = [
  { code: "1h", label: "1 小时" },
  { code: "6h", label: "6 小时" },
  { code: "24h", label: "24 小时" },
  { code: "7d", label: "7 天" },
  { code: "30d", label: "30 天" },
];
const RANK_TYPES = [
  { code: "up", label: "涨幅榜" },
  { code: "down", label: "跌幅榜" },
  { code: "volatility", label: "波动榜" },
  { code: "opportunity", label: "机会榜" },
  { code: "cheap", label: "低位榜" },
];

// ── 状态 ────────────────────────────────────────────────────────────────
const state = {
  view: "market",
  items: [],
  total: 0,
  updatedAt: null,
  stale: true,
  ageSeconds: null,
  q: "",
  tiers: new Set(),
  caliber: "",
  sort: "opportunity",
  order: "desc",
  onlyTradeable: true,
  rankType: "up",
  detailId: null,
  detailRange: "24h",
  // 当前已经画出来的曲线（"ammo_id|range"）。同一次打开抽屉里列表行与详情返回会各触发
  // 一次渲染，靠它避免为同一张图重复请求价格接口；关闭抽屉时清空，下次打开重新取数。
  detailChartKey: null,
  rules: [],
  logs: [],
  health: null,
  packs: null,
  notifyEnabled: false,
};

// ── 工具 ────────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);

function fmtNum(v, digits = 0) {
  // 关键：null / undefined 一律显示 "—"，绝不显示 0
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toLocaleString("zh-CN", {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

function fmtPct(v, withSign = true) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const s = withSign && v > 0 ? "+" : "";
  return s + Number(v).toFixed(2) + "%";
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", { hour12: false });
}

function fmtAge(sec) {
  if (sec === null || sec === undefined) return "无数据";
  if (sec < 60) return sec + " 秒前";
  if (sec < 3600) return Math.floor(sec / 60) + " 分钟前";
  if (sec < 86400) return Math.floor(sec / 3600) + " 小时前";
  return Math.floor(sec / 86400) + " 天前";
}

function cls(value, invert = false) {
  if (value === null || value === undefined || Number.isNaN(value)) return "flat";
  if (value > 0) return invert ? "down" : "up";
  if (value < 0) return invert ? "up" : "down";
  return "flat";
}

function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ── API ─────────────────────────────────────────────────────────────────
async function api(path, opts) {
  const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || JSON.stringify(j); } catch (e) { /* ignore */ }
    throw new Error(`${res.status} ${detail}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

const API = {
  health: () => api("/api/health"),
  packCheck: () => api("/api/health/pack-check"),
  ammo: (params) => api("/api/ammo?" + new URLSearchParams(params)),
  ammoOne: (id) => api(`/api/ammo/${encodeURIComponent(id)}`),
  price: (id, range) => api(`/api/price/${encodeURIComponent(id)}?range=${range}`),
  rank: (type) => api(`/api/rank?type=${type}&limit=15`),
  rules: () => api("/api/alerts/rules"),
  createRule: (body) => api("/api/alerts/rules", { method: "POST", body: JSON.stringify(body) }),
  toggleRule: (id) => api(`/api/alerts/rules/${id}/toggle`, { method: "POST" }),
  deleteRule: (id) => api(`/api/alerts/rules/${id}`, { method: "DELETE" }),
  logs: () => api("/api/alerts/log?limit=80"),
  pending: () => api("/api/alerts/pending?limit=20"),
  watchlist: () => api("/api/watchlist"),
  addWatch: (id) => api(`/api/watchlist/${encodeURIComponent(id)}`, { method: "POST" }),
  delWatch: (id) => api(`/api/watchlist/${encodeURIComponent(id)}`, { method: "DELETE" }),
  ingest: () => api("/api/ingest/trigger", { method: "POST" }),
};

// ── 手写 SVG 折线图 ─────────────────────────────────────────────────────
/**
 * 画一张折线图。刻意手写而不引图表库：见文件头注释。
 * points: [{ts, price}]；可选 hlines 画水平参考线（周内高/低）。
 */
function lineChart(points, opts = {}) {
  const W = 700, H = 260, PL = 58, PR = 14, PT = 14, PB = 34;
  if (!points || points.length === 0) {
    return `<div class="empty">该区间暂无数据。历史曲线会在首次启动回补或后续采集中逐步填充。</div>`;
  }
  const prices = points.map((p) => p.price);
  let min = Math.min(...prices), max = Math.max(...prices);
  if (min === max) { min -= 1; max += 1; }              // 全部同价时避免除零
  const pad = (max - min) * 0.08;
  min -= pad; max += pad;

  const n = points.length;
  const x = (i) => PL + (n === 1 ? (W - PL - PR) / 2 : (i * (W - PL - PR)) / (n - 1));
  const y = (v) => PT + (1 - (v - min) / (max - min)) * (H - PT - PB);

  const parts = [];
  // 水平网格与刻度
  for (let g = 0; g <= 4; g++) {
    const v = min + ((max - min) * g) / 4;
    const yy = y(v);
    parts.push(`<line x1="${PL}" y1="${yy.toFixed(1)}" x2="${W - PR}" y2="${yy.toFixed(1)}" stroke="#2b3a4f" stroke-width="1"/>`);
    parts.push(`<text x="${PL - 7}" y="${(yy + 4).toFixed(1)}" font-size="10" fill="#8494ad" text-anchor="end">${fmtNum(v)}</text>`);
  }
  // 参考线（7 日高/低）
  (opts.hlines || []).forEach((h) => {
    if (h.value < min || h.value > max) return;
    const yy = y(h.value);
    parts.push(`<line x1="${PL}" y1="${yy.toFixed(1)}" x2="${W - PR}" y2="${yy.toFixed(1)}" stroke="${h.color}" stroke-width="1" stroke-dasharray="4 3" opacity="0.8"/>`);
    parts.push(`<text x="${W - PR - 2}" y="${(yy - 3).toFixed(1)}" font-size="10" fill="${h.color}" text-anchor="end">${esc(h.label)}</text>`);
  });
  // 折线
  const d = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.price).toFixed(1)}`).join(" ");
  parts.push(`<path d="${d}" fill="none" stroke="#4a90d9" stroke-width="1.8" stroke-linejoin="round"/>`);
  // 末点
  const last = points[n - 1];
  parts.push(`<circle cx="${x(n - 1).toFixed(1)}" cy="${y(last.price).toFixed(1)}" r="3.4" fill="#4a90d9"/>`);
  // x 轴标签（首/中/末）
  [0, Math.floor(n / 2), n - 1].forEach((i) => {
    parts.push(`<text x="${x(i).toFixed(1)}" y="${H - 12}" font-size="10" fill="#8494ad" text-anchor="middle">${esc(fmtTime(points[i].ts).slice(5, 16))}</text>`);
  });
  // 悬停命中区（用原生 title，零 JS 依赖的 tooltip）
  points.forEach((p, i) => {
    const step = n > 1 ? (W - PL - PR) / (n - 1) : W;
    parts.push(`<rect x="${(x(i) - step / 2).toFixed(1)}" y="${PT}" width="${Math.max(step, 2).toFixed(1)}" height="${H - PT - PB}" fill="transparent"><title>${esc(fmtTime(p.ts))}　${fmtNum(p.price)}</title></rect>`);
  });

  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${parts.join("")}</svg>`;
}

// ── 渲染：顶部 ──────────────────────────────────────────────────────────
function renderFreshness() {
  const dot = $("freshDot"), txt = $("freshText");
  if (state.updatedAt === null) {
    dot.className = "dot bad"; txt.textContent = "尚未采集到数据";
  } else if (state.stale) {
    dot.className = "dot warn";
    txt.textContent = `数据已过期（${fmtAge(state.ageSeconds)}）`;
  } else {
    dot.className = "dot ok";
    txt.textContent = `数据正常 · ${fmtAge(state.ageSeconds)}更新`;
  }
}

function renderBanner() {
  const b = $("banner");
  const h = state.health;
  if (!h) { b.hidden = true; return; }
  const sched = h.scheduler || {};
  if (h.status === "down") {
    b.className = "banner bad"; b.hidden = false;
    b.textContent = "⚠ " + h.message;
  } else if (sched.consecutive_failures >= 3) {
    b.className = "banner bad"; b.hidden = false;
    b.textContent = `⚠ 数据源已连续 ${sched.consecutive_failures} 次采集失败。最后错误：` +
      (sched.last_summary || "—");
  } else if (h.data_freshness && h.data_freshness.stale) {
    b.className = "banner warn"; b.hidden = false;
    b.textContent = "⚠ " + h.message;
  } else {
    b.className = "banner ok"; b.hidden = false;
    b.textContent = `✓ ${h.message}` +
      (sched.backfill_done ? " 历史曲线已回补。" : " 历史曲线正在回补中…");
  }
}

function renderKpis() {
  const rows = state.items;
  const priced = rows.filter((r) => r.price !== null);
  const up = priced.filter((r) => (r.today_change_pct || 0) > 0).length;
  const down = priced.filter((r) => (r.today_change_pct || 0) < 0).length;
  const cheap = rows.filter((r) => r.percentile_7d !== null && r.percentile_7d < 0.35).length;
  const reliable = rows.filter((r) => r.percentile_reliable).length;

  const cards = [
    { k: "有价物品", v: fmtNum(priced.length), s: `共 ${fmtNum(state.total)} 个可交易物品` },
    { k: "今日上涨 / 下跌", v: `${up} / ${down}`, s: "红涨绿跌" },
    { k: "处于低位", v: fmtNum(cheap), s: "7 日分位 < 35%" },
    { k: "分位数可信", v: `${reliable}`, s: "样本 ≥ 24 条" },
    { k: "数据时间", v: fmtAge(state.ageSeconds), s: fmtTime(state.updatedAt) },
  ];
  $("kpis").innerHTML = cards.map((c) => `
    <div class="kpi"><div class="k">${esc(c.k)}</div>
      <div class="v">${c.v}</div><div class="s">${esc(c.s)}</div></div>`).join("");
}

// ── 渲染：表格 ──────────────────────────────────────────────────────────
function renderTable() {
  const tb = $("tbody");
  if (!state.items.length) {
    tb.innerHTML = `<tr><td colspan="11"><div class="empty">
      没有符合条件的物品。若刚启动服务，请点击右上角「刷新数据」触发首次采集。</div></td></tr>`;
    $("tableCount").textContent = "";
    return;
  }
  $("tableCount").textContent = `显示 ${state.items.length} / ${state.total} 个`;

  tb.innerHTML = state.items.map((r) => {
    const staleMark = (r.price_age_seconds && r.price_age_seconds > 3600)
      ? `<span class="stale-mark" title="价格已 ${fmtAge(r.price_age_seconds)}未更新">旧</span>` : "";
    const pct = r.percentile_7d;
    const bar = pct === null ? "—" : `
      <span class="bar"><i style="width:${Math.round(pct * 100)}%"></i></span>
      <span class="flat" title="${esc(r.percentile_label)}（样本 ${r.percentile_samples_7d} 条）">${(pct * 100).toFixed(0)}%</span>`;
    const range = (r.low_7d === null || r.high_7d === null)
      ? "—" : `${fmtNum(r.low_7d)} – ${fmtNum(r.high_7d)}`;
    const score = r.opportunity_score === null
      ? `<span class="flat" title="分位数样本不足，无法给出可信评分">数据不足</span>`
      : fmtNum(r.opportunity_score, 0);

    return `<tr data-id="${esc(r.ammo_id)}">
      <td class="star-col"><span class="star ${r.in_watchlist ? "on" : ""}"
        data-star="${esc(r.ammo_id)}" title="加入/移出自选">${r.in_watchlist ? "★" : "☆"}</span></td>
      <td class="name-cell">${esc(r.name)}${staleMark}</td>
      <td class="flat">${esc(r.caliber || "—")}</td>
      <td><span class="tier tier-${esc(r.tier || "白")}">${esc(r.tier || "?")}</span></td>
      <td class="num">${fmtNum(r.price)}</td>
      <td class="num ${cls(r.today_change_pct)}">${fmtPct(r.today_change_pct)}</td>
      <td class="num ${cls(r.day7_change_pct)}">${fmtPct(r.day7_change_pct)}</td>
      <td class="num flat">${range}</td>
      <td class="num">${bar}</td>
      <td class="num">${score}</td>
      <td class="act-col"><button class="btn tiny" data-alert="${esc(r.ammo_id)}"
        data-name="${esc(r.name)}" data-price="${r.price === null ? "" : r.price}"
        title="为该物品快速建规则">告警</button></td>
    </tr>`;
  }).join("");
}

// ── 渲染：排行榜 ────────────────────────────────────────────────────────
async function renderRank() {
  $("rankChips").innerHTML = RANK_TYPES.map((t) =>
    `<button class="chip ${t.code === state.rankType ? "on" : ""}" data-rank="${t.code}">${esc(t.label)}</button>`
  ).join("");
  $("rankList").innerHTML = `<div class="empty">加载中…</div>`;
  try {
    const data = await API.rank(state.rankType);
    $("rankNote").textContent = data.note || "";
    if (!data.items.length) {
      $("rankList").innerHTML = `<div class="empty">暂无数据。历史曲线回补完成后才会有排名。</div>`;
      return;
    }
    const isPct = ["up", "down"].includes(state.rankType);
    $("rankList").innerHTML = data.items.map((it, i) => {
      const val = isPct ? fmtPct(it.value) : fmtNum(it.value, state.rankType === "cheap" ? 3 : 0);
      const c = isPct ? cls(it.value) : "flat";
      return `<div class="rank-row" data-id="${esc(it.ammo_id)}">
        <span class="rank-idx">${i + 1}</span>
        <span class="rank-name">${esc(it.name)}
          <span class="flat" style="font-size:11px"> ${esc(it.caliber || "")}</span></span>
        <span class="rank-val ${c}">${val}</span>
      </div>`;
    }).join("");
  } catch (e) {
    $("rankList").innerHTML = `<div class="err">排行榜加载失败：${esc(e.message)}</div>`;
  }
}

// ── 渲染：自选 ──────────────────────────────────────────────────────────
function renderWatch() {
  const rows = state.items.filter((r) => r.in_watchlist);
  $("watchCount").textContent = `共 ${rows.length} 个`;
  if (!rows.length) {
    $("watchBody").innerHTML = `<div class="empty">
      还没有自选。在「行情总览」里点名称左侧的 ☆ 即可加入。</div>`;
    return;
  }
  $("watchBody").innerHTML = `<table><thead><tr>
      <th>名称</th><th>口径</th><th class="num">当前价</th><th class="num">今日</th>
      <th class="num">7 日</th><th class="num">分位</th><th class="num">机会分</th><th></th>
    </tr></thead><tbody>` + rows.map((r) => `<tr data-id="${esc(r.ammo_id)}">
      <td class="name-cell">${esc(r.name)}</td>
      <td class="flat">${esc(r.caliber || "—")}</td>
      <td class="num">${fmtNum(r.price)}</td>
      <td class="num ${cls(r.today_change_pct)}">${fmtPct(r.today_change_pct)}</td>
      <td class="num ${cls(r.day7_change_pct)}">${fmtPct(r.day7_change_pct)}</td>
      <td class="num">${r.percentile_7d === null ? "—" : (r.percentile_7d * 100).toFixed(0) + "%"}</td>
      <td class="num">${r.opportunity_score === null ? "数据不足" : fmtNum(r.opportunity_score, 0)}</td>
      <td class="act-col"><span class="star on" data-star="${esc(r.ammo_id)}" title="移出自选">★</span></td>
    </tr>`).join("") + `</tbody></table>`;
}

// ── 渲染：告警 ──────────────────────────────────────────────────────────
function ruleLabel(type) {
  return {
    price_below: "价格 ≤", price_above: "价格 ≥",
    pct_up: "涨幅 ≥", pct_down: "跌幅 ≥",
    percentile_below: "7 日分位 ≤", vol_spike: "价格异动 |z| ≥",
  }[type] || type;
}

function renderRules() {
  if (!state.rules.length) {
    $("ruleBody").innerHTML = `<div class="empty">
      还没有任何告警规则。用下面的表单建一条，例如「价格 ≤ 200」来等抄底机会。</div>`;
    return;
  }
  const names = {};
  state.items.forEach((i) => { names[i.ammo_id] = i.name; });
  $("ruleBody").innerHTML = state.rules.map((r) => `
    <div class="rule-row">
      <div class="rule-main">
        <div>${esc(ruleLabel(r.rule_type))} <b>${esc(String(r.threshold))}</b>${
          r.rule_type === "percentile_below" ? "（0~1 小数）" : ""}
          <span class="flat">· ${esc(r.ammo_id ? (names[r.ammo_id] || r.ammo_id) : "全部可交易物品")}</span>
        </div>
        <div class="rule-sub">
          冷却 ${r.cooldown_min} 分钟 · 持续确认 ${r.confirm_min} 分钟 ·
          静默 ${esc(r.quiet_hours || "无")} ·
          ${r.last_triggered_at ? "上次触发 " + fmtTime(r.last_triggered_at) : "尚未触发"}
        </div>
      </div>
      <span class="badge ${r.enabled ? "on" : "off"}">${r.enabled ? "启用" : "停用"}</span>
      <button class="btn tiny" data-toggle="${r.rule_id}">${r.enabled ? "停用" : "启用"}</button>
      <button class="btn tiny" data-drop="${r.rule_id}">删除</button>
    </div>`).join("");
}

function renderLogs() {
  $("logCount").textContent = `共 ${state.logs.length} 条`;
  if (!state.logs.length) {
    $("logBody").innerHTML = `<div class="empty">还没有触发记录。</div>`;
    return;
  }
  $("logBody").innerHTML = state.logs.map((l) => `
    <div class="log-row">
      <div>${esc(l.ammo_name || l.ammo_id || "—")}
        <span class="flat"> ${l.price === null ? "" : fmtNum(l.price)}</span></div>
      <div class="rule-sub">${esc(l.reason || "")}</div>
      <div class="rule-sub">${fmtTime(l.triggered_at)}</div>
    </div>`).join("");
}

// ── 渲染：数据健康 ──────────────────────────────────────────────────────
function renderHealth() {
  const h = state.health;
  if (!h) { $("healthBody").innerHTML = `<div class="empty">加载中…</div>`; return; }
  $("healthTime").textContent = "刷新于 " + fmtTime(h.now_ts);

  const sched = h.scheduler || {};
  const cards = [
    ["总体状态", { ok: "正常", degraded: "降级", down: "不可用" }[h.status] || h.status],
    ["数据新鲜度", fmtAge(h.data_freshness.age_seconds)],
    ["物品总数 / 已启用", `${h.ammo_counts.total} / ${h.ammo_counts.enabled}`],
    ["价格点总数", fmtNum(h.tick_counts.total)],
    ["其中判异（不进曲线）", fmtNum(h.tick_counts.suspect)],
    ["采集间隔", (h.config.poll_interval_seconds / 60) + " 分钟"],
    ["过期阈值", h.config.stale_after_minutes + " 分钟"],
    ["明细保留", h.config.tick_retention_days + " 天"],
    ["跳变判异阈值", "±" + h.config.suspect_jump_pct + "%（仅相邻采集间）"],
    ["连续失败次数", String(sched.consecutive_failures ?? 0)],
    ["上次采集", sched.last_run_ts ? fmtTime(sched.last_run_ts) : "—"],
    ["上次结果", sched.last_ok === null || sched.last_ok === undefined ? "—" : (sched.last_ok ? "成功" : "失败")],
    ["历史回补", sched.backfill_done ? "已完成" : "进行中/未开始"],
    ["数据源", h.config.source_search_url],
    ["数据库", h.config.db_path],
  ];
  let html = `<div class="health-grid">` + cards.map(([k, v]) =>
    `<div class="hcard"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`
  ).join("") + `</div>`;

  html += `<div class="panel-head" style="margin-top:14px"><h2>数据源健康</h2></div>`;
  if (!h.sources.length) {
    html += `<div class="empty">还没有采集记录。</div>`;
  } else {
    html += h.sources.map((s) => `<div class="hcard" style="margin-bottom:8px">
      <div class="k">${esc(s.source)} · 成功 ${s.ok_count} 次 / 失败 ${s.fail_count} 次 · 连续失败 ${s.fail_streak}</div>
      <div class="v" style="font-size:13px">
        最后成功：${fmtTime(s.last_ok_ts)}　延迟：${s.last_latency_ms ?? "—"} ms<br>
        ${s.last_error ? `<span class="err">最后错误：${esc(s.last_error)}</span>` : ""}
      </div></div>`).join("");
  }

  if (state.packs) {
    html += `<div class="panel-head" style="margin-top:14px"><h2>打包物项检查（决定是否展示套利分析）</h2></div>
      <div class="hcard" style="border:1px solid ${state.packs.has_pack_items ? "#2f6d63" : "#6d3f3f"}">
        <div class="v" style="font-size:13px">${esc(state.packs.message)}</div></div>`;
  }

  $("healthBody").innerHTML = html;
  $("healthHistory").innerHTML = (sched.history || []).length
    ? sched.history.map((s) => `<li>${esc(s)}</li>`).join("")
    : `<li>暂无记录</li>`;
}

// ── 详情抽屉 ────────────────────────────────────────────────────────────
/* 抽屉曾经只从 state.items 里找物品，而 state.items 是**被筛选、排序后的那一页表格**。
 * 于是从排行榜点开一个不在当前筛选结果里的子弹时，抽屉标题会退化成 ammo_id、
 * 副标题空白、14 项指标全空 —— 看起来像"没选中子弹"。
 * 现在改为：列表里有就用列表的（立刻出图），同时总是向 /api/ammo/{id} 取一次权威详情，
 * 保证标题、指标、曲线三者永远描述同一个物品，且与当前筛选无关。 */
async function fetchDetailItem(ammoId) {
  const item = await API.ammoOne(ammoId);
  if (!item) throw new Error("接口未返回物品");
  return item;               // 接口返回的就是 AmmoListResponse 里的同一套字段
}

async function openDetail(ammoId) {
  if (!ammoId) return;
  state.detailId = ammoId;
  state.detailChartKey = null;    // 换了子弹就必须重新画，绝不能命中上一张图的记账
  $("drawer").hidden = false; $("mask").hidden = false;
  // 立刻清掉上一个子弹的残留，避免"先看到别人的曲线和脚注"
  $("dChart").innerHTML = `<div class="empty">加载中…</div>`;
  $("dNote").textContent = "";

  // 1) 先用手上的列表行把抽屉填上（立刻出图，且不会白白多请求一次价格）
  const cached = state.items.find((i) => i.ammo_id === ammoId) || null;
  paintDetail(cached, ammoId, { loading: true });

  // 2) 再取权威详情；慢一点也没关系，但失败必须看得见，不能静默留白
  try {
    const item = await fetchDetailItem(ammoId);
    if (state.detailId !== ammoId) return;        // 期间用户已经换了别的子弹
    paintDetail(item, ammoId);
  } catch (e) {
    if (state.detailId !== ammoId) return;
    paintDetail(cached, ammoId, { error: e.message });
  }
}

/** 把某个物品画进抽屉：标题 / 副标题 / 区间按钮 / 14 项指标 / 曲线。
 *  item 为 null（列表里没有且详情还没回来）时也会画出「加载中」，绝不静默留白。 */
function paintDetail(item, ammoId, opts) {
  const o = opts || {};
  $("dName").textContent = item ? item.name : ammoId;
  $("dMeta").textContent = item
    ? `${item.caliber || "—"} · 品质 ${item.tier || "?"} · ${item.length}×${item.width} 格`
    : (o.error ? "" : "加载中…");
  renderDetailMetrics(item, o.error);
  $("dRanges").innerHTML = RANGE_OPTIONS.map((r) =>
    `<button class="chip ${r.code === state.detailRange ? "on" : ""}" data-range="${r.code}">${esc(r.label)}</button>`
  ).join("");
  if (o.error && !item) {
    $("dChart").innerHTML = `<div class="err">详情加载失败：${esc(o.error)}</div>`;
    $("dNote").textContent = "";
    return;
  }
  renderDetailChart({ item: item, loading: o.loading });
}

/** 渲染指标区。error 只在完全没有数据时展示，避免把刷新失败说成"没有指标"。 */
function renderDetailMetrics(item, error) {
  if (!item) {
    $("dMetrics").innerHTML = error
      ? `<div class="err">指标加载失败：${esc(error)}</div>`
      : `<div class="empty">正在读取该物品的指标…</div>`;
    return;
  }
  const cells = [
    ["当前价", fmtNum(item.price), ""],
    ["今日涨跌", fmtPct(item.today_change_pct), cls(item.today_change_pct)],
    ["7 日涨跌", fmtPct(item.day7_change_pct), cls(item.day7_change_pct)],
    ["30 日涨跌", fmtPct(item.day30_change_pct), cls(item.day30_change_pct)],
    ["7 日最高 / 最低", item.high_7d === null ? "—" : `${fmtNum(item.high_7d)} / ${fmtNum(item.low_7d)}`, ""],
    ["距 7 日高点", fmtPct(item.drawdown_from_7d_high_pct), cls(item.drawdown_from_7d_high_pct)],
    ["24h 均价", fmtNum(item.ma_24h), ""],
    ["24h 波动率 σ", item.sigma_24h === null ? "—" : item.sigma_24h.toFixed(4), ""],
    ["z-score", item.z_score === null ? "—" : item.z_score.toFixed(2), ""],
    ["7 日分位", item.percentile_7d === null ? "—"
      : `${(item.percentile_7d * 100).toFixed(1)}% ${item.percentile_label}`, ""],
    ["分位样本", `${item.percentile_samples_7d} 条${item.percentile_reliable ? "" : "（不足，仅供参考）"}`, ""],
    ["机会分", item.opportunity_score === null ? "数据不足" : fmtNum(item.opportunity_score, 1), ""],
    ["数据源自报指标 7zf", item.source_index_7zf === null ? "—" : fmtNum(item.source_index_7zf, 2), ""],
    ["数据点", String(item.samples), ""],
  ];
  $("dMetrics").innerHTML = cells.map(([k, v, c]) =>
    `<div class="metric"><div class="k">${esc(k)}</div><div class="v ${c}">${esc(v)}</div></div>`
  ).join("");
}

/** 画曲线。item 可以是列表行，也可以是详情接口返回的完整对象。
 *  同一次打开抽屉里，列表行和详情返回会各触发一次调用；用 detailChartKey 去掉重复，
 *  避免为同一张图重复打价格接口。关闭抽屉会清空它，下次打开一定重新取数。 */
async function renderDetailChart(opts) {
  const o = opts || {};
  if (!state.detailId) return;
  const ammoId = state.detailId;
  const range = state.detailRange;
  const item = (o.item !== undefined ? o.item : null)
    || state.items.find((i) => i.ammo_id === ammoId) || null;

  const key = `${ammoId}|${range}`;
  if (state.detailChartKey === key) return;    // 这张图已经在画 / 已经画好了
  if (o.loading) $("dChart").innerHTML = `<div class="empty">加载中…</div>`;

  try {
    const data = await API.price(ammoId, range);
    if (state.detailId !== ammoId || state.detailRange !== range) return;   // 已切换
    const hlines = [];
    if (item && item.high_7d !== null && item.high_7d !== undefined) {
      hlines.push({ value: item.high_7d, label: "7 日高", color: "#ef5350" });
    }
    if (item && item.low_7d !== null && item.low_7d !== undefined) {
      hlines.push({ value: item.low_7d, label: "7 日低", color: "#26a69a" });
    }
    $("dChart").innerHTML = lineChart(data.points, { hlines });
    const span = data.points.length
      ? `${fmtTime(data.points[0].ts)} ~ ${fmtTime(data.points[data.points.length - 1].ts)}`
      : "";
    $("dNote").textContent = `${data.count} 个点（粒度：${data.bucket}）　${span}` +
      (data.note ? "　｜　" + data.note : "");
    state.detailChartKey = key;                // 成功了才记账，失败允许重试
  } catch (e) {
    if (state.detailId !== ammoId || state.detailRange !== range) return;
    $("dChart").innerHTML = `<div class="err">曲线加载失败：${esc(e.message)}</div>`;
    $("dNote").textContent = "";
  }
}

function closeDetail() {
  $("drawer").hidden = true; $("mask").hidden = true;
  state.detailId = null;
  state.detailChartKey = null;    // 下次打开重新取数，不显示上次的旧曲线
}

// ── 交易计算器 ──────────────────────────────────────────────────────────
/* 设计取舍：文件头约定「指标一律由后端算」，但计算器的输入来自用户手动填写，
 * 属于纯交互工具而非采集指标，不存在前后端公式漂移问题，因此例外地前端直算。 */

function calcRate() {
  const v = parseFloat($("cRate").value);
  return (Number.isFinite(v) && v > 0 && v <= 1) ? v : null;
}

/** 填充计算器的子弹下拉框（复用行情总览已加载的物品列表）。 */
function renderCalcAmmo() {
  const sel = $("cAmmo");
  const cur = sel.value;
  sel.innerHTML = `<option value="">— 选择子弹 —</option>` +
    state.items.map((i) => `<option value="${esc(i.ammo_id)}">${esc(i.name)}${
      i.price === null ? "" : ` · ${fmtNum(i.price)}`}</option>`).join("");
  sel.value = cur;
  renderCalcAmmoInfo();
}

/** 选中子弹后展示当前价与 7 日区间，方便对照着填价格。 */
function renderCalcAmmoInfo() {
  const el = $("cAmmoInfo");
  const item = state.items.find((i) => i.ammo_id === $("cAmmo").value);
  if (!item) { el.textContent = ""; return; }
  const range = (item.low_7d === null || item.high_7d === null)
    ? "" : ` · 7 日区间 ${fmtNum(item.low_7d)} – ${fmtNum(item.high_7d)}`;
  el.innerHTML = `当前价 <b>${fmtNum(item.price)}</b>${range}` +
    (item.price === null ? "" :
      `　<button class="btn tiny" id="cFill" type="button">用当前价填入买入价</button>`);
}

/** 利润计算：每发利润 = 卖出价 × 到手系数 − 买入价。 */
function renderCalcProfit() {
  const out = $("cProfitOut");
  const rate = calcRate();
  if (rate === null) {
    out.innerHTML = `<div class="empty">到手系数需在 0~1 之间（当前市场约 0.87）。</div>`;
    return;
  }
  const buy = parseFloat($("cBuy").value);
  const sell = parseFloat($("cSell").value);
  if (!Number.isFinite(buy) || !Number.isFinite(sell) || buy <= 0 || sell < 0) {
    out.innerHTML = `<div class="empty">填写买入价和卖出价后自动计算：每发利润 = 卖出价 × ${rate} − 买入价。</div>`;
    return;
  }
  const net = sell * rate;             // 到手单价
  const profit = net - buy;            // 每发利润
  const pct = profit / buy * 100;      // 利润率
  const cells = [
    ["到手单价", fmtNum(net, 2), ""],
    ["每发利润", (profit > 0 ? "+" : "") + fmtNum(profit, 2), cls(profit)],
    ["利润率", fmtPct(pct), cls(pct)],
  ];
  out.innerHTML = cells.map(([k, v, c]) =>
    `<div class="metric"><div class="k">${esc(k)}</div><div class="v ${c}">${esc(v)}</div></div>`).join("");
}

/** 保本计算：卖出价 ≥ 买入价 ÷ 系数 才不亏；买入价 ≤ 卖出价 × 系数 才不亏。 */
function renderCalcBreakEven() {
  const out = $("cBeOut");
  const rate = calcRate();
  if (rate === null) {
    out.innerHTML = `<div class="empty">到手系数需在 0~1 之间（当前市场约 0.87）。</div>`;
    return;
  }
  const buy = parseFloat($("cBeBuy").value);
  const sell = parseFloat($("cBeSell").value);
  const hasBuy = Number.isFinite(buy) && buy > 0;
  const hasSell = Number.isFinite(sell) && sell > 0;
  if (!hasBuy && !hasSell) {
    out.innerHTML = `<div class="empty">填任意一侧即可：保本卖出价 = 买入价 ÷ ${rate}；最高买入价 = 卖出价 × ${rate}。</div>`;
    return;
  }
  const cells = [];
  if (hasBuy) {
    cells.push(["保本卖出价（挂单价）", fmtNum(buy / rate, 2), "",
      `挂单价低于它必亏（买入 ${fmtNum(buy, 2)}）`]);
  }
  if (hasSell) {
    cells.push(["最高可买入价", fmtNum(sell * rate, 2), "",
      `买入价高于它必亏（挂单 ${fmtNum(sell, 2)}）`]);
  }
  out.innerHTML = cells.map(([k, v, c, note]) =>
    `<div class="metric"><div class="k">${esc(k)}</div><div class="v ${c}">${esc(v)}</div>` +
    `<div class="k" style="margin-top:3px">${esc(note)}</div></div>`).join("");
}

// ── 数据加载 ────────────────────────────────────────────────────────────
async function loadAmmo() {
  const params = {
    sort: state.sort, order: state.order, limit: "300",
    only_tradeable: state.onlyTradeable ? "true" : "false",
  };
  if (state.q) params.q = state.q;
  if (state.tiers.size === 1) params.tier = [...state.tiers][0];
  if (state.caliber) params.caliber = state.caliber;
  const data = await API.ammo(params);
  state.total = data.total;
  state.updatedAt = data.updated_at;
  state.stale = data.stale;
  state.ageSeconds = data.age_seconds;
  let items = data.items;
  // 多品质筛选在前端做：后端接口只支持单品质，避免为一个小功能加复杂查询参数
  if (state.tiers.size > 1) items = items.filter((i) => state.tiers.has(i.tier));
  state.items = items;
}

async function loadAll() {
  try {
    const [health] = await Promise.all([API.health()]);
    state.health = health;
  } catch (e) { /* 健康接口失败不应阻塞主流程 */ }

  try {
    await loadAmmo();
  } catch (e) {
    $("tbody").innerHTML = `<tr><td colspan="11"><div class="err">
      加载失败：${esc(e.message)}<br>请确认后端服务正在运行。</div></td></tr>`;
    return;
  }
  renderFreshness(); renderBanner(); renderKpis(); renderTable();
  renderCaliberOptions();
  renderCalcAmmo();
  await renderRank();
  if (state.view === "watch") renderWatch();
}

function renderCaliberOptions() {
  const sel = $("caliber");
  const cur = sel.value;
  const cals = [...new Set(state.items.map((i) => i.caliber).filter(Boolean))].sort();
  sel.innerHTML = `<option value="">全部口径</option>` +
    cals.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  sel.value = cur;

  const ammoSel = $("rAmmo");
  const curA = ammoSel.value;
  ammoSel.innerHTML = `<option value="">全部可交易物品</option>` +
    state.items.map((i) => `<option value="${esc(i.ammo_id)}">${esc(i.name)}</option>`).join("");
  ammoSel.value = curA;
}

async function loadAlerts() {
  try {
    const [rules, logs] = await Promise.all([API.rules(), API.logs()]);
    state.rules = rules; state.logs = logs;
    renderRules(); renderLogs();
  } catch (e) {
    $("ruleBody").innerHTML = `<div class="err">规则加载失败：${esc(e.message)}</div>`;
  }
}

async function loadPacks() {
  try { state.packs = await API.packCheck(); } catch (e) { state.packs = null; }
}

// ── 浏览器内提醒 ────────────────────────────────────────────────────────
function showToast(text) {
  const el = document.createElement("div");
  el.textContent = text;
  el.style.cssText = "position:fixed;right:18px;bottom:18px;z-index:99;background:#1e2939;" +
    "border:1px solid #4a90d9;border-radius:9px;padding:11px 15px;max-width:380px;" +
    "font-size:13px;box-shadow:0 6px 20px rgba(0,0,0,.45)";
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 9000);
}

async function pollPending() {
  try {
    const data = await API.pending();
    if (!data.items.length) return;
    data.items.forEach((n) => {
      const text = `【${n.ammo_name || n.ammo_id}】${n.reason}`;
      showToast(text);
      if (state.notifyEnabled && "Notification" in window && Notification.permission === "granted") {
        try { new Notification("子弹物价告警", { body: text }); } catch (e) { /* 忽略 */ }
      }
    });
    // 有触发就刷新历史
    state.logs = await API.logs();
    renderLogs();
  } catch (e) { /* 轮询失败静默重试 */ }
}

// ── 事件绑定 ────────────────────────────────────────────────────────────
function bind() {
  // 视图切换
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", async () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    state.view = t.dataset.view;
    ["market", "watch", "calc", "alerts", "health"].forEach((v) => {
      $("view-" + v).hidden = (v !== state.view);
    });
    if (state.view === "watch") renderWatch();
    if (state.view === "calc") renderCalcAmmo();
    if (state.view === "alerts") await loadAlerts();
    if (state.view === "health") { state.health = await API.health(); renderHealth(); }
  }));

  // 交易计算器：输入即算，无需提交
  ["cRate", "cBuy", "cSell"].forEach((id) => $(id).addEventListener("input", renderCalcProfit));
  ["cRate", "cBeBuy", "cBeSell"].forEach((id) => $(id).addEventListener("input", renderCalcBreakEven));
  $("cAmmo").addEventListener("change", renderCalcAmmoInfo);
  $("cAmmoInfo").addEventListener("click", (e) => {
    if (e.target.id !== "cFill") return;
    const item = state.items.find((i) => i.ammo_id === $("cAmmo").value);
    if (item && item.price !== null) {
      $("cBuy").value = item.price;
      renderCalcProfit();
    }
  });

  // 筛选
  $("q").addEventListener("input", debounce(async () => {
    state.q = $("q").value.trim();
    await loadAmmo(); renderKpis(); renderTable();
  }, 260));
  $("sort").addEventListener("change", async () => {
    state.sort = $("sort").value; await loadAmmo(); renderTable();
  });
  $("order").addEventListener("change", async () => {
    state.order = $("order").value; await loadAmmo(); renderTable();
  });
  $("caliber").addEventListener("change", async () => {
    state.caliber = $("caliber").value; await loadAmmo(); renderKpis(); renderTable();
  });
  $("onlyTradeable").addEventListener("change", async () => {
    state.onlyTradeable = $("onlyTradeable").checked;
    await loadAmmo(); renderKpis(); renderTable();
  });

  // 品质 chips
  $("tierChips").innerHTML = TIERS.map((t) =>
    `<button class="chip" data-tier="${t}">${t}</button>`).join("");
  $("tierChips").addEventListener("click", async (e) => {
    const t = e.target.dataset.tier; if (!t) return;
    state.tiers.has(t) ? state.tiers.delete(t) : state.tiers.add(t);
    e.target.classList.toggle("on");
    await loadAmmo(); renderKpis(); renderTable();
  });

  // 排行榜 chips
  $("rankChips").addEventListener("click", async (e) => {
    const t = e.target.dataset.rank; if (!t) return;
    state.rankType = t; await renderRank();
  });

  // 表格交互（事件委托）
  $("tbody").addEventListener("click", async (e) => {
    const star = e.target.closest("[data-star]");
    if (star) {
      e.stopPropagation();
      const id = star.dataset.star;
      const item = state.items.find((i) => i.ammo_id === id);
      try {
        if (item && item.in_watchlist) await API.delWatch(id); else await API.addWatch(id);
        await loadAmmo(); renderTable();
      } catch (err) { showToast("自选操作失败：" + err.message); }
      return;
    }
    const alertBtn = e.target.closest("[data-alert]");
    if (alertBtn) {
      e.stopPropagation();
      state.view = "alerts";
      document.querySelector('.tab[data-view="alerts"]').click();
      $("rAmmo").value = alertBtn.dataset.alert;
      $("rThreshold").value = alertBtn.dataset.price || "";
      $("rThreshold").focus();
      return;
    }
    const row = e.target.closest("tr[data-id]");
    if (row) openDetail(row.dataset.id);
  });

  $("watchBody").addEventListener("click", async (e) => {
    const star = e.target.closest("[data-star]");
    if (star) {
      try { await API.delWatch(star.dataset.star); await loadAmmo(); renderWatch(); renderTable(); }
      catch (err) { showToast("操作失败：" + err.message); }
      return;
    }
    const row = e.target.closest("tr[data-id]");
    if (row) openDetail(row.dataset.id);
  });

  $("rankList").addEventListener("click", (e) => {
    const row = e.target.closest(".rank-row");
    if (row) openDetail(row.dataset.id);
  });

  // 抽屉
  $("dClose").addEventListener("click", closeDetail);
  $("mask").addEventListener("click", closeDetail);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDetail(); });
  $("dRanges").addEventListener("click", async (e) => {
    const r = e.target.dataset.range; if (!r) return;
    if (r === state.detailRange) return;         // 重复点同一区间不重画、不闪
    state.detailRange = r;
    document.querySelectorAll("#dRanges .chip").forEach((c) =>
      c.classList.toggle("on", c.dataset.range === r));
    await renderDetailChart({ loading: true });
  });

  // 规则
  $("rType").addEventListener("change", updateRuleHint);
  updateRuleHint();
  $("ruleForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      ammo_id: $("rAmmo").value || null,
      rule_type: $("rType").value,
      threshold: parseFloat($("rThreshold").value),
      cooldown_min: parseInt($("rCooldown").value || "30", 10),
      confirm_min: parseInt($("rConfirm").value || "0", 10),
      quiet_hours: $("rQuiet").value.trim() || null,
      enabled: true,
    };
    try {
      await API.createRule(body);
      await loadAlerts();
      showToast("规则已创建");
    } catch (err) { showToast("创建失败：" + err.message); }
  });

  $("ruleBody").addEventListener("click", async (e) => {
    const tg = e.target.dataset.toggle, dp = e.target.dataset.drop;
    try {
      if (tg) { await API.toggleRule(tg); await loadAlerts(); }
      if (dp) { if (confirm("确认删除这条规则？")) { await API.deleteRule(dp); await loadAlerts(); } }
    } catch (err) { showToast("操作失败：" + err.message); }
  });

  // 顶栏按钮
  $("btnRefresh").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; btn.textContent = "采集中…";
    try {
      const r = await API.ingest();
      showToast(r.ok ? r.summary : "采集失败：" + r.error);
      await loadAll();
      state.health = await API.health(); renderBanner(); renderHealth();
    } catch (err) {
      showToast("触发失败：" + err.message);
    } finally {
      btn.disabled = false; btn.textContent = "刷新数据";
    }
  });

  $("btnNotify").addEventListener("click", async () => {
    if (!("Notification" in window)) { showToast("当前浏览器不支持系统通知，将只显示页面内提醒"); return; }
    const perm = await Notification.requestPermission();
    state.notifyEnabled = (perm === "granted");
    $("btnNotify").textContent = state.notifyEnabled ? "通知已开启" : "通知被拒绝";
  });
}

function updateRuleHint() {
  const t = $("rType").value;
  const hints = {
    price_below: "单发价跌到该值或以下时提醒。适合等抄底。",
    price_above: "单发价涨到该值或以上时提醒。适合挂高价出货。",
    pct_up: "今日涨幅达到该百分比时提醒。",
    pct_down: "今日跌幅达到该百分比时提醒。",
    percentile_below: "⚠ 阈值必须是 0~1 的小数（如 0.15 表示「处于 7 日最低的 15%」）。填 15 永远不会触发。",
    vol_spike: "价格偏离 24 小时均价的标准化程度。2 表示偏离 2 个标准差。该规则不受静默时段限制。",
  };
  $("ruleHint").textContent = hints[t] || "";
  if (t === "percentile_below") { $("rThreshold").value = "0.15"; $("rThreshold").step = "0.01"; }
  else if (t === "vol_spike") { $("rThreshold").value = "3"; $("rThreshold").step = "0.5"; }
  else { $("rThreshold").step = "any"; }
}

function debounce(fn, ms) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ── 启动 ────────────────────────────────────────────────────────────────
(async function main() {
  $("footLeft").textContent =
    "三角洲行动 · 子弹物价监控 v0.1 · 单机自用 · 红涨绿跌";
  bind();
  await loadPacks();
  await loadAll();
  await loadAlerts();
  renderHealth();
  renderCalcProfit();
  renderCalcBreakEven();

  // 浏览器内提醒轮询：每 30 秒取一次待推送告警
  setInterval(pollPending, POLL_MS);
  pollPending();

  // 数据 + 排行榜定时刷新（与后端采集间隔无关，只是让页面保持新鲜）
  setInterval(async () => {
    if (document.hidden) return;         // 页面不可见时不浪费请求
    await loadAll();
  }, 5 * 60 * 1000);

  // 从后台切回来时立即刷新一次（数据可能已过期）
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadAll();
  });
  window.addEventListener("pageshow", (e) => { if (e.persisted) loadAll(); });
})();
