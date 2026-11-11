const state = {
  libraries: [],
  runId: null,
  pollTimer: null,
  page: 1,
  // 卡片墙固定每页 15 部
  pageSize: 15,
  filters: { library_id: "", q: "", include_removed: false, scan_state: "" },
  settings: null,
  status: null,
  view: "main",
  // 日志：记住最后一条 seq，每次只拉增量
  logs: [],
  logSeq: 0,
  logFilter: "all",
  logTimer: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!resp.ok) {
    const msg = (data && (data.detail || data.message)) || `HTTP ${resp.status}`;
    throw new Error(msg);
  }
  return data;
}

function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", { hour12: false });
}

function fmtDateShort(iso) {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  if (Number.isNaN(d.getTime())) return "";
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/* ---------------------------------------------------------------- 视图 */

function switchView(view) {
  state.view = view;
  // 控制台是「左内容 + 右日志栏」的一个整体，切到设置时整块收起
  $("#workspace").hidden = view !== "main";
  $("#view-settings").hidden = view !== "settings";
  $$(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  if (view === "settings") loadSettings();
  if (view === "main") {
    loadLibraries();
    loadStats();
    loadMedia(state.page);
  }
}

/* ---------------------------------------------------------------- 健康 */

async function loadHealth() {
  const badge = $("#health");
  try {
    const h = await api("/api/health");
    $("#server-label").textContent = h.base_url || "";
    if (!h.configured) {
      badge.className = "badge";
      badge.textContent = "未配置 Emby";
    } else if (h.emby_ok) {
      badge.className = "badge ok";
      badge.textContent = `已连接 · ${h.emby}`;
    } else {
      badge.className = "badge err";
      badge.textContent = "连接失败";
      badge.title = h.emby || "";
    }
  } catch (e) {
    badge.className = "badge err";
    badge.textContent = "服务异常";
    badge.title = e.message;
  }
}

/* ---------------------------------------------------------------- 媒体库 */

const TYPE_LABEL = { tvshows: "剧集", movies: "电影" };
const typeLabel = (t) => TYPE_LABEL[(t || "").toLowerCase()] || "混合";

function buildLibraryRow(lib, onToggle) {
  const row = el("label", "lib-row");
  row.classList.toggle("checked", lib.enabled);
  row.classList.toggle("absent", !lib.present);

  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = lib.enabled;
  cb.disabled = !lib.present;
  cb.addEventListener("change", () => onToggle(lib, cb));

  const info = el("div");
  const nameLine = el("div", "name");
  nameLine.appendChild(el("span", "", lib.name));
  nameLine.appendChild(el("span", "tag", typeLabel(lib.collection_type)));
  if (!lib.present) nameLine.appendChild(el("span", "tag warn", "已在 Emby 移除"));
  info.appendChild(nameLine);
  info.appendChild(
    el(
      "div",
      "meta",
      lib.last_scan_at ? `上次扫描 ${fmtTime(lib.last_scan_at)}` : "尚未扫描"
    )
  );

  row.append(cb, info, el("div", "count", `${lib.item_count || 0} 条`));
  return row;
}

function renderLibraries() {
  const box = $("#lib-list");
  if (!box) return;
  box.innerHTML = "";
  if (!state.libraries.length) {
    const hint =
      state.status && state.status.configured
        ? "还没有媒体库，点右上角「同步库列表」。"
        : "尚未连接 Emby，请先在上方填写地址与 API Key。";
    box.appendChild(el("div", "empty", hint));
    updateScanScope();
    return;
  }
  for (const lib of state.libraries) {
    box.appendChild(buildLibraryRow(lib, toggleLibrary));
  }
  updateScanScope();
}

async function loadLibraries() {
  if (!state.status || !state.status.configured) {
    state.libraries = [];
    renderLibraries();
    return;
  }
  try {
    const data = await api("/api/libraries");
    state.libraries = data.items;
  } catch (e) {
    state.libraries = [];
    const box = $("#lib-list");
    if (box) {
      box.innerHTML = "";
      box.appendChild(el("div", "err-text", "读取媒体库失败：" + e.message));
    }
    return;
  }
  renderLibraries();
  refreshLibraryFilter();
}

async function toggleLibrary(lib, checkbox) {
  checkbox.disabled = true;
  try {
    const updated = await api(`/api/libraries/${lib.id}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled: checkbox.checked }),
    });
    Object.assign(lib, updated);
    renderLibraries();
    await loadStatus();
  } catch (e) {
    alert("保存失败：" + e.message);
    checkbox.checked = !checkbox.checked;
    checkbox.disabled = false;
  }
}

async function refreshLibraries() {
  const btn = $("#btn-refresh");
  btn.disabled = true;
  btn.textContent = "同步中…";
  try {
    const r = await api("/api/libraries/refresh", { method: "POST" });
    const extra = r.skipped ? ` · 忽略 ${r.skipped} 个非影视库` : "";
    $("#refresh-result").textContent = `新增 ${r.added} · 共 ${r.total} 个媒体库${extra}`;
    await loadLibraries();
    await loadStatus();
  } catch (e) {
    $("#refresh-result").textContent = "同步失败：" + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "同步库列表";
  }
}

function setAll(predicate) {
  const targets = state.libraries.filter((l) => l.present && l.scannable);
  Promise.all(
    targets.map((lib) => {
      const want = predicate(lib);
      if (lib.enabled === want) return null;
      lib.enabled = want;
      return api(`/api/libraries/${lib.id}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: want }),
      }).catch(() => {});
    })
  ).then(() => {
    renderLibraries();
    loadStatus();
  });
}

