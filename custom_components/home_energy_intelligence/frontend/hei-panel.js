const TABS = [
  ["overview", "mdi:home-outline", "Overview"],
  ["thermal", "mdi:home-thermometer-outline", "House Thermal Profile"],
  ["cop", "mdi:chart-bar", "COP & Efficiency"],
  ["history", "mdi:clock-outline", "History"],
  ["forecasts", "mdi:chart-timeline-variant", "Forecasts"],
  ["settings", "mdi:cog-outline", "Settings"],
];

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
const n = (value, digits = 1) => value == null ? "—" : Number(value).toFixed(digits);
const sum = (object, keys) => keys.reduce((total, key) => total + Number(object?.[key] || 0), 0);

class HomeEnergyIntelligencePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.active = "overview";
    this.loading = true;
    this.latest = null;
    this.history = [];
    this.historySummary = null;
    this.settings = null;
    this.equipment = [];
    this.temperatureSources = [];
    this.overviewData = null;
    this.period = "daily";
    this.historyOffset = 0;
  }

  set hass(value) {
    const first = !this._hass;
    this._hass = value;
    if (first) {
      this.load();
      this._poll = window.setInterval(() => this.load(false), 5 * 60 * 1000);
    }
  }
  set panel(value) { this._panel = value; }
  disconnectedCallback() { if (this._poll) window.clearInterval(this._poll); }

  async load(showLoading = true) {
    if (showLoading) this.loading = true;
    this.error = null;
    this.render();
    try {
      const [overview, history] = await Promise.all([
        this._hass.callWS({ type: "home_energy_intelligence/overview" }),
        this._hass.callWS({ type: "home_energy_intelligence/history", limit: 90 }),
      ]);
      let settings = null;
      try {
        settings = await this._hass.callWS({ type: "home_energy_intelligence/settings" });
      } catch (error) {
        if (!String(error?.code || error?.message || error).includes("unauthorized")) throw error;
      }
      this.latest = overview.latest;
      this.overviewData = overview;
      this.equipment = (overview.equipment || []).filter((item) => item.enabled);
      this.temperatureSources = (overview.temperature_sources || []).filter((item) => item.enabled);
      this.history = history.records || [];
      this.historySummary = history.summary;
      this.settings = settings;
    } catch (error) {
      if (this.overviewData) {
        this.refreshError = error?.message || String(error);
        this.overviewData.stale = true;
      } else this.error = error?.message || String(error);
    }
    this.loading = false;
    this.render();
  }

  category(record, device, keys) {
    return sum(record?.allocations?.[device]?.categories, keys);
  }

  devices(kind) {
    return this.equipment.filter((item) => !kind || item.kind === kind);
  }

  categories(record, kinds, keys) {
    return this.equipment
      .filter((item) => kinds.includes(item.kind))
      .reduce((total, item) => total + this.category(record, item.equipment_id, keys), 0);
  }

  assumedHeating(record) {
    return this.devices("heat_pump").reduce((total, item) => {
      const allocation = record?.allocations?.[item.equipment_id];
      return total + (allocation?.reported_assumed_heating_kwh
        ?? this.category(record, item.equipment_id, ["heating_assumed"]));
    }, 0);
  }

  heating(record) {
    return this.categories(record, ["heat_pump"], ["confirmed_heating"])
      + this.assumedHeating(record)
      + this.categories(record, ["air_conditioner"], ["heating", "heating_selected", "heating_estimated"]);
  }

  cooling(record) {
    return this.categories(record, ["heat_pump", "air_conditioner"], ["cooling", "cooling_selected", "cooling_estimated"]);
  }

  temperatureForRole(record, role) {
    const source = this.temperatureSources.find((item) => item.role === role);
    return source ? record?.temperatures?.[source.source_id] : null;
  }

  kpis() {
    const r = this.latest;
    if (!r) return [
      ["mdi:flash", "Whole-House Electricity", null, "blue"],
      ["mdi:radiator", "Heating Electricity", null, "red"],
      ["mdi:water", "DHW Electricity", null, "orange"],
      ["mdi:heat-wave", "Heating + DHW", null, "orange"],
      ["mdi:snowflake", "Cooling Electricity", null, "blue"],
      ["mdi:white-balance-sunny", "PV Generation", null, "amber"],
      ["mdi:leaf", "Self-Sufficiency", null, "green", "%"],
    ];
    const energy = r.energy || {};
    return [
      ["mdi:flash", "Whole-House Electricity", energy.household_kwh, "blue"],
      ["mdi:radiator", "Heating Electricity", energy.space_heating_kwh, "red"],
      ["mdi:water", "DHW Electricity", energy.dhw_kwh, "orange"],
      ["mdi:heat-wave", "Heating + DHW", energy.heating_and_dhw_kwh, "orange"],
      ["mdi:snowflake", "Cooling Electricity", energy.cooling_kwh, "blue"],
      ["mdi:white-balance-sunny", "PV Generation", energy.pv_kwh, "amber"],
      ["mdi:leaf", "Self-Sufficiency", energy.self_sufficiency_percent, "green", "%"],
    ];
  }

  tabs() {
    return `<nav class="tabs" aria-label="Home Energy Intelligence sections">${TABS.map(([id, icon, label]) => `
      <button class="tab ${this.active === id ? "active" : ""}" data-tab="${id}">
        <ha-icon icon="${icon}"></ha-icon><span>${label}</span>
      </button>`).join("")}</nav>`;
  }

  quality(record) {
    const value = record?.quality || "missing";
    return `<span class="quality ${esc(value)}">${esc(value)}</span>`;
  }

  splitChart() {
    const records = [...this.history].slice(0, 7).reverse();
    if (!records.length) return `<div class="empty">No validated daily records yet.</div>`;
    const max = Math.max(1, ...records.map((r) => r.household_kwh || 0));
    return `<div class="bars" role="img" aria-label="Daily electricity split">${records.map((r) => {
      const heat = r.energy?.space_heating_kwh || 0;
      const dhw = r.energy?.dhw_kwh || 0;
      const cool = r.energy?.cooling_kwh || 0;
      const idle = r.energy?.idle_kwh || 0;
      const other = Math.max(0, r.energy?.other_household_kwh || 0);
      const unknown = (r.energy?.unknown_kwh || 0) + Math.max(0, r.energy?.unallocated_discrepancy_kwh || 0);
      const scale = 150 / max;
      return `<div class="bar-col" title="${esc(r.local_date)}">
        <div class="bar" style="height:${Math.max(2, (r.household_kwh || 0) * scale)}px">
          <i class="seg unknown" style="height:${unknown * scale}px"></i>
          <i class="seg other" style="height:${other * scale}px"></i>
          <i class="seg idle" style="height:${idle * scale}px"></i>
          <i class="seg cool" style="height:${cool * scale}px"></i>
          <i class="seg dhw" style="height:${dhw * scale}px"></i>
          <i class="seg heat" style="height:${heat * scale}px"></i>
        </div><span>${esc(r.local_date.slice(5))}</span></div>`;
    }).join("")}</div><div class="legend"><i class="dot heat"></i>Heating <i class="dot dhw"></i>DHW <i class="dot cool"></i>Cooling <i class="dot idle"></i>Idle <i class="dot other"></i>Other household <i class="dot unknown"></i>Unknown/discrepancy</div>`;
  }

  scatterChart() {
    const points = this.history.flatMap((r) => {
      const temp = this.temperatureForRole(r, "outdoor")?.mean;
      const heat = r.energy?.space_heating_kwh;
      return temp == null ? [] : [[temp, heat, r.local_date]];
    });
    if (!points.length) return `<div class="empty">Needs validated outdoor temperature and heating records.</div>`;
    const xs = points.map((p) => p[0]); const ys = points.map((p) => p[1]);
    const xmin = Math.min(...xs) - 1, xmax = Math.max(...xs) + 1;
    const ymax = Math.max(1, ...ys) * 1.1;
    const px = (x) => 42 + ((x - xmin) / Math.max(1, xmax - xmin)) * 480;
    const py = (y) => 180 - (y / ymax) * 145;
    return `<svg viewBox="0 0 550 215" class="chart" role="img" aria-label="Heating electricity versus outdoor temperature">
      ${[0, .25, .5, .75, 1].map((v) => `<line x1="42" x2="522" y1="${35 + v * 145}" y2="${35 + v * 145}" />`).join("")}
      <text x="8" y="40">${n(ymax)}</text><text x="22" y="183">0</text>
      ${points.map((p) => `<circle cx="${px(p[0])}" cy="${py(p[1])}" r="5"><title>${esc(p[2])}: ${n(p[0])} °C, ${n(p[1])} kWh</title></circle>`).join("")}
      <text x="42" y="205">${n(xmin)} °C</text><text x="480" y="205">${n(xmax)} °C</text>
    </svg>`;
  }

  recentTable(records = this.history.slice(0, 7)) {
    if (!records.length) return `<div class="empty">No daily records yet. The first close runs at 05:00 local time.</div>`;
    return `<div class="table-wrap"><table><thead><tr><th>Date</th><th>Whole house</th><th>HP confirmed</th><th>HP assumed</th><th>AC confirmed</th><th>AC selected/estimated</th><th>Total heating</th><th>DHW</th><th>Heating + DHW</th><th>Cooling</th><th>HVAC idle</th><th>Other household</th><th>Unknown / discrepancy</th><th>PV</th><th>Import</th><th>Export</th><th>Self-sufficiency</th><th>Quality</th></tr></thead><tbody>
      ${records.map((r) => `<tr><td>${esc(r.local_date)}</td><td>${n(r.energy?.household_kwh)} kWh</td>
        <td>${n(r.energy?.confirmed_heat_pump_heating_kwh)} kWh</td><td>${n(r.energy?.assumed_heat_pump_heating_kwh)} kWh</td>
        <td>${n(r.energy?.confirmed_ac_heating_kwh)} kWh</td><td>${n((r.energy?.selected_ac_heating_kwh || 0)+(r.energy?.estimated_ac_heating_kwh || 0))} kWh</td>
        <td>${n(r.energy?.space_heating_kwh)} kWh</td><td>${n(r.energy?.dhw_kwh)} kWh</td><td>${n(r.energy?.heating_and_dhw_kwh)} kWh</td><td>${n(r.energy?.cooling_kwh)} kWh</td>
        <td>${n(r.energy?.idle_kwh)} kWh</td><td>${n(r.energy?.other_household_kwh)} kWh</td><td>${n((r.energy?.unknown_kwh || 0)+(r.energy?.unallocated_discrepancy_kwh || 0))} kWh</td>
        <td>${n(r.energy?.pv_kwh)} kWh</td><td>${n(r.energy?.import_kwh)} kWh</td><td>${n(r.energy?.export_kwh)} kWh</td><td>${n(r.energy?.self_sufficiency_percent)}%</td><td>${this.quality(r)}</td></tr>`).join("")}
    </tbody></table></div>`;
  }

  overview() {
    const comparison = this.overviewData?.yesterday_same_cutoff?.energy;
    const full = this.overviewData?.yesterday_full?.energy;
    const status = this.overviewData?.stale ? `Stale — ${this.refreshError || "refresh required"}` : "Actual through latest completed hour";
    const metric = (label, object) => label === "Whole-House Electricity" ? object?.household_kwh : label === "Heating Electricity" ? object?.space_heating_kwh : label === "DHW Electricity" ? object?.dhw_kwh : label === "Heating + DHW" ? object?.heating_and_dhw_kwh : label === "Cooling Electricity" ? object?.cooling_kwh : label === "PV Generation" ? object?.pv_kwh : object?.self_sufficiency_percent;
    return `<div class="toolbar"><div><b>Today <span class="quality partial">Partial</span></b><span>${esc(status)} · cutoff ${esc(this.overviewData?.cutoff_utc || "—")} · updated ${esc(this.latest?.updated_at || "—")}</span></div><button class="refresh">Refresh now</button></div>
    <section class="kpis">${this.kpis().map(([icon, label, value, color, unit = "kWh"]) => `
      <article class="card kpi"><div class="kpi-icon ${color}"><ha-icon icon="${icon}"></ha-icon></div>
      <div><span>${label}</span><strong>${n(value)} ${value == null ? "" : unit}</strong>
      <small>${this.comparisonText(value, metric(label, comparison), metric(label, full), unit)}</small></div></article>`).join("")}</section>
    <section class="grid top-grid">
      <article class="card wide"><h2><ha-icon icon="mdi:home-thermometer-outline"></ha-icon>Heating Energy vs Outdoor Temperature</h2>${this.scatterChart()}</article>
      <article class="card"><h2><ha-icon icon="mdi:chart-box-outline"></ha-icon>Daily Energy Split</h2>${this.splitChart()}</article>
      <article class="card status-card"><h2><ha-icon icon="mdi:gauge"></ha-icon>COP & Efficiency</h2><div class="upcoming"><ha-icon icon="mdi:timeline-clock-outline"></ha-icon><b>Planned for V0.2</b><span>No thermal output is inferred from electricity.</span></div></article>
    </section>
    <section class="grid bottom-grid">
      ${this.temperatureCard("indoor")}${this.temperatureCard("outdoor")}
      <article class="card"><h2><ha-icon icon="mdi:solar-power"></ha-icon>Solar PV</h2><div class="mini-stats"><span>On-site use <b>${n(this.latest?.energy?.pv_consumed_on_site_kwh)} kWh</b></span><span>Self-consumption <b>${n(this.latest?.energy?.pv_self_consumption_percent)}%</b></span><span>Export <b>${n(this.latest?.energy?.export_kwh)} kWh</b></span></div><p class="muted">Unavailable inputs remain unavailable.</p></article>
      <article class="card wide"><h2><ha-icon icon="mdi:clock-outline"></ha-icon>Recent History</h2>${this.recentTable(this.history.slice(0, 5))}</article>
    </section><section class="card page-card"><h2>Equipment breakdown</h2>${this.equipmentBreakdown()}</section>`;
  }

  equipmentBreakdown() {
    const rows = this.latest?.energy?.equipment || [];
    if (!rows.length) return `<div class="empty">No enabled equipment data for this period.</div>`;
    return `<div class="table-wrap"><table><thead><tr><th>Equipment</th><th>Meter</th><th>Confirmed heating</th><th>Assumed/selected/estimated</th><th>DHW</th><th>Cooling</th><th>Idle</th><th>Residual</th><th>Quality</th></tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(r.display_name)}</td><td>${n(r.meter_kwh)} kWh</td><td>${n(r.confirmed_heating_kwh)} kWh</td><td>${n((r.assumed_heating_kwh || 0)+(r.selected_heating_kwh || 0)+(r.estimated_heating_kwh || 0))} kWh</td><td>${n(r.dhw_kwh)} kWh</td><td>${n(r.cooling_kwh)} kWh</td><td>${n(r.idle_kwh)} kWh</td><td>${n(r.residual_kwh)} kWh</td><td>${this.quality(r)}</td></tr>`).join("")}</tbody></table></div>`;
  }

  temperatureCard(role) {
    const configuredId = this.overviewData?.[`representative_${role}_source_id`];
    const source = this.temperatureSources.find((item) => item.source_id === configuredId)
      || this.temperatureSources.find((item) => item.role === role);
    const today = source ? this.latest?.temperatures?.[source.source_id] : null;
    const yesterday = source ? this.overviewData?.yesterday_full?.temperatures?.[source.source_id] : null;
    const label = role === "indoor" ? "Indoor" : "Outdoor";
    const explanation = role === "indoor" ? "Selected location; not a whole-house mean." : "Selected measured source; not weather-provider data.";
    return `<article class="card temperature-card"><h2><ha-icon icon="${role === "indoor" ? "mdi:home-thermometer" : "mdi:thermometer-lines"}"></ha-icon>Representative ${label} Temperature</h2>
      <div class="temperature">${n(today?.latest_value)}${today?.latest_value == null ? "" : " °C"}</div>
      <p class="muted">Latest valid: ${esc(today?.latest_timestamp || "unavailable")} · ${esc(explanation)}</p>
      <div class="mini-stats"><span>Today mean <b>${n(today?.mean)} °C</b></span><span>Yesterday mean <b>${n(yesterday?.mean)} °C</b></span><span>Min / max <b>${n(today?.minimum)} / ${n(today?.maximum)} °C</b></span><span>Coverage <b>${n(today?.coverage_percent,0)}%</b></span><span>Quality <b>${esc(today?.quality || "missing")}</b></span></div>${this.temperatureTimeline(role)}</article>`;
  }

  comparisonText(today, same, full, unit) {
    if (today == null || same == null) return `Yesterday same cutoff unavailable · full ${n(full)} ${full == null ? "" : unit}`;
    const difference = today-same; const percent = same>0 ? 100*difference/same : null;
    return `Yesterday same cutoff ${n(same)} ${unit} · Δ ${difference>=0?"+":""}${n(difference)} ${unit}${percent==null?"":` (${percent>=0?"+":""}${n(percent)}%)`} · full ${n(full)} ${full==null?"":unit}`;
  }

  temperatureTimeline(role = null) {
    const timeline = this.overviewData?.temperature_timeline || [];
    const sources = this.temperatureSources.filter((source) => role ? source.role === role : ["indoor", "outdoor", "supply", "return"].includes(source.role));
    const values = timeline.flatMap((row) => sources.map((source) => row.temperatures?.[source.source_id]?.mean).filter((value) => value != null));
    if (!timeline.length || !values.length) return `<p class="muted">No validated hourly temperature timeline yet.</p>`;
    const low = Math.min(...values), high = Math.max(...values), span = Math.max(1, high-low);
    const lines = sources.map((source, index) => {
      const segments=[]; let points=[];
      timeline.forEach((row, x) => { const value=row.temperatures?.[source.source_id]?.mean; if(value==null){if(points.length){segments.push(points);points=[];}}else points.push(`${10+x*280/Math.max(1,timeline.length-1)},${75-(value-low)*65/span}`); });
      if(points.length) segments.push(points);
      return segments.map((segment) => `<polyline points="${segment.join(" ")}" class="temp-line t${index%4}"><title>${esc(source.display_name)}</title></polyline>`).join("");
    }).join("");
    return `<svg class="temp-chart" viewBox="0 0 300 85" role="img" aria-label="Validated hourly temperature timelines">${lines}</svg>`;
  }

  roadmap(title, version, text) {
    return `<section class="roadmap card"><ha-icon icon="mdi:timeline-clock-outline"></ha-icon><h2>${title}</h2><b>${version}</b><p>${text}</p><p>No illustrative values are shown as measurements.</p></section>`;
  }

  historyView() {
    return `<section class="card page-card"><div class="section-head"><div><h2>Validated Energy History</h2><p>Canonical actuals, explicit uncertainty, bounded pagination and CSV export.</p></div><div><button class="csv">Export CSV</button> <button class="refresh">Refresh</button></div></div>
      <div class="period"><label>Period <select class="period-select"><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="monthly">Monthly</option><option value="year">Rolling year</option><option value="calendar">Calendar year</option><option value="season" title="An energy season combines autumn, winter and spring in one reporting year.">Energy season</option><option value="custom">Custom</option></select></label><label>From <input class="start-date" type="date"></label><label>To <input class="end-date" type="date"></label><button class="apply-period">Apply</button></div>
      ${this.periodSummary()}
      <div class="grid temperature-history"><article class="card"><h2>Indoor temperature history</h2>${this.historyTemperatureTimeline("indoor")}</article><article class="card"><h2>Outdoor temperature history</h2>${this.historyTemperatureTimeline("outdoor")}</article></div>
      ${this.recentTable(this.history)}<div class="pager"><button class="previous">Previous</button><span>Showing ${this.historyOffset + 1}–${this.historyOffset + this.history.length}</span><button class="next">Next</button></div></section>`;
  }

  historyTemperatureTimeline(role) {
    const configuredId = this.overviewData?.[`representative_${role}_source_id`];
    const source = this.temperatureSources.find((item) => item.source_id === configuredId)
      || this.temperatureSources.find((item) => item.role === role);
    if (!source) return `<div class="empty">No representative source configured.</div>`;
    const records=[...this.history].reverse();
    const values=records.map((record) => record.temperatures?.[source.source_id]?.mean);
    const valid=values.filter((value) => value!=null);
    if(!valid.length) return `<div class="empty">No validated measurements in this period.</div>`;
    const low=Math.min(...valid), high=Math.max(...valid), span=Math.max(1,high-low); const segments=[]; let points=[];
    values.forEach((value,index)=>{if(value==null){if(points.length){segments.push(points);points=[];}}else points.push(`${10+index*280/Math.max(1,values.length-1)},${75-(value-low)*65/span}`);}); if(points.length)segments.push(points);
    return `<svg class="temp-chart" viewBox="0 0 300 85" role="img" aria-label="${esc(role)} daily time-weighted temperature means">${segments.map((segment)=>`<polyline points="${segment.join(" ")}" class="temp-line t${role==="indoor"?0:1}"></polyline>`).join("")}</svg><p class="muted">Daily means are elapsed-time weighted; missing days are gaps. Period mean ${n(this.historySummary?.temperatures?.[source.source_id]?.mean)} °C.</p>`;
  }

  periodSummary() {
    const energy = this.historySummary?.energy;
    if (!energy) return "";
    return `<div class="mini-stats period-summary"><span>Household <b>${n(energy.household_kwh)} kWh</b></span><span>Heating <b>${n(energy.space_heating_kwh)} kWh</b></span><span>DHW <b>${n(energy.dhw_kwh)} kWh</b></span><span>Heating + DHW <b>${n(energy.heating_and_dhw_kwh)} kWh</b></span><span>Cooling <b>${n(energy.cooling_kwh)} kWh</b></span><span>PV <b>${n(energy.pv_kwh)} kWh</b></span><span>Import / export <b>${n(energy.import_kwh)} / ${n(energy.export_kwh)} kWh</b></span></div>`;
  }

  settingsView() {
    if (!this.settings) return `<section class="card page-card"><h2>Administrator access required</h2><p>Overview and History remain available. Integration mappings and database details are restricted to administrators.</p></section>`;
    if (!this.settings.configured) return `<section class="card page-card"><h2>Setup required</h2><p>Add the integration from Settings → Devices & services.</p></section>`;
    return `<section class="settings-grid">
      <article class="card page-card"><h2>Processing</h2><dl><dt>Time zone</dt><dd>${esc(this.settings.timezone)}</dd><dt>Cycle</dt><dd>${esc(this.settings.cycle_hours)} hours</dd><dt>Floor area</dt><dd>${n(this.settings.floor_area_m2, 0)} m²</dd><dt>Database</dt><dd>${n(this.settings.database_size_bytes / 1024, 0)} KiB</dd><dt>InfluxDB backfill</dt><dd>${this.settings.influx_enabled ? "Enabled (read-only)" : "Disabled"}</dd></dl>
      <a class="primary" href="/config/integrations/integration/home_energy_intelligence">Open integration options</a></article>
      <article class="card page-card"><h2>Entity mapping</h2><div class="mapping">${this.settings.mapping.map((row) => `<div><span>${esc(row.label)}</span><code>${row.entity_id ? esc(row.entity_id) : "Not selected"}</code></div>`).join("")}</div></article>
       <article class="card page-card notice"><h2>Data guarantees</h2><p>Recorder is read-only. Unknown heat-pump operation is reported as assumed heating, idle remains separate, gaps remain gaps, and unrelated circuit sensors are never substituted.</p></article>
    </section>`;
  }

  content() {
    if (this.loading) return `<div class="loading"><ha-circular-progress active></ha-circular-progress><span>Loading validated records…</span></div>`;
    if (this.error) return `<div class="error">${esc(this.error)}</div>`;
    if (this.active === "overview") return this.overview();
    if (this.active === "history") return this.historyView();
    if (this.active === "settings") return this.settingsView();
    if (this.active === "thermal") return this.roadmap("House Thermal Profile", "Planned for V0.2", "Building-level response curves will combine Ecodan and AC space heating while excluding DHW.");
    if (this.active === "cop") return this.roadmap("COP & Efficiency", "Planned for V0.2", "COP requires separately measured or entered thermal energy; it is not calculated from electricity alone.");
    return this.roadmap("Forecasts", "Planned for V0.3", "Weather-dependent energy forecasts and uncertainty bands are intentionally deferred.");
  }

  render() {
    this.shadowRoot.innerHTML = `<style>${HomeEnergyIntelligencePanel.styles}</style>
      <main><header><div class="brand"><ha-icon icon="mdi:home-lightning-bolt-outline"></ha-icon><div><h1>Home Energy Intelligence</h1><p>Whole-house energy analytics and forecasting</p></div></div>
      <div class="connection"><i></i><div><b>${this.latest || this.settings?.configured ? "Connected" : "Setup required"}</b><span>${this.latest ? `Live partial ${esc(this.latest.local_date)}` : "No completed hourly interval yet"}</span></div></div></header>
      ${this.tabs()}<div class="content">${this.content()}</div></main>`;
    this.shadowRoot.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => { this.active = button.dataset.tab; this.render(); }));
    this.shadowRoot.querySelector(".refresh")?.addEventListener("click", () => this.refreshNow());
    this.shadowRoot.querySelector(".csv")?.addEventListener("click", () => this.exportCsv());
    this.shadowRoot.querySelector(".apply-period")?.addEventListener("click", () => this.loadPeriod());
    this.shadowRoot.querySelector(".next")?.addEventListener("click", () => { this.historyOffset += 90; this.loadPeriod(); });
    this.shadowRoot.querySelector(".previous")?.addEventListener("click", () => { this.historyOffset = Math.max(0, this.historyOffset - 90); this.loadPeriod(); });
  }

  async loadPeriod() {
    const period = this.shadowRoot.querySelector(".period-select")?.value || "daily";
    const endDate = new Date(); let startDate = new Date(endDate);
    if (period === "weekly") startDate.setDate(endDate.getDate()-6);
    else if (period === "monthly") startDate.setDate(endDate.getDate()-30);
    else if (period === "year") startDate.setFullYear(endDate.getFullYear()-1);
    else if (period === "calendar") startDate = new Date(endDate.getFullYear(),0,1);
    else if (period === "season") { const month=(this.overviewData?.energy_season_start_month || 7)-1, day=this.overviewData?.energy_season_start_day || 1; const passed=endDate.getMonth()>month || (endDate.getMonth()===month && endDate.getDate()>=day); startDate=new Date(endDate.getFullYear()-(passed?0:1),month,day); }
    const iso = (value) => value.toISOString().slice(0,10);
    const start = period === "custom" ? this.shadowRoot.querySelector(".start-date")?.value : (period === "daily" ? null : iso(startDate));
    const end = period === "custom" ? this.shadowRoot.querySelector(".end-date")?.value : (period === "daily" ? null : iso(endDate));
    const response = await this._hass.callWS({type:"home_energy_intelligence/history", limit:90, offset:this.historyOffset, ...(start?{start_date:start}:{}), ...(end?{end_date:end}:{})});
    this.history = response.records || []; this.historySummary = response.summary; this.render();
  }

  async refreshNow() {
    try { await this._hass.callWS({type:"home_energy_intelligence/refresh"}); this.refreshError=null; await this.load(); }
    catch(error) { this.refreshError=error?.message || String(error); if(this.overviewData)this.overviewData.stale=true; this.loading=false; this.render(); }
  }

  async exportCsv() {
    const response = await this._hass.callWS({type:"home_energy_intelligence/history_csv"});
    const link = document.createElement("a"); link.href = URL.createObjectURL(new Blob([response.csv], {type:"text/csv"})); link.download = response.filename; link.click(); URL.revokeObjectURL(link.href);
  }

  static styles = `
    :host{display:block;background:var(--primary-background-color,#f4f7fb);color:var(--primary-text-color,#17233b);min-height:100%;font-family:var(--paper-font-body1_-_font-family,Inter,system-ui,sans-serif)}
    *{box-sizing:border-box} main{max-width:1600px;margin:auto;padding:24px 26px 40px} header{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:14px}.brand{display:flex;align-items:center;gap:18px}.brand>ha-icon{--mdc-icon-size:62px;color:#0984d8}.brand h1{font-size:34px;line-height:1;margin:0;color:var(--primary-text-color,#111827)}.brand p{margin:6px 0 0;font-size:18px;color:var(--secondary-text-color,#65728a)}.connection{display:flex;align-items:center;gap:10px}.connection i{width:12px;height:12px;border-radius:50%;background:#20a35a}.connection div{display:flex;flex-direction:column}.connection span{font-size:12px;color:var(--secondary-text-color,#65728a);margin-top:4px}
    .tabs{display:flex;gap:20px;border-bottom:1px solid var(--divider-color,#dce4ee);overflow-x:auto}.tab{display:flex;align-items:center;gap:10px;border:0;border-bottom:3px solid transparent;background:none;color:var(--secondary-text-color,#41506a);padding:18px 14px;font-size:15px;white-space:nowrap;cursor:pointer}.tab.active{color:var(--primary-color,#0785db);border-bottom-color:var(--primary-color,#0785db);font-weight:700}.content{padding-top:22px}.toolbar,.period,.pager{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.toolbar>div{display:flex;flex-direction:column}.toolbar span{font-size:12px;color:var(--secondary-text-color,#65728a)}.period{justify-content:flex-start;flex-wrap:wrap;padding:12px;background:var(--secondary-background-color,#f2f6fa);border-radius:8px}.period label{display:flex;gap:6px;align-items:center}.card{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#e0e7ef);border-radius:12px;box-shadow:0 3px 12px rgba(35,65,100,.05);padding:18px;min-width:0}.card h2{display:flex;align-items:center;gap:10px;font-size:17px;margin:0 0 16px}.card h2 ha-icon{color:#233b5b}.kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-bottom:18px}.kpi{display:flex;gap:13px;align-items:flex-start}.kpi-icon{display:grid;place-items:center;width:48px;height:48px;border-radius:50%;flex:none}.kpi-icon.blue{color:#0785db;background:#e7f4fe}.kpi-icon.red{color:#ef4444;background:#fff0f0}.kpi-icon.orange{color:#f59e0b;background:#fff6e5}.kpi-icon.amber{color:#f6a800;background:#fff8df}.kpi-icon.green{color:#159b5b;background:#eaf9f1}.kpi-icon ha-icon{--mdc-icon-size:27px}.kpi>div:last-child{display:flex;flex-direction:column;min-width:0}.kpi span{font-size:14px;line-height:1.3}.kpi strong{font-size:24px;margin-top:3px;white-space:nowrap}.kpi small{color:var(--secondary-text-color,#65728a);margin-top:7px}.grid{display:grid;gap:14px;margin-bottom:14px}.top-grid{grid-template-columns:1.3fr 1fr .85fr}.bottom-grid{grid-template-columns:1fr .95fr 1.35fr}.wide{min-width:0}.chart{width:100%;height:220px}.chart line{stroke:var(--divider-color,#dbe4ee);stroke-width:1}.chart text{font-size:11px;fill:var(--secondary-text-color,#66758d)}.chart circle{fill:#46a4ef;stroke:#fff;stroke-width:2}.temp-chart{width:100%;height:85px;margin-top:10px}.temp-line{fill:none;stroke-width:2}.temp-line.t0{stroke:#ef4444}.temp-line.t1{stroke:#238ee8}.temp-line.t2{stroke:#f59e0b}.temp-line.t3{stroke:#159b5b}.bars{height:185px;border-bottom:1px solid var(--divider-color,#dbe4ee);display:flex;align-items:flex-end;justify-content:space-around;gap:10px;padding:10px 10px 0}.bar-col{height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:6px;font-size:11px;color:var(--secondary-text-color,#65728a)}.bar{width:26px;display:flex;flex-direction:column;justify-content:flex-end;border-radius:3px 3px 0 0;overflow:hidden;background:#eef2f6}.seg{display:block;width:100%}.heat{background:#ef4444}.dhw{background:#f59e0b}.cool{background:#238ee8}.idle{background:#8b9bb0}.other{background:#91c788}.unknown{background:#8b5cf6}.legend{display:flex;justify-content:center;align-items:center;gap:7px;font-size:12px;margin-top:12px;flex-wrap:wrap}.dot{width:10px;height:10px;border-radius:2px;margin-left:8px}.upcoming{min-height:150px;border-radius:10px;background:linear-gradient(145deg,#f1f8ff,#e9f4fc);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:8px;color:#2269a5;padding:18px}.upcoming span{font-size:13px;color:#496980}.temperature{font-size:34px;font-weight:700;margin:12px 0}.muted{color:var(--secondary-text-color,#65728a);font-size:13px}.mini-stats{display:flex;gap:20px;border-top:1px solid var(--divider-color,#e0e7ef);padding-top:14px}.mini-stats span{display:flex;flex-direction:column;color:var(--secondary-text-color,#65728a);font-size:12px}.mini-stats b{color:var(--primary-text-color,#17233b);margin-top:4px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}th{background:var(--secondary-background-color,#f2f6fa);text-align:left}th,td{padding:9px 8px;border-bottom:1px solid var(--divider-color,#e4eaf0);white-space:nowrap}.quality{display:inline-flex;padding:3px 7px;border-radius:12px;background:#eef2f6;text-transform:capitalize}.quality.good{background:#e5f7ed;color:#08743d}.quality.partial,.quality.filtered{background:#fff4d6;color:#8a5900}.quality.missing,.quality.unreconciled{background:#ffe9e8;color:#a82e28}.empty{min-height:150px;display:grid;place-items:center;text-align:center;color:var(--secondary-text-color,#65728a);padding:20px}.status-card{min-width:0}.roadmap{max-width:720px;margin:50px auto;text-align:center;padding:54px}.roadmap>ha-icon{--mdc-icon-size:64px;color:var(--primary-color,#0785db)}.roadmap h2{display:block;font-size:28px;margin-top:18px}.roadmap b{color:var(--primary-color,#0785db)}.roadmap p{color:var(--secondary-text-color,#65728a)}.page-card{padding:24px}.section-head{display:flex;justify-content:space-between;align-items:center}.section-head p{margin:4px 0 18px;color:var(--secondary-text-color,#65728a)}button.refresh,button.csv,button.apply-period,button.previous,button.next,.primary{border:0;border-radius:8px;background:var(--primary-color,#0785db);color:#fff;padding:10px 15px;text-decoration:none;cursor:pointer}.settings-grid{display:grid;grid-template-columns:1fr 1.6fr;gap:16px}.notice{grid-column:1/-1}dl{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:24px}dt{color:var(--secondary-text-color,#65728a)}dd{margin:0;text-align:right;font-weight:600}.mapping{display:grid;gap:1px;background:var(--divider-color,#e0e7ef);border:1px solid var(--divider-color,#e0e7ef);border-radius:8px;overflow:hidden}.mapping>div{display:grid;grid-template-columns:1fr 1.25fr;gap:12px;background:var(--card-background-color,#fff);padding:10px}.mapping code{color:var(--secondary-text-color,#65728a);overflow-wrap:anywhere}.loading{display:grid;place-items:center;gap:12px;padding:100px}.error{padding:18px;border-radius:10px;background:#ffe9e8;color:#a82e28}
    @media(max-width:1200px){.kpis{grid-template-columns:repeat(3,1fr)}.top-grid,.bottom-grid{grid-template-columns:1fr 1fr}.status-card,.bottom-grid .wide{grid-column:1/-1}}
    @media(max-width:760px){main{padding:16px 12px}.brand>ha-icon{--mdc-icon-size:42px}.brand h1{font-size:23px}.brand p{font-size:13px}.connection{display:none}.tabs{gap:2px}.tab{padding:13px 11px}.tab span{display:none}.kpis{grid-template-columns:1fr}.top-grid,.bottom-grid,.settings-grid{grid-template-columns:1fr}.status-card,.bottom-grid .wide,.notice{grid-column:auto}.section-head{align-items:flex-start}.mapping>div{grid-template-columns:1fr}.mini-stats{flex-wrap:wrap}}
  `;
}

customElements.define("home-energy-intelligence-panel", HomeEnergyIntelligencePanel);
