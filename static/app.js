// Получаем принтеры с backend
async function loadPrinters() {
  const resp = await fetch("/api/printers");
  if (!resp.ok) {
    throw new Error("Ошибка загрузки списка принтеров");
  }
  return await resp.json();
}

function buildGrid(printers) {
  // Группируем по row -> rack -> level -> slot
  const layout = {};

  for (const p of printers) {
    const { row, rack, level, slot } = p;
    layout[row] = layout[row] || {};
    layout[row][rack] = layout[row][rack] || {};
    layout[row][rack][level] = layout[row][rack][level] || {};
    layout[row][rack][level][slot] = p;
  }

  const grid = document.getElementById("grid");
  grid.innerHTML = "";

  [1, 2].forEach((rowNum) => {
    const rowDiv = document.createElement("div");
    rowDiv.className = "rack-row";

    const rackCount = rowNum === 1 ? 6 : 5;

    for (let rackNum = 1; rackNum <= rackCount; rackNum++) {
      const rackPrinters = layout[rowNum]?.[rackNum] || {};
      const rackDiv = document.createElement("div");
      rackDiv.className = "rack";

      const title = document.createElement("div");
      title.className = "rack-title";
      title.textContent = `Row ${rowNum}, Rack ${rackNum}`;
      rackDiv.appendChild(title);

      // ✅ Клик по заголовку: выбрать/снять все в стеллаже
      title.style.cursor = "pointer";
      title.title = "Клик: выбрать/снять все в стеллаже";
      title.addEventListener("click", () => {
        const buttons = rackDiv.querySelectorAll(".printer-btn");
        const enabled = Array.from(buttons).filter(
          (b) => !b.disabled && b.dataset.printerId
        );
        const allSelected =
          enabled.length > 0 &&
          enabled.every((b) => b.classList.contains("selected"));
        enabled.forEach((b) => b.classList.toggle("selected", !allSelected));
      });

      // ✅ Поворот на 90° по часовой: 6 строк по 2 колонки (L1 | L2)
      for (let slotNum = 1; slotNum <= 6; slotNum++) {
        const levelDiv = document.createElement("div");
        levelDiv.className = "level";

        const pL1 = rackPrinters[1]?.[slotNum];
        const pL2 = rackPrinters[2]?.[slotNum];

        const btn1 = document.createElement("button");
        btn1.className = "printer-btn";
        if (pL1) {
          btn1.textContent = pL1.id;
          btn1.dataset.printerId = pL1.id;
        } else {
          btn1.textContent = "—";
          btn1.disabled = true;
        }
        btn1.addEventListener("click", () => {
          if (btn1.disabled) return;
          btn1.classList.toggle("selected");
        });

        const btn2 = document.createElement("button");
        btn2.className = "printer-btn";
        if (pL2) {
          btn2.textContent = pL2.id;
          btn2.dataset.printerId = pL2.id;
        } else {
          btn2.textContent = "—";
          btn2.disabled = true;
        }
        btn2.addEventListener("click", () => {
          if (btn2.disabled) return;
          btn2.classList.toggle("selected");
        });

        levelDiv.appendChild(btn1);
        levelDiv.appendChild(btn2);
        rackDiv.appendChild(levelDiv);
      }

      rowDiv.appendChild(rackDiv);
    }

    grid.appendChild(rowDiv);
  });
}

const search = document.getElementById("search");
if (search) {
  search.addEventListener("input", () => {
    const q = search.value.trim().toUpperCase();
    document.querySelectorAll(".printer-btn").forEach(btn => {
      if (!btn.dataset.printerId) return; // пустышки
      const id = btn.dataset.printerId.toUpperCase();
      btn.style.display = (!q || id.includes(q)) ? "" : "none";
    });
  });
}


function applyStatusClass(btn, status) {
  btn.classList.remove("st-running", "st-pause", "st-offline", "st-error");

  if (!status) return;

  // offline как раньше (тёмно-синий)
  if (status.ok === false) {
    btn.classList.add("st-offline");
    btn.title = "Offline / нет данных";
    return;
  }

  // красный ТОЛЬКО runout
  if (status.error === "filament_runout") {
    btn.classList.add("st-error");
    btn.title = "Закончился филамент";
    return;
  }

  btn.title = `ok=${status.ok} state=${(status.gcode_state || "NONE")} err=${status.error || ""}`;


  const st = (status.gcode_state || "").toUpperCase();
  if (!st || st === "NONE") return;

  if (st === "RUNNING" || st === "PRINTING") {
    btn.classList.add("st-running");
  } else if (st === "PAUSE" || st === "PAUSED") {
    btn.classList.add("st-pause");
  }
}



async function refreshStatuses() {
  const resp = await fetch("/api/status");
  if (!resp.ok) return;

  const data = await resp.json();
  const map = new Map();
  for (const s of (data.statuses || [])) {
    map.set(s.id, s);
  }

  document.querySelectorAll(".printer-btn").forEach((btn) => {
    const pid = btn.dataset.printerId;
    const st = map.get(pid) || null;
    applyStatusClass(btn, st);
  });
}


