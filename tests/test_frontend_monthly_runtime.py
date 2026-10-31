from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PANEL = Path(__file__).parents[1] / "custom_components/home_energy_intelligence/frontend/hei-panel.js"


def run_panel(script_body: str, payload: object) -> str:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for frontend runtime contracts")
    script = r"""
const fs=require('fs'),vm=require('vm');let Panel;
global.HTMLElement=class{attachShadow(){return {}}};
global.customElements={define:(_name,ctor)=>{Panel=ctor}};
global.window={setInterval:()=>0,clearInterval:()=>{},clearTimeout:()=>{},confirm:()=>true};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
const input=JSON.parse(fs.readFileSync(0,'utf8'));const panel=new Panel();
""" + script_body
    result = subprocess.run(
        ["node", "-e", script, str(PANEL)], input=json.dumps(payload),
        text=True, capture_output=True, check=True,
    )
    return result.stdout


def monthly_payload():
    months = [f"2025-{month:02d}" for month in range(10, 13)] + [
        f"2026-{month:02d}" for month in range(1, 11)
    ]
    return {
        "months": months,
        "rows": [{
            "month": month,
            "ecodan_heating_electricity_kwh": 10 + index,
            "heating_thermal_kwh": 30 + index,
            "primary_heating_cop": 3 + index / 100,
            "shelly_minus_vendor_percent": -2 + index / 10,
            "quality": "good", "primary_cop_quality": "measured_exact_cutoff",
            "snapshot_status": "final", "cutoff_match": True,
            "snapshot_cutoff_utc": f"{month}-28T23:00:00+00:00",
        } for index, month in enumerate(months)],
        "metric_catalog": [
            {"id": "ecodan_heating_electricity_kwh", "label": "Shelly heating", "unit": "kWh", "axis": "energy"},
            {"id": "heating_thermal_kwh", "label": "Heating thermal", "unit": "kWh", "axis": "energy"},
            {"id": "primary_heating_cop", "label": "Primary heating COP", "unit": "COP", "axis": "cop"},
            {"id": "shelly_minus_vendor_percent", "label": "Difference", "unit": "%", "axis": "percent", "group": "vendor"},
        ],
        "default_metric_ids": [
            "ecodan_heating_electricity_kwh", "heating_thermal_kwh",
            "primary_heating_cop", "shelly_minus_vendor_percent",
        ],
    }


def test_monthly_cop_is_separate_from_verified_interval_reports():
    output = run_panel(r"""
panel.selectedPeriod={start_date:'2026-09-01',end_date:'2026-09-30'};
panel.selectedMonthReport={month:'2026-09',provisional_shelly_heating_cop:4.45,provisional_shelly_dhw_cop:3.63,provisional_shelly_combined_cop:3.94};
panel.v02Report={ecodan_reports:{},ecodan_space_heating:[],ecodan_dhw:[]};
panel.periodControls=()=>'';panel.monthlyView=()=>'';
process.stdout.write(panel.copView());
panel.selectedPeriod={start_date:'2026-09-02',end_date:'2026-09-30'};
process.stdout.write('\n---\n'+panel.copView());
""", {})
    full, partial = output.split("\n---\n")
    assert "Provisional monthly COP" in full and "4.45" in full and "3.63" in full and "3.94" in full
    assert "Space heating COP<b>Unavailable" in full
    assert "matching intervals" in full and "No matched measured intervals" in full
    assert "Provisional monthly COP" not in partial


def test_temperature_analysis_uses_duration_weighted_percentiles_and_preserves_mean():
    output = run_panel(r"""
const item=(mean,seconds,min,max)=>({mean,valid_seconds:seconds,expected_seconds:seconds,minimum:min,maximum:max});
panel.analysisData={mode:'year',period:'2026-03',source:{source_id:'outdoor'},daily:{intervals:[{local_date:'2026-03-29',temperatures:{outdoor:item(8,23*3600,7,29)}},{local_date:'2026-03-30',temperatures:{outdoor:item(10,3600,10,10)}}]},hourly:{intervals:[{local_date:'2026-03-29',temperatures:{outdoor:item(7,22*3600,7,7)}},{local_date:'2026-03-29',temperatures:{outdoor:item(29,3600,29,29)}},{local_date:'2026-03-30',temperatures:{outdoor:item(10,3600,10,10)}}]},cop:{rows:[{month:'2026-03',vendor_combined_cop:3.8}]}};
panel.analysisLower=0;panel.analysisUpper=5;const a=panel.temperatureCopRows();panel.analysisLower=10;panel.analysisUpper=0;const b=panel.temperatureCopRows();panel.analysisData.hourly.intervals=[];const c=panel.temperatureCopRows();
process.stdout.write(JSON.stringify({a:a[2],b:b[2],c:c[2],count:a.length}));
""", {})
    result = json.loads(output)
    assert result["count"] == 12
    assert result["a"]["mean"] == pytest.approx((8 * 23 + 10) / 24)
    assert result["a"]["high"] == 10
    assert result["b"]["high"] == 29
    assert result["a"]["mean"] == result["b"]["mean"]
    assert result["c"]["low"] is None
    assert result["a"]["cop"]["vendor_combined_cop"] == 3.8
