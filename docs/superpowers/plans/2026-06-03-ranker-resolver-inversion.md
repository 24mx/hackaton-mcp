# Ranker Resolver-Inversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rerank `rank_users_for_task` by who actually resolved similar (same-component) tickets among active/assignable users, replacing the flat-tie member-enumeration scoring.

**Architecture:** Invert the algorithm in the `hackaton-mcp` server: fetch active assignable users, fetch resolved tickets matching the ticket's component within a recency window, aggregate their assignees (resolvers), keep those in the active set, then score by recency-weighted similar-resolution volume (dominant) plus a workload tie-break. Role scoring is removed. The MCP tool signature is unchanged so the triage app needs no changes.

**Tech Stack:** Python 3.12, `jira` (jira-python) library, `mcp[cli]` FastMCP, Fly.io deploy. Repo: `/home/joel/support-triage/hackaton-mcp`.

**Conventions:** No automated tests for this change (per user instruction). Each task: implement with full code, run a syntax/sanity check, commit. Manual verification + redeploy at the end. Commit messages end with:
```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

**Verified facts (do not re-derive):**
- `jira_client.py` wraps `from jira import JIRA`; existing methods use `async def` + `asyncio.to_thread(sync_helper, ...)`. Reuse that pattern.
- `JIRA.search_assignable_users_for_projects("", project_key, maxResults=N)` returns user resources with `.accountId`, `.displayName`, `.active`.
- `JIRA.search_issues(jql, maxResults=N, fields=[...])` returns issues; `issue.fields.assignee` is a user resource or `None`; `issue.fields.resolutiondate` is an ISO string (e.g. `"2024-05-01T12:00:00.000+0000"`).
- JQL relative dates: `resolved >= -365d`. `statusCategory = Done` is valid. `component = "X"` is valid.
- `get_user_open_ticket_count(account_id, project_key)` already exists and returns an int.
- `main.py` builds `JiraClient(...)` at import time from env vars, so importing it requires Jira creds — use `python -m py_compile` for syntax checks, NOT `import main`.

---

## File Structure

- **Modify** `jira_client.py` — add `get_active_assignable_users()` and `search_resolved_by_component()` (+ their sync helpers). Leave existing methods in place.
- **Modify** `ranking.py` — replace contents: add `rank_resolvers()` + helpers and module constants; remove the now-unused `WEIGHT_*`, `ROLE_WEIGHTS`, `_role_score`, `_specialization_score`, `rank_users_by_task`. Keep a `_workload_score` helper.
- **Modify** `main.py` — rewrite the `rank_users_for_task` body + add constants and imports. Signature and other tools unchanged.

---

## Task 1: Jira client — active users + resolved-by-component lookups

**Files:** Modify `jira_client.py`

- [ ] **Step 1: Add the two async methods + sync helpers**

Add these methods to the `JiraClient` class (e.g. after `get_user_open_ticket_count`):

```python
    async def get_active_assignable_users(
        self, project_key: str, max_results: int = 1000
    ) -> dict[str, str]:
        """Return {accountId: displayName} for active users assignable in the project."""
        return await asyncio.to_thread(
            self._active_assignable, project_key, max_results
        )

    def _active_assignable(self, project_key: str, max_results: int) -> dict[str, str]:
        users = self._jira.search_assignable_users_for_projects(
            "", project_key, maxResults=max_results
        )
        return {
            u.accountId: u.displayName
            for u in users
            if getattr(u, "active", True)
        }

    async def search_resolved_by_component(
        self, project_key: str, component: str, since_days: int, cap: int
    ) -> list[dict]:
        """Resolved tickets matching a component within the recency window, newest first."""
        return await asyncio.to_thread(
            self._resolved_by_component, project_key, component, since_days, cap
        )

    def _resolved_by_component(
        self, project_key: str, component: str, since_days: int, cap: int
    ) -> list[dict]:
        safe_component = component.replace('"', '\\"')
        jql = (
            f'project = "{project_key}" AND component = "{safe_component}" '
            f"AND statusCategory = Done AND resolved >= -{int(since_days)}d "
            f"ORDER BY resolved DESC"
        )
        issues = self._jira.search_issues(
            jql, maxResults=cap, fields=["assignee", "resolutiondate"]
        )
        out = []
        for issue in issues:
            assignee = issue.fields.assignee
            if assignee is None:
                continue
            out.append(
                {
                    "key": issue.key,
                    "assignee_id": assignee.accountId,
                    "assignee_name": assignee.displayName,
                    "resolved_at": issue.fields.resolutiondate,
                }
            )
        return out
```

- [ ] **Step 2: Syntax check**

Run: `cd /home/joel/support-triage/hackaton-mcp && python -m py_compile jira_client.py && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
cd /home/joel/support-triage/hackaton-mcp
git add jira_client.py
git commit -m "feat: add active-assignable + resolved-by-component Jira lookups

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Ranking — resolver scoring

**Files:** Modify `ranking.py` (replace entire contents)

- [ ] **Step 1: Replace `ranking.py` with the resolver scorer**

Overwrite `ranking.py` with exactly:

```python
"""Rank active resolvers by recency-weighted similar-resolution volume + workload."""
from datetime import date, datetime

HALF_LIFE_DAYS = 180   # a resolution this old contributes half weight
K = 2.0                # saturation constant: expertise = rw / (rw + K)
W_EXPERTISE = 0.7
W_WORKLOAD = 0.3


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


def _recency_weight(resolved_date, reference_date):
    if resolved_date is None:
        return 0.0
    age_days = max(0, (reference_date - resolved_date).days)
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def _workload_score(open_ticket_count):
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
```

