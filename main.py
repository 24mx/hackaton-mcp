import os
import asyncio
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from jira_client import JiraClient
from ranking import rank_users_by_task

load_dotenv()

mcp = FastMCP("Jira User Ranker", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

jira = JiraClient(
    base_url=os.environ["JIRA_BASE_URL"],
    email=os.environ["JIRA_EMAIL"],
    api_token=os.environ["JIRA_API_TOKEN"],
)


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
    Given a Jira task, return a ranked list of project users best suited to handle it.

    Ranking weights:
    - 45% specialization: how often user handled similar ticket types/components/labels
    - 35% workload:       fewer open tickets = higher score
    - 20% role:           role seniority in the project

    Args:
        project_key:  Jira project key, e.g. "PROJ"
        task_summary: Description or summary of the task
        issue_type:   Issue type name, e.g. "Bug", "Story", "Task"
        components:   List of component names from the task
        labels:       List of labels from the task
    """
    members = await jira.get_project_members(project_key)

    async def enrich(member: dict) -> dict:
        account_id = member["accountId"]
        recent, open_count = await asyncio.gather(
            jira.get_user_tickets(account_id, project_key, limit=20),
            jira.get_user_open_ticket_count(account_id, project_key),
        )
        return {**member, "recent_tickets": recent, "open_ticket_count": open_count}

    enriched = await asyncio.gather(*[enrich(m) for m in members])

    return rank_users_by_task(
        users=list(enriched),
        issue_type=issue_type,
        components=components,
        labels=labels,
        task_summary=task_summary,
    )


if __name__ == "__main__":
    mcp.run(transport="sse")
