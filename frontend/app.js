"use strict";

const RED = "#f85149";   // A股：涨/多
const GREEN = "#2ea043"; // A股：跌/空
const $ = (id) => document.getElementById(id);

let chart = null;
let currentCode = null;
let watchSet = new Set();

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

function showLoading(on) { $("loading").hidden = !on; }

/* 客户端缓存 + stale-while-revalidate：先用本地旧数据立即渲染，再请求新数据回填 */
function cacheGet(key) {
  try { return JSON.parse(localStorage.getItem("swr:" + key) || "null"); } catch { return null; }
}
function cacheSet(key, data) {
  try { localStorage.setItem("swr:" + key, JSON.stringify(data)); } catch {}
}
async function swr(key, url, onData) {
  const cached = cacheGet(key);
  if (cached) onData(cached, true);       // 立即渲染旧数据
  const fresh = await api(url);            // 后台取新数据
  cacheSet(key, fresh);
  onData(fresh, false);                    // 回填
  return fresh;
}

function skeleton(n, cls) {
  return Array.from({ length: n }, () => `<div class="skel ${cls || ""}"></div>`).join("");
}

/* 按钮忙碌态：点击异步操作时显示转圈 + 文案，并禁用防重复点击 */
function busyOn(btn, label) {
  if (!btn || btn.dataset.busy === "1") return false;
  btn.dataset.orig = btn.innerHTML;
  btn.dataset.busy = "1";
  btn.disabled = true;
  btn.classList.add("is-busy");
  btn.innerHTML = `<span class="spin"></span>${label || "处理中…"}`;
  return true;
}
function busyOff(btn) {
  if (!btn || btn.dataset.busy !== "1") return;
  btn.disabled = false;
  btn.classList.remove("is-busy");
  btn.innerHTML = btn.dataset.orig || btn.innerHTML;
  btn.dataset.busy = "";
}
async function withBusy(btn, fn, label) {
  if (btn && btn.dataset.busy === "1") return;  // 正在进行中，忽略重复点击
  busyOn(btn, label);
  try { return await fn(); }
  finally { busyOff(btn); }
}

/* ---------------- Health ---------------- */
async function loadHealth() {
  try {
    const h = await api("/api/health");
    $("health-dot").className = "dot ok";
    $("health-text").textContent = h.akshare ? "akshare 已就绪" : "akshare 不可用（演示模式）";
    $("data-badge").textContent = h.akshare ? "" : "演示";
  } catch {
    $("health-dot").className = "dot bad";
    $("health-text").textContent = "后端未连接";
  }
}

/* ---------------- Watchlist ---------------- */
async function loadWatchlist() {
  const { items } = await api("/api/watchlist");
  watchSet = new Set(items.map((i) => i.code));
  renderWatchlist(items);
  if (!currentCode && items.length) selectStock(items[0].code);
  updateAddBtn();
}

function renderWatchlist(items) {
  const ul = $("watchlist");
  ul.innerHTML = "";
  items.forEach((it) => {
    const li = document.createElement("li");
    li.dataset.code = it.code;
    if (it.code === currentCode) li.classList.add("active");
    li.innerHTML = `<span class="wl-name">${it.name}</span><span class="wl-code">${it.code}</span><span class="wl-del" title="移除">×</span>`;
    li.querySelector(".wl-name").onclick = () => selectStock(it.code);
    li.querySelector(".wl-code").onclick = () => selectStock(it.code);
    li.querySelector(".wl-del").onclick = (e) => { e.stopPropagation(); removeWatch(it.code); };
    ul.appendChild(li);
  });
}

async function addWatch(code) { renderWatchlist((await api(`/api/watchlist/${code}`, { method: "POST" })).items); watchSet.add(code); updateAddBtn(); loadWatchlist(); }
async function removeWatch(code) { const { items } = await api(`/api/watchlist/${code}`, { method: "DELETE" }); watchSet.delete(code); renderWatchlist(items); updateAddBtn(); }

function updateAddBtn() {
  const btn = $("add-btn");
  if (!currentCode) { btn.hidden = true; return; }
  btn.hidden = watchSet.has(currentCode);
}

/* ---------------- Search ---------------- */
let searchTimer = null;
function initSearch() {
  const input = $("search-input");
  const box = $("search-results");
  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const q = input.value.trim();
    if (!q) { box.classList.remove("show"); return; }
    searchTimer = setTimeout(async () => {
      try {
        const { results } = await api(`/api/search?q=${encodeURIComponent(q)}`);
        box.innerHTML = "";
        results.forEach((r) => {
          const d = document.createElement("div");
          d.className = "item";
          d.innerHTML = `<span class="c">${r.code}</span><span>${r.name}</span>`;
          d.onclick = () => { selectStock(r.code); box.classList.remove("show"); input.value = ""; };
          box.appendChild(d);
        });
        box.classList.toggle("show", results.length > 0);
      } catch { box.classList.remove("show"); }
    }, 250);
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search")) box.classList.remove("show");
  });
}

/* ---------------- Stock detail ---------------- */
const _stockMem = new Map();  // 会话内股票详情缓存（含 K 线，太大不入 localStorage）

function renderStock(d) {
  renderHeader(d);
  renderVerdict(d.analysis);
  renderTrends(d.trends);
  renderCharts(d);
  renderSignals(d.analysis);
}

function renderTrends(t) {
  const box = $("trends");
  if (!t || !t.items) { box.hidden = true; return; }
  box.hidden = false;
  const cells = Object.values(t.items)
    .map((it) => `<span class="tf ${it.tone}"><b>${it.name}</b>${it.label}</span>`)
    .join("");
  box.innerHTML =
    `<span class="trends-title">多周期趋势</span>${cells}` +
    `<span class="tf-align ${t.align.tone}">${t.align.label}</span>`;
}

