from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

PANEL = Path(__file__).parents[1] / "custom_components/home_energy_intelligence/frontend/hei-panel.js"


def render(method: str, payload: object, options: object) -> str:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for frontend runtime contracts")
    script = r"""
const fs=require('fs'),vm=require('vm');let Panel;
global.HTMLElement=class{attachShadow(){return {}}};
global.customElements={define:(_name,ctor)=>{Panel=ctor}};
global.window={setInterval:()=>0,clearInterval:()=>{},clearTimeout:()=>{},confirm:()=>true};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
const input=JSON.parse(fs.readFileSync(0,'utf8'));const panel=new Panel();
process.stdout.write(panel[input.method](input.payload,input.options));
"""
    result = subprocess.run(
        ["node", "-e", script, str(PANEL)], input=json.dumps({
            "method": method, "payload": payload, "options": options,
        }), text=True, capture_output=True, check=True,
    )
    return result.stdout


def test_all_four_v02_curves_render_values_and_cop_numbers():
    daily = [{
        "local_date": "2026-10-29", "outdoor_mean_c": 5, "outdoor_coverage_percent": 100,
        "whole_house_heating_kwh": 12, "normalized_heating_kwh_m2_day": .071,
        "ac_cooling_kwh": 3, "normalized_cooling_kwh_m2_day": .018,
        "heat_pumps": [{"display_name": "Ecodan", "electricity_kwh": 8}],
        "air_conditioners": [{"display_name": "Aurora", "electricity_kwh": 4}],
        "cooling_units": [{"display_name": "Aurora", "electricity_kwh": 3}],
    }]
    heating = render("responseCurve", daily, {"kind": "heating"})
    cooling = render("responseCurve", daily, {"kind": "cooling"})
    assert "Ecodan: 8.00 kWh" in heating and "Aurora: 4.00 kWh" in heating
    assert "0.071" in heating and "/day" in heating
    assert "Aurora: 3.00 kWh" in cooling and "0.018" in cooling
    interval = [{
        "start_utc": "2026-10-28T23:00:00+00:00", "end_utc": "2026-10-29T23:00:00+00:00",
        "outdoor_mean_c": 5, "heating_electricity_kwh": 5,
        "heating_thermal_delta_kwh": 20, "heating_cop": 4,
        "dhw_electricity_kwh": 2, "dhw_thermal_delta_kwh": 6, "dhw_cop": 3,
    }]
    heat_cop = render("responseCurve", interval, {"kind": "heating", "thermal": True})
    dhw_cop = render("responseCurve", interval, {"kind": "dhw", "thermal": True})
    assert "COP 4.00" in heat_cop and "20.00 kWh thermal" in heat_cop
    assert "COP 3.00" in dhw_cop and "6.00 kWh thermal" in dhw_cop


def test_partial_energy_and_missing_outdoor_evidence_are_distinct():
    points = [{"local_date": "2026-09-01", "outdoor_mean_c": None,
               "whole_house_heating_known_subtotal_kwh": 12}]
    rendered = render("responseCurve", points, {"kind": "heating"})
    assert "not a temperature-response curve" in rendered
    assert "2026-09-01" in rendered and "12.00" in rendered
    assert "Partial known subtotal" in rendered
    assert "<circle" not in rendered


def test_monthly_controller_thermal_is_not_interval_heat():
    script = r"""
const fs=require('fs'),vm=require('vm');let Panel;
global.HTMLElement=class{attachShadow(){return {}}};global.customElements={define:(_,c)=>Panel=c};
global.window={setInterval:()=>0,clearInterval:()=>{}};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));const panel=new Panel();
panel.selectedPeriod={start_date:'2026-09-01',end_date:'2026-09-30'};
panel.selectedMonthReport={month:'2026-09',heating_thermal_kwh:309,dhw_thermal_kwh:424,vendor_combined_thermal_kwh:734,ecodan_total_generated_thermal_kwh:733,snapshot_status:'final'};
process.stdout.write(panel.wholeHouseEnergyCard());
"""
    output = subprocess.run(["node", "-e", script, str(PANEL)], text=True, capture_output=True, check=True).stdout
    assert all(value in output for value in ("309.0", "424.0", "734.0", "733.0", "1.0"))
    assert "not matched interval measurements" in output


def test_backfill_poll_keeps_job_id_after_persisted_status_response():
    script = r"""
const fs=require('fs'),vm=require('vm');let Panel,scheduled;
global.HTMLElement=class{attachShadow(){return {}}};global.customElements={define:(_,c)=>Panel=c};
global.window={setInterval:()=>0,clearInterval:()=>{},setTimeout:(fn)=>{scheduled=fn;return 1}};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));const p=new Panel();p.render=()=>{};
let calls=0;p._hass={callWS:async()=>({id:42,status:++calls===1?'running':'completed'})};
p.load=async()=>{};p.loadTemperatureDiagnostics=async()=>{};p.temperatureBackfillJob={job_id:42};
(async()=>{await p.pollTemperatureBackfill();await scheduled();process.stdout.write(JSON.stringify({calls,job:p.temperatureBackfillJob}));})();
"""
    result = subprocess.run(["node", "-e", script, str(PANEL)], text=True, capture_output=True, check=True)
    data = json.loads(result.stdout)
    assert data["calls"] == 2
    assert data["job"]["job_id"] == 42 and data["job"]["status"] == "completed"


def test_backfill_job_id_is_recovered_after_panel_refresh():
    script = r"""
const fs=require('fs'),vm=require('vm');let Panel;
global.HTMLElement=class{attachShadow(){return {}}};global.customElements={define:(_,c)=>Panel=c};
global.window={setInterval:()=>0,clearInterval:()=>{},sessionStorage:{getItem:()=> '42'}};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));const p=new Panel();
p.load=()=>{};p.pollTemperatureBackfill=()=>{process.stdout.write(String(p.temperatureBackfillJob.job_id));};
p.hass={callWS:async()=>{}};
"""
    result = subprocess.run(["node", "-e", script, str(PANEL)], text=True, capture_output=True, check=True)
    assert result.stdout == "42"
