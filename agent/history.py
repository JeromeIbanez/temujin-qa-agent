"""
Self-learning history layer for the QA agent.

Responsibilities:
- Load and save qa-history.json from/to the `history` branch of the calling repo.
- Lazily resolve outcomes of pending COMPLEX PR records on each pipeline run.
- Select few-shot examples ranked by file-path Jaccard similarity.
- Build context notes and dynamic rules for the GPT-4o system prompt.
- Apply a hard Python override when repeated false negatives are detected.

Storage: GitHub Contents API via PyGithub — no git CLI, works in stateless CI.
The `history` branch is auto-initialized on first run; no manual setup needed.
"""
import json
import os
import uuid
from collections import Counter
from datetime import datetime, timezone

from github import Github, GithubException, InputGitTreeElement

HISTORY_BRANCH = "history"
HISTORY_FILE = "qa-history.json"


# ── Storage ──────────────────────────────────────────────────────────────────

def load_history(repo_name: str, gh_token: str) -> list:
    """Fetch qa-history.json from the history branch. Returns [] on first run."""
    repo = Github(gh_token).get_repo(repo_name)
    try:
        contents = repo.get_contents(HISTORY_FILE, ref=HISTORY_BRANCH)
        return json.loads(contents.decoded_content.decode())
    except GithubException as e:
        if e.status == 404:
            return []
        raise


def save_history(repo_name: str, gh_token: str, records: list) -> None:
    """Write records back to the history branch, creating branch/file if needed."""
    repo = Github(gh_token).get_repo(repo_name)
    content = json.dumps(records, indent=2)

    try:
        existing = repo.get_contents(HISTORY_FILE, ref=HISTORY_BRANCH)
        repo.update_file(
            path=HISTORY_FILE,
            message="QA agent: update history",
            content=content,
            sha=existing.sha,
            branch=HISTORY_BRANCH,
        )
    except GithubException as e:
        if e.status == 404:
            _init_history_branch(repo, content)
        else:
            raise


def _init_history_branch(repo, initial_content: str) -> None:
    """Create orphan history branch with initial qa-history.json."""
    try:
        repo.get_branch(HISTORY_BRANCH)
        # Branch exists but file is missing — just create the file
        repo.create_file(
            path=HISTORY_FILE,
            message="QA agent: initialize history",
            content=initial_content,
            branch=HISTORY_BRANCH,
        )
        return
    except GithubException:
        pass

    # Branch doesn't exist — create an orphan branch via the Git Data API
    blob = repo.create_git_blob(initial_content, "utf-8")
    tree = repo.create_git_tree(
        [InputGitTreeElement(HISTORY_FILE, "100644", "blob", sha=blob.sha)]
    )
    commit = repo.create_git_commit(
        message="QA agent: initialize history branch",
        tree=tree,
        parents=[],
    )
    repo.create_git_ref(f"refs/heads/{HISTORY_BRANCH}", commit.sha)


# ── Record construction ───────────────────────────────────────────────────────

def new_record(
    repo: str,
    commit_sha: str,
    commit_msg: str,
    classification: str,
    summary: str,
    reasoning: str,
    risk_areas: list,
    changed_files: list,
    diff_size_chars: int,
    criteria_hash: str,
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "commit_sha": commit_sha,
        "commit_message": commit_msg,
        "classification": classification,
        "summary": summary,
        "reasoning": reasoning,
        "risk_areas": risk_areas,
        "changed_files": changed_files,
        "diff_size_chars": diff_size_chars,
        "criteria_hash": criteria_hash,
        # SIMPLE → immediately resolved; COMPLEX → pending until PR closes
        "outcome": "auto_deployed" if classification == "simple" else "pending",
        "outcome_captured_at": None,
        "pr_number": None,
        "time_to_merge_seconds": None,
        "human_label": None,
    }


# ── Lazy outcome resolution ───────────────────────────────────────────────────

def resolve_pending_outcomes(records: list, gh_token: str) -> tuple:
    """
    Check GitHub API for the status of any pending COMPLEX PRs.
    Returns (updated_records, changed: bool).
    Called at the start of each pipeline run before classification.
    """
    gh = Github(gh_token)
    changed = False

    for record in records:
        if record.get("outcome") != "pending" or not record.get("pr_number"):
            continue

        try:
            repo = gh.get_repo(record["repo"])
            pr = repo.get_pull(record["pr_number"])

            if pr.state == "open":
                continue  # still awaiting review

            # Check for human feedback labels
            human_label = None
            for label in pr.labels:
                if label.name in ("qa-false-positive", "qa-false-negative"):
                    human_label = label.name
                    break

            if pr.merged:
                seconds = int((pr.merged_at - pr.created_at).total_seconds())
                outcome = "merged_fast" if seconds < 3600 else "merged"
                record["time_to_merge_seconds"] = seconds
            else:
                outcome = "closed_unmerged"

            record["outcome"] = outcome
            record["outcome_captured_at"] = datetime.now(timezone.utc).isoformat()
            if human_label:
                record["human_label"] = human_label
            changed = True

        except Exception:
            continue  # don't let a stale record block a new deploy

    return records, changed


# ── Similarity ────────────────────────────────────────────────────────────────

def _jaccard(a: list, b: list) -> float:
    sa, sb = set(a), set(b)
    union = sa | sb
    return len(sa & sb) / len(union) if union else (1.0 if not sa and not sb else 0.0)


# ── Few-shot selection ────────────────────────────────────────────────────────