/* ---------------------------------------------------------------- 扫描 */

function selectedIds() {
  return state.libraries.filter((l) => l.enabled && l.present && l.scannable).map((l) => l.id);
}

/** 控制台只展示「将扫描几个库」，真正的勾选在设置页。 */
function updateScanScope() {
  const elScope = $("#scan-scope");
  if (!elScope) return;
  if (!state.status || !state.status.configured) {
    elScope.textContent = "尚未连接 Emby";
    return;
  }
  const ids = selectedIds();
  const items = state.libraries
    .filter((l) => ids.includes(l.id))
    .reduce((sum, l) => sum + (l.item_count || 0), 0);
  elScope.textContent = ids.length
    ? `将扫描 ${ids.length} 个媒体库 · 约 ${items} 条`
    : "尚未勾选媒体库，去设置里选择";
}

async function startScan() {
  // 扫描范围一律以设置页勾选的库为准，不在控制台传参
  if (!selectedIds().length) {
    $("#scan-error").textContent = "还没有勾选媒体库，请到「设置 → 监控的媒体库」选择";
    return null;
  }
  const modeEl = document.querySelector('input[name="mode"]:checked');
  const mode = modeEl ? modeEl.value : "full";
  $("#scan-error").textContent = "";
  try {
    const r = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
    state.runId = r.run_id;
    setScanning(true);
    pollRun();
    return r.run_id;
  } catch (e) {
    $("#scan-error").textContent = e.message;
    return null;
  }
}

/** 未配置 / 重置后，把控制台上的旧数据全部清掉，避免"背后还有记录"。 */
function clearConsoleData() {
  state.libraries = [];
  const libBox = $("#lib-list");
  if (libBox) libBox.innerHTML = "";
  const stats = $("#stats");
  if (stats) stats.innerHTML = "";
  const cards = $("#media-groups");
  if (cards) {
    cards.innerHTML = "";
    cards.appendChild(el("div", "empty", "尚未连接 Emby。"));
  }
  const summary = $("#media-summary");
  if (summary) summary.textContent = "";
  const pager = $("#pager-info");
  if (pager) pager.textContent = "";
  const lb = $("#lib-progress");
  if (lb) lb.innerHTML = "";
  const head = $("#progress-head-left");
  if (head) head.textContent = "尚未开始";
  const pct = $("#progress-percent");
  if (pct) pct.textContent = "0%";
  const fill = $("#progress-fill");
  if (fill) fill.style.width = "0%";
  refreshLibraryFilter();
  updateScanScope();
}

function setScanning(scanning) {
  $("#btn-scan").disabled = scanning;
  $("#btn-cancel").disabled = !scanning;
  $$('input[name="mode"]').forEach((r) => (r.disabled = scanning));
}

