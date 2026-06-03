import os
import asyncio
import logging
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from jira_client import JiraClient
from datetime import datetime
from ranking import rank_resolvers

logger = logging.getLogger(__name__)

load_dotenv()

mcp = FastMCP("Jira User Ranker", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

jira = JiraClient(
    base_url=os.environ["JIRA_BASE_URL"],
    email=os.environ["JIRA_EMAIL"],
    api_token=os.environ["JIRA_API_TOKEN"],
)

SINCE_DAYS = 365      # recency window for "similar resolved" tickets
RESOLVED_CAP = 200    # max resolved tickets to aggregate (newest first)
TOP_N = 10            # candidates returned
ASSIGNABLE_MAX = 1000 # cap for the assignable-users fetch
OPEN_COUNT_CONCURRENCY = 8


@mcp.tool()
async def list_project_users(project_key: str) -> list[dict]:
    """List all active users who have a role in the given Jira project."""
    return await jira.get_project_members(project_key)


@mcp.tool()
async def get_user_recent_tickets(
    account_id: str,
    project_key: str,
    limit: int = 10,
) -> list[dict]:
    """Get the most recently updated tickets assigned to a user in a project."""
    return await jira.get_user_tickets(account_id, project_key, limit)


@mcp.tool()
async def get_project_roles(project_key: str) -> dict:
    """Return all roles defined in the project and the users assigned to each role."""
    return await jira.get_roles_with_members(project_key)


@mcp.tool()
async def rank_users_for_task(
    project_key: str,
    task_summary: str,
    issue_type: str = "",
    components: list[str] = [],
    labels: list[str] = [],
) -> list[dict]:
    """
    Rank active project members by who has actually resolved similar tickets.

    "Similar" = resolved tickets sharing the task's component (components[0]).
    Candidates are restricted to active, assignable users who resolved such a
    ticket within the recency window. Ranking:
    - expertise (70%): recency-weighted count of similar resolutions
    - workload  (30%): fewer open tickets ranks higher (tie-break/load-balance)

    Returns up to TOP_N candidates, best first. Returns [] when there is no
    component to match, no matching resolved tickets, or no active resolvers
    (the caller should then fall back to its own routing).

    Args:
        project_key:  Jira project key, e.g. "IIS"
        task_summary: Description or summary of the task (currently unused by ranking)
        issue_type:   Unused (kept for compatibility)
        components:   components[0] is the classification matched against history
        labels:       Unused (kept for compatibility)
    """
    component = components[0] if components else ""
    if not component:
        return []

    try:
        active, resolved = await asyncio.gather(
            jira.get_active_assignable_users(project_key, ASSIGNABLE_MAX),
            jira.search_resolved_by_component(
                project_key, component, SINCE_DAYS, RESOLVED_CAP
            ),
        )

        agg: dict[str, dict] = {}
        for ticket in resolved:
            account_id = ticket["assignee_id"]
            if account_id not in active:
                continue
            entry = agg.setdefault(
                account_id,
                {
                    "accountId": account_id,
                    "displayName": active[account_id],
                    "resolved_dates": [],
                },
            )
            entry["resolved_dates"].append(ticket["resolved_at"])

        if not agg:
            return []

        sem = asyncio.Semaphore(OPEN_COUNT_CONCURRENCY)

        async def _open_count(account_id):
            async with sem:
                return await jira.get_user_open_ticket_count(account_id, project_key)

        candidate_ids = list(agg.keys())
        open_counts = await asyncio.gather(*[_open_count(aid) for aid in candidate_ids])
        for account_id, open_count in zip(candidate_ids, open_counts):
            agg[account_id]["open_ticket_count"] = open_count
    except Exception:
        logger.exception("rank_users_for_task failed for project %s / component %r", project_key, component)
        return []

    ranked = rank_resolvers(
        list(agg.values()), reference_date=datetime.now().date()
    )
    return ranked[:TOP_N]


if __name__ == "__main__":
    mcp.run(transport="sse")
