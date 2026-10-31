from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PANEL = (
    Path(__file__).parents[1]
    / "custom_components/home_energy_intelligence/frontend/hei-panel.js"
)


def test_today_overview_renders_provisional_energy_without_reference_error():
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for the frontend runtime contract")
    script = r"""
const fs=require('fs'),vm=require('vm');let Panel;
global.HTMLElement=class{attachShadow(){return {}}};
global.customElements={define:(_name,ctor)=>{Panel=ctor}};
global.window={setInterval:()=>0,clearInterval:()=>{},clearTimeout:()=>{}};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
const panel=new Panel();
panel.historyPreset='today';
panel.selectedPeriod={label:'Today',start_date:'2026-10-30',end_date:'2026-10-30'};
panel.historyCoverage={complete:false};
panel.overviewData={cutoff_local:'2026-10-30T12:00:00+01:00',timezone:'Europe/Ljubljana',today_interval_coverage:{coverage_percent:50,missing_intervals:[]}};
panel.historySummary={local_date:'2026-10-30',energy:{household_kwh:4.5,provisional:true},temperatures:{}};
panel.history=[];panel.equipment=[];panel.temperatureSources=[];
process.stdout.write(panel.overview());
"""
    result = subprocess.run(
        ["node", "-e", script, str(PANEL)], text=True,
        capture_output=True, check=True,
    )
    assert "Whole-House Electricity" in result.stdout
    assert "Known/provisional through cutoff" in result.stdout
    assert "Europe/Ljubljana" in result.stdout


def test_daily_split_and_pv_history_render_numeric_kwh_axes():
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for the frontend runtime contract")
    script = r"""
const fs=require('fs'),vm=require('vm');let Panel;
global.HTMLElement=class{attachShadow(){return {}}};
global.customElements={define:(_name,ctor)=>{Panel=ctor}};
global.window={setInterval:()=>0,clearInterval:()=>{},clearTimeout:()=>{}};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
const panel=new Panel();panel.historySummary={energy:{self_sufficiency_percent:40,pv_self_consumption_percent:50}};
panel.history=[{local_date:'2026-10-30',household_kwh:10,known_household_kwh:10,quality:'good',energy:{household_kwh:10,pv_kwh:4,import_kwh:8,export_kwh:2,pv_consumed_on_site_kwh:2,space_heating_kwh:3,known_space_heating_kwh:3,dhw_kwh:2,known_dhw_kwh:2,cooling_kwh:0,known_cooling_kwh:0,idle_kwh:1,known_idle_kwh:1,other_hvac_kwh:0,known_other_hvac_kwh:0,other_household_kwh:4,unknown_kwh:0,equipment:[]}}];
process.stdout.write(panel.splitChart()+'\n'+panel.pvHistoryChart());
"""
    result = subprocess.run(
        ["node", "-e", script, str(PANEL)], text=True,
        capture_output=True, check=True,
    )
    assert "bar-axis" in result.stdout
    assert "numeric kWh axis" in result.stdout
    assert result.stdout.count("kWh") >= 6
