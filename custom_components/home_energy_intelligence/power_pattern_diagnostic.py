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
    episodes = []
    exclusion_reasons = {}
    totals = {"heating_compatible_kwh": 0.0, "dhw_compatible_kwh": 0.0, "idle_compatible_kwh": 0.0,
              "ambiguous_kwh": 0.0, "insufficient_evidence_kwh": 0.0}
    for entry in trace:
        if entry.get("status") != "accepted" or not entry.get("start") or not entry.get("end"):
            continue
        start, end = datetime.fromisoformat(entry["start"]), datetime.fromisoformat(entry["end"])
        duration = (end-start).total_seconds()
        first = max(0, bisect_left(power_times, start)-1)
        last = min(len(ordered_power), bisect_right(power_times, end)+1)
        segments, gaps = power_segments(ordered_power[first:last], start, end)
        stats = power_statistics(segments, duration, gaps)
        category = entry.get("category")
        index = bisect_right(times, start)-1
        obs = primary[index] if index >= 0 else None
        relevant_fallback = sorted((sample for sample in streams.get("fallback", {}).get("samples", [])
                                    if sample.timestamp.tzinfo and sample.timestamp < end), key=lambda sample: sample.timestamp)
        previous_fallback = [sample for sample in relevant_fallback if sample.timestamp <= start]
        fallback_checks = (previous_fallback[-1:] + [sample for sample in relevant_fallback if start < sample.timestamp < end])
        conflicting_fallback = any(sample.timestamp + freshness > start and
            ((category == "confirmed_heating" and str(sample.attributes.get("hvac_action", "")).lower() == "idle") or
             (category in {"idle", "dhw"} and str(sample.attributes.get("hvac_action", "")).lower() == "heating"))
            for sample in fallback_checks)
        primary_interruption = any(start < sample.timestamp < end and mapping.get(sample.state.lower()) != category for sample in primary)
        dhw_samples = sorted((sample for sample in streams.get("actual_dhw", {}).get("samples", [])
                              if sample.timestamp.tzinfo and sample.timestamp < end), key=lambda sample: sample.timestamp)
        previous_dhw = [sample for sample in dhw_samples if sample.timestamp <= start]
        dhw_checks = previous_dhw[-1:] + [sample for sample in dhw_samples if start < sample.timestamp < end]
        conflicting_dhw = category in {"confirmed_heating", "idle", "cooling"} and any(
            sample.timestamp + freshness > start and sample.state.lower() == "heating_water" for sample in dhw_checks)
        if category in roles:
            reason = ("transition" if entry.get("mode_transition") or primary_interruption else
                "primary_missing_or_stale" if not obs or mapping.get(obs.state.lower()) != category or
                    start-obs.timestamp >= freshness or end > obs.timestamp+freshness or
                    (entry.get("state") or {}).get("source") != "primary" else
                "fallback_or_dhw_conflict" if conflicting_fallback or conflicting_dhw else
                "power_coverage_incomplete" if stats["coverage_percent"] != 100 else None)
            if reason:
                exclusion_reasons[reason] = exclusion_reasons.get(reason, 0) + 1
        else:
            reason = "not_primary_reference"
        if category in roles and reason is None:
            ref = references[category]
            ref["interval_count"] += 1
            episodes.append({"start": start.isoformat(), "end": end.isoformat(), "category": category,
                "mean_w": stats["mean_w"], "coverage_percent": stats["coverage_percent"],
                "mapping_verified": False})
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
            "references": refs, "reference_exclusions": exclusion_reasons, "_episodes": episodes,
            "candidates": candidates, "candidate_totals": totals,
            "validated_recovery_kwh": 0 if any(e.get("status") == "accepted" for e in trace) else None}


