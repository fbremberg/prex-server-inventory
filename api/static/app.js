"use strict";
const $ = (id) => document.getElementById(id);
let version = "v1", view = "current", offset = 0, report = null, requestNumber = 0;
const pageSize = 25;
const utc = (value) => value ? new Date(value).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC") : "No disponible";

function status(message, error = false) {
  $("status").textContent = message;
  $("status").className = error ? "error" : "";
}

async function get(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) {
    const messages = {404: "No hay datos para esta consulta.", 422: "Revisá la IPv4 y la fecha ingresadas."};
    const detail = await response.json().catch(() => ({}));
    throw new Error(typeof detail.detail === "string" ? detail.detail : messages[response.status] || "No se pudo completar la consulta.");
  }
  return response.json();
}

// Los valores del inventario se insertan como texto, nunca como HTML ejecutable.
function cells(body, rows) {
  body.replaceChildren();
  for (const values of rows) {
    const tr = document.createElement("tr");
    for (const value of values) {
      const td = document.createElement("td");
      td.textContent = value ?? "No disponible";
      tr.append(td);
    }
    body.append(tr);
  }
}

function facts(target, values) {
  target.replaceChildren();
  for (const [name, value] of values) {
    const item = document.createElement("div"); item.className = "fact";
    const label = document.createElement("span"); label.textContent = name;
    const text = document.createElement("strong"); text.textContent = value;
    item.append(label, text); target.append(item);
  }
}

function showReport(data) {
  report = data;
  $("report").hidden = false;
  $("report-title").textContent = `${data.hostname} · ${data.server_ip}`;
  $("timestamps").textContent = `Recolectado: ${utc(data.collected_at)} · Recibido: ${utc(data.received_at)}`;
  $("report-context").textContent = view === "history" ? "Detalle de una ejecución histórica seleccionada." : "Último envío recibido. Consultar no genera una nueva recolección.";
  const cpu = [
    ["Modelo", data.cpu.model || "No disponible"],
    ["Arquitectura", data.cpu.architecture],
    ["Núcleos físicos / lógicos", `${data.cpu.physical_cores ?? "—"} / ${data.cpu.logical_cores ?? "—"}`],
    ["Frecuencia informada", data.cpu.frequency_mhz === null ? "No disponible" : `${data.cpu.frequency_mhz} MHz`]
  ];
  $("cpu").replaceChildren();
  for (const [name, value] of cpu) {
    const item = document.createElement("div");
    const label = document.createElement("dt"); label.textContent = name;
    const text = document.createElement("dd"); text.textContent = value;
    item.append(label, text); $("cpu").append(item);
  }
  $("os-name").textContent = data.operating_system.name;
  $("os-version").textContent = data.operating_system.version;
  $("os-version-hint").textContent = data.operating_system.name.startsWith("Windows")
    ? "Versión interna / compilación de Windows; puede empezar con 10.0 también en Windows 11."
    : "Versión de la distribución; si no está disponible, se informa la del kernel.";
  const users = new Set(data.sessions.map(s => s.username)).size;
  facts($("summary"), [["Procesos", data.processes.length], ["Usuarios / sesiones", `${users} / ${data.sessions.length}`], ["Avisos de recolección", data.warnings.length]]);
  $("summary").hidden = version !== "v2";
  $("warnings").replaceChildren();
  for (const message of data.warnings.length ? data.warnings : ["Sin avisos de recolección."]) {
    const item = document.createElement("li"); item.textContent = message; $("warnings").append(item);
  }
  $("process-count").textContent = `Procesos informados: ${data.processes.length}. El listado tiene desplazamiento vertical.`;
  $("session-count").textContent = `Usuarios: ${users}. Sesiones informadas: ${data.sessions.length}. Un usuario puede tener más de una sesión.`;
  cells($("processes"), data.processes.length ? data.processes.map(p => [p.pid, p.name]) : [["—", "El sistema no informó procesos."]]);
  cells($("sessions"), data.sessions.length ? data.sessions.map(s => [s.username, s.terminal, utc(s.started_at)]) : [["El sistema no informó sesiones. Revisá las observaciones.", "—", "—"]]);
}

