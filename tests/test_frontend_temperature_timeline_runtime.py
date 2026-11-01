from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PANEL = Path(__file__).parents[1] / "custom_components" / "home_energy_intelligence" / "frontend" / "hei-panel.js"


def _render(payload: dict) -> str:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for frontend runtime contracts")
    script = r"""
const fs=require('fs'),vm=require('vm');
let Panel;
global.HTMLElement=class{attachShadow(){return {}}};
global.customElements={define:(_name,ctor)=>{Panel=ctor}};
global.window={setInterval:()=>0,clearInterval:()=>{},clearTimeout:()=>{}};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const panel=new Panel();
panel.overviewData={representative_indoor_source_id:'indoor'};
panel.temperatureSources=[{source_id:'indoor',display_name:'Indoor',entity_id:'sensor.indoor',role:'indoor'}];
panel.historySummary=input.daily || null;
panel.temperatureTimelineData=input.timeline;
const first=panel.temperatureTimeline('indoor');
if(input.twice){
  panel.overviewData.representative_outdoor_source_id='outdoor';
  panel.temperatureSources.push({source_id:'outdoor',display_name:'Outdoor',entity_id:'sensor.outdoor',role:'outdoor'});
  input.timeline.source_ids.push('outdoor');
  input.timeline.sources.push({source_id:'outdoor',display_name:'Outdoor',entity_id:'sensor.outdoor',role:'outdoor'});
  input.timeline.intervals.forEach((row)=>{row.temperatures.outdoor={...row.temperatures.indoor};});
}
process.stdout.write(first+(input.twice?panel.temperatureTimeline('outdoor'):''));
"""
    result = subprocess.run(
        ["node", "-e", script, str(PANEL)], input=json.dumps(payload), text=True,
        capture_output=True, check=True,
    )
    return result.stdout


def _timeline(*, missing=(), available_only=None):
    intervals = []
    range_start = datetime(2026, 10, 28, 23, tzinfo=UTC)
    for hour in range(24):
        available = hour not in missing and (available_only is None or hour in available_only)
        interval_start = range_start + timedelta(hours=hour)
        intervals.append({
            "start_utc": interval_start.isoformat(),
            "end_utc": (interval_start + timedelta(hours=1)).isoformat(),
            "local_label": f"2026-10-29 {hour:02d}:00 +0100",
            "temperatures": {"indoor": {
                "mean": 23 + hour / 100 if available else None,
                "minimum": 22.5 if available else None,
                "maximum": 23.2 if available else None,
                "coverage_percent": 100 if available else 0,
                "valid_seconds": 3600 if available else 0,
                "expected_seconds": 3600, "quality": "good" if available else "missing",
                "missing_reason": None if available else "hourly_temperature_history_not_archived_or_backfilled",
                "rejected_reasons": [],
            }},
        })
    return {
        "resolution": "hourly", "timezone": "Europe/Berlin",
        "start_date": "2026-10-29", "end_date": "2026-10-29",
        "range_start_utc": "2026-10-28T23:00:00+00:00",
        "range_end_utc": "2026-10-29T23:00:00+00:00",
        "source_ids": ["indoor"],
        "sources": [{"source_id": "indoor", "display_name": "Indoor",
                     "entity_id": "sensor.indoor", "role": "indoor"}],
        "intervals": intervals,
        "available_interval_count": sum(
            item["temperatures"]["indoor"]["mean"] is not None for item in intervals
        ),
        "expected_interval_count": 24,
        "axis_ticks": [
            {"timestamp_utc": "2026-10-28T23:00:00+00:00", "label": "00:00"},
            {"timestamp_utc": "2026-10-29T05:00:00+00:00", "label": "06:00"},
            {"timestamp_utc": "2026-10-29T11:00:00+00:00", "label": "12:00"},
            {"timestamp_utc": "2026-10-29T17:00:00+00:00", "label": "18:00"},
            {"timestamp_utc": "2026-10-29T23:00:00+00:00", "label": "24:00"},
        ],
    }


def test_daily_temperature_axes_show_dates_for_both_sources():
    timeline = _timeline()
    timeline.update(resolution="daily", start_date="2026-10-01", end_date="2026-10-05",
                    range_start_utc="2026-09-30T22:00:00+00:00",
                    range_end_utc="2026-10-05T22:00:00+00:00", axis_ticks=[])
    timeline["intervals"] = timeline["intervals"][:5]
    for index, row in enumerate(timeline["intervals"], 1):
        row["local_date"] = f"2026-10-0{index}"
        row["start_utc"] = f"2026-10-0{index}T00:00:00+00:00"
    html = _render({"timeline": timeline, "twice": True})
    assert html.count(">10-01</text>") == 2
    assert html.count(">10-05</text>") == 2


