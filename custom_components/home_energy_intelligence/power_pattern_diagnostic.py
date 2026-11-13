"""Read-only exploratory power analysis; never changes electricity classification."""
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from math import isfinite, sqrt
from .models import NumericSample

# Diagnostic evidence boundary, not a production classification threshold.
MAX_POWER_GAP = timedelta(minutes=15)


def power_segments(samples, start, end):
    """Retain observed intervals bracketed by valid readings, never fill gaps."""
    ordered = sorted((s for s in samples if s.timestamp.tzinfo), key=lambda s: s.timestamp)
    # Conflicting simultaneous readings cannot form trustworthy intervals.
    deduplicated = []
    for sample in ordered:
        if deduplicated and deduplicated[-1].timestamp == sample.timestamp:
            previous = deduplicated[-1]
            if previous.value != sample.value:
                deduplicated[-1] = NumericSample(sample.timestamp, None, reason="conflicting_power_timestamp")
        else:
            deduplicated.append(sample)
    segments, gaps = [], []
    for left, right in zip(deduplicated, deduplicated[1:]):
        lo, hi = max(left.timestamp, start), min(right.timestamp, end)
        if hi <= lo:
            continue
        gap = (right.timestamp - left.timestamp).total_seconds()
        valid = (gap > 0 and gap <= MAX_POWER_GAP.total_seconds() and
                 left.value is not None and right.value is not None and
                 isfinite(left.value) and isfinite(right.value) and left.value >= 0 and right.value >= 0)
        gaps.append(gap)
        if valid:
            segments.append((float(left.value), (hi-lo).total_seconds()))
    return segments, gaps


def power_statistics(segments, expected_seconds, gaps=()):
    duration = sum(seconds for _, seconds in segments)
    if not duration:
        return {"valid_seconds": 0, "coverage_percent": 0 if expected_seconds else None,
                "mean_w": None, "median_w": None, "p10_w": None, "p25_w": None,
                "p75_w": None, "p90_w": None, "min_w": None, "max_w": None,
                "stddev_w": None, "max_observation_gap_seconds": max(gaps, default=None)}
    mean = sum(value*seconds for value, seconds in segments) / duration
    def percentile(fraction):
        target, elapsed = duration*fraction, 0
        for value, seconds in sorted(segments):
            elapsed += seconds
            if elapsed >= target:
                return value
        return None
    return {"valid_seconds": duration, "coverage_percent": 100*duration/expected_seconds if expected_seconds else None,
            "mean_w": mean, "median_w": percentile(.5), "p10_w": percentile(.1), "p25_w": percentile(.25),
            "p75_w": percentile(.75), "p90_w": percentile(.9),
            "min_w": min(v for v, _ in segments), "max_w": max(v for v, _ in segments),
            "stddev_w": sqrt(sum(seconds*(value-mean)**2 for value, seconds in segments)/duration),
            "max_observation_gap_seconds": max(gaps, default=None)}


def analyze_power(trace, history, streams, state_map, freshness, continuity):
    """Build unverified references and partition unknown meter electricity."""
    roles = ("confirmed_heating", "dhw", "idle", "cooling")
    references = {role: {"interval_count": 0, "expected_seconds": 0, "segments": [], "gaps": [], "observations": set()} for role in roles}
    primary = sorted((s for s in streams["primary"]["samples"] if s.timestamp.tzinfo), key=lambda s: s.timestamp)
    times = [s.timestamp for s in primary]
    mapping = {str(k).lower(): v for k, v in state_map.items()}
    unknowns = {(c["start_utc"], c["end_utc"]): c for c in (continuity or {}).get("candidates", [])}
    ordered_power = sorted((s for s in history.power if s.timestamp.tzinfo), key=lambda s: s.timestamp)
    power_times = [s.timestamp for s in ordered_power]
    candidates = []
    totals = {"heating_compatible_kwh": 0.0, "dhw_compatible_kwh": 0.0, "idle_compatible_kwh": 0.0,
              "ambiguous_kwh": 0.0, "insufficient_evidence_kwh": 0.0}
    for entry in trace:
        if entry.get("status") != "accepted" or not entry.get("start") or not entry.get("end"):
            continue
        start, end = datetime.fromisoformat(entry["start"]), datetime.fromisoformat(entry["end"])
        duration = (end-start).total_seconds()
        first = max(0, bisect_right(power_times, start)-1)
        last = min(len(ordered_power), bisect_left(power_times, end)+1)
        segments, gaps = power_segments(ordered_power[first:last], start, end)
        stats = power_statistics(segments, duration, gaps)
        category = entry.get("category")
        index = bisect_right(times, start)-1
        obs = primary[index] if index >= 0 else None
        if (category in roles and not entry.get("mode_transition") and obs and
            mapping.get(obs.state.lower()) == category and start-obs.timestamp < freshness and
            end <= obs.timestamp+freshness and (entry.get("state") or {}).get("source") == "primary" and
            stats["coverage_percent"] == 100):
            ref = references[category]
            ref["interval_count"] += 1
            ref["expected_seconds"] += duration
            ref["segments"].extend(segments)
            ref["gaps"].extend(gaps)
            ref["observations"].update((sample.timestamp.isoformat(), sample.source)
                                       for sample in ordered_power[first:last]
                                       if start <= sample.timestamp <= end and sample.value is not None
                                       and isfinite(sample.value) and sample.value >= 0)
        if category == "unknown":
            known = unknowns.get((entry["start"], entry["end"]), {})
            sufficient = stats["coverage_percent"] == 100
            label = "ambiguous" if sufficient else "insufficient_evidence"
            totals[label+"_kwh"] += entry["delta_kwh"]
            candidates.append({"start_utc": entry["start"], "end_utc": entry["end"],
                "meter_kwh": entry["delta_kwh"], "power": stats, "candidate": label,
                "primary_previous": known.get("primary_previous"), "primary_next": known.get("primary_next"),
                "fallback_previous": known.get("fallback_previous"), "fallback_next": known.get("fallback_next"),
                "actual_dhw_previous": known.get("actual_dhw_previous"),
                "potential_conflict": bool(known.get("possible_fallback_conflict") or known.get("possible_dhw_conflict") or known.get("primary_interruption")),
                "validation_status": "NOT VALIDATED",
                "reason": "No independently validated reference classifier" if sufficient else "Power observations do not cover the complete increment"})
    refs = {}
    for role, ref in references.items():
        refs[role] = {"interval_count": ref["interval_count"], "sample_count": len(ref["observations"]), "segment_count": len(ref["segments"]),
            "statistics": power_statistics(ref["segments"], ref["expected_seconds"], ref["gaps"]),
            "mapping_quality": "unverified; exploratory only", "_segments": ref["segments"],
            "_expected_seconds": ref["expected_seconds"], "_gaps": ref["gaps"], "_observations": list(ref["observations"])}
    if not any(e.get("status") == "accepted" for e in trace):
        totals = {key: None for key in totals}
    return {"power_source": {"entity_id": history.provenance.get("power_entity"),
                "sources": sorted({sample.source for sample in history.power if sample.source}),
                "unit": "W", "mapping_quality": "unverified"},
            "references": refs, "candidates": candidates, "candidate_totals": totals,
            "validated_recovery_kwh": 0 if any(e.get("status") == "accepted" for e in trace) else None}
