/* music-refill 前端：Dashboard → 歌单补歌 / 搜索补歌；全局任务栏常驻。无框架，纯 fetch。 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const fmtDur = (ms) => { const s = Math.round(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const QUALITY_RANK = { flac: 3, 320: 2, 128: 1 };
const QUALITY_LABEL = { flac: "FLAC", 320: "320kbps", 128: "128kbps" };

async function api(path, opts) {
  const resp = await fetch(path, opts);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const d = body.detail;
    const msg = typeof d === "string" ? d : d ? JSON.stringify(d).slice(0, 150) : resp.statusText;
    throw new Error(msg);
  }
  return body;
}

/* ---------- 视图与登录 ---------- */

let qrUnikey = null;
let qrTimer = null;
let currentView = "login";

function setView(name) {
  currentView = name;
  $("#login-card").classList.toggle("hidden", name !== "login");
  $("#dashboard-card").classList.toggle("hidden", name !== "dashboard");
  $$(".view").forEach((el) => el.classList.toggle("hidden", el.dataset.view !== name));
  $("#taskbar").classList.toggle("hidden", name === "login");
  const hash = { dashboard: "#/dashboard", playlist: "#/playlist", gaps: "#/gaps", search: "#/search", match: "#/match" }[name];
  if (hash && location.hash !== hash) history.replaceState(null, "", hash);
  window.scrollTo(0, 0);
}

function goDashboard() { setView("dashboard"); }

async function refreshStatus() {
  const st = await api("/api/status");
  const chip = $("#account");
  if (st.logged_in) {
    chip.textContent = `已登录：${st.nickname}`;
    chip.classList.add("on");
    $("#btn-logout").classList.remove("hidden");
    stopQrPolling();
    defaultQuality = st.config?.quality || "flac";
    $("#default-quality-label").textContent = QUALITY_LABEL[defaultQuality] || defaultQuality;
    const globalQuality = $("#search-quality");
    if (globalQuality && [...globalQuality.options].some((o) => o.value === defaultQuality)) {
      globalQuality.value = defaultQuality;
    }
    if (currentView === "login") setView("dashboard");
  } else {
    chip.textContent = "未登录";
    chip.classList.remove("on");
    $("#btn-logout").classList.add("hidden");
    setView("login");
    startQrLogin();
  }
}

async function logout() {
  try { await api("/api/logout", { method: "POST" }); } catch { /* 忽略 */ }
  await refreshStatus();
}

async function startQrLogin() {
  $("#qr-img").hidden = true;
  $("#qr-hint").textContent = "生成中…";
  try {
    const qr = await api("/api/login/qr/create", { method: "POST" });
    qrUnikey = qr.unikey;
    $("#qr-img").src = qr.qr_data_url;
    $("#qr-img").hidden = false;
    $("#qr-hint").textContent = "请用网易云音乐 App 扫码";
    qrTimer = setInterval(checkQr, 2000);
  } catch (e) {
    $("#qr-hint").textContent = `二维码生成失败：${e.message}`;
  }
}

async function checkQr() {
  if (!qrUnikey) return;
  let r;
  try {
    r = await api(`/api/login/qr/check?unikey=${encodeURIComponent(qrUnikey)}`);
  } catch (e) {
    $("#qr-hint").textContent = `轮询出错（自动重试中）：${e.message}`;
    return;
  }
  if (r.code === 802) $("#qr-hint").textContent = "已扫码，请在手机上确认";
  if (r.code === 8821) {
    stopQrPolling();
    $("#qr-hint").textContent = "网易风控（8821）：扫码被拦，请用上方「一键登录」或「手动 Cookie」。";
  }
  if (r.code === 800) { stopQrPolling(); $("#qr-hint").textContent = "二维码已过期，正在刷新…"; setTimeout(startQrLogin, 800); }
  if (r.code === 803) { $("#qr-hint").textContent = "登录成功！"; stopQrPolling(); await refreshStatus(); }
}

function stopQrPolling() { if (qrTimer) { clearInterval(qrTimer); qrTimer = null; } }

