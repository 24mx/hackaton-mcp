import asyncio
from jira import JIRA


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str):
        self._jira = JIRA(server=base_url, basic_auth=(email, api_token))

    async def get_project_members(self, project_key: str) -> list[dict]:
        """Return unique users who can be assigned to issues in the project."""
        try:
            return await asyncio.to_thread(self._get_members_via_roles, project_key)
        except Exception:
            return await asyncio.to_thread(self._get_members_via_assignable, project_key)

    def _get_members_via_roles(self, project_key: str) -> list[dict]:
        roles = self._jira.project_roles(project_key)
        members: dict[str, dict] = {}

        for role_name, role_info in roles.items():
            role_id = role_info["id"]
            role_detail = self._jira.project_role(project_key, role_id)
            for actor in role_detail.actors:
                if actor.type != "atlassian-user-role-actor":
                    continue
                account_id = actor.actorUser.accountId
                if account_id not in members:
                    members[account_id] = {
                        "accountId": account_id,
                        "displayName": actor.displayName,
                        "roles": [],
                    }
                members[account_id]["roles"].append(role_name)

        return list(members.values())

    def _get_members_via_assignable(self, project_key: str) -> list[dict]:
        users = self._jira.search_assignable_users_for_projects("", project_key, maxResults=100)
        return [
            {
                "accountId": u.accountId,
                "displayName": u.displayName,
                "roles": [],
            }
            for u in users
            if getattr(u, "active", True)
        ]

    async def get_user_tickets(
        self, account_id: str, project_key: str, limit: int = 10
    ) -> list[dict]:
        jql = (
            f'project = "{project_key}" AND assignee = "{account_id}" '
            f"ORDER BY updated DESC"
        )
        return await asyncio.to_thread(self._search_tickets, jql, limit)

    def _search_tickets(self, jql: str, limit: int) -> list[dict]:
        issues = self._jira.search_issues(
            jql,
            maxResults=limit,
            fields=["summary", "status", "issuetype", "components", "labels"],
        )
        return [
            {
                "key": issue.key,
                "summary": issue.fields.summary,
                "status": issue.fields.status.name,
                "type": issue.fields.issuetype.name,
                "components": [c.name for c in issue.fields.components],
                "labels": list(issue.fields.labels),
            }
            for issue in issues
        ]

    async def get_user_open_ticket_count(self, account_id: str, project_key: str) -> int:
        jql = (
            f'project = "{project_key}" AND assignee = "{account_id}" '
            f'AND status not in (Done, Closed, Resolved, Cancelled)'
        )
        result = await asyncio.to_thread(
            self._jira.search_issues, jql, maxResults=1, fields=["summary"]
        )
        return result.total

    async def get_roles_with_members(self, project_key: str) -> dict:
        return await asyncio.to_thread(self._get_members_via_roles_dict, project_key)

    def _get_members_via_roles_dict(self, project_key: str) -> dict:
        roles = self._jira.project_roles(project_key)
        result: dict[str, list] = {}

        for role_name, role_info in roles.items():
            role_id = role_info["id"]
            role_detail = self._jira.project_role(project_key, role_id)
            users = [
                {
                    "accountId": actor.actorUser.accountId,
                    "displayName": actor.displayName,
                }
                for actor in role_detail.actors
                if actor.type == "atlassian-user-role-actor"
            ]
            if users:
                result[role_name] = users

        return result
