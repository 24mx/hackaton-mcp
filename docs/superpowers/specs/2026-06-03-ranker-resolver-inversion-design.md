# Ranker Resolver-Inversion — Design

**Date:** 2026-06-03
**Status:** Approved (pending spec review)
**Repo:** hackaton-mcp (the deployed Jira User Ranker MCP server)

## Problem

`rank_users_for_task` produces near-useless rankings. Empirically, for IIS-91463
(component "Intershop"), all 268 candidates collapsed into 4 score buckets and
~130 people tied at exactly 0.47; "rank #1" was just the alphabetically-first of
that tie, with zero ticket history.

Root causes:
- **Candidate sourcing** uses `project_roles` first, which returns everyone ever
  added to a role — including inactive accounts (268 of them) — and only falls back
  to active assignable users if roles fail.
- **Specialization** matches the incoming `components`/`labels` against each member's
  recent tickets, but returned 0.0 for everyone (the history isn't tagged that way,
  and most members have no assigned tickets).
- **Workload** is ~uniform (almost everyone has 0 open tickets).
- That leaves **role** (20%) as the only live signal → 3 buckets → massive ties.

## Goal

Rank by **who actually resolved similar (same-classification) tickets**, considering
only **active** people. Invert the algorithm: instead of enumerating all members and
scoring each, find the resolved tickets that match the ticket's classification, take
their resolvers, keep the active ones, and rank by recency-weighted volume of similar
resolutions (workload as tie-break).

## Decisions

1. **Invert** to "resolvers of same-classification resolved tickets" (not enumerate-and-score).
2. **Match classification** on the structured **component** field (the triage app already
   predicts a component, passed as `components=[component]`).
3. **Active** = Jira `active` flag / assignable in project **AND** recent resolution activity
   (both). Recent activity falls out of the windowed resolved-ticket query; the active/assignable
   flag comes from `search_assignable_users_for_projects`.
4. **Scoring:** expertise (recency-weighted similar-resolution count, saturating) dominant,
   workload as load-balancing tie-break. **Role dropped.**
5. **No automated tests** for this change (per explicit user request). Verification is manual
   against the live ranker.

## Scope

All changes are in `hackaton-mcp/`. The triage app (`support-triage`) is unchanged: it
already calls `rank_users_for_task(project_key, task_summary, components=[component],
labels=[category])`. The tool signature is preserved; only its behavior changes.
`components[0]` is treated as the classification to match. `labels`/`issue_type`/`task_summary`
become unused by the new algorithm (kept in the signature for compatibility).

## Architecture & Data Flow

```
triage agent → rank_users_for_task(project_key, task_summary,
                                    components=[component], labels=[category])
                          │  component = components[0] if components else ""
                          ▼
  main.py  rank_users_for_task
   1. active set  ── jira_client.get_active_assignable_users(project_key)
   │                  → {accountId: displayName}        (active flag + assignable)
   2. similar work ── jira_client.search_resolved_by_component(project_key, component, since_days, cap)
   │                  JQL: project="X" AND component="<c>" AND statusCategory = Done
   │                       AND resolved >= -<since_days>d  ORDER BY resolved DESC   (maxResults=cap)
   │                  → [{key, assignee_id, assignee_name, resolved_at}]   (skip null assignee)
   3. aggregate   ── group by assignee_id → {count, resolved_dates[], last_resolved}
   4. candidates  ── resolvers ∩ active set             (active flag AND recent activity)
   5. enrich      ── workload = get_user_open_ticket_count(accountId)  (candidates only)
   6. score+rank  ── ranking.rank_resolvers(candidates, reference_date, since_days) → top_n
                          ▼
            ranked resolvers (top N) → agent picks rank #1
```

Candidates now come from resolved-ticket history, not a 268-member enumeration. Workload
is fetched only for the handful of real candidates.

## Components (units)

### `jira_client.py` (add two methods; uses the `jira` Python library already imported)
- `get_active_assignable_users(project_key) -> dict[str, str]`
  Wraps `search_assignable_users_for_projects("", project_key, maxResults=...)`; returns
  `{accountId: displayName}` for users with `active` truthy. (Generalizes the existing
  `_get_members_via_assignable`.)