async function browserLogin() {
  const btn = $("#btn-browser-login"), hint = $("#browser-hint");
  btn.disabled = true;
  hint.textContent = "正在弹出浏览器窗口…请在新窗口里登录，成功后本页自动进入";
  try {
    const r = await api("/api/login/browser", { method: "POST" });
    hint.textContent = `登录成功（${r.nickname ?? ""}）`;
    await refreshStatus();
  } catch (e) {
    hint.textContent = `失败：${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

async function cookieLogin() {
  const raw = $("#cookie-input").value.trim();
  const hint = $("#cookie-hint");
  if (!raw) { hint.textContent = "请先粘贴 Cookie 串"; return; }
  try {
    await api("/api/login/cookie", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ raw }),
    });
    hint.textContent = "登录成功";
    await refreshStatus();
  } catch (e) {
    hint.textContent = `失败：${e.message}`;
  }
}

/* ---------- 歌单补歌 ---------- */

let defaultQuality = "flac";
let gapItems = [];
let matchItems = [];
let matchPage = 0;
let matchTargetCount = 0;
const MATCH_PAGE_SIZE = 10;
let matchPollTimer = null;

async function openPlaylist() {
  setView("playlist");
  await loadPlaylists();
}

async function loadPlaylists() {
  const box = $("#pl-list");
  box.textContent = "加载中…";
  try {
    const data = await api("/api/playlists");
    const fixed = new Set(data.fixed_playlist_ids || []);
    box.innerHTML = data.playlists.map((p) => `
      <label class="pl-item">
        <input type="checkbox" value="${p.id}" data-liked="${p.liked ? 1 : 0}"
          ${p.liked || fixed.has(p.id) ? "checked" : ""}>
        <span>${esc(p.name)}</span><span class="cnt">${p.count}首</span>
      </label>`).join("");
  } catch (e) {
    box.textContent = `加载失败：${e.message}`;
  }
}

async function doScan() {
  const boxes = [...document.querySelectorAll("#pl-list input:checked")];
  if (!boxes.length) { $("#scan-hint").textContent = "请至少勾选一个歌单"; return; }
  const likedCb = boxes.find((b) => b.dataset.liked === "1");
  const ids = boxes.filter((b) => b !== likedCb).map((b) => Number(b.value));
  const btn = $("#btn-scan");
  btn.disabled = true;
  $("#scan-hint").textContent = "扫描中…（歌单越大越慢）";
  try {
    const r = await api("/api/scan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ playlist_ids: ids, include_liked: !!likedCb }),
    });
    gapItems = r.gap || [];
    $("#scan-hint").textContent = "";
    renderGaps(r);
  } catch (e) {
    $("#scan-hint").textContent = `扫描失败：${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

function renderGaps(r) {
  setView("gaps");
  $("#res-summary").innerHTML = `
    <div class="stat"><b>${r.summary.total}</b>曲目总数</div>
    <div class="stat"><b>${r.summary.cloud_fuzzy}</b>云盘疑似已有</div>
    <div class="stat gap"><b>${r.summary.gap}</b>疑似缺失</div>`;
  const tb = $("#gap-table tbody");
  tb.innerHTML = gapItems.map((g, i) => {
    let ci = "—";
    if (g.cloud_info) {
      ci = `<span class="tag fuzzy">疑似</span><b>${esc(g.cloud_info.songName)}</b> / ${esc(g.cloud_info.artist)}
         <div class="muted small">${esc(g.cloud_info.file_name)} · ${fmtDur(g.cloud_info.duration_ms)} · ${g.cloud_info.bitrate || "?"}kbps · ${Math.round((g.cloud_info.file_size || 0) / 1048576 * 10) / 10}MB</div>`;
    }
    const skip = !!g.in_cloud;
    return `<tr>
      <td><input type="checkbox" class="gap-check" value="${i}" ${skip ? "" : "checked"}></td>
      <td>${i + 1}</td>
      <td><b>${esc(g.name)}</b></td>
      <td>${esc((g.artists || []).join("、"))}</td>
      <td>${esc(g.album || "")}</td>
      <td>${fmtDur(g.duration_ms)}</td>
      <td><span class="tag">${esc(g.reason || "")}</span>${g.in_cloud ? ' <span class="tag fuzzy">云盘疑似已有</span>' : ""}</td>
      <td>${ci}</td>
      <td>${esc((g.playlists || []).join("、"))}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="9" class="muted">没有需要补的歌</td></tr>';
  const skipN = gapItems.filter((g) => g.in_cloud).length;
  $("#gap-count").textContent = `共 ${gapItems.length} 首（${skipN} 首默认不选）`;
  $("#report-link").href = `/api/report/${r.report_file}`;
  updateGapSelection();
}

function updateGapSelection() {
  const n = $$(".gap-check:checked").length;
  $("#gap-hint").textContent = n ? `已选 ${n} 首` : "";
  $("#btn-start-match").disabled = !n;
}

async function startMatch() {
  const selected = $$(".gap-check:checked")
    .map((cb) => gapItems[Number(cb.value)])
    .filter(Boolean);
  if (!selected.length) { $("#gap-hint").textContent = "请先勾选要补的歌"; return; }
  matchTargetCount = selected.length;
  setView("match");
  matchItems = [];
  stopMatchPoll();
  matchPage = 0;
  $("#match-table tbody").innerHTML = "";
  $("#match-progress").textContent = `匹配中… 0/${selected.length}`;
  $("#match-current").textContent = "正在准备音源…";
  $("#match-fill").style.width = "2%";
  $("#match-hint").textContent = "";
  try {
    await api("/api/match/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ songs: selected, origin: "gap", quality: defaultQuality }),
    });
  } catch (e) {
    $("#match-progress").textContent = `启动失败：${e.message}`;
    return;
  }
  pollMatch();
}

function stopMatchPoll() {
  if (matchPollTimer) { clearTimeout(matchPollTimer); matchPollTimer = null; }
}

async function pollMatch() {
  stopMatchPoll();
  let r;
  try {
    r = await api(`/api/match/results?offset=${matchItems.length}&limit=10`);
  } catch (e) {
    $("#match-progress").textContent = `拉取结果失败：${e.message}（自动重试）`;
    matchPollTimer = setTimeout(pollMatch, 3000);
    return;
  }
  if (r.results?.length) {
    matchItems.push(...r.results);
    renderMatchPage();
  }
  const pct = r.total ? Math.round((r.done / r.total) * 100) : 0;
  $("#match-fill").style.width = `${r.running ? Math.max(pct, 2) : 100}%`;
  $("#match-progress").textContent =
    `已匹配 ${r.done}/${r.total}` + (r.running ? " · 后台仍在搜索…" : " · 全部匹配完成");
  const c = r.current || {};
  if (r.running && c.name) {
    const elapsed = c.started ? Math.max(0, Math.round(Date.now() / 1000 - c.started)) : 0;
    $("#match-current").textContent = `正在匹配第 ${c.index}/${r.total} 首：${c.name}${c.artist ? ` · ${c.artist}` : ""}（已用时 ${elapsed}s）`;
  } else if (r.running) {
    $("#match-current").textContent = "正在等待下一首…";
  } else {
    $("#match-current").textContent = "";
  }
  // 匹配期间固定 1 秒刷新，让“当前歌曲/已用时”持续走动；避免轮询间隔掩盖卡顿。
  if (r.running || matchItems.length < r.total) {
    matchPollTimer = setTimeout(pollMatch, 1000);
  }
}

function availableQualities(version) {
  return (version?.qualities || []).map((q) => q.quality);
}

function pickQuality(version, desired) {
  const available = availableQualities(version);
  if (!available.length) return "";
  if (available.includes(desired)) return desired;
  const want = QUALITY_RANK[desired] || 3;
  const higher = available.filter((q) => QUALITY_RANK[q] > want);
  if (higher.length) return higher.sort((a, b) => QUALITY_RANK[a] - QUALITY_RANK[b])[0];
  const lower = available.filter((q) => QUALITY_RANK[q] < want);
  if (lower.length) return lower.sort((a, b) => QUALITY_RANK[b] - QUALITY_RANK[a])[0];
  return available[0];
}

function qualityOptions(version, selected) {
  return availableQualities(version).map((q) => {
    const isDefault = q === defaultQuality;
    return `<option value="${q}" ${q === selected ? "selected" : ""}>${QUALITY_LABEL[q] || q}${isDefault ? "（默认）" : ""}</option>`;
  }).join("");
}

function qualityHint(version) {
  const available = availableQualities(version);
  if (!available.length) return "该版本无可用音质";
  if (available.length === 1) {
    const only = available[0];
    return only === defaultQuality
      ? `该版本仅提供 ${QUALITY_LABEL[only] || only}（默认音质）`
      : `该版本仅提供 ${QUALITY_LABEL[only] || only}；默认 ${QUALITY_LABEL[defaultQuality] || defaultQuality} 不可用`;
  }
  return "";
}

function versionSummaryHtml(v) {
  const qs = v.available_qualities?.map((q) => QUALITY_LABEL[q] || q).join("/") || "无音质";
  return `<span class="cand-main"><span class="cand-source">[${esc(v.source)}]</span> <b>${esc(v.title)}</b><span class="cand-artist"> · ${esc(v.artist || "未知歌手")}</span></span>
    <span class="cand-meta">${esc(v.album || "未知专辑")} · ${esc(qs)} · ${esc(fmtDur((v.duration_s || 0) * 1000))} · ${esc(v.score ?? 0)}分</span>`;
}

function versionPicker(versions, selectedIdx, idx) {
  return `<details class="cand-details" data-idx="${idx}">
    <summary class="cand-summary" title="展开并选择音源版本">${versionSummaryHtml(versions[selectedIdx])}</summary>
    <div class="cand-options" role="listbox">${versions.map((v, i) => `
      <button type="button" class="cand-option${i === selectedIdx ? " selected" : ""}" data-idx="${idx}" data-version="${i}">
        ${versionSummaryHtml(v)}
      </button>`).join("")}</div>
  </details>`;
}

function closeVersionPicker() {
  $$("#match-table .cand-details[open]").forEach((d) => { d.open = false; });
}

function placeVersionPicker(details) {
  const popover = details.querySelector(".cand-options");
  const anchor = details.querySelector(".cand-summary")?.getBoundingClientRect();
  if (!popover || !anchor) return;
  const gap = 6;
  const edge = 10;
  const viewportW = document.documentElement.clientWidth;
  const viewportH = document.documentElement.clientHeight;
  const width = Math.min(Math.max(anchor.width, 360), viewportW - edge * 2);
  popover.style.width = `${width}px`;
  popover.style.maxHeight = `${Math.min(320, viewportH - edge * 2)}px`;
  const height = Math.min(popover.scrollHeight, 320);
  const below = viewportH - anchor.bottom - gap - edge;
  const above = anchor.top - gap - edge;
  const openUp = below < Math.min(height, 180) && above > below;
  const top = openUp
    ? Math.max(edge, anchor.top - gap - height)
    : Math.min(anchor.bottom + gap, viewportH - edge - height);
  const left = Math.min(Math.max(edge, anchor.left), viewportW - edge - width);
  popover.style.top = `${top}px`;
  popover.style.left = `${left}px`;
  popover.classList.toggle("up", openUp);
}

function renderMatchPage() {
  const total = matchItems.length;
  const pages = Math.max(1, Math.ceil(total / MATCH_PAGE_SIZE));
  matchPage = Math.min(matchPage, pages - 1);
  const start = matchPage * MATCH_PAGE_SIZE;
  const rows = matchItems.slice(start, start + MATCH_PAGE_SIZE);
  rows.forEach((it) => {
    if (it._selectedVersion == null) it._selectedVersion = 0;
    if (it._selectedQuality == null) it._selectedQuality = pickQuality((it.versions || [])[0], defaultQuality);
    if (it._checked == null) it._checked = !it.song.in_cloud;
    if (it._queued == null) it._queued = false;
  });
  const tableBox = $("#match-table").closest(".table-box");
  const scrollTop = tableBox?.scrollTop;
  const openIdx = $("#match-table .cand-details[open]")?.dataset.idx;
  const tb = $("#match-table tbody");
  tb.innerHTML = rows.map((it, i) => {
    const g = it.song;
    const versions = it.versions || [];
    const idx = start + i;
    const versionIdx = it._selectedVersion || 0;
    const selectedQuality = it._selectedQuality || pickQuality(versions[versionIdx], defaultQuality);
    const check = versions.length
      ? `<input type="checkbox" class="match-check" data-idx="${idx}" ${it._checked ? "checked" : ""} ${it._queued ? "disabled" : ""}>`
      : '<input type="checkbox" disabled>';
    const cand = versions.length
      ? versionPicker(versions, versionIdx, idx)
      : '<span class="muted">无可用音源</span>';
    const quality = versions.length
      ? `<select class="quality" data-idx="${idx}" title="${esc(qualityHint(versions[versionIdx]))}">${qualityOptions(versions[versionIdx], selectedQuality)}</select>`
      : '<span class="muted">—</span>';
    return `<tr>
      <td>${check}</td>
      <td><b>${esc(g.name)}</b>
        <div class="muted small">${esc((g.artists || []).join("、"))} · ${esc(g.album || "")}</div>
        <div class="muted small">${esc(fmtDur(g.duration_ms))} · ${esc(g.reason || "")}${g.in_cloud ? " · 云盘疑似已有" : ""}</div></td>
      <td>${cand}</td>
      <td>${quality}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="4" class="muted">等待匹配结果…</td></tr>';
  if (openIdx != null) {
    document.querySelector(`#match-table .cand-details[data-idx="${openIdx}"]`)?.setAttribute("open", "");
  }
  requestAnimationFrame(() => {
    const open = $("#match-table .cand-details[open]");
    if (open) placeVersionPicker(open);
  });
  if (scrollTop != null && tableBox) tableBox.scrollTop = scrollTop;
  $("#page-label").textContent = `第 ${matchPage + 1}/${pages} 页 · 共 ${total} 首` + (total < matchTargetCount ? `（已匹配 ${total}/${matchTargetCount}）` : "");
  $("#btn-prev").disabled = matchPage <= 0;
  $("#btn-next").disabled = matchPage >= pages - 1;
  updateMatchHint();
}

function updateMatchHint() {
  const n = $$(".match-check:checked").length;
  $("#match-hint").textContent = n ? `本页已选 ${n} 首` : "";
}

function changeMatchVersion(idx, versionIdx) {
  const item = matchItems[idx];
  if (!item?.versions?.[versionIdx]) return;
  item._selectedVersion = versionIdx;
  const version = item.versions[versionIdx];
  const sel = document.querySelector(`#match-table .quality[data-idx="${idx}"]`);
  const quality = pickQuality(version, defaultQuality);
  item._selectedQuality = quality;
  if (sel) sel.innerHTML = qualityOptions(version, quality);
  const details = document.querySelector(`#match-table .cand-details[data-idx="${idx}"]`);
  if (details) {
    const summary = details.querySelector(".cand-summary");
    if (summary) summary.innerHTML = versionSummaryHtml(version);
    details.querySelectorAll(".cand-option").forEach((btn) => {
      btn.classList.toggle("selected", Number(btn.dataset.version) === versionIdx);
    });
    details.open = false;
  }
}

function changeMatchQuality(idx, quality) {
  const item = matchItems[idx];
  if (item) item._selectedQuality = quality;
}

async function queueMatchPage() {
  const hint = $("#match-hint");
  const rows = $$("#match-table .match-check:checked");
  if (!rows.length) { hint.textContent = "本页没有勾选歌曲"; return; }
  const items = [];
  for (const cb of rows) {
    const idx = Number(cb.dataset.idx);
    const item = matchItems[idx];
    const versionIdx = Number(item._selectedVersion ?? 0);
    const version = item.versions[versionIdx];
    const quality = document.querySelector(`#match-table .quality[data-idx="${idx}"]`)?.value || item._selectedQuality || "";
    const entry = version?.qualities?.find((q) => q.quality === quality);
    if (!entry) { hint.textContent = `《${item.song.name}》没有可用音质或未选择音源版本`; return; }
    items.push({ song: item.song, track: entry.track, quality });
  }
  const btn = $("#btn-queue-page");
  btn.disabled = true;
  try {
    const r = await api("/api/download", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items, origin: "gap" }),
    });
    rows.forEach((cb) => {
      const item = matchItems[Number(cb.dataset.idx)];
      if (item) { item._checked = false; item._queued = true; }
      cb.checked = false;
      cb.disabled = true;
    });
    hint.textContent = `已加入队列 ${r.queued} 首`;
    updateMatchHint();
    pollTasks();
  } catch (e) {
    hint.textContent = `入队失败：${e.message}`;
  }
  btn.disabled = false;
}

/* ---------- 搜索补歌 ---------- */

let searchVersions = [];
let searchPage = 0;
const SEARCH_PAGE_SIZE = 20;
let searchSelected = new Map(); // key -> {versionIdx, quality}
let searchToken = null;
let searchTimer = null;

async function openSearch() {
  setView("search");
}

async function doSourceSearch() {
  const keyword = $("#search-input").value.trim();
  const hint = $("#search-hint");
  if (!keyword) { hint.textContent = "请输入歌名"; return; }
  stopSearchPoll();
  $("#btn-search").disabled = true;
  hint.textContent = "搜索音源中…（先显示已返回的音源）";
  try {
    const r = await api("/api/source-search", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keyword, limit: 20 }),
    });
    searchVersions = r.versions || [];
    searchToken = r.token || null;
    searchPage = 0;
    searchSelected.clear();
    renderSearchPage();
    hint.textContent = `已返回 ${searchVersions.length} 个版本` + (r.pending ? " · 其余音源搜索中…" : "") + (r.errors?.length ? ` · ${r.errors.length} 个音源失败` : "");
    if (r.pending && searchToken) searchTimer = setTimeout(() => pollSearch(searchToken), 1200);
  } catch (e) {
    hint.textContent = `搜索失败：${e.message}`;
  } finally {
    $("#btn-search").disabled = false;
  }
}

