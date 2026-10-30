from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

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
