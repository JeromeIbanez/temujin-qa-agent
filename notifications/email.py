"""
Email notifications via Gmail SMTP.
Supports: test failure alerts, approval requests, auto-deploy confirmations.

Swap this module later for Slack/Telegram by implementing the same
send_failure(), send_approval_request(), send_auto_deployed() signatures.
"""
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _send(to: str, subject: str, body_html: str):
    gmail_user = os.environ["GMAIL_USER"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Temujin QA Agent <{gmail_user}>"
    msg["To"] = to
    msg.attach(MIMEText(body_html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, to, msg.as_string())


def send_failure(to: str, app_name: str, commit_msg: str, smoke_results: list, diff_summary: str):
    rows = "".join(
        f"<tr><td>{r['name']}</td>"
        f"<td style='color:{'green' if r['status']=='pass' else 'red'}'>"
        f"{'✅' if r['status']=='pass' else '❌'} {r['status'].upper()}</td>"
        f"<td>{r['detail']}</td></tr>"
        for r in smoke_results
    )
    _send(
        to=to,
        subject=f"[{app_name}] ❌ Deploy blocked — tests failed",
        body_html=f"""
        <h2>Deploy blocked for <strong>{app_name}</strong></h2>
        <p><strong>Commit:</strong> {commit_msg}</p>
        <p><strong>Change summary:</strong> {diff_summary}</p>
        <h3>Test Results</h3>
        <table border="1" cellpadding="6" cellspacing="0">
          <tr><th>Test</th><th>Result</th><th>Detail</th></tr>
          {rows}
        </table>
        <p>Fix the issues on staging and push again.</p>
        """,
    )


def send_approval_request(
    to: str,
    app_name: str,
    commit_msg: str,
    diff_summary: str,
    diff_reasoning: str,
    risk_areas: list,
    smoke_results: list,
    pr_url: str,
    staging_url: str,
):
    rows = "".join(
        f"<tr><td>{r['name']}</td>"
        f"<td style='color:{'green' if r['status']=='pass' else 'red'}'>"
        f"{'✅' if r['status']=='pass' else '❌'} {r['status'].upper()}</td></tr>"
        for r in smoke_results
    )
    risks = "".join(f"<li>{r}</li>" for r in risk_areas) if risk_areas else "<li>None identified</li>"
    _send(
        to=to,
        subject=f"[{app_name}] 👀 Review required before deploying to production",
        body_html=f"""
        <h2>Review required for <strong>{app_name}</strong></h2>
        <p><strong>Commit:</strong> {commit_msg}</p>

        <h3>What changed</h3>
        <p>{diff_summary}</p>

        <h3>Why this needs your review</h3>
        <p>{diff_reasoning}</p>

        <h3>Risk areas</h3>
        <ul>{risks}</ul>

        <h3>Test Results</h3>
        <table border="1" cellpadding="6" cellspacing="0">
          <tr><th>Test</th><th>Result</th></tr>
          {rows}
        </table>

        <h3>Actions</h3>
        <p>
          <a href="{staging_url}" style="margin-right:16px">🔍 Check staging</a>
          <a href="{pr_url}" style="background:#2da44e;color:white;padding:8px 16px;text-decoration:none;border-radius:4px">
            ✅ Approve &amp; merge to production
          </a>
        </p>
        <p style="color:grey;font-size:12px">
          To reject: close the pull request at {pr_url}
        </p>
        <hr style="margin-top:24px">
        <p style="color:grey;font-size:12px">
          <strong>Help the QA agent learn:</strong> If this review was unnecessary,
          add the label <code>qa-false-positive</code> to the pull request before merging.
          If a change was auto-deployed and caused an issue, add <code>qa-false-negative</code>.
          These labels are used to improve future classifications.
        </p>
        """,
    )


def send_auto_deployed(to: str, app_name: str, commit_msg: str, diff_summary: str, production_url: str):
    _send(
        to=to,
        subject=f"[{app_name}] ✅ Auto-deployed to production",
        body_html=f"""
        <h2>✅ <strong>{app_name}</strong> deployed to production</h2>
        <p><strong>Commit:</strong> {commit_msg}</p>
        <p><strong>What changed:</strong> {diff_summary}</p>
        <p>All tests passed. Change was classified as low-risk and deployed automatically.</p>
        <p><a href="{production_url}">View production →</a></p>
        """,
    )