def test_yesterday_renders_real_hourly_points_time_axis_and_accessible_details():
    html = _render({"timeline": _timeline()})
    assert html.count('class="temp-point') == 24
    assert "<polyline" in html
    for label in ("00:00", "06:00", "12:00", "18:00", "24:00"):
        assert f">{label}</text>" in html
    assert html.count('tabindex="0"') == 24
    assert "Europe/Berlin" in html and "hourly" in html
    assert "sensor.indoor" in html and "coverage 100.0%" in html
    assert 'viewBox="0 0 320 190"' in html
    assert '<clipPath id="hei-temp-plot-1">' in html
    assert 'clip-path="url(#hei-temp-plot-1)"' in html
    assert 'class="temp-chart-shell"' in html
    assert "°C" in html


def test_missing_hours_break_lines_and_one_real_hour_remains_one_point():
    gap_html = _render({"timeline": _timeline(missing={8, 9})})
    assert gap_html.count('class="temp-point') == 22
    assert gap_html.count("<polyline") == 2
    assert "22/24 intervals" in gap_html
    one_html = _render({"timeline": _timeline(available_only={5})})
    assert one_html.count('class="temp-point') == 1
    assert "<polyline" not in one_html
    assert "1/24 intervals" in one_html


def test_daily_summary_without_hourly_archive_shows_truthful_empty_state():
    timeline = _timeline(available_only=set())
    daily = {"temperatures": {"indoor": {
        "mean": 23.0, "minimum": 22.5, "maximum": 23.2,
        "coverage_percent": 100, "quality": "partial",
    }}}
    html = _render({"timeline": timeline, "daily": daily})
    assert "Daily summary available, but hourly temperature history has not been archived/backfilled" in html
    assert "<polyline" not in html and 'class="temp-point' not in html


def test_large_valid_range_and_whiskers_stay_inside_plot_rectangle():
    timeline = _timeline()
    timeline["intervals"][0]["temperatures"]["indoor"].update({
        "mean": -12.0, "minimum": -20.0, "maximum": -5.0,
    })
    timeline["intervals"][-1]["temperatures"]["indoor"].update({
        "mean": 39.0, "minimum": 34.0, "maximum": 45.0,
    })
    html = _render({"timeline": timeline})
    ranges = re.findall(r'class="temp-range[^\"]*"[^>]*y1="([0-9.]+)" y2="([0-9.]+)"', html)
    assert ranges
    assert all(12 <= float(value) <= 148 for pair in ranges for value in pair)
    points = re.findall(r'class="temp-point[^\"]*" cx="([0-9.]+)" cy="([0-9.]+)"', html)
    assert all(48 <= float(x) <= 308 and 12 <= float(y) <= 148 for x, y in points)
    assert "min -20.0" in html and "max -5.0" in html
    assert "min 34.0" in html and "max 45.0" in html


def test_rejected_spikes_partial_day_and_missing_data_do_not_create_graphics():
    timeline = _timeline(missing={3, 4})
    for index, rejected in ((7, -55), (12, 155)):
        timeline["intervals"][index]["temperatures"]["indoor"].update({
            "mean": None, "minimum": None, "maximum": None,
            "coverage_percent": 0, "valid_seconds": 0, "quality": "filtered",
            "rejected_reasons": [f"invalid_sentinel:{rejected}"],
        })
    timeline["available_interval_count"] = 20
    html = _render({"timeline": timeline})
    assert html.count('class="temp-point') == 20
    assert "20/24 intervals" in html
    assert "-55.0 °C" not in html and "155.0 °C" not in html


def test_multiple_chart_instances_have_independent_clip_paths():
    html = _render({"timeline": _timeline(), "twice": True})
    ids = re.findall(r'<clipPath id="([^"]+)"', html)
    references = re.findall(r'clip-path="url\(#([^\)]+)\)"', html)
    assert ids == ["hei-temp-plot-1", "hei-temp-plot-2"]
    assert references == ids


@pytest.mark.parametrize("viewport_width", [320, 360, 390, 430, 1280])
def test_responsive_viewbox_fits_mobile_and_desktop_without_horizontal_scroll(viewport_width):
    html = _render({"timeline": _timeline()})
    view_width, view_height = map(int, re.search(r'viewBox="0 0 (\d+) (\d+)"', html).groups())
    rendered_width = min(viewport_width, view_width if viewport_width > 800 else viewport_width)
    rendered_height = min(230, rendered_width * view_height / view_width)
    assert rendered_width <= viewport_width
    assert 190 <= rendered_height <= 230
