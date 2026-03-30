"""
AI-powered diff analysis.
Reads the git diff and classifies the change as simple or complex,
with a human-readable summary and reasoning.
"""
import os
from openai import OpenAI

SIMPLE = "simple"
COMPLEX = "complex"

SYSTEM_PROMPT = """You are a senior software engineer doing a pre-deployment code review.
Your job is to analyze a git diff and decide whether it is safe to auto-deploy to production,
or whether it needs a human to review it first.

Classify as SIMPLE (safe to auto-deploy) if:
- UI text, style, or layout changes only
- Small bug fixes with no logic changes
- Config value updates (non-security)
- Dependency minor version bumps
- Copy/content changes

Classify as COMPLEX (needs human review) if:
- New features or significant functionality changes
- Database schema changes (migrations)
- API contract changes (new/removed/renamed endpoints or fields)
- Authentication or security-related changes
- Changes to payment, billing, or user data handling
- Large refactors touching many files
- Environment variable or secrets changes

Respond with valid JSON only:
{
  "classification": "simple" or "complex",
  "summary": "1-2 sentence plain English summary of what changed",
  "reasoning": "1-2 sentence explanation of why you classified it this way",
  "risk_areas": ["list", "of", "specific", "concerns"] // empty list if simple
}"""


def analyze_diff(diff: str) -> dict:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    if not diff.strip():
        return {
            "classification": SIMPLE,
            "summary": "No code changes detected.",
            "reasoning": "Empty diff — nothing to review.",
            "risk_areas": [],
        }

    # Trim very large diffs to stay within token limits
    if len(diff) > 12000:
        diff = diff[:12000] + "\n\n[diff truncated for length]"

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Analyze this diff:\n\n{diff}"},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    import json
    return json.loads(response.choices[0].message.content)