function renderRun(run) {
  const percent = run.percent || 0;
  const noop = run.mode === "incremental" && run.processed === 0 && run.state !== "running";
  $("#progress-head-left").textContent =
    `#${run.run_id} ${run.mode} · ${run.state}` +
    (run.total ? ` · ${noop ? "无变更" : `${run.processed}/${run.total}`}` : "");
  $("#progress-head-right").textContent =
    `新增 ${run.added} · 更新 ${run.updated} · 移除 ${run.removed} · 未变 ${run.unchanged}`;
  $("#progress-fill").style.width = `${noop ? 100 : Math.min(100, percent)}%`;
  $("#progress-percent").textContent = noop ? "无变更" : `${percent.toFixed(1)}%`;

  const box = $("#lib-progress");
  box.innerHTML = "";
  for (const lib of run.libraries || []) {
    const row = el("div", "row");
    const top = el("div", "top");
    top.appendChild(
      el("span", "", `${lib.name} · ${lib.state}${lib.error ? " · " + lib.error : ""}`)
    );
    top.appendChild(el("span", "", `${lib.processed}/${lib.total}`));
    const track = el("div", "track");
    const fill = el("span");
    fill.style.width = `${Math.min(100, lib.percent || 0)}%`;
    if (lib.state === "failed") fill.style.background = "var(--err)";
    if (lib.state === "done") fill.style.background = "var(--ok)";
    track.appendChild(fill);
    row.append(top, track);
    box.appendChild(row);
  }
}

async function pollRun() {
  clearTimeout(state.pollTimer);
  if (!state.runId) return;
  try {
    const run = await api(`/api/scan/${state.runId}`);
    renderRun(run);
    if (run.state === "running") {
      state.pollTimer = setTimeout(pollRun, 1000);
      return;
    }
    setScanning(false);
    if (run.error) $("#scan-error").textContent = run.error;
    await Promise.all([loadLibraries(), loadStats(), loadMedia(1)]);
  } catch (e) {
    $("#scan-error").textContent = "查询进度失败：" + e.message;
    setScanning(false);
  }
}

async function cancelScan() {
  if (!state.runId) return;
  try {
    await api(`/api/scan/${state.runId}/cancel`, { method: "POST" });
  } catch (e) {
    $("#scan-error").textContent = e.message;
  }
}

/* ---------------------------------------------------------------- 统计 */

async function loadStats() {
  const box = $("#stats");
  if (!box) return;
  if (!state.status || !state.status.configured) {
    box.innerHTML = "";
    return;
  }
  try {
    const s = await api("/api/media/stats");
    box.innerHTML = "";
    const add = (k, v) => {
      const item = el("div", "item");
      item.append(el("div", "k", k), el("div", "v", String(v)));
      box.appendChild(item);
    };
    add("条目总数", s.total);
    add("已移除", s.removed);
    add("已匹配", (s.by_match_status && s.by_match_status.matched) || 0);
    add("待匹配", (s.by_match_status && s.by_match_status.pending) || 0);
  } catch {
    /* 静默 */
  }
}

/* ---------------------------------------------------------------- 条目 */

function refreshLibraryFilter() {
  const sel = $("#f-library");
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">全部库</option>';
  for (const lib of state.libraries) {
    const opt = document.createElement("option");
    opt.value = lib.id;
    opt.textContent = lib.name;
    sel.appendChild(opt);
  }
  sel.value = current;
}

async function loadMedia(page = 1) {
  if (!state.status || !state.status.configured) {
    clearConsoleData();
    return;
  }
  state.page = page;
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(state.pageSize),
  });
  if (state.filters.library_id) params.set("library_id", state.filters.library_id);
  if (state.filters.q) params.set("q", state.filters.q);
  if (state.filters.include_removed) params.set("include_removed", "true");

  const data = await api("/api/media/groups?" + params.toString());
  const box = $("#media-groups");
  box.innerHTML = "";
  if (!data.groups.length) {
    box.appendChild(
      el("div", "empty", "没有内容。先同步库列表并执行扫描。")
    );
  }
  for (const g of data.groups) box.appendChild(buildGroupCard(g));

  $("#media-summary").textContent = `${data.total} 部`;
  $("#pager-info").textContent = `共 ${data.total} 部 · 第 ${data.page}/${data.pages || 1} 页`;
  $("#btn-prev").disabled = data.page <= 1;
  $("#btn-next").disabled = data.page >= (data.pages || 1);
}