async function selectStock(code) {
  currentCode = code;
  const out = $("ai-output");
  out.classList.add("placeholder");
  out.textContent = "点击右上「用我的 LLM 分析」，由你接入的大模型基于真实指标流式生成点评。";
  document.querySelectorAll("#watchlist li").forEach((li) =>
    li.classList.toggle("active", li.dataset.code === code));
  updateAddBtn();
  resetBacktest();
  loadFundamentals(code);
  loadPlan(code);
  loadStockNews(code);

  const cached = _stockMem.get(code);
  if (cached) renderStock(cached);   // 立即用缓存渲染（瞬开）
  else showLoading(true);
  try {
    const d = await api(`/api/stock/${code}?days=160`);  // 后台取新数据
    _stockMem.set(code, d);
    if (currentCode === code) renderStock(d);  // 用户未切走才回填
  } catch (e) {
    if (!cached) $("verdict").textContent = "加载失败：" + e.message;
  } finally {
    showLoading(false);
  }
}

function renderHeader(d) {
  $("stock-name").textContent = d.name;
  $("stock-code").textContent = d.code;
  $("demo-tag").hidden = !d.is_demo;
  const up = d.quote.change >= 0;
  $("price").textContent = d.quote.close.toFixed(2);
  $("price").className = "price " + (up ? "up" : "down");
  const sign = up ? "+" : "";
  $("change").textContent = `${sign}${d.quote.change.toFixed(2)}  (${sign}${d.quote.pct.toFixed(2)}%)`;
  $("change").className = "change " + (up ? "up" : "down");
}

function renderVerdict(a) {
  const v = $("verdict");
  v.textContent = "综合判断：" + a.verdict;
  v.className = "verdict " + a.verdict_tone;
  $("cnt-bull").textContent = a.counts.bullish;
  $("cnt-bear").textContent = a.counts.bearish;
  $("cnt-warn").textContent = a.counts.warning;
}

const TONE_LABEL = { bullish: "偏多", bearish: "偏空", warning: "风险", neutral: "中性" };
function renderSignals(a) {
  const wrap = $("signals");
  wrap.innerHTML = "";
  a.signals.forEach((s) => {
    const el = document.createElement("div");
    el.className = "sig " + s.tone;
    el.innerHTML = `<div class="sig-head"><span class="sig-dim">${s.dim}</span><span class="sig-label">${s.label}</span></div><div class="sig-text">${s.text}</div>`;
    wrap.appendChild(el);
  });
  $("summary").textContent = a.summary;
  $("disclaimer").textContent = a.disclaimer;
}

/* ---------------- Charts (ECharts, 3 grids) ---------------- */
function renderCharts(d) {
  if (!chart) chart = echarts.init($("chart-main"), null, { renderer: "canvas" });
  const k = d.kline;
  const axisStyle = { axisLine: { lineStyle: { color: "#232d3b" } }, axisLabel: { color: "#9aa7b6" }, splitLine: { lineStyle: { color: "#1a232f" } } };

  const option = {
    backgroundColor: "transparent",
    animation: false,
    legend: {
      data: ["MA5", "MA20", "MA60", "BOLL上轨", "BOLL下轨"],
      top: 4, textStyle: { color: "#9aa7b6" }, itemWidth: 14, itemHeight: 8,
    },
    tooltip: {
      trigger: "axis", axisPointer: { type: "cross" },
      backgroundColor: "#1a232f", borderColor: "#232d3b", textStyle: { color: "#e6edf3" },
    },
    axisPointer: { link: [{ xAxisIndex: "all" }], label: { backgroundColor: "#2f81f7" } },
    grid: [
      { left: 56, right: 20, top: 36, height: "48%" },
      { left: 56, right: 20, top: "60%", height: "12%" },
      { left: 56, right: 20, top: "76%", height: "16%" },
    ],
    xAxis: [
      { type: "category", data: k.dates, gridIndex: 0, boundaryGap: true, axisLine: { lineStyle: { color: "#232d3b" } }, axisLabel: { show: false } },
      { type: "category", data: k.dates, gridIndex: 1, axisLabel: { show: false }, axisLine: { lineStyle: { color: "#232d3b" } } },
      { type: "category", data: k.dates, gridIndex: 2, axisLine: { lineStyle: { color: "#232d3b" } }, axisLabel: { color: "#9aa7b6" } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, ...axisStyle },
      { scale: true, gridIndex: 1, name: "量", nameTextStyle: { color: "#5f6e7e" }, axisLabel: { show: false }, splitLine: { show: false }, axisLine: { lineStyle: { color: "#232d3b" } } },
      { scale: true, gridIndex: 2, name: "MACD", nameTextStyle: { color: "#5f6e7e" }, ...axisStyle },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1, 2], start: 55, end: 100 },
      { type: "slider", xAxisIndex: [0, 1, 2], bottom: 4, height: 16, start: 55, end: 100, borderColor: "#232d3b", textStyle: { color: "#5f6e7e" } },
    ],
    series: [
      {
        name: "K线", type: "candlestick", data: k.ohlc, xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: { color: RED, color0: GREEN, borderColor: RED, borderColor0: GREEN },
      },
      { name: "MA5", type: "line", data: k.ma5, xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, lineStyle: { width: 1, color: "#e3b341" } },
      { name: "MA20", type: "line", data: k.ma20, xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, lineStyle: { width: 1, color: "#2f81f7" } },
      { name: "MA60", type: "line", data: k.ma60, xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, lineStyle: { width: 1, color: "#a371f7" } },
      { name: "BOLL上轨", type: "line", data: k.boll_upper, xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, lineStyle: { width: 1, type: "dashed", color: "#6b7785" } },
      { name: "BOLL下轨", type: "line", data: k.boll_lower, xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, lineStyle: { width: 1, type: "dashed", color: "#6b7785" } },
      {
        name: "成交量", type: "bar", data: k.volume, xAxisIndex: 1, yAxisIndex: 1,
        itemStyle: {
          color: (p) => {
            const o = k.ohlc[p.dataIndex];
            return o && o[1] >= o[0] ? RED : GREEN;
          },
        },
      },
      {
        name: "MACD", type: "bar", data: k.macd, xAxisIndex: 2, yAxisIndex: 2,
        itemStyle: { color: (p) => (p.data >= 0 ? RED : GREEN) },
      },
      { name: "DIF", type: "line", data: k.dif, xAxisIndex: 2, yAxisIndex: 2, showSymbol: false, lineStyle: { width: 1, color: "#e3b341" } },
      { name: "DEA", type: "line", data: k.dea, xAxisIndex: 2, yAxisIndex: 2, showSymbol: false, lineStyle: { width: 1, color: "#2f81f7" } },
    ],
  };
  chart.setOption(option, true);
}

