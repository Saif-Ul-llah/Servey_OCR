/* ==========================================================================
   Servey OCR — front-end controller
   Single-file vanilla JS. State lives here; the server holds only uploaded
   images. The grid is the source of truth for survey/meter/remarks and is
   posted back for validation and export.
   ========================================================================== */

const state = {
  mode: "local",
  job: null,
  images: [],       // [{name, url}]
  activeImage: null,
  rows: [],         // grid rows from the server (or blank manual rows)
  audit: null,
  settings: {
    has_key: false, has_gemini_key: false, has_grok_key: false,
    gemini_model: "gemini-2.0-flash", grok_model: "grok-4-fast",
    local_engine: "easyocr", mode: "local",
  },
};

const $ = (sel) => document.querySelector(sel);
const el = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ API */
async function apiJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}
async function apiGet(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Request failed (${res.status})`);
  return res.json();
}

/* --------------------------------------------------------------- toasts */
function toast(type, title, msg = "") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.innerHTML = `<strong></strong><span></span>`;
  node.querySelector("strong").textContent = title;
  node.querySelector("span").textContent = msg;
  el("toasts").appendChild(node);
  setTimeout(() => {
    node.style.opacity = "0";
    node.style.transition = "opacity .3s";
    setTimeout(() => node.remove(), 300);
  }, type === "error" ? 6500 : 3800);
}
function showOverlay(text) { el("overlay-text").textContent = text; el("overlay").hidden = false; }
function hideOverlay() { el("overlay").hidden = true; }

/* ---------------------------------------------------------------- theme */
function initTheme() {
  const saved = localStorage.getItem("ocr-theme");
  const theme = saved || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.setAttribute("data-theme", theme);
}
el("theme-btn").addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("ocr-theme", next);
});

/* ----------------------------------------------------------------- mode */
const MODE_LABEL = { gemini: "Gemini", grok: "Grok", local: "Local OCR" };
const isCloud = (mode) => mode === "gemini" || mode === "grok";
function hasKeyFor(mode) {
  return mode === "grok" ? state.settings.has_grok_key : state.settings.has_gemini_key;
}

function setMode(mode) {
  state.mode = mode;
  [["gemini", "mode-gemini"], ["grok", "mode-grok"], ["local", "mode-manual"]].forEach(([key, id]) => {
    const on = mode === key;
    el(id).classList.toggle("is-active", on);
    el(id).setAttribute("aria-selected", String(on));
  });
  // Every mode recognises from uploaded pages; only the recognizer differs.
  el("recognize-label").textContent = isCloud(mode)
    ? `Recognise with ${MODE_LABEL[mode]}`
    : "Run local OCR";
  el("empty-title").textContent = "No rows yet";
  el("empty-sub").textContent = isCloud(mode)
    ? `Upload page photos, then Recognise with ${MODE_LABEL[mode]}. Review and correct, then export.`
    : "Upload page photos, then Run local OCR (offline). Review and correct, then export.";
  // Dim a mode this build cannot run, so it reads as unavailable before it is
  // clicked rather than only after.
  const localBtn = el("mode-manual");
  const localOff = state.settings.local_available === false;
  localBtn.style.opacity = localOff ? "0.45" : "";
  localBtn.title = localOff ? (state.settings.local_unavailable_reason || "") : "";

  updateRecognizeAvailability();
  settingsStoreMode(mode);
}
function settingsStoreMode(mode) {
  apiJSON("/api/settings", { mode }).catch(() => {});
}
el("mode-gemini").addEventListener("click", () => setMode("gemini"));
el("mode-grok").addEventListener("click", () => setMode("grok"));
el("mode-manual").addEventListener("click", () => setMode("local"));

function updateRecognizeAvailability() {
  const btn = el("recognize-btn");
  const hasImages = state.job && state.images.length > 0;
  const needsKey = isCloud(state.mode) && !hasKeyFor(state.mode);
  // Local OCR is absent from the standalone .exe; say so up front rather than
  // letting the user upload, press the button and wait for a failure.
  const localBlocked = state.mode === "local" && state.settings.local_available === false;
  btn.disabled = !hasImages || needsKey || localBlocked;
  btn.title = localBlocked
    ? (state.settings.local_unavailable_reason || "Local OCR is unavailable in this build")
    : !hasImages
      ? "Upload page photos first"
      : (needsKey ? `Add a ${MODE_LABEL[state.mode]} API key in Settings first` : "");

  const note = el("mode-note");
  if (localBlocked) {
    note.textContent = state.settings.local_unavailable_reason || "";
    note.hidden = false;
  } else {
    note.hidden = true;
  }
}

/* --------------------------------------------------------------- upload */
function wireUpload() {
  const input = el("file-input");
  const zone = el("dropzone");
  input.addEventListener("change", () => uploadFiles(input.files));
  ["dragenter", "dragover"].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("dragover"); }));
  zone.addEventListener("drop", (e) => uploadFiles(e.dataTransfer.files));
}

async function uploadFiles(fileList) {
  const files = Array.from(fileList || []).filter((f) => f.type.startsWith("image/"));
  if (!files.length) return;
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  showOverlay(`Uploading ${files.length} image${files.length > 1 ? "s" : ""}…`);
  try {
    const res = await fetch("/api/upload", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Upload failed");
    state.job = data.job;
    state.images = data.images;
    renderThumbs();
    if (data.images.length) selectImage(data.images[0]);
    toast("success", "Uploaded", `${data.images.length} page${data.images.length > 1 ? "s" : ""} ready.`);
  } catch (err) {
    toast("error", "Upload failed", err.message);
  } finally {
    hideOverlay();
    updateRecognizeAvailability();
  }
}

function renderThumbs() {
  el("page-count").textContent = state.images.length;
  const list = el("thumb-list");
  list.innerHTML = "";
  state.images.forEach((img) => {
    const li = document.createElement("li");
    li.className = "thumb" + (state.activeImage === img.name ? " is-active" : "");
    li.innerHTML = `
      <img src="${img.url}" alt="" loading="lazy" />
      <div class="thumb-meta">
        <div class="thumb-name" title="${img.name}">${img.name}</div>
        <div class="thumb-sub">${rowCountForPage(img.name)}</div>
      </div>`;
    li.addEventListener("click", () => selectImage(img));
    list.appendChild(li);
  });
}
function rowCountForPage(name) {
  const stem = name.replace(/\.[^.]+$/, "");
  const n = state.rows.filter((r) => r.page === stem).length;
  return n ? `${n} rows` : "not read";
}

function selectImage(img) {
  state.activeImage = img.name;
  renderThumbs();
  el("preview-pane").style.display = "";
  el("preview-body").innerHTML = `
    <div class="preview-caption">${img.name}</div>
    <img src="${img.url}" alt="Page ${img.name}" />`;
}
el("preview-close").addEventListener("click", () => {
  el("preview-pane").style.display = "none";
});

/* ------------------------------------------------------------- recognise */
el("recognize-btn").addEventListener("click", () => {
  if (!state.job) return toast("warn", "Upload first", "Add page photos before recognising.");
  return isCloud(state.mode) ? runCloud(state.mode) : runLocal();
});

async function runCloud(provider) {
  const label = MODE_LABEL[provider];
  if (!hasKeyFor(provider)) {
    openSettings(provider);
    return toast("warn", "No API key", `Add a ${label} key in Settings.`);
  }
  showOverlay(`Recognising ${state.images.length} page${state.images.length > 1 ? "s" : ""} with ${label}…`);
  try {
    const data = await apiJSON("/api/recognize", { job: state.job, provider });
    applyResult(data);
    (data.recognition_errors || []).forEach((e) => toast("error", `Page ${e.page} failed`, e.error));
    toast("success", "Recognised", `${data.summary.row_count} rows read · ${data.summary.needs_review} need review.`);
  } catch (err) {
    toast("error", "Recognition failed", err.message);
  } finally {
    hideOverlay();
  }
}

async function runLocal() {
  showOverlay(`Reading ${state.images.length} page${state.images.length > 1 ? "s" : ""} with the local OCR engine… this can take a while.`);
  try {
    const data = await apiJSON("/api/recognize_local", { job: state.job });
    applyResult(data);
    (data.recognition_errors || []).forEach((e) => toast("error", `Page ${e.page} skipped`, e.error));
    toast("success", "Local OCR done", `${data.summary.row_count} rows · ${data.summary.needs_review} need review.`);
  } catch (err) {
    toast("error", "Local OCR failed", err.message);
  } finally {
    hideOverlay();
  }
}

/* -------------------------------------------------------------- validate */
el("validate-btn").addEventListener("click", async () => {
  if (!state.rows.length) return toast("warn", "Nothing to validate", "Add or recognise some rows first.");
  showOverlay("Applying correction rules…");
  try {
    const data = await apiJSON("/api/validate", { rows: gatherRows() });
    applyResult(data);
    toast("success", "Validated", `${data.summary.needs_review} of ${data.summary.row_count} rows need review.`);
  } catch (err) {
    toast("error", "Validation failed", err.message);
  } finally {
    hideOverlay();
  }
});

/* --------------------------------------------------------------- export */
el("export-btn").addEventListener("click", async () => {
  if (!state.rows.length) return toast("warn", "Nothing to export", "There are no rows to write.");
  showOverlay("Building workbook…");
  try {
    const data = await apiJSON("/api/export", { rows: gatherRows() });
    hideOverlay();
    const a = document.createElement("a");
    a.href = data.download; a.download = data.filename;
    document.body.appendChild(a); a.click(); a.remove();
    const dirty = data.audit && !data.audit.clean;
    toast(dirty ? "warn" : "success", "Exported",
      `${data.filename} · ${data.row_count} rows` + (dirty ? " (audit warnings)" : ""));
  } catch (err) {
    hideOverlay();
    toast("error", "Export failed", err.message);
  }
});

/* ------------------------------------------------------------- add row */
el("addrow-btn").addEventListener("click", () => {
  const page = state.activeImage ? state.activeImage.replace(/\.[^.]+$/, "") : "manual";
  const idx = state.rows.filter((r) => r.page === page).length;
  state.rows.push(blankRow(page, idx));
  renderGrid();
  const inputs = el("grid-body").querySelectorAll("tr:last-child .cell-input");
  if (inputs[0]) inputs[0].focus();
});
function blankRow(page, index) {
  return {
    id: `${page}#${index}-${Math.random().toString(36).slice(2, 7)}`,
    page, index, survey: "", meter: "", remarks: "",
    annotations: [], needs_review: false,
    cells: {
      survey: emptyCell(), meter: emptyCell(), remarks: emptyCell(),
    },
  };
}
const emptyCell = () => ({ text: "", raw: "", status: "empty", needs_review: false, candidates: [], rules: [] });

