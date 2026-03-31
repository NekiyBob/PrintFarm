const appState = {
  printers: [],
  printerMap: new Map(),
  statusMap: new Map(),
  selectionMode: false,
  drawerPrinterId: null,
  drawerTab: "general",
  drawerDetails: null,
  detailsRequestId: 0,
  detailsRefreshTimer: null,
  drawerActionPending: false,
  maintenanceByPrinter: new Map(),
  materialEditorByPrinter: new Map(),
};

const MAINTENANCE_EVENT_OPTIONS = [
  { value: "CLEANING_LUBRICATION", label: "Чистка и смазка" },
  { value: "NOZZLE_REPLACEMENT", label: "Замена сопла" },
  { value: "FAN_REPLACEMENT", label: "Замена вентилятора" },
  { value: "EXTRUDER_REPLACEMENT", label: "Замена экструдера" },
  { value: "EXTRUDER_CLEANING", label: "Чистка экструдера" },
  { value: "OTHER", label: "Другое" },
];

const MAINTENANCE_NOZZLE_OPTIONS = ["0.2", "0.4", "0.6"];
const MAINTENANCE_PERFORMED_BY_PLACEHOLDER = "Не указано";

async function loadPrinters() {
  const resp = await fetch("/api/printers");
  if (!resp.ok) {
    throw new Error("Ошибка загрузки списка принтеров");
  }
  return await resp.json();
}

function updateSelectionCount() {
  const badge = document.getElementById("selected-count");
  if (!badge) return;
  const count = document.querySelectorAll(".printer-btn.selected").length;
  badge.textContent = `Выбрано: ${count}`;
}

function shortFileName(value) {
  if (!value) return "";
  const normalized = String(value).replaceAll("\\", "/");
  const last = normalized.split("/").pop() || normalized;
  return last.length > 28 ? `${last.slice(0, 25)}...` : last;
}

function formatPercent(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "";
  return `${Math.max(0, Math.min(100, Math.round(num)))}%`;
}

function normalizeProgress(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return 0;
  return Math.max(0, Math.min(100, num));
}

function hasStatusValue(value) {
  return value !== null && value !== undefined && value !== "";
}

function normalizeMaterialValue(value) {
  if (!hasStatusValue(value)) return "";
  const normalized = String(value).trim();
  return normalized === "—" ? "" : normalized;
}

function createDefaultMaterialEditorState(currentValue = "") {
  return {
    isEditing: false,
    draft: normalizeMaterialValue(currentValue),
    saving: false,
    message: "",
    messageKind: "",
    messageTimeoutId: null,
  };
}

function getMaterialEditorState(printerId, currentValue = "") {
  let state = appState.materialEditorByPrinter.get(printerId);
  if (!state) {
    state = createDefaultMaterialEditorState(currentValue);
    appState.materialEditorByPrinter.set(printerId, state);
    return state;
  }

  if (!state.isEditing && !state.saving) {
    state.draft = normalizeMaterialValue(currentValue);
  }

  return state;
}

function clearMaterialEditorMessageTimer(state) {
  if (!state?.messageTimeoutId) return;
  clearTimeout(state.messageTimeoutId);
  state.messageTimeoutId = null;
}

function scheduleMaterialEditorMessageClear(printerId, delayMs = 5000) {
  const state = getMaterialEditorState(printerId);
  clearMaterialEditorMessageTimer(state);
  state.messageTimeoutId = setTimeout(() => {
    const currentState = appState.materialEditorByPrinter.get(printerId);
    if (!currentState) return;
    currentState.message = "";
    currentState.messageKind = "";
    currentState.messageTimeoutId = null;

    if (appState.drawerPrinterId === printerId && appState.drawerTab === "general" && appState.drawerDetails) {
      renderPrinterDetails(appState.drawerDetails);
    }
  }, delayMs);
}

function createDefaultMaintenanceDraft() {
  return {
    eventType: "NOZZLE_REPLACEMENT",
    nozzleDiameter: "0.4",
    customTypeName: "",
    printHours: "",
    note: "",
  };
}

function getMaintenanceUiState(printerId) {
  let state = appState.maintenanceByPrinter.get(printerId);
  if (!state) {
    state = {
      items: null,
      loading: false,
      error: "",
      requestId: 0,
      submitting: false,
      formMessage: "",
      formMessageKind: "",
      draft: createDefaultMaintenanceDraft(),
    };
    appState.maintenanceByPrinter.set(printerId, state);
  }
  return state;
}

function setDrawerTab(nextTab) {
  if (!["general", "maintenance"].includes(nextTab)) return;
  appState.drawerTab = nextTab;

  if (appState.drawerDetails) {
    renderPrinterDetails(appState.drawerDetails);
  }

  if (nextTab === "maintenance" && appState.drawerPrinterId) {
    void loadMaintenanceHistoryForPrinter(appState.drawerPrinterId);
  }
}

function setSelectionMode(nextValue) {
  const wasSelectionMode = appState.selectionMode;
  appState.selectionMode = nextValue;
  document.body.classList.toggle("selection-mode", nextValue);

  const toggleBtn = document.getElementById("select-mode-btn");
  const hint = document.getElementById("grid-hint");
  const jobPanel = document.getElementById("job-panel");
  if (toggleBtn) {
    toggleBtn.classList.toggle("is-active", nextValue);
    toggleBtn.textContent = nextValue ? "Готово" : "Выбрать";
  }

  if (jobPanel) {
    jobPanel.hidden = !nextValue;
  }

  if (hint) {
    hint.textContent = nextValue
      ? "Режим выбора активен. Клик по принтеру отмечает его для массовых действий."
      : 'Обычный клик открывает информацию о принтере. Режим "Выбрать" включает множественный выбор.';
  }

  if (nextValue) {
    closeDrawer();
  }

  if (wasSelectionMode && !nextValue) {
    document.querySelectorAll(".printer-btn.selected").forEach((btn) => {
      btn.classList.remove("selected");
    });
  }

  updateSelectionCount();
}

function stopDetailsAutoRefresh() {
  if (appState.detailsRefreshTimer) {
    clearInterval(appState.detailsRefreshTimer);
    appState.detailsRefreshTimer = null;
  }
}

function startDetailsAutoRefresh() {
  stopDetailsAutoRefresh();
  if (!appState.drawerPrinterId) return;

  appState.detailsRefreshTimer = setInterval(() => {
    if (!appState.drawerPrinterId) {
      stopDetailsAutoRefresh();
      return;
    }
    if (appState.drawerTab === "maintenance") {
      return;
    }
    const materialEditorState = appState.materialEditorByPrinter.get(appState.drawerPrinterId);
    if (materialEditorState?.isEditing || materialEditorState?.saving) {
      return;
    }
    loadPrinterDetails(appState.drawerPrinterId, { silent: true });
  }, 5000);
}

function closeDrawer() {
  appState.drawerPrinterId = null;
  appState.drawerDetails = null;
  appState.drawerTab = "general";
  stopDetailsAutoRefresh();
  document.body.classList.remove("drawer-open");

  const drawer = document.getElementById("details-drawer");
  if (drawer) {
    drawer.setAttribute("aria-hidden", "true");
  }
}

