const appState = {
  printers: [],
  printerMap: new Map(),
  selectionMode: false,
  drawerPrinterId: null,
  detailsRequestId: 0,
  detailsRefreshTimer: null,
  drawerActionPending: false,
};

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

function setSelectionMode(nextValue) {
  appState.selectionMode = nextValue;
  document.body.classList.toggle("selection-mode", nextValue);

  const toggleBtn = document.getElementById("select-mode-btn");
  const hint = document.getElementById("grid-hint");
  if (toggleBtn) {
    toggleBtn.classList.toggle("is-active", nextValue);
    toggleBtn.textContent = nextValue ? "Готово" : "Выбрать";
  }

  if (hint) {
    hint.textContent = nextValue
      ? "Режим выбора активен. Клик по принтеру отмечает его для массовых действий."
      : 'Обычный клик открывает информацию о принтере. Режим "Выбрать" включает множественный выбор.';
  }

  if (nextValue) {
    closeDrawer();
  }
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
    loadPrinterDetails(appState.drawerPrinterId, { silent: true });
  }, 5000);
}

function closeDrawer() {
  appState.drawerPrinterId = null;
  stopDetailsAutoRefresh();
  document.body.classList.remove("drawer-open");

  const drawer = document.getElementById("details-drawer");
  if (drawer) {
    drawer.setAttribute("aria-hidden", "true");
  }
}

function openDrawerShell(printer, { silent = false } = {}) {
  appState.drawerPrinterId = printer.id;
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
  if (content && !silent) {
    content.innerHTML = `<div class="drawer-loading">Загрузка информации о принтере...</div>`;
  }
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
  meta.textContent = printer.model || "";

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

    loadPrinterDetails(printer.id);
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
    if (metaEl) metaEl.textContent = model;
    if (progressEl) progressEl.textContent = "";
    return;
  }

  const hasProgress = hasStatusValue(status.progress_percent);
  const progress = hasProgress ? normalizeProgress(status.progress_percent) : 0;
  const state = (status.gcode_state || "").toUpperCase();
  const fileName = shortFileName(status.file);
  const nozzleDiameter = hasStatusValue(status.nozzle_diameter) ? String(status.nozzle_diameter) : "";

  const showFill = state === "RUNNING" || state === "PRINTING";
  const showProgress = showFill || state === "PAUSE" || state === "PAUSED";

  btn.style.setProperty("--progress", String(showFill ? progress : 0));
  if (fileEl) {
    fileEl.textContent = fileName || "";
  }
  if (metaEl) {
    metaEl.textContent = [model, nozzleDiameter].filter(Boolean).join(" ");
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

async function refreshStatuses() {
  const resp = await fetch("/api/status");
  if (!resp.ok) return;

  const data = await resp.json();
  const statusMap = new Map();
  for (const status of data.statuses || []) {
    statusMap.set(status.id, status);
  }

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
          <strong>${formatValue(Math.round(progress), "%")}</strong>
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

async function loadPrinterDetails(printerId, { silent = false } = {}) {
  const printer = appState.printerMap.get(printerId);
  if (!printer) return;

  openDrawerShell(printer, { silent });
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
      setTimeout(refreshStatuses, 2000);
      if (appState.drawerPrinterId) {
        setTimeout(() => loadPrinterDetails(appState.drawerPrinterId, { silent: true }), 2000);
      }
    } catch (e) {
      statusEl.textContent = "Ошибка: " + e.message;
    } finally {
      refreshBtn.disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", main);
