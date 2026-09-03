"""Which score components actually predicted paper-signal outcomes.

Pure and deterministic: given the same resolved-signal records it always returns the
same analysis. It never tunes anything automatically; it only surfaces the lift so an
operator can decide whether a component earns its weight.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from statistics import median

RESOLVED_STATUSES = ("TARGET", "STOP")


@dataclass(frozen=True)
class ScoreRecord:
    strategy_version: str
    score_breakdown: dict
    status: str
    realized_r: float


@dataclass(frozen=True)
class Bucket:
    samples: int
    wins: int
    win_rate_percent: float
    average_realized_r: float


@dataclass(frozen=True)
class ComponentInsight:
    component: str
    threshold: float
    above: Bucket
    below: Bucket
    lift_r: float  # above.average_realized_r - below.average_realized_r


def _bucket(records: list[ScoreRecord]) -> Bucket:
    if not records:
        return Bucket(0, 0, 0.0, 0.0)
    wins = sum(record.status == "TARGET" for record in records)
    total_r = sum(record.realized_r for record in records)
    return Bucket(
        samples=len(records),
        wins=wins,
        win_rate_percent=round(wins * 100 / len(records), 2),
        average_realized_r=round(total_r / len(records), 4),
    )


def analyse_score_components(records: Iterable[ScoreRecord], minimum_samples: int = 6) -> dict:
    """Split resolved signals by each component's median score and report the outcome lift."""
    resolved = [
        record for record in records if record.status in RESOLVED_STATUSES and isinstance(record.score_breakdown, dict)
    ]
    if len(resolved) < minimum_samples:
        return {"resolved_signals": len(resolved), "insufficient_data": True, "components": []}

    components = sorted({key for record in resolved for key in record.score_breakdown})
    insights: list[ComponentInsight] = []
    for component in components:
        scored = [record for record in resolved if isinstance(record.score_breakdown.get(component), int | float)]
        if len(scored) < minimum_samples:
            continue
        threshold = float(median(float(record.score_breakdown[component]) for record in scored))
        above = [record for record in scored if float(record.score_breakdown[component]) >= threshold]
        below = [record for record in scored if float(record.score_breakdown[component]) < threshold]
        if not above or not below:
            continue
        above_bucket = _bucket(above)
        below_bucket = _bucket(below)
        insights.append(
            ComponentInsight(
                component=component,
                threshold=round(threshold, 2),
                above=above_bucket,
                below=below_bucket,
                lift_r=round(above_bucket.average_realized_r - below_bucket.average_realized_r, 4),
            )
        )

    insights.sort(key=lambda item: item.lift_r, reverse=True)
    overall = _bucket(resolved)
    return {
        "resolved_signals": len(resolved),
        "insufficient_data": False,
        "overall": {
            "samples": overall.samples,
            "win_rate_percent": overall.win_rate_percent,
            "average_realized_r": overall.average_realized_r,
        },
        "components": [
            {
                "component": item.component,
                "threshold": item.threshold,
                "lift_r": item.lift_r,
                "above": _bucket_dict(item.above),
                "below": _bucket_dict(item.below),
            }
            for item in insights
        ],
    }


def _bucket_dict(bucket: Bucket) -> dict:
    return {
        "samples": bucket.samples,
        "win_rate_percent": bucket.win_rate_percent,
        "average_realized_r": bucket.average_realized_r,
    }