//: 一屏最多完整展示几季；超出的季折成一行摘要
const SEASONS_SHOWN = 6;
//: 每季最多画几个方块，剩下的用 +N 表示
const EPS_PER_SEASON = 30;
//: 超过这个集数后，卡片只放摘要，完整列表进抽屉
const EPISODE_DRAWER_THRESHOLD = 24;
const CARD_PREVIEW_SEASONS = 2;
const CARD_PREVIEW_EPS = 6;

function epTitle(it) {
  if (it.season != null && it.episode != null) {
    return `S${String(it.season).padStart(2, "0")}E${String(it.episode).padStart(2, "0")}`;
  }
  return it.name || "";
}

function epState(it) {
  if (it.scan_state === "removed") return "removed";
  if (it.danmaku_status === "cached") return "cached";
  if (it.match_status === "matched") return "matched";
  if (it.match_status === "failed" || it.match_status === "unmatched") return "failed";
  return "pending";
}

function buildGroupCard(g) {
  const card = el("article", "m-card");

  const poster = el("div", "m-poster");
  const showInitial = () => {
    const stale = poster.querySelector("img");
    if (stale) stale.remove();
    if (!poster.querySelector(".initial")) {
      poster.appendChild(el("span", "initial", (g.title || "?").trim().charAt(0)));
    }
  };
  if (g.cover_id) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = g.title;
    const first = (g.items || []).find((it) => it.emby_id !== g.cover_id);
    const fallback = first ? `&fallback=${encodeURIComponent(first.emby_id)}` : "";
    img.src = `/api/media/cover/${encodeURIComponent(g.cover_id)}?w=300${fallback}`;
    img.addEventListener("error", showInitial, { once: true });
    poster.appendChild(img);
  } else {
    showInitial();
  }
  // 绝大多数都是剧集，只给电影打角标，避免整墙「剧集」噪声
  if (g.kind === "movie") poster.appendChild(el("span", "m-kind", "电影"));
  card.appendChild(poster);

  const body = el("div", "m-body");
  const title = el("div", "m-title", g.title);
  title.title = g.updated_at
    ? `${g.title}\n最近更新：${fmtTime(g.updated_at)}`
    : g.title;
  body.appendChild(title);

  const bits = [];
  if (g.year) bits.push(String(g.year));
  bits.push(g.kind === "movie" ? "1 部" : `${g.total} 集`);
  if (g.updated_at) bits.push(`更新 ${fmtDateShort(g.updated_at)}`);
  if (g.library_name) bits.push(g.library_name);
  body.appendChild(el("div", "m-sub", bits.join(" · ")));

  const list = g.items || [];
  if (g.kind === "movie") {
    const row = el("div", "season-row solo");
    const eps = el("div", "m-eps");
    const chip = el("span", "ep movie");
    chip.textContent = "▶";
    chip.title = `${g.title} · ${matchLabel((list[0] || {}).match_status)}`;
    eps.appendChild(chip);
    row.appendChild(eps);
    body.appendChild(row);
  } else {
    const needsDrawer = list.length > EPISODE_DRAWER_THRESHOLD;
    body.appendChild(buildSeasonBlock(list, { preview: needsDrawer }));
    if (needsDrawer) {
      const btn = el("button", "m-episode-drawer-btn", `查看全部 ${g.total} 集`);
      btn.type = "button";
      btn.addEventListener("click", () => openEpisodeDrawer(g));
      body.appendChild(btn);
    }
  }
  card.appendChild(body);
  return card;
}

/** 季标签：S1 / S2，特别篇(season=0) 记作 SP，缺失记作 — */
function seasonLabel(season) {
  if (season === null || season === undefined) return "—";
  if (Number(season) === 0) return "SP";
  return `S${season}`;
}

function seasonRank(key) {
  if (key === "x") return 2; // 没有季号，排最后
  return Number(key) === 0 ? 1 : 0; // 正片季在前，特别篇次之
}

