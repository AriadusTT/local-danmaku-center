/* 初始化页：独立路由 /setup，三步走。 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const state = { libraries: [], status: null, step: 1 };

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
  if (!resp.ok) throw new Error((data && (data.detail || data.message)) || `HTTP ${resp.status}`);
  return data;
}

function showStep(step) {
  state.step = step;
  $$(".ob-body").forEach((p) => (p.hidden = Number(p.dataset.panel) !== step));
  $$(".ob-step").forEach((s) => {
    const n = Number(s.dataset.step);
    s.classList.toggle("active", n === step);
    s.classList.toggle("done", n < step);
  });
}

/* ---------------------------------------------------------------- 库列表 */

const TYPE_LABEL = { tvshows: "剧集", movies: "电影" };
const typeLabel = (t) => TYPE_LABEL[(t || "").toLowerCase()] || "混合";

function buildRow(lib) {
  const row = el("label", "lib-row");
  row.classList.toggle("checked", lib.enabled);

  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = lib.enabled;
  cb.addEventListener("change", async () => {
    cb.disabled = true;
    try {
      const updated = await api(`/api/libraries/${lib.id}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: cb.checked }),
      });
      Object.assign(lib, updated);
      row.classList.toggle("checked", lib.enabled);
      $("#setup-err-2").textContent = "";
    } catch (e) {
      cb.checked = !cb.checked;
      $("#setup-err-2").textContent = "保存失败：" + e.message;
    } finally {
      cb.disabled = false;
    }
  });

  const info = el("div");
  const nameLine = el("div", "name");
  nameLine.appendChild(el("span", "", lib.name));
  nameLine.appendChild(el("span", "tag", typeLabel(lib.collection_type)));
  info.appendChild(nameLine);

  row.append(cb, info, el("div", "count", `${lib.item_count || 0} 条`));
  return row;
}

function renderLibraries() {
  const box = $("#setup-lib-list");
  box.innerHTML = "";
  if (!state.libraries.length) {
    box.appendChild(el("div", "empty", "还没有媒体库，点下方「同步媒体库」从 Emby 拉取。"));
    return;
  }
  for (const lib of state.libraries) box.appendChild(buildRow(lib));
}

async function loadLibraries() {
  const data = await api("/api/libraries");
  state.libraries = data.items;
  renderLibraries();
}

function selectedCount() {
  return state.libraries.filter((l) => l.enabled).length;
}

/* ---------------------------------------------------------------- 交互 */

async function testConnection() {
  const btn = $("#setup-test");
  btn.disabled = true;
  $("#setup-err-1").textContent = "";
  $("#setup-test-result").textContent = "测试中…";
  try {
    const r = await api("/api/settings/test", {
      method: "POST",
      body: JSON.stringify({
        emby_base_url: $("#setup-url").value,
        emby_api_key: $("#setup-key").value || null,
      }),
    });
    $("#setup-test-result").textContent =
      `${r.server_name} ${r.version} · ${r.media_library_count} 个媒体库`;
    return r;
  } catch (e) {
    $("#setup-test-result").textContent = "";
    $("#setup-err-1").textContent = e.message;
    return null;
  } finally {
    btn.disabled = false;
  }
}

async function next1() {
  const url = $("#setup-url").value.trim();
  const key = $("#setup-key").value.trim();
  if (!url) {
    $("#setup-err-1").textContent = "请填写 Emby 地址";
    return;
  }
  const btn = $("#setup-next-1");
  btn.disabled = true;
  $("#setup-err-1").textContent = "";
  try {
    const payload = { emby_base_url: url };
    if (key) payload.emby_api_key = key;
    await api("/api/settings", { method: "PUT", body: JSON.stringify(payload) });

    const test = await api("/api/settings/test", {
      method: "POST",
      body: JSON.stringify({ emby_base_url: url, emby_api_key: key || null }),
    });
    await api("/api/libraries/refresh", { method: "POST" });
    await loadLibraries();
    showStep(2);
    $("#setup-sync-result").textContent = `已发现 ${test.media_library_count} 个媒体库`;
    $("#setup-url").dataset.saved = "1";
  } catch (e) {
    $("#setup-err-1").textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function syncLibraries() {
  const btn = $("#setup-sync");
  btn.disabled = true;
  $("#setup-sync-result").textContent = "同步中…";
  try {
    const r = await api("/api/libraries/refresh", { method: "POST" });
    await loadLibraries();
    $("#setup-sync-result").textContent = `共 ${r.total} 个媒体库`;
  } catch (e) {
    $("#setup-sync-result").textContent = "同步失败：" + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function next2() {
  if (!selectedCount()) {
    $("#setup-err-2").textContent = "请至少勾选一个媒体库";
    return;
  }
  $("#setup-err-2").textContent = "";
  $("#setup-summary").textContent =
    `已连接 Emby，纳入监控 ${selectedCount()} 个媒体库。` +
    "可以现在扫一次建立索引（约 5 秒），也可以稍后在控制台手动触发。";
  showStep(3);
}

async function scanNow() {
  const btn = $("#setup-scan");
  btn.disabled = true;
  $("#setup-scan-result").textContent = "正在启动扫描…";
  try {
    const r = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({ mode: "full" }),
    });
    $("#setup-scan-result").textContent = `扫描已启动（#${r.run_id}），正在进入控制台…`;
    setTimeout(() => location.replace("/"), 700);
  } catch (e) {
    $("#setup-err-3").textContent = e.message;
    btn.disabled = false;
    $("#setup-scan-result").textContent = "";
  }
}

/* ---------------------------------------------------------------- 启动 */

async function boot() {
  try {
    state.status = await api("/api/settings/status");
  } catch {
    location.replace("/");
    return;
  }

  // 已经配置好且选了库，就不该停在这里
  if (!state.status.needs_onboarding) {
    location.replace("/");
    return;
  }

  if (state.status.stage === "unconfigured") {
    const cfg = await api("/api/settings").catch(() => null);
    // 只回填用户自己存过的地址；环境变量兜底值不要假装是已配置
    if (cfg && cfg.emby_base_url && cfg.sources && cfg.sources.emby_base_url === "db") {
      $("#setup-url").value = cfg.emby_base_url;
    }
    showStep(1);
  } else {
    await loadLibraries();
    showStep(2);
    if (!state.libraries.length) await syncLibraries();
  }

  $("#setup-test").addEventListener("click", testConnection);
  $("#setup-next-1").addEventListener("click", next1);
  $("#setup-sync").addEventListener("click", syncLibraries);
  $("#setup-back-2").addEventListener("click", () => showStep(1));
  $("#setup-next-2").addEventListener("click", next2);
  $("#setup-skip").addEventListener("click", () => location.replace("/"));
  $("#setup-scan").addEventListener("click", scanNow);

  $("#setup-key").addEventListener("keydown", (e) => {
    if (e.key === "Enter") next1();
  });
}

boot();
