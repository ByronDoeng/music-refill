/* music-refill 前端逻辑：登录 → 扫描 → 缺口清单（可勾选）→ 后台匹配（分批拉取）→ 人工确认（下拉选候选）→ 下载。无框架，纯 fetch。 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const fmtDur = (ms) => { const s = Math.round(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

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

/* ---------- 状态与登录 ---------- */

let qrUnikey = null;
let qrTimer = null;

async function refreshStatus() {
  const st = await api("/api/status");
  const chip = $("#account");
  if (st.logged_in) {
    chip.textContent = `已登录：${st.nickname}`;
    chip.classList.add("on");
    $("#btn-logout").classList.remove("hidden");
    stopQrPolling();
    $("#login-card").classList.add("hidden");
    await loadPlaylists();
  } else {
    chip.textContent = "未登录";
    chip.classList.remove("on");
    $("#btn-logout").classList.add("hidden");
    $("#pl-card").classList.add("hidden");
    $("#gap-card").classList.add("hidden");
    $("#match-card").classList.add("hidden");
    startQrLogin();
  }
}

async function logout() {
  try { await api("/api/logout", { method: "POST" }); } catch { /* 忽略 */ }
  await refreshStatus();
}

async function startQrLogin() {
  $("#login-card").classList.remove("hidden");
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

/* ---------- 一键浏览器登录 / 手动 Cookie ---------- */

async function browserLogin() {
  const btn = $("#btn-browser-login"), hint = $("#browser-hint");
  btn.disabled = true;
  hint.textContent = "正在弹出浏览器窗口…请在新窗口里登录（扫码/密码均可），成功后本页自动进入";
  try {
    const r = await api("/api/login/browser", { method: "POST" });
    hint.textContent = `登录成功 ✓（${r.nickname ?? ""}）`;
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
    hint.textContent = "登录成功 ✓";
    await refreshStatus();
  } catch (e) {
    hint.textContent = `失败：${e.message}`;
  }
}

/* ---------- ② 歌单 ---------- */

async function loadPlaylists() {
  $("#pl-card").classList.remove("hidden");
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

/* ---------- ③ 扫描 → 缺口清单 ---------- */

let gapItems = [];

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
    renderGaps(r);
    $("#scan-hint").textContent = "";
  } catch (e) {
    $("#scan-hint").textContent = `扫描失败：${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

function renderGaps(r) {
  $("#pl-card").classList.add("hidden");
  $("#match-card").classList.add("hidden");
  $("#gap-card").classList.remove("hidden");
  $("#res-summary").innerHTML = `
    <div class="stat"><b>${r.summary.total}</b>曲目总数</div>
    <div class="stat"><b>${r.summary.cloud_fuzzy}</b>云盘疑似已有</div>
    <div class="stat gap"><b>${r.summary.gap}</b>疑似缺失</div>`;
  gapItems = r.gap;
  const tb = $("#gap-table tbody");
  tb.innerHTML = r.gap.map((g, i) => {
    let ci = "—";
    if (g.cloud_info) {
      ci = `<span class="tag fuzzy">疑似</span><b>${esc(g.cloud_info.songName)}</b> / ${esc(g.cloud_info.artist)}
         <div class="muted small">${esc(g.cloud_info.file_name)} · ${fmtDur(g.cloud_info.duration_ms)} · ${g.cloud_info.bitrate || "?"}kbps · ${Math.round((g.cloud_info.file_size || 0) / 1048576 * 10) / 10}MB</div>`;
    }
    return `
    <tr>
      <td><input type="checkbox" class="gap-check" value="${g.id}" ${g.in_cloud ? "" : "checked"}></td>
      <td>${i + 1}</td>
      <td><b>${esc(g.name)}</b></td>
      <td>${esc(g.artists.join("、"))}</td>
      <td>${esc(g.album)}</td>
      <td>${fmtDur(g.duration_ms)}</td>
      <td><span class="tag">${esc(g.reason)}</span></td>
      <td>${ci}</td>
      <td>${esc(g.playlists.join("、"))}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="9" class="muted">没有需要补的歌 🎉</td></tr>';
  const skipN = r.gap.filter((g) => g.in_cloud).length;
  $("#gap-count").textContent = `共 ${r.gap.length} 首疑似缺失（含 ${skipN} 首云盘疑似已有，默认不选）`;
  $("#btn-start-match").disabled = !r.gap.length;
  $("#report-link").href = `/api/report/${r.report_file}`;
  window.scrollTo(0, 0);
}

function updateSelCount() {
  const n = document.querySelectorAll(".gap-check:checked").length;
  $("#gap-hint").textContent = n ? `已选 ${n} 首` : "";
  $("#btn-start-match").disabled = !n;
}

/* ---------- ④ 后台匹配 + 分批确认（每屏 10 首） ---------- */

let matchResults = [];   // 服务器返回的全部结果（按序）
let rendered = 0;        // 已从服务器拉取的数量
let windowStart = 0;     // 当前确认窗口起点
const WINDOW = 10;

async function startMatch() {
  const selected = [...document.querySelectorAll(".gap-check:checked")]
    .map((cb) => gapItems.find((g) => g.id === Number(cb.value)))
    .filter(Boolean);
  if (!selected.length) { $("#gap-hint").textContent = "请至少选择一首"; return; }
  $("#btn-start-match").disabled = true;
  $("#gap-hint").textContent = `已提交 ${selected.length} 首，后台匹配中…`;
  try {
    await api("/api/match/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ songs: selected }),
    });
    matchResults = [];
    rendered = 0;
    windowStart = 0;
    windowRendered = 0;
    $("#match-table tbody").innerHTML = "";
    $("#gap-card").classList.add("hidden");
    $("#match-card").classList.remove("hidden");
    $("#btn-download").disabled = true;
    $("#match-progress").textContent = `匹配中… 0/${selected.length}`;
    window.scrollTo(0, 0);
    setTimeout(pollMatch, 1500);
  } catch (e) {
    $("#gap-hint").textContent = `启动失败：${e.message}`;
    $("#btn-start-match").disabled = false;
  }
}

