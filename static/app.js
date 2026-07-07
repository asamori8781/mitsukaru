"use strict";

// ---- 共通ユーティリティ ----

async function api(path, method = "GET", body = null, signal = null) {
  const opts = { method, headers: {} };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (signal) {
    opts.signal = signal;
  }
  const res = await fetch(path, opts);
  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    data = null;
  }
  if (!res.ok) {
    const message = (data && data.detail) ? data.detail : `エラーが発生しました(HTTP ${res.status})`;
    throw new Error(message);
  }
  return data;
}

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => { toast.hidden = true; }, 4000);
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}(概算)`;
}

function formatTimestamp(unixSeconds) {
  if (!unixSeconds) return "-";
  const d = new Date(unixSeconds * 1000);
  return d.toLocaleString("ja-JP");
}

function isPywebview() {
  return !!(window.pywebview && window.pywebview.api);
}

async function pickFolder() {
  if (isPywebview()) {
    try {
      return await window.pywebview.api.select_folder();
    } catch (e) {
      showToast("フォルダ選択に失敗しました: " + e);
      return null;
    }
  }
  showToast("この環境ではネイティブのフォルダ選択が使えないため、パスを直接入力してください。");
  return null;
}

// ---- 画面切り替え ----

const VIEW_IDS = ["view-folder-setup", "view-scan-progress", "view-search", "view-settings"];

function showView(id) {
  VIEW_IDS.forEach((v) => {
    document.getElementById(v).hidden = (v !== id);
  });
  const searchView = document.getElementById("view-search");
  const heroActive = (id === "view-search") && !searchView.classList.contains("searched");
  document.body.classList.toggle("hero-mode", heroActive);
  if (heroActive) {
    loadHeroStats();
    document.getElementById("search-input").focus();
  }
}

async function loadHeroStats() {
  const el = document.getElementById("hero-stats");
  try {
    const stats = await api("/api/stats");
    if (stats.file_count > 0) {
      el.textContent = `${stats.file_count.toLocaleString("ja-JP")} 件のファイルを索引済み ・ 最終スキャン: ${formatTimestamp(stats.last_diff_scan_at || stats.last_full_scan_at)}`;
    } else {
      el.textContent = "まだファイルがスキャンされていません(設定画面からスキャンできます)";
    }
  } catch (e) {
    el.textContent = "";
  }
}

function updateNav() {
  const cfg = window._mitsukaruStatus || {};
  const ready = cfg.config_exists && !cfg.needs_folder_setup;
  document.getElementById("btn-nav-search").hidden = !ready;
  document.getElementById("btn-nav-settings").hidden = !cfg.config_exists;
  document.getElementById("btn-settings-back").hidden = !ready;
}

// ---- 利用ログ(検索日時/入力クエリ/展開語数/ヒット件数/開いたファイルの有無) ----

let pendingUsageLog = null; // { query, keywordCount, hitCount, openedFile, flushed }

function schedulePendingLog(query, keywordCount, hitCount) {
  flushPendingLog();
  pendingUsageLog = { query, keywordCount, hitCount, openedFile: false, flushed: false };
}

function updatePendingLogHitCount(keywordCount, hitCount) {
  if (pendingUsageLog && !pendingUsageLog.flushed) {
    pendingUsageLog.keywordCount = keywordCount;
    pendingUsageLog.hitCount = hitCount;
  }
}

function flushPendingLog(useBeacon = false) {
  if (!pendingUsageLog || pendingUsageLog.flushed) return;
  const payload = {
    query: pendingUsageLog.query,
    keyword_count: pendingUsageLog.keywordCount,
    hit_count: pendingUsageLog.hitCount,
    opened_file: pendingUsageLog.openedFile,
  };
  pendingUsageLog.flushed = true;
  if (useBeacon && navigator.sendBeacon) {
    const blob = new Blob([JSON.stringify(payload)], { type: "application/json" });
    navigator.sendBeacon("/api/usage-log", blob);
  } else {
    fetch("/api/usage-log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).catch(() => {});
  }
}

function markFileOpened() {
  if (pendingUsageLog && !pendingUsageLog.flushed) {
    pendingUsageLog.openedFile = true;
    flushPendingLog();
  }
}

window.addEventListener("beforeunload", () => flushPendingLog(true));

// ---- 初期化フロー ----

async function init() {
  wireEvents();
  const status = await api("/api/status");
  window._mitsukaruStatus = status;
  document.getElementById("mock-banner").hidden = !status.mock_mode;
  updateNav();

  if (!status.config_exists) {
    document.getElementById("modal-initial-setup").hidden = false;
    return;
  }
  await afterConfigReady(status);
}

async function afterConfigReady(status) {
  document.getElementById("modal-initial-setup").hidden = true;
  if (status.needs_folder_setup) {
    await refreshFolderSetupList();
    showView("view-folder-setup");
  } else {
    showView("view-search");
  }
}

// ---- 初回API設定モーダル ----

function wireInitialSetupEvents() {
  document.getElementById("btn-init-test").addEventListener("click", async () => {
    const resultEl = document.getElementById("init-test-result");
    resultEl.textContent = "接続テスト中...";
    resultEl.className = "test-result";
    try {
      const body = {
        base_url: document.getElementById("init-base-url").value.trim(),
        api_key: document.getElementById("init-api-key").value,
        model: document.getElementById("init-model").value.trim(),
        timeout_sec: parseInt(document.getElementById("init-timeout").value, 10) || 20,
      };
      const result = await api("/api/config/test-connection", "POST", body);
      resultEl.textContent = result.message + (result.elapsed_sec != null ? `(応答時間: ${result.elapsed_sec}秒)` : "");
      resultEl.className = "test-result " + (result.success ? "success" : "error");
    } catch (e) {
      resultEl.textContent = e.message;
      resultEl.className = "test-result error";
    }
  });

  document.getElementById("btn-init-skip").addEventListener("click", async () => {
    await api("/api/config", "POST", { skip: true });
    const status = await api("/api/status");
    window._mitsukaruStatus = status;
    document.getElementById("mock-banner").hidden = !status.mock_mode;
    updateNav();
    await afterConfigReady(status);
  });

  document.getElementById("btn-init-save").addEventListener("click", async () => {
    const body = {
      base_url: document.getElementById("init-base-url").value.trim(),
      api_key: document.getElementById("init-api-key").value,
      model: document.getElementById("init-model").value.trim(),
      timeout_sec: parseInt(document.getElementById("init-timeout").value, 10) || 20,
      skip: false,
    };
    if (!body.base_url || !body.model) {
      showToast("ベースURLとモデル名は必須です。");
      return;
    }
    try {
      await api("/api/config", "POST", body);
      const status = await api("/api/status");
      window._mitsukaruStatus = status;
      document.getElementById("mock-banner").hidden = !status.mock_mode;
      updateNav();
      await afterConfigReady(status);
    } catch (e) {
      showToast(e.message);
    }
  });
}

// ---- 初回フォルダ選択 ----

let folderSetupList = [];

async function refreshFolderSetupList() {
  const settings = await api("/api/settings");
  folderSetupList = settings.target_folders || [];
  renderFolderList("folder-setup-list", folderSetupList);
}

function renderFolderList(ulId, list) {
  const ul = document.getElementById(ulId);
  ul.innerHTML = "";
  list.forEach((path, idx) => {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.textContent = path;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "secondary";
    btn.textContent = "削除";
    btn.addEventListener("click", () => {
      list.splice(idx, 1);
      renderFolderList(ulId, list);
    });
    li.appendChild(span);
    li.appendChild(btn);
    ul.appendChild(li);
  });
}

function wireFolderSetupEvents() {
  document.getElementById("btn-scan-all-drives").addEventListener("click", async () => {
    await startScan({ mode: "all_drives", folders: [] });
  });

  document.getElementById("btn-folder-setup-pick").addEventListener("click", async () => {
    const path = await pickFolder();
    if (path) {
      document.getElementById("folder-setup-path-input").value = path;
    }
  });

  document.getElementById("btn-folder-setup-add").addEventListener("click", () => {
    const input = document.getElementById("folder-setup-path-input");
    const path = input.value.trim();
    if (!path) return;
    if (!folderSetupList.includes(path)) {
      folderSetupList.push(path);
      renderFolderList("folder-setup-list", folderSetupList);
    }
    input.value = "";
  });

  document.getElementById("btn-folder-setup-start").addEventListener("click", async () => {
    if (folderSetupList.length === 0) {
      showToast("フォルダを1つ以上追加してください。");
      return;
    }
    await startScan({ mode: "folders", folders: folderSetupList });
  });
}

// ---- スキャン進捗 ----

let scanPollTimer = null;

async function startScan(body) {
  try {
    await api("/api/scan/start", "POST", body);
  } catch (e) {
    showToast(e.message);
    return;
  }
  showView("view-scan-progress");
  document.getElementById("scan-progress-title").textContent = "スキャン中...";
  document.getElementById("scan-summary").hidden = true;
  pollScanProgress();
}

function pollScanProgress() {
  clearTimeout(scanPollTimer);
  const poll = async () => {
    let progress;
    try {
      progress = await api("/api/scan/progress");
    } catch (e) {
      showToast(e.message);
      return;
    }
    document.getElementById("scan-current-folder").textContent = progress.current_folder || "-";
    document.getElementById("scan-processed-count").textContent = progress.processed_count;
    document.getElementById("scan-error-count").textContent = progress.error_count;

    if (progress.running) {
      scanPollTimer = setTimeout(poll, 500);
      return;
    }

    document.getElementById("scan-progress-title").textContent = "スキャン完了";
    if (progress.error_message) {
      showToast(progress.error_message);
    }
    if (progress.summary) {
      document.getElementById("summary-file-count").textContent = progress.summary.file_count;
      document.getElementById("summary-db-size").textContent = formatBytes(progress.summary.db_size_bytes);
      document.getElementById("summary-phase1").textContent = formatBytes(progress.summary.phase1_estimate_bytes);
      document.getElementById("scan-summary").hidden = false;
    }

    const status = await api("/api/status");
    window._mitsukaruStatus = status;
    updateNav();
  };
  poll();
}

function wireScanEvents() {
  document.getElementById("btn-scan-cancel").addEventListener("click", async () => {
    await api("/api/scan/cancel", "POST", {});
  });
  document.getElementById("btn-scan-summary-continue").addEventListener("click", () => {
    showView("view-search");
  });
}

// ---- 検索画面 ----

let lastSearchQuery = "";
let searchInFlight = false;
let searchAbortController = null;

function setSearchBusy(message) {
  searchInFlight = true;
  searchAbortController = new AbortController();
  document.getElementById("btn-search").disabled = true;
  document.getElementById("btn-research").disabled = true;
  document.getElementById("btn-search-cancel").hidden = false;
  const info = document.getElementById("results-info");
  info.classList.remove("error");
  info.innerHTML = "";
  const spinner = document.createElement("span");
  spinner.className = "spinner";
  info.appendChild(spinner);
  info.appendChild(document.createTextNode(message));
}

function clearSearchBusy() {
  searchInFlight = false;
  searchAbortController = null;
  document.getElementById("btn-search").disabled = false;
  document.getElementById("btn-research").disabled = false;
  document.getElementById("btn-search-cancel").hidden = true;
}

function showSearchError(message) {
  const info = document.getElementById("results-info");
  info.classList.add("error");
  info.textContent = `エラー: ${message}`;
  showToast(message);
}

function enterSearchedMode() {
  document.getElementById("view-search").classList.add("searched");
  document.body.classList.remove("hero-mode");
}

function wireSearchEvents() {
  document.getElementById("search-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (searchInFlight) return;
    const query = document.getElementById("search-input").value.trim();
    if (!query) return;
    lastSearchQuery = query;
    enterSearchedMode();
    const mock = window._mitsukaruStatus && window._mitsukaruStatus.mock_mode;
    setSearchBusy(mock
      ? "ローカル索引を検索しています..."
      : "AIでキーワードを展開し、ローカル索引を検索しています(APIの応答に十数秒かかることがあります)...");
    try {
      const result = await api("/api/search", "POST", { query }, searchAbortController.signal);
      renderSearchResult(result);
      schedulePendingLog(query, result.keywords.length, result.results.length);
    } catch (e) {
      if (e.name === "AbortError") {
        const info = document.getElementById("results-info");
        info.classList.remove("error");
        info.textContent = "検索をキャンセルしました。";
      } else {
        showSearchError(e.message);
      }
    } finally {
      clearSearchBusy();
    }
  });

  document.getElementById("btn-search-cancel").addEventListener("click", () => {
    if (searchAbortController) {
      searchAbortController.abort();
    }
  });

  document.getElementById("btn-research").addEventListener("click", async () => {
    if (searchInFlight) return;
    const keywords = document.getElementById("keywords-input").value
      .split(",").map((s) => s.trim()).filter((s) => s);
    const extensions = document.getElementById("extensions-input").value
      .split(",").map((s) => s.trim()).filter((s) => s);
    const recencyRaw = document.getElementById("recency-input").value;
    const recencyDays = recencyRaw === "" ? null : parseInt(recencyRaw, 10);

    if (keywords.length === 0) {
      showToast("キーワードを入力してください。");
      return;
    }
    setSearchBusy("この条件で再検索しています(APIは呼びません)...");
    document.getElementById("results-info").scrollIntoView({ block: "nearest" });
    try {
      const result = await api("/api/search/local", "POST", {
        keywords, extensions, recency_days: recencyDays,
      }, searchAbortController.signal);
      renderResultsTable(result.results);
      updatePendingLogHitCount(keywords.length, result.results.length);
    } catch (e) {
      if (e.name === "AbortError") {
        const info = document.getElementById("results-info");
        info.classList.remove("error");
        info.textContent = "検索をキャンセルしました。";
      } else {
        showSearchError(e.message);
      }
    } finally {
      clearSearchBusy();
    }
  });
}

function renderSearchResult(result) {
  document.getElementById("fallback-notice").hidden = !result.fallback_used;
  if (result.fallback_used) {
    document.getElementById("fallback-notice").textContent = result.fallback_reason;
  }

  document.getElementById("expand-panel").hidden = false;
  document.getElementById("keywords-input").value = result.keywords.join(", ");
  document.getElementById("extensions-input").value = (result.extensions || []).join(", ");
  document.getElementById("recency-input").value = result.recency_days != null ? result.recency_days : "";

  renderResultsTable(result.results);
}

function renderMatchBasis(r) {
  const wrap = document.createElement("div");
  wrap.className = "match-badges";

  if (r.matched_keywords && r.matched_keywords.length > 0) {
    if (r.matched_in_name) {
      const b = document.createElement("span");
      b.className = "badge";
      b.textContent = "ファイル名: " + r.matched_keywords.join(", ");
      wrap.appendChild(b);
    }
    if (r.matched_in_content) {
      const b = document.createElement("span");
      b.className = "badge badge-content";
      b.textContent = "本文: " + r.matched_keywords.join(", ");
      wrap.appendChild(b);
    }
  }
  if (r.semantic_score !== null && r.semantic_score !== undefined) {
    const b = document.createElement("span");
    b.className = "badge badge-semantic";
    b.textContent = `意味検索(類似度 ${r.semantic_score.toFixed(2)})`;
    wrap.appendChild(b);
    if (r.snippet) {
      const s = document.createElement("p");
      s.className = "snippet";
      s.textContent = r.snippet;
      wrap.appendChild(s);
    }
  }
  return wrap;
}

function renderResultsTable(results) {
  const table = document.getElementById("results-table");
  const tbody = document.getElementById("results-body");
  const info = document.getElementById("results-info");
  info.classList.remove("error");
  tbody.innerHTML = "";

  if (results.length === 0) {
    table.hidden = true;
    info.textContent = "一致するファイルが見つかりませんでした。ファイル名に手がかりがない場合は見つからない仕様です。";
    return;
  }

  const semanticCount = results.filter((r) => r.semantic_score !== null && r.semantic_score !== undefined).length;
  let text = `${results.length} 件見つかりました(上限50件)`;
  if (semanticCount > 0) {
    text += ` (うち意味検索による補完 ${semanticCount} 件)`;
  }
  info.textContent = text;
  table.hidden = false;

  results.forEach((r) => {
    const tr = document.createElement("tr");

    const tdName = document.createElement("td");
    tdName.textContent = r.name;
    tr.appendChild(tdName);

    const tdPath = document.createElement("td");
    tdPath.textContent = r.path;
    tr.appendChild(tdPath);

    const tdMtime = document.createElement("td");
    tdMtime.textContent = formatTimestamp(r.mtime);
    tr.appendChild(tdMtime);

    const tdBasis = document.createElement("td");
    tdBasis.appendChild(renderMatchBasis(r));
    tr.appendChild(tdBasis);

    const tdActions = document.createElement("td");
    tdActions.className = "actions";

    const btnOpen = document.createElement("button");
    btnOpen.type = "button";
    btnOpen.className = "secondary";
    btnOpen.textContent = "ファイルを開く";
    btnOpen.addEventListener("click", async () => {
      try {
        await api("/api/open-file", "POST", { path: r.path });
        markFileOpened();
      } catch (e) {
        showToast(e.message);
      }
    });
    tdActions.appendChild(btnOpen);

    const btnFolder = document.createElement("button");
    btnFolder.type = "button";
    btnFolder.className = "secondary";
    btnFolder.textContent = "フォルダを開く";
    btnFolder.addEventListener("click", async () => {
      try {
        await api("/api/open-folder", "POST", { path: r.path });
      } catch (e) {
        showToast(e.message);
      }
    });
    tdActions.appendChild(btnFolder);

    tr.appendChild(tdActions);
    tbody.appendChild(tr);
  });
}

// ---- 設定画面 ----

let settingsLoadInFlight = false;

async function loadSettingsView() {
  if (settingsLoadInFlight) return;
  settingsLoadInFlight = true;
  try {
    await _loadSettingsViewInner();
  } finally {
    settingsLoadInFlight = false;
  }
}

async function _loadSettingsViewInner() {
  const [scanSettings, aiConfig, stats] = await Promise.all([
    api("/api/settings"),
    api("/api/config"),
    api("/api/stats"),
  ]);

  document.getElementById("settings-all-drives").checked = scanSettings.scan_all_drives;
  window._settingsFolderList = scanSettings.target_folders.slice();
  renderFolderList("settings-folder-list", window._settingsFolderList);
  document.getElementById("settings-exclude-folders").value = scanSettings.exclude_folders.join("\n");
  document.getElementById("settings-exclude-extensions").value = scanSettings.exclude_extensions.join("\n");
  document.getElementById("settings-diff-interval").value = scanSettings.diff_interval_minutes;

  document.getElementById("settings-mock-mode").checked = aiConfig.mock_mode;
  document.getElementById("settings-base-url").value = aiConfig.base_url;
  document.getElementById("settings-api-key").value = aiConfig.api_key;
  document.getElementById("settings-model").value = aiConfig.model;
  document.getElementById("settings-timeout").value = aiConfig.timeout_sec;

  document.getElementById("stats-file-count").textContent = stats.file_count;
  document.getElementById("stats-db-size").textContent = formatBytes(stats.db_size_bytes);
  document.getElementById("stats-phase1").textContent = formatBytes(stats.phase1_estimate_bytes);
  document.getElementById("stats-last-full").textContent = formatTimestamp(stats.last_full_scan_at);
  document.getElementById("stats-last-diff").textContent = formatTimestamp(stats.last_diff_scan_at);

  document.getElementById("stats-content-indexed").textContent = stats.content_indexed_count;
  document.getElementById("stats-file-count-2").textContent = stats.file_count;
  document.getElementById("stats-embedded-count").textContent = stats.embedded_file_count;
  document.getElementById("stats-semantic-available").textContent =
    stats.semantic_search_available ? "利用可能" : "未ダウンロード(インデックス作成時に自動取得)";
  document.getElementById("stats-last-content-index").textContent = formatTimestamp(stats.last_content_index_at);

  const progress = await api("/api/content-index/progress");
  if (progress.running) {
    document.getElementById("content-index-progress").hidden = false;
    document.getElementById("btn-content-index-start").disabled = true;
    pollContentIndexProgress();
  }
}

function wireSettingsEvents() {
  document.getElementById("btn-nav-settings").addEventListener("click", () => {
    // 先に画面を切り替えてからデータを読み込む。読み込み完了をawaitしてから
    // 切り替える方式だと、サーバーが重い時にボタンが無反応に見える上、
    // 連打した回数分のshowViewが遅れて発火し、検索画面へ移動した後から
    // 設定画面に引き戻される不具合になっていた。
    showView("view-settings");
    loadSettingsView().catch((e) => showToast(e.message));
  });

  document.getElementById("btn-nav-search").addEventListener("click", () => {
    showView("view-search");
  });

  document.getElementById("btn-settings-back").addEventListener("click", () => {
    showView("view-search");
  });

  document.getElementById("btn-settings-folder-pick").addEventListener("click", async () => {
    const path = await pickFolder();
    if (path) {
      document.getElementById("settings-folder-path-input").value = path;
    }
  });

  document.getElementById("btn-settings-folder-add").addEventListener("click", () => {
    const input = document.getElementById("settings-folder-path-input");
    const path = input.value.trim();
    if (!path) return;
    if (!window._settingsFolderList.includes(path)) {
      window._settingsFolderList.push(path);
      renderFolderList("settings-folder-list", window._settingsFolderList);
    }
    input.value = "";
  });

  document.getElementById("btn-settings-save-scan").addEventListener("click", async () => {
    const excludeFolders = document.getElementById("settings-exclude-folders").value
      .split("\n").map((s) => s.trim()).filter((s) => s);
    const excludeExtensions = document.getElementById("settings-exclude-extensions").value
      .split("\n").map((s) => s.trim()).filter((s) => s);
    const body = {
      scan_all_drives: document.getElementById("settings-all-drives").checked,
      target_folders: window._settingsFolderList,
      exclude_folders: excludeFolders,
      exclude_extensions: excludeExtensions,
      diff_interval_minutes: parseInt(document.getElementById("settings-diff-interval").value, 10) || 10,
    };
    try {
      await api("/api/settings", "POST", body);
      showToast("対象フォルダ・除外設定を保存しました。");
    } catch (e) {
      showToast(e.message);
    }
  });

  document.getElementById("btn-settings-test").addEventListener("click", async () => {
    const resultEl = document.getElementById("settings-test-result");
    resultEl.textContent = "接続テスト中...";
    resultEl.className = "test-result";
    try {
      const body = {
        base_url: document.getElementById("settings-base-url").value.trim(),
        api_key: document.getElementById("settings-api-key").value,
        model: document.getElementById("settings-model").value.trim(),
        timeout_sec: parseInt(document.getElementById("settings-timeout").value, 10) || 20,
      };
      const result = await api("/api/config/test-connection", "POST", body);
      resultEl.textContent = result.message + (result.elapsed_sec != null ? `(応答時間: ${result.elapsed_sec}秒)` : "");
      resultEl.className = "test-result " + (result.success ? "success" : "error");
    } catch (e) {
      resultEl.textContent = e.message;
      resultEl.className = "test-result error";
    }
  });

  document.getElementById("btn-settings-save-ai").addEventListener("click", async () => {
    const mockMode = document.getElementById("settings-mock-mode").checked;
    const body = {
      skip: mockMode,
      base_url: document.getElementById("settings-base-url").value.trim(),
      api_key: document.getElementById("settings-api-key").value,
      model: document.getElementById("settings-model").value.trim(),
      timeout_sec: parseInt(document.getElementById("settings-timeout").value, 10) || 20,
    };
    if (!mockMode && (!body.base_url || !body.model)) {
      showToast("ベースURLとモデル名は必須です(モックモードにする場合はチェックを入れてください)。");
      return;
    }
    try {
      await api("/api/config", "POST", body);
      const status = await api("/api/status");
      window._mitsukaruStatus = status;
      document.getElementById("mock-banner").hidden = !status.mock_mode;
      updateNav();
      showToast("API設定を保存しました。");
    } catch (e) {
      showToast(e.message);
    }
  });

  document.getElementById("btn-full-rescan").addEventListener("click", async () => {
    const scanSettings = await api("/api/settings");
    const body = scanSettings.scan_all_drives
      ? { mode: "all_drives", folders: [] }
      : { mode: "folders", folders: scanSettings.target_folders };
    await startScan(body);
  });
}

// ---- コンテンツインデックス作成(Phase 1) ----

const CONTENT_INDEX_PHASE_LABELS = {
  downloading_model: "埋め込みモデルをダウンロード中(初回のみ、数十〜100MB程度)...",
  indexing: "ファイルを処理中...",
};

let contentIndexPollTimer = null;
let contentIndexPollActive = false;

function wireContentIndexEvents() {
  document.getElementById("btn-content-index-start").addEventListener("click", async () => {
    try {
      await api("/api/content-index/start", "POST", {});
    } catch (e) {
      showToast(e.message);
      return;
    }
    document.getElementById("content-index-progress").hidden = false;
    document.getElementById("btn-content-index-start").disabled = true;
    pollContentIndexProgress();
  });

  document.getElementById("btn-content-index-cancel").addEventListener("click", async () => {
    await api("/api/content-index/cancel", "POST", {});
  });
}

function pollContentIndexProgress() {
  // 設定画面を開くたびに呼ばれても、ポーリングのループは常に1本だけにする
  // (多重ループになるとリクエストが積み重なりサーバー・UIの両方が重くなる)
  if (contentIndexPollActive) return;
  contentIndexPollActive = true;
  clearTimeout(contentIndexPollTimer);
  const poll = async () => {
    let progress;
    try {
      progress = await api("/api/content-index/progress");
    } catch (e) {
      contentIndexPollActive = false;
      return;
    }
    document.getElementById("content-index-phase-label").textContent =
      CONTENT_INDEX_PHASE_LABELS[progress.phase] || "";
    const total = progress.total_count || 0;
    const pct = total > 0 ? Math.min(100, Math.round((progress.processed_count / total) * 100)) : 0;
    document.getElementById("content-index-progress-bar").style.width = pct + "%";
    document.getElementById("content-index-detail").textContent =
      `${progress.processed_count} / ${total}件 `
      + `(エラー: ${progress.error_count}件 [抽出: ${progress.extract_error_count ?? "-"} / 埋め込み: ${progress.embed_error_count ?? "-"}], `
      + `埋め込み済み: ${progress.embedded_count}件) `
      + (progress.current_file ? `現在: ${progress.current_file}` : "");

    const lastErrorEl = document.getElementById("content-index-last-error");
    if (progress.last_error) {
      lastErrorEl.hidden = false;
      lastErrorEl.textContent = `直近のエラー: ${progress.last_error}`;
    } else {
      lastErrorEl.hidden = true;
    }

    if (progress.running) {
      contentIndexPollTimer = setTimeout(poll, 500);
      return;
    }

    contentIndexPollActive = false;
    // 統計(本文インデックス済み件数など)の再取得が終わる前にボタンを再度
    // 有効化すると、「完了」に見える画面のまま古い(0件のままの)統計が
    // 一瞬〜通信が遅い場合はしばらく表示され続けてしまう。再取得が終わって
    // からボタンを有効化・完了トーストを出す。
    await loadSettingsView().catch(() => {});
    document.getElementById("btn-content-index-start").disabled = false;
    if (progress.error_message) {
      showToast(progress.error_message);
    } else if (progress.error_count > 0) {
      showToast(`コンテンツインデックスの作成が完了しました(エラー ${progress.error_count}件。詳細は logs/content_index_errors.jsonl を確認してください)。`);
    } else {
      showToast("コンテンツインデックスの作成が完了しました。");
    }
  };
  poll();
}

// ---- イベント登録 ----

function wireEvents() {
  wireInitialSetupEvents();
  wireFolderSetupEvents();
  wireScanEvents();
  wireSearchEvents();
  wireSettingsEvents();
  wireContentIndexEvents();
}

document.addEventListener("DOMContentLoaded", init);