/* ---------------------------------------------------- apply + gather */
function applyResult(data) {
  state.rows = data.rows;
  state.audit = data.audit;
  renderGrid();
  renderAudit(data.audit, data.summary);
  renderThumbs();
}
function gatherRows() {
  // Reindex per page in current DOM order so the server groups them correctly.
  const perPage = {};
  return state.rows.map((r) => {
    const page = r.page || "manual";
    perPage[page] = (perPage[page] ?? -1) + 1;
    return { page, index: perPage[page], survey: r.survey, meter: r.meter, remarks: r.remarks };
  });
}

/* --------------------------------------------------------------- grid */
function rowStatusBadge(row) {
  if (row.needs_review) return `<span class="status-badge st-review"><span class="d"></span>Review</span>`;
  const cells = row.cells || {};
  const anyCorrected = ["survey", "meter", "remarks"].some((k) => cells[k] && cells[k].status === "corrected");
  if (anyCorrected) return `<span class="status-badge st-corrected"><span class="d"></span>Corrected</span>`;
  const allEmpty = ["survey", "meter", "remarks"].every((k) => !row[k]);
  if (allEmpty) return `<span class="status-badge st-empty"><span class="d"></span>Empty</span>`;
  return `<span class="status-badge st-ok"><span class="d"></span>OK</span>`;
}