window.addEventListener("resize", () => chart && chart.resize());

/* ---------------- LLM 接入（多档管理 + 切换） ---------------- */
async function loadActiveModel() {
  try {
    const c = await api("/api/llm/config");
    $("ai-model").textContent = c.configured ? `· ${c.model}` : "· 未配置";
  } catch {}
}

function resetForm() {
  $("cfg-id").value = "";
  $("cfg-name").value = "";
  $("cfg-base").value = "";
  $("cfg-key").value = "";
  $("cfg-key").placeholder = "sk-...（本地保存，不上传）";
  $("cfg-model").value = "";
  $("cfg-proxy").value = "";
  $("form-title").textContent = "新增接入";
  $("cfg-reset").hidden = true;
}

async function renderProfiles() {
  const wrap = $("profile-list");
  try {
    const { active, profiles } = await api("/api/llm/profiles");
    if (!profiles.length) {
      wrap.innerHTML = '<div class="profile-empty">还没有配置。在下方填写并保存第一套接入。</div>';
    } else {
      wrap.innerHTML = "";
      profiles.forEach((p) => {
        const row = document.createElement("div");
        row.className = "profile-row" + (p.id === active ? " active" : "");
        row.innerHTML =
          `<input type="radio" name="active-profile" ${p.id === active ? "checked" : ""} />` +
          `<div class="p-main"><div class="p-name">${p.name}${p.id === active ? ' <span class="p-act">· 使用中</span>' : ""}</div>` +
          `<div class="p-meta">${p.model || "?"} · ${p.base_url} · ${p.configured ? p.api_key_masked : "无 key"}</div></div>` +
          `<button class="p-edit">编辑</button><button class="p-del">删除</button>`;
        row.querySelector('input[type="radio"]').onclick = () => setActiveProfile(p.id);
        row.querySelector(".p-main").onclick = () => setActiveProfile(p.id);
        row.querySelector(".p-edit").onclick = (e) => { e.stopPropagation(); editProfile(p); };
        row.querySelector(".p-del").onclick = (e) => { e.stopPropagation(); deleteProfile(p.id); };
        wrap.appendChild(row);
      });
    }
  } catch (e) {
    wrap.innerHTML = `<div class="profile-empty">加载失败：${e.message}</div>`;
  }
}