function openDrawerShell(printer, { silent = false } = {}) {
  const isNewPrinter = appState.drawerPrinterId !== printer.id;
  appState.drawerPrinterId = printer.id;
  if (isNewPrinter) {
    appState.drawerTab = "general";
    appState.drawerDetails = null;
  }
  document.body.classList.add("drawer-open");

  const drawer = document.getElementById("details-drawer");
  const eyebrow = drawer ? drawer.querySelector(".drawer-eyebrow") : null;
  const title = document.getElementById("drawer-title");
  const content = document.getElementById("drawer-content");
  if (drawer) {
    drawer.setAttribute("aria-hidden", "false");
  }
  if (eyebrow) {
    eyebrow.textContent = `Принтер ${printer.id}`;
  }
  if (title) {
    title.textContent = printer.name || printer.id;
  }
}

function buildCachedPrinterDetails(printer) {
  const cachedStatus = appState.statusMap.get(printer.id) || {};
  const stateUpper = String(cachedStatus.gcode_state || "").toUpperCase();
  const activeStates = new Set(["RUNNING", "PRINTING", "PAUSE", "PAUSED", "PREPARE", "PREPARING"]);
  const isActivePrint = activeStates.has(stateUpper);

  return {
    id: printer.id,
    name: printer.name,
    ip: printer.ip,
    model: printer.model,
    configured: Boolean(printer.configured),
    status: {
      ok: cachedStatus.ok,
      error: cachedStatus.error,
      gcode_state: cachedStatus.gcode_state || null,
      is_active_print: isActivePrint,
      current_file: isActivePrint ? (cachedStatus.file || cachedStatus.current_file || null) : null,
      current_layer: cachedStatus.current_layer ?? null,
      total_layers: cachedStatus.total_layers ?? null,
      remaining_time_min: cachedStatus.remaining_time_min ?? null,
      progress_percent: cachedStatus.progress_percent ?? null,
      nozzle_temp: cachedStatus.nozzle_temp ?? null,
      nozzle_target_temp: cachedStatus.nozzle_target_temp ?? null,
      bed_temp: cachedStatus.bed_temp ?? null,
      bed_target_temp: cachedStatus.bed_target_temp ?? null,
      nozzle_diameter: cachedStatus.nozzle_diameter ?? null,
      loaded_material: cachedStatus.loaded_material ?? null,
      filament_remaining_g: cachedStatus.filament_remaining_g ?? null,
      can_pause: ["RUNNING", "PRINTING", "PREPARE", "PREPARING"].includes(stateUpper),
      can_resume: ["PAUSE", "PAUSED"].includes(stateUpper),
      can_stop: isActivePrint,
    },
  };
}

function buildPrinterButtonContent(printer) {
  const wrapper = document.createElement("span");
  wrapper.className = "printer-btn-content";

  const top = document.createElement("span");
  top.className = "printer-btn-top";

  const id = document.createElement("span");
  id.className = "printer-btn-id";
  id.textContent = printer.id;

  const progress = document.createElement("span");
  progress.className = "printer-btn-progress";
  progress.textContent = "";

  const file = document.createElement("span");
  file.className = "printer-btn-file";
  file.textContent = "";

  const meta = document.createElement("span");
  meta.className = "printer-btn-meta";
  meta.innerHTML = buildPrinterCardMeta(printer.model || "", "", "");

  top.appendChild(id);
  top.appendChild(progress);
  wrapper.appendChild(top);
  wrapper.appendChild(file);
  wrapper.appendChild(meta);

  return { wrapper, progress, file, meta };
}

function buildGrid(printers) {
  const layout = {};
  const fixedRows = [1, 2];
  const fixedRacks = [1, 2, 3, 4, 5, 6];

  function handlePrinterClick(btn, printer) {
    if (!printer) return;

    const configured = btn.dataset.configured === "true";
    if (appState.selectionMode) {
      if (!configured) return;
      btn.classList.toggle("selected");
      updateSelectionCount();
      return;
    }

    openDrawerShell(printer, { silent: true });
    renderPrinterDetails(buildCachedPrinterDetails(printer));
    requestAnimationFrame(() => {
      if (appState.drawerPrinterId !== printer.id) return;
      loadPrinterDetails(printer.id, { silent: true, skipOpen: true });
    });
  }

  function makePrinterButton(printer) {
    const btn = document.createElement("button");
    btn.className = "printer-btn";
    btn.style.setProperty("--progress", "0");

    if (!printer) {
      btn.textContent = "—";
      btn.disabled = true;
      return btn;
    }

    btn.dataset.printerId = printer.id;
    btn.dataset.configured = String(Boolean(printer.configured));
    btn.dataset.model = printer.model || "";

    const content = buildPrinterButtonContent(printer);
    btn.appendChild(content.wrapper);

    if (!printer.configured) {
      btn.title = "Принтер не настроен";
    }

    btn.addEventListener("click", () => handlePrinterClick(btn, printer));
    return btn;
  }

  for (const printer of printers) {
    const { row, rack, level, slot } = printer;
    layout[row] = layout[row] || {};
    layout[row][rack] = layout[row][rack] || {};
    layout[row][rack][level] = layout[row][rack][level] || {};
    layout[row][rack][level][slot] = printer;
  }

  const grid = document.getElementById("grid");
  grid.innerHTML = "";

  fixedRows.forEach((rowNum) => {
    const rowDiv = document.createElement("div");
    rowDiv.className = "rack-row";

    for (const rackNum of fixedRacks) {
      const rackPrinters = layout[rowNum]?.[rackNum] || {};
      const rackDiv = document.createElement("div");
      rackDiv.className = "rack";

      const title = document.createElement("div");
      title.className = "rack-title";
      title.textContent = `Row ${rowNum}, Rack ${rackNum}`;
      rackDiv.appendChild(title);

      title.addEventListener("click", () => {
        if (!appState.selectionMode) return;

        const buttons = rackDiv.querySelectorAll(".printer-btn");
        const enabled = Array.from(buttons).filter(
          (btn) => !btn.disabled && btn.dataset.configured === "true"
        );
        const allSelected =
          enabled.length > 0 &&
          enabled.every((btn) => btn.classList.contains("selected"));
        enabled.forEach((btn) => btn.classList.toggle("selected", !allSelected));
        updateSelectionCount();
      });

      for (let slotNum = 1; slotNum <= 6; slotNum++) {
        const levelDiv = document.createElement("div");
        levelDiv.className = "level";

        const pL1 = rackPrinters[1]?.[slotNum];
        const pL2 = rackPrinters[2]?.[slotNum];

        levelDiv.appendChild(makePrinterButton(pL1));
        levelDiv.appendChild(makePrinterButton(pL2));
        rackDiv.appendChild(levelDiv);
      }

      rowDiv.appendChild(rackDiv);
    }

    grid.appendChild(rowDiv);
  });
}