function cellHtml(row, idx, field, mono) {
  const cell = (row.cells && row.cells[field]) || emptyCell();
  const cands = (cell.candidates || []).filter((c) => c && c !== row[field]);
  const value = (row[field] ?? "").toString().replace(/"/g, "&quot;");
  const chips = cands.length
    ? `<div class="candidates">${cands.map((c) =>
        `<button class="chip" data-idx="${idx}" data-field="${field}" data-val="${c.replace(/"/g, "&quot;")}">${c}</button>`).join("")}</div>`
    : "";
  const rules = (cell.rules || []).length && cell.status !== "ok"
    ? `<div class="rules-note" title="${cell.rules.join(', ')}">${prettyRules(cell.rules)}</div>` : "";
  return `<input class="cell-input ${mono ? "mono" : ""}" data-idx="${idx}" data-field="${field}"
            value="${value}" spellcheck="false" />${chips}${rules}`;
}
function prettyRules(rules) {
  const map = {
    repair_1_edit: "prefix fixed", repair_2_edit: "prefix fixed",
    ditto_meter: "repeat mark", ditto_remark: "repeat mark",
    special_token: "placeholder", annotation_extracted: "side code",
    survey_struck_out: "struck out", struck_out_correction: "struck out",
    ambiguous_correction: "ambiguous", non_latin_dropped: "Urdu dropped",
    remarks_joined: "joined remarks", section_marker: "section",
    low_confidence: "low confidence",
  };
  return rules.map((r) => map[r] || r.replace(/_/g, " ")).slice(0, 2).join(" · ");
}