function stopSearchPoll() {
  if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
}

async function pollSearch(token) {
  searchTimer = null;
  if (token !== searchToken) return;
  try {
    const r = await api("/api/source-search/results", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (token !== searchToken) return;
    // 慢源结果只会追加；保留已勾选状态（以版本内容 key 而不是数组下标关联）。
    const previouslySelected = new Map(searchSelected);
    searchVersions = r.versions || [];
    searchSelected.clear();
    previouslySelected.forEach((value, key) => {
      const idx = searchVersions.findIndex((v) => searchKey(v) === key);
      if (idx >= 0) searchSelected.set(key, { versionIdx: idx, quality: value.quality });
    });
    renderSearchPage();
    $("#search-hint").textContent = `已返回 ${searchVersions.length} 个版本`
      + (r.done ? " · 搜索完成" : " · 其余音源搜索中…")
      + (r.errors?.length ? ` · ${r.errors.length} 个音源失败` : "");
    if (!r.done) searchTimer = setTimeout(() => pollSearch(token), 1200);
  } catch (e) {
    $("#search-hint").textContent = `合并慢音源失败：${e.message}（已显示现有结果）`;
  }
}

function searchKey(v) {
  return `${v.source}\0${v.title}\0${v.artist}\0${v.album}\0${v.duration_s}`;
}

function renderSearchPage() {
  const total = searchVersions.length;
  const pages = Math.max(1, Math.ceil(total / SEARCH_PAGE_SIZE));
  searchPage = Math.min(searchPage, pages - 1);
  const start = searchPage * SEARCH_PAGE_SIZE;
  const rows = searchVersions.slice(start, start + SEARCH_PAGE_SIZE);
  const tb = $("#search-table tbody");
  tb.innerHTML = rows.map((v, i) => {
    const idx = start + i;
    const key = searchKey(v);
    const selected = searchSelected.get(key);
    const quality = selected?.quality || pickQuality(v, $("#search-quality").value || defaultQuality);
    return `<tr>
      <td><input type="checkbox" class="search-check" data-idx="${idx}" ${selected ? "checked" : ""}></td>
      <td><b>${esc(v.title)}</b>
        <div class="muted small">${esc(v.artist)} · ${esc(v.album || "未知专辑")}</div>
        <div class="muted small">[${esc(v.source)}] · ${esc(fmtDur(v.duration_s * 1000))}</div></td>
      <td><select class="quality" data-idx="${idx}" title="${esc(qualityHint(v))}">${qualityOptions(v, quality)}</select></td>
    </tr>`;
  }).join("") || '<tr><td colspan="3" class="muted">没有候选</td></tr>';
  $("#search-page-label").textContent = `第 ${searchPage + 1}/${pages} 页 · 共 ${total} 个版本`;
  $("#btn-search-prev").disabled = searchPage <= 0;
  $("#btn-search-next").disabled = searchPage >= pages - 1;
  $("#btn-apply-quality").disabled = !rows.length;
  updateSearchSelection();
}

function updateSearchSelection() {
  const n = searchSelected.size;
  $("#btn-queue-search").disabled = !n;
  $("#btn-queue-search").textContent = n ? `加入下载队列（已选 ${n}）` : "加入下载队列";
}

function toggleSearchSelection(idx, checked) {
  const v = searchVersions[idx];
  if (!v) return;
  const key = searchKey(v);
  if (checked) {
    searchSelected.set(key, { versionIdx: idx, quality: pickQuality(v, $("#search-quality").value || defaultQuality) });
  } else {
    searchSelected.delete(key);
  }
  updateSearchSelection();
}

async function queueSearch() {
  const hint = $("#search-hint");
  const items = [];
  for (const [key, { versionIdx, quality }] of searchSelected.entries()) {
    const v = searchVersions.find((item) => searchKey(item) === key) || searchVersions[versionIdx];
    const entry = v?.qualities?.find((q) => q.quality === quality);
    if (!entry) continue;
    const song = v.suggested_song || { id: externalId(v), name: v.title, artists: [v.artist], album: v.album, duration_ms: (v.duration_s || 0) * 1000 };
    items.push({ song, track: entry.track, quality });
  }
  if (!items.length) { hint.textContent = "请先勾选候选并选择音质"; return; }
  const btn = $("#btn-queue-search");
  btn.disabled = true;
  try {
    const r = await api("/api/download", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items, origin: "search" }),
    });
    hint.textContent = `已加入队列 ${r.queued} 首`;
    searchSelected.clear();
    stopSearchPoll();
    searchToken = null;
    renderSearchPage();
    pollTasks();
  } catch (e) {
    hint.textContent = `入队失败：${e.message}`;
    btn.disabled = false;
  }
}