async function load() {
  const number = ++requestNumber;
  report = null; $("report").hidden = true;
  for (const id of ["history", "dashboard", "comparison"]) $(id).hidden = true;
  $("search-card").hidden = view === "dashboard";
  const selectedIp = $("ip").value.trim();
  if (comparisonIp !== selectedIp) resetComparison(selectedIp);
  status("Consultando…");
  try {
    const ip = encodeURIComponent($("ip").value.trim());
    if (view === "dashboard") {
      await loadDashboard(number);
    } else if (view === "current") {
      const data = await get(`/reports?ip=${ip}`);
      if (number !== requestNumber) return;
      showReport(data); status("Último relevamiento recibido.");
    } else {
      const day = $("day").value ? `&day=${encodeURIComponent($("day").value)}` : "";
      const rows = await get(`/history?ip=${ip}&limit=${pageSize}&offset=${offset}${day}`);
      if (number !== requestNumber) return;
      $("history").hidden = false;
      cells($("history-rows"), rows.map(r => [utc(r.received_at), r.hostname]));
      rows.forEach((row, index) => {
        const td = document.createElement("td"), button = document.createElement("button");
        button.textContent = `Ver #${row.id}`; button.className = "secondary";
        button.onclick = async () => {
          const selected = ++requestNumber;
          $("report").hidden = true; report = null;
          try {
            const data = await get(`/history/${row.id}`);
            if (selected !== requestNumber) return;
            showReport(data); status(`Relevamiento #${row.id}.`);
          } catch (error) { if (selected === requestNumber) status(error.message, true); }
        };
        td.className = "history-actions"; td.append(button); comparisonActions(td, row);
        $("history-rows").children[index].append(td);
      });
      $("previous").disabled = offset === 0;
      $("next").disabled = rows.length < pageSize;
      $("page").textContent = `Página ${offset / pageSize + 1}`;
      status(rows.length ? "Seleccioná un relevamiento para ver su detalle." : "No hay relevamientos para esta búsqueda.");
    }
  } catch (error) { if (number === requestNumber) status(error.message, true); }
}

for (const selectedVersion of ["v1", "v2"]) {
  $(`${selectedVersion}-tab`).onclick = () => {
    version = selectedVersion; view = version === "v2" ? "dashboard" : "current"; offset = 0;
    $("v1-tab").setAttribute("aria-pressed", version === "v1");
    $("v2-tab").setAttribute("aria-pressed", version === "v2");
    $("views").hidden = version !== "v2";
    $("dashboard-tab").setAttribute("aria-pressed", view === "dashboard");
    $("current-tab").setAttribute("aria-pressed", view === "current");
    $("history-tab").setAttribute("aria-pressed", "false");
    $("version-title").textContent = version === "v1" ? "Los cinco datos solicitados" : "Seguimiento y análisis del inventario";
    $("version-description").textContent = version === "v1"
      ? "Procesador, procesos corriendo, usuarios con sesión abierta, nombre y versión del sistema operativo. Consulta del último envío por IP y descarga JSON."
      : "Dashboard de equipos, vigencia de los envíos, histórico y comparación entre capturas. Exportá diferencias con sus fuentes en CSV o en un informe imprimible.";
    load();
  };
}

$("search").onsubmit = (event) => { event.preventDefault(); offset = 0; load(); };
function selectView(selectedView) {
  view = selectedView; offset = 0;
  for (const [id, target] of [["dashboard-tab", "dashboard"], ["current-tab", "current"], ["history-tab", "history"]]) {
    $(id).setAttribute("aria-pressed", view === target);
  }
  load();
}
for (const [id, target] of [["dashboard-tab", "dashboard"], ["current-tab", "current"], ["history-tab", "history"]]) {
  $(id).onclick = () => selectView(target);
}
$("filter").onclick = () => { offset = 0; load(); };
$("previous").onclick = () => { offset = Math.max(0, offset - pageSize); load(); };
$("next").onclick = () => { offset += pageSize; load(); };
$("clear").onclick = () => {
  ++requestNumber; report = null; resetComparison($("ip").value.trim());
  $("report").hidden = true; $("history").hidden = true;
  for (const id of ["history-rows", "cpu", "summary", "processes", "sessions", "warnings"]) $(id).replaceChildren();
  status("Resultados limpiados.");
};
$("download").onclick = () => {
  if (!report) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type: "application/json"}));
  const link = document.createElement("a"); link.href = url;
  link.download = `${report.server_ip}_${report.received_at.slice(0, 10)}.json`;
  document.body.append(link);
  link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