async function setActiveProfile(id) {
  try {
    await api("/api/llm/active", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    renderProfiles();
    loadActiveModel();
  } catch {}
}

function editProfile(p) {
  $("cfg-id").value = p.id;
  $("cfg-name").value = p.name;
  $("cfg-base").value = p.base_url;
  $("cfg-model").value = p.model;
  $("cfg-proxy").value = p.proxy || "";
  $("cfg-key").value = "";
  $("cfg-key").placeholder = p.configured ? "已配置：" + p.api_key_masked + "（留空则不修改）" : "sk-...";
  $("form-title").textContent = "编辑接入：" + p.name;
  $("cfg-reset").hidden = false;
}

async function deleteProfile(id) {
  try {
    await api(`/api/llm/profiles/${id}`, { method: "DELETE" });
    if ($("cfg-id").value === id) resetForm();
    renderProfiles();
    loadActiveModel();
  } catch {}
}

function openModal() { $("llm-modal").hidden = false; resetForm(); renderProfiles(); }
function closeModal() { $("llm-modal").hidden = true; }

async function saveLlmConfig() {
  const status = $("cfg-status");
  const base = $("cfg-base").value.trim();
  const model = $("cfg-model").value.trim();
  const id = $("cfg-id").value;
  if (!id && (!base || !model)) {
    status.textContent = "请至少填写 Base URL 和 Model"; status.className = "cfg-status err";
    return;
  }
  status.textContent = "保存中…"; status.className = "cfg-status";
  try {
    const body = { name: $("cfg-name").value.trim(), base_url: base, model, proxy: $("cfg-proxy").value.trim() };
    if (id) body.id = id;
    const key = $("cfg-key").value.trim();
    if (key) body.api_key = key;
    await api("/api/llm/profiles", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    status.textContent = "已保存 ✓"; status.className = "cfg-status ok";
    resetForm();
    renderProfiles();
    loadActiveModel();
    setTimeout(() => (status.textContent = ""), 1500);
  } catch (e) {
    status.textContent = "保存失败：" + e.message;
    status.className = "cfg-status err";
  }
}

async function runAiAnalysis() {
  if (!currentCode) return;
  const out = $("ai-output");
  out.classList.remove("placeholder");
  out.innerHTML = '<span class="loading-line"><span class="spin"></span>正在请求你的 LLM，等待首个字…</span>';
  const pref = $("ai-pref") ? $("ai-pref").value : "balanced";
  try {
    const res = await fetch(`/api/stock/${currentCode}/ai/stream?days=160&pref=${pref}`, { method: "POST" });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    out.textContent = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      out.textContent += decoder.decode(value, { stream: true });
      out.scrollTop = out.scrollHeight;
    }
    if (!out.textContent.trim()) out.textContent = "（模型未返回内容，请重试或检查配置）";
  } catch (e) {
    out.textContent = "调用失败：" + e.message;
  }
}

$("llm-settings-btn").onclick = openModal;
$("llm-close").onclick = closeModal;
$("cfg-save").onclick = saveLlmConfig;
$("cfg-reset").onclick = resetForm;
$("ai-btn").onclick = (e) => withBusy(e.currentTarget, runAiAnalysis, "生成中…");
$("llm-modal").addEventListener("click", (e) => { if (e.target.id === "llm-modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

/* ---------------- Fundamentals ---------------- */
function renderFundamentals(d) {
  const el = $("fundamentals");
  {
    const v = d.valuation || {};
    const pctText = (p) => p == null ? "" : `近5年 ${p}% 分位`;
    const pctCls = (p) => p == null ? "" : (p <= 30 ? "cheap" : (p >= 70 ? "rich" : "mid"));
    const metrics = [];
    if (v.pe_ttm != null) metrics.push(["市盈率 PE(TTM)", v.pe_ttm, pctText(v.pe_ttm_pct), pctCls(v.pe_ttm_pct)]);
    if (v.pb != null) metrics.push(["市净率 PB", v.pb, pctText(v.pb_pct), pctCls(v.pb_pct)]);
    if (d.fund_flow && d.fund_flow.main_net != null)
      metrics.push(["主力净流入(万)", d.fund_flow.main_net, "", ""]);

    let html = "";
    if (metrics.length) {
      html += '<div class="funda-metrics">' +
        metrics.map(([l, val, sub, cls]) =>
          `<div class="funda-metric"><div class="v">${val}</div><div class="l">${l}</div>` +
          (sub ? `<div class="pctile ${cls}">${sub}</div>` : "") + "</div>").join("") +
        "</div>";
    }
    const fin = d.financials || [];
    if (fin.length) {
      const cols = ["报告期", "营业总收入", "营业总收入同比增长率", "净利润", "净利润同比增长率", "净资产收益率", "销售毛利率", "资产负债率", "基本每股收益"];
      const present = cols.filter((c) => fin.some((r) => r[c] != null));
      html += '<table class="funda-table"><thead><tr>' +
        present.map((c) => `<th>${c.replace("营业总收入", "营收").replace("同比增长率", "同比").replace("净资产收益率", "ROE").replace("销售毛利率", "毛利率").replace("基本每股收益", "EPS")}</th>`).join("") +
        "</tr></thead><tbody>" +
        fin.map((r) => "<tr>" + present.map((c) => `<td>${r[c] == null ? "—" : r[c]}</td>`).join("") + "</tr>").join("") +
        "</tbody></table>";
    }
    el.innerHTML = html || '<span class="funda-empty">该股暂无可用的基本面数据（数据源可能临时不可用）。</span>';
  }
}

async function loadFundamentals(code) {
  const el = $("fundamentals");
  if (!cacheGet("funda:" + code)) el.innerHTML = skeleton(3, "funda-skel");
  try {
    await swr("funda:" + code, `/api/stock/${code}/fundamentals`, (d) => renderFundamentals(d));
  } catch (e) {
    el.innerHTML = `<span class="funda-empty">基本面加载失败：${e.message}</span>`;
  }
}

/* ---------------- 操作建议（投资大师 Skills） ---------------- */
function renderPlan(d) {
  const plan = d.plan || {};
  const lv = plan.levels || {};
  const st = plan.stance || {};
  $("plan-stance").innerHTML = st.label
    ? `<span class="ps ${st.tone}">${st.label}</span>` +
      (st.score != null
        ? `<span class="ps-meta">加权多空分 <b>${st.score}</b>/100 · 平均置信 ${st.confidence}% · 分歧 ${st.dispersion}（看多 ${st.buy}/看空 ${st.reduce}）</span>`
        : "")
    : "";

  const cell = (label, val) =>
    val == null ? "" : `<span class="pl-cell"><i>${label}</i><b>${val}</b></span>`;
  $("plan-levels").innerHTML =
    cell("现价", lv.close) + cell("支撑", lv.support) + cell("压力", lv.resistance) +
    cell("MA20", lv.ma20) + cell("MA60", lv.ma60) +
    cell("布林下轨", lv.boll_lower) + cell("布林上轨", lv.boll_upper);

  renderValuation(plan.valuation || {});

  const masters = plan.masters || [];
  const wrap = $("plan-masters");
  wrap.innerHTML = masters.map((m) => {
    const zone = m.buy_zone ? `${m.buy_zone[0]} ~ ${m.buy_zone[1]}` : "—";
    const tp = m.take_profit != null ? m.take_profit : "—";
    const sl = m.stop_loss != null ? m.stop_loss : "—";
    const meta = m.score != null
      ? `<div class="pm-meta"><span class="pm-score ${m.tone}">多空 ${m.score}</span>` +
        `<span class="pm-conf" title="置信度 ${m.confidence}%"><i style="width:${m.confidence}%"></i></span>` +
        `<span class="pm-conf-t">置信 ${m.confidence}%</span></div>`
      : (m.confidence != null
        ? `<div class="pm-meta"><span class="pm-conf" title="置信度 ${m.confidence}%"><i style="width:${m.confidence}%"></i></span><span class="pm-conf-t">置信 ${m.confidence}%</span></div>`
        : "");
    return `<div class="pm ${m.tone}">` +
      `<div class="pm-top"><span class="pm-name">${m.name}</span>` +
      `<span class="pm-horizon">${m.horizon}</span>` +
      `<span class="pm-action ${m.tone}">${m.action}</span></div>` +
      meta +
      `<div class="pm-levels">` +
      `<span class="pm-lv buy"><i>买入区</i>${zone}</span>` +
      `<span class="pm-lv tp"><i>止盈</i>${tp}</span>` +
      `<span class="pm-lv sl"><i>止损</i>${sl}</span></div>` +
      `<div class="pm-reason">${m.reason}</div>` +
      `<div class="pm-ai">` +
      `<button class="pm-ai-btn" data-key="${m.key}">🧠 让这位大师 AI 解读</button>` +
      `<div class="pm-ai-out" hidden></div></div></div>`;
  }).join("");
  wrap.querySelectorAll(".pm-ai-btn").forEach((btn) => {
    btn.onclick = () => runMasterAi(d.code, btn.dataset.key, btn);
  });
  $("plan-disclaimer").textContent = plan.disclaimer || "";
}

function renderValuation(v) {
  const box = $("plan-valuation");
  if (!box) return;
  if (!v || !v.fair_mid) {
    box.innerHTML = v && v.note
      ? `<div class="pv-head"><span class="pv-title">合理估值锚</span><span class="pv-note">${v.note}</span></div>`
      : "";
    return;
  }
  const toneMap = { 低估: "bullish", 合理: "neutral", 高估: "bearish" };
  const tone = toneMap[v.verdict] || "neutral";
  const mosTxt = `${v.mos > 0 ? "+" : ""}${v.mos}%`;
  // 区间条上现价的位置（0~100%），现价低于下沿/高于上沿做夹取
  const span = v.fair_high - v.fair_low;
  let pos = span > 0 ? ((v.price - v.fair_low) / span) * 100 : 50;
  pos = Math.max(2, Math.min(98, pos));
  const methods = (v.methods || []).map((m) =>
    `<span class="pv-m" title="${m.note || ""}">${m.name}<b>${m.fair}</b></span>`).join("");
  box.innerHTML =
    `<div class="pv-head"><span class="pv-title">合理估值锚</span>` +
    `<span class="pv-verdict ${tone}">${v.verdict} · 安全边际 ${mosTxt}</span>` +
    `<span class="pv-note">${v.note || ""}</span></div>` +
    `<div class="pv-bar">` +
      `<span class="pv-lo">${v.fair_low}</span>` +
      `<div class="pv-track"><div class="pv-mid" style="left:50%"></div>` +
        `<div class="pv-price ${tone}" style="left:${pos}%" title="现价 ${v.price}">▲</div></div>` +
      `<span class="pv-hi">${v.fair_high}</span></div>` +
    `<div class="pv-sub">现价 <b>${v.price}</b> · 合理中枢 <b>${v.fair_mid}</b>（多法中位）</div>` +
    `<div class="pv-methods">${methods}</div>`;
}

async function runMasterAi(code, key, btn) {
  const out = btn.parentElement.querySelector(".pm-ai-out");
  out.hidden = false;
  out.innerHTML = '<span class="loading-line"><span class="spin"></span>正在请求你的 LLM…</span>';
  if (!busyOn(btn, "解读中…")) return;
  let ok = false;
  try {
    const res = await fetch(`/api/stock/${code}/master/${key}/ai/stream?days=160`, { method: "POST" });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    out.textContent = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      out.textContent += decoder.decode(value, { stream: true });
    }
    if (!out.textContent.trim()) out.textContent = "（模型未返回内容，请重试）";
    ok = true;
  } catch (e) {
    out.textContent = "调用失败：" + e.message;
  } finally {
    busyOff(btn);
    if (ok) btn.innerHTML = "↻ 重新解读";
  }
}

async function loadPlan(code) {
  const wrap = $("plan-masters");
  if (!cacheGet("plan:" + code)) {
    $("plan-stance").innerHTML = "";
    $("plan-levels").innerHTML = "";
    wrap.innerHTML = skeleton(4, "plan-skel");
  }
  try {
    await swr("plan:" + code, `/api/stock/${code}/plan?days=160`, (d) => {
      if (currentCode === code) renderPlan(d);
    });
  } catch (e) {
    wrap.innerHTML = `<span class="funda-empty">操作建议加载失败：${e.message}</span>`;
  }
}

/* ---------------- 情报（板块热度 + 财经快讯） ---------------- */
function renderSectors(d) {
  const box = $("sector-board");
  const row = (s, dir) => {
    const cls = s.pct >= 0 ? "up" : "down";
    const col = s.pct >= 0 ? "var(--bear)" : "var(--bull)";
    return `<div class="sec-row ${dir}">` +
      `<span class="sec-name">${s.name}</span>` +
      `<span class="sec-pct" style="color:${col}">${s.pct >= 0 ? "+" : ""}${s.pct}%</span>` +
      `<span class="sec-leader">领涨 ${s.leader || "—"}</span></div>`;
  };
  const leaders = (d.leaders || []).map((s) => row(s, "lead")).join("");
  const laggards = (d.laggards || []).map((s) => row(s, "lag")).join("");
  box.innerHTML =
    `<div class="sec-group"><div class="sec-head bull">领涨板块</div>${leaders || '<div class="news-empty">暂无</div>'}</div>` +
    `<div class="sec-group"><div class="sec-head bear">领跌板块</div>${laggards || '<div class="news-empty">暂无</div>'}</div>`;
}

function sentBadge(n) {
  if (!n.sentiment) return "";
  const cls = n.sentiment === "利好" ? "pos" : n.sentiment === "利空" ? "neg" : "neu";
  const sc = (n.score || n.score === 0) && n.sentiment !== "中性"
    ? ` ${n.score > 0 ? "+" : ""}${n.score}` : "";
  return `<span class="news-sent ${cls}">${n.sentiment}${sc}</span>`;
}

function renderNews(d) {
  const box = $("news-stream");
  const items = d.items || [];
  if (!items.length) { box.innerHTML = '<div class="news-empty">暂无快讯（数据源可能临时不可用）。</div>'; return; }
  box.innerHTML = items.map((n) => {
    const tags = (n.sectors || []).map((s) => `<span class="news-tag">${s}</span>`).join("");
    const title = n.url
      ? `<a href="${n.url}" target="_blank" rel="noreferrer">${n.title}</a>` : n.title;
    return `<div class="news-item"><div class="news-top"><span class="news-time">${n.time}</span>${sentBadge(n)}${tags}</div>` +
      `<div class="news-title">${title}</div>` +
      (n.summary ? `<div class="news-sum">${n.summary}</div>` : "") + "</div>";
  }).join("");
}

async function runNewsSentiment() {
  const btn = $("news-sentiment-btn"); const bar = $("news-sentiment-bar");
  if (!busyOn(btn, "分析中…")) return;
  bar.hidden = false;
  bar.innerHTML = '<span class="loading-line"><span class="spin"></span>正在用 LLM 给快讯打标（约数十秒，结果会缓存）…</span>';
  try {
    const d = await api("/api/news/sentiment?limit=40&cap=24", { method: "POST" });
    if (d.is_demo) { bar.hidden = false; bar.innerHTML = '<span class="news-empty">数据源暂不可用，无法打标。</span>'; return; }
    renderNews(d);
    const c = d.counts || {};
    const secs = (d.sectors || []).slice(0, 6).map((s) =>
      `<span class="news-tag ${s.score >= 0 ? "pos" : "neg"}">${s.sector} ${s.score > 0 ? "+" : ""}${s.score}</span>`).join("");
    bar.hidden = false;
    bar.innerHTML =
      `<div class="sent-counts">情绪统计：<b class="pos">利好 ${c.利好 || 0}</b> · <b class="neg">利空 ${c.利空 || 0}</b> · 中性 ${c.中性 || 0}</div>` +
      `<div class="sent-secs">板块情绪榜：${secs || "—"}</div>`;
  } catch (e) {
    bar.hidden = false; bar.innerHTML = `<span class="news-empty">打标失败：${e.message}</span>`;
  } finally { busyOff(btn); }
}
$("news-sentiment-btn").onclick = runNewsSentiment;

async function loadIntel() {
  const sb = $("sector-board");
  const ns = $("news-stream");
  if (!cacheGet("sectors")) sb.innerHTML = skeleton(2, "plan-skel");
  if (!cacheGet("news")) ns.innerHTML = skeleton(3, "funda-skel");
  try { await swr("sectors", "/api/sectors?kind=industry", (d) => renderSectors(d)); }
  catch (e) { sb.innerHTML = `<span class="news-empty">板块加载失败：${e.message}</span>`; }
  try { await swr("news", "/api/news?limit=40", (d) => renderNews(d)); }
  catch (e) { ns.innerHTML = `<span class="news-empty">快讯加载失败：${e.message}</span>`; }
}
$("intel-refresh").onclick = (e) => withBusy(e.currentTarget, loadIntel, "刷新中…");

function renderStockNews(d) {
  const box = $("stock-news");
  const items = (d && d.items) || [];
  if (!items.length) { box.innerHTML = '<span class="news-empty">暂无相关新闻。</span>'; return; }
  box.innerHTML = items.map((n) => {
    const title = n.url ? `<a href="${n.url}" target="_blank" rel="noreferrer">${n.title}</a>` : n.title;
    return `<div class="news-item"><div class="news-top"><span class="news-time">${n.time}</span>` +
      (n.source ? `<span class="news-src">${n.source}</span>` : "") + sentBadge(n) + "</div>" +
      `<div class="news-title">${title}</div>` +
      (n.summary ? `<div class="news-sum">${n.summary}</div>` : "") + "</div>";
  }).join("");
}

async function runStockIntel() {
  if (!currentCode) return;
  const btn = $("stock-intel-btn"); const out = $("stock-intel-out");
  const pref = $("ai-pref") ? $("ai-pref").value : "balanced";
  if (!busyOn(btn, "分析中…")) return;
  out.hidden = false;
  out.innerHTML = '<span class="loading-line"><span class="spin"></span>正在用 LLM 分析消息面（约数十秒）…</span>';
  try {
    const d = await api(`/api/stock/${currentCode}/intel?limit=8&pref=${pref}`, { method: "POST" });
    if (d.is_demo) { out.innerHTML = '<span class="news-empty">暂无足够新闻用于消息面分析。</span>'; return; }
    if (d.items && d.items.length) renderStockNews(d);
    const c = d.counts || {};
    const head = `<div class="sent-counts">消息面净值 <b class="${d.net >= 0 ? "pos" : "neg"}">${d.net > 0 ? "+" : ""}${d.net}</b>` +
      ` · 利好 ${c.利好 || 0} · 利空 ${c.利空 || 0} · 中性 ${c.中性 || 0}</div>`;
    out.innerHTML = head + `<div class="pm-ai-text">${(d.summary || "").replace(/\n/g, "<br>")}</div>`;
  } catch (e) {
    out.innerHTML = `<span class="news-empty">分析失败：${e.message}</span>`;
  } finally { busyOff(btn); }
}
$("stock-intel-btn").onclick = runStockIntel;

async function loadStockNews(code) {
  const box = $("stock-news");
  const io = $("stock-intel-out");
  if (io) { io.hidden = true; io.innerHTML = ""; }
  if (!cacheGet("snews:" + code)) box.innerHTML = skeleton(2, "funda-skel");
  try {
    await swr("snews:" + code, `/api/stock/${code}/news?limit=8`, (d) => {
      if (currentCode === code) renderStockNews(d);
    });
  } catch (e) {
    box.innerHTML = `<span class="news-empty">新闻加载失败：${e.message}</span>`;
  }
}

/* ---------------- View nav ---------------- */
function switchView(view) {
  document.querySelectorAll(".nav-tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== "view-" + view));
  if (view === "market") loadMarket();
  if (view === "intel") loadIntel();
  if (view === "overview") loadOverview();
  if (view === "scan") loadScanRules();
  if (view === "stock" && chart) setTimeout(() => chart.resize(), 50);
}
document.querySelectorAll(".nav-tab").forEach((t) => (t.onclick = () => switchView(t.dataset.view)));

/* ---------------- Market（大盘） ---------------- */
function renderMarket(d) {
  const idxBox = $("market-indices");
  const brBox = $("market-breadth");
  {
    idxBox.innerHTML = "";
    d.indices.forEach((ix) => {
      const up = ix.pct >= 0;
      const col = up ? "var(--bear)" : "var(--bull)";
      const card = document.createElement("div");
      card.className = "mk-card";
      card.innerHTML =
        `<div class="mk-name">${ix.name}</div>` +
        `<div class="mk-price" style="color:${col}">${ix.price}</div>` +
        `<div class="mk-pct" style="color:${col}">${up ? "+" : ""}${ix.change} (${up ? "+" : ""}${ix.pct}%)</div>`;
      idxBox.appendChild(card);
    });
    const b = d.breadth || {};
    if (b.up != null || b.down != null) {
      const total = (b.up || 0) + (b.down || 0) + (b.flat || 0);
      const upPct = total ? Math.round(((b.up || 0) / total) * 100) : 0;
      brBox.innerHTML =
        `<div class="br-title">全市场涨跌家数${d.is_demo ? "（演示）" : ""}</div>` +
        `<div class="br-bar"><span style="width:${upPct}%"></span></div>` +
        `<div class="br-nums">` +
        `<span class="bull">上涨 ${b.up ?? "-"}</span>` +
        `<span class="neutral">平盘 ${b.flat ?? "-"}</span>` +
        `<span class="bear">下跌 ${b.down ?? "-"}</span>` +
        `<span class="bull">涨停 ${b.limit_up ?? "-"}</span>` +
        `<span class="bear">跌停 ${b.limit_down ?? "-"}</span>` +
        `</div>`;
    }
    if (d.is_demo) idxBox.insertAdjacentHTML("afterbegin", '<div class="mk-demo">演示数据（实时源暂不可用）</div>');
  }
}

async function loadMarket() {
  const idxBox = $("market-indices");
  if (!cacheGet("market")) idxBox.innerHTML = skeleton(6, "mk-skel");  // 无缓存才显骨架
  try {
    await swr("market", "/api/market", (d) => renderMarket(d));
  } catch (e) {
    idxBox.innerHTML = `<span class="funda-empty">加载失败：${e.message}</span>`;
  }
}
$("mk-refresh").onclick = (e) => withBusy(e.currentTarget, loadMarket, "刷新中…");

/* ---------------- Overview ---------------- */
function renderOverview(items) {
  const grid = $("overview-grid");
  {
    grid.innerHTML = "";
    items.forEach((it) => {
      const card = document.createElement("div");
      card.className = "ov-card";
      if (it.error) {
        card.innerHTML = `<div class="ov-top"><span class="ov-name">${it.name}<span class="ov-code">${it.code}</span></span></div><div class="ov-verdict neutral">加载失败</div>`;
      } else {
        const up = it.pct >= 0;
        const dots = it.signals.map((s) => `<span class="ov-dot"><i class="${s.tone}"></i>${s.dim}</span>`).join("");
        card.innerHTML =
          `<div class="ov-top"><span class="ov-name">${it.name}<span class="ov-code">${it.code}</span></span>` +
          `<span class="ov-price ${up ? "up" : "down"}" style="color:${up ? "var(--bear)" : "var(--bull)"}">${it.close} (${up ? "+" : ""}${it.pct}%)</span></div>` +
          `<div class="ov-verdict ${it.verdict_tone}">${it.verdict}</div>` +
          `<div class="ov-dots">${dots}</div>`;
      }
      card.onclick = () => { switchView("stock"); selectStock(it.code); };
      grid.appendChild(card);
    });
  }
}

async function loadOverview() {
  const grid = $("overview-grid");
  const cached = cacheGet("overview");
  if (!cached) grid.innerHTML = skeleton(Math.max(watchSet.size, 3), "ov-skel");  // 无缓存才显骨架
  try {
    await swr("overview", "/api/overview", (d) => renderOverview(d.items));
  } catch (e) {
    grid.innerHTML = `<span class="funda-empty">加载失败：${e.message}</span>`;
  }
}
$("ov-refresh").onclick = (e) => withBusy(e.currentTarget, loadOverview, "刷新中…");

/* ---------------- Scanner ---------------- */
let scanRulesLoaded = false;
async function loadScanRules() {
  if (scanRulesLoaded) return;
  try {
    const { rules } = await api("/api/scan/rules");
    $("scan-rules").innerHTML = rules.map((r) =>
      `<label class="scan-rule"><input type="checkbox" value="${r.id}" />${r.label}</label>`).join("");
    scanRulesLoaded = true;
  } catch {}
}
function buildScanRow(it) {
  const row = document.createElement("div");
  row.className = "scan-row";
  const up = it.pct >= 0;
  row.innerHTML =
    `<span class="sr-name">${it.name}<span class="ov-code">${it.code}</span></span>` +
    `<span class="ov-price" style="color:${up ? "var(--bear)" : "var(--bull)"}">${it.close} (${up ? "+" : ""}${it.pct}%)</span>` +
    `<span class="sr-tags">${it.matched.map((m) => `<span class="scan-tag">${m}</span>`).join("")}</span>`;
  row.onclick = () => { switchView("stock"); selectStock(it.code); };
  return row;
}

let scanES = null;
function runScan() {
  const rules = [...document.querySelectorAll("#scan-rules input:checked")].map((i) => i.value);
  const scope = $("scan-scope").value;
  const box = $("scan-results");
  if (!rules.length) { box.innerHTML = '<div class="scan-msg">请至少勾选一个规则。</div>'; return; }
  if (scanES) scanES.close();  // 关闭上一次未结束的流
  busyOn($("scan-run"), "扫描中…");

  box.innerHTML = '<div class="scan-msg" id="scan-head">扫描中…<span id="scan-prog"></span></div><div id="scan-rows"></div>';
  const rows = $("scan-rows");
  let hits = 0;
  const url = `/api/scan/stream?rules=${encodeURIComponent(rules.join(","))}&scope=${encodeURIComponent(scope)}`;
  const es = new EventSource(url);
  scanES = es;

  es.addEventListener("progress", (e) => {
    const p = JSON.parse(e.data);
    const el = $("scan-prog");
    if (el) el.textContent = ` ${p.done}/${p.total}`;
  });
  es.addEventListener("hit", (e) => {
    hits++;
    rows.appendChild(buildScanRow(JSON.parse(e.data)));  // 边扫边出
  });
  es.addEventListener("done", (e) => {
    es.close(); scanES = null;
    busyOff($("scan-run"));
    const d = JSON.parse(e.data);
    const info = d.pool_total
      ? `池 ${d.pool_total} 只，实扫 ${d.scanned} 只${d.failed ? `（${d.failed} 只取数失败已跳过）` : ""}`
      : `扫描 ${d.scanned} 只`;
    const head = $("scan-head");
    if (head) head.textContent = d.count ? `${info}，命中 ${d.count} 只：` : `${info}，暂无同时满足所选规则的标的。`;
  });
  es.onerror = () => {
    es.close(); scanES = null;
    busyOff($("scan-run"));
    if (!hits) { const head = $("scan-head"); if (head) head.textContent = "扫描中断（连接错误），请重试。"; }
  };
}
$("scan-run").onclick = runScan;

/* ---------------- Chat ---------------- */
function addChatMsg(role, text, used) {
  const log = $("chat-log");
  const div = document.createElement("div");
  div.className = "chat-msg " + role;
  div.textContent = text;
  if (used && used.length) {
    const u = document.createElement("span");
    u.className = "used";
    u.textContent = "参考数据：" + used.map((x) => `${x.name}(${x.code})`).join("、");
    div.appendChild(u);
  }
  log.appendChild(div);
  div.scrollIntoView({ behavior: "smooth", block: "end" });
  return div;
}
const chatSources = new Set();
async function sendChat() {
  const input = $("chat-text");
  const q = input.value.trim();
  if (!q) return;
  input.value = "";
  const sources = [...chatSources];
  addChatMsg("user", q);
  const thinking = addChatMsg("bot", "思考中…");
  try {
    const d = await api("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, sources }),
    });
    thinking.textContent = d.text;
    const refs = [];
    if (d.used && d.used.length) refs.push(...d.used.map((x) => `${x.name}(${x.code})`));
    if (d.sources_used && d.sources_used.length) refs.push(...d.sources_used);
    if (refs.length) {
      const u = document.createElement("span");
      u.className = "used";
      u.textContent = "参考数据：" + refs.join("、");
      thinking.appendChild(u);
    }
  } catch (e) {
    thinking.textContent = "回答失败：" + e.message;
  }
}
$("chat-send").onclick = (e) => withBusy(e.currentTarget, sendChat, "发送中…");
$("chat-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter") withBusy($("chat-send"), sendChat, "发送中…");
});
document.querySelectorAll(".ctx-chip").forEach((chip) => {
  chip.onclick = () => {
    const src = chip.dataset.src;
    if (chatSources.has(src)) { chatSources.delete(src); chip.classList.remove("on"); }
    else { chatSources.add(src); chip.classList.add("on"); }
  };
});

/* ---------------- Backtest ---------------- */
let btChart = null;
function resetBacktest() {
  $("bt-summary").hidden = true;
  $("bt-chart").hidden = true;
  $("bt-trades").innerHTML = "";
}
async function initBacktestStrategies() {
  try {
    const { strategies } = await api("/api/backtest/strategies");
    $("bt-strategy").innerHTML = strategies
      .map((s) => `<option value="${s.id}" title="${s.desc}">${s.label}</option>`)
      .join("");
  } catch {}
}
function btMetric(label, val, cls) {
  return `<div class="bt-metric"><span class="bt-m-l">${label}</span><span class="bt-m-v ${cls || ""}">${val}</span></div>`;
}
function renderBacktest(d) {
  const sum = $("bt-summary");
  if (d.error) {
    sum.hidden = false;
    sum.innerHTML = `<div class="bt-err">回测失败：${d.error}</div>`;
    $("bt-chart").hidden = true;
    $("bt-trades").innerHTML = "";
    return;
  }
  const m = d.metrics;
  const sign = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
  sum.hidden = false;
  sum.innerHTML =
    `<div class="bt-head-line">${d.strategy_label}｜${d.start} ~ ${d.end}（${d.days} 个交易日）` +
    `<span class="bt-desc">${d.strategy_desc}</span></div>` +
    `<div class="bt-metrics">` +
    btMetric("策略收益", m.total_return + "%", sign(m.total_return)) +
    btMetric("买入持有", m.benchmark_return + "%", sign(m.benchmark_return)) +
    btMetric("超额", (m.excess > 0 ? "+" : "") + m.excess + "%", sign(m.excess)) +
    btMetric("年化", m.annualized + "%", sign(m.annualized)) +
    btMetric("最大回撤", m.max_drawdown + "%", "neg") +
    btMetric("胜率", m.win_rate == null ? "—" : m.win_rate + "%") +
    btMetric("交易次数", m.trades) +
    btMetric("持仓占比", m.exposure + "%") +
    btMetric("夏普", m.sharpe) +
    `</div>`;

  const box = $("bt-chart");
  box.hidden = false;
  if (!btChart) btChart = echarts.init(box, null, { renderer: "canvas" });
  btChart.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "axis" },
    legend: { data: ["策略", "买入持有"], textStyle: { color: "#9aa7b6" }, top: 0 },
    grid: { left: 48, right: 16, top: 28, bottom: 28 },
    xAxis: { type: "category", data: d.curve.map((c) => c.date),
      axisLine: { lineStyle: { color: "#232d3b" } }, axisLabel: { color: "#9aa7b6" } },
    yAxis: { type: "value", scale: true, axisLabel: { color: "#9aa7b6", formatter: (v) => v.toFixed(2) },
      splitLine: { lineStyle: { color: "#1a232f" } } },
    series: [
      { name: "策略", type: "line", showSymbol: false, smooth: true,
        data: d.curve.map((c) => c.eq), lineStyle: { width: 2, color: "#3b82f6" }, itemStyle: { color: "#3b82f6" } },
      { name: "买入持有", type: "line", showSymbol: false, smooth: true,
        data: d.curve.map((c) => c.bench), lineStyle: { width: 1.5, color: "#8b95a3" }, itemStyle: { color: "#8b95a3" } },
    ],
  }, true);
  btChart.resize();

  const trades = d.trades || [];
  $("bt-trades").innerHTML = trades.length
    ? `<table class="bt-tbl"><thead><tr><th>建仓</th><th>买入</th><th>平仓</th><th>卖出</th><th>收益</th><th>持有日</th></tr></thead><tbody>` +
      trades.map((t) =>
        `<tr><td>${t.entry_date}</td><td>${t.entry_price}</td><td>${t.exit_date}</td><td>${t.exit_price}</td>` +
        `<td class="${t.ret > 0 ? "pos" : t.ret < 0 ? "neg" : ""}">${t.ret > 0 ? "+" : ""}${t.ret}%</td><td>${t.bars}</td></tr>`
      ).join("") + `</tbody></table>`
    : `<p class="muted">该区间内策略未触发任何交易。</p>`;
}
async function runBacktest() {
  if (!currentCode) return;
  const strategy = $("bt-strategy").value || "composite";
  const days = $("bt-days").value || "250";
  try {
    const d = await api(`/api/stock/${currentCode}/backtest?strategy=${strategy}&days=${days}`);
    if (currentCode === d.code) renderBacktest(d);
  } catch (e) {
    $("bt-summary").hidden = false;
    $("bt-summary").innerHTML = `<div class="bt-err">回测失败：${e.message}</div>`;
  }
}
$("bt-run").onclick = (e) => withBusy(e.currentTarget, runBacktest, "回测中…");

/* ---------------- Boot ---------------- */
$("add-btn").onclick = () => currentCode && addWatch(currentCode);
initSearch();
initBacktestStrategies();
loadHealth();
loadWatchlist();
loadActiveModel();