function externalId(v) {
  let h = 0;
  const s = `${v.title}\0${v.artist}\0${v.album || ""}`;
  for (let i = 0; i < s.length; i++) h = (Math.imul(h, 31) + s.charCodeAt(i)) | 0;
  return -Math.abs(h || 1);
}

/* ---------- 全局任务栏 ---------- */

let taskbarOpen = false;
let stageStart = null;

function toggleTaskbar() {
  taskbarOpen = !taskbarOpen;
  $("#taskbar-detail").classList.toggle("hidden", !taskbarOpen);
  $("#taskbar-toggle").setAttribute("aria-expanded", String(taskbarOpen));
}

async function pollTasks() {
  let p;
  try {
    p = await api("/api/tasks");
  } catch {
    return;
  }
  const c = p.running ? p.current : null;
  let main = "暂无后台任务";
  let pct = 0;
  if (c) {
    const now = Date.now();
    if (!stageStart || stageStart.name !== c.name || stageStart.stage !== c.stage) {
      stageStart = { name: c.name, stage: c.stage, t: now };
    }
    if (c.recv != null && c.total) {
      pct = Math.min(100, Math.round((c.recv / c.total) * 100));
      main = `${c.stage}：${c.name}（${pct}%）`;
    } else {
      pct = c.total ? 100 : 0;
      main = `${c.stage}：${c.name}`;
    }
  } else if (p.match?.running) {
    main = `匹配中：${p.match.done}/${p.match.total}`;
  } else if (p.queue_len > 0) {
    main = `排队中：${p.queue_len} 首`;
  } else if (p.done > 0) {
    main = `空闲 · 已完成 ${p.done} 首`;
  }
  $("#taskbar-main").textContent = main;
  $("#taskbar-meta").textContent = `✓${p.ok} ✗${p.failed} · 队列 ${p.queue_len}`;
  $("#taskbar-fill").style.width = `${pct}%`;
  $("#taskbar-results").innerHTML = (p.recent || []).map((r) => r.status === "ok"
    ? `<div>✓ ${esc(r.name)}（${esc(r.quality_label || r.quality || "")}${r.instant ? " · 秒传" : " · 已上传"}${r.verified ? " · 已验证" : ""}）</div>`
    : `<div class="tag">✗ ${esc(r.name)}：${esc(r.detail || "失败")}</div>`).join("") || '<div class="muted">暂无最近结果</div>';
}