function renderGrid() {
  const body = el("grid-body");
  body.innerHTML = "";
  state.rows.forEach((row, idx) => {
    const tr = document.createElement("tr");
    if (row.needs_review) tr.className = "needs-review";
    tr.innerHTML = `
      <td class="rownum">${idx + 1}</td>
      <td class="pagecell" title="${row.page}">${shortPage(row.page)}</td>
      <td>${cellHtml(row, idx, "survey", true)}</td>
      <td>${cellHtml(row, idx, "meter", true)}</td>
      <td>${cellHtml(row, idx, "remarks", false)}</td>
      <td>${rowStatusBadge(row)}</td>
      <td><button class="icon-btn sm row-del" data-del="${idx}" title="Delete row" aria-label="Delete row">
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>
      </button></td>`;
    body.appendChild(tr);
  });
  el("empty-state").style.display = state.rows.length ? "none" : "";
  el("stat-rows").textContent = state.rows.length;
  el("stat-review").textContent = state.rows.filter((r) => r.needs_review).length;
}
function shortPage(page) {
  if (!page || page === "manual") return "—";
  return page.length > 12 ? "…" + page.slice(-11) : page;
}

// Delegated events on the grid body.
el("grid-body").addEventListener("input", (e) => {
  const t = e.target;
  if (!t.classList.contains("cell-input")) return;
  const row = state.rows[+t.dataset.idx];
  if (row) row[t.dataset.field] = t.value;
});
el("grid-body").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (chip) {
    const row = state.rows[+chip.dataset.idx];
    if (row) {
      row[chip.dataset.field] = chip.dataset.val;
      if (row.cells[chip.dataset.field]) {
        row.cells[chip.dataset.field].status = "corrected";
        row.cells[chip.dataset.field].needs_review = false;
      }
      recomputeReview(row);
      renderGrid();
    }
    return;
  }
  const del = e.target.closest(".row-del");
  if (del) {
    state.rows.splice(+del.dataset.del, 1);
    renderGrid(); renderThumbs();
  }
});
function recomputeReview(row) {
  row.needs_review = ["survey", "meter", "remarks"].some(
    (k) => row.cells[k] && row.cells[k].needs_review);
}

/* --------------------------------------------------------------- audit */
function renderAudit(audit, summary) {
  const banner = el("audit-banner");
  if (!audit) { banner.hidden = true; return; }
  banner.hidden = false;
  if (audit.clean) {
    const [lo, hi] = summary.survey_range || [null, null];
    const range = lo && hi ? ` Survey ${lo}–${hi}.` : "";
    banner.className = "audit-banner clean";
    banner.innerHTML = `<div><strong>Audit clean.</strong>${range} ${summary.row_count} rows, ${summary.skipped} skipped.</div>`;
    return;
  }
  banner.className = "audit-banner warn";
  const items = [];
  audit.survey_gaps.forEach((g) => items.push(`Gap in survey numbers: ${g[0]} → ${g[1]}`));
  if (audit.survey_repeats.length) items.push(`Repeated survey numbers: ${audit.survey_repeats.join(", ")}`);
  if (audit.duplicate_meters.length) items.push(`Duplicate meter codes: ${audit.duplicate_meters.slice(0, 6).join(", ")}${audit.duplicate_meters.length > 6 ? "…" : ""}`);
  audit.row_count_mismatches.forEach((m) => items.push(`Row-count mismatch on ${m[0]} (${m[1]} vs ${m[2]})`));
  banner.innerHTML = `<div><strong>Audit found ${items.length} thing${items.length > 1 ? "s" : ""} to check</strong>
    <ul>${items.map((i) => `<li>${i}</li>`).join("")}</ul></div>`;
}

/* ------------------------------------------------------------- settings */
const modal = el("settings-modal");
const PROVIDER_INFO = {
  gemini: {
    label: "Gemini",
    link: "https://aistudio.google.com/app/apikey",
    models: ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash", "gemini-1.5-pro"],
  },
  grok: {
    label: "Grok (xAI)",
    link: "https://console.x.ai/",
    models: ["grok-4-fast", "grok-4", "grok-4-latest", "grok-3"],
  },
};
//: Model text typed but not yet saved, kept per provider across tab switches.
const draftModel = {};
let settingsProvider = "gemini";

