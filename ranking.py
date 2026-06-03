"""Rank active resolvers by recency-weighted similar-resolution volume + workload."""
from datetime import date, datetime

HALF_LIFE_DAYS = 180   # a resolution this old contributes half weight
K = 2.0                # saturation constant: expertise = rw / (rw + K)
W_EXPERTISE = 0.7
W_WORKLOAD = 0.3
assert abs(W_EXPERTISE + W_WORKLOAD - 1.0) < 1e-9, "expertise/workload weights must sum to 1.0"


def _parse_date(value):
    """Parse a Jira resolutiondate (ISO string / date / datetime) to a date, or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _recency_weight(resolved_date, reference_date) -> float:
    """Exponential decay weight in (0,1]: 1.0 today, 0.5 at HALF_LIFE_DAYS old."""
    if resolved_date is None:
        return 0.0
    age_days = max(0, (reference_date - resolved_date).days)
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def _workload_score(open_ticket_count) -> float:
    """Inverse score — 0 open tickets → 1.0, grows heavier as tickets pile up."""
    return 1.0 / (1.0 + open_ticket_count * 0.25)


def rank_resolvers(candidates, reference_date):
    """Rank candidates who resolved similar tickets.

    candidates: [{accountId, displayName, resolved_dates: [iso_str|date,...],
                  open_ticket_count}]
    reference_date: a datetime.date used for recency decay.
    Returns a list sorted best-first with rank, score, and breakdown.
    """
    ranked = []
    for c in candidates:
        dates = [d for d in (_parse_date(x) for x in c.get("resolved_dates", [])) if d]
        rw = sum(_recency_weight(d, reference_date) for d in dates)
        expertise = rw / (rw + K) if rw > 0 else 0.0
        workload = _workload_score(c.get("open_ticket_count", 0))
        score = W_EXPERTISE * expertise + W_WORKLOAD * workload
        last_resolved = max(dates).isoformat() if dates else None
        ranked.append(
            {
                "rank": 0,
                "accountId": c["accountId"],
                "displayName": c["displayName"],
                "score": round(score, 4),
                "similar_resolved_count": len(dates),
                "last_resolved": last_resolved,
                "score_breakdown": {
                    "expertise": round(expertise, 4),
                    "workload": round(workload, 4),
                },
            }
        )

    # all sort keys: higher/later = better, hence reverse=True
    ranked.sort(
        key=lambda x: (
            x["score"],
            x["similar_resolved_count"],
            x["last_resolved"] or "",
        ),
        reverse=True,
    )
    for i, entry in enumerate(ranked, start=1):
        entry["rank"] = i
    return ranked