async function pollMatch() {
  let r;
  try {
    r = await api(`/api/match/results?offset=${rendered}&limit=50`);
  } catch (e) {
    $("#match-progress").textContent = `拉取结果失败：${e.message}（自动重试）`;
    setTimeout(pollMatch, 3000);
    return;
  }
  matchResults.push(...r.results);
  rendered = r.offset;
  appendNewRows();          // 只追加新行，绝不重渲染已显示的行（保住打开中的下拉框）
  if (r.running || rendered < r.total) setTimeout(pollMatch, 2000);
}

function renderWindow() {
  const tb = $("#match-table tbody");
  tb.innerHTML = "";
  windowRendered = windowStart;
  appendNewRows();
  window.scrollTo(0, 0);
}

let windowRendered = 0;    // 当前窗口内已渲染的绝对行号

function appendNewRows() {
  const tb = $("#match-table tbody");
  const end = Math.min(windowStart + WINDOW, matchResults.length);
  while (windowRendered < end) {
    tb.insertAdjacentHTML("beforeend", matchRow(matchResults[windowRendered], windowRendered));
    windowRendered++;
  }
  const total = Math.max(rendered, matchResults.length);
  $("#match-progress").textContent =
    `后台进度：${Math.min(rendered, total)}/${total} 已匹配 · 本批显示第 ${windowStart + 1}–${end} 首` +
    (rendered < total ? " · 后台仍在搜索…" : " · 全部匹配完成");
  if (tb.querySelector('input[type="checkbox"]:checked')) $("#btn-download").disabled = false;
}

function matchRow(it, idx) {
  const s = it.song;
  const songCell = `<b>${esc(s.name)}</b>
    <div class="muted small">${esc(s.artists.join("、"))} · ${esc(s.album)}</div>
    <div class="muted small">${fmtDur(s.duration_ms)} · ${esc(s.reason)}</div>`;
  let candCell, checkCell;
  if (!it.candidates.length) {
    checkCell = '<input type="checkbox" disabled>';
    candCell = '<span class="muted">未找到候选（自动跳过）</span>';
  } else {
    const opts = it.candidates.map((c, j) => {
      const mb = Math.round((c.size_bytes / 1048576) * 10) / 10;
      return `<option value="${j}">[${esc(c.source)}] ${esc(c.title)} - ${esc(c.artist)} · ${esc(c.album)}（${esc(c.ext)} ${mb}MB · ${fmtDur(c.duration_s * 1000)} · ${c.score}分）</option>`;
    }).join("");
    checkCell = `<input type="checkbox" class="m-check" data-idx="${idx}" checked>`;
    candCell = `<select class="cand" data-idx="${idx}">${opts}</select>
      <div class="muted small">${it.auto ? "默认最高分，可下拉切换" : "低于自动推荐分，请人工核对"}</div>`;
  }
  return `<tr><td>${checkCell}</td><td>${songCell}</td><td>${candCell}</td></tr>`;
}

function advanceWindow() {
  windowStart += WINDOW;
  renderWindow();
  if (windowRendered < windowStart) {
    // 窗口推进到还没有结果的区间：轮询会补齐
    setTimeout(pollMatch, 1500);
  }
  window.scrollTo(0, 0);
}

/* ---------- ⑤ 后台下载/上传队列 ---------- */

