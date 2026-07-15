const CIRCUMFERENCE = 653.45; // 2 * pi * 104, must match the SVG radius in style.css
const POLL_MS = 5000;
const EXPORT_CAP_KW = 100;

const gaugeFill = document.getElementById("gaugeFill");
const powerValue = document.getElementById("powerValue");
const powerCaption = document.getElementById("powerCaption");
const statePill = document.getElementById("statePill");
const connDot = document.getElementById("connDot");
const connText = document.getElementById("connText");
const rigsEl = document.getElementById("rigs");
const logList = document.getElementById("logList");

function gaugeClassFor(fraction) {
  if (fraction >= 1.0) return "state-cap";
  if (fraction >= 0.9) return "state-high";
  if (fraction >= 0.7) return "state-mid";
  return "state-low";
}

function relativeTime(unixSeconds) {
  if (!unixSeconds) return "--";
  const diff = Math.max(0, Date.now() / 1000 - unixSeconds);
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function renderGauge(powerKw) {
  const fraction = powerKw == null ? 0 : Math.min(Math.max(powerKw / EXPORT_CAP_KW, 0), 1);
  const offset = CIRCUMFERENCE * (1 - fraction);
  gaugeFill.style.strokeDashoffset = offset;
  gaugeFill.setAttribute("class", `gauge-fill ${gaugeClassFor(powerKw == null ? 0 : powerKw / EXPORT_CAP_KW)}`);

  powerValue.textContent = powerKw == null ? "--" : powerKw.toFixed(1);
  powerCaption.textContent = powerKw == null ? "no data" : `of ${EXPORT_CAP_KW}kW export cap`;
}

function renderStatePill(state, rigs) {
  // Controller now reports a single shared level for both rigs:
  // idle / eco / standard / super (or "partial"/"max" from older builds).
  statePill.textContent = state === "unknown" ? "—" : state.toUpperCase();
  statePill.className = `state-pill ${state}`;
}

function renderConnection(ok) {
  connDot.className = `dot ${ok ? "ok" : "bad"}`;
  connText.textContent = ok ? "connected" : "no connection to inverter";
}

function renderRigs(rigs) {
  const ips = Object.keys(rigs);
  if (ips.length === 0) {
    rigsEl.innerHTML = `<div class="log-empty">No rigs configured yet.</div>`;
    return;
  }

  rigsEl.innerHTML = ips
    .map((ip, i) => {
      const rig = rigs[ip];
      const online = rig.reachable;
      const level = rig.level || rig.commanded_level || "—";
      const hashrate = rig.hashrate_ths != null ? `${rig.hashrate_ths.toFixed(1)}` : "--";
      const temp = rig.temp_c != null ? `${rig.temp_c.toFixed(0)}°` : "--";
      return `
        <div class="rig-card">
          <div class="rig-card-head">
            <div>
              <div class="rig-name">Rig ${i + 1}</div>
              <span class="rig-ip">${ip}</span>
            </div>
            <span class="rig-badge ${online ? "online" : "offline"}">${online ? level : "offline"}</span>
          </div>
          <div class="rig-stats">
            <div>
              <div class="rig-stat-label">Hashrate</div>
              <div class="rig-stat-value">${hashrate} <span style="font-size:12px;color:var(--text-2)">TH/s</span></div>
            </div>
            <div>
              <div class="rig-stat-label">Temp</div>
              <div class="rig-stat-value">${temp}</div>
            </div>
          </div>
        </div>
      `;
    })
    .join("");
}

function renderLog(events) {
  if (!events || events.length === 0) {
    logList.innerHTML = `<li class="log-empty">No events yet.</li>`;
    return;
  }
  logList.innerHTML = events
    .map(
      (e) => `
        <li class="log-item ${e.level}">
          <span class="log-time">${relativeTime(e.time)}</span>
          <span class="log-message">${e.message}</span>
        </li>
      `
    )
    .join("");
}

async function poll() {
  try {
    const resp = await fetch("/api/state", { cache: "no-store" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    renderGauge(data.power_kw);
    renderStatePill(data.control_state || "unknown", data.rigs || {});
    renderConnection(!!data.connection_ok);
    renderRigs(data.rigs || {});
    renderLog(data.events || []);
  } catch (e) {
    renderConnection(false);
    connText.textContent = "dashboard can't reach controller state";
  }
}

poll();
setInterval(poll, POLL_MS);