function applyStatusClass(btn, status) {
  btn.classList.remove("st-running", "st-pause", "st-offline", "st-error", "st-finish");

  const fileEl = btn.querySelector(".printer-btn-file");
  const metaEl = btn.querySelector(".printer-btn-meta");
  const progressEl = btn.querySelector(".printer-btn-progress");
  const model = btn.dataset.model || "";

  if (!status) {
    btn.style.setProperty("--progress", "0");
    if (fileEl) fileEl.textContent = "";
    if (metaEl) metaEl.innerHTML = buildPrinterCardMeta(model, "", "");
    if (progressEl) progressEl.textContent = "";
    return;
  }

  const hasProgress = hasStatusValue(status.progress_percent);
  const progress = hasProgress ? normalizeProgress(status.progress_percent) : 0;
  const state = (status.gcode_state || "").toUpperCase();
  const fileName = shortFileName(status.file);
  const nozzleDiameter = hasStatusValue(status.nozzle_diameter) ? String(status.nozzle_diameter) : "";
  const loadedMaterial = hasStatusValue(status.loaded_material) ? String(status.loaded_material) : "";

  const showFill = state === "RUNNING" || state === "PRINTING";
  const showProgress = showFill || state === "PAUSE" || state === "PAUSED";

  btn.style.setProperty("--progress", String(showFill ? progress : 0));
  if (fileEl) {
    fileEl.textContent = fileName || "";
  }
  if (metaEl) {
    metaEl.innerHTML = buildPrinterCardMeta(model, nozzleDiameter, loadedMaterial);
  }
  if (progressEl) {
    progressEl.textContent = showProgress && hasProgress ? formatPercent(progress) : "";
  }

  if (status.ok === false) {
    btn.classList.add("st-offline");
    btn.title = "Offline / нет данных";
    return;
  }

  if (status.error === "filament_runout") {
    btn.classList.add("st-error");
    btn.title = "Закончился филамент";
    return;
  }

  btn.title = `ok=${status.ok} state=${status.gcode_state || "NONE"} err=${status.error || ""}`;

  if (state === "RUNNING" || state === "PRINTING") {
    btn.classList.add("st-running");
  } else if (state === "PAUSE" || state === "PAUSED") {
    btn.classList.add("st-pause");
  } else if (state === "FINISH" && status.finish_recent) {
    btn.classList.add("st-finish");
  }
}

function buildPrinterCardMeta(model, nozzleDiameter, loadedMaterial) {
  const safeModel = escapeHtml(model || "");
  const nozzleLabel = nozzleDiameter
    ? `<span class="printer-nozzle-label">${escapeHtml(nozzleDiameter)}</span>`
    : "";
  const materialBadge = buildPrinterMaterialBadge(loadedMaterial);

  return `
    <span class="printer-btn-main-meta">
      <span class="printer-btn-model">${safeModel}</span>
      ${nozzleLabel}
    </span>
    <span class="printer-btn-side-meta">${materialBadge}</span>
  `;
}

function buildPrinterMaterialBadge(materialValue) {
  if (!hasStatusValue(materialValue)) return "";

  const materialText = String(materialValue).trim();
  if (!materialText) return "";

  const materialUpper = materialText.toUpperCase();
  let tone = "is-blue";
  if (materialUpper.includes("PA-CF")) {
    tone = "is-orange";
  } else if (materialUpper.includes("PETG")) {
    tone = "is-green";
  }

  return `<span class="printer-material-badge ${tone}">${escapeHtml(materialText)}</span>`;
}

function updatePrinterMaterialInUi(printerId, loadedMaterial) {
  const currentStatus = appState.statusMap.get(printerId) || { id: printerId };
  const nextStatus = {
    ...currentStatus,
    loaded_material: normalizeMaterialValue(loadedMaterial) || null,
  };
  appState.statusMap.set(printerId, nextStatus);

  document.querySelectorAll(".printer-btn").forEach((btn) => {
    if (btn.dataset.printerId === printerId) {
      applyStatusClass(btn, nextStatus);
    }
  });
}

async function refreshStatuses() {
  const resp = await fetch("/api/status");
  if (!resp.ok) return;

  const data = await resp.json();
  const statusMap = new Map();
  for (const status of data.statuses || []) {
    statusMap.set(status.id, status);
  }
  appState.statusMap = statusMap;

  document.querySelectorAll(".printer-btn").forEach((btn) => {
    const pid = btn.dataset.printerId;
    const status = statusMap.get(pid) || null;
    applyStatusClass(btn, status);
  });
}

async function pollJob(jobId, statusEl, buttonsToDisable = []) {
  for (const btn of buttonsToDisable) btn.disabled = true;

  async function step() {
    const resp = await fetch(`/api/jobs/${jobId}`);
    if (!resp.ok) {
      statusEl.textContent += "\nНе могу получить прогресс.";
      for (const btn of buttonsToDisable) btn.disabled = false;
      return;
    }

    const job = await resp.json();
    let out =
      `Job: ${job.job_id}\n` +
      `Файл: ${job.filename}\n` +
      `Готово: ${job.done}/${job.total} | OK: ${job.ok_count} | ERR: ${job.err_count}\n\n`;

    for (const [pid, state] of Object.entries(job.printers || {})) {
      const msg = state.message ? ` (${state.message})` : "";
      const file = state.file ? ` [${state.file}]` : "";
      out += `${pid}${file}: ${state.stage}${msg}\n`;
    }

    statusEl.textContent = out;

    if (!job.finished) {
      setTimeout(step, 2000);
      return;
    }

    statusEl.textContent += "\nГотово.";
    for (const btn of buttonsToDisable) btn.disabled = false;
  }

  step();
}

function getSelectedPrinters() {
  return Array.from(document.querySelectorAll(".printer-btn.selected")).map(
    (btn) => btn.dataset.printerId
  );
}

function formatValue(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "—";
  return `${value}${suffix}`;
}

function formatTemperature(current, target) {
  const cur = current === null || current === undefined ? "—" : `${Math.round(current)}°C`;
  const tar = target === null || target === undefined ? "—" : `${Math.round(target)}°C`;
  return `${cur} / ${tar}`;
}

function formatRemainingTime(minutes) {
  if (minutes === null || minutes === undefined) return "—";
  const total = Number(minutes);
  if (!Number.isFinite(total) || total <= 0) return "—";
  const hours = Math.floor(total / 60);
  const mins = total % 60;
  if (hours === 0) return `${mins} мин`;
  return `${hours} ч ${mins} мин`;
}