def select_few_shot_examples(
    records: list, current_files: list, criteria_hash: str, n: int = 6
) -> list:
    """
    Select up to n resolved examples (n/2 SIMPLE, n/2 COMPLEX) ranked by
    file-path Jaccard similarity to the current diff.
    Same-criteria records get a small relevance bonus.
    """
    resolved = [
        r for r in records if r.get("outcome") not in (None, "pending")
    ]
    if not resolved:
        return []

    scored = []
    for r in resolved:
        sim = _jaccard(current_files, r.get("changed_files", []))
        version_bonus = 0.1 if r.get("criteria_hash") == criteria_hash else 0.0
        scored.append((sim + version_bonus, r))

    scored.sort(key=lambda x: x[0], reverse=True)

    per_class = n // 2
    simple_ex = [r for _, r in scored if r["classification"] == "simple"][:per_class]
    complex_ex = [r for _, r in scored if r["classification"] == "complex"][:per_class]

    return simple_ex + complex_ex


def build_few_shot_messages(examples: list, criteria_hash: str) -> list:
    """
    Format selected examples as GPT-4o message pairs.
    Each pair shows: files changed → classification decision → real-world outcome.
    """
    messages = []
    for ex in examples:
        files = ex.get("changed_files", [])
        files_str = ", ".join(files[:10])
        if len(files) > 10:
            files_str += f" (+{len(files) - 10} more)"

        version_note = (
            " [different criteria version — treat as lower confidence]"
            if ex.get("criteria_hash") != criteria_hash
            else ""
        )

        outcome_parts = [ex["outcome"].replace("_", " ")]
        if ex.get("human_label"):
            outcome_parts.append(f"human label: {ex['human_label']}")
        outcome_str = " | ".join(outcome_parts)

        messages.append({
            "role": "user",
            "content": (
                f"Analyze this diff:\n\n"
                f"Files changed: {files_str}\n"
                f"[Past decision{version_note} — outcome: {outcome_str}]"
            ),
        })
        messages.append({
            "role": "assistant",
            "content": json.dumps({
                "classification": ex["classification"],
                "summary": ex["summary"],
                "reasoning": ex["reasoning"],
                "risk_areas": ex.get("risk_areas", []),
            }),
        })

    return messages


# ── System prompt additions ───────────────────────────────────────────────────

def build_context_note(records: list, criteria_hash: str) -> str:
    """
    Generate a stats note injected into the system prompt once ≥10 resolved
    records exist. Gives GPT-4o calibration context for this specific repo.
    """
    resolved = [r for r in records if r.get("outcome") not in (None, "pending")]
    if len(resolved) < 10:
        return ""

    total = len(resolved)
    simple_count = sum(1 for r in resolved if r["classification"] == "simple")
    complex_count = total - simple_count
    false_pos = sum(1 for r in resolved if r.get("human_label") == "qa-false-positive")
    false_neg = sum(1 for r in resolved if r.get("human_label") == "qa-false-negative")

    return (
        f"[Deployment history for this repo: {total} past decisions — "
        f"{simple_count} SIMPLE ({100 * simple_count // total}%), "
        f"{complex_count} COMPLEX ({100 * complex_count // total}%). "
        f"Known over-classifications (false positives): {false_pos}. "
        f"Known risky auto-deploys (false negatives): {false_neg}.]"
    )


def build_dynamic_rules(records: list) -> str:
    """
    Derive repo-specific learned rules from labeled false positives/negatives.
    Surfaces directory-level patterns to GPT-4o as extra guidance.
    """
    false_neg_files: list = []
    false_pos_files: list = []

    for r in records:
        label = r.get("human_label")
        if label == "qa-false-negative":
            false_neg_files.extend(r.get("changed_files", []))
        elif label == "qa-false-positive":
            false_pos_files.extend(r.get("changed_files", []))

    if not false_neg_files and not false_pos_files:
        return ""

    rules = []

    if false_neg_files:
        dirs = Counter(
            os.path.dirname(f) for f in false_neg_files if os.path.dirname(f)
        )
        top = [d for d, count in dirs.most_common(3) if count >= 2]
        if top:
            dirs_str = ", ".join(f"`{d}/`" for d in top)
            rules.append(
                f"Warning: past changes to {dirs_str} were incorrectly auto-deployed "
                f"in this repo. Apply extra caution to those paths."
            )

    if false_pos_files:
        dirs = Counter(
            os.path.dirname(f) for f in false_pos_files if os.path.dirname(f)
        )
        top = [d for d, count in dirs.most_common(3) if count >= 2]
        if top:
            dirs_str = ", ".join(f"`{d}/`" for d in top)
            rules.append(
                f"Note: past changes to {dirs_str} were over-classified as COMPLEX "
                f"in this repo. You can be less cautious about those paths."
            )

    return "\n".join(rules)


# ── Hard override ─────────────────────────────────────────────────────────────

def check_hard_override(
    records: list,
    current_files: list,
    similarity_threshold: float = 0.3,
    min_count: int = 3,
) -> tuple:
    """
    If min_count+ past decisions with high file-path overlap were labeled
    as false negatives, force COMPLEX regardless of GPT-4o's answer.
    Returns (should_override: bool, reason: str).
    """
    matching = [
        r for r in records
        if r.get("human_label") == "qa-false-negative"
        and _jaccard(current_files, r.get("changed_files", [])) >= similarity_threshold
    ]

    if len(matching) >= min_count:
        return True, (
            f"Overridden to COMPLEX: {len(matching)} similar past changes were "
            f"labeled as false negatives by a human reviewer."
        )
    return False, ""
