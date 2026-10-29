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
const addNullable = (...values) => values.some((value) => value == null)
  ? null : values.reduce((total, value) => total + Number(value), 0);

class HomeEnergyIntelligencePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.active = "overview";
    this.loading = true;
    this.latest = null;
    this.history = [];
    this.historySummary = null;
    this.historyCoverage = null;
    this.settings = null;
    this.equipment = [];
    this.temperatureSources = [];
    this.overviewData = null;
    this.historyPreset = "today";
    this.historyStart = null;
    this.historyEnd = null;
    this.historyOlderMonth = null;
    this.selectedPeriod = null;
    this.historyOffset = 0;
    this.historyTotal = 0;
    this.responseProfileData = null;
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
      const overview = await this._hass.callWS({ type: "home_energy_intelligence/overview" });
      this.overviewData = overview;
      const history = await this._hass.callWS(this.historyRequest());
      let settings = null;
      try {
        settings = await this._hass.callWS({ type: "home_energy_intelligence/settings" });
      } catch (error) {
        if (!String(error?.code || error?.message || error).includes("unauthorized")) throw error;
      }
      this.latest = overview.latest;
      this.equipment = (overview.equipment || []).filter((item) => item.enabled);
      this.temperatureSources = (overview.temperature_sources || []).filter((item) => item.enabled);
      this.history = history.records || [];
      this.historyTotal = history.total || 0;
      this.historySummary = history.summary;
      this.historyCoverage = history.period_coverage;
      this.responseProfileData = history.response_profile;
      this.selectedPeriod = history.selected_period;
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

  historyRequest() {
    return {type:"home_energy_intelligence/history", limit:90, offset:this.historyOffset,
      preset:this.historyPreset,
      ...(this.historyPreset === "today" && this.overviewData?.cutoff_utc
        ? {cutoff_utc:this.overviewData.cutoff_utc}:{}),
      ...(this.historyPreset === "custom" && this.historyStart ? {start_date:this.historyStart}:{}),
      ...(this.historyPreset === "custom" && this.historyEnd ? {end_date:this.historyEnd}:{}),
      ...(this.historyPreset === "older_month" && this.historyOlderMonth ? {older_month:this.historyOlderMonth}:{})};
  }

  temperatureForRole(record, role) {
    const source = this.representativeSource(role);
    return source ? record?.temperatures?.[source.source_id] : null;
  }

  representativeSource(role) {
    const configuredId = this.overviewData?.[`representative_${role}_source_id`];
    return configuredId
      ? this.temperatureSources.find((item) => item.source_id === configuredId) || null
      : null;
  }

  selectedRecord() { return this.historySummary || (this.historyPreset === "today" ? this.latest : null); }

  kpis() {
    const r = this.selectedRecord();
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
      ["mdi:flash", "Whole-House Electricity", energy.household_kwh, "blue", "kWh", r.known_household_kwh],
      ["mdi:radiator", "Heating Electricity", energy.space_heating_kwh, "red", "kWh", energy.known_space_heating_kwh],
      ["mdi:water", "DHW Electricity", energy.dhw_kwh, "orange", "kWh", energy.known_dhw_kwh],
      ["mdi:heat-wave", "Heating + DHW", energy.heating_and_dhw_kwh, "orange", "kWh", energy.known_space_heating_kwh == null ? null : energy.known_space_heating_kwh + (energy.known_dhw_kwh || 0)],
      ["mdi:snowflake", "Cooling Electricity", energy.cooling_kwh, "blue", "kWh", energy.known_cooling_kwh],
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
    const history = [...this.history].slice(0, 6).reverse();
    const records = history;
    if (!records.length) return `<div class="empty">No validated daily records yet.</div>`;
    const max = Math.max(1, ...records.map((r) => r.household_kwh ?? r.known_household_kwh ?? 0));
    return `<div class="bars" role="img" aria-label="Daily electricity split">${records.map((r) => {
      const total = r.household_kwh ?? r.known_household_kwh;
      const heat = r.energy?.space_heating_kwh ?? r.energy?.known_space_heating_kwh ?? 0;
      const dhw = r.energy?.dhw_kwh ?? r.energy?.known_dhw_kwh ?? 0;
      const cool = r.energy?.cooling_kwh ?? r.energy?.known_cooling_kwh ?? 0;
      const idle = r.energy?.idle_kwh ?? r.energy?.known_idle_kwh ?? 0;
      const otherHvac = r.energy?.other_hvac_kwh ?? r.energy?.known_other_hvac_kwh ?? 0;
      const other = r.energy?.other_household_kwh ?? 0;
      const unknown = r.energy?.unknown_kwh ?? 0;
      const discrepancy = r.energy?.unallocated_discrepancy_kwh;
      const scale = 150 / max;
      const segment=(name,value,css)=>value>0?`<i class="seg ${css}" style="height:${value*scale}px" title="${esc(name)}: ${n(value)} kWh · ${n(total>0?100*value/total:null)}% · ${esc(r.energy?.quality || r.quality)} · ${esc(r.local_date)}"></i>`:"";
      return `<div class="bar-col" title="${esc(r.local_date)}">
        <div class="bar" style="height:${Math.max(2,(total||0)*scale)}px">${segment("Unallocated",unknown,"unknown")}${segment("Other household",other,"other")}${segment("Other HVAC (fan/dry)",otherHvac,"other-hvac")}${segment("Idle/standby",idle,"idle")}${segment("Cooling",cool,"cool")}${segment("DHW",dhw,"dhw")}${segment("Space heating",heat,"heat")}</div>
        ${!r.energy?.complete?`<b class="missing" title="Required meter or allocation data is unavailable">missing</b>`:""}
        ${discrepancy!=null&&Math.abs(discrepancy)>.05?`<b class="discrepancy" title="Discrepancy: ${n(discrepancy)} kWh · ${n(total?100*discrepancy/total:null)}% · ${esc(r.energy?.quality || r.quality)} · ${esc(r.local_date)}">Δ ${n(discrepancy)}</b>`:""}${other<0?`<b class="discrepancy" title="Other household: ${n(other)} kWh · ${n(total?100*other/total:null)}% · ${esc(r.energy?.quality || r.quality)} · ${esc(r.local_date)}">Other ${n(other)}</b>`:""}<span>${esc(r.local_date.slice(5))}${r===this.latest||!r.energy?.complete?" partial":""}</span></div>`;
    }).join("")}</div><div class="legend"><i class="dot heat"></i>Heating <i class="dot dhw"></i>DHW <i class="dot cool"></i>Cooling <i class="dot idle"></i>Idle <i class="dot other-hvac"></i>Other HVAC <i class="dot other"></i>Other household <i class="dot unknown"></i>Unallocated <b class="discrepancy">Δ discrepancy (signed)</b></div>`;
  }

  scatterChart() {
    const points = this.history.flatMap((r) => {
      const temp = this.temperatureForRole(r, "outdoor")?.mean;
      const heat = r.energy?.space_heating_kwh;
      return temp == null || heat == null ? [] : [[temp, heat, r.local_date]];
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

  responseProfile() {
    const area=Number(this.overviewData?.floor_area_m2); const source=this.representativeSource("outdoor");
    if(!source) return `<div class="empty">Configure a representative measured outdoor-air sensor to build the electrical response curve.</div>`;
    if(!(area>0)) return `<div class="empty">Configure floor area to normalize electrical space-heating demand.</div>`;
    const points=(this.responseProfileData?.points||[]).flatMap((record)=>record.outdoor_mean_c==null||record.space_heating_kwh==null?[]:[{x:Number(record.outdoor_mean_c),y:Number(record.space_heating_kwh)/area,date:record.local_date,coverage:Number(record.temperature_coverage_percent||0),quality:record.quality,hp:Number(record.heat_pump_heating_kwh||0),ac:Number(record.ac_heating_kwh||0)}]);
    if(!points.length)return `<div class="empty">No complete daily record contains both validated representative outdoor temperature and reconciled space-heating electricity.</div>`;
    const bins=new Map();for(const point of points){const key=Math.floor(point.x/2)*2;const bucket=bins.get(key)||{x:0,y:0,hp:0,ac:0,n:0};bucket.x+=point.x;bucket.y+=point.y;bucket.hp+=point.hp;bucket.ac+=point.ac;bucket.n+=1;bins.set(key,bucket);}const series=[...bins.values()].map((bucket)=>({x:bucket.x/bucket.n,y:bucket.y/bucket.n,hp:bucket.hp/bucket.n,ac:bucket.ac/bucket.n,n:bucket.n})).sort((a,b)=>a.x-b.x);
    const xs=points.map((p)=>p.x),ys=points.map((p)=>p.y),xMean=xs.reduce((a,b)=>a+b,0)/xs.length,yMean=ys.reduce((a,b)=>a+b,0)/ys.length;const denominator=xs.reduce((sum,x)=>sum+(x-xMean)**2,0);const slope=denominator?xs.reduce((sum,x,index)=>sum+(x-xMean)*(ys[index]-yMean),0)/denominator:null;const intercept=slope==null?null:yMean-slope*xMean;const residual=slope==null?null:ys.reduce((sum,y,index)=>sum+(y-(intercept+slope*xs[index]))**2,0);const variance=ys.reduce((sum,y)=>sum+(y-yMean)**2,0);const r2=residual==null||variance===0?null:1-residual/variance;const base=slope!=null&&slope<0?-intercept/slope:null;const degreeDays=points.reduce((sum,p)=>sum+Math.max(0,18-p.x),0);
    const maxY=Math.max(.01,...series.map((p)=>p.y))*1.1,minX=Math.min(...series.map((p)=>p.x))-1,maxX=Math.max(...series.map((p)=>p.x))+1,px=(x)=>44+(x-minX)/Math.max(1,maxX-minX)*470,py=(y)=>178-y/maxY*142;
    const annualReady=this.historyCoverage?.complete&&["this_year","previous_year","current_season","previous_season"].includes(this.historyPreset);const annual=this.historySummary?.energy?.space_heating_kwh;
    return `<div class="response-evidence"><p><b>Source:</b> ${esc(source.display_name)} · <b>floor area:</b> ${n(area,0)} m² · complete daily points only</p><svg viewBox="0 0 550 215" class="chart" role="img" aria-label="Whole-house electrical space-heating response"><line x1="44" x2="514" y1="178" y2="178"/>${series.map((p)=>`<circle cx="${px(p.x)}" cy="${py(p.y)}" r="${Math.min(9,4+p.n)}"><title>${n(p.x)} °C · ${n(p.y,3)} kWh/m²/day · HP ${n(p.hp)} kWh/day · AC ${n(p.ac)} kWh/day · ${p.n} day(s)</title></circle>`).join("")}<text x="8" y="38">${n(maxY,3)}</text><text x="8" y="181">0</text><text x="44" y="205">${n(minX)} °C</text><text x="462" y="205">${n(maxX)} °C</text></svg><div class="mini-stats"><span>Model fit <b>${points.length>=5&&r2!=null?`R² ${n(r2,3)}`:"Insufficient data"}</b></span><span>Electrical balance point <b>${points.length>=5&&base!=null?n(base)+" °C":"Unavailable"}</b></span><span>Observed degree-days (18 °C) <b>${n(degreeDays,1)}</b></span><span>Annual/season electricity <b>${annualReady&&annual!=null?n(annual)+" kWh":"Incomplete period"}</b></span><span>Thermal output / COP <b>Unavailable</b></span><span>Design day <b>Not configured</b></span></div><p class="muted">Electrical demand only. Heat-pump and AC heating are combined from dynamically configured equipment. Thermal output and COP are never inferred from electricity.</p></div>`;
  }

  thermalView() {
    return `${this.periodControls()}<section class="card page-card"><h2><ha-icon icon="mdi:home-thermometer-outline"></ha-icon>Whole-House Electrical Heating Response</h2>${this.responseProfile()}</section><section class="grid equipment-charts"><article class="card"><h2>Measured thermal output</h2><div class="empty">Unavailable — no configured measured thermal-energy source. Electricity is not heat output.</div></article><article class="card"><h2>Evidence and uncertainty</h2><p class="muted">Only complete daily allocations and the selected representative outdoor sensor enter the response curve. Missing dates, partial device allocations and invalid temperatures remain excluded and visible through period quality.</p></article></section>`;
  }

  recentTable(records = this.history.slice(0, 7)) {
    if (!records.length) return `<div class="empty">No daily records yet. The first close runs at 05:00 local time.</div>`;
    return `<div class="table-wrap"><table><thead><tr><th>Date</th><th>Whole house</th><th>HP confirmed</th><th>HP assumed</th><th>AC confirmed</th><th>AC selected/estimated</th><th>Total heating</th><th>DHW</th><th>Heating + DHW</th><th>Cooling</th><th>HVAC idle</th><th>Other HVAC</th><th>Other household</th><th>Unallocated</th><th>Discrepancy (signed)</th><th>PV</th><th>Import</th><th>Export</th><th>Self-sufficiency</th><th>Quality</th></tr></thead><tbody>
      ${records.map((r) => `<tr><td>${esc(r.local_date)}</td><td>${n(r.energy?.household_kwh)} kWh</td>
        <td>${n(r.energy?.confirmed_heat_pump_heating_kwh)} kWh</td><td>${n(r.energy?.assumed_heat_pump_heating_kwh)} kWh</td>
        <td>${n(r.energy?.confirmed_ac_heating_kwh)} kWh</td><td>${n(addNullable(r.energy?.selected_ac_heating_kwh,r.energy?.estimated_ac_heating_kwh))} kWh</td>
        <td>${n(r.energy?.space_heating_kwh)} kWh</td><td>${n(r.energy?.dhw_kwh)} kWh</td><td>${n(r.energy?.heating_and_dhw_kwh)} kWh</td><td>${n(r.energy?.cooling_kwh)} kWh</td>
        <td>${n(r.energy?.idle_kwh)} kWh</td><td>${n(r.energy?.other_hvac_kwh)} kWh</td><td>${n(r.energy?.other_household_kwh)} kWh</td><td>${n(r.energy?.unknown_kwh)} kWh</td><td>${n(r.energy?.unallocated_discrepancy_kwh)} kWh</td>
        <td>${n(r.energy?.pv_kwh)} kWh</td><td>${n(r.energy?.import_kwh)} kWh</td><td>${n(r.energy?.export_kwh)} kWh</td><td>${n(r.energy?.self_sufficiency_percent)}%</td><td>${this.quality(r)}</td></tr>`).join("")}
    </tbody></table></div>`;
  }

  overview() {
    const selected = this.selectedRecord();
    const isToday = this.historyPreset === "today";
    const comparison = isToday ? this.overviewData?.yesterday_same_cutoff?.energy : null;
    const full = isToday ? this.overviewData?.yesterday_full?.energy : null;
    const hourlyCoverage=this.overviewData?.today_interval_coverage;
    const status = isToday ? (this.overviewData?.stale ? `Partial/stale — ${this.refreshError || "hourly coverage incomplete"}` : "Actual through latest completed hour") : "Validated selected-period summary";
    const metric = (label, object) => label === "Whole-House Electricity" ? object?.household_kwh : label === "Heating Electricity" ? object?.space_heating_kwh : label === "DHW Electricity" ? object?.dhw_kwh : label === "Heating + DHW" ? object?.heating_and_dhw_kwh : label === "Cooling Electricity" ? object?.cooling_kwh : label === "PV Generation" ? object?.pv_kwh : object?.self_sufficiency_percent;
    return `${this.periodControls()}<div class="toolbar"><div><b>${esc(this.selectedPeriod?.label || "Today")} <span class="quality ${this.historyCoverage?.complete?"good":"partial"}">${this.historyCoverage?.complete?"Complete":"Partial"}</span></b><span>${esc(status)}${isToday?` · ${n(hourlyCoverage?.coverage_percent,0)}% hourly coverage · cutoff ${esc(this.overviewData?.cutoff_local || "—")} (${esc(this.overviewData?.timezone || "HA timezone")})`:""} · updated ${esc(selected?.updated_at || "—")}</span>${isToday&&hourlyCoverage?.missing_intervals?.length?`<span class="warning">Missing: ${hourlyCoverage.missing_intervals.map((item)=>esc(item.start)).join(", ")}</span>`:""}</div><button class="refresh">Refresh now</button></div>
    <section class="kpis">${this.kpis().map(([icon, label, value, color, unit = "kWh", known]) => `
      <article class="card kpi"><div class="kpi-icon ${color}"><ha-icon icon="${icon}"></ha-icon></div>
      <div><span>${label}</span><strong>${n(value)} ${value == null ? "" : unit}</strong>
      ${value==null&&known!=null?`<small>Known partial subtotal: ${n(known)} ${unit}</small>`:""}<small>${isToday?this.comparisonText(value, metric(label, comparison), metric(label, full), unit):`Selected period · ${esc(this.selectedPeriod?.start_date || "—")}–${esc(this.selectedPeriod?.end_date || "—")}`}</small></div></article>`).join("")}</section>
    <section class="grid top-grid">
      <article class="card wide"><h2><ha-icon icon="mdi:home-thermometer-outline"></ha-icon>Heating Energy vs Outdoor Temperature</h2>${this.scatterChart()}</article>
      <article class="card"><h2><ha-icon icon="mdi:chart-box-outline"></ha-icon>Daily Energy Split</h2>${this.splitChart()}</article>
      <article class="card status-card"><h2><ha-icon icon="mdi:gauge"></ha-icon>COP & Efficiency</h2><div class="upcoming"><ha-icon icon="mdi:timeline-clock-outline"></ha-icon><b>Planned for V0.2</b><span>No thermal output is inferred from electricity.</span></div></article>
    </section>
    <section class="grid bottom-grid">
      ${this.temperatureCard("indoor")}${this.temperatureCard("outdoor")}
      <article class="card"><h2><ha-icon icon="mdi:solar-power"></ha-icon>Solar PV</h2><div class="mini-stats"><span>On-site use <b>${n(selected?.energy?.pv_consumed_on_site_kwh)} kWh</b></span><span>Self-consumption <b>${n(selected?.energy?.pv_self_consumption_percent)}%</b></span><span>Export <b>${n(selected?.energy?.export_kwh)} kWh</b></span></div><p class="muted">Unavailable inputs remain unavailable.</p></article>
      <article class="card wide"><h2><ha-icon icon="mdi:clock-outline"></ha-icon>Recent History</h2>${this.recentTable(this.history.slice(0, 5))}</article>
    </section><section class="card page-card"><h2>Equipment breakdown</h2>${this.equipmentBreakdown()}</section>`;
  }

  equipmentBreakdown() {
    const rows = this.selectedRecord()?.energy?.equipment || [];
    if (!rows.length) return `<div class="empty">No enabled equipment data for this period.</div>`;
    return `<div class="table-wrap"><table><thead><tr><th>Equipment</th><th>Meter</th><th>Confirmed heating</th><th>Assumed/selected/estimated</th><th>DHW</th><th>Cooling confirmed</th><th>Cooling selected</th><th>Cooling estimated</th><th>Idle</th><th>Other HVAC</th><th>Residual</th><th>Coverage</th><th>Evidence</th><th>Quality</th></tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(r.display_name)}</td><td>${n(r.meter_kwh)} kWh</td><td>${n(r.confirmed_heating_kwh)} kWh</td><td>${n(addNullable(r.assumed_heating_kwh,r.selected_heating_kwh,r.estimated_heating_kwh))} kWh</td><td>${n(r.dhw_kwh)} kWh</td><td>${n(r.confirmed_cooling_kwh)} kWh</td><td>${n(r.selected_cooling_kwh)} kWh</td><td>${n(r.estimated_cooling_kwh)} kWh</td><td>${n(r.idle_kwh)} kWh</td><td>${n(r.other_hvac_kwh)} kWh</td><td>${n(r.residual_kwh)} kWh</td><td>${r.coverage_percent==null?"Unavailable":n(r.coverage_percent,0)+"%"}</td><td>${esc(r.classification_source||r.source||"Unavailable")}</td><td>${this.quality(r)}</td></tr>`).join("")}</tbody></table></div><div class="equipment-charts"><article><h3>Heating by equipment</h3>${this.equipmentComparison("heating")}</article><article><h3>Cooling by equipment</h3>${this.equipmentComparison("cooling")}</article></div>`;
  }

  equipmentComparison(category) {
    const rows=this.selectedRecord()?.energy?.equipment || [];
    const available=rows.filter((row)=>row.available);
    const values=available.map((row)=>category==="heating"?(row.confirmed_heating_kwh||0)+(row.assumed_heating_kwh||0)+(row.selected_heating_kwh||0)+(row.estimated_heating_kwh||0):(row.cooling_kwh||0));
    const max=Math.max(0,...values);
    if(!available.length||max===0)return `<p class="muted">No validated ${category} allocation.${rows.some((row)=>!row.available)?" Some equipment is unavailable.":""}</p>`;
    return `<div class="device-bars">${available.map((row,index)=>`<div><span>${esc(row.display_name)} ${this.quality(row)}</span><i><b class="${category==='heating'?'heat':'cool'}" style="width:${100*values[index]/max}%" title="${n(values[index])} kWh · ${esc(row.quality)}"></b></i><strong>${n(values[index])} kWh</strong></div>`).join("")}</div>${rows.some((row)=>!row.available)?'<p class="warning">Unavailable equipment is excluded; total remains partial.</p>':""}`;
  }

  temperatureCard(role) {
    const source = this.representativeSource(role);
    const label = role === "indoor" ? "Indoor" : "Outdoor";
    if (!source) return `<article class="card temperature-card"><h2><ha-icon icon="${role === "indoor" ? "mdi:home-thermometer" : "mdi:thermometer-lines"}"></ha-icon>Representative ${label} Temperature</h2><div class="not-configured">Not configured</div><p class="muted">Select one ${role === "indoor" ? "indoor location" : "measured outdoor-air sensor"} in Settings → Devices & services → Home Energy Intelligence → Configure. No coverage percentage is inferred.</p></article>`;
    const today = source ? this.selectedRecord()?.temperatures?.[source.source_id] : null;
    const yesterday = source ? this.overviewData?.yesterday_full?.temperatures?.[source.source_id] : null;
    const explanation = role === "indoor" ? "Selected location; not a whole-house mean." : "Selected measured source; not weather-provider data.";
    return `<article class="card temperature-card"><h2><ha-icon icon="${role === "indoor" ? "mdi:home-thermometer" : "mdi:thermometer-lines"}"></ha-icon>Representative ${label} Temperature</h2>
      <div class="temperature">${n(today?.latest_value)}${today?.latest_value == null ? "" : " °C"}</div>
      <p class="muted">Latest valid: ${esc(today?.latest_timestamp || "unavailable")} · ${esc(explanation)}</p>
      <div class="mini-stats"><span>Period mean <b>${n(today?.mean)} °C</b></span>${this.historyPreset==="today"?`<span>Yesterday mean <b>${n(yesterday?.mean)} °C</b></span>`:""}<span>Min / max <b>${n(today?.minimum)} / ${n(today?.maximum)} °C</b></span><span>Coverage <b>${n(today?.coverage_percent,0)}%</b></span><span>Quality <b>${esc(today?.quality || "missing")}</b></span></div>${this.historyPreset === "today" ? this.temperatureTimeline(role) : this.historyTemperatureTimeline(role)}</article>`;
  }

  comparisonText(today, same, full, unit) {
    if (today == null || same == null) return `Yesterday same cutoff unavailable · full ${n(full)} ${full == null ? "" : unit}`;
    const difference = today-same; const percent = same>0 ? 100*difference/same : null;
    return `Yesterday same cutoff ${n(same)} ${unit} · Δ ${difference>=0?"+":""}${n(difference)} ${unit}${percent==null?"":` (${percent>=0?"+":""}${n(percent)}%)`} · full ${n(full)} ${full==null?"":unit}`;
  }

  temperatureTimeline(role = null) {
    const timeline = this.overviewData?.temperature_timeline || [];
    const representative = role ? this.representativeSource(role) : null;
    const sources = role ? (representative ? [representative] : [])
      : this.temperatureSources.filter((source) => ["indoor", "outdoor", "supply", "return"].includes(source.role));
    const values = timeline.flatMap((row) => sources.map((source) => row.temperatures?.[source.source_id]?.mean).filter((value) => value != null));
    if (!timeline.length || !values.length) return `<p class="muted">No validated hourly temperature timeline yet.</p>`;
    const low = Math.min(...values), high = Math.max(...values), span = Math.max(1, high-low);
    const lines = sources.map((source, index) => {
      const segments=[]; let points=[];
      timeline.forEach((row, x) => { const value=row.temperatures?.[source.source_id]?.mean; if(value==null){if(points.length){segments.push(points);points=[];}}else points.push(`${10+x*280/Math.max(1,timeline.length-1)},${75-(value-low)*65/span}`); });
      if(points.length) segments.push(points);
      return segments.map((segment) => `<polyline points="${segment.join(" ")}" class="temp-line t${index%4}"><title>${esc(source.display_name)}</title></polyline>`).join("");
    }).join("");
    return `<svg class="temp-chart" viewBox="0 0 300 95" role="img" aria-label="Validated hourly temperature timelines">${lines}<text x="10" y="92">${esc(timeline[0]?.start?.slice(11,16))}</text><text x="260" y="92">${esc(timeline.at(-1)?.end?.slice(11,16))}</text></svg>`;
  }

  roadmap(title, version, text) {
    return `<section class="roadmap card"><ha-icon icon="mdi:timeline-clock-outline"></ha-icon><h2>${title}</h2><b>${version}</b><p>${text}</p><p>No illustrative values are shown as measurements.</p></section>`;
  }

  historyView() {
    return `<section class="card page-card"><div class="section-head"><div><h2>Validated Energy History</h2><p>Canonical actuals, explicit uncertainty, bounded pagination and CSV export.</p></div><div><button class="csv">Export CSV</button> <button class="refresh">Refresh</button></div></div>
      ${this.periodControls()}
      ${this.periodSummary()}
      <article class="card pv-history"><h2>PV and grid history</h2>${this.pvHistoryChart()}</article>
      <div class="grid temperature-history"><article class="card"><h2>Indoor temperature history</h2>${this.historyTemperatureTimeline("indoor")}</article><article class="card"><h2>Outdoor temperature history</h2>${this.historyTemperatureTimeline("outdoor")}</article></div>
      ${this.recentTable(this.history)}<div class="pager"><button class="previous" ${this.historyOffset===0?"disabled":""}>Previous</button><span>Showing ${this.history.length?this.historyOffset+1:0}–${this.historyOffset+this.history.length} of ${this.historyTotal}</span><button class="next" ${this.historyOffset+this.history.length>=this.historyTotal?"disabled":""}>Next</button></div></section>`;
  }

  periodOptions() {
    const options = [["today","Today"],["yesterday","Yesterday"],["day_2","−2 days"],["day_3","−3 days"],["day_4","−4 days"],["day_5","−5 days"],["day_6","−6 days"],["day_7","−7 days"],["this_week","This week"],["previous_week","Previous week"],["last_7_completed","Last 7 completed days"],["this_month","This month"],["previous_month","Previous month"],["older_month","Older month"],["this_year","This year"],["previous_year","Previous year"],["current_season","Current energy season"],["previous_season","Previous energy season"],["custom","Custom range"]];
    return options.map(([value,label])=>`<option value="${value}" ${this.historyPreset===value?"selected":""}>${label}</option>`).join("");
  }

  periodControls() {
    return `<div class="period"><label>Period <select class="period-select">${this.periodOptions()}</select></label><label class="conditional ${this.historyPreset==="older_month"?"visible":""}">Older month <input class="older-month" type="month" value="${esc(this.historyOlderMonth || "")}"></label><label class="conditional ${this.historyPreset==="custom"?"visible":""}">From <input class="start-date" type="date" value="${esc(this.historyStart || "")}"></label><label class="conditional ${this.historyPreset==="custom"?"visible":""}">To <input class="end-date" type="date" value="${esc(this.historyEnd || "")}"></label><button class="apply-period">Apply</button></div>`;
  }

  historyTemperatureTimeline(role) {
    const source = this.representativeSource(role);
    if (!source) return `<div class="empty">No representative source configured.</div>`;
    const records=[...this.history].reverse();
    const values=records.map((record) => record.temperatures?.[source.source_id]?.mean);
    const valid=values.filter((value) => value!=null);
    if(!valid.length) return `<div class="empty">No validated measurements in this period.</div>`;
    const low=Math.min(...valid), high=Math.max(...valid), span=Math.max(1,high-low); const segments=[]; let points=[]; const firstDay=Date.parse(records[0].local_date+"T00:00:00Z"),lastDay=Date.parse(records.at(-1).local_date+"T00:00:00Z"),daySpan=Math.max(1,Math.round((lastDay-firstDay)/86400000)); let priorDay=null;
    values.forEach((value,index)=>{const day=Math.round((Date.parse(records[index].local_date+"T00:00:00Z")-firstDay)/86400000);if(value==null||(priorDay!=null&&day-priorDay>1)){if(points.length){segments.push(points);points=[];}}if(value!=null){points.push(`${10+day*280/daySpan},${75-(value-low)*65/span}`);priorDay=day;}}); if(points.length)segments.push(points);
    const markers=records.map((record)=>{const item=record.temperatures?.[source.source_id];if(item?.mean==null)return "";const day=Math.round((Date.parse(record.local_date+"T00:00:00Z")-firstDay)/86400000),x=10+day*280/daySpan,y=75-(item.mean-low)*65/span;return `<circle cx="${x}" cy="${y}" r="2.5" class="t${role==='indoor'?0:1}"><title>${esc(record.local_date)} · mean ${n(item.mean)} °C · min ${n(item.minimum)} · max ${n(item.maximum)} · coverage ${n(item.coverage_percent,0)}% · ${esc(item.quality)}</title></circle>`;}).join("");
    return `<svg class="temp-chart" viewBox="0 0 300 95" role="img" aria-label="${esc(role)} daily time-weighted temperature means">${segments.map((segment)=>`<polyline points="${segment.join(" ")}" class="temp-line t${role==="indoor"?0:1}"></polyline>`).join("")}${markers}<text x="10" y="92">${esc(records[0]?.local_date || "")}</text><text x="235" y="92">${esc(records.at(-1)?.local_date || "")}</text></svg><p class="muted">Daily means are elapsed-time weighted; hover for min/max and coverage. Missing days are gaps. Period mean ${n(this.historySummary?.temperatures?.[source.source_id]?.mean)} °C.</p>`;
  }

  pvHistoryChart() {
    const records=[...this.history].reverse();
    const series=[["PV","pv_kwh","pv"],["Import","import_kwh","grid-import"],["Export","export_kwh","grid-export"],["PV self-use","pv_consumed_on_site_kwh","self-use"],["Household","household_kwh","household"]];
    const values=records.flatMap((record)=>series.map(([,key])=>record.energy?.[key]).filter((value)=>value!=null));
    if(!values.length)return `<div class="empty">PV/grid sources are unavailable for this period.</div>`;
    const max=Math.max(1,...values),firstDay=Date.parse(records[0].local_date+"T00:00:00Z"),lastDay=Date.parse(records.at(-1).local_date+"T00:00:00Z"),daySpan=Math.max(1,Math.round((lastDay-firstDay)/86400000)),width=Math.max(2,240/Math.max(1,daySpan+1));
    return `<svg class="pv-chart" viewBox="0 0 300 120" role="img" aria-label="Daily measured PV, import, export, self-use and household electricity">${records.map((record)=>{const day=Math.round((Date.parse(record.local_date+"T00:00:00Z")-firstDay)/86400000);return series.map(([label,key,css],seriesIndex)=>{const value=record.energy?.[key];if(value==null)return "";return `<rect class="${css}" x="${10+day*280/daySpan}" y="${100-80*value/max}" width="${width/series.length}" height="${80*value/max}" transform="translate(${seriesIndex*width/series.length} 0)"><title>${esc(record.local_date)} · ${label}: ${n(value)} kWh · ${esc(record.quality)}</title></rect>`;}).join("");}).join("")}</svg><div class="legend">${series.map(([label,,css])=>`<i class="dot ${css}"></i>${label}`).join(" ")}<span>Self-sufficiency ${n(this.historySummary?.energy?.self_sufficiency_percent)}% · PV self-consumption ${n(this.historySummary?.energy?.pv_self_consumption_percent)}%</span></div>`;
  }

  periodSummary() {
    const energy = this.historySummary?.energy;
    const coverage=this.historyCoverage || this.historySummary?.period_coverage;
    const dates=coverage?.date_coverage; const meter=coverage?.meter_coverage; const allocation=coverage?.allocation_coverage;
    const representativeCoverage=(role)=>{const source=this.representativeSource(role);if(!source)return "Not configured";const item=coverage?.temperature_coverage?.[source.source_id];return item?`${n(item.coverage_percent,0)}%` : "Unavailable";};
    const heading=`<p><b>${esc(this.selectedPeriod?.label || "Selected period")}</b> · ${esc(this.selectedPeriod?.start_date || "—")}–${esc(this.selectedPeriod?.end_date || "—")} · dates ${n(dates?.coverage_percent ?? coverage?.coverage_percent,0)}% · meter ${n(meter?.coverage_percent,0)}% · allocation ${n(allocation?.coverage_percent,0)}% · indoor ${representativeCoverage("indoor")} · outdoor ${representativeCoverage("outdoor")} ${coverage?.complete?'<span class="quality good">Complete</span>':'<span class="quality partial">Partial</span>'}</p>`;
    if(!energy)return `${heading}<div class="empty">No complete daily records in the selected period.</div>`;
    return `${heading}<div class="mini-stats period-summary"><span>Household <b>${n(energy.household_kwh)} kWh</b><small>Known ${n(this.historySummary?.known_household_kwh)} kWh</small></span><span>Heating <b>${n(energy.space_heating_kwh)} kWh</b><small>Known ${n(energy.known_space_heating_kwh)} kWh</small></span><span>DHW <b>${n(energy.dhw_kwh)} kWh</b></span><span>Heating + DHW <b>${n(energy.heating_and_dhw_kwh)} kWh</b></span><span>Cooling <b>${n(energy.cooling_kwh)} kWh</b></span><span>PV <b>${n(energy.pv_kwh)} kWh</b></span><span>Import / export <b>${n(energy.import_kwh)} / ${n(energy.export_kwh)} kWh</b></span></div>${coverage?.missing_dates?.length?`<p class="warning">Missing dates: ${coverage.missing_dates.map(esc).join(", ")}</p>`:""}`;
  }

  settingsView() {
    if (!this.settings) return `<section class="card page-card"><h2>Administrator access required</h2><p>Overview and History remain available. Integration mappings and database details are restricted to administrators.</p></section>`;
    if (!this.settings.configured) return `<section class="card page-card"><h2>Setup required</h2><p>Add the integration from Settings → Devices & services.</p></section>`;
    const representative=(role)=>{const source=this.representativeSource(role);const item=source?this.selectedRecord()?.temperatures?.[source.source_id]:null;return source?`${esc(source.display_name)} · ${n(item?.latest_value)}${item?.latest_value==null?"":" °C"} · ${esc(item?.quality||"unavailable")} · ${esc(item?.latest_timestamp||"no validated timestamp")}`:"Not configured";};
    return `<section class="settings-grid">
      <article class="card page-card"><h2>Processing</h2><dl><dt>Time zone</dt><dd>${esc(this.settings.timezone)}</dd><dt>Cycle</dt><dd>${esc(this.settings.cycle_hours)} hours</dd><dt>Floor area</dt><dd>${n(this.settings.floor_area_m2, 0)} m²</dd><dt>Database</dt><dd>${n(this.settings.database_size_bytes / 1024, 0)} KiB</dd><dt>InfluxDB backfill</dt><dd>${this.settings.influx_enabled ? "Enabled (read-only)" : "Disabled"}</dd></dl>
      <a class="primary" href="/config/integrations/integration/home_energy_intelligence">Open integration options</a></article>
      <article class="card page-card"><h2>Entity mapping</h2><div class="mapping">${this.settings.mapping.map((row) => `<div><span>${esc(row.label)}</span><code>${row.entity_id ? esc(row.entity_id) : "Not selected"}</code></div>`).join("")}</div></article>
      <article class="card page-card notice"><h2>Representative temperatures</h2><p><b>Indoor:</b> ${representative("indoor")}</p><p><b>Measured outdoor:</b> ${representative("outdoor")}</p><p class="muted">Selections are independent. A weather provider or water-circuit sensor is never substituted.</p></article>
       <article class="card page-card notice"><h2>Data guarantees</h2><p>Recorder is read-only. Unknown heat-pump operation is reported as assumed heating, idle remains separate, gaps remain gaps, and unrelated circuit sensors are never substituted.</p></article>
    </section>`;
  }

  content() {
    if (this.loading) return `<div class="loading"><ha-circular-progress active></ha-circular-progress><span>Loading validated records…</span></div>`;
    if (this.error) return `<div class="error">${esc(this.error)}</div>`;
    if (this.active === "overview") return this.overview();
    if (this.active === "history") return this.historyView();
    if (this.active === "settings") return this.settingsView();
    if (this.active === "thermal") return this.thermalView();
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
    this.shadowRoot.querySelector(".apply-period")?.addEventListener("click", () => { this.historyOffset=0; this.loadPeriod(); });
    this.shadowRoot.querySelector(".period-select")?.addEventListener("change", (event) => { this.historyPreset=event.target.value; this.render(); });
    this.shadowRoot.querySelector(".next")?.addEventListener("click", () => { this.historyOffset += 90; this.loadPeriod(); });
    this.shadowRoot.querySelector(".previous")?.addEventListener("click", () => { this.historyOffset = Math.max(0, this.historyOffset - 90); this.loadPeriod(); });
  }

  async loadPeriod() {
    this.historyPreset = this.shadowRoot.querySelector(".period-select")?.value || "today";
    this.historyStart = this.shadowRoot.querySelector(".start-date")?.value || null;
    this.historyEnd = this.shadowRoot.querySelector(".end-date")?.value || null;
    this.historyOlderMonth = this.shadowRoot.querySelector(".older-month")?.value || null;
    try {
      const response = await this._hass.callWS(this.historyRequest());
      this.history = response.records || []; this.historySummary = response.summary;
      this.historyTotal = response.total || 0;
      this.historyCoverage = response.period_coverage;
      this.responseProfileData = response.response_profile;
      this.selectedPeriod = response.selected_period; this.refreshError=null; this.render();
    } catch(error) { this.refreshError=error?.message || String(error); this.render(); }
  }

  async refreshNow() {
    try { await this._hass.callWS({type:"home_energy_intelligence/refresh"}); this.refreshError=null; await this.load(); }
    catch(error) { this.refreshError=error?.message || String(error); if(this.overviewData)this.overviewData.stale=true; this.loading=false; this.render(); }
  }

  async exportCsv() {
    const response = await this._hass.callWS({type:"home_energy_intelligence/history_csv",
      ...(this.selectedPeriod?.start_date?{start_date:this.selectedPeriod.start_date}:{}),
      ...(this.selectedPeriod?.end_date?{end_date:this.selectedPeriod.end_date}:{}),
      ...(this.historyPreset==="today"&&this.overviewData?.cutoff_utc?{cutoff_utc:this.overviewData.cutoff_utc}:{})});
    const link = document.createElement("a"); link.href = URL.createObjectURL(new Blob([response.csv], {type:"text/csv"})); link.download = response.filename; link.click(); URL.revokeObjectURL(link.href);
  }

  static styles = `
    :host{display:block;background:var(--primary-background-color,#f4f7fb);color:var(--primary-text-color,#17233b);min-height:100%;font-family:var(--paper-font-body1_-_font-family,Inter,system-ui,sans-serif)}
    *{box-sizing:border-box} main{max-width:1600px;margin:auto;padding:24px 26px 40px} header{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:14px}.brand{display:flex;align-items:center;gap:18px}.brand>ha-icon{--mdc-icon-size:62px;color:#0984d8}.brand h1{font-size:34px;line-height:1;margin:0;color:var(--primary-text-color,#111827)}.brand p{margin:6px 0 0;font-size:18px;color:var(--secondary-text-color,#65728a)}.connection{display:flex;align-items:center;gap:10px}.connection i{width:12px;height:12px;border-radius:50%;background:#20a35a}.connection div{display:flex;flex-direction:column}.connection span{font-size:12px;color:var(--secondary-text-color,#65728a);margin-top:4px}
    .tabs{display:flex;gap:20px;border-bottom:1px solid var(--divider-color,#dce4ee);overflow-x:auto}.tab{display:flex;align-items:center;gap:10px;border:0;border-bottom:3px solid transparent;background:none;color:var(--secondary-text-color,#41506a);padding:18px 14px;font-size:15px;white-space:nowrap;cursor:pointer}.tab.active{color:var(--primary-color,#0785db);border-bottom-color:var(--primary-color,#0785db);font-weight:700}.content{padding-top:22px}.toolbar,.period,.pager{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.toolbar>div{display:flex;flex-direction:column}.toolbar span{font-size:12px;color:var(--secondary-text-color,#65728a)}.period{justify-content:flex-start;flex-wrap:wrap;padding:12px;background:var(--secondary-background-color,#f2f6fa);border-radius:8px}.period label{display:flex;gap:6px;align-items:center}.card{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#e0e7ef);border-radius:12px;box-shadow:0 3px 12px rgba(35,65,100,.05);padding:18px;min-width:0}.card h2{display:flex;align-items:center;gap:10px;font-size:17px;margin:0 0 16px}.card h2 ha-icon{color:#233b5b}.kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-bottom:18px}.kpi{display:flex;gap:13px;align-items:flex-start}.kpi-icon{display:grid;place-items:center;width:48px;height:48px;border-radius:50%;flex:none}.kpi-icon.blue{color:#0785db;background:#e7f4fe}.kpi-icon.red{color:#ef4444;background:#fff0f0}.kpi-icon.orange{color:#f59e0b;background:#fff6e5}.kpi-icon.amber{color:#f6a800;background:#fff8df}.kpi-icon.green{color:#159b5b;background:#eaf9f1}.kpi-icon ha-icon{--mdc-icon-size:27px}.kpi>div:last-child{display:flex;flex-direction:column;min-width:0}.kpi span{font-size:14px;line-height:1.3}.kpi strong{font-size:24px;margin-top:3px;white-space:nowrap}.kpi small{color:var(--secondary-text-color,#65728a);margin-top:7px}.grid{display:grid;gap:14px;margin-bottom:14px}.top-grid{grid-template-columns:1.3fr 1fr .85fr}.bottom-grid{grid-template-columns:1fr .95fr 1.35fr}.wide{min-width:0}.chart{width:100%;height:220px}.chart line{stroke:var(--divider-color,#dbe4ee);stroke-width:1}.chart text{font-size:11px;fill:var(--secondary-text-color,#66758d)}.chart circle{fill:#46a4ef;stroke:#fff;stroke-width:2}.temp-chart{width:100%;height:85px;margin-top:10px}.temp-line{fill:none;stroke-width:2}.temp-line.t0{stroke:#ef4444}.temp-line.t1{stroke:#238ee8}.temp-line.t2{stroke:#f59e0b}.temp-line.t3{stroke:#159b5b}.bars{height:185px;border-bottom:1px solid var(--divider-color,#dbe4ee);display:flex;align-items:flex-end;justify-content:space-around;gap:10px;padding:10px 10px 0}.bar-col{height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:6px;font-size:11px;color:var(--secondary-text-color,#65728a)}.bar{width:26px;display:flex;flex-direction:column;justify-content:flex-end;border-radius:3px 3px 0 0;overflow:hidden;background:#eef2f6}.seg{display:block;width:100%}.heat{background:#ef4444}.dhw{background:#f59e0b}.cool{background:#238ee8}.idle{background:#8b9bb0}.other{background:#91c788}.unknown{background:#8b5cf6}.legend{display:flex;justify-content:center;align-items:center;gap:7px;font-size:12px;margin-top:12px;flex-wrap:wrap}.dot{width:10px;height:10px;border-radius:2px;margin-left:8px}.upcoming{min-height:150px;border-radius:10px;background:linear-gradient(145deg,#f1f8ff,#e9f4fc);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:8px;color:#2269a5;padding:18px}.upcoming span{font-size:13px;color:#496980}.temperature{font-size:34px;font-weight:700;margin:12px 0}.muted{color:var(--secondary-text-color,#65728a);font-size:13px}.mini-stats{display:flex;gap:20px;border-top:1px solid var(--divider-color,#e0e7ef);padding-top:14px}.mini-stats span{display:flex;flex-direction:column;color:var(--secondary-text-color,#65728a);font-size:12px}.mini-stats b{color:var(--primary-text-color,#17233b);margin-top:4px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}th{background:var(--secondary-background-color,#f2f6fa);text-align:left}th,td{padding:9px 8px;border-bottom:1px solid var(--divider-color,#e4eaf0);white-space:nowrap}.quality{display:inline-flex;padding:3px 7px;border-radius:12px;background:#eef2f6;text-transform:capitalize}.quality.good{background:#e5f7ed;color:#08743d}.quality.partial,.quality.filtered{background:#fff4d6;color:#8a5900}.quality.missing,.quality.unreconciled{background:#ffe9e8;color:#a82e28}.empty{min-height:150px;display:grid;place-items:center;text-align:center;color:var(--secondary-text-color,#65728a);padding:20px}.status-card{min-width:0}.roadmap{max-width:720px;margin:50px auto;text-align:center;padding:54px}.roadmap>ha-icon{--mdc-icon-size:64px;color:var(--primary-color,#0785db)}.roadmap h2{display:block;font-size:28px;margin-top:18px}.roadmap b{color:var(--primary-color,#0785db)}.roadmap p{color:var(--secondary-text-color,#65728a)}.page-card{padding:24px}.section-head{display:flex;justify-content:space-between;align-items:center}.section-head p{margin:4px 0 18px;color:var(--secondary-text-color,#65728a)}button.refresh,button.csv,button.apply-period,button.previous,button.next,.primary{border:0;border-radius:8px;background:var(--primary-color,#0785db);color:#fff;padding:10px 15px;text-decoration:none;cursor:pointer}.settings-grid{display:grid;grid-template-columns:1fr 1.6fr;gap:16px}.notice{grid-column:1/-1}dl{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:24px}dt{color:var(--secondary-text-color,#65728a)}dd{margin:0;text-align:right;font-weight:600}.mapping{display:grid;gap:1px;background:var(--divider-color,#e0e7ef);border:1px solid var(--divider-color,#e0e7ef);border-radius:8px;overflow:hidden}.mapping>div{display:grid;grid-template-columns:1fr 1.25fr;gap:12px;background:var(--card-background-color,#fff);padding:10px}.mapping code{color:var(--secondary-text-color,#65728a);overflow-wrap:anywhere}.loading{display:grid;place-items:center;gap:12px;padding:100px}.error{padding:18px;border-radius:10px;background:#ffe9e8;color:#a82e28}
    .other-hvac{background:#14b8a6}.missing{color:#a82e28;background:#ffe9e8}.not-configured{font-size:24px;font-weight:700;color:var(--secondary-text-color,#65728a);margin:22px 0}.warning,.discrepancy{color:#b45309}.equipment-charts{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px}.device-bars>div{display:grid;grid-template-columns:minmax(120px,1fr) 2fr 65px;gap:10px;align-items:center;margin:8px 0}.device-bars i{height:12px;background:#eef2f6;border-radius:6px;overflow:hidden}.device-bars i b{display:block;height:100%}.pv-chart{width:100%;height:220px}.pv-chart .pv{fill:#f6a800}.pv-chart .grid-import{fill:#238ee8}.pv-chart .grid-export{fill:#8b5cf6}.pv-chart .self-use{fill:#159b5b}.pv-chart .household{fill:#8b9bb0}
    @media(max-width:1200px){.kpis{grid-template-columns:repeat(3,1fr)}.top-grid,.bottom-grid{grid-template-columns:1fr 1fr}.status-card,.bottom-grid .wide{grid-column:1/-1}}
    .period .conditional{display:none}.period .conditional.visible{display:flex}button:disabled{opacity:.45;cursor:not-allowed}
    @media(max-width:760px){main{padding:12px 9px}.brand>ha-icon{--mdc-icon-size:42px}.brand h1{font-size:23px}.brand p{font-size:13px}.connection{display:none}.tabs{gap:2px}.tab{padding:13px 11px;min-height:44px}.tab span{display:none}.kpis{grid-template-columns:1fr}.top-grid,.bottom-grid,.settings-grid,.equipment-charts{grid-template-columns:1fr}.status-card,.bottom-grid .wide,.notice{grid-column:auto}.section-head{align-items:flex-start}.mapping>div{grid-template-columns:1fr}.mini-stats{flex-wrap:wrap}.device-bars>div{grid-template-columns:1fr}.period{align-items:stretch;flex-direction:column}.period label{justify-content:space-between}.period input,.period select,.period button,.toolbar button,.pager button{min-height:44px}.card{padding:14px}.chart,.pv-chart{height:180px}.empty,.upcoming{min-height:72px}.table-wrap{max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}.table-wrap th:first-child,.table-wrap td:first-child{position:sticky;left:0;background:var(--card-background-color,#fff);z-index:1}.table-wrap th:first-child{background:var(--secondary-background-color,#f2f6fa)}.pager{flex-wrap:wrap}.pager span{order:-1;width:100%;text-align:center}}
  `;
}

customElements.define("home-energy-intelligence-panel", HomeEnergyIntelligencePanel);