function formatLayer(current, total) {
  if (current === null || current === undefined) return "—";
  if (total === null || total === undefined || total <= 0) return String(current);
  return `${current} / ${total}`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderPrinterDetails(details) {
  const title = document.getElementById("drawer-title");
  const content = document.getElementById("drawer-content");
  const eyebrow = document.querySelector("#details-drawer .drawer-eyebrow");
  if (!content) return;

  if (eyebrow) {
    eyebrow.textContent = `Принтер ${details.id || "—"}`;
  }

  if (title) {
    title.textContent = details.name || details.id;
  }

  const status = details.status || {};
  const hasProgress = hasStatusValue(status.progress_percent);
  const progress = hasProgress ? normalizeProgress(status.progress_percent) : 0;
  const fileName = status.is_active_print ? status.current_file || "Не удалось определить" : "Сейчас не печатает";
  const stateText = status.gcode_state || status.error || "Нет данных";

  content.innerHTML = `
    <section class="drawer-section">
      <h4>Основная информация</h4>
      <div class="drawer-meta">
        <div class="meta-item">
          <span class="meta-label">Название</span>
          <span class="meta-value">${details.name || "—"}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">IP</span>
          <span class="meta-value">${details.ip || "—"}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Модель</span>
          <span class="meta-value">${details.model || "—"}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Состояние</span>
          <span class="meta-value">${stateText}</span>
        </div>
      </div>
    </section>

    <section class="drawer-section">
      <h4>Печать</h4>
      <div class="progress-card">
        <div class="progress-line">
          <span>Файл</span>
          <strong>${fileName}</strong>
        </div>
        <div class="progress-line">
          <span>Слой</span>
          <strong>${formatLayer(status.current_layer, status.total_layers)}</strong>
        </div>
        <div class="progress-line">
          <span>Осталось времени</span>
          <strong>${formatRemainingTime(status.remaining_time_min)}</strong>
        </div>
        <div class="progress-line">
          <span>Прогресс</span>
          <strong>${hasProgress ? formatValue(Math.round(progress), "%") : "—"}</strong>
        </div>
        <div class="progress-track">
          <div class="progress-fill" style="width: ${progress}%"></div>
        </div>
      </div>
    </section>

    <section class="drawer-section">
      <h4>Температуры</h4>
      <div class="temp-grid">
        <div class="temp-card">
          <strong>Сопло</strong>
          <span>${formatTemperature(status.nozzle_temp, status.nozzle_target_temp)}</span>
        </div>
        <div class="temp-card">
          <strong>Стол</strong>
          <span>${formatTemperature(status.bed_temp, status.bed_target_temp)}</span>
        </div>
      </div>
    </section>
  `;
}

function renderPrinterDetails(details) {
  const title = document.getElementById("drawer-title");
  const content = document.getElementById("drawer-content");
  if (!content) return;

  if (title) {
    title.textContent = details.name || details.id;
  }

  const status = details.status || {};
  const hasProgress = hasStatusValue(status.progress_percent);
  const progress = hasProgress ? normalizeProgress(status.progress_percent) : 0;
  const fileName = status.is_active_print ? status.current_file || "Не удалось определить" : "Сейчас не печатает";
  const stateText = status.gcode_state || status.error || "Нет данных";
  const nozzleDiameter = status.nozzle_diameter || "—";
  const loadedMaterial = status.loaded_material || "—";
  const filamentRemaining = hasStatusValue(status.filament_remaining_g)
    ? formatValue(status.filament_remaining_g, " г")
    : "—";
  const primaryAction = status.can_resume ? "resume" : "pause";
  const primaryActionLabel = status.can_resume ? "Возобновить" : "Пауза";
  const primaryDisabled = (!status.can_pause && !status.can_resume) || appState.drawerActionPending ? "disabled" : "";
  const pauseDisabled = primaryDisabled;
  const stopDisabled = !status.can_stop || appState.drawerActionPending ? "disabled" : "";

  content.innerHTML = `
    <section class="drawer-section">
      <h4>Основная информация</h4>
      <div class="drawer-meta">
        <div class="meta-item">
          <span class="meta-label">Название</span>
          <span class="meta-value">${escapeHtml(details.name || "—")}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">IP</span>
          <span class="meta-value">${escapeHtml(details.ip || "—")}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Модель</span>
          <span class="meta-value">${escapeHtml(details.model || "—")}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Состояние</span>
          <span class="meta-value">${escapeHtml(stateText)}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Диаметр сопла</span>
          <span class="meta-value">${escapeHtml(nozzleDiameter)}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Материал</span>
          <span class="meta-value">${escapeHtml(loadedMaterial)}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Остаток пластика</span>
          <span class="meta-value">${escapeHtml(filamentRemaining)}</span>
        </div>
      </div>
    </section>

    <section class="drawer-section">
      <div class="section-head">
        <h4>Печать</h4>
        <div class="section-actions">
          <button type="button" class="btn btn-small" data-printer-action="pause" ${pauseDisabled}>Пауза</button>
          <button type="button" class="btn btn-small btn-danger" data-printer-action="stop" ${stopDisabled}>Отмена</button>
        </div>
      </div>
      <div class="progress-card">
        <div class="progress-line">
          <span>Файл</span>
          <strong>${escapeHtml(fileName)}</strong>
        </div>
        <div class="progress-line">
          <span>Слой</span>
          <strong>${formatLayer(status.current_layer, status.total_layers)}</strong>
        </div>
        <div class="progress-line">
          <span>Осталось времени</span>
          <strong>${formatRemainingTime(status.remaining_time_min)}</strong>
        </div>
        <div class="progress-line">
          <span>Прогресс</span>
          <strong>${hasProgress ? formatValue(Math.round(progress), "%") : "—"}</strong>
        </div>
        <div class="progress-track">
          <div class="progress-fill" style="width: ${progress}%"></div>
        </div>
        <div id="print-action-status" class="drawer-inline-status"></div>
      </div>
    </section>

    <section class="drawer-section">
      <h4>Температуры</h4>
      <div class="temp-grid">
        <div class="temp-card">
          <strong>Сопло</strong>
          <span>${formatTemperature(status.nozzle_temp, status.nozzle_target_temp)}</span>
        </div>
        <div class="temp-card">
          <strong>Стол</strong>
          <span>${formatTemperature(status.bed_temp, status.bed_target_temp)}</span>
        </div>
      </div>
    </section>
  `;

  const primaryButton = content.querySelector('.section-actions .btn-small:not(.btn-danger)');
  if (primaryButton) {
    primaryButton.dataset.printerAction = primaryAction;
    primaryButton.textContent = primaryActionLabel;
    primaryButton.disabled = primaryDisabled === "disabled";
  }

  content.querySelectorAll("[data-printer-action]").forEach((button) => {
    button.addEventListener("click", () => runPrinterAction(button.dataset.printerAction));
  });
}

async function runPrinterAction(action) {
  if (!appState.drawerPrinterId || appState.drawerActionPending) return;

  const statusEl = document.getElementById("print-action-status");
  appState.drawerActionPending = true;
  const actionMessages = {
    pause: "Ставим на паузу...",
    resume: "Возобновляем печать...",
    stop: "Отменяем печать...",
  };

  if (statusEl) {
    statusEl.textContent = actionMessages[action] || "Выполняем действие...";
    statusEl.textContent = action === "pause" ? "Ставим на паузу..." : "Отменяем печать...";
  }

  if (statusEl) {
    statusEl.textContent = actionMessages[action] || "Выполняем действие...";
  }

  try {
    const resp = await fetch(`/api/printers/${encodeURIComponent(appState.drawerPrinterId)}/${action}`, {
      method: "POST",
    });
    const data = await resp.json().catch(async () => ({ error: await resp.text() }));

    if (!resp.ok || !data.ok) {
      throw new Error(data.error || "action_failed");
    }

    await refreshStatuses();
    await loadPrinterDetails(appState.drawerPrinterId, { silent: true });
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = `Ошибка: ${e.message}`;
    }
  } finally {
    appState.drawerActionPending = false;
  }
}

async function loadPrinterDetails(printerId, { silent = false, skipOpen = false } = {}) {
  const printer = appState.printerMap.get(printerId);
  if (!printer) return;

  if (!skipOpen) {
    openDrawerShell(printer, { silent });
  }
  const requestId = ++appState.detailsRequestId;

  try {
    const resp = await fetch(`/api/printers/${encodeURIComponent(printerId)}/details`);
    const data = await resp.json().catch(async () => ({ error: await resp.text() }));

    if (requestId !== appState.detailsRequestId) return;

    if (!resp.ok) {
      throw new Error(data.error || "Не удалось получить информацию о принтере");
    }

    renderPrinterDetails(data);
    startDetailsAutoRefresh();
  } catch (e) {
    const content = document.getElementById("drawer-content");
    if (!content) return;
    content.innerHTML = `<div class="drawer-empty">Не удалось загрузить данные: ${e.message}</div>`;
  }
}

