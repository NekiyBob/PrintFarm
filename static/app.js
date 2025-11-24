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

async function main() {
  const statusEl = document.getElementById("status");
  statusEl.textContent = "Загружаем список принтеров...";

  try {
    const printers = await loadPrinters();
    buildGrid(printers);
    statusEl.textContent = "Готово. Выберите принтеры и файл.";
  } catch (e) {
    statusEl.textContent = "Ошибка: " + e.message;
  }

  const startBtn = document.getElementById("start-btn");
  const filenameInput = document.getElementById("filename");

  startBtn.addEventListener("click", async () => {
    const filename = filenameInput.value.trim();
    if (!filename) {
      statusEl.textContent = "Укажите имя файла (из папки jobs/).";
      return;
    }

    const selectedButtons = Array.from(
      document.querySelectorAll(".printer-btn.selected")
    );

    if (selectedButtons.length === 0) {
      statusEl.textContent = "Выберите хотя бы один принтер.";
      return;
    }

    const printerIds = selectedButtons.map(btn => btn.dataset.printerId);

    statusEl.textContent = "Отправляем задание...\n";

    try {
      const resp = await fetch("/api/print", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          printers: printerIds,
          filename: filename,
        }),
      });

      if (!resp.ok) {
        const txt = await resp.text();
        statusEl.textContent += `Ошибка HTTP ${resp.status}: ${txt}`;
        return;
      }

      const result = await resp.json();
      let msg = "Результат:\n";
      for (const r of result) {
        msg += `  ${r.printer}: ${r.status}`;
        if (r.message) {
          msg += ` (${r.message})`;
        }
        msg += "\n";
      }
      statusEl.textContent += msg;
    } catch (e) {
      statusEl.textContent += "Ошибка отправки: " + e.message;
    }
  });
}

document.addEventListener("DOMContentLoaded", main);