async function clearCache() {
  const hint = $("#clear-hint");
  if (!confirm("删除 data/downloads 下所有已下载的歌曲文件？（不影响已上传到云盘的内容）")) return;
  try {
    const r = await api("/api/downloads/clear", { method: "POST" });
    hint.textContent = `已清除 ${r.removed} 个文件，释放 ${r.freed_mb}MB`;
  } catch (e) {
    hint.textContent = `失败：${e.message}`;
  }
}

async function retryFailed() {
  const hint = $("#clear-hint");
  try {
    const r = await api("/api/jobs/retry?limit=20", { method: "POST" });
    hint.textContent = r.requeued ? `已重新入队 ${r.requeued} 首失败任务` : "没有可重试的失败任务";
  } catch (e) {
    hint.textContent = `重试失败：${e.message}`;
  }
}

/* ---------- 绑定 ---------- */

$("#brand").addEventListener("click", goDashboard);
$$("[data-go]").forEach((el) => el.addEventListener("click", () => {
  if (el.dataset.go === "dashboard") goDashboard();
  if (el.dataset.go === "playlist") openPlaylist();
  if (el.dataset.go === "gaps") setView("gaps");
  if (el.dataset.go === "search") openSearch();
}));
$("#btn-browser-login").addEventListener("click", browserLogin);
$("#btn-cookie-login").addEventListener("click", cookieLogin);
$("#btn-logout").addEventListener("click", logout);
$("#btn-scan").addEventListener("click", doScan);
$("#btn-start-match").addEventListener("click", startMatch);
$("#btn-sel-all").addEventListener("click", () => { $$(".gap-check").forEach((c) => { c.checked = true; }); updateGapSelection(); });
$("#btn-sel-none").addEventListener("click", () => { $$(".gap-check").forEach((c) => { c.checked = false; }); updateGapSelection(); });
$("#btn-search").addEventListener("click", doSourceSearch);
$("#search-input").addEventListener("keydown", (e) => { if (e.key === "Enter") doSourceSearch(); });
$("#btn-prev").addEventListener("click", () => { matchPage--; renderMatchPage(); });
$("#btn-next").addEventListener("click", () => { matchPage++; renderMatchPage(); });
$("#btn-page-all").addEventListener("click", () => {
  $$(".match-check").forEach((c) => {
    if (c.disabled) return;
    c.checked = true;
    const item = matchItems[Number(c.dataset.idx)];
    if (item) item._checked = true;
  });
  updateMatchHint();
});
$("#btn-page-none").addEventListener("click", () => {
  $$(".match-check").forEach((c) => {
    c.checked = false;
    const item = matchItems[Number(c.dataset.idx)];
    if (item) item._checked = false;
  });
  updateMatchHint();
});
$("#btn-queue-page").addEventListener("click", queueMatchPage);
$("#btn-search-prev").addEventListener("click", () => { searchPage--; renderSearchPage(); });
$("#btn-search-next").addEventListener("click", () => { searchPage++; renderSearchPage(); });
$("#btn-search-page-all").addEventListener("click", () => {
  const start = searchPage * SEARCH_PAGE_SIZE;
  searchVersions.slice(start, start + SEARCH_PAGE_SIZE).forEach((v, i) => {
    searchSelected.set(searchKey(v), { versionIdx: start + i, quality: pickQuality(v, $("#search-quality").value || defaultQuality) });
  });
  renderSearchPage();
});
$("#btn-search-page-none").addEventListener("click", () => { searchSelected.clear(); renderSearchPage(); });
$("#btn-queue-search").addEventListener("click", queueSearch);
$("#btn-apply-quality").addEventListener("click", () => {
  const q = $("#search-quality").value;
  const start = searchPage * SEARCH_PAGE_SIZE;
  searchVersions.slice(start, start + SEARCH_PAGE_SIZE).forEach((v, i) => {
    const idx = start + i;
    const key = searchKey(v);
    if (searchSelected.has(key)) searchSelected.set(key, { versionIdx: idx, quality: pickQuality(v, q) });
  });
  renderSearchPage();
});
$("#taskbar-toggle").addEventListener("click", toggleTaskbar);
$("#btn-clear-cache").addEventListener("click", clearCache);
$("#btn-retry-failed").addEventListener("click", retryFailed);
document.addEventListener("change", (e) => {
  if (e.target.classList?.contains("gap-check")) updateGapSelection();
  if (e.target.classList?.contains("match-check")) {
    const item = matchItems[Number(e.target.dataset.idx)];
    if (item) item._checked = e.target.checked;
    updateMatchHint();
  }
  if (e.target.classList?.contains("search-check")) toggleSearchSelection(Number(e.target.dataset.idx), e.target.checked);
  if (e.target.classList?.contains("quality") && e.target.closest("#match-table")) {
    changeMatchQuality(Number(e.target.dataset.idx), e.target.value);
  }
  if (e.target.classList?.contains("quality") && e.target.closest("#search-table")) {
    const idx = Number(e.target.dataset.idx);
    const v = searchVersions[idx];
    if (!v) return;
    const key = searchKey(v);
    const prev = searchSelected.get(key);
    searchSelected.set(key, { versionIdx: idx, quality: e.target.value });
    updateSearchSelection();
  }
});

document.addEventListener("click", (e) => {
  const option = e.target.closest(".cand-option");
  if (option) {
    changeMatchVersion(Number(option.dataset.idx), Number(option.dataset.version));
    return;
  }
  if (!e.target.closest(".cand-details")) {
    $$("#match-table .cand-details[open]").forEach((d) => { d.open = false; });
  }
});

document.addEventListener("toggle", (e) => {
  if (e.target.classList?.contains("cand-details") && e.target.open) {
    $$("#match-table .cand-details[open]").forEach((d) => { if (d !== e.target) d.open = false; });
    placeVersionPicker(e.target);
  }
}, true);

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeVersionPicker();
});

$("#match-table")?.closest(".table-box")?.addEventListener("scroll", closeVersionPicker);
window.addEventListener("resize", () => {
  const open = $("#match-table .cand-details[open]");
  if (open) placeVersionPicker(open);
});

setInterval(pollTasks, 1000);
pollTasks();
refreshStatus();