async function submitFileJob({ endpoint, statusText, statusEl, fileInput, buttonsToDisable }) {
  const printers = getSelectedPrinters();
  if (printers.length === 0) {
    statusEl.textContent = "Выберите хотя бы один принтер.";
    return;
  }

  if (!fileInput.files || fileInput.files.length === 0) {
    statusEl.textContent = "Сначала выберите файл.";
    return;
  }

  const file = fileInput.files[0];
  statusEl.textContent = `${statusText}\n`;

  try {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("printers", JSON.stringify(printers));

    const resp = await fetch(endpoint, {
      method: "POST",
      body: formData,
    });

    const data = await resp.json().catch(async () => ({ error: await resp.text() }));
    if (!resp.ok) {
      statusEl.textContent += `Ошибка: ${data.error || "unknown"}`;
      return;
    }

    const jobId = data.job_id;
    if (!jobId) {
      statusEl.textContent += "Ошибка: сервер не вернул job_id";
      return;
    }

    statusEl.textContent = `Задача создана: ${jobId}\n`;
    pollJob(jobId, statusEl, buttonsToDisable);
  } catch (e) {
    statusEl.textContent += "Ошибка отправки: " + e.message;
    buttonsToDisable.forEach((btn) => {
      btn.disabled = false;
    });
  }
}

function formatDrawerDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatMaintenanceComment(value) {
  if (value === null || value === undefined || value === "") return "—";
  return escapeHtml(String(value)).replaceAll("\n", "<br>");
}

function getMaintenanceTypeLabel(eventType, customTypeName) {
  const labels = {
    FAN_REPLACEMENT: "Замена вентилятора",
    NOZZLE_REPLACEMENT: "Замена сопла",
    CLEANING_LUBRICATION: "Чистка и смазка",
    EXTRUDER_REPLACEMENT: "Замена экструдера",
    EXTRUDER_CLEANING: "Чистка экструдера",
    OTHER: customTypeName || "Другое",
  };
  return labels[eventType] || eventType || "—";
}

function formatMaintenanceNozzleLabel(value) {
  if (!hasStatusValue(value)) return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return `${value} мм`;
  return `${numeric.toFixed(1)} мм`;
}

function formatMaintenancePrintHoursLabel(value) {
  if (!hasStatusValue(value)) return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return `${value} ч`;
  return `${new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  }).format(numeric)} ч`;
}

function buildPrettySelect(fieldName, value, options, { disabled = false } = {}) {
  const selectedOption = options.find((option) => option.value === value) || options[0] || null;
  const stateClass = disabled ? " is-disabled" : "";
  const triggerLabel = selectedOption ? selectedOption.label : "";

  return `
    <details class="pretty-select${stateClass}" ${disabled ? 'data-disabled="true"' : ""}>
      <summary class="pretty-select-trigger" aria-haspopup="listbox">
        <span class="pretty-select-trigger-text">${escapeHtml(triggerLabel)}</span>
        <span class="pretty-select-trigger-icon" aria-hidden="true"></span>
      </summary>
      <div class="pretty-select-menu" role="listbox">
        ${options.map((option) => `
          <button
            type="button"
            class="pretty-select-option ${option.value === value ? "is-selected" : ""}"
            data-select-field="${fieldName}"
            data-select-value="${option.value}"
            role="option"
            aria-selected="${option.value === value ? "true" : "false"}"
            ${disabled ? "disabled" : ""}
          >
            <span class="pretty-select-option-label">${escapeHtml(option.label)}</span>
            <span class="pretty-select-option-check" aria-hidden="true">${option.value === value ? "✓" : ""}</span>
          </button>
        `).join("")}
      </div>
    </details>
  `;
}

function buildGeneralDetailsTab(details) {
  const status = details.status || {};
  const hasProgress = hasStatusValue(status.progress_percent);
  const progress = hasProgress ? normalizeProgress(status.progress_percent) : 0;
  const fileName = status.is_active_print ? status.current_file || "Не удалось определить" : "Сейчас не печатает";
  const stateText = status.gcode_state || status.error || "Нет данных";
  const nozzleDiameter = status.nozzle_diameter || "—";
  const loadedMaterial = normalizeMaterialValue(status.loaded_material);
  const filamentRemaining = hasStatusValue(status.filament_remaining_g)
    ? formatValue(status.filament_remaining_g, " г")
    : "—";
  const primaryAction = status.can_resume ? "resume" : "pause";
  const primaryActionLabel = status.can_resume ? "Возобновить" : "Пауза";
  const primaryDisabled = (!status.can_pause && !status.can_resume) || appState.drawerActionPending ? "disabled" : "";
  const stopDisabled = !status.can_stop || appState.drawerActionPending ? "disabled" : "";
  const materialEditorState = getMaterialEditorState(details.id, loadedMaterial);
  const materialStatusClass = materialEditorState.messageKind
    ? ` drawer-inline-status is-${materialEditorState.messageKind}`
    : " drawer-inline-status";
  const materialValueMarkup = loadedMaterial
    ? buildPrinterMaterialBadge(loadedMaterial)
    : '<span class="material-inline-placeholder">Не указан</span>';
  const materialDisabled = materialEditorState.saving ? "disabled" : "";

  return `
    <section class="drawer-section">
      <h4>Основная информация</h4>
      <div class="drawer-meta">
        <div class="meta-item">
          <span class="meta-label">Название</span>
          <span class="meta-value">${escapeHtml(details.name || "—")}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">IP</span>
          <span class="meta-value">${escapeHtml(details.ip || "—")}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Модель</span>
          <span class="meta-value">${escapeHtml(details.model || "—")}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Состояние</span>
          <span class="meta-value">${escapeHtml(stateText)}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Диаметр сопла</span>
          <span class="meta-value">${escapeHtml(nozzleDiameter)}</span>
        </div>
        <div class="meta-item meta-item-material">
          <div class="meta-label-row">
            <span class="meta-label">Материал</span>
            ${materialEditorState.isEditing ? "" : `
              <button
                type="button"
                class="material-label-edit-btn"
                data-material-edit
                aria-label="Изменить материал"
                title="Изменить материал"
              >
                <span aria-hidden="true">✎</span>
              </button>
            `}
          </div>
          ${materialEditorState.isEditing ? `
            <form id="material-editor-form" class="material-inline-form" autocomplete="off">
              <input
                type="text"
                name="material_value"
                class="input material-inline-input"
                placeholder="Например, PA-CF"
                value="${escapeHtml(materialEditorState.draft)}"
                autocomplete="off"
                autocapitalize="off"
                autocorrect="off"
                spellcheck="false"
                data-lpignore="true"
                ${materialDisabled}
              />
              <div class="material-inline-actions">
                <button type="submit" class="btn btn-small btn-primary" ${materialDisabled}>Сохранить</button>
                <button type="button" class="btn btn-small" data-material-cancel ${materialDisabled}>Отмена</button>
              </div>
              <div id="material-editor-status" class="${materialStatusClass.trim()}">${escapeHtml(materialEditorState.message || "")}</div>
            </form>
          ` : `
            <button type="button" class="material-inline-trigger" data-material-edit>
              ${materialValueMarkup}
            </button>
            <div id="material-editor-status" class="${materialStatusClass.trim()}">${escapeHtml(materialEditorState.message || "")}</div>
          `}
        </div>
        <div class="meta-item">
          <span class="meta-label">Остаток пластика</span>
          <span class="meta-value">${escapeHtml(filamentRemaining)}</span>
        </div>
      </div>
    </section>

    <section class="drawer-section">
      <div class="section-head">
        <h4>Печать</h4>
        <div class="section-actions">
          <button type="button" class="btn btn-small" data-printer-action="${primaryAction}" ${primaryDisabled}>${primaryActionLabel}</button>
          <button type="button" class="btn btn-small btn-danger" data-printer-action="stop" ${stopDisabled}>Отмена</button>
        </div>
      </div>
      <div class="progress-card">
        <div class="progress-line">
          <span>Файл</span>
          <strong>${escapeHtml(fileName)}</strong>
        </div>
        <div class="progress-line">
          <span>Слой</span>
          <strong>${formatLayer(status.current_layer, status.total_layers)}</strong>
        </div>
        <div class="progress-line">
          <span>Осталось времени</span>
          <strong>${formatRemainingTime(status.remaining_time_min)}</strong>
        </div>
        <div class="progress-line">
          <span>Прогресс</span>
          <strong>${hasProgress ? formatValue(Math.round(progress), "%") : "—"}</strong>
        </div>
        <div class="progress-track">
          <div class="progress-fill" style="width: ${progress}%"></div>
        </div>
        <div id="print-action-status" class="drawer-inline-status"></div>
      </div>
    </section>

    <section class="drawer-section">
      <h4>Температуры</h4>
      <div class="temp-grid">
        <div class="temp-card">
          <strong>Сопло</strong>
          <span>${formatTemperature(status.nozzle_temp, status.nozzle_target_temp)}</span>
        </div>
        <div class="temp-card">
          <strong>Стол</strong>
          <span>${formatTemperature(status.bed_temp, status.bed_target_temp)}</span>
        </div>
      </div>
    </section>
  `;
}

