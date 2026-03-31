"""
Smoke tests — quick sanity checks that the app is alive.
Runs against the staging URL before anything else.
"""
import os
import sys
import requests


def run(staging_url: str) -> dict:
    results = []
    passed = True

    def check(name: str, url: str, expected_status: int = 200, json_key: str = None):
        nonlocal passed
        try:
            r = requests.get(url, timeout=30)
            ok = r.status_code == expected_status
            if ok and json_key:
                ok = json_key in r.json()
            results.append({
                "name": name,
                "status": "pass" if ok else "fail",
                "detail": f"HTTP {r.status_code}",
            })
            if not ok:
                passed = False
        except Exception as e:
            results.append({"name": name, "status": "fail", "detail": str(e)})
            passed = False

    base = staging_url.rstrip("/")
    api_url = os.environ.get("STAGING_API_URL", "").rstrip("/")

    check("Frontend loads", base)
    if api_url:
        check("API health", f"{api_url}/health", json_key="status")

    return {"passed": passed, "results": results}


if __name__ == "__main__":
    staging_url = sys.argv[1] if len(sys.argv) > 1 else os.environ["STAGING_URL"]
    result = run(staging_url)
    import json
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)
