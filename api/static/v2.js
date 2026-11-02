"use strict";
let dashboardOffset = 0, comparisonIp = "";
const selectedReports = {before: null, after: null};

function describeThreshold() {
  const input = $("stale-hours");
  if (!input.validity.valid) {
    $("stale-help").textContent = "Ingresá un número entero entre 1 y 168 horas (7 días).";
    return;
  }
  const hours = Number(input.value);
  const duration = `${hours} ${hours === 1 ? "hora" : "horas"}`;
  $("stale-help").textContent = `Con ${duration}, un equipo cuyo último inventario llegó hace ${hours + 1} horas aparecerá como “Sin reportes recientes” al aplicar.`;
}

async function loadDashboard(number) {
  const hours = $("stale-hours").value;
  const data = await get(`/dashboard?stale_hours=${encodeURIComponent(hours)}&limit=${pageSize}&offset=${dashboardOffset}`);
  if (number !== requestNumber) return;
  $("dashboard").hidden = false;
  facts($("dashboard-summary"), [["Equipos registrados", data.summary.hosts], ["Con reportes recientes", data.summary.recent], ["Sin reportes recientes", data.summary.stale], ["Equipos con avisos", data.summary.with_warnings]]);
  const applied = `${data.stale_hours} ${data.stale_hours === 1 ? "hora" : "horas"}`;
  $("dashboard-time").textContent = `Límite aplicado: ${applied} desde el último inventario recibido. Actualizado: ${utc(data.generated_at)} · ${data.summary.reports} relevamientos guardados.`;
  cells($("system-rows"), data.systems.map(item => [item.name, item.count]));
  cells($("host-rows"), data.items.map(item => [
    `${item.hostname}\n${item.server_ip}`, `${item.os_name}\n${item.os_version}`, utc(item.received_at),
    item.stale ? "Sin reportes recientes" : "Reciente", `${item.processes} / ${item.sessions}`, item.warnings
  ]));
  data.items.forEach((item, index) => {
    const tr = $("host-rows").children[index];
    tr.children[3].className = item.stale ? "stale" : "recent";
    const td = document.createElement("td"), button = document.createElement("button");
    button.textContent = "Abrir"; button.className = "secondary";
    button.setAttribute("aria-label", `Abrir equipo ${item.server_ip}`);
    button.onclick = () => { $("ip").value = item.server_ip; selectView("current"); };
    td.append(button); tr.append(td);
  });
  $("hosts-previous").disabled = dashboardOffset === 0;
  $("hosts-next").disabled = dashboardOffset + data.items.length >= data.summary.hosts;
  $("hosts-page").textContent = `Página ${dashboardOffset / pageSize + 1}`;
  status(data.summary.hosts ? "Equipos que enviaron al menos un inventario. Abrí uno para ver sus datos." : "Todavía no se recibieron inventarios.");
}

function resetComparison(ip) {
  comparisonIp = ip;
  selectedReports.before = null; selectedReports.after = null;
  updateSelection();
}

function updateSelection() {
  $("comparison").hidden = true;
  for (const side of ["before", "after"]) {
    const row = selectedReports[side];
    $(`selected-${side}`).textContent = row ? `#${row.id} · ${utc(row.received_at)}` : "Sin seleccionar";
    document.querySelectorAll(`[data-side="${side}"]`).forEach(button => {
      button.setAttribute("aria-pressed", button.dataset.reportId === String(row?.id));
    });
  }
  $("compare").disabled = !selectedReports.before || !selectedReports.after;
}

function comparisonActions(td, row) {
  for (const [side, label] of [["before", "Base"], ["after", "Comparado"]]) {
    const button = document.createElement("button");
    button.textContent = label; button.className = "secondary pick";
    button.dataset.side = side; button.dataset.reportId = row.id;
    button.setAttribute("aria-label", `Usar #${row.id} como ${label.toLowerCase()}`);
    button.setAttribute("aria-pressed", selectedReports[side]?.id === row.id);
    button.onclick = () => {
      ++requestNumber;
      selectedReports[side] = row; updateSelection();
      status("Elegí una base y una captura posterior; después pulsá Comparar selección.");
    };
    td.append(button);
  }
}

async function runComparison() {
  if (!selectedReports.before || !selectedReports.after) return;
  const number = ++requestNumber;
  $("comparison").hidden = true;
  status("Comparando relevamientos…");
  const query = `before=${selectedReports.before.id}&after=${selectedReports.after.id}`;
  try {
    const data = await get(`/comparisons?${query}`);
    if (number !== requestNumber) return;
    $("comparison").hidden = false;
    $("comparison-title").textContent = `${data.server_ip} · #${data.before.id} → #${data.after.id}`;
    $("comparison-source").textContent = `Base recibida: ${utc(data.before.received_at)} · Comparado recibido: ${utc(data.after.received_at)} · Generado: ${utc(data.generated_at)}`;
    facts($("comparison-summary"), [["Diferencias", data.changes.length], ["Nombres de proceso con cambios", data.summary.Procesos || 0], ["Grupos de sesiones con cambios", data.summary.Sesiones || 0]]);
    cells($("change-rows"), data.changes.slice(0, 100).map(item => [item.section, item.field, item.before, item.after]));
    $("change-count").textContent = data.changes.length > 100
      ? `Mostrando 100 de ${data.changes.length} diferencias. Las exportaciones incluyen todas.`
      : data.changes.length ? "En procesos y sesiones, los valores son cantidades observadas." : "Sin diferencias en los campos comparados.";
    $("comparison-notes").replaceChildren();
    const notes = [...data.notes, ...data.before.warnings.map(text => `Aviso de la base: ${text}`), ...data.after.warnings.map(text => `Aviso del comparado: ${text}`)];
    for (const note of notes) {
      const li = document.createElement("li"); li.textContent = note; $("comparison-notes").append(li);
    }
    $("export-csv").href = `/comparisons?${query}&format=csv`;
    $("export-html").href = `/comparisons?${query}&format=html`;
    status("Comparación lista. Podés descargar el CSV o abrir el informe imprimible.");
    $("comparison").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) { if (number === requestNumber) status(error.message, true); }
}

$("stale-hours").oninput = describeThreshold;
describeThreshold();
$("dashboard-filter").onsubmit = event => { event.preventDefault(); dashboardOffset = 0; load(); };
$("hosts-previous").onclick = () => { dashboardOffset = Math.max(0, dashboardOffset - pageSize); load(); };
$("hosts-next").onclick = () => { dashboardOffset += pageSize; load(); };
$("compare").onclick = runComparison;