function buildMaintenanceForm(printerId) {
  const state = getMaintenanceUiState(printerId);
  const draft = state.draft;
  const disabled = state.submitting ? "disabled" : "";
  const showNozzleField = draft.eventType === "NOZZLE_REPLACEMENT";
  const showCustomTypeField = draft.eventType === "OTHER";
  const formStatusClass = state.formMessageKind ? ` drawer-inline-status is-${state.formMessageKind}` : " drawer-inline-status";
  const nozzleOptions = MAINTENANCE_NOZZLE_OPTIONS.map((option) => ({ value: option, label: `${option} мм` }));

  return `
    <section class="drawer-section maintenance-form-section">
      <div class="section-head maintenance-section-head">
        <div>
          <h4>Новая запись</h4>
          <div class="section-subtitle">Дата записи подставляется автоматически.</div>
        </div>
      </div>

      <form id="maintenance-form" class="maintenance-form">
        <label class="maintenance-field">
          <span class="maintenance-label">Тип обслуживания</span>
          ${buildPrettySelect("event_type", draft.eventType, MAINTENANCE_EVENT_OPTIONS, { disabled: Boolean(disabled) })}
        </label>

        ${showNozzleField ? `
          <label class="maintenance-field">
            <span class="maintenance-label">Диаметр сопла</span>
            ${buildPrettySelect("nozzle_diameter", draft.nozzleDiameter, nozzleOptions, { disabled: Boolean(disabled) })}
          </label>
        ` : ""}

        ${showCustomTypeField ? `
          <label class="maintenance-field">
            <span class="maintenance-label">Описание обслуживания</span>
            <input
              type="text"
              name="custom_type_name"
              class="input maintenance-input"
              placeholder="Опишите выполненное обслуживание"
              value="${escapeHtml(draft.customTypeName)}"
              ${disabled}
            />
          </label>
        ` : ""}

        <label class="maintenance-field">
          <span class="maintenance-label">Часы на принтере</span>
          <input
            type="number"
            name="print_hours_snapshot"
            class="input maintenance-input"
            min="0"
            step="0.01"
            inputmode="decimal"
            placeholder=""
            value="${escapeHtml(draft.printHours)}"
            ${disabled}
          />
        </label>

        <label class="maintenance-field">
          <span class="maintenance-label">Комментарий</span>
          <textarea
            name="note"
            class="input maintenance-textarea"
            rows="3"
            placeholder="Дополнительные детали по обслуживанию"
            ${disabled}
          >${escapeHtml(draft.note)}</textarea>
        </label>

        <div class="maintenance-form-footer">
          <div id="maintenance-form-status" class="${formStatusClass.trim()}">${escapeHtml(state.formMessage || "")}</div>
          <button type="submit" class="btn btn-primary" ${disabled}>Сохранить</button>
        </div>
      </form>
    </section>
  `;
}

function buildMaintenanceHistory(printerId) {
  const state = getMaintenanceUiState(printerId);
  const items = Array.isArray(state.items) ? state.items : [];

  let historyContent = "";
  if (state.loading && items.length === 0) {
    historyContent = `<div class="drawer-loading">Загружаем историю обслуживания...</div>`;
  } else if (state.error && items.length === 0) {
    historyContent = `<div class="drawer-empty">${escapeHtml(state.error)}</div>`;
  } else if (items.length === 0) {
    historyContent = `<div class="drawer-empty">Для этого принтера пока нет записей обслуживания.</div>`;
  } else {
    historyContent = `
      <div class="maintenance-history-list">
        ${items.map((item) => `
          <article class="maintenance-entry">
            <div class="maintenance-entry-row">
              <span class="maintenance-entry-label">Дата</span>
              <strong>${escapeHtml(formatDrawerDateTime(item.event_at))}</strong>
            </div>
            <div class="maintenance-entry-row">
              <span class="maintenance-entry-label">Тип обслуживания</span>
              <strong>${escapeHtml(getMaintenanceTypeLabel(item.event_type, item.custom_type_name))}</strong>
            </div>
            ${hasStatusValue(item.nozzle_diameter) ? `
              <div class="maintenance-entry-row">
                <span class="maintenance-entry-label">Диаметр сопла</span>
                <strong>${escapeHtml(formatMaintenanceNozzleLabel(item.nozzle_diameter))}</strong>
              </div>
            ` : ""}
            ${hasStatusValue(item.print_hours_snapshot) ? `
              <div class="maintenance-entry-row">
                <span class="maintenance-entry-label">Часы на принтере</span>
                <strong>${escapeHtml(formatMaintenancePrintHoursLabel(item.print_hours_snapshot))}</strong>
              </div>
            ` : ""}
            <div class="maintenance-entry-row maintenance-entry-row-comment">
              <span class="maintenance-entry-label">Комментарий</span>
              <span class="maintenance-entry-comment">${formatMaintenanceComment(item.note)}</span>
            </div>
          </article>
        `).join("")}
      </div>
    `;
  }

  return `
    <section class="drawer-section">
      <div class="section-head maintenance-section-head">
        <div>
          <h4>История обслуживания</h4>
          <div class="section-subtitle">Список записей по этому принтеру.</div>
        </div>
      </div>
      ${state.loading && items.length > 0 ? '<div class="maintenance-history-state">Обновляем историю...</div>' : ""}
      ${state.error && items.length > 0 ? `<div class="maintenance-history-state is-error">${escapeHtml(state.error)}</div>` : ""}
      ${historyContent}
    </section>
  `;
}

function bindFinalDrawerTabs(content) {
  content.querySelectorAll("[data-drawer-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      setDrawerTab(button.dataset.drawerTab);
    });
  });
}

