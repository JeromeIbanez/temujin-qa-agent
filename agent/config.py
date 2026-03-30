"""
Per-repo configuration for the QA agent.

Reads AGENT_CONFIG env var (YAML string from .qa-agent.yml in the calling repo).
Falls back to built-in defaults if absent or unparseable.
Computes a stable SHA-256 hash of the active criteria for history versioning.
"""
import hashlib
import json
import os

import yaml

DEFAULT_SIMPLE_CRITERIA = [
    "UI text, style, or layout changes only",
    "Small bug fixes with no logic changes",
    "Config value updates (non-security)",
    "Dependency minor version bumps",
    "Copy/content changes",
]

DEFAULT_COMPLEX_CRITERIA = [
    "New features or significant functionality changes",
    "Database schema changes (migrations)",
    "API contract changes (new/removed/renamed endpoints or fields)",
    "Authentication or security-related changes",
    "Changes to payment, billing, or user data handling",
    "Large refactors touching many files",
    "Environment variable or secrets changes",
]


def load_config() -> dict:
    """
    Load QA agent config from the AGENT_CONFIG env var (raw YAML string).
    Returns a dict with keys: simple_criteria, complex_criteria, custom_context, criteria_hash.
    """
    raw = os.environ.get("AGENT_CONFIG", "")
    cfg = {}
    if raw.strip():
        try:
            parsed = yaml.safe_load(raw)
            if isinstance(parsed, dict):
                cfg = parsed
        except Exception:
            pass  # fall through to defaults

    simple = cfg.get("simple_criteria", DEFAULT_SIMPLE_CRITERIA)
    complex_ = cfg.get("complex_criteria", DEFAULT_COMPLEX_CRITERIA)
    custom_context = cfg.get("custom_context", "") or ""

    criteria_str = json.dumps(
        {"simple": sorted(simple), "complex": sorted(complex_)}, sort_keys=True
    )
    criteria_hash = hashlib.sha256(criteria_str.encode()).hexdigest()[:16]

    return {
        "simple_criteria": simple,
        "complex_criteria": complex_,
        "custom_context": custom_context,
        "criteria_hash": criteria_hash,
    }


def build_system_prompt(config: dict) -> str:
    """Build the GPT-4o system prompt from the active config."""
    simple_lines = "\n".join(f"- {c}" for c in config["simple_criteria"])
    complex_lines = "\n".join(f"- {c}" for c in config["complex_criteria"])
    custom = (
        f"\n\n{config['custom_context'].strip()}"
        if config.get("custom_context", "").strip()
        else ""
    )

    return f"""You are a senior software engineer doing a pre-deployment code review.
Your job is to analyze a git diff and decide whether it is safe to auto-deploy to production,
or whether it needs a human to review it first.{custom}

Classify as SIMPLE (safe to auto-deploy) if:
{simple_lines}

Classify as COMPLEX (needs human review) if:
{complex_lines}

Respond with valid JSON only:
{{
  "classification": "simple" or "complex",
  "summary": "1-2 sentence plain English summary of what changed",
  "reasoning": "1-2 sentence explanation of why you classified it this way",
  "risk_areas": ["list", "of", "specific", "concerns"]
}}"""