async function pollJob(jobId, statusEl, buttonsToDisable = []) {
  // отключаем кнопки
  for (const b of buttonsToDisable) b.disabled = true;

  async function step() {
    const r = await fetch(`/api/jobs/${jobId}`);
    if (!r.ok) {
      statusEl.textContent += "\nНе могу получитьполучить прогресс.";
      for (const b of buttonsToDisable) b.disabled = false;
      return;
    }

    const j = await r.json();

    let out =
      `Job: ${j.job_id}\n` +
      `Файл: ${j.filename}\n` +
      `Готово: ${j.done}/${j.total} | OK: ${j.ok_count} | ERR: ${j.err_count}\n\n`;

    for (const [pid, st] of Object.entries(j.printers || {})) {
      const msg = st.message ? ` (${st.message})` : "";
      const f = st.file ? ` [${st.file}]` : "";
      out += `${pid}${f}: ${st.stage}${msg}\n`;
    }

    statusEl.textContent = out;

    if (!j.finished) {
      setTimeout(step, 2000);
    } else {
      statusEl.textContent += "\n✅ Готово.";
      for (const b of buttonsToDisable) b.disabled = false;
    }
  }

  step();
}


async function main() {
  const statusEl = document.getElementById("status");
  statusEl.textContent = "Загружаем список принтеров...";

  try {
    const printers = await loadPrinters();
    buildGrid(printers);
    refreshStatuses();
    setInterval(refreshStatuses, 5000);
    statusEl.textContent = "Готово. Выберите принтеры и файл.";
  } catch (e) {
    statusEl.textContent = "Ошибка: " + e.message;
  }

  const fileInput = document.getElementById("file-input");
  const chooseFileBtn = document.getElementById("choose-file-btn");
  const chosenFileNameSpan = document.getElementById("chosen-file-name");
  const startBtn = document.getElementById("start-btn");
  const restartBtn = document.getElementById("restart-btn");
  const refreshBtn = document.getElementById("refresh-btn");


  // Кнопка "Выбрать файл..." открывает системный проводник
  chooseFileBtn.addEventListener("click", () => {
    fileInput.click();
  });

  // Когда пользователь выбрал файл, показываем его имя
  fileInput.addEventListener("change", () => {
    if (fileInput.files.length > 0) {
      const f = fileInput.files[0];
      chosenFileNameSpan.textContent = f.name;
    } else {
      chosenFileNameSpan.textContent = "Файл не выбран";
    }
  });

  startBtn.addEventListener("click", async () => {
  const selectedButtons = Array.from(document.querySelectorAll(".printer-btn.selected"));

  if (selectedButtons.length === 0) {
    statusEl.textContent = "Выберите хотя бы один принтер.";
    return;
  }
  if (!fileInput.files || fileInput.files.length === 0) {
    statusEl.textContent = "Сначала выберите файл.";
    return;
  }

  const printers = selectedButtons.map(btn => btn.dataset.printerId);
  const file = fileInput.files[0];

  statusEl.textContent = "Отправляем задание...\n";

  let jobId = null; // <-- ВАЖНО: объявили заранее

  try {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("printers", JSON.stringify(printers));

    const resp = await fetch("/api/upload_and_print", {
      method: "POST",
      body: formData,
    });

    const data = await resp.json().catch(async () => ({ error: await resp.text() }));
    if (!resp.ok) {
      statusEl.textContent += `Ошибка: ${data.error || "unknown"}`;
      return;
    }

    jobId = data.job_id;
    if (!jobId) {
      statusEl.textContent += "Ошибка: сервер не вернул job_id";
      return;
    }

    statusEl.textContent = `Задача создана: ${jobId}\n`;
    startBtn.disabled = true;

    pollJob(jobId, statusEl, [startBtn, restartBtn]);


  } catch (e) {
    statusEl.textContent += "Ошибка отправки: " + e.message;
    startBtn.disabled = false;
  }
});

restartBtn.addEventListener("click", async () => {
  const selectedButtons = Array.from(document.querySelectorAll(".printer-btn.selected"));
  if (selectedButtons.length === 0) {
    statusEl.textContent = "Выберите хотя бы один принтер.";
    return;
  }

  const printers = selectedButtons.map(btn => btn.dataset.printerId);

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
    pollJob(jobId, statusEl, [startBtn, restartBtn]);

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

    statusEl.textContent = "✅ Переподключение запущено. Ждём статусы...";
    // через пару секунд обновим раскраску
    setTimeout(refreshStatuses, 2000);

  } catch (e) {
    statusEl.textContent = "Ошибка: " + e.message;
  } finally {
    refreshBtn.disabled = false;
  }
});


}

document.addEventListener("DOMContentLoaded", main);
