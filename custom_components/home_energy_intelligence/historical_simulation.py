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
            "idle_kwh": category_total("idle"),
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


def continuity_candidates(trace, history, streams, state_map, freshness):
    """Describe unknown increments without asserting an unverified historical mapping."""
    from bisect import bisect_left, bisect_right
    from datetime import datetime
    from math import isfinite

    ordered = {role: sorted((s for s in info["samples"] if s.timestamp.tzinfo), key=lambda s: s.timestamp)
               for role, info in streams.items()}
    times = {role: [s.timestamp for s in values] for role, values in ordered.items()}
    def adjacent(role, start, end):
        samples, stamps = ordered[role], times[role]
        previous, following = bisect_right(stamps, start)-1, bisect_left(stamps, end)
        def show(index):
            if index < 0 or index >= len(samples):
                return None
            sample = samples[index]
            return {"timestamp_utc": sample.timestamp.isoformat(), "state": sample.state,
                    "source": sample.attributes.get("source", "recorder"),
                    "hvac_action": str(sample.attributes["hvac_action"]) if "hvac_action" in sample.attributes else None}
        return show(previous), show(following)
    mapping = {str(k).lower(): v for k, v in state_map.items()}
    results = []
    totals = {"supported_kwh": 0.0, "plausible_unverified_kwh": 0.0, "conflicting_or_insufficient_kwh": 0.0}
    for entry in trace:
        if entry.get("status") != "accepted" or entry.get("category") != "unknown":
            continue
        start, end = datetime.fromisoformat(entry["start"]), datetime.fromisoformat(entry["end"])
        primary_before, primary_after = adjacent("primary", start, end)
        fallback_before, fallback_after = adjacent("fallback", start, end)
        dhw_before, dhw_after = adjacent("actual_dhw", start, end)
        state = primary_before["state"].lower() if primary_before else None
        category = mapping.get(state) if state else None
        bracketed = bool(primary_before and primary_after and primary_after["state"].lower() == state
                         and category in {"confirmed_heating", "dhw", "idle", "cooling"})
        action = (fallback_before or {}).get("hvac_action")
        fallback_conflict = bool(action and str(action).lower() == "idle" and category == "confirmed_heating")
        dhw_conflict = bool(dhw_before and dhw_before["state"].lower() == "heating_water"
                            and datetime.fromisoformat(dhw_before["timestamp_utc"]) + freshness > start
                            and category in {"confirmed_heating", "idle", "cooling"})
        primary_interruption = any(sample.state.lower() != state for sample in ordered["primary"]
                                   if start < sample.timestamp < end)
        conflict = entry.get("reason") == "unsupported_transition" or primary_interruption or fallback_conflict or dhw_conflict
        classification = "plausible_unverified" if bracketed and not conflict else "conflicting_or_insufficient"
        rationale = (["Matching surrounding primary observations; silence is not proof of operation",
                      "Historical operation-state mapping validity is unverified"] if classification == "plausible_unverified" else
                     ["Unsupported transition, conflicting signal, unsupported state, or missing bounding observation"])
        power = [{"timestamp_utc": s.timestamp.isoformat(), "value_w": s.value, "source": s.source}
                 for s in history.power if start <= s.timestamp <= end and s.value is not None and isfinite(s.value)]
        amount = entry["delta_kwh"]
        totals[classification + "_kwh"] += amount
        results.append({"start_utc": start.isoformat(), "end_utc": end.isoformat(), "meter_kwh": amount,
            "primary_previous": primary_before, "primary_next": primary_after,
            "fallback_previous": fallback_before, "fallback_next": fallback_after,
            "actual_dhw_previous": dhw_before, "actual_dhw_next": dhw_after,
            "primary_observation_gap_seconds": (datetime.fromisoformat(primary_after["timestamp_utc"])-datetime.fromisoformat(primary_before["timestamp_utc"])).total_seconds() if primary_before and primary_after else None,
            "power_observations": power, "power_coverage": "unverified" if power else "unavailable",
            "primary_interruption": primary_interruption, "possible_idle_conflict": fallback_conflict, "possible_dhw_conflict": dhw_conflict,
            "confidence": classification, "rationale": rationale, "evidence_type": "inferred",
            "mapping_status": "unverified; continuity cannot be promoted"})
    if not any(entry.get("status") == "accepted" for entry in trace):
        totals = {key: None for key in totals}
    return {"candidates": results, "totals": totals,
            "limitation": "Actual-state mapping applicability and independent continuity are not verified by this export; supported scenario retains baseline."}