function bindFinalGeneralActions(content, details) {
  content.querySelectorAll("[data-printer-action]").forEach((button) => {
    button.addEventListener("click", () => runPrinterAction(button.dataset.printerAction));
  });

  const printerId = details.id;
  const materialState = getMaterialEditorState(printerId, details.status?.loaded_material || "");

  content.querySelectorAll("[data-material-edit]").forEach((materialEditButton) => {
    materialEditButton.addEventListener("click", () => {
      clearMaterialEditorMessageTimer(materialState);
      materialState.isEditing = true;
      materialState.draft = normalizeMaterialValue(details.status?.loaded_material || "");
      materialState.message = "";
      materialState.messageKind = "";
      renderPrinterDetails(appState.drawerDetails || details);
      requestAnimationFrame(() => {
        const input = document.querySelector('#material-editor-form [name="material_value"]');
        if (input) {
          input.focus();
          input.select();
        }
      });
    });
  });

  const materialCancelButton = content.querySelector("[data-material-cancel]");
  if (materialCancelButton) {
    materialCancelButton.addEventListener("click", () => {
      clearMaterialEditorMessageTimer(materialState);
      materialState.isEditing = false;
      materialState.saving = false;
      materialState.draft = normalizeMaterialValue(details.status?.loaded_material || "");
      materialState.message = "";
      materialState.messageKind = "";
      renderPrinterDetails(appState.drawerDetails || details);
    });
  }

  const materialForm = content.querySelector("#material-editor-form");
  if (materialForm) {
    const materialInput = materialForm.querySelector('[name="material_value"]');
    if (materialInput) {
      materialInput.addEventListener("input", () => {
        clearMaterialEditorMessageTimer(materialState);
        materialState.message = "";
        materialState.messageKind = "";
        materialState.draft = materialInput.value;
      });
    }

    materialForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      await submitPrinterMaterial(printerId);
    });
  }
}

async function submitPrinterMaterial(printerId) {
  const state = getMaterialEditorState(
    printerId,
    appState.drawerDetails?.status?.loaded_material || "",
  );
  if (state.saving) return;

  clearMaterialEditorMessageTimer(state);
  state.saving = true;
  state.message = "Сохраняем материал...";
  state.messageKind = "";

  if (appState.drawerPrinterId === printerId && appState.drawerDetails) {
    renderPrinterDetails(appState.drawerDetails);
  }

  try {
    const resp = await fetch(`/api/printers/${encodeURIComponent(printerId)}/material`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ material: state.draft.trim() }),
    });
    const data = await resp.json().catch(async () => ({ error: await resp.text() }));
    if (!resp.ok) {
      throw new Error(data.error || "Не удалось сохранить материал");
    }

    const loadedMaterial = normalizeMaterialValue(data.loaded_material);
    const savedOverride = normalizeMaterialValue(data.material_override);
    state.isEditing = false;
    state.draft = loadedMaterial;
    state.message = savedOverride ? "Материал сохранён." : "Ручное значение очищено.";
    state.messageKind = "success";
    scheduleMaterialEditorMessageClear(printerId, 5000);

    if (appState.drawerDetails?.id === printerId) {
      appState.drawerDetails = {
        ...appState.drawerDetails,
        status: {
          ...(appState.drawerDetails.status || {}),
          loaded_material: loadedMaterial || null,
        },
      };
    }

    updatePrinterMaterialInUi(printerId, loadedMaterial);
  } catch (e) {
    state.message = `Ошибка: ${e.message}`;
    state.messageKind = "error";
  } finally {
    state.saving = false;

    if (appState.drawerPrinterId === printerId && appState.drawerDetails) {
      renderPrinterDetails(appState.drawerDetails);
    }
  }
}