- [ ] **Step 2: Syntax check + inline sanity (pure module, no Jira/creds needed)**

Run:
```bash
cd /home/joel/support-triage/hackaton-mcp && python -m py_compile ranking.py && \
python -c "
from datetime import date
from ranking import rank_resolvers
ref = date(2024, 6, 1)
cands = [
  {'accountId':'a','displayName':'Expert','resolved_dates':['2024-05-20','2024-05-25','2024-04-01'],'open_ticket_count':1},
  {'accountId':'b','displayName':'OneFix','resolved_dates':['2024-05-28'],'open_ticket_count':0},
  {'accountId':'c','displayName':'Stale','resolved_dates':['2023-01-01'],'open_ticket_count':0},
]
r = rank_resolvers(cands, ref)
for x in r: print(x['rank'], x['displayName'], x['score'], x['score_breakdown'], x['similar_resolved_count'])
assert r[0]['displayName']=='Expert', 'most recent-similar resolver should rank first'
assert r[0]['score'] > r[-1]['score'], 'scores must vary (no flat tie)'
print('SANITY OK')
"
```
Expected: three rows with **distinct** scores, `Expert` rank 1, ending `SANITY OK`.

- [ ] **Step 3: Commit**

```bash
cd /home/joel/support-triage/hackaton-mcp
git add ranking.py
git commit -m "feat: resolver-based ranking (recency-weighted expertise + workload)

Replaces role/specialization scoring that produced flat ties.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Orchestrate the inversion in `rank_users_for_task`

**Files:** Modify `main.py`

- [ ] **Step 1: Update imports + add constants**

In `main.py`, change the ranking import line:
```python
from ranking import rank_users_by_task
```
to:
```python
from datetime import datetime
from ranking import rank_resolvers
```

Add these module-level constants just after the `jira = JiraClient(...)` block:
```python
SINCE_DAYS = 365      # recency window for "similar resolved" tickets
RESOLVED_CAP = 200    # max resolved tickets to aggregate (newest first)
TOP_N = 10            # candidates returned
ASSIGNABLE_MAX = 1000 # cap for the assignable-users fetch
```

- [ ] **Step 2: Replace the `rank_users_for_task` function body**

Replace the entire `@mcp.tool() async def rank_users_for_task(...)` definition with:

```python
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

        candidate_ids = list(agg.keys())
        open_counts = await asyncio.gather(
            *[
                jira.get_user_open_ticket_count(account_id, project_key)
                for account_id in candidate_ids
            ]
        )
        for account_id, open_count in zip(candidate_ids, open_counts):
            agg[account_id]["open_ticket_count"] = open_count
    except Exception:
        return []

    ranked = rank_resolvers(
        list(agg.values()), reference_date=datetime.now().date()
    )
    return ranked[:TOP_N]
```

- [ ] **Step 3: Syntax check**

Run: `cd /home/joel/support-triage/hackaton-mcp && python -m py_compile main.py && echo OK`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd /home/joel/support-triage/hackaton-mcp
git add main.py
git commit -m "feat: rank_users_for_task uses resolver inversion over active users

Component (components[0]) -> resolved-ticket resolvers, intersected with
active assignable users, scored by recency-weighted expertise + workload.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Manual Verification (after all tasks)

Requires the ranker running against a Jira with resolved, component-tagged tickets, and an MCP client. Two ways to run:

**Option A — local run** (needs the deployed Jira creds in env):
```bash
cd /home/joel/support-triage/hackaton-mcp
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
JIRA_BASE_URL=... JIRA_EMAIL=... JIRA_API_TOKEN=... PORT=8000 ./.venv/bin/python main.py
```

**Option B — redeploy to Fly.io** (outward action; run only with the user's go-ahead):
```bash
cd /home/joel/support-triage/hackaton-mcp && flyctl deploy
```

Then exercise the tool via an MCP SSE client (pattern from earlier `/tmp/call_ranker.py`):
1. Call `rank_users_for_task(project_key, task_summary, components=["<real component>"], labels=[])`.
   - Confirm: few candidates, **scores vary** (no flat 0.47 tie), `similar_resolved_count` and
     `last_resolved` populated, rank #1 is a recent real resolver, role no longer in the breakdown.
2. Call with `components=[]` or an unmatched component → confirm `[]`.
3. (End-to-end) From `support-triage`, run `MCP_RANKER_URL=<sse-url> uv run triage.py <KEY> --dry-run`
   → confirm `ranking_source: "ranker"` with a varied score when a component matches, and a
   fall back to the static map when it returns `[]`.

---

## Self-Review Notes

- **Spec coverage:** invert (Task 3) ✔; match on component field (Task 1 `search_resolved_by_component`) ✔; active = flag/assignable AND recent activity (Task 3 intersection of assignable-active set with windowed resolvers) ✔; expertise+workload scoring, role dropped (Task 2) ✔; empty/error → `[]` (Task 3 guards + try/except) ✔; constants/defaults (Task 2 + Task 3) ✔; tests skipped per instruction ✔.
- **Name/type consistency:** `get_active_assignable_users`, `search_resolved_by_component`, `rank_resolvers`, keys `assignee_id`/`resolved_at`/`resolved_dates`/`open_ticket_count`, constants `SINCE_DAYS`/`RESOLVED_CAP`/`TOP_N`/`ASSIGNABLE_MAX`/`HALF_LIFE_DAYS`/`K`/`W_EXPERTISE`/`W_WORKLOAD` used consistently across tasks. ✔
- **No placeholders:** every step has full code + exact commands. ✔
