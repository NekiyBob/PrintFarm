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

  // Ряды: 1..2
  [1, 2].forEach(rowNum => {
    const rowDiv = document.createElement("div");
    rowDiv.className = "rack-row";

    // Стеллажи: 1..5
    for (let rackNum = 1; rackNum <= 5; rackNum++) {
      const rackPrinters = layout[rowNum]?.[rackNum] || {};
      const rackDiv = document.createElement("div");
      rackDiv.className = "rack";

      const title = document.createElement("div");
      title.className = "rack-title";
      title.textContent = `Row ${rowNum}, Rack ${rackNum}`;
      rackDiv.appendChild(title);

      // Уровни: сначала верхний (2), потом нижний (1)
      [2, 1].forEach(levelNum => {
        const levelDiv = document.createElement("div");
        levelDiv.className = "level";

        for (let slotNum = 1; slotNum <= 6; slotNum++) {
          const p = rackPrinters[levelNum]?.[slotNum];
          const btn = document.createElement("button");
          btn.className = "printer-btn";
          if (p) {
            btn.textContent = p.id;
            btn.dataset.printerId = p.id;
          } else {
            btn.textContent = "—";
            btn.disabled = true;
          }

          btn.addEventListener("click", () => {
            if (btn.disabled) return;
            btn.classList.toggle("selected");
          });

          levelDiv.appendChild(btn);
        }

        rackDiv.appendChild(levelDiv);
      });

      rowDiv.appendChild(rackDiv);
    }

    grid.appendChild(rowDiv);
  });
}
function applyStatusClass(btn, status) {
  // сначала убираем старые классы статуса
  btn.classList.remove("st-running", "st-pause", "st-offline");

  // если статуса нет вообще (принтер ещё не опрошен) — не меняем цвет (NONE)
  if (!status) return;

  // если не удалось получить статус — тёмно-синий
  if (status.ok === false) {   // только если явно false
  btn.classList.add("st-offline");
  return;
  }

  const st = (status.gcode_state || "").toUpperCase();

  // NONE → не менять цвет (оставить как сейчас)
  if (!st || st === "NONE") return;

  if (st === "RUNNING" || st === "PRINTING") {
    btn.classList.add("st-running");
  } else if (st === "PAUSE" || st === "PAUSED") {
    btn.classList.add("st-pause");
  } else {
    // другие состояния (IDLE/FINISH и т.п.) — оставляем дефолт
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

async function main() {
  const statusEl = document.getElementById("status");
  statusEl.textContent = "Загружаем список принтеров...";

  try {
    const printers = await loadPrinters();
    buildGrid(printers);
    refreshStatuses();
    setInterval(refreshStatuses, 15000);
    statusEl.textContent = "Готово. Выберите принтеры и файл.";
  } catch (e) {
    statusEl.textContent = "Ошибка: " + e.message;
  }

  const fileInput = document.getElementById("file-input");
  const chooseFileBtn = document.getElementById("choose-file-btn");
  const chosenFileNameSpan = document.getElementById("chosen-file-name");
  const startBtn = document.getElementById("start-btn");

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

    async function poll(jid) { // <-- ВАЖНО: используем jid, а не jobId
      const r = await fetch(`/api/jobs/${jid}`);
      if (!r.ok) {
        statusEl.textContent += "\nНе могу получить прогресс.";
        startBtn.disabled = false;
        return;
      }

      const j = await r.json();

      let out =
        `Файл: ${j.filename}\n` +
        `Готово: ${j.done}/${j.total} | OK: ${j.ok_count} | ERR: ${j.err_count}\n\n`;

      for (const [pid, st] of Object.entries(j.printers || {})) {
        const msg = st.message ? ` (${st.message})` : "";
        out += `${pid}: ${st.stage}${msg}\n`;
      }

      statusEl.textContent = out;

      if (!j.finished) {
        setTimeout(() => poll(jid), 2000);
      } else {
        statusEl.textContent += "\n✅ Готово.";
        startBtn.disabled = false;
      }
    }

    poll(jobId);

  } catch (e) {
    statusEl.textContent += "Ошибка отправки: " + e.message;
    startBtn.disabled = false;
  }
});

}

document.addEventListener("DOMContentLoaded", main);