function buildSeasonBlock(list, { preview = false } = {}) {
  const wrap = el("div", "m-seasons");

  const buckets = new Map();
  for (const it of list) {
    const key = it.season === null || it.season === undefined ? "x" : String(it.season);
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(it);
  }

  const keys = [...buckets.keys()].sort(
    (a, b) => seasonRank(a) - seasonRank(b) || (Number(a) || 0) - (Number(b) || 0)
  );
  // 每季单独一行并始终带 S1/S2/SP 标识，单季也显示，避免用户不知道季号
  const withLabel = true;

  const seasonLimit = preview ? CARD_PREVIEW_SEASONS : SEASONS_SHOWN;
  const episodeLimit = preview ? CARD_PREVIEW_EPS : EPS_PER_SEASON;
  const shownKeys = keys.slice(0, seasonLimit);
  for (const key of shownKeys) {
    const items = buckets
      .get(key)
      .slice()
      .sort((a, b) => (a.episode ?? 0) - (b.episode ?? 0));

    const row = el("div", withLabel ? "season-row" : "season-row solo");
    if (withLabel) {
      const label = el("span", "season-label", seasonLabel(items[0].season));
      label.title = `${seasonLabel(items[0].season)} · ${items.length} 集`;
      row.appendChild(label);
    }

    const eps = el("div", "m-eps");
    for (const it of items.slice(0, episodeLimit)) {
      const chip = el("span", `ep ${epState(it)}`);
      chip.textContent = it.episode != null ? String(it.episode) : "·";
      chip.title = `${epTitle(it)} · ${matchLabel(it.match_status)}`;
      eps.appendChild(chip);
    }
    if (items.length > episodeLimit) {
      eps.appendChild(
        el("span", "ep more", `+${items.length - episodeLimit}`)
      );
    }
    row.appendChild(eps);
    wrap.appendChild(row);
  }

  // 季太多时不再往下堆，折成一行摘要
  if (keys.length > seasonLimit) {
    const rest = keys.slice(seasonLimit);
    const restCount = rest.reduce((n, k) => n + buckets.get(k).length, 0);
    const names = rest.map((k) => seasonLabel(buckets.get(k)[0].season)).join(" ");
    const row = el("div", withLabel ? "season-row" : "season-row solo");
    if (withLabel) row.appendChild(el("span", "season-label", "…"));
    const hint = el("div", "m-eps");
    hint.appendChild(el("span", "ep more", `还有 ${names} · ${restCount} 集`));
    row.appendChild(hint);
    wrap.appendChild(row);
  }

  return wrap;
}

function buildDrawerContent(list) {
  const wrap = el("div", "drawer-seasons");
  const buckets = new Map();
  for (const it of list) {
    const key = it.season === null || it.season === undefined ? "x" : String(it.season);
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(it);
  }
  const keys = [...buckets.keys()].sort(
    (a, b) => seasonRank(a) - seasonRank(b) || (Number(a) || 0) - (Number(b) || 0)
  );
  for (const key of keys) {
    const items = buckets
      .get(key)
      .slice()
      .sort((a, b) => (a.episode ?? 0) - (b.episode ?? 0));
    const section = el("section", "drawer-season");
    const head = el("div", "drawer-season-head");
    head.appendChild(el("span", "drawer-season-name", seasonLabel(items[0].season)));
    head.appendChild(el("span", "drawer-season-count", `${items.length} 集`));
    section.appendChild(head);
    const eps = el("div", "drawer-eps");
    for (const it of items) {
      const chip = el("span", `ep drawer-ep ${epState(it)}`);
      chip.textContent = it.episode != null ? String(it.episode) : "·";
      chip.title = `${epTitle(it)} · ${matchLabel(it.match_status)}`;
      eps.appendChild(chip);
    }
    section.appendChild(eps);
    wrap.appendChild(section);
  }
  return wrap;
}

function openEpisodeDrawer(g) {
  const overlay = $("#episode-drawer");
  if (!overlay) return;
  $("#drawer-title").textContent = g.title || "未命名剧集";
  const bits = [];
  if (g.year) bits.push(String(g.year));
  bits.push(`${g.total} 集`);
  if (g.library_name) bits.push(g.library_name);
  if (g.updated_at) bits.push(`最近更新 ${fmtTime(g.updated_at)}`);
  $("#drawer-sub").textContent = bits.join(" · ");
  const body = $("#drawer-body");
  body.innerHTML = "";
  body.appendChild(buildDrawerContent(g.items || []));
  overlay.hidden = false;
  document.body.classList.add("drawer-open");
  body.scrollTop = 0;
  $("#drawer-close").focus();
}