def test_annual_cop_uses_matched_months_and_ratio_of_sums():
    output = run_panel(r"""
panel.historyPreset='this_year';panel.selectedPeriod={start_date:'2026-01-01',end_date:'2026-10-31'};
const row=(month,heat,dhw,vh,vd,sh,sd)=>({month,snapshot_status:'final',provisional_cop_quality:'provisional_controller_proportions',heating_thermal_kwh:heat,dhw_thermal_kwh:dhw,vendor_heating_electricity_kwh:vh,vendor_dhw_electricity_kwh:vd,vendor_combined_electricity_kwh:vh+vd,provisional_shelly_heating_electricity_kwh:sh,provisional_shelly_dhw_electricity_kwh:sd,provisional_shelly_heating_cop:heat/sh,provisional_shelly_dhw_cop:dhw/sd,provisional_shelly_combined_cop:(heat+dhw)/(sh+sd)});
panel.selectedYearReport={rows:[row('2026-08',100,200,30,50,29,49),row('2026-09',200,300,50,70,49,68),{month:'2026-10',snapshot_status:'partial'}]};
panel.v02Report={ecodan_reports:{},ecodan_space_heating:[],ecodan_dhw:[]};panel.periodControls=()=>'';panel.monthlyView=()=>'';
process.stdout.write(panel.copView());
""", {})
    assert "August to September 2026, 2 eligible months" in output
    assert "2026-08, 2026-09" in output
    assert "Combined<b>4.00" in output
    assert "Combined<b>4.10" in output
    assert "COP for available months" in output
    assert "Temperature & COP Analysis" in output
    assert "Space heating COP<b>Unavailable" in output


def test_house_profile_uses_configured_area_qualified_points_and_separate_thermal():
    output = run_panel(r"""
panel.overviewData={floor_area_m2:170};panel.heatingIntensity=true;
panel.responseProfileData={points:Array.from({length:10},(_,i)=>({status:'validated',outdoor_mean_c:i,heating_electricity_kwh:17+i*1.7,local_date:`2026-01-${String(i+1).padStart(2,'0')}`,outdoor_coverage_percent:100,equipment_heating:[]})).concat([{status:'excluded',outdoor_mean_c:50,heating_electricity_kwh:999,exclusion_reasons:['partial']}])};
panel.monthlyReport={rows:[{month:'2026-09',period_coverage:{allocation_coverage:{complete:true},date_coverage:{complete_through_available_cutoff:true}},ecodan_heating_electricity_kwh:10,ac_heating_electricity_kwh:7,whole_house_heating_electricity_kwh:17,heating_thermal_kwh:34,dhw_thermal_kwh:8,ecodan_dhw_electricity_kwh:3,ac_cooling_electricity_kwh:2,quality:'good',snapshot_status:'final'},{month:'2026-10',period_coverage:{allocation_coverage:{complete:false},date_coverage:{complete_through_available_cutoff:true}},ecodan_heating_electricity_kwh:0,quality:'partial',snapshot_status:'partial'}]};
process.stdout.write(panel.scatterChart()+'\n'+panel.monthlyHouseProfile());
panel.responseProfileData.points=panel.responseProfileData.points.slice(0,2);process.stdout.write('\n---\n'+panel.scatterChart());
""", {})
    output, insufficient = output.split("\n---\n")
    assert "Configured area: 170 m" in output
    assert "0.100 kWh/m" in output
    assert "y = 0.0100x + 0.1000" in output and "1.000" in output
    assert "1 excluded day(s)" in output
    assert "0.10" in output and "34.00" in output
    assert "AC units provide measured electrical consumption" in output
    assert "DHW controller kWh" in output and "Unavailable" in output
    assert "max-width:100%;overflow-x:auto" in output
    assert "Trendline: Unavailable" in insufficient


def test_rolling_13_month_chart_table_and_distinct_snapshot_form_render():
    output = run_panel(r"""
panel.monthlyReport=input.report;panel.monthlyMetricIds=input.report.default_metric_ids;
panel.monthlyEndMonth='2026-10';panel.equipment=[{equipment_id:'hp',display_name:'Heat pump',kind:'heat_pump'}];
panel.overviewData={cutoff_local:'2026-10-31T00:29:00+01:00'};
panel.monthlyLedger={snapshots:[]};process.stdout.write(panel.monthlyView());
""", {"report": monthly_payload()})
    assert output.count('class="month-label"') == 13
    assert "Rolling 13-Month Ecodan Analytics" in output
    assert "Primary heating COP" in output
    assert "Vendor electricity and vendor COP are secondary" in output
    assert "Monthly / Month-to-Date Ecodan report snapshots" in output
    assert "separate from cumulative lifetime thermal" in output
    assert "monthly-form" in output
    assert 'name="entry_local_date" value="2026-10-31"' in output
    assert 'name="entry_local_time" value="00:29:00"' in output
    assert "2025-10" in output and "2026-10" in output
    assert "zero-line" in output


def test_monthly_missing_values_and_house_card_do_not_render_false_zero():
    report = monthly_payload()
    report["rows"][-1]["heating_thermal_kwh"] = None
    output = run_panel(r"""
panel.monthlyReport=input.report;panel.monthlyMetricIds=input.report.default_metric_ids;
panel.monthlyEndMonth='2026-10';panel.equipment=[];panel.monthlyLedger=null;
const monthly=panel.monthlyView();panel.selectedRecord=()=>({energy:{}});panel.v02Report={ecodan_space_heating:[{heating_thermal_delta_kwh:null,dhw_thermal_delta_kwh:null}]};
process.stdout.write(monthly+'\n'+panel.wholeHouseEnergyCard());
""", {"report": report})
    assert "2026-10</td><td>22.000</td><td>" in output
    assert "0.0 kWh thermal" not in output
    assert "Known measured subtotals are shown without filling gaps" in output
    assert "Administrator access is required" in output


def test_monthly_mobile_contract_has_scroll_and_touch_sized_controls():
    source = PANEL.read_text(encoding="utf-8")
    assert ".monthly-chart{width:100%;min-width:900px" in source
    assert ".monthly-chart-wrap{max-width:100%;overflow-x:auto" in source
    assert ".monthly-controls input{min-height:44px}" in source
    assert "monthly_report_csv" in source