- `search_resolved_by_component(project_key, component, since_days, cap) -> list[dict]`
  Runs JQL `project = "<project_key>" AND component = "<component>" AND statusCategory = Done
  AND resolved >= -<since_days>d ORDER BY resolved DESC`, `maxResults=cap`, fields
  `["assignee", "resolutiondate"]`. Returns `[{key, assignee_id, assignee_name, resolved_at}]`,
  skipping issues with no assignee. Component value is quoted/escaped for JQL.

### `ranking.py` (add `rank_resolvers`; retire `rank_users_by_task` and the role/spec helpers)
- `rank_resolvers(candidates, reference_date) -> list[dict]`
  `candidates`: `[{accountId, displayName, resolved_dates: [date,...], open_ticket_count}]`.
  Per candidate:
  ```
  recency_factor(d) = 0.5 ** (age_days(d, reference_date) / HALF_LIFE_DAYS)   # HALF_LIFE_DAYS = 180
  rw         = Σ recency_factor over resolved_dates
  expertise  = rw / (rw + K)                                                  # K = 2.0  → 0..1
  workload   = 1 / (1 + open_ticket_count * 0.25)
  score      = W_EXPERTISE * expertise + W_WORKLOAD * workload                # 0.7 / 0.3
  ```
  Sort by `score` desc, tie-break by `len(resolved_dates)` desc then `last_resolved` desc.
  Return each as `{rank, accountId, displayName, score, similar_resolved_count,
  last_resolved, score_breakdown: {expertise, workload}}`.

### `main.py` (rewrite `rank_users_for_task` body)
Orchestrates steps 1–6 above; `component = components[0] if components else ""`. Returns
the top `TOP_N` (default 10). Keeps the `@mcp.tool()` signature; updates the docstring to
describe the resolver-based ranking. Wraps Jira access in try/except → returns `[]` on
failure.

## Tunable constants (defaults, module-level in `ranking.py`/`main.py`)
`SINCE_DAYS = 365`, `HALF_LIFE_DAYS = 180`, `K = 2.0`, `W_EXPERTISE = 0.7`,
`W_WORKLOAD = 0.3`, `RESOLVED_CAP = 200`, `TOP_N = 10`,
`ASSIGNABLE_MAX = 1000` (cap for the assignable-users fetch).

## Empty & error handling (ranker returns `[]` → agent uses its fallback map)
- Empty/absent component (e.g. `N/A`) → `[]`.
- No resolved tickets match the component in the window → `[]`.
- Resolvers exist but none are active/assignable (empty intersection) → `[]`.
- Any Jira/JQL exception → caught, return `[]`, log server-side.
- Resolved ticket with no assignee → skipped in aggregation.
- More than `RESOLVED_CAP` matches → aggregate the most-recent `cap` (`ORDER BY resolved DESC`);
  deliberate recency-biased sample.
- Candidate sourcing via assignable-users search (browse-level) also avoids the project-roles
  401 seen on Pierce Jira for non-admins.

## Out of scope (possible follow-ups)
- Text/keyword fallback when the component match is too sparse (Q2 chose structured-only).
- Reconciling the triage app's predicted component values with the project's real Jira
  component names (a data-alignment concern; if they don't match, the lookup returns `[]`
  and the agent falls back).
- "Resolver" is approximated by the issue's assignee at resolution (Jira has no native
  resolver field).
- Automated tests (explicitly skipped for this change).

## Manual verification (after implementation)
1. Redeploy the ranker (or run locally) pointed at a Jira with resolved, component-tagged tickets.
2. Call `rank_users_for_task(project_key, task_summary, components=["<real component>"], labels=[...])`
   directly via an MCP client; confirm: candidates are few and active, scores vary (no flat tie),
   `similar_resolved_count`/`last_resolved` populated, rank #1 is a real recent resolver.
3. Call with `components=[]` or an unmatched component → confirm `[]` and that the triage agent
   falls back (`ranking_source: "fallback"`).