async function downloadSelected() {
  const hint = $("#match-hint");
  const items = [...document.querySelectorAll("#match-table .m-check:checked")]
    .map((cb) => {
      const idx = Number(cb.dataset.idx);
      const it = matchResults[idx];
      const sel = document.querySelector(`#match-table select.cand[data-idx="${idx}"]`);
      return it && sel ? { song: it.song, track: it.candidates[Number(sel.value)] } : null;
    })
    .filter(Boolean);
  if (!items.length) { hint.textContent = "本批没有勾选的歌曲"; return; }
  try {
    const r = await api("/api/download", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    hint.textContent = `已入队 ${r.queued} 首（后台执行中，队列共 ${r.queue_len}），自动进入下一批`;
    advanceWindow();   // 立即切到下一批筛选，后台自己跑
  } catch (e) {
    hint.textContent = `入队失败：${e.message}`;
  }
}

/* ---------- 后台进度轮询 ---------- */

let prevProg = null;   // 上一次采样 {name, stage, recv, t}，用于算速度
let stageStart = null; // 当前阶段起始时刻，用于计算已用时

async function pollProgress() {
  let p;
  try {
    p = await api("/api/download/progress");
  } catch {
    return;
  }
  const card = $("#progress-card");
  const active = p.running || p.queue_len > 0;
  if (active || p.done > 0) card.classList.remove("hidden");
  let pct = 0, txt = "", speedTxt = "";
  const c = p.running ? p.current : null;
  if (c) {
    const now = Date.now();
    if (!stageStart || stageStart.name !== c.name || stageStart.stage !== c.stage) {
      stageStart = { name: c.name, stage: c.stage, t: now };
    }
    const elapsed = Math.round((now - stageStart.t) / 1000);
    if (prevProg && prevProg.name === c.name && prevProg.stage === c.stage
        && c.recv != null && prevProg.recv != null && c.recv > prevProg.recv) {
      const dt = (now - prevProg.t) / 1000;
      if (dt > 0) speedTxt = ` · ${Math.round((c.recv - prevProg.recv) / dt / 1048576 * 100) / 100} MB/s`;
    }
    prevProg = c.recv != null ? { name: c.name, stage: c.stage, recv: c.recv, t: now } : null;
    if (c.recv != null && c.total) {
      pct = Math.min(100, Math.round((c.recv / c.total) * 100));
      txt = `${c.stage}：${c.name}（${pct}%，${Math.round(c.recv / 1048576 * 10) / 10}/${Math.round(c.total / 1048576 * 10) / 10}MB${speedTxt}）`;
    } else if (c.total) {
      // 上传云盘（整块发送，无逐字节进度）：显示大小与已用时
      txt = `${c.stage}：${c.name}（${Math.round(c.total / 1048576 * 10) / 10}MB · 已用时 ${elapsed}s，视带宽约 10~90s）`;
      pct = 100;
    } else {
      txt = `${c.stage}：${c.name}（已用时 ${elapsed}s…）`;
    }
  } else if (p.queue_len > 0) {
    txt = "准备下一首…";
    prevProg = null;
  } else if (p.done > 0) {
    txt = `空闲 · 累计完成 ${p.done} 首（成功 ${p.ok} / 失败 ${p.failed}）`;
    prevProg = null;
  } else {
    txt = "空闲";
    prevProg = null;
  }
  $("#prog-fill").style.width = (c ? Math.max(pct, 4) : active ? 100 : pct) + "%";
  $("#prog-text").textContent = txt + ` · 已完成 ${p.done}（✓${p.ok} ✗${p.failed}） · 队列 ${p.queue_len}`;
  $("#prog-results").innerHTML = p.results.map((r) => r.status === "ok"
    ? `<div>✓ ${esc(r.name)} → 云盘 ${r.cloud_song_id}（${esc(r.quality)}${r.instant ? " · 秒传" : " · 真实上传"}${r.matched_library ? " · 已匹配曲库" : " · 私有版本"}）</div>`
    : `<div class="tag">✗ ${esc(r.name)}：${esc(r.detail || "失败")}</div>`).join("");
}

setInterval(pollProgress, 1000);

/* ---------- 绑定 ---------- */

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

$("#btn-scan").addEventListener("click", doScan);
$("#btn-clear-cache").addEventListener("click", clearCache);
$("#btn-browser-login").addEventListener("click", browserLogin);
$("#btn-cookie-login").addEventListener("click", cookieLogin);
$("#btn-logout").addEventListener("click", logout);
$("#btn-start-match").addEventListener("click", startMatch);
$("#btn-download").addEventListener("click", downloadSelected);
$("#btn-skip").addEventListener("click", advanceWindow);
document.addEventListener("change", (e) => {
  if (e.target.classList?.contains("gap-check")) updateSelCount();
});
$("#btn-sel-all").addEventListener("click", () => { document.querySelectorAll(".gap-check").forEach((c) => (c.checked = true)); updateSelCount(); });
$("#btn-sel-none").addEventListener("click", () => { document.querySelectorAll(".gap-check").forEach((c) => (c.checked = false)); updateSelCount(); });
refreshStatus();
