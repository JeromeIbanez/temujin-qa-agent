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

    # ── Step 5: Simple → auto-merge ──────────────────────────────────────────
    if classification == SIMPLE:
        print("[QA] Change classified as SIMPLE. Auto-merging to production.")
        staging_ref = repo.get_branch(staging_branch)
        repo.merge(production_branch, staging_ref.commit.sha, f"Auto-deploy: {commit_msg}")
        print("[QA] Merged successfully.")
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
                title=f"[QA] Deploy to production: {commit_msg}",
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
                title=f"[QA] Deploy to production: {commit_msg}",
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


if __name__ == "__main__":
    main()