function bindMaintenanceFormControls(content, details) {
  const form = content.querySelector("#maintenance-form");
  if (!form) return;

  const printerId = details.id;
  const state = getMaintenanceUiState(printerId);

  const clearFormMessage = () => {
    state.formMessage = "";
    state.formMessageKind = "";
  };

  form.querySelectorAll("[data-select-field]").forEach((button) => {
    button.addEventListener("click", () => {
      const field = button.dataset.selectField;
      const nextValue = button.dataset.selectValue || "";
      if (!field || !nextValue) return;
      const dropdown = button.closest(".pretty-select");

      clearFormMessage();

      if (field === "event_type") {
        state.draft.eventType = nextValue;
        if (nextValue === "NOZZLE_REPLACEMENT" && !state.draft.nozzleDiameter) {
          state.draft.nozzleDiameter = "0.4";
        }
      }

      if (field === "nozzle_diameter") {
        state.draft.nozzleDiameter = nextValue;
      }

      if (dropdown) {
        dropdown.removeAttribute("open");
      }

      renderPrinterDetails(appState.drawerDetails || details);
    });
  });

  const customTypeField = form.querySelector('[name="custom_type_name"]');
  if (customTypeField) {
    customTypeField.addEventListener("input", () => {
      clearFormMessage();
      state.draft.customTypeName = customTypeField.value;
    });
  }

  const noteField = form.querySelector('[name="note"]');
  if (noteField) {
    noteField.addEventListener("input", () => {
      clearFormMessage();
      state.draft.note = noteField.value;
    });
  }

  const printHoursField = form.querySelector('[name="print_hours_snapshot"]');
  if (printHoursField) {
    printHoursField.addEventListener("input", () => {
      clearFormMessage();
      state.draft.printHours = printHoursField.value.replace(",", ".");
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitMaintenanceRecord(printerId);
  });
}

async function loadMaintenanceHistoryForPrinter(printerId, { force = false } = {}) {
  const state = getMaintenanceUiState(printerId);
  if (state.loading) return;
  if (!force && Array.isArray(state.items)) return;

  state.loading = true;
  state.error = "";
  state.requestId += 1;
  const requestId = state.requestId;

  if (appState.drawerPrinterId === printerId && appState.drawerTab === "maintenance" && appState.drawerDetails) {
    renderPrinterDetails(appState.drawerDetails);
  }

  try {
    const resp = await fetch(`/printers/${encodeURIComponent(printerId)}/maintenance`);
    const data = await resp.json().catch(async () => ({ error: await resp.text() }));
    if (requestId !== state.requestId) return;
    if (!resp.ok) {
      throw new Error(data.error || "Не удалось загрузить историю обслуживания");
    }

    state.items = Array.isArray(data.items) ? data.items : [];
  } catch (e) {
    if (requestId !== state.requestId) return;
    state.error = `Не удалось загрузить историю: ${e.message}`;
  } finally {
    if (requestId === state.requestId) {
      state.loading = false;
    }

    if (appState.drawerPrinterId === printerId && appState.drawerTab === "maintenance" && appState.drawerDetails) {
      renderPrinterDetails(appState.drawerDetails);
    }
  }
}

async function submitMaintenanceRecord(printerId) {
  const state = getMaintenanceUiState(printerId);
  if (state.submitting) return;

  state.submitting = true;
  state.loading = false;
  state.requestId += 1;
  state.formMessage = "Сохраняем запись...";
  state.formMessageKind = "";

  if (appState.drawerPrinterId === printerId && appState.drawerDetails) {
    renderPrinterDetails(appState.drawerDetails);
  }

  const normalizedPrintHours = (state.draft.printHours || "").trim();
  const payload = {
    event_type: state.draft.eventType,
    performed_by: MAINTENANCE_PERFORMED_BY_PLACEHOLDER,
    note: state.draft.note.trim(),
  };

  if (normalizedPrintHours !== "") {
    payload.print_hours_snapshot = normalizedPrintHours;
  }

  if (state.draft.eventType === "NOZZLE_REPLACEMENT") {
    payload.nozzle_diameter = state.draft.nozzleDiameter || "0.4";
  }

  if (state.draft.eventType === "OTHER") {
    payload.custom_type_name = state.draft.customTypeName.trim();
  }

  try {
    const resp = await fetch(`/printers/${encodeURIComponent(printerId)}/maintenance`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(async () => ({ error: await resp.text() }));
    if (!resp.ok) {
      throw new Error(data.error || "Не удалось сохранить запись");
    }

    const item = data.item;
    if (!item) {
      throw new Error("Сервер не вернул сохранённую запись");
    }

    state.items = Array.isArray(state.items) ? [item, ...state.items] : [item];
    state.error = "";
    state.draft = createDefaultMaintenanceDraft();
    state.formMessage = "Запись обслуживания сохранена.";
    state.formMessageKind = "success";
  } catch (e) {
    state.formMessage = `Ошибка: ${e.message}`;
    state.formMessageKind = "error";
  } finally {
    state.submitting = false;

    if (appState.drawerPrinterId === printerId && appState.drawerDetails) {
      renderPrinterDetails(appState.drawerDetails);
    }
  }
}

function renderPrinterDetails(details) {
  appState.drawerDetails = details;

  const title = document.getElementById("drawer-title");
  const content = document.getElementById("drawer-content");
  const eyebrow = document.querySelector("#details-drawer .drawer-eyebrow");
  if (!content) return;

  if (eyebrow) {
    eyebrow.textContent = `Принтер ${details.id || "—"}`;
  }

  if (title) {
    title.textContent = details.name || details.id;
  }

  const generalTabActive = appState.drawerTab !== "maintenance";
  const maintenanceTabActive = !generalTabActive;

  content.innerHTML = `
    <div class="drawer-tabs" role="tablist" aria-label="Вкладки принтера">
      <button
        type="button"
        class="drawer-tab-btn ${generalTabActive ? "is-active" : ""}"
        data-drawer-tab="general"
      >
        Общая информация
      </button>
      <button
        type="button"
        class="drawer-tab-btn ${maintenanceTabActive ? "is-active" : ""}"
        data-drawer-tab="maintenance"
      >
        Обслуживание
      </button>
    </div>

    <div class="drawer-tab-panel" ${generalTabActive ? "" : "hidden"}>
      ${buildGeneralDetailsTab(details)}
    </div>

    <div class="drawer-tab-panel" ${maintenanceTabActive ? "" : "hidden"}>
      ${buildMaintenanceForm(details.id)}
      ${buildMaintenanceHistory(details.id)}
    </div>
  `;

  bindFinalDrawerTabs(content);

  if (generalTabActive) {
    bindFinalGeneralActions(content, details);
  }

  if (maintenanceTabActive) {
    bindMaintenanceFormControls(content, details);
    void loadMaintenanceHistoryForPrinter(details.id);
  }
}

async function main() {
  const statusEl = document.getElementById("status");
  statusEl.textContent = "Загружаем список принтеров...";

  try {
    const printers = await loadPrinters();
    appState.printers = printers;
    appState.printerMap = new Map(printers.map((printer) => [printer.id, printer]));
    buildGrid(printers);
    await refreshStatuses();
    setInterval(refreshStatuses, 5000);
    statusEl.textContent = "Готово. Включите режим выбора, если хотите отмечать принтеры для действий.";
  } catch (e) {
    statusEl.textContent = "Ошибка: " + e.message;
  }

  const fileInput = document.getElementById("file-input");
  const chooseFileBtn = document.getElementById("choose-file-btn");
  const chosenFileNameSpan = document.getElementById("chosen-file-name");
  const selectModeBtn = document.getElementById("select-mode-btn");
  const startBtn = document.getElementById("start-btn");
  const uploadBtn = document.getElementById("upload-btn");
  const restartBtn = document.getElementById("restart-btn");
  const refreshBtn = document.getElementById("refresh-btn");
  const drawerBackdrop = document.getElementById("drawer-backdrop");
  const drawerCloseBtn = document.getElementById("drawer-close-btn");

  setSelectionMode(false);

  chooseFileBtn.addEventListener("click", () => {
    fileInput.click();
  });

  fileInput.addEventListener("change", () => {
    chosenFileNameSpan.textContent =
      fileInput.files.length > 0 ? fileInput.files[0].name : "Файл не выбран";
  });

  selectModeBtn.addEventListener("click", () => {
    setSelectionMode(!appState.selectionMode);
  });

  drawerBackdrop.addEventListener("click", closeDrawer);
  drawerCloseBtn.addEventListener("click", closeDrawer);

  startBtn.addEventListener("click", async () => {
    await submitFileJob({
      endpoint: "/api/upload_and_print",
      statusText: "Отправляем задание на печать...",
      statusEl,
      fileInput,
      buttonsToDisable: [startBtn, uploadBtn, restartBtn],
    });
  });

  uploadBtn.addEventListener("click", async () => {
    await submitFileJob({
      endpoint: "/api/upload_to_sd",
      statusText: "Загружаем файл на SD...",
      statusEl,
      fileInput,
      buttonsToDisable: [startBtn, uploadBtn, restartBtn],
    });
  });

  restartBtn.addEventListener("click", async () => {
    const printers = getSelectedPrinters();
    if (printers.length === 0) {
      statusEl.textContent = "Выберите хотя бы один принтер.";
      return;
    }

    statusEl.textContent = "Перезапускаем последний напечатанный файл...\n";

    statusEl.textContent = "Перезапускаем файл со второй строки карточки...\n";
    statusEl.textContent = "Перезапускаем последний напечатанный файл...\n";
    try {
      const resp = await fetch("/api/restart_last_printed", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ printers }),
      });

      const data = await resp.json().catch(async () => ({ error: await resp.text() }));
      if (!resp.ok) {
        statusEl.textContent += `Ошибка: ${data.error || "unknown"}`;
        return;
      }

      const jobId = data.job_id;
      if (!jobId) {
        statusEl.textContent += "Ошибка: сервер не вернул job_id";
        return;
      }

      statusEl.textContent = `Задача создана: ${jobId}\n`;
      pollJob(jobId, statusEl, [startBtn, uploadBtn, restartBtn]);
    } catch (e) {
      statusEl.textContent += "Ошибка: " + e.message;
    }
  });

  refreshBtn.addEventListener("click", async () => {
    try {
      refreshBtn.disabled = true;
      statusEl.textContent = "Переподключаем все принтеры (MQTT)...";

      const resp = await fetch("/api/mqtt/restart", { method: "POST" });
      const data = await resp.json().catch(async () => ({ error: await resp.text() }));

      if (!resp.ok || !data.ok) {
        statusEl.textContent = "Ошибка переподключения: " + (data.error || "unknown");
        return;
      }

      statusEl.textContent = "Переподключение запущено. Ждём статусы...";
      const printers = await loadPrinters();
      appState.printers = printers;
      appState.printerMap = new Map(printers.map((printer) => [printer.id, printer]));
      buildGrid(printers);
      updateSelectionCount();
      setTimeout(refreshStatuses, 2000);
      if (appState.drawerPrinterId) {
        if (appState.printerMap.has(appState.drawerPrinterId)) {
          setTimeout(() => loadPrinterDetails(appState.drawerPrinterId, { silent: true }), 2000);
        } else {
          closeDrawer();
        }
      }
    } catch (e) {
      statusEl.textContent = "Ошибка: " + e.message;
    } finally {
      refreshBtn.disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", main);
