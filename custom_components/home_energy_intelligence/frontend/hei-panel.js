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
    this.temperatureDiagnostics = null;
    this.temperatureRepairResult = null;
    this.temperatureBackfillJob = null;
    this.temperatureBackfillPolling = null;
    this.temperatureTimelineData = null;
    this.temperatureTimelineError = null;
    this.v02Report = null;
    this.thermalLedger = null;
    this.thermalEdit = null;
    this.monthlyReport = null;
    this.monthlyLedger = null;
    this.monthlyEdit = null;
    this.monthlyEndMonth = null;
    this.monthlyMetricIds = null;
    this.temperatureChartSequence = 0;
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
  disconnectedCallback() { if (this._poll) window.clearInterval(this._poll); if(this.temperatureBackfillPolling)window.clearTimeout(this.temperatureBackfillPolling); }

  async load(showLoading = true) {
    if (showLoading) this.loading = true;
    this.error = null;
    this.render();
    try {
      const overview = await this._hass.callWS({ type: "home_energy_intelligence/overview" });
      this.overviewData = overview;
      this.monthlyEndMonth ||= String(overview.cutoff_local || overview.latest?.local_date || "").slice(0,7) || null;
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
      await this.loadTemperatureTimeline(history, overview, false);
      await this.loadV02(history, false);
    } catch (error) {
      if (this.overviewData) {
        this.refreshError = error?.message || String(error);
        this.overviewData.stale = true;
      } else this.error = error?.message || String(error);
    }
    this.loading = false;
    this.render();
  }

  async loadV02(historyResponse = null, shouldRender = true) {
    const period = historyResponse?.selected_period || this.selectedPeriod;
    if (!period?.start_date || !period?.end_date) return;
    this.v02Report = await this._hass.callWS({type:"home_energy_intelligence/v02_report",start_date:period.start_date,end_date:period.end_date});
    try { this.thermalLedger = await this._hass.callWS({type:"home_energy_intelligence/thermal_readings"}); }
    catch(error) { if(!String(error?.code||error?.message||error).includes("unauthorized")) throw error; this.thermalLedger=null; }
    await this.loadMonthly(false);
    if (shouldRender) this.render();
  }

  async loadMonthly(shouldRender = true) {
    if(!this.monthlyEndMonth)return;
    this.monthlyReport=await this._hass.callWS({type:"home_energy_intelligence/monthly_report",end_month:this.monthlyEndMonth,count:13});
    this.monthlyMetricIds ||= [...(this.monthlyReport.default_metric_ids||[])];
    try{this.monthlyLedger=await this._hass.callWS({type:"home_energy_intelligence/monthly_ecodan_snapshots",start_month:this.monthlyReport.months?.[0],end_month:this.monthlyReport.months?.at(-1)});}
    catch(error){if(!String(error?.code||error?.message||error).includes("unauthorized"))throw error;this.monthlyLedger=null;}
    if(shouldRender)this.render();
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

  temperatureQualityLabel(summary) {
    if (!summary) return "Missing";
    const reasons = (summary.rejected_reasons || summary.rejections || [])
      .map((item) => typeof item === "string" ? item : item?.reason)
      .filter(Boolean).map((item) => String(item).replaceAll("_", " "));
    if (summary.quality === "filtered") return `Filtered samples${reasons.length ? `: ${[...new Set(reasons)].join(", ")}` : ""}`;
    if (summary.quality === "partial" && (summary.rejected_count || reasons.length)) return `Partial: filtered samples${reasons.length ? ` (${[...new Set(reasons)].join(", ")})` : ""}`;
    return String(summary.quality || "missing").replaceAll("_", " ");
  }

  async loadTemperatureTimeline(historyResponse = null, overview = null, shouldRender = true) {
    const period = historyResponse?.selected_period || this.selectedPeriod;
    const data = overview || this.overviewData;
    const sourceIds = [data?.representative_indoor_source_id, data?.representative_outdoor_source_id].filter(Boolean);
    if (!period?.start_date || !period?.end_date || !sourceIds.length) {
      this.temperatureTimelineData = null;
      this.temperatureTimelineError = null;
      if (shouldRender) this.render();
      return;
    }
    const resolution = period.start_date === period.end_date ? "hourly" : "daily";
    try {
      this.temperatureTimelineData = await this._hass.callWS({
        type: "home_energy_intelligence/temperature_timeline",
        start_date: period.start_date, end_date: period.end_date,
        resolution, source_ids: [...new Set(sourceIds)],
      });
      this.temperatureTimelineError = null;
    } catch (error) {
      this.temperatureTimelineData = null;
      this.temperatureTimelineError = error?.message || String(error);
    }
    if (shouldRender) this.render();
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
    const stored = record?.stored_quality;
    const title = record?.quality_basis
      ? `Derived from independent coverage layers${stored && stored !== value ? `; stored legacy label: ${stored}` : ""}`
      : "Persisted source quality";
    return `<span class="quality ${esc(value)}" title="${esc(title)}">${esc(value)}</span>`;
  }

  evidence(value, known) {
    if (value != null) return `${n(value)} kWh`;
    if (known != null) return `${n(known)} kWh <span class="quality partial">known subtotal</span>`;
    return `<span class="missing">Unavailable</span>`;
  }

  splitQuality(record) {
    const labels=[];const energy=record?.energy||{};const devices=energy.equipment||[];
    if(record?.partial===true||record?.finalized===false)labels.push(["partial","incomplete period"]);
    if(record?.household_kwh==null)labels.push(["missing","missing meter"]);
    if(devices.some((row)=>row.available===false||row.classification_complete===false))labels.push(["partial","incomplete classification"]);
    if(devices.some((row)=>["uncertain","estimated","filtered"].includes(row.quality)||Number(row.assumed_heating_kwh||row.selected_heating_kwh||row.estimated_heating_kwh||row.selected_cooling_kwh||row.estimated_cooling_kwh||row.known_assumed_heating_kwh||row.known_selected_heating_kwh||row.known_estimated_heating_kwh||row.known_selected_cooling_kwh||row.known_estimated_cooling_kwh||0)>0))labels.push(["filtered","estimated allocation"]);
    const temperatureMissing=["indoor","outdoor"].some((role)=>{const source=this.representativeSource(role);return source&&record?.temperatures?.[source.source_id]?.mean==null;});
    if(temperatureMissing)labels.push(["temperature-missing","temperature evidence missing"]);
    if(!labels.length)labels.push(["good","complete"]);
    return labels.map(([css,label])=>`<b class="quality ${css}">${esc(label)}</b>`).join(" ");
  }

  splitChart() {
    const history = [...this.history].slice(0, 6).reverse();
    const records = history;
    if (!records.length) return `<div class="empty">No validated daily records yet.</div>`;
    const max = Math.max(1, ...records.map((r) => r.household_kwh ?? r.known_household_kwh ?? 0));
    const ticks=this.axisTicks(0,max);
    return `<div class="bar-chart-shell"><div class="bar-axis">${ticks.map((value)=>`<span style="bottom:${100*value/max}%">${n(value,1)} kWh</span>`).join("")}</div><div class="bars" role="img" aria-label="Daily electricity split with numeric kWh axis">${records.map((r) => {
      const total = r.household_kwh ?? r.known_household_kwh;
      const heat = r.energy?.space_heating_kwh ?? r.energy?.known_space_heating_kwh ?? null;
      const dhw = r.energy?.dhw_kwh ?? r.energy?.known_dhw_kwh ?? null;
      const cool = r.energy?.cooling_kwh ?? r.energy?.known_cooling_kwh ?? null;
      const idle = r.energy?.idle_kwh ?? r.energy?.known_idle_kwh ?? null;
      const otherHvac = r.energy?.other_hvac_kwh ?? r.energy?.known_other_hvac_kwh ?? null;
      const other = r.energy?.other_household_kwh ?? null;
      const unknown = r.energy?.unknown_kwh ?? null;
      const discrepancy = r.energy?.unallocated_discrepancy_kwh;
      const scale = 150 / max;
      const segment=(name,value,css)=>Number.isFinite(value)&&value>0?`<i class="seg ${css}" style="height:${value*scale}px" title="${esc(name)}: ${n(value)} kWh · ${n(total>0?100*value/total:null)}% · ${esc(r.energy?.quality || r.quality)} · ${esc(r.local_date)}"></i>`:"";
      const unavailable=[["heating",heat],["DHW",dhw],["cooling",cool],["idle",idle],["other HVAC",otherHvac],["other household",other],["unallocated",unknown]].filter(([,value])=>value==null).map(([name])=>name);
      return `<div class="bar-col" title="${esc(r.local_date)}">
        <div class="bar" style="height:${Math.max(2,(total||0)*scale)}px">${segment("Unallocated",unknown,"unknown")}${segment("Other household",other,"other")}${segment("Other HVAC (fan/dry)",otherHvac,"other-hvac")}${segment("Idle/standby",idle,"idle")}${segment(r.energy?.cooling_kwh==null&&cool!=null?"Known partial cooling":"Cooling",cool,"cool")}${segment(r.energy?.dhw_kwh==null&&dhw!=null?"Known partial DHW":"DHW",dhw,"dhw")}${segment(r.energy?.space_heating_kwh==null&&heat!=null?"Known partial space heating":"Space heating",heat,"heat")}</div>
        <div class="split-quality">${this.splitQuality(r)}</div>
        ${unavailable.length?`<small class="muted" title="Unavailable is not measured zero">Unavailable: ${unavailable.map(esc).join(", ")}</small>`:""}${discrepancy!=null&&Math.abs(discrepancy)>.05?`<b class="discrepancy" title="Discrepancy: ${n(discrepancy)} kWh · ${n(total?100*discrepancy/total:null)}% · ${esc(r.energy?.quality || r.quality)} · ${esc(r.local_date)}">Δ ${n(discrepancy)}</b>`:""}${other!=null&&other<0?`<b class="discrepancy" title="Other household: ${n(other)} kWh · ${n(total?100*other/total:null)}% · ${esc(r.energy?.quality || r.quality)} · ${esc(r.local_date)}">Other ${n(other)}</b>`:""}<span>${esc(r.local_date.slice(5))}${r.partial===true||r.finalized===false?" partial":""}</span></div>`;
    }).join("")}</div></div><div class="legend"><i class="dot heat"></i>Heating <i class="dot dhw"></i>DHW <i class="dot cool"></i>Cooling <i class="dot idle"></i>Idle <i class="dot other-hvac"></i>Other HVAC <i class="dot other"></i>Other household <i class="dot unknown"></i>Unallocated <b class="discrepancy">Δ discrepancy (signed)</b></div>`;
  }

  scatterChart() {
    const evidence=this.responseProfileData?.points||[];
    const excluded=evidence.filter((point)=>point.status!=="validated");
    const points=evidence.filter((point)=>point.status==="validated").map((point)=>[
      Number(point.outdoor_mean_c),Number(point.heating_electricity_kwh),point.local_date,
      point.outdoor_coverage_percent,point.equipment_heating||[],
    ]);
    const diagnostics=excluded.length?`<details><summary>${excluded.length} excluded day(s) — qualification evidence</summary><div class="table-wrap"><table><thead><tr><th>Date</th><th>Outdoor</th><th>Coverage</th><th>Heating</th><th>Reason</th></tr></thead><tbody>${excluded.map((point)=>`<tr><td>${esc(point.local_date)}</td><td>${n(point.outdoor_mean_c)} °C</td><td>${point.outdoor_coverage_percent==null?"Unavailable":n(point.outdoor_coverage_percent,2)+"%"}</td><td>${n(point.heating_electricity_kwh)} kWh</td><td>${esc((point.exclusion_reasons||[]).join(", "))}</td></tr>`).join("")}</tbody></table></div></details>`:"";
    if (!points.length) return `<div class="empty">No backend-qualified outdoor-temperature/heating points for this period.</div>${diagnostics}`;
    const xs = points.map((p) => p[0]); const ys = points.map((p) => p[1]);
    const xmin = Math.min(...xs) - 1, xmax = Math.max(...xs) + 1;
    const ymax = Math.max(1, ...ys) * 1.1;
    const px = (x) => 42 + ((x - xmin) / Math.max(1, xmax - xmin)) * 480;
    const py = (y) => 180 - (y / ymax) * 145;
    const numericAxes=this.axisTicks(0,ymax).map((value)=>`<text x="4" y="${py(value)+4}">${n(value,1)} kWh</text>`).join("")+this.axisTicks(xmin,xmax).map((value)=>`<text x="${px(value)}" y="205" text-anchor="middle">${n(value,1)} °C</text>`).join("");
    return `<svg viewBox="0 0 550 215" class="chart" role="img" aria-label="Heating electricity versus outdoor temperature">
      ${numericAxes}
      ${[0, .25, .5, .75, 1].map((v) => `<line x1="42" x2="522" y1="${35 + v * 145}" y2="${35 + v * 145}" />`).join("")}
      <text x="8" y="40">${n(ymax)}</text><text x="22" y="183">0</text>
      ${points.map((p) => `<circle cx="${px(p[0])}" cy="${py(p[1])}" r="5"><title>${esc(p[2])}: ${n(p[0])} °C, ${n(p[1])} kWh electric · coverage ${n(p[3],2)}% · ${p[4].map((item)=>`${esc(item.display_name)} ${n(item.space_heating_kwh)} kWh`).join(" · ")}</title></circle>`).join("")}
      <text x="42" y="205">${n(xmin)} °C</text><text x="480" y="205">${n(xmax)} °C</text>
    </svg>${diagnostics}`;
  }

  responseProfile() {
    const area=Number(this.overviewData?.floor_area_m2); const source=this.representativeSource("outdoor");
    if(!source) return `<div class="empty">Configure a representative measured outdoor-air sensor to build the electrical response curve.</div>`;
    if(!(area>0)) return `<div class="empty">Configure floor area to normalize electrical space-heating demand.</div>`;
    const evidence=this.responseProfileData?.points||[];const excluded=evidence.filter((record)=>record.status!=="validated");
    const points=evidence.filter((record)=>record.status==="validated").map((record)=>({x:Number(record.outdoor_mean_c),y:Number(record.normalized_heating_kwh_m2_day),date:record.local_date,coverage:Number(record.outdoor_coverage_percent),hp:Number(record.heat_pump_heating_kwh||0),ac:Number(record.ac_heating_kwh||0),uncertain:Number(record.uncertain_heating_kwh||0)}));
    const diagnostics=excluded.length?`<details><summary>${excluded.length} excluded day(s) — evidence retained</summary><div class="table-wrap"><table><thead><tr><th>Date</th><th>Outdoor</th><th>Coverage</th><th>Heating</th><th>Reason</th></tr></thead><tbody>${excluded.map((row)=>`<tr><td>${esc(row.local_date)}</td><td>${n(row.outdoor_mean_c)} °C</td><td>${n(row.outdoor_coverage_percent,0)}%</td><td>${n(row.heating_electricity_kwh)} kWh</td><td>${esc((row.exclusion_reasons||[]).join(", "))}</td></tr>`).join("")}</tbody></table></div></details>`:"";
    if(!points.length)return `<div class="empty">Insufficient validated data. Daily exclusions remain available below.</div>${diagnostics}`;
    const bins=new Map();for(const point of points){const key=Math.floor(point.x/2)*2;const bucket=bins.get(key)||{x:0,y:0,hp:0,ac:0,n:0};bucket.x+=point.x;bucket.y+=point.y;bucket.hp+=point.hp;bucket.ac+=point.ac;bucket.n+=1;bins.set(key,bucket);}const series=[...bins.values()].map((bucket)=>({x:bucket.x/bucket.n,y:bucket.y/bucket.n,hp:bucket.hp/bucket.n,ac:bucket.ac/bucket.n,n:bucket.n})).sort((a,b)=>a.x-b.x);
    const xs=points.map((p)=>p.x),ys=points.map((p)=>p.y),xMean=xs.reduce((a,b)=>a+b,0)/xs.length,yMean=ys.reduce((a,b)=>a+b,0)/ys.length;const denominator=xs.reduce((sum,x)=>sum+(x-xMean)**2,0);const slope=denominator?xs.reduce((sum,x,index)=>sum+(x-xMean)*(ys[index]-yMean),0)/denominator:null;const intercept=slope==null?null:yMean-slope*xMean;const residual=slope==null?null:ys.reduce((sum,y,index)=>sum+(y-(intercept+slope*xs[index]))**2,0);const variance=ys.reduce((sum,y)=>sum+(y-yMean)**2,0);const r2=residual==null||variance===0?null:1-residual/variance;const degreeDays=points.reduce((sum,p)=>sum+Math.max(0,18-p.x),0);const outdoorSpan=Math.max(...xs)-Math.min(...xs);const minimumPoints=Number(this.responseProfileData?.minimum_regression_points||10),minimumSpan=Number(this.responseProfileData?.minimum_outdoor_span_c||8),minimumR2=Number(this.responseProfileData?.minimum_regression_r_squared||.5);const modelValid=points.length>=minimumPoints&&outdoorSpan>=minimumSpan&&r2!=null&&r2>=minimumR2;
    const maxY=Math.max(.01,...series.map((p)=>p.y))*1.1,minX=Math.min(...series.map((p)=>p.x))-1,maxX=Math.max(...series.map((p)=>p.x))+1,px=(x)=>44+(x-minX)/Math.max(1,maxX-minX)*470,py=(y)=>178-y/maxY*142;
    const annualReady=this.historyCoverage?.complete&&["this_year","previous_year","current_season","previous_season"].includes(this.historyPreset);const annual=this.historySummary?.energy?.space_heating_kwh;
    return `<div class="response-evidence"><p><b>Source:</b> ${esc(source.display_name)} · <b>floor area:</b> ${n(area,0)} m² · minimum outdoor coverage ${n(this.responseProfileData?.minimum_temperature_coverage_percent,0)}%</p><svg viewBox="0 0 550 215" class="chart" role="img" aria-label="Whole-house electrical space-heating response"><line x1="44" x2="514" y1="178" y2="178"/>${series.map((p)=>`<circle cx="${px(p.x)}" cy="${py(p.y)}" r="${Math.min(9,4+p.n)}"><title>${n(p.x)} °C · ${n(p.y,3)} kWh/m²/day · HP ${n(p.hp)} kWh/day · AC ${n(p.ac)} kWh/day · ${p.n} day(s)</title></circle>`).join("")}${this.axisTicks(0,maxY).map((value)=>`<text x="8" y="${py(value)+4}">${n(value,3)}</text>`).join("")}<text x="8" y="22">kWh/m²/day</text><text x="44" y="205">${n(minX)} °C</text><text x="462" y="205">${n(maxX)} °C</text></svg><div class="mini-stats"><span>Validated days <b>${points.length}</b></span><span>Observed outdoor span <b>${n(outdoorSpan)} °C</b></span><span>Regression <b>${modelValid?`Validated · R² ${n(r2,3)}`:"Insufficient validated data"}</b></span><span>Observed degree-days (18 °C) <b>${n(degreeDays,1)}</b></span><span>Annual/season electricity <b>${annualReady&&annual!=null?n(annual)+" kWh":"Incomplete period"}</b></span><span>Thermal output / COP <b>Unavailable</b></span><span>Design day <b>Unavailable</b></span></div><p class="muted">Observed electrical demand only. A fit requires at least ${minimumPoints} qualified days, ${n(minimumSpan)} °C outdoor span and R² ≥ ${n(minimumR2,2)}. Confirmed and uncertain heating remain separate in daily evidence. No physical balance point, thermal output, COP, design-day capacity or short-period annual extrapolation is inferred.</p>${diagnostics}</div>`;
  }

  responseCurve(points, {kind="heating", thermal=false}={}) {
    const usable=(points||[]).filter((point)=>point.outdoor_mean_c!=null);
    if(!usable.length)return `<div class="empty">No matched measured intervals with a validated outdoor temperature.</div>`;
    const values=usable.flatMap((point)=>thermal
      ? [point[`${kind}_electricity_kwh`],point[`${kind}_thermal_delta_kwh`]].filter((value)=>value!=null)
      : [kind==="cooling"?point.ac_cooling_kwh:point.whole_house_heating_kwh].filter((value)=>value!=null));
    if(!values.length)return `<div class="empty">Measurements exist, but this category is unavailable for the exact selected interval.</div>`;
    if(kind==="cooling"&&!thermal&&values.every((value)=>Number(value)===0))return `<div class="empty">Measured AC cooling electricity: 0.0 kWh for the selected intervals.</div>`;
    const minX=Math.min(...usable.map((point)=>point.outdoor_mean_c)),maxX=Math.max(...usable.map((point)=>point.outdoor_mean_c));
    const maxY=Math.max(1,...values);const px=(value)=>45+(Number(value)-minX)*450/Math.max(1,maxX-minX);const py=(value)=>175-Number(value)*140/maxY;
    const numericAxes=this.axisTicks(0,maxY).map((value)=>`<g><line x1="45" x2="495" y1="${py(value)}" y2="${py(value)}"/><text x="4" y="${py(value)+4}">${n(value,1)} kWh</text></g>`).join("")+this.axisTicks(minX,maxX).map((value)=>`<g><line x1="${px(value)}" x2="${px(value)}" y1="35" y2="175"/><text x="${px(value)}" y="202" text-anchor="middle">${n(value,1)} °C</text></g>`).join("");
    const marks=numericAxes+usable.map((point)=>{
      if(thermal){const electricity=point[`${kind}_electricity_kwh`],heat=point[`${kind}_thermal_delta_kwh`],cop=point[`${kind}_cop`];return `${electricity==null?"":`<circle class="electric" cx="${px(point.outdoor_mean_c)}" cy="${py(electricity)}" r="5"><title>${n(point.outdoor_mean_c)} °C · electricity ${n(electricity,2)} kWh · ${esc(point.start_utc)}–${esc(point.end_utc)}</title></circle>`}${heat==null?"":`<rect class="thermal" x="${px(point.outdoor_mean_c)-5}" y="${py(heat)-5}" width="10" height="10"><title>${n(point.outdoor_mean_c)} °C · measured thermal ${n(heat,2)} kWh thermal</title></rect>`}${cop==null?"":`<text class="cop-label" x="${px(point.outdoor_mean_c)+7}" y="${py(heat??electricity)-7}">Interval COP ${n(cop,2)}</text>`}`;}
      const total=kind==="cooling"?point.ac_cooling_kwh:point.whole_house_heating_kwh;const contributions=(kind==="cooling"?point.cooling_units:point.heat_pumps.concat(point.air_conditioners)).map((item)=>`${item.display_name}: ${n(item.electricity_kwh,2)} kWh (${item.quality||"unknown quality"})`).join(" · ");return total==null?"":`<circle cx="${px(point.outdoor_mean_c)}" cy="${py(total)}" r="6"><title>${esc(point.local_date)} · ${n(point.outdoor_mean_c)} °C · ${n(total,2)} kWh · ${esc(contributions)} · normalized ${n(kind==="cooling"?point.normalized_cooling_kwh_m2_day:point.normalized_heating_kwh_m2_day,3)} kWh/m²/day</title></circle>`;
    }).join("");
    return `<svg viewBox="0 0 530 210" class="chart response-curve" role="img" aria-label="${thermal?`Ecodan ${kind} measured electricity thermal energy and COP`:kind==="cooling"?"Air conditioner cooling electricity versus outdoor temperature":"Whole house heating electricity versus outdoor temperature"}"><line x1="45" x2="495" y1="175" y2="175"/>${marks}<text x="7" y="38">${n(maxY,1)} kWh</text><text x="45" y="201">${n(minX)} °C</text><text x="450" y="201">${n(maxX)} °C</text></svg><div class="legend">${thermal?'<i class="dot electric"></i>Electricity <i class="dot thermal"></i>Measured thermal · COP is ratio on the exact interval':kind==="cooling"?'Per-device AC cooling and combined total':'Ecodan and each AC heating contribution; total and kWh/m²/day in tooltips'}</div>`;
  }

  axisTicks(minimum, maximum, count=4) {
    const min=Number.isFinite(Number(minimum))?Number(minimum):0,max=Number.isFinite(Number(maximum))?Number(maximum):1;
    const span=Math.max(.000001,max-min);return Array.from({length:count+1},(_,index)=>min+span*index/count);
  }

  monthlyHistogram() {
    const report=this.monthlyReport;if(!report?.rows?.length)return `<div class="empty">Monthly evidence has not loaded.</div>`;
    const catalog=new Map((report.metric_catalog||[]).map((item)=>[item.id,item]));
    const selected=(this.monthlyMetricIds||[]).map((id)=>catalog.get(id)).filter(Boolean);
    const energy=selected.filter((item)=>item.axis==="energy"),cop=selected.filter((item)=>item.axis==="cop"),percent=selected.filter((item)=>item.axis==="percent"||item.axis==="normalized");
    const colors=["#ef4444","#f59e0b","#238ee8","#159b5b","#8b5cf6","#14b8a6","#8b9bb0","#ec4899"];
    const width=1100,left=66,right=52,top=30,bottom=72,height=350,plotWidth=width-left-right,plotHeight=height-top-bottom;
    const step=plotWidth/report.rows.length;
    const energyValues=report.rows.flatMap((row)=>energy.map((metric)=>row[metric.id]).filter((value)=>value!=null).map(Number));
    const energyMin=Math.min(0,...energyValues),energyMax=energyValues.length?Math.max(0,...energyValues):0;
    const energyY=(value)=>top+(energyMax-Number(value))*plotHeight/Math.max(1,energyMax-energyMin);
    const zeroY=energyY(0);const barWidth=Math.max(2,Math.min(12,step*.72/Math.max(1,energy.length)));
    const energyTicks=!energyValues.length?[]:(energyMin===energyMax?[energyMin]:this.axisTicks(energyMin,energyMax));const grid=energyTicks.map((value)=>`<g><line x1="${left}" x2="${width-right}" y1="${energyY(value)}" y2="${energyY(value)}"/><text x="4" y="${energyY(value)+4}">${n(value,1)} kWh</text></g>`).join("");
    const bars=report.rows.flatMap((row,rowIndex)=>energy.map((metric,metricIndex)=>{const value=row[metric.id];if(value==null)return "";const x=left+step*rowIndex+step/2-(energy.length*barWidth)/2+metricIndex*barWidth;const y=energyY(Math.max(0,Number(value))),heightValue=Math.abs(energyY(Number(value))-zeroY);const assumed=row.ecodan_assumed_heating_kwh==null?"":` · assumed heating ${n(row.ecodan_assumed_heating_kwh,3)} kWh (${n(row.ecodan_assumed_heating_percent,2)}%)`;return `<rect class="monthly-bar" x="${x}" y="${Number(value)>=0?y:zeroY}" width="${Math.max(1,barWidth-1)}" height="${Math.max(1,heightValue)}" fill="${colors[metricIndex%colors.length]}"><title>${esc(row.month)} · ${esc(metric.label)}: ${n(value,3)} ${esc(metric.unit)} · ${esc(row.quality)}${assumed} · cutoff ${esc(row.snapshot_cutoff_utc||row.energy_period_end_utc||"unavailable")}</title></rect>`;})).join("");
    const copValues=report.rows.flatMap((row)=>cop.map((metric)=>row[metric.id]).filter((value)=>value!=null).map(Number));const copMax=copValues.length?Math.max(0,...copValues):0;const copY=(value)=>top+(copMax-Number(value))*plotHeight/Math.max(1,copMax);
    const copLines=cop.map((metric,index)=>{const points=report.rows.map((row,rowIndex)=>row[metric.id]==null?null:[left+step*rowIndex+step/2,copY(row[metric.id]),row]).filter(Boolean);return `${points.length>1?`<polyline class="monthly-line" stroke="${colors[(energy.length+index)%colors.length]}" points="${points.map((point)=>`${point[0]},${point[1]}`).join(" ")}"/>`:""}${points.map((point)=>`<circle class="monthly-point" fill="${colors[(energy.length+index)%colors.length]}" cx="${point[0]}" cy="${point[1]}" r="4"><title>${esc(point[2].month)} · ${esc(metric.label)}: ${n(point[2][metric.id],3)} · ${esc(point[2].primary_cop_quality)} · assumed heating ${n(point[2].ecodan_assumed_heating_kwh,3)} kWh (${n(point[2].ecodan_assumed_heating_percent,2)}%)</title></circle>`).join("")}`;}).join("");
    const copAxis=copValues.length?(copMax===0?[0]:this.axisTicks(0,copMax)).map((value)=>`<text x="${width-right+5}" y="${copY(value)+4}">${n(value,1)} COP</text>`).join(""):"";
    const months=report.rows.map((row,index)=>`<text class="month-label" transform="translate(${left+step*index+step/2},${height-bottom+18}) rotate(45)">${esc(row.month)}</text>`).join("");
    const legend=selected.map((metric,index)=>`<span><i style="background:${colors[index%colors.length]}"></i>${esc(metric.label)} (${esc(metric.unit)})</span>`).join("");
    const percentChart=percent.map((metric,index)=>{const values=report.rows.map((row)=>row[metric.id]).filter((value)=>value!=null).map(Number);if(!values.length)return `<article class="monthly-percent"><h3>${esc(metric.label)} (${esc(metric.unit)})</h3><div class="empty">Unavailable for all 13 months.</div></article>`;const bound=Math.max(...values.map(Math.abs));const scale=Math.max(1,bound);const y=(value)=>85-Number(value)*60/scale;const ticks=bound===0?[0]:this.axisTicks(-bound,bound);return `<article class="monthly-percent"><h3>${esc(metric.label)} (${esc(metric.unit)})</h3><svg viewBox="0 0 1100 125" role="img" aria-label="${esc(metric.label)}"><line x1="${left}" x2="${width-right}" y1="${y(0)}" y2="${y(0)}"/>${ticks.map((value)=>`<text x="4" y="${y(value)+4}">${n(value,1)} ${esc(metric.unit)}</text>`).join("")}${report.rows.map((row,rowIndex)=>row[metric.id]==null?"":`<circle cx="${left+step*rowIndex+step/2}" cy="${y(row[metric.id])}" r="4" fill="${colors[(energy.length+cop.length+index)%colors.length]}"><title>${esc(row.month)} · ${n(row[metric.id],3)} ${esc(metric.unit)} · ${esc(row.quality)}</title></circle>`).join("")}</svg></article>`;}).join("");
    return `<div class="monthly-chart-wrap"><svg class="monthly-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Rolling 13-month Ecodan energy and COP histogram">${grid}${energyValues.length?`<line x1="${left}" x2="${width-right}" y1="${zeroY}" y2="${zeroY}" class="zero-line"/>`:""}${bars}${copLines}${copAxis}${months}</svg><div class="monthly-legend">${legend}</div>${percentChart}</div>`;
  }

  monthlyTable() {
    const report=this.monthlyReport;if(!report?.rows)return "";const catalog=new Map((report.metric_catalog||[]).map((item)=>[item.id,item]));const selected=(this.monthlyMetricIds||[]).map((id)=>catalog.get(id)).filter(Boolean);
    return `<div class="table-wrap monthly-table"><table><thead><tr><th>Month</th>${selected.map((item)=>`<th>${esc(item.label)}<small>${esc(item.unit)}</small></th>`).join("")}<th>Cutoff / evidence</th><th>Quality</th></tr></thead><tbody>${report.rows.map((row)=>`<tr><td>${esc(row.month)}</td>${selected.map((item)=>`<td>${n(row[item.id],item.unit==="COP"?2:3)}</td>`).join("")}<td>${esc(row.snapshot_cutoff_utc||row.energy_period_end_utc||"Unavailable")}<br><small>${row.cutoff_match?"exactly matched":"not exactly matched"} · ${esc(row.snapshot_status)}</small></td><td><span class="quality ${esc(row.quality)}">${esc(row.quality)}</span></td></tr>`).join("")}</tbody></table></div>`;
  }

  monthlyView() {
    const report=this.monthlyReport;const catalog=report?.metric_catalog||[];const selected=new Set(this.monthlyMetricIds||[]);const heatPumps=this.equipment.filter((item)=>item.kind==="heat_pump");const edit=this.monthlyEdit||{};const ledger=this.monthlyLedger?.snapshots||[];
    const entryLocal=String(this.overviewData?.cutoff_local||"");const entryDefaultDate=entryLocal.slice(0,10);const entryDefaultTime=entryLocal.includes("T")?entryLocal.slice(11,19):"";
    const groupLabels={house_pv:"House electricity and PV",ecodan_shelly:"Ecodan Shelly electricity",ac_house:"AC and whole-house electricity",thermal:"Ecodan measured generated heat",primary_cop:"Primary Shelly COP",vendor:"Controller comparison and diagnostics"};const groups=Object.groupBy?Object.groupBy(catalog,(item)=>item.group||"other"):catalog.reduce((all,item)=>{(all[item.group||"other"]??=[]).push(item);return all;},{});const metricGroups=Object.entries(groups).map(([group,items])=>`<fieldset class="metric-selector"><legend>${esc(groupLabels[group]||group)}</legend>${items.map((item)=>`<label><input type="checkbox" class="monthly-metric" value="${esc(item.id)}" ${selected.has(item.id)?"checked":""}>${esc(item.label)} <small>${esc(item.unit)}</small></label>`).join("")}</fieldset>`).join("");
    const controls=`<div class="monthly-controls"><label>End month <input class="monthly-end" type="month" value="${esc(this.monthlyEndMonth||"")}"></label><button class="primary monthly-apply">Load rolling 13 months</button><button class="primary monthly-csv">Export selected CSV</button></div>${metricGroups}`;
    const form=this.monthlyLedger?`<form class="thermal-form monthly-form"><input type="hidden" name="snapshot_id" value="${esc(edit.id||"")}"><label>Report month <input required type="month" name="report_month" value="${esc(edit.report_month||this.monthlyEndMonth||"")}"></label><label>Heat pump <select name="ecodan_equipment_id">${heatPumps.map((item)=>`<option value="${esc(item.equipment_id)}" ${edit.ecodan_equipment_id===item.equipment_id?"selected":""}>${esc(item.display_name)}</option>`).join("")}</select></label><label>Report cutoff date <input required type="date" name="cutoff_local_date" value="${esc(edit.cutoff_local_date||"")}"></label><label>Report cutoff time <input type="time" step="1" name="cutoff_local_time" value="${esc(edit.cutoff_local_time||"")}"></label><label>Cutoff certainty <select name="cutoff_certainty"><option value="exact" ${edit.cutoff_certainty==="exact"?"selected":""}>Exact date and time</option><option value="day_only" ${edit.cutoff_certainty!=="exact"&&edit.cutoff_certainty!=="unknown"?"selected":""}>Date only</option><option value="unknown" ${edit.cutoff_certainty==="unknown"?"selected":""}>Unknown cutoff</option></select></label><label>Entry date <input required type="date" name="entry_local_date" value="${esc(edit.entry_local_date||entryDefaultDate)}"></label><label>Entry time <input required type="time" step="1" name="entry_local_time" value="${esc(edit.entry_local_time||entryDefaultTime)}"></label><label>Status <select name="status"><option value="partial" ${edit.status!=="final"?"selected":""}>Partial / month-to-date</option><option value="final" ${edit.status==="final"?"selected":""}>Verified final full month</option></select></label><label>Heating produced (kWh thermal) <input type="number" min="0" step="any" name="heating_thermal_kwh" value="${esc(edit.heating_thermal_kwh??"")}"></label><label>DHW produced (kWh thermal) <input type="number" min="0" step="any" name="dhw_thermal_kwh" value="${esc(edit.dhw_thermal_kwh??"")}"></label><label>Ecodan-reported heating electricity <input type="number" min="0" step="any" name="vendor_heating_electricity_kwh" value="${esc(edit.vendor_heating_electricity_kwh??"")}"></label><label>Ecodan-reported DHW electricity <input type="number" min="0" step="any" name="vendor_dhw_electricity_kwh" value="${esc(edit.vendor_dhw_electricity_kwh??"")}"></label><label>Ecodan-reported combined electricity <input type="number" min="0" step="any" name="vendor_combined_electricity_kwh" value="${esc(edit.vendor_combined_electricity_kwh??"")}"></label><label>Ecodan-reported combined heat <input type="number" min="0" step="any" name="vendor_combined_thermal_kwh" value="${esc(edit.vendor_combined_thermal_kwh??"")}"></label><label>Report identity <input required name="meter_identity" value="${esc(edit.meter_identity||"monthly_report")}"></label><label>Source label <input required name="source_label" value="${esc(edit.source_label||"")}"></label><label>Screenshot / source reference <input name="source_reference" value="${esc(edit.source_reference||"")}"></label><label>Unit <select name="unit"><option>kWh</option><option>Wh</option><option>MWh</option></select></label><label>Note <input name="note" value="${esc(edit.note||"")}"></label><button class="primary" type="submit">${edit.id?"Save monthly revision":"Add monthly snapshot"}</button></form><div class="table-wrap"><table><thead><tr><th>Report / cutoff</th><th>Equipment</th><th>Produced H / DHW</th><th>Vendor electricity H / DHW</th><th>Status / source</th><th>Actions</th></tr></thead><tbody>${ledger.map((row)=>`<tr><td>${esc(row.report_month)} · ${esc(row.cutoff_local_date)} ${esc(row.cutoff_local_time||row.cutoff_certainty)}<br><small>entered ${esc(row.entry_local_date)} ${esc(row.entry_local_time)}</small></td><td>${esc(row.ecodan_equipment_id)}</td><td>${n(row.heating_thermal_kwh,3)} / ${n(row.dhw_thermal_kwh,3)}</td><td>${n(row.vendor_heating_electricity_kwh,3)} / ${n(row.vendor_dhw_electricity_kwh,3)}</td><td>${esc(row.status)} · ${esc(row.source_label)}</td><td><button class="monthly-edit" data-id="${row.id}">Edit</button> <button class="monthly-delete" data-id="${row.id}">Delete</button></td></tr>`).join("")||'<tr><td colspan="6">No monthly snapshots entered.</td></tr>'}</tbody></table></div>`:'<p class="muted">Administrator access is required to manage monthly report snapshots.</p>';
    return `<section><article class="card page-card"><div class="section-head"><div><h2>Rolling 13-Month Ecodan Analytics</h2><p>Shelly-classified electricity is primary. Vendor electricity and vendor COP are secondary comparisons.</p></div></div>${controls}${this.monthlyHistogram()}${this.monthlyTable()}</article><article class="card page-card"><h2>Monthly / Month-to-Date Ecodan report snapshots</h2><p class="muted">This form records per-period report values. It is separate from cumulative lifetime thermal meter observations. Blank fields remain unavailable.</p>${form}</article></section>`;
  }

  wholeHouseEnergyCard() {
    const selectedRecord=this.selectedRecord()||{};const energy=selectedRecord.energy||{};const thermal=this.v02Report?.ecodan_space_heating||[];
    if(energy.known_household_kwh==null)energy.known_household_kwh=selectedRecord.known_household_kwh;
    const measuredSubtotal=(field)=>{const values=thermal.map((item)=>item[field]).filter((value)=>value!=null).map(Number);return values.length?{value:values.reduce((sum,value)=>sum+value,0),count:values.length}:null;};
    const heat=measuredSubtotal("heating_thermal_delta_kwh"),dhw=measuredSubtotal("dhw_thermal_delta_kwh");
    const hpHeating=addNullable(energy.confirmed_heat_pump_heating_kwh,energy.assumed_heat_pump_heating_kwh);const hpKnown=addNullable(energy.known_confirmed_heat_pump_heating_kwh,energy.known_assumed_heat_pump_heating_kwh);
    const acRows=(energy.equipment||[]).filter((item)=>item.kind==="air_conditioner"),acComplete=acRows.length&&acRows.every((item)=>item.classification_complete);const acKnown=acRows.length?acRows.reduce((sum,item)=>sum+Number(item.known_confirmed_heating_kwh||0)+Number(item.known_selected_heating_kwh||0)+Number(item.known_estimated_heating_kwh||0),0):null;const acHeating=acComplete?acKnown:null;
    const thermalEvidence=(item)=>item?`${n(item.value)} kWh thermal <span class="quality partial">${item.count} exact interval(s); not a filled period total</span>`:`<span class="missing">Unavailable</span>`;
    return `<article class="card page-card"><h2><ha-icon icon="mdi:home-lightning-bolt-outline"></ha-icon>Whole-House Energy / Toplotni profil hiše</h2><div class="table-wrap"><table><thead><tr><th>Energy path</th><th>Electricity</th><th>Measured thermal</th><th>Evidence</th></tr></thead><tbody><tr><td>Whole-house electricity</td><td>${this.evidence(energy.household_kwh,energy.known_household_kwh)}</td><td>—</td><td>physical household balance; missing remains unavailable</td></tr><tr><td>Ecodan space heating</td><td>${this.evidence(hpHeating,hpKnown)}</td><td>${thermalEvidence(heat)}</td><td>classified Shelly meter / fully contained cumulative intervals</td></tr><tr><td>Ecodan DHW</td><td>${this.evidence(energy.dhw_kwh,energy.known_dhw_kwh)}</td><td>${thermalEvidence(dhw)}</td><td>matched intervals only</td></tr><tr><td>AC heating</td><td>${this.evidence(acHeating,acKnown)}</td><td><span class="missing">Unavailable</span></td><td>AC thermal output unavailable</td></tr><tr><td>AC cooling</td><td>${this.evidence(energy.cooling_kwh,energy.known_cooling_kwh)}</td><td><span class="missing">Unavailable</span></td><td>no AC COP inferred</td></tr><tr><td>Idle / other / unknown</td><td>${this.evidence(addNullable(energy.idle_kwh,energy.other_hvac_kwh,energy.unknown_kwh),addNullable(energy.known_idle_kwh,energy.known_other_hvac_kwh,energy.unknown_kwh))}</td><td>—</td><td>preserved separately</td></tr></tbody></table></div><p class="quality partial">Whole-house thermal total is unavailable while AC thermal output or interval coverage is missing. Known measured subtotals are shown without filling gaps.</p></article>`;
  }

  thermalView() {
    const daily=this.v02Report?.whole_house_heating||[];const thermal=this.v02Report?.ecodan_space_heating||[];
    return `${this.periodControls()}${this.wholeHouseEnergyCard()}<section class="grid equipment-charts"><article class="card"><h2>Whole-House Heating Electricity</h2>${this.responseCurve(daily,{kind:"heating"})}</article><article class="card"><h2>AC Cooling Electricity</h2>${this.responseCurve(this.v02Report?.ac_cooling||[],{kind:"cooling"})}</article></section><section class="card page-card"><h2>Evidence and uncertainty</h2><p>Outdoor X-axis: <code>${esc(this.v02Report?.outdoor_source_id||"not configured")}</code>. Floor area ${n(this.v02Report?.floor_area_m2,0)} m². Electrical categories remain separate. AC thermal generation and AC COP are unavailable by design.</p><p class="muted">Measured thermal points are interval observations, not invented daily values. Missing source coverage remains missing.</p></section>`;
  }

  copView() {
    const points=this.v02Report?.ecodan_space_heating||[];
    const reports=this.v02Report?.ecodan_reports||{};const report=(label,item)=>`<span>${label}<b>${item?.cop==null?"Unavailable":n(item.cop,2)}</b><small>${n(item?.thermal_kwh,2)} kWh thermal / ${n(item?.electricity_kwh,2)} kWh electricity · ${item?.matched_interval_count||0} matched category interval(s)</small></span>`;
    const heatPumps=this.equipment.filter((item)=>item.kind==="heat_pump");const edit=this.thermalEdit||{};
    const ledger=this.thermalLedger?.readings||[];const rows=ledger.map((row)=>`<tr><td>${esc(row.local_date)} ${esc(row.local_time)} ${esc(row.timezone)} fold ${row.fold}</td><td>${esc(row.ecodan_equipment_id)}</td><td>${n(row.heating_thermal_kwh,3)}</td><td>${n(row.dhw_thermal_kwh,3)}</td><td>${esc(row.source_label)}</td><td><button class="thermal-edit" data-id="${row.id}">Edit</button> <button class="thermal-delete" data-id="${row.id}">Delete</button></td></tr>`).join("");
    const form=this.thermalLedger?`<form class="thermal-form"><input type="hidden" name="reading_id" value="${esc(edit.id||"")}"><label>Heat pump <select name="ecodan_equipment_id">${heatPumps.map((item)=>`<option value="${esc(item.equipment_id)}" ${edit.ecodan_equipment_id===item.equipment_id?"selected":""}>${esc(item.display_name)}</option>`).join("")}</select></label><label>Local date <input required type="date" name="local_date" value="${esc(edit.local_date||"")}"></label><label>Local time <input required type="time" step="1" name="local_time" value="${esc(edit.local_time||"")}"></label><label>DST occurrence <select name="fold"><option value="">Unambiguous</option><option value="0" ${edit.fold===0?"selected":""}>First</option><option value="1" ${edit.fold===1?"selected":""}>Second</option></select></label><label>Cumulative heating thermal <input type="number" min="0" step="any" name="heating_thermal_kwh" value="${esc(edit.heating_thermal_kwh??"")}"></label><label>Cumulative DHW thermal <input type="number" min="0" step="any" name="dhw_thermal_kwh" value="${esc(edit.dhw_thermal_kwh??"")}"></label><label>Vendor heating electricity (optional) <input type="number" min="0" step="any" name="vendor_heating_electricity_kwh" value="${esc(edit.vendor_heating_electricity_kwh??"")}"></label><label>Vendor DHW electricity (optional) <input type="number" min="0" step="any" name="vendor_dhw_electricity_kwh" value="${esc(edit.vendor_dhw_electricity_kwh??"")}"></label><label>Meter identity <input required name="meter_identity" value="${esc(edit.meter_identity||"")}"></label><label>Source label <input required name="source_label" value="${esc(edit.source_label||"")}"></label><label>Unit <select name="unit"><option>kWh</option><option>Wh</option><option>MWh</option></select></label><label>Note <input name="note" value="${esc(edit.note||"")}"></label><button class="primary" type="submit">${edit.id?"Save revision":"Add cumulative reading"}</button></form><div class="table-wrap"><table><thead><tr><th>Effective time</th><th>Equipment</th><th>Heat kWh</th><th>DHW kWh</th><th>Source</th><th>Actions</th></tr></thead><tbody>${rows||'<tr><td colspan="6">First reading establishes the baseline.</td></tr>'}</tbody></table></div>`:'<p class="muted">Administrator access is required to manage cumulative readings.</p>';
    return `${this.periodControls()}<article class="card"><h2>Ecodan-only COP reports</h2><div class="mini-stats">${report("Space heating COP",reports.heating)}${report("DHW COP",reports.dhw)}${report("Combined COP",reports.combined)}</div><p class="muted">Combined COP is the ratio of measured thermal sums to exactly matched electricity sums. COP values are never averaged. AC electricity and thermal estimates are excluded.</p></article><section class="grid equipment-charts"><article class="card"><h2>Ecodan Space-Heating Performance Curve</h2>${this.responseCurve(points,{kind:"heating",thermal:true})}</article><article class="card"><h2>Ecodan DHW Performance Curve</h2>${this.responseCurve(this.v02Report?.ecodan_dhw||[],{kind:"dhw",thermal:true})}</article></section><article class="card page-card"><h2>Measured cumulative Ecodan generation readings</h2><p class="muted">Enter cumulative meter observations only. The first is a baseline. Negative differences are resets/corrections; no value is clamped. Multi-day spans remain clearly labelled interval COP.</p>${this.thermalLedger?`<p><button type="button" class="primary thermal-backfill">Backfill configured thermal counters for selected period</button></p>`:""}${form}</article>${this.monthlyView()}`;
  }

  recentTable(records = this.history.slice(0, 7)) {
    if (!records.length) return `<div class="empty">No daily records yet. The first close runs at 05:00 local time.</div>`;
    return `<div class="table-wrap"><table><thead><tr><th>Date</th><th>Whole house</th><th>HP confirmed</th><th>HP assumed</th><th>AC confirmed</th><th>AC selected/estimated</th><th>Total heating</th><th>DHW</th><th>Heating + DHW</th><th>Cooling</th><th>HVAC idle</th><th>Other HVAC</th><th>Other household</th><th>Unallocated</th><th>Discrepancy (signed)</th><th>PV</th><th>Import</th><th>Export</th><th>Self-sufficiency</th><th>Quality</th></tr></thead><tbody>
      ${records.map((r) => `<tr><td>${esc(r.local_date)}</td><td>${n(r.energy?.household_kwh)} kWh</td>
        <td>${n(r.energy?.confirmed_heat_pump_heating_kwh)} kWh</td><td>${n(r.energy?.assumed_heat_pump_heating_kwh)} kWh</td>
        <td>${n(r.energy?.confirmed_ac_heating_kwh)} kWh</td><td>${n(addNullable(r.energy?.selected_ac_heating_kwh,r.energy?.estimated_ac_heating_kwh))} kWh</td>
        <td>${this.evidence(r.energy?.space_heating_kwh,r.energy?.known_space_heating_kwh)}</td><td>${this.evidence(r.energy?.dhw_kwh,r.energy?.known_dhw_kwh)}</td><td>${this.evidence(r.energy?.heating_and_dhw_kwh,addNullable(r.energy?.known_space_heating_kwh,r.energy?.known_dhw_kwh))}</td><td>${this.evidence(r.energy?.cooling_kwh,r.energy?.known_cooling_kwh)}</td>
        <td>${this.evidence(r.energy?.idle_kwh,r.energy?.known_idle_kwh)}</td><td>${this.evidence(r.energy?.other_hvac_kwh,r.energy?.known_other_hvac_kwh)}</td><td>${n(r.energy?.other_household_kwh)} kWh</td><td>${n(r.energy?.unknown_kwh)} kWh</td><td>${n(r.energy?.unallocated_discrepancy_kwh)} kWh</td>
        <td>${n(r.energy?.pv_kwh)} kWh</td><td>${n(r.energy?.import_kwh)} kWh</td><td>${n(r.energy?.export_kwh)} kWh</td><td>${n(r.energy?.self_sufficiency_percent)}%</td><td>${this.quality(r)}</td></tr>`).join("")}
    </tbody></table></div>`;
  }

  overview() {
    const selected = this.selectedRecord();
    const isToday = this.historyPreset === "today";
    const comparison = isToday ? this.overviewData?.yesterday_same_cutoff?.energy : null;
    const full = isToday ? this.overviewData?.yesterday_full?.energy : null;
    const hourlyCoverage=this.overviewData?.today_interval_coverage;
    const status = isToday ? (this.overviewData?.stale ? `Partial/stale — ${this.refreshError || "hourly coverage incomplete"}` : "Actual through latest completed hour") : (selected?.provisional&&selected?.finalized===false?"Provisional completed day — authoritative daily close pending":"Validated selected-period summary");
    const metric = (label, object) => label === "Whole-House Electricity" ? object?.household_kwh : label === "Heating Electricity" ? object?.space_heating_kwh : label === "DHW Electricity" ? object?.dhw_kwh : label === "Heating + DHW" ? object?.heating_and_dhw_kwh : label === "Cooling Electricity" ? object?.cooling_kwh : label === "PV Generation" ? object?.pv_kwh : object?.self_sufficiency_percent;
    return `${this.periodControls()}<div class="toolbar"><div><b>${esc(this.selectedPeriod?.label || "Today")} <span class="quality ${this.historyCoverage?.complete?"good":"partial"}">${this.historyCoverage?.complete?"Complete":"Partial"}</span></b><span>${esc(status)}${isToday?` · ${n(hourlyCoverage?.coverage_percent,0)}% hourly coverage · cutoff ${esc(this.overviewData?.cutoff_local || "—")} (${esc(this.overviewData?.timezone || "HA timezone")})`:""} · updated ${esc(selected?.updated_at || "—")}</span>${isToday&&hourlyCoverage?.missing_intervals?.length?`<span class="warning">Missing: ${hourlyCoverage.missing_intervals.map((item)=>esc(item.start)).join(", ")}</span>`:""}</div><button class="refresh">Refresh now</button></div>
    <section class="kpis">${this.kpis().map(([icon, label, value, color, unit = "kWh", known]) => {const shown=value??known;return `
      <article class="card kpi"><div class="kpi-icon ${color}"><ha-icon icon="${icon}"></ha-icon></div>
      <div><span>${label}</span><strong>${n(shown)} ${shown == null ? "" : unit}</strong>
      ${value==null&&known!=null?`<small><b>Known/provisional subtotal</b> · complete total unavailable</small>`:""}${isToday&&shown!=null&&(selected?.energy?.provisional||value==null)?`<small><b>Known/provisional through cutoff</b> · ${esc(this.overviewData?.cutoff_local || "latest completed hour")} ${esc(this.overviewData?.timezone || "")}</small>`:""}<small>${isToday?this.comparisonText(value, metric(label, comparison), metric(label, full), unit):`Selected period · ${esc(this.selectedPeriod?.start_date || "—")}–${esc(this.selectedPeriod?.end_date || "—")}`}</small></div></article>`;}).join("")}</section>
    <section class="grid top-grid">
      <article class="card wide"><h2><ha-icon icon="mdi:home-thermometer-outline"></ha-icon>Heating Energy vs Outdoor Temperature</h2>${this.scatterChart()}</article>
      <article class="card"><h2><ha-icon icon="mdi:chart-box-outline"></ha-icon>Daily Energy Split</h2>${this.splitChart()}</article>
      <article class="card status-card"><h2><ha-icon icon="mdi:gauge"></ha-icon>Ecodan COP & Efficiency</h2><div class="upcoming"><ha-icon icon="mdi:chart-scatter-plot"></ha-icon><b>${this.v02Report?.ecodan_space_heating?.some((point)=>point.heating_cop!=null)?"Measured interval evidence available":"Awaiting matched thermal evidence"}</b><span>Only Ecodan measured thermal energy and exactly matched electricity are used. AC COP is never inferred.</span></div></article>
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
    return `<div class="table-wrap"><table><thead><tr><th>Equipment</th><th>Meter</th><th>Confirmed heating</th><th>Assumed/selected/estimated</th><th>DHW</th><th>Cooling confirmed</th><th>Cooling selected</th><th>Cooling estimated</th><th>Idle</th><th>Other HVAC</th><th>Unknown / residual</th><th>Coverage</th><th>Evidence</th><th>Quality</th></tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(r.display_name)}</td><td>${this.evidence(r.meter_kwh,null)}</td><td>${this.evidence(r.confirmed_heating_kwh,r.known_confirmed_heating_kwh)}</td><td>${this.evidence(addNullable(r.assumed_heating_kwh,r.selected_heating_kwh,r.estimated_heating_kwh),addNullable(r.known_assumed_heating_kwh,r.known_selected_heating_kwh,r.known_estimated_heating_kwh))}</td><td>${this.evidence(r.dhw_kwh,r.known_dhw_kwh)}</td><td>${this.evidence(r.confirmed_cooling_kwh,r.known_confirmed_cooling_kwh)}</td><td>${this.evidence(r.selected_cooling_kwh,r.known_selected_cooling_kwh)}</td><td>${this.evidence(r.estimated_cooling_kwh,r.known_estimated_cooling_kwh)}</td><td>${this.evidence(r.idle_kwh,r.known_idle_kwh)}</td><td>${this.evidence(r.other_hvac_kwh,r.known_other_hvac_kwh)}</td><td>${n(r.unknown_kwh)} / ${n(r.residual_kwh)} kWh</td><td>${r.coverage_percent==null?"Unknown":n(r.coverage_percent,0)+"%"}</td><td>${esc(r.classification_source||r.source||"Unavailable")}</td><td>${this.quality(r)}</td></tr>`).join("")}</tbody></table></div><div class="equipment-charts"><article><h3>Heating by equipment</h3>${this.equipmentComparison("heating")}</article><article><h3>Cooling by equipment</h3>${this.equipmentComparison("cooling")}</article></div>`;
  }

  equipmentComparison(category) {
    const rows=this.selectedRecord()?.energy?.equipment || [];
    const available=rows.filter((row)=>row.available&&row.classification_complete);
    const incomplete=rows.filter((row)=>row.available&&!row.classification_complete);
    const values=available.map((row)=>category==="heating"?(row.confirmed_heating_kwh||0)+(row.assumed_heating_kwh||0)+(row.selected_heating_kwh||0)+(row.estimated_heating_kwh||0):(row.cooling_kwh||0));
    const max=Math.max(0,...values);
    if(!available.length||max===0)return `<p class="muted">No complete validated ${category} allocation.${incomplete.length?" Partial device meters remain in the evidence table and are not rendered as zero.":""}${rows.some((row)=>!row.available)?" Some equipment is unavailable.":""}</p>`;
    return `<div class="device-bars">${available.map((row,index)=>`<div><span>${esc(row.display_name)} ${this.quality(row)}</span><i><b class="${category==='heating'?'heat':'cool'}" style="width:${100*values[index]/max}%" title="${n(values[index])} kWh · ${esc(row.quality)}"></b></i><strong>${n(values[index])} kWh</strong></div>`).join("")}</div>${incomplete.length?`<p class="warning">Partial classification excluded from bars: ${incomplete.map((row)=>esc(row.display_name)).join(", ")}. Meter and unknown energy remain in the table.</p>`:""}${rows.some((row)=>!row.available)?'<p class="warning">Unavailable equipment is excluded; total remains partial.</p>':""}`;
  }

  temperatureCard(role) {
    const source = this.representativeSource(role);
    const label = role === "indoor" ? "Indoor" : "Outdoor";
    if (!source) return `<article class="card temperature-card"><h2><ha-icon icon="${role === "indoor" ? "mdi:home-thermometer" : "mdi:thermometer-lines"}"></ha-icon>Representative ${label} Temperature</h2><div class="not-configured">Not configured</div><p class="muted">Select one ${role === "indoor" ? "indoor location" : "measured outdoor-air sensor"} in Settings → Devices & services → Home Energy Intelligence → Configure. No coverage percentage is inferred.</p></article>`;
    const today = source ? this.selectedRecord()?.temperatures?.[source.source_id] : null;
    const yesterday = source ? this.overviewData?.yesterday_full?.temperatures?.[source.source_id] : null;
    const diagnostic=(this.overviewData?.temperature_pipeline||[]).find((item)=>item.source_id===source.source_id)||{};
    const explanation = role === "indoor" ? "Selected location; not a whole-house mean." : "Selected measured source; not weather-provider data.";
    return `<article class="card temperature-card"><h2><ha-icon icon="${role === "indoor" ? "mdi:home-thermometer" : "mdi:thermometer-lines"}"></ha-icon>Representative ${label} Temperature</h2>
      <div class="temperature">${n(today?.latest_value)}${today?.latest_value == null ? "" : " °C"}</div>
      <p class="muted">Source: ${esc(source.entity_id || source.source_id)} · latest valid: ${esc(today?.latest_timestamp || "unavailable")} · ${esc(explanation)}</p>
      <div class="mini-stats"><span>Period mean <b>${n(today?.mean)} °C</b></span>${this.historyPreset==="today"?`<span>Yesterday mean <b>${n(yesterday?.mean)} °C</b></span>`:""}<span>Min / max <b>${n(today?.minimum)} / ${n(today?.maximum)} °C</b></span><span>Coverage <b>${today?.coverage_percent==null?"Unavailable":n(today.coverage_percent,0)+"%"}</b></span><span>Quality <b>${esc(today ? this.temperatureQualityLabel(today) : (diagnostic.pipeline_stage || "missing"))}</b></span></div>${today?.mean==null?`<p class="warning">Pipeline: ${esc(diagnostic.pipeline_stage||"not yet diagnosed").replaceAll("_"," ")} · live ${esc(diagnostic.live_state??"unavailable")} ${esc(diagnostic.live_unit||"")} · last update ${esc(diagnostic.live_last_updated||"unavailable")} · persisted hourly summaries ${n(diagnostic.persisted_hourly_summaries,0)}</p>`:""}${this.temperatureTimeline(role)}</article>`;
  }

  comparisonText(today, same, full, unit) {
    if (today == null || same == null) return `Yesterday same cutoff unavailable · full ${n(full)} ${full == null ? "" : unit}`;
    const difference = today-same; const percent = same>0 ? 100*difference/same : null;
    return `Yesterday same cutoff ${n(same)} ${unit} · Δ ${difference>=0?"+":""}${n(difference)} ${unit}${percent==null?"":` (${percent>=0?"+":""}${n(percent)}%)`} · full ${n(full)} ${full==null?"":unit}`;
  }

  temperatureTimeline(role = null) {
    const source = role ? this.representativeSource(role) : null;
    if (role && !source) return `<div class="empty">No representative source configured.</div>`;
    const timeline = this.temperatureTimelineData;
    if (this.temperatureTimelineError) return `<div class="empty">Temperature timeline unavailable: ${esc(this.temperatureTimelineError)}</div>`;
    if (!timeline) return `<div class="empty">Temperature timeline has not loaded.</div>`;
    const sourceIds = role ? [source.source_id] : timeline.source_ids;
    const sourceById = Object.fromEntries((timeline.sources || []).map((item) => [item.source_id, item]));
    const values = timeline.intervals.flatMap((row) => sourceIds.flatMap((sourceId) => {
      const item = row.temperatures?.[sourceId];
      return [item?.minimum, item?.mean, item?.maximum]
        .filter((value) => value != null && Number.isFinite(Number(value))).map(Number);
    }));
    if (!values.length) {
      const daily = role ? this.selectedRecord()?.temperatures?.[source.source_id] : null;
      const explanation = timeline.resolution === "hourly" && daily?.mean != null
        ? "Daily summary available, but hourly temperature history has not been archived/backfilled. Use Settings diagnostics and bounded temperature-only backfill after verifying historical mappings."
        : "No validated temperature measurements are archived for this period.";
      return `<div class="empty">${esc(explanation)}</div>`;
    }
    const measuredLow = Math.min(...values), measuredHigh = Math.max(...values);
    const measuredSpan = measuredHigh-measuredLow;
    const domainPadding = measuredSpan > 0 ? Math.max(.5, measuredSpan*.08) : 1;
    const low = measuredLow-domainPadding, high = measuredHigh+domainPadding;
    const span = high-low;
    const chartWidth=320,chartHeight=190,plotLeft=48,plotRight=308,plotTop=12,plotBottom=148;
    const dataLeft=plotLeft+8,dataRight=plotRight-8;
    const rangeStart = Date.parse(timeline.range_start_utc), rangeEnd = Date.parse(timeline.range_end_utc);
    const clamp=(value,minimum,maximum)=>Math.max(minimum,Math.min(maximum,value));
    const xFor = (timestamp) => clamp(dataLeft+(dataRight-dataLeft)*(Date.parse(timestamp)-rangeStart)/Math.max(1,rangeEnd-rangeStart),dataLeft,dataRight);
    const yFor = (value) => clamp(plotBottom-(Number(value)-low)*(plotBottom-plotTop)/span,plotTop,plotBottom);
    const yAxis=this.axisTicks(low,high).map((value)=>`<g><line x1="${plotLeft}" x2="${plotRight}" y1="${yFor(value)}" y2="${yFor(value)}" class="temp-grid"></line><text x="${plotLeft-6}" y="${yFor(value)+3}" text-anchor="end">${n(value,1)} °C</text></g>`).join("");
    const rendered = sourceIds.map((sourceId, sourceIndex) => {
      const segments=[]; let points=[];
      timeline.intervals.forEach((row) => {
        const item=row.temperatures?.[sourceId];
        if(item?.mean==null||!Number.isFinite(Number(item.mean))){if(points.length){segments.push(points);points=[];}return;}
        points.push(`${xFor(row.start_utc)},${yFor(item.mean)}`);
      });
      if(points.length)segments.push(points);
      const sourceInfo=sourceById[sourceId]||{};
      const markers=timeline.intervals.map((row) => {
        const item=row.temperatures?.[sourceId];if(item?.mean==null||!Number.isFinite(Number(item.mean)))return "";
        const x=xFor(row.start_utc),y=yFor(item.mean);
        const detail=`${row.local_label} ${timeline.timezone} · ${n(item.mean)} °C · min ${n(item.minimum)} · max ${n(item.maximum)} · coverage ${n(item.coverage_percent,1)}% (${n(item.valid_seconds,0)}/${n(item.expected_seconds,0)} s) · ${this.temperatureQualityLabel(item)} · ${sourceInfo.display_name||sourceId} · ${sourceInfo.entity_id||sourceId}`;
        const range=item.minimum!=null&&item.maximum!=null&&Number.isFinite(Number(item.minimum))&&Number.isFinite(Number(item.maximum))?`<line class="temp-range t${sourceIndex%4}" x1="${x}" x2="${x}" y1="${yFor(item.minimum)}" y2="${yFor(item.maximum)}"></line>`:"";
        return `${range}<g tabindex="0" role="button" aria-label="${esc(detail)}"><circle class="temp-hit" cx="${x}" cy="${y}" r="8"></circle><circle class="temp-point t${sourceIndex%4}" cx="${x}" cy="${y}" r="3"><title>${esc(detail)}</title></circle></g>`;
      }).join("");
      return `${segments.map((segment)=>segment.length>1?`<polyline points="${segment.join(" ")}" class="temp-line t${sourceIndex%4}"></polyline>`:"").join("")}${markers}`;
    }).join("");
    const allTicks=timeline.axis_ticks||[];
    const tickStride=Math.max(1,Math.ceil((allTicks.length-1)/4));
    const visibleTicks=allTicks.filter((_tick,index)=>index===0||index===allTicks.length-1||index%tickStride===0);
    const ticks=visibleTicks.map((tick,index,array)=>`<text x="${xFor(tick.timestamp_utc)}" y="172" text-anchor="${index===0?"start":index===array.length-1?"end":"middle"}">${esc(tick.label)}</text>`).join("");
    const label=`${timeline.start_date}${timeline.start_date===timeline.end_date?"":`–${timeline.end_date}`} · ${timeline.timezone} · ${timeline.resolution}`;
    const clipId=`hei-temp-plot-${++this.temperatureChartSequence}`;
    return `<div class="temp-chart-shell"><svg class="temp-chart" viewBox="0 0 ${chartWidth} ${chartHeight}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Validated ${esc(timeline.resolution)} temperature timeline for ${esc(label)}"><defs><clipPath id="${clipId}"><rect x="${plotLeft}" y="${plotTop}" width="${plotRight-plotLeft}" height="${plotBottom-plotTop}"></rect></clipPath></defs>${yAxis}<line class="temp-axis" x1="${plotLeft}" x2="${plotRight}" y1="${plotBottom}" y2="${plotBottom}"></line><g class="temp-plot" clip-path="url(#${clipId})">${rendered}</g>${ticks}</svg></div><p class="muted temp-chart-caption">${esc(label)} · ${timeline.available_interval_count}/${timeline.expected_interval_count} intervals contain validated evidence. Gaps are not interpolated. Focus or tap a point for source, min/max, coverage and quality.</p>`;
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
    return this.temperatureTimeline(role);
  }

  pvHistoryChart() {
    const records=[...this.history].reverse();
    const series=[["PV","pv_kwh","pv"],["Import","import_kwh","grid-import"],["Export","export_kwh","grid-export"],["PV self-use","pv_consumed_on_site_kwh","self-use"],["Household","household_kwh","household"]];
    const values=records.flatMap((record)=>series.map(([,key])=>record.energy?.[key]).filter((value)=>value!=null));
    if(!values.length)return `<div class="empty">PV/grid sources are unavailable for this period.</div>`;
    const max=Math.max(1,...values),firstDay=Date.parse(records[0].local_date+"T00:00:00Z"),lastDay=Date.parse(records.at(-1).local_date+"T00:00:00Z"),daySpan=Math.max(1,Math.round((lastDay-firstDay)/86400000)),width=Math.max(2,240/Math.max(1,daySpan+1));
    const y=(value)=>95-70*Number(value)/max;const axis=this.axisTicks(0,max).map((value)=>`<g><line x1="38" x2="292" y1="${y(value)}" y2="${y(value)}"/><text x="2" y="${y(value)+3}">${n(value,1)} kWh</text></g>`).join("");const dateTicks=[records[0],records.at(-1)].map((record,index)=>`<text x="${index?292:38}" y="116" text-anchor="${index?"end":"start"}">${esc(record.local_date)}</text>`).join("");
    return `<svg class="pv-chart" viewBox="0 0 300 120" role="img" aria-label="Daily measured PV, import, export, self-use and household electricity with numeric kWh axis">${axis}${dateTicks}${records.map((record)=>{const day=Math.round((Date.parse(record.local_date+"T00:00:00Z")-firstDay)/86400000);return series.map(([label,key,css],seriesIndex)=>{const value=record.energy?.[key];if(value==null)return "";return `<rect class="${css}" x="${38+day*250/daySpan}" y="${y(value)}" width="${width/series.length}" height="${70*value/max}" transform="translate(${seriesIndex*width/series.length} 0)"><title>${esc(record.local_date)} · ${label}: ${n(value)} kWh · ${esc(record.quality)}</title></rect>`;}).join("");}).join("")}</svg><div class="legend">${series.map(([label,,css])=>`<i class="dot ${css}"></i>${label}`).join(" ")}<span>Self-sufficiency ${n(this.historySummary?.energy?.self_sufficiency_percent)}% · PV self-consumption ${n(this.historySummary?.energy?.pv_self_consumption_percent)}%</span></div>`;
  }

  periodSummary() {
    const energy = this.historySummary?.energy;
    const coverage=this.historyCoverage || this.historySummary?.period_coverage;
    const dates=coverage?.date_coverage; const meter=coverage?.meter_coverage; const allocation=coverage?.allocation_coverage;
    const representativeCoverage=(role)=>{const source=this.representativeSource(role);if(!source)return "Not configured";const item=coverage?.temperature_coverage?.[source.source_id];return item?`${n(item.coverage_percent,0)}%` : "Unavailable";};
    const layer=(name,item,percent)=>`<span>${esc(name)} ${percent} <b class="quality ${esc(item?.quality || (item?.complete?"good":"partial"))}">${esc(item?.quality || (item?.complete?"good":"partial"))}</b></span>`;
    const temperatureLayer=(role)=>{const source=this.representativeSource(role);if(!source)return `<span>${esc(role)} not configured</span>`;const item=coverage?.temperature_coverage?.[source.source_id];return layer(role,item,representativeCoverage(role));};
    const heading=`<div class="quality-layers"><b>${esc(this.selectedPeriod?.label || "Selected period")}</b><span>${esc(this.selectedPeriod?.start_date || "—")}–${esc(this.selectedPeriod?.end_date || "—")}</span>${layer("dates",dates,n(dates?.coverage_percent ?? coverage?.coverage_percent,0)+"%")}${layer("meter",meter,n(meter?.coverage_percent,0)+"%")}${layer("allocation",allocation,allocation?.coverage_percent==null?"unknown":n(allocation.coverage_percent,0)+"%")} ${temperatureLayer("indoor")} ${temperatureLayer("outdoor")} ${this.quality({quality:coverage?.quality||"missing",quality_basis:coverage?.quality_basis})}</div>`;
    if(!energy)return `${heading}<div class="empty">No complete daily records in the selected period.</div>`;
    return `${heading}<div class="mini-stats period-summary"><span>Household <b>${n(energy.household_kwh)} kWh</b><small>Known ${n(this.historySummary?.known_household_kwh)} kWh</small></span><span>Heating <b>${n(energy.space_heating_kwh)} kWh</b><small>Known ${n(energy.known_space_heating_kwh)} kWh</small></span><span>DHW <b>${n(energy.dhw_kwh)} kWh</b></span><span>Heating + DHW <b>${n(energy.heating_and_dhw_kwh)} kWh</b></span><span>Cooling <b>${n(energy.cooling_kwh)} kWh</b></span><span>PV <b>${n(energy.pv_kwh)} kWh</b></span><span>Import / export <b>${n(energy.import_kwh)} / ${n(energy.export_kwh)} kWh</b></span></div>${coverage?.missing_dates?.length?`<p class="warning">Missing dates: ${coverage.missing_dates.map(esc).join(", ")}</p>`:""}`;
  }

  settingsView() {
    if (!this.settings) return `<section class="card page-card"><h2>Administrator access required</h2><p>Overview and History remain available. Integration mappings and database details are restricted to administrators.</p></section>`;
    if (!this.settings.configured) return `<section class="card page-card"><h2>Setup required</h2><p>Add the integration from Settings → Devices & services.</p></section>`;
    const representative=(role)=>{const source=this.representativeSource(role);const item=source?this.selectedRecord()?.temperatures?.[source.source_id]:null;return source?`${esc(source.display_name)} · ${n(item?.latest_value)}${item?.latest_value==null?"":" °C"} · ${esc(item?.quality||"unavailable")} · ${esc(item?.latest_timestamp||"no validated timestamp")}`:"Not configured";};
    const diagnosticRows=(this.temperatureDiagnostics?.sources||[]).map((row)=>`<tr><td>${esc(row.display_name)}<br>${row.configured&&row.enabled?"configured · enabled":"disabled or unavailable"}</td><td><code>${esc(row.source_id)}</code><br><code>${esc(row.entity_id)}</code><br>${row.attribute?`attribute <code>${esc(row.attribute)}</code>`:"entity state"}</td><td>${esc(row.live_state??"unavailable")} ${esc(row.live_unit||"")}<br>${esc(row.live_last_updated||"no update")}<br>${row.attribute_available?"attribute valid":"attribute unavailable"} · ${esc(row.unit_validation||"unknown unit")}</td><td>${row.recorder_state_rows||0} states / ${row.recorder_statistics_rows||0} statistics<br>${esc(row.available_start||"—")}–${esc(row.available_end||"—")}<br>${n(row.validated_coverage_seconds,0)} validated seconds</td><td>${row.persisted_valid_hourly_summaries||0} hourly / ${row.persisted_valid_daily_summaries||0} daily<br>${row.temperature_only_hourly_summaries||0} / ${row.temperature_only_daily_summaries||0} temperature-only<br>${n(row.persisted_hourly_coverage_percent,1)}% · ${row.missing_interval_count||0} missing</td><td>${esc(row.pipeline_stage).replaceAll("_"," ")}<br>${esc(row.mapping_eligibility?.reason||"mapping checked").replaceAll("_"," ")}<br>${row.recoverable_missing_interval_count||0} recoverable / ${row.nonrecoverable_interval_count||0} blocked</td><td>${esc((row.invalid_reasons||[]).join(", ")||"—")}<br>${esc((row.rejected_samples||[]).map((item)=>item.reason).join(", ")||"—")}</td></tr>`).join("");
    const diagnosticSetup=this.temperatureDiagnostics?.configuration_status==="not_configured"?'<p class="warning">No temperature source is configured. <a href="/config/integrations/integration/home_energy_intelligence">Open integration options</a> to add one; no source will be inferred.</p>':"";
    const localToday=String(this.overviewData?.cutoff_local||"").slice(0,10);
    const startDefault=this.temperatureBackfillJob?.start_date||this.temperatureDiagnostics?.requested_start||localToday;
    const endDefault=this.temperatureBackfillJob?.end_date||this.temperatureDiagnostics?.requested_end||localToday;
    const job=this.temperatureBackfillJob; const jobDetails=job?.details||job||{};
    const jobOutcome=jobDetails.outcome||job?.status;
    const jobOutcomeLabel={records_recovered:"Records recovered",no_changes_needed:"No changes needed",no_records_recovered_action_required:"No records recovered — action required",preview_no_recoverable_records_action_required:"No recoverable records in preview — action required",preview_ready:"Preview ready"}[jobOutcome]||String(jobOutcome||"queued").replaceAll("_"," ");
    const jobClass=["records_recovered","no_changes_needed","completed"].includes(jobOutcome)?"good":["failed","no_records_recovered_action_required","preview_no_recoverable_records_action_required"].includes(jobOutcome)?"missing":"partial";
    const reasonRows=(jobDetails.skip_reasons||[]).slice(-25).map((row)=>`<li>${esc(row.date||row.interval_start_utc||"interval")} · ${esc(row.source_id||"source")} · ${esc(row.reason||row.error||"skipped").replaceAll("_"," ")}</li>`).join("");
    const repairChanges=Number(this.temperatureRepairResult?.changed_hourly_records||0)+Number(this.temperatureRepairResult?.changed_daily_records||0);
    const repairOutstanding=(this.temperatureDiagnostics?.sources||[]).reduce((total,row)=>total+Number(row.missing_interval_count||0),0);
    const repairActionRequired=Boolean(this.temperatureRepairResult&&repairChanges===0&&repairOutstanding>0);
    const jobResult=job?`<div class="backfill-result"><p class="quality ${jobClass}">${esc(jobOutcomeLabel)}</p><p>Status ${esc(job.status||"unknown")}${job.cancel_requested?" · cancellation requested":""} · processed ${jobDetails.processed_days||0} day(s) · ${job.dry_run?"would insert":"inserted"} ${jobDetails.inserted_hourly||0} hourly / ${jobDetails.inserted_daily||0} daily · ${job.dry_run?"would update":"updated"} ${jobDetails.updated_hourly||0} / ${jobDetails.updated_daily||0} · unchanged ${jobDetails.unchanged_hourly||0} / ${jobDetails.unchanged_daily||0} · skipped ${jobDetails.skipped||0} · failed ${jobDetails.failed||0}.</p><p>Before ${esc(JSON.stringify(jobDetails.temperature_summaries_before||{}))} · after ${esc(JSON.stringify(jobDetails.temperature_summaries_after||{}))} · net ${esc(JSON.stringify(jobDetails.net_new_temperature_summaries||{}))}.</p>${reasonRows?`<details><summary>Skipped and blocked intervals</summary><ul>${reasonRows}</ul></details>`:""}${["queued","running"].includes(job.status)&&!job.cancel_requested?'<button class="primary temperature-backfill-cancel">Cancel backfill</button>':""}</div>`:"";
    return `<section class="settings-grid">
      <article class="card page-card"><h2>Processing</h2><dl><dt>Time zone</dt><dd>${esc(this.settings.timezone)}</dd><dt>Cycle</dt><dd>${esc(this.settings.cycle_hours)} hours</dd><dt>Floor area</dt><dd>${n(this.settings.floor_area_m2, 0)} m²</dd><dt>Database</dt><dd>${n(this.settings.database_size_bytes / 1024, 0)} KiB</dd><dt>Schema</dt><dd>v${esc(this.settings.storage?.schema_version)} / v${esc(this.settings.storage?.target_schema_version)}</dd><dt>Startup compaction</dt><dd>${this.settings.storage?.startup_vacuum_enabled ? "Enabled" : "Disabled"}</dd><dt>Backup policy</dt><dd>Protected SQLite backup recommended before migration</dd><dt>InfluxDB backfill</dt><dd>${this.settings.influx_enabled ? "Enabled (read-only)" : "Disabled"}</dd></dl>
      <a class="primary" href="/config/integrations/integration/home_energy_intelligence">Open integration options</a></article>
      <article class="card page-card"><h2>Entity mapping</h2><div class="mapping">${this.settings.mapping.map((row) => `<div><span>${esc(row.label)}</span><code>${row.entity_id ? esc(row.entity_id) : "Not selected"}</code></div>`).join("")}</div></article>
      <article class="card page-card notice"><h2>Representative temperatures</h2><p><b>Indoor:</b> ${representative("indoor")}</p><p><b>Measured outdoor:</b> ${representative("outdoor")}</p><p class="muted">Selections are independent. A weather provider or water-circuit sensor is never substituted.</p></article>
      <article class="card page-card notice"><h2>Temperature pipeline diagnostics and recovery</h2><p>Choose 1–31 local dates. Diagnosis is read-only. “Repair existing summaries” only updates energy records that already exist; temperature-only backfill can create independent temperature history without creating electricity.</p><div class="backfill-controls"><label>Start <input class="temperature-start" type="date" value="${esc(startDefault)}"></label><label>End <input class="temperature-end" type="date" value="${esc(endDefault)}"></label><label><input class="temperature-attest" type="checkbox"> I attest the current source mapping applied throughout this range</label></div><p><button class="primary temperature-diagnostics">Diagnose</button> <button class="primary temperature-repair">Repair existing summaries</button> <button class="primary temperature-backfill-preview">Preview temperature-only backfill</button> <button class="primary temperature-backfill-start">Start temperature-only backfill</button></p>${this.temperatureRepairResult?`<p class="quality ${repairActionRequired||this.temperatureRepairResult.error_count?"missing":this.temperatureRepairResult.skipped_repair_count?"partial":"good"}">${repairActionRequired?"No records recovered — action required. ":""}Processed ${this.temperatureRepairResult.processed_days} day(s); updated ${this.temperatureRepairResult.changed_hourly_records} hourly and ${this.temperatureRepairResult.changed_daily_records} daily record(s); skipped ${this.temperatureRepairResult.sources_skipped_complete||0} complete source/day(s); blocked ${this.temperatureRepairResult.skipped_repair_count||0}; ${this.temperatureRepairResult.error_count||0} error(s).</p>`:""}${jobResult}${diagnosticSetup}${this.temperatureDiagnostics?`<div class="table-wrap"><table><thead><tr><th>Source</th><th>IDs</th><th>Live</th><th>Recorder evidence</th><th>Persisted</th><th>Stage / mapping</th><th>Invalid/rejected</th></tr></thead><tbody>${diagnosticRows}</tbody></table></div><p class="muted">Expected ${this.temperatureDiagnostics.expected_hourly_intervals||0} completed intervals; Recorder queries ${this.temperatureDiagnostics.estimated_recorder_query_count||0}; energy rows ${esc(JSON.stringify(this.temperatureDiagnostics.energy_history_records||{}))}; temperature-only rows ${esc(JSON.stringify(this.temperatureDiagnostics.temperature_only_history_records||{}))}. Missing details are capped at 200 displayed intervals per source. Future hours are never counted.</p>`:'<p class="muted">Diagnose to locate the first missing stage before writing any summaries.</p>'}</article>
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
    if (this.active === "cop") return this.copView();
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
    this.shadowRoot.querySelector(".older-month")?.addEventListener("input", (event) => { this.historyOlderMonth=event.target.value; });
    this.shadowRoot.querySelector(".start-date")?.addEventListener("input", (event) => { this.historyStart=event.target.value; });
    this.shadowRoot.querySelector(".end-date")?.addEventListener("input", (event) => { this.historyEnd=event.target.value; });
    this.shadowRoot.querySelector(".next")?.addEventListener("click", () => { this.historyOffset += 90; this.loadPeriod(); });
    this.shadowRoot.querySelector(".previous")?.addEventListener("click", () => { this.historyOffset = Math.max(0, this.historyOffset - 90); this.loadPeriod(); });
    this.shadowRoot.querySelector(".temperature-diagnostics")?.addEventListener("click", () => this.loadTemperatureDiagnostics());
    this.shadowRoot.querySelector(".temperature-repair")?.addEventListener("click", () => this.repairTemperatureHistory());
    this.shadowRoot.querySelector(".temperature-backfill-preview")?.addEventListener("click", () => this.startTemperatureBackfill(true));
    this.shadowRoot.querySelector(".temperature-backfill-start")?.addEventListener("click", () => this.startTemperatureBackfill(false));
    this.shadowRoot.querySelector(".temperature-backfill-cancel")?.addEventListener("click", () => this.cancelTemperatureBackfill());
    this.shadowRoot.querySelector(".thermal-form")?.addEventListener("submit", (event) => this.saveThermalReading(event));
    this.shadowRoot.querySelectorAll(".thermal-edit").forEach((button)=>button.addEventListener("click",()=>{this.thermalEdit=(this.thermalLedger?.readings||[]).find((row)=>row.id===Number(button.dataset.id))||null;this.render();}));
    this.shadowRoot.querySelectorAll(".thermal-delete").forEach((button)=>button.addEventListener("click",()=>this.deleteThermalReading(Number(button.dataset.id))));
    this.shadowRoot.querySelector(".thermal-backfill")?.addEventListener("click",()=>this.backfillThermalReadings());
    this.shadowRoot.querySelector(".monthly-apply")?.addEventListener("click",()=>{this.monthlyEndMonth=this.shadowRoot.querySelector(".monthly-end")?.value||this.monthlyEndMonth;this.loadMonthly();});
    this.shadowRoot.querySelectorAll(".monthly-metric").forEach((checkbox)=>checkbox.addEventListener("change",()=>{this.monthlyMetricIds=[...this.shadowRoot.querySelectorAll(".monthly-metric:checked")].map((item)=>item.value);this.render();}));
    this.shadowRoot.querySelector(".monthly-csv")?.addEventListener("click",()=>this.exportMonthlyCsv());
    this.shadowRoot.querySelector(".monthly-form")?.addEventListener("submit",(event)=>this.saveMonthlySnapshot(event));
    this.shadowRoot.querySelectorAll(".monthly-edit").forEach((button)=>button.addEventListener("click",()=>{this.monthlyEdit=(this.monthlyLedger?.snapshots||[]).find((row)=>row.id===Number(button.dataset.id))||null;this.render();}));
    this.shadowRoot.querySelectorAll(".monthly-delete").forEach((button)=>button.addEventListener("click",()=>this.deleteMonthlySnapshot(Number(button.dataset.id))));
  }

  async loadPeriod() {
    this.historyPreset = this.shadowRoot.querySelector(".period-select")?.value || "today";
    this.historyStart = this.shadowRoot.querySelector(".start-date")?.value || null;
    this.historyEnd = this.shadowRoot.querySelector(".end-date")?.value || null;
    this.historyOlderMonth = this.shadowRoot.querySelector(".older-month")?.value || null;
    try {
      const response = await this._hass.callWS(this.historyRequest());
      if ((response.total || 0) > 0 && !response.records?.length && this.historyOffset > 0) {
        this.historyOffset=Math.floor(((response.total||1)-1)/90)*90;
        return this.loadPeriod();
      }
      this.history = response.records || []; this.historySummary = response.summary;
      this.historyTotal = response.total || 0;
      this.historyCoverage = response.period_coverage;
      this.responseProfileData = response.response_profile;
      this.selectedPeriod = response.selected_period;
      await this.loadTemperatureTimeline(response, this.overviewData, false);
      await this.loadV02(response, false);
      this.refreshError=null; this.render();
    } catch(error) { this.refreshError=error?.message || String(error); this.render(); }
  }

  async saveThermalReading(event) {
    event.preventDefault();const form=new FormData(event.currentTarget);const payload={type:"home_energy_intelligence/save_thermal_reading"};
    for(const [key,value] of form.entries()){if(value!=="")payload[key]=["reading_id","fold"].includes(key)?Number(value):value;}
    try{await this._hass.callWS(payload);this.thermalEdit=null;await this.loadV02(null,false);this.refreshError=null;this.render();}
    catch(error){this.refreshError=error?.message||String(error);this.render();}
  }

  async deleteThermalReading(readingId) {
    if(!window.confirm("Delete this cumulative reading? The deletion remains in the audit ledger."))return;
    try{await this._hass.callWS({type:"home_energy_intelligence/delete_thermal_reading",reading_id:readingId});this.thermalEdit=null;await this.loadV02(null,false);this.refreshError=null;this.render();}
    catch(error){this.refreshError=error?.message||String(error);this.render();}
  }

  async backfillThermalReadings() {
    const period=this.selectedPeriod;if(!period?.start_date||!period?.end_date)return;
    try{const result=await this._hass.callWS({type:"home_energy_intelligence/backfill_thermal_readings",start_date:period.start_date,end_date:period.end_date});this.refreshError=`Thermal backfill processed ${result.processed_days} day(s), added ${result.new_readings} reading(s), ${result.error_count} error(s).`;await this.loadV02(null,false);this.render();}
    catch(error){this.refreshError=error?.message||String(error);this.render();}
  }

  async saveMonthlySnapshot(event) {
    event.preventDefault();const form=new FormData(event.currentTarget);const payload={type:"home_energy_intelligence/save_monthly_ecodan_snapshot"};
    for(const [key,value] of form.entries()){if(value!=="")payload[key]=["snapshot_id","fold"].includes(key)?Number(value):value;}
    try{await this._hass.callWS(payload);this.monthlyEdit=null;await this.loadMonthly(false);this.refreshError=null;this.render();}
    catch(error){this.refreshError=error?.message||String(error);this.render();}
  }

  async deleteMonthlySnapshot(snapshotId) {
    if(!window.confirm("Delete this monthly snapshot? The deletion remains in the audit ledger."))return;
    try{await this._hass.callWS({type:"home_energy_intelligence/delete_monthly_ecodan_snapshot",snapshot_id:snapshotId});this.monthlyEdit=null;await this.loadMonthly(false);this.refreshError=null;this.render();}
    catch(error){this.refreshError=error?.message||String(error);this.render();}
  }

  async exportMonthlyCsv() {
    if(!this.monthlyMetricIds?.length)return;
    try{const response=await this._hass.callWS({type:"home_energy_intelligence/monthly_report_csv",end_month:this.monthlyEndMonth,count:13,metric_ids:this.monthlyMetricIds});const link=document.createElement("a");link.href=URL.createObjectURL(new Blob([response.csv],{type:"text/csv"}));link.download=response.filename;link.click();URL.revokeObjectURL(link.href);}
    catch(error){this.refreshError=error?.message||String(error);this.render();}
  }

  async refreshNow() {
    try { await this._hass.callWS({type:"home_energy_intelligence/refresh"}); this.refreshError=null; await this.load(); }
    catch(error) { this.refreshError=error?.message || String(error); if(this.overviewData)this.overviewData.stale=true; this.loading=false; this.render(); }
  }

  async loadTemperatureDiagnostics() {
    const range=this.temperatureRecoveryRange();
    try { this.temperatureDiagnostics=await this._hass.callWS({type:"home_energy_intelligence/temperature_diagnostics",...range});this.refreshError=null;this.render(); }
    catch(error){this.refreshError=error?.message||String(error);this.render();}
  }

  async repairTemperatureHistory() {
    const range=this.temperatureRecoveryRange();
    try { this.temperatureRepairResult=await this._hass.callWS({type:"home_energy_intelligence/reprocess_temperature_history",...range});this.temperatureDiagnostics=null;await this.load(false);await this.loadTemperatureDiagnostics(); }
    catch(error){this.refreshError=error?.message||String(error);this.render();}
  }

  temperatureRecoveryRange() {
    return {
      start_date:this.shadowRoot.querySelector(".temperature-start")?.value,
      end_date:this.shadowRoot.querySelector(".temperature-end")?.value,
    };
  }

  async startTemperatureBackfill(dryRun) {
    const range=this.temperatureRecoveryRange();
    const attest=Boolean(this.shadowRoot.querySelector(".temperature-attest")?.checked);
    try {
      this.temperatureBackfillJob=await this._hass.callWS({
        type:`home_energy_intelligence/temperature_backfill_${dryRun?"preview":"start"}`,
        ...range,attest_current_mapping:attest,
      });
      this.refreshError=null;this.render();this.pollTemperatureBackfill();
    } catch(error){this.refreshError=error?.message||String(error);this.render();}
  }

  async pollTemperatureBackfill() {
    if(!this.temperatureBackfillJob?.job_id)return;
    try {
      const job=await this._hass.callWS({type:"home_energy_intelligence/temperature_backfill_status",job_id:this.temperatureBackfillJob.job_id});
      this.temperatureBackfillJob=job;this.render();
      if(["queued","running","interrupted"].includes(job.status)){
        this.temperatureBackfillPolling=window.setTimeout(()=>this.pollTemperatureBackfill(),1000);
      }else{
        this.temperatureDiagnostics=null;await this.load(false);await this.loadTemperatureDiagnostics();
      }
    }catch(error){this.refreshError=error?.message||String(error);this.render();}
  }

  async cancelTemperatureBackfill() {
    if(!this.temperatureBackfillJob?.job_id)return;
    try { this.temperatureBackfillJob=await this._hass.callWS({type:"home_energy_intelligence/temperature_backfill_cancel",job_id:this.temperatureBackfillJob.job_id});this.render(); }
    catch(error){this.refreshError=error?.message||String(error);this.render();}
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
    .tabs{display:flex;gap:20px;border-bottom:1px solid var(--divider-color,#dce4ee);overflow-x:auto}.tab{display:flex;align-items:center;gap:10px;border:0;border-bottom:3px solid transparent;background:none;color:var(--secondary-text-color,#41506a);padding:18px 14px;font-size:15px;white-space:nowrap;cursor:pointer}.tab.active{color:var(--primary-color,#0785db);border-bottom-color:var(--primary-color,#0785db);font-weight:700}.content{padding-top:22px}.toolbar,.period,.pager{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.toolbar>div{display:flex;flex-direction:column}.toolbar span{font-size:12px;color:var(--secondary-text-color,#65728a)}.period{justify-content:flex-start;flex-wrap:wrap;padding:12px;background:var(--secondary-background-color,#f2f6fa);border-radius:8px}.period label{display:flex;gap:6px;align-items:center}.card{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#e0e7ef);border-radius:12px;box-shadow:0 3px 12px rgba(35,65,100,.05);padding:18px;min-width:0}.card h2{display:flex;align-items:center;gap:10px;font-size:17px;margin:0 0 16px}.card h2 ha-icon{color:#233b5b}.kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-bottom:18px}.kpi{display:flex;gap:13px;align-items:flex-start}.kpi-icon{display:grid;place-items:center;width:48px;height:48px;border-radius:50%;flex:none}.kpi-icon.blue{color:#0785db;background:#e7f4fe}.kpi-icon.red{color:#ef4444;background:#fff0f0}.kpi-icon.orange{color:#f59e0b;background:#fff6e5}.kpi-icon.amber{color:#f6a800;background:#fff8df}.kpi-icon.green{color:#159b5b;background:#eaf9f1}.kpi-icon ha-icon{--mdc-icon-size:27px}.kpi>div:last-child{display:flex;flex-direction:column;min-width:0}.kpi span{font-size:14px;line-height:1.3}.kpi strong{font-size:24px;margin-top:3px;white-space:nowrap}.kpi small{color:var(--secondary-text-color,#65728a);margin-top:7px}.grid{display:grid;gap:14px;margin-bottom:14px}.top-grid{grid-template-columns:1.3fr 1fr .85fr}.bottom-grid{grid-template-columns:1fr .95fr 1.35fr}.wide{min-width:0}.chart{width:100%;height:220px}.chart line{stroke:var(--divider-color,#dbe4ee);stroke-width:1}.chart text{font-size:11px;fill:var(--secondary-text-color,#66758d)}.chart circle{fill:#46a4ef;stroke:#fff;stroke-width:2}.response-curve circle.electric,.dot.electric{fill:#ef4444;background:#ef4444}.response-curve rect.thermal,.dot.thermal{fill:#159b5b;background:#159b5b}.response-curve .cop-label{font-weight:700;fill:#17233b}.thermal-form{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:16px 0}.thermal-form label{display:flex;flex-direction:column;gap:5px;font-size:12px}.thermal-form input,.thermal-form select{min-height:38px}.temp-chart-shell{width:100%;max-width:100%;margin-top:10px;overflow:hidden}.temp-chart{display:block;width:100%;height:auto;aspect-ratio:320/190;max-height:230px;overflow:hidden}.temp-chart text{font-size:10px;fill:var(--secondary-text-color,#66758d)}.temp-grid,.temp-axis{stroke:var(--divider-color,#dbe4ee);stroke-width:1}.temp-line{fill:none;stroke-width:2}.temp-line.t0,.temp-point.t0,.temp-range.t0{stroke:#ef4444}.temp-line.t1,.temp-point.t1,.temp-range.t1{stroke:#238ee8}.temp-line.t2,.temp-point.t2,.temp-range.t2{stroke:#f59e0b}.temp-line.t3,.temp-point.t3,.temp-range.t3{stroke:#159b5b}.temp-point{fill:var(--card-background-color,#fff);stroke-width:2}.temp-range{stroke-width:1;opacity:.65}.temp-hit{fill:transparent;stroke:transparent;pointer-events:all}.temp-chart g:focus .temp-hit{stroke:var(--primary-color,#0785db);stroke-width:1;outline:none}.temp-chart-caption{margin:8px 0 0;overflow-wrap:anywhere}.bars{height:185px;border-bottom:1px solid var(--divider-color,#dbe4ee);display:flex;align-items:flex-end;justify-content:space-around;gap:10px;padding:10px 10px 0}.bar-col{height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:6px;font-size:11px;color:var(--secondary-text-color,#65728a)}.bar{width:26px;display:flex;flex-direction:column;justify-content:flex-end;border-radius:3px 3px 0 0;overflow:hidden;background:#eef2f6}.seg{display:block;width:100%}.heat{background:#ef4444}.dhw{background:#f59e0b}.cool{background:#238ee8}.idle{background:#8b9bb0}.other{background:#91c788}.unknown{background:#8b5cf6}.legend{display:flex;justify-content:center;align-items:center;gap:7px;font-size:12px;margin-top:12px;flex-wrap:wrap}.dot{width:10px;height:10px;border-radius:2px;margin-left:8px}.upcoming{min-height:150px;border-radius:10px;background:linear-gradient(145deg,#f1f8ff,#e9f4fc);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:8px;color:#2269a5;padding:18px}.upcoming span{font-size:13px;color:#496980}.temperature{font-size:34px;font-weight:700;margin:12px 0}.muted{color:var(--secondary-text-color,#65728a);font-size:13px}.mini-stats{display:flex;gap:20px;border-top:1px solid var(--divider-color,#e0e7ef);padding-top:14px}.mini-stats span{display:flex;flex-direction:column;color:var(--secondary-text-color,#65728a);font-size:12px}.mini-stats b{color:var(--primary-text-color,#17233b);margin-top:4px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}th{background:var(--secondary-background-color,#f2f6fa);text-align:left}th,td{padding:9px 8px;border-bottom:1px solid var(--divider-color,#e4eaf0);white-space:nowrap}.quality{display:inline-flex;padding:3px 7px;border-radius:12px;background:#eef2f6;text-transform:capitalize}.quality.good{background:#e5f7ed;color:#08743d}.quality.partial,.quality.filtered{background:#fff4d6;color:#8a5900}.quality.missing,.quality.unreconciled{background:#ffe9e8;color:#a82e28}.empty{min-height:150px;display:grid;place-items:center;text-align:center;color:var(--secondary-text-color,#65728a);padding:20px}.status-card{min-width:0}.roadmap{max-width:720px;margin:50px auto;text-align:center;padding:54px}.roadmap>ha-icon{--mdc-icon-size:64px;color:var(--primary-color,#0785db)}.roadmap h2{display:block;font-size:28px;margin-top:18px}.roadmap b{color:var(--primary-color,#0785db)}.roadmap p{color:var(--secondary-text-color,#65728a)}.page-card{padding:24px}.section-head{display:flex;justify-content:space-between;align-items:center}.section-head p{margin:4px 0 18px;color:var(--secondary-text-color,#65728a)}button.refresh,button.csv,button.apply-period,button.previous,button.next,.primary{border:0;border-radius:8px;background:var(--primary-color,#0785db);color:#fff;padding:10px 15px;text-decoration:none;cursor:pointer}.settings-grid{display:grid;grid-template-columns:1fr 1.6fr;gap:16px}.notice{grid-column:1/-1}dl{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:24px}dt{color:var(--secondary-text-color,#65728a)}dd{margin:0;text-align:right;font-weight:600}.mapping{display:grid;gap:1px;background:var(--divider-color,#e0e7ef);border:1px solid var(--divider-color,#e0e7ef);border-radius:8px;overflow:hidden}.mapping>div{display:grid;grid-template-columns:1fr 1.25fr;gap:12px;background:var(--card-background-color,#fff);padding:10px}.mapping code{color:var(--secondary-text-color,#65728a);overflow-wrap:anywhere}.loading{display:grid;place-items:center;gap:12px;padding:100px}.error{padding:18px;border-radius:10px;background:#ffe9e8;color:#a82e28}
    .other-hvac{background:#14b8a6}.missing{color:#a82e28;background:#ffe9e8}.not-configured{font-size:24px;font-weight:700;color:var(--secondary-text-color,#65728a);margin:22px 0}.warning,.discrepancy{color:#b45309}.split-quality{display:flex;flex-direction:column;gap:3px;align-items:center}.quality.temperature-missing{background:#eef2f6;color:#56657a}.quality.unknown{background:#eef2f6;color:#56657a}.quality-layers{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin:0 0 14px}.quality-layers>span{white-space:nowrap}.equipment-charts{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px}.device-bars>div{display:grid;grid-template-columns:minmax(120px,1fr) 2fr 65px;gap:10px;align-items:center;margin:8px 0}.device-bars i{height:12px;background:#eef2f6;border-radius:6px;overflow:hidden}.device-bars i b{display:block;height:100%}.pv-chart{width:100%;height:220px}.pv-chart .pv{fill:#f6a800}.pv-chart .grid-import{fill:#238ee8}.pv-chart .grid-export{fill:#8b5cf6}.pv-chart .self-use{fill:#159b5b}.pv-chart .household{fill:#8b9bb0}.backfill-controls{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:12px;background:var(--secondary-background-color,#f2f6fa);border-radius:8px}.backfill-controls label{display:flex;align-items:center;gap:7px}.backfill-result{border-left:4px solid var(--primary-color,#0785db);padding:4px 14px;margin:12px 0}.backfill-result p{margin:8px 0;overflow-wrap:anywhere}
    .monthly-controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}.monthly-controls label{display:flex;gap:7px;align-items:center}.metric-selector{display:flex;gap:8px 18px;flex-wrap:wrap;border:1px solid var(--divider-color,#dbe4ee);border-radius:8px;margin:0 0 16px;padding:10px 14px}.metric-selector label{display:flex;gap:5px;align-items:center;font-size:12px}.metric-selector small{color:var(--secondary-text-color,#65728a)}.monthly-chart-wrap{overflow-x:auto}.monthly-chart{width:100%;min-width:900px;height:350px}.monthly-chart line,.monthly-percent line{stroke:var(--divider-color,#dbe4ee);stroke-width:1}.monthly-chart .zero-line{stroke:#52647d;stroke-width:1.5}.monthly-chart text,.monthly-percent text{font-size:11px;fill:var(--secondary-text-color,#65728a)}.monthly-chart .month-label{text-anchor:start}.monthly-line{fill:none;stroke-width:2.5}.monthly-point{stroke:#fff;stroke-width:1.5}.monthly-legend{display:flex;gap:8px 16px;flex-wrap:wrap;justify-content:center;font-size:12px}.monthly-legend span{display:flex;align-items:center;gap:5px}.monthly-legend i{display:inline-block;width:10px;height:10px;border-radius:2px}.monthly-percent svg{width:100%;min-width:900px;height:125px}.monthly-percent h3{font-size:14px}.monthly-table th small{display:block;color:var(--secondary-text-color,#65728a);font-weight:400}.monthly-table td:first-child{font-weight:700}
    @media(max-width:1200px){.kpis{grid-template-columns:repeat(3,1fr)}.top-grid,.bottom-grid{grid-template-columns:1fr 1fr}.status-card,.bottom-grid .wide{grid-column:1/-1}}
    .period .conditional{display:none}.period .conditional.visible{display:flex}button:disabled{opacity:.45;cursor:not-allowed}
    @media(max-width:800px){main{padding:12px 9px}.brand>ha-icon{--mdc-icon-size:42px}.brand h1{font-size:23px}.brand p{font-size:13px}.connection{display:none}.tabs{gap:2px}.tab{padding:13px 11px;min-height:44px}.tab span{display:none}.kpis{grid-template-columns:1fr}.top-grid,.bottom-grid,.settings-grid,.equipment-charts,.thermal-form{grid-template-columns:1fr}.status-card,.bottom-grid .wide,.notice{grid-column:auto}.section-head{align-items:flex-start}.mapping>div{grid-template-columns:1fr}.mini-stats{flex-wrap:wrap}.device-bars>div{grid-template-columns:1fr}.period,.monthly-controls{align-items:stretch;flex-direction:column}.period label,.monthly-controls label{justify-content:space-between}.period input,.period select,.period button,.toolbar button,.pager button,.primary,.monthly-controls input{min-height:44px}.metric-selector{display:grid;grid-template-columns:1fr}.metric-selector label{min-height:36px}.card{padding:14px}.chart,.pv-chart{height:180px}.bars{height:120px}.empty,.upcoming{min-height:72px}.table-wrap,.monthly-chart-wrap{max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}.table-wrap th:first-child,.table-wrap td:first-child{position:sticky;left:0;background:var(--card-background-color,#fff);z-index:1}.table-wrap th:first-child{background:var(--secondary-background-color,#f2f6fa)}.pager{flex-wrap:wrap}.pager span{order:-1;width:100%;text-align:center}}
    .bar-chart-shell{position:relative;padding-left:48px}.bar-axis{position:absolute;left:0;top:10px;bottom:0;width:48px}.bar-axis span{position:absolute;right:5px;transform:translateY(50%);font-size:10px;color:var(--secondary-text-color,#66758d);white-space:nowrap}
    .pv-chart line{stroke:var(--divider-color,#dbe4ee);stroke-width:.7}.pv-chart text{font-size:7px;fill:var(--secondary-text-color,#66758d)}
  `;
}

customElements.define("home-energy-intelligence-panel", HomeEnergyIntelligencePanel);