function renderProviderTab() {
  const info = PROVIDER_INFO[settingsProvider];
  ["gemini", "grok"].forEach((key) => {
    const on = key === settingsProvider;
    el(`tab-${key}`).classList.toggle("is-active", on);
    el(`tab-${key}`).setAttribute("aria-selected", String(on));
  });
  el("key-label").textContent = info.label;
  el("key-link").href = info.link;
  el("model-options").innerHTML = info.models
    .map((m) => `<option value="${m}"></option>`).join("");
  el("model-input").value =
    draftModel[settingsProvider] ??
    (settingsProvider === "grok"
      ? state.settings.grok_model || "grok-4-fast"
      : state.settings.gemini_model || "gemini-2.0-flash");
  el("api-key").value = "";
  el("api-key").type = "password";
  el("api-key").placeholder = hasKeyFor(settingsProvider)
    ? "•••••••••• (saved — leave blank to keep)"
    : "Paste your API key";
  el("key-status").textContent = "";
  el("key-status").className = "key-status";
}

function openSettings(provider) {
  settingsProvider = provider && PROVIDER_INFO[provider]
    ? provider
    : (state.mode === "grok" ? "grok" : "gemini");
  Object.keys(draftModel).forEach((k) => delete draftModel[k]);
  el("engine-select").value = state.settings.local_engine || "easyocr";
  renderProviderTab();
  modal.hidden = false;
}
["gemini", "grok"].forEach((key) => {
  el(`tab-${key}`).addEventListener("click", () => {
    draftModel[settingsProvider] = el("model-input").value;
    settingsProvider = key;
    renderProviderTab();
  });
});
function closeSettings() { modal.hidden = true; }
el("settings-btn").addEventListener("click", () => openSettings());
el("settings-close").addEventListener("click", closeSettings);
el("settings-cancel").addEventListener("click", closeSettings);
modal.addEventListener("click", (e) => { if (e.target === modal) closeSettings(); });

el("key-reveal").addEventListener("click", () => {
  const input = el("api-key");
  input.type = input.type === "password" ? "text" : "password";
});

el("test-key-btn").addEventListener("click", async () => {
  const status = el("key-status");
  status.className = "key-status"; status.textContent = "Testing…";
  try {
    const data = await apiJSON("/api/settings/test", {
      provider: settingsProvider,
      api_key: el("api-key").value.trim(),
      model: el("model-input").value.trim(),
    });
    status.textContent = data.message;
    status.className = "key-status " + (data.ok ? "ok" : "err");
  } catch (err) {
    status.textContent = err.message; status.className = "key-status err";
  }
});

el("settings-save").addEventListener("click", async () => {
  // Save the visible tab plus any model edited on the other one.
  draftModel[settingsProvider] = el("model-input").value;
  const payload = { local_engine: el("engine-select").value };
  const key = el("api-key").value.trim();
  if (settingsProvider === "grok") payload.grok_api_key = key;
  else payload.gemini_api_key = key;
  if (draftModel.gemini !== undefined) payload.gemini_model = draftModel.gemini.trim();
  if (draftModel.grok !== undefined) payload.grok_model = draftModel.grok.trim();

  try {
    state.settings = await apiJSON("/api/settings", payload);
    updateRecognizeAvailability();
    closeSettings();
    toast("success", "Settings saved", key ? `${PROVIDER_INFO[settingsProvider].label} key stored.` : "Settings updated.");
  } catch (err) {
    toast("error", "Could not save", err.message);
  }
});

/* ----------------------------------------------------------------- init */
async function init() {
  initTheme();
  wireUpload();
  try {
    state.settings = await apiGet("/api/settings");
  } catch { /* server default is fine */ }
  const mode = state.settings.mode || "local";
  setMode(mode);
  renderGrid();
  if (isCloud(mode) && !hasKeyFor(mode)) {
    // Only offer the offline fallback where it actually exists (it does not in
    // the standalone .exe), so the hint never sends the user somewhere broken.
    toast("warn", `Add a ${MODE_LABEL[mode]} key`,
      state.settings.local_available === false
        ? "Open Settings to enable AI recognition."
        : "Open Settings to enable AI recognition — or switch to Local OCR (offline).");
  }
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeSettings(); } });
init();