def validate_power_model(intervals, *, minimum_training_episodes=2, minimum_coverage_percent=100):
    """Evaluate only mapping-verified complete episodes in chronological order.

    Episode means define category ranges. Ambiguous overlapping ranges abstain.
    The conservative validation gate requires perfect held-out precision/recall
    and no abstentions; this is a diagnostic gate, not production classification.
    """
    eligible = []
    for row in intervals:
        try:
            start, end = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])
            value = float(row["mean_w"])
            if (not row.get("mapping_verified") or not row.get("episode_id") or not start.tzinfo or not end.tzinfo or end <= start
                    or not isfinite(value) or value < 0 or row.get("coverage_percent", 0) < minimum_coverage_percent
                    or row.get("category") not in {"confirmed_heating", "dhw", "idle", "cooling"}):
                continue
            eligible.append({"start": start, "end": end, "category": row["category"], "mean_w": value,
                             "episode_id": row["episode_id"]})
        except (KeyError, TypeError, ValueError):
            continue
    eligible.sort(key=lambda row: (row["start"], row["end"]))
    episodes = []
    closed_episode_ids = set()
    for row in eligible:
        if episodes and row["start"] < episodes[-1]["end"]:
            return {"verdict": "NOT VALIDATED", "reason": "overlapping reference intervals", "training_episodes": 0, "validation_episodes": 0}
        duration = (row["end"]-row["start"]).total_seconds()
        if episodes and row["episode_id"] == episodes[-1]["episode_id"]:
            episode = episodes[-1]
            if row["category"] != episode["category"]:
                return {"verdict": "NOT VALIDATED", "reason": "inconsistent episode category", "training_episodes": 0, "validation_episodes": 0}
            episode["weighted_sum"] += row["mean_w"]*duration
            episode["duration"] += duration
            episode["end"] = row["end"]
        else:
            if row["episode_id"] in closed_episode_ids:
                return {"verdict": "NOT VALIDATED", "reason": "noncontiguous episode identity", "training_episodes": 0, "validation_episodes": 0}
            if episodes:
                closed_episode_ids.add(episodes[-1]["episode_id"])
            episodes.append({**row, "weighted_sum": row["mean_w"]*duration, "duration": duration})
    split = int(len(episodes)*0.7)
    train, validation = episodes[:split], episodes[split:]
    categories = sorted({row["category"] for row in episodes})
    result = {"verdict": "NOT VALIDATED", "reason": "insufficient independent verified episodes",
        "eligible_episodes": len(episodes), "training_episodes": len(train), "validation_episodes": len(validation),
        "training_end_utc": train[-1]["end"].isoformat() if train else None,
        "validation_start_utc": validation[0]["start"].isoformat() if validation else None,
        "confusion_matrix": None, "precision": None, "recall": None, "abstention_rate": None, "ranges_w": {}}
    if (minimum_training_episodes < 1 or len(categories) < 2 or not validation or
        (train and train[-1]["end"] >= validation[0]["start"]) or
        any(sum(x["category"] == c for x in train) < minimum_training_episodes or
            not any(x["category"] == c for x in validation) for c in categories)):
        return result
    ranges = {c: [min(x["weighted_sum"]/x["duration"] for x in train if x["category"] == c),
                  max(x["weighted_sum"]/x["duration"] for x in train if x["category"] == c)] for c in categories}
    matrix = {c: {pred: 0 for pred in categories + ["abstain"]} for c in categories}
    for episode in validation:
        pred = classify_power(episode["weighted_sum"]/episode["duration"], ranges)
        matrix[episode["category"]][pred or "abstain"] += 1
    precision, recall = {}, {}
    for category in categories:
        true_positive = matrix[category][category]
        predicted = sum(matrix[actual][category] for actual in categories)
        actual = sum(matrix[category].values())
        precision[category] = true_positive/predicted if predicted else None
        recall[category] = true_positive/actual if actual else None
    abstained = sum(matrix[c]["abstain"] for c in categories)
    success = not abstained and all(precision[c] == 1 and recall[c] == 1 for c in categories)
    result.update({"verdict": "VALIDATED" if success else "NOT VALIDATED",
        "reason": "held-out diagnostic criteria passed" if success else "held-out errors or abstentions",
        "confusion_matrix": matrix, "precision": precision, "recall": recall,
        "abstention_rate": abstained/len(validation), "ranges_w": ranges})
    return result


def classify_power(mean_w, ranges):
    """Return a unique distribution match; overlapping or unmatched ranges abstain."""
    if mean_w is None or not isfinite(mean_w):
        return None
    matched = [category for category, bounds in ranges.items() if bounds[0] <= mean_w <= bounds[1]]
    return matched[0] if len(matched) == 1 else None
