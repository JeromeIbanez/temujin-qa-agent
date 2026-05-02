"""
Main orchestrator for the QA pipeline.

Flow:
  1. Run smoke tests
  2. Fetch diff via GitHub API
  3. Analyze diff with AI
  4. If tests failed → send failure email, exit 1
  5. If tests passed + simple → auto-merge to production
  6. If tests passed + complex → create/update PR + send approval email
"""
import json
import os
import sys
import time

import requests
from github import Github

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.analyze import analyze_diff, SIMPLE
from notifications.email import (
    send_failure,
    send_approval_request,
    send_auto_deployed,
)
from tests.smoke import run as run_smoke


def main():
    # ── Config from environment ──────────────────────────────────────────────
    staging_url = os.environ["STAGING_URL"]
    staging_api_url = os.environ.get("STAGING_API_URL", "")
    production_url = os.environ["PRODUCTION_URL"]
    notify_email = os.environ["NOTIFY_EMAIL"]
    app_name = os.environ["APP_NAME"]
    repo_name = os.environ["GITHUB_REPO"]
    staging_branch = os.environ.get("STAGING_BRANCH", "staging")
    production_branch = os.environ.get("PRODUCTION_BRANCH", "main")
    commit_msg = os.environ.get("COMMIT_MESSAGE", "No commit message")
    gh_token = os.environ["GH_TOKEN"]

    # Short title: first line only, max 72 chars
    _first_line = commit_msg.splitlines()[0] if commit_msg else "No commit message"
    pr_title = (
        f"[QA] Deploy to production: {_first_line[:72]}…"
        if len(_first_line) > 72
        else f"[QA] Deploy to production: {_first_line}"
    )

    print(f"[QA] Starting pipeline for {app_name}")
    print(f"[QA] Commit: {commit_msg}")

    # ── Step 1: Smoke tests ──────────────────────────────────────────────────
    print("[QA] Running smoke tests...")
    os.environ["STAGING_API_URL"] = staging_api_url
    smoke = run_smoke(staging_url)
    print(json.dumps(smoke, indent=2))

    # ── Step 2: Fetch diff via GitHub API ────────────────────────────────────
    print("[QA] Fetching diff via GitHub API...")
    gh = Github(gh_token)
    repo = gh.get_repo(repo_name)
    try:
        comparison = repo.compare(production_branch, staging_branch)
        diff = "\n".join(
            f"--- {f.filename}\n{f.patch or ''}"
            for f in comparison.files
            if f.patch
        )
    except Exception as e:
        print(f"[QA] Could not fetch diff: {e}. Using commit message only.")
        diff = f"Commit: {commit_msg}"

    # ── Step 3: AI diff analysis ─────────────────────────────────────────────
    print("[QA] Analyzing diff with AI...")
    analysis = analyze_diff(diff)
    print(json.dumps(analysis, indent=2))

    classification = analysis.get("classification", "complex")
    summary = analysis.get("summary", "No summary available.")
    reasoning = analysis.get("reasoning", "")
    risk_areas = analysis.get("risk_areas", [])

    # ── Step 4: Tests failed → block deploy ──────────────────────────────────
    if not smoke["passed"]:
        print("[QA] Smoke tests failed. Blocking deploy.")
        send_failure(
            to=notify_email,
            app_name=app_name,
            commit_msg=commit_msg,
            smoke_results=smoke["results"],
            diff_summary=summary,
        )
        sys.exit(1)

    # ── Step 5: Simple → PR + auto-merge (merges once CI checks pass) ──────────
    if classification == SIMPLE:
        print("[QA] Change classified as SIMPLE. Creating PR with auto-merge enabled.")
        existing_prs = list(repo.get_pulls(
            state="open",
            head=f"{repo.owner.login}:{staging_branch}",
            base=production_branch,
        ))
        if existing_prs:
            pr = existing_prs[0]
            pr.edit(
                title=pr_title,
                body=(
                    f"**Auto-deploy (SIMPLE)**\n\n"
                    f"**Summary:** {summary}\n\n"
                    f"QA passed. Classified as SIMPLE — will merge to production automatically once CI passes."
                ),
            )
        else:
            pr = repo.create_pull(
                title=pr_title,
                body=(
                    f"**Auto-deploy (SIMPLE)**\n\n"
                    f"**Summary:** {summary}\n\n"
                    f"QA passed. Classified as SIMPLE — will merge to production automatically once CI passes."
                ),
                head=staging_branch,
                base=production_branch,
            )
        _enable_auto_merge(pr.node_id, gh_token)
        print(f"[QA] PR ready: {pr.html_url}")
        send_auto_deployed(
            to=notify_email,
            app_name=app_name,
            commit_msg=commit_msg,
            diff_summary=summary,
            production_url=production_url,
        )

    # ── Step 6: Complex → create or reuse PR + email ─────────────────────────
    else:
        print("[QA] Change classified as COMPLEX. Creating PR for review.")
        existing_prs = list(repo.get_pulls(
            state="open",
            head=f"{repo.owner.login}:{staging_branch}",
            base=production_branch,
        ))
        if existing_prs:
            pr = existing_prs[0]
            pr.edit(
                title=pr_title,
                body=(
                    f"**Summary:** {summary}\n\n"
                    f"**Why review needed:** {reasoning}\n\n"
                    f"**Risk areas:** {', '.join(risk_areas) if risk_areas else 'None'}\n\n"
                    f"Approved by QA agent — all tests passed. Merge to deploy to production."
                ),
            )
            print(f"[QA] Updated existing PR: {pr.html_url}")
        else:
            pr = repo.create_pull(
                title=pr_title,
                body=(
                    f"**Summary:** {summary}\n\n"
                    f"**Why review needed:** {reasoning}\n\n"
                    f"**Risk areas:** {', '.join(risk_areas) if risk_areas else 'None'}\n\n"
                    f"Approved by QA agent — all tests passed. Merge to deploy to production."
                ),
                head=staging_branch,
                base=production_branch,
            )
            print(f"[QA] PR created: {pr.html_url}")

        send_approval_request(
            to=notify_email,
            app_name=app_name,
            commit_msg=commit_msg,
            diff_summary=summary,
            diff_reasoning=reasoning,
            risk_areas=risk_areas,
            smoke_results=smoke["results"],
            pr_url=pr.html_url,
            staging_url=staging_url,
        )

    print("[QA] Pipeline complete.")


def _enable_auto_merge(pr_node_id: str, gh_token: str) -> None:
    """Enable auto-merge on a PR via GitHub GraphQL API.

    Uses MERGE method so it respects branch protection required status checks —
    the PR merges automatically once all checks pass, never before.
    """
    query = """
    mutation($prId: ID!) {
      enablePullRequestAutoMerge(input: {
        pullRequestId: $prId,
        mergeMethod: MERGE
      }) {
        pullRequest {
          autoMergeRequest { enabledAt }
        }
      }
    }
    """
    resp = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": {"prId": pr_node_id}},
        headers={
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        print(f"[QA] Warning: could not enable auto-merge: {data['errors']}")
    else:
        print("[QA] Auto-merge enabled — PR will merge once CI checks pass.")


if __name__ == "__main__":
    main()