function closeEpisodeDrawer() {
  const overlay = $("#episode-drawer");
  if (!overlay) return;
  overlay.hidden = true;
  document.body.classList.remove("drawer-open");
}

function matchLabel(status) {
  return (
    { pending: "待匹配", matched: "已匹配", unmatched: "未匹配", failed: "匹配失败" }[
      status
    ] || status || "待匹配"
  );
}

/* ---------------------------------------------------------------- 运行日志 */

const LOG_KEEP = 200;

function logTime(ts) {
  const d = new Date(ts * 1000);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, "0"))
    .join(":");
}

function renderLogRow(e) {
  const row = el("div", `log-row ${e.level}`);
  row.appendChild(el("span", "t", logTime(e.ts)));
  const msg = el("span", "msg");
  msg.appendChild(el("span", "src", e.source || "app"));
  msg.appendChild(document.createTextNode(e.message));
  row.appendChild(msg);
  return row;
}

function logVisible(e) {
  if (state.logFilter === "all") return true;
  if (state.logFilter === "warn") return e.level === "warn" || e.level === "error";
  return e.level === "error";
}

function renderLogs() {
  const box = $("#log-list");
  if (!box) return;
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = "";
  const items = state.logs.filter(logVisible);
  const count = $("#log-count");
  if (count) count.textContent = `${items.length} 条`;
  if (!items.length) {
    box.appendChild(el("div", "log-empty", "暂无记录"));
    return;
  }
  for (const e of items) box.appendChild(renderLogRow(e));
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

async function loadLogs() {
  const data = await api(`/api/logs?after=${state.logSeq}`);
  if (!data.items.length) return;
  state.logs.push(...data.items);
  if (state.logs.length > LOG_KEEP) {
    state.logs = state.logs.slice(-LOG_KEEP);
  }
  state.logSeq = data.last_seq;
  renderLogs();
}

function startLogPoll() {
  loadLogs().catch(() => {});
  if (state.logTimer) clearInterval(state.logTimer);
  state.logTimer = setInterval(() => {
    if (document.hidden) return;
    loadLogs().catch(() => {});
  }, 1500);
}

function visibleLogText() {
  return state.logs
    .filter(logVisible)
    .map((e) => {
      const ts = new Date(e.ts * 1000).toLocaleString("zh-CN", { hour12: false });
      return `${ts} [${(e.level || "info").toUpperCase()}] ${e.source || "app"} ${e.message}`;
    })
    .join("\n");
}

async function copyLogs() {
  const text = visibleLogText();
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // 非安全上下文/旧浏览器兜底
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  const btn = $("#btn-log-copy");
  if (btn) {
    const old = btn.textContent;
    btn.textContent = "已复制";
    setTimeout(() => (btn.textContent = old), 1200);
  }
}

function downloadLogs() {
  const text = visibleLogText();
  if (!text) return;
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `danmaku-logs-${new Date().toISOString().replace(/[:.]/g, "-")}.txt`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/* ---------------------------------------------------------------- 设置 */

async function loadSettings() {
  try {
    const s = await api("/api/settings");
    state.settings = s;
    $("#s-url").value = s.emby_base_url || "";
    $("#s-key").value = "";
    $("#s-key").placeholder = s.emby_api_key_set ? "留空表示不修改" : "请输入 API Key";
    $("#s-key-state").textContent = s.emby_api_key_set ? s.emby_api_key_masked : "未设置";
    $("#s-page-size").value = s.page_size;
    $("#s-scheduler").checked = !!s.scheduler_enabled;
    $("#s-cron-inc").value = s.scan_cron_incremental;
    $("#s-cron-full").value = s.scan_cron_full;
    $("#s-msg").textContent = "";
    $("#s-scan-msg").textContent = "";
  } catch (e) {
    $("#s-msg").textContent = "读取设置失败：" + e.message;
  }
}

async function testConnection() {
  const btn = $("#btn-test");
  btn.disabled = true;
  $("#s-msg").textContent = "测试中…";
  try {
    const r = await api("/api/settings/test", {
      method: "POST",
      body: JSON.stringify({
        emby_base_url: $("#s-url").value,
        emby_api_key: $("#s-key").value || null,
      }),
    });
    $("#s-msg").textContent = `连接成功：${r.server_name} ${r.version} · 发现 ${r.media_library_count} 个媒体库`;
  } catch (e) {
    $("#s-msg").textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function saveConnection() {
  const btn = $("#btn-save");
  btn.disabled = true;
  try {
    const payload = {
      emby_base_url: $("#s-url").value,
      page_size: Number($("#s-page-size").value) || 500,
      scheduler_enabled: $("#s-scheduler").checked,
      scan_cron_incremental: $("#s-cron-inc").value.trim(),
      scan_cron_full: $("#s-cron-full").value.trim(),
    };
    const key = $("#s-key").value.trim();
    if (key) payload.emby_api_key = key;

    const s = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    state.settings = s;
    $("#s-key").value = "";
    $("#s-key-state").textContent = s.emby_api_key_set ? s.emby_api_key_masked : "未设置";
    $("#s-msg").textContent = "已保存";
    await Promise.all([loadStatus(), loadHealth(), loadLibraries()]);
  } catch (e) {
    $("#s-msg").textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function saveScanSettings() {
  const btn = $("#btn-save-scan");
  btn.disabled = true;
  try {
    const s = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        page_size: Number($("#s-page-size").value) || 500,
        scheduler_enabled: $("#s-scheduler").checked,
        scan_cron_incremental: $("#s-cron-inc").value.trim(),
        scan_cron_full: $("#s-cron-full").value.trim(),
      }),
    });
    state.settings = s;
    $("#s-scan-msg").textContent = "已保存并重新排期";
  } catch (e) {
    $("#s-scan-msg").textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

function resetConfig() {
  openDanger({
    title: "完全重置",
    desc: "将删除 Emby 配置与全部扫描数据，回到初始化页面。此操作不可撤销。",
    items: [
      "删除：Emby 服务器地址、API Key、扫描与定时设置",
      "删除：所有媒体库记录与条目索引",
      "删除：全部扫描历史",
      "完成后需要重新填写地址与密钥，并重新选择媒体库",
    ],
    onConfirm: async () => {
      await api("/api/settings/reset", { method: "POST" });
      location.replace("/setup");
    },
  });
}

function purgeData() {
  const n = (state.status && state.status.library_count) || 0;
  openDanger({
    title: "清空扫描数据",
    desc: "此操作不可撤销，将删除全部索引数据，配置保留。",
    items: [
      "删除：所有条目索引与已缓存的扫描结果",
      `删除：${n} 个媒体库记录`,
      "删除：全部扫描历史",
      "保留：Emby 地址、API Key、定时设置",
    ],
    onConfirm: async () => {
      const r = await api("/api/settings/purge", { method: "POST" });
      await loadStatus();
      await loadLibraries();
      await loadStats();
      await loadMedia(1);
      $("#refresh-result").textContent =
        `已清空 ${r.media} 条条目、${r.libraries} 个库记录`;
    },
  });
}

/* ---------------------------------------------------------------- 状态 / 危险操作 */

async function loadStatus() {
  state.status = await api("/api/settings/status");
  return state.status;
}

let dangerAction = null;

function openDanger({ title, desc, items, onConfirm }) {
  dangerAction = onConfirm;
  $("#danger-title").textContent = title;
  $("#danger-desc").textContent = desc;
  const list = $("#danger-list");
  list.innerHTML = "";
  for (const item of items || []) list.appendChild(el("li", "", item));
  $("#danger-err").textContent = "";
  const btn = $("#danger-confirm");
  btn.disabled = false;
  btn.textContent = "确认执行";
  $("#danger").hidden = false;
  setTimeout(() => btn.focus(), 30);
}

function closeDanger() {
  $("#danger").hidden = true;
  dangerAction = null;
}

async function confirmDanger() {
  const action = dangerAction;
  if (!action) return;
  const btn = $("#danger-confirm");
  btn.disabled = true;
  btn.textContent = "执行中…";
  try {
    await action();
    closeDanger();
  } catch (e) {
    $("#danger-err").textContent = e.message;
    btn.disabled = false;
    btn.textContent = "确认执行";
  }
}


/* ---------------------------------------------------------------- 绑定 */

function bind() {
  $$(".nav-btn").forEach((b) =>
    b.addEventListener("click", () => switchView(b.dataset.view))
  );

  $("#btn-refresh").addEventListener("click", refreshLibraries);
  $("#btn-scan").addEventListener("click", () => startScan());
  $("#btn-cancel").addEventListener("click", cancelScan);

  $("#btn-all").addEventListener("click", () => setAll(() => true));
  $("#btn-video").addEventListener("click", () =>
    setAll((l) => l.scannable && (l.collection_type || "").toLowerCase() === "tvshows")
  );
  $("#btn-none").addEventListener("click", () => setAll(() => false));

  $("#f-library").addEventListener("change", (e) => {
    state.filters.library_id = e.target.value;
    loadMedia(1);
  });
  $("#f-removed").addEventListener("change", (e) => {
    state.filters.include_removed = e.target.checked;
    loadMedia(1);
  });
  let timer = null;
  $("#f-q").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.filters.q = e.target.value.trim();
      loadMedia(1);
    }, 300);
  });
  $("#btn-prev").addEventListener("click", () => loadMedia(Math.max(1, state.page - 1)));
  $("#btn-next").addEventListener("click", () => loadMedia(state.page + 1));

  $("#btn-test").addEventListener("click", testConnection);
  $("#btn-save").addEventListener("click", saveConnection);
  $("#btn-save-scan").addEventListener("click", saveScanSettings);
  $("#btn-reset").addEventListener("click", resetConfig);
  $("#btn-purge").addEventListener("click", purgeData);
  $("#btn-goto-libs").addEventListener("click", () => {
    switchView("settings");
    const panel = $("#lib-list");
    if (panel) panel.scrollIntoView({ behavior: "smooth", block: "center" });
  });

  const logFilter = $("#log-filter");
  if (logFilter) {
    logFilter.addEventListener("change", (e) => {
      state.logFilter = e.target.value;
      renderLogs();
    });
  }
  const btnLogClear = $("#btn-log-clear");
  if (btnLogClear) {
    btnLogClear.addEventListener("click", () => {
      // 只清显示，不动后端缓冲；下次进来仍能看到这之后的记录
      state.logs = [];
      renderLogs();
    });
  }
  const btnLogCopy = $("#btn-log-copy");
  if (btnLogCopy) btnLogCopy.addEventListener("click", copyLogs);
  const btnLogDownload = $("#btn-log-download");
  if (btnLogDownload) btnLogDownload.addEventListener("click", downloadLogs);

  const drawer = $("#episode-drawer");
  if (drawer) {
    $("#drawer-close").addEventListener("click", closeEpisodeDrawer);
    drawer.addEventListener("click", (e) => {
      if (e.target === drawer) closeEpisodeDrawer();
    });
  }

  // 危险操作：弹窗列出影响面，点「确认执行」即生效
  $("#danger-cancel").addEventListener("click", closeDanger);
  $("#danger-confirm").addEventListener("click", confirmDanger);
  $("#danger").addEventListener("click", (e) => {
    if (e.target === $("#danger")) closeDanger();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#episode-drawer").hidden) {
      closeEpisodeDrawer();
      return;
    }
    if (!$("#danger").hidden) closeDanger();
  });
}
async function boot() {
  bind();
  try {
    await loadStatus();
  } catch (e) {
    state.status = {
      configured: false,
      needs_onboarding: true,
      stage: "unconfigured",
      library_count: 0,
      enabled_count: 0,
    };
  }

  // 初始化没走完就跳独立页面，不在控制台上叠遮罩。
  // 只拦「没配置」和「一个库都没有」两种情况：已配置但没勾选时放行到控制台，
  // 免得用户把勾选清空后反而进不去（控制台里会有引导提示）。
  const stage = state.status && state.status.stage;
  if (stage === "unconfigured" || stage === "no_libraries") {
    location.replace("/setup");
    return;
  }

  // 支持 ?q=xxx 直接带上搜索词，方便分享/排查
  const urlQ = new URLSearchParams(location.search).get("q");
  if (urlQ) {
    state.filters.q = urlQ;
    const box = $("#f-q");
    if (box) box.value = urlQ;
  }

  await loadHealth().catch(() => {});
  await loadLibraries().catch(() => {});
  await loadStats().catch(() => {});
  await loadMedia(1).catch(() => {});
  startLogPoll();

  const current = await api("/api/scan/current").catch(() => null);
  if (current && current.running && current.run_id) {
    state.runId = current.run_id;
    setScanning(true);
    pollRun();
  }
}

boot();
