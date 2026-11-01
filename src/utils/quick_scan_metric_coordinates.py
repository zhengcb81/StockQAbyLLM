"""Pure metric-row uniqueness for the unchanged standard-answer wire format.

The row's owning answer/fact item supplies its economic scope. Definition text,
value and evidence are not coordinates and cannot disguise a duplicate. Segment
metrics belong to separate fact items or explicitly scoped custom metric IDs.
Single legacy rows may have unknown dates; repeated IDs need an explicit end.
"""

from typing import Any


def metric_coordinate(metric: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        metric.get(key) for key in ("metric_id", "period_start", "period_end", "basis", "currency")
    )


def validate_metric_coordinates(metrics: list[dict[str, Any]]) -> None:
    """Accept distinct periods/bases/currencies; reject duplicate/ambiguous rows."""
    by_id: dict[str, list[dict[str, Any]]] = {}
    for metric in metrics:
        previous = by_id.setdefault(metric["metric_id"], [])
        for other in previous:
            if metric_coordinate(metric) == metric_coordinate(other):
                raise ValueError("duplicate metric coordinate")
            if metric.get("unit") != other.get("unit"):
                raise ValueError("repeated metric has incompatible unit")
            if not metric.get("period_end") or not other.get("period_end"):
                raise ValueError("repeated metric requires explicit period_end")
            if all(
                metric.get(key) == other.get(key) for key in ("period_end", "basis", "currency")
            ) and (not metric.get("period_start") or not other.get("period_start")):
                raise ValueError("duplicate metric with ambiguous period_start")
        previous.append(metric)
