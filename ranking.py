# Ranking weights — must sum to 1.0
WEIGHT_SPECIALIZATION = 0.45
WEIGHT_WORKLOAD = 0.35
WEIGHT_ROLE = 0.20

# Jira default role names → seniority weight (0.0–1.0)
ROLE_WEIGHTS: dict[str, float] = {
    "Administrator": 0.6,
    "Project Lead": 1.0,
    "atlassian-addons-project-access": 0.0,
    "Member": 0.7,
    "Developer": 0.9,
    "Viewer": 0.2,
    "Service Desk Team": 0.6,
    "Service Desk Customer - Portal Access": 0.1,
}
DEFAULT_ROLE_WEIGHT = 0.5


def _role_score(roles: list[str]) -> float:
    if not roles:
        return DEFAULT_ROLE_WEIGHT
    return max(ROLE_WEIGHTS.get(r, DEFAULT_ROLE_WEIGHT) for r in roles)


def _specialization_score(
    recent_tickets: list[dict],
    issue_type: str,
    components: list[str],
    labels: list[str],
) -> float:
    """Score 0–1: how well user's history matches the incoming task."""
    if not recent_tickets:
        return 0.0

    criteria_count = sum([bool(issue_type), bool(components), bool(labels)])
    if criteria_count == 0:
        return 0.0

    total_match = 0.0
    for ticket in recent_tickets:
        match = 0
        if issue_type and ticket.get("type", "").lower() == issue_type.lower():
            match += 1
        if components:
            ticket_comps = {c.lower() for c in ticket.get("components", [])}
            if any(c.lower() in ticket_comps for c in components):
                match += 1
        if labels:
            ticket_labels = {l.lower() for l in ticket.get("labels", [])}
            if any(l.lower() in ticket_labels for l in labels):
                match += 1
        total_match += match / criteria_count

    return total_match / len(recent_tickets)


def _workload_score(open_ticket_count: int) -> float:
    """Inverse score — 0 open tickets → 1.0, grows heavier as tickets pile up."""
    return 1.0 / (1.0 + open_ticket_count * 0.25)


def rank_users_by_task(
    users: list[dict],
    issue_type: str,
    components: list[str],
    labels: list[str],
    task_summary: str,
) -> list[dict]:
    ranked = []
    for user in users:
        spec = _specialization_score(
            user.get("recent_tickets", []),
            issue_type,
            components,
            labels,
        )
        workload = _workload_score(user.get("open_ticket_count", 0))
        role = _role_score(user.get("roles", []))

        score = (
            WEIGHT_SPECIALIZATION * spec
            + WEIGHT_WORKLOAD * workload
            + WEIGHT_ROLE * role
        )

        ranked.append(
            {
                "rank": 0,  # filled below
                "accountId": user["accountId"],
                "displayName": user["displayName"],
                "roles": user.get("roles", []),
                "open_tickets": user.get("open_ticket_count", 0),
                "score": round(score, 4),
                "score_breakdown": {
                    "specialization": round(spec, 4),
                    "workload": round(workload, 4),
                    "role": round(role, 4),
                },
            }
        )

    ranked.sort(key=lambda x: x["score"], reverse=True)
    for i, entry in enumerate(ranked, start=1):
        entry["rank"] = i

    return ranked
