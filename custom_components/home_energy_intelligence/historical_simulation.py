"""Read-only hypothetical replay of existing historical counter allocation."""
from datetime import timedelta

from .allocation import allocate_historical_counter_increments
from .historical_modes import historical_heat_pump_modes
from .models import StateSample


def simulate_modes(start, end, history, streams, state_map, maximum_delta, baseline_freshness):
    """Compare isolated mode policies; no results are persisted or validated."""
    primary, dhw, fallback = (streams[name]["samples"] for name in ("primary", "actual_dhw", "fallback"))
    # Only actual Recorder attributes are eligible. A heating action remains assumed.
    actions = [StateSample(s.timestamp, "heat" if s.state.lower() == "heat" and
               str(s.attributes.get("hvac_action", "")).lower() == "heating" else "unavailable", s.attributes)
               for s in fallback if s.attributes.get("source") != "influx"]
    scenarios = {}
    for name, age, samples, primary_age in (
        ("baseline", baseline_freshness, fallback, baseline_freshness),
        ("fallback_120m", timedelta(minutes=120), fallback, baseline_freshness),
        ("fallback_180m", timedelta(minutes=180), fallback, baseline_freshness),
        ("recorder_hvac_action", baseline_freshness, actions, baseline_freshness),
    ):
        modes = historical_heat_pump_modes(start, end, primary=primary, actual_dhw=dhw, fallback=samples,
                                           state_map=state_map, maximum_age=primary_age, fallback_maximum_age=age)
        trace = []
        result = allocate_historical_counter_increments(start=start, end=end, mode_samples=modes,
            counter_samples=history.counter, power_samples=history.power, classifier=lambda sample: sample.state,
            maximum_delta_kwh=maximum_delta, trace=trace)
        categories = result.allocations_kwh
        def category_total(category):
            return (categories.get(category, 0.0) + categories.get(category + "_power_estimate", 0.0)) if result.meter_kwh is not None else None
        scenarios[name] = {"meter_kwh": result.meter_kwh, "categories_kwh": categories,
            "unknown_kwh": category_total("unknown"),
            "heating_assumed_kwh": category_total("heating_assumed"),
            "confirmed_heating_kwh": category_total("confirmed_heating"),
            "dhw_kwh": category_total("dhw"),
            "unsupported_transition_kwh": sum(x["delta_kwh"] for x in trace if x.get("reason") == "unsupported_transition") if result.meter_kwh is not None else None,
            "residual_kwh": result.residual_kwh, "classification_coverage_percent": result.coverage_percent,
            "covered_seconds": result.covered_seconds, "period_seconds": result.period_seconds,
            "reconciles": abs(sum(categories.values()) + (result.residual_kwh or 0) - result.meter_kwh) < 1e-7 if result.meter_kwh is not None else None,
            "quality": result.quality, "rejected_count": result.rejected_count,
            "evidence": "unverified historical mapping; hypothetical, not validated recovery" if name != "baseline" else "unchanged preview baseline; unverified historical mapping",
            "fallback_freshness_seconds": age.total_seconds(), "primary_freshness_seconds": primary_age.total_seconds(),
            "hvac_action_observations": sum("hvac_action" in s.attributes for s in samples) if name == "recorder_hvac_action" else None}
    for name, report in scenarios.items():
        report["difference_from_baseline_kwh"] = {key: (report[key] - scenarios["baseline"][key])
            if report[key] is not None and scenarios["baseline"][key] is not None else None
            for key in ("unknown_kwh", "heating_assumed_kwh", "confirmed_heating_kwh", "dhw_kwh", "unsupported_transition_kwh")}
    return scenarios
