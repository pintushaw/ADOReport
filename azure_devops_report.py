"""
Azure DevOps Bug & Test Report — Option B
------------------------------------------
Runs as a standalone script via:
  - Local cron job
  - GitHub Actions scheduled workflow
  - Any external scheduler (Jenkins, Airflow, etc.)

Setup:
  pip install requests jinja2 python-dotenv

Env vars (set in .env locally, or in GitHub Actions secrets):
  ADO_ORG, ADO_PROJECT, ADO_PAT
  TEAMS_WEBHOOK   (optional)
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_TO  (optional)
"""

import os
import base64
import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from jinja2 import Template

# Load .env if present (for local runs)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed; rely on real env vars

# ── Config ────────────────────────────────────────────────────────────────────
ORG     = os.environ["ADO_ORG"]
PROJECT = os.environ["ADO_PROJECT"]
PAT     = os.environ["ADO_PAT"]

BASE_URL = f"https://dev.azure.com/{ORG}/{PROJECT}/_apis"
HEADERS  = {
    "Authorization": "Basic " + base64.b64encode(f":{PAT}".encode()).decode(),
    "Content-Type":  "application/json",
}

TODAY       = datetime.date.today()
SPRINT_NAME = os.environ.get("SPRINT_NAME", "@CurrentIteration")
DAYS_BACK   = int(os.environ.get("DAYS_BACK", "7"))
OUTPUT_DIR  = os.path.normpath(os.environ.get("OUTPUT_DIR", "."))


# ── REST helpers ──────────────────────────────────────────────────────────────
def ado_get(path, params=None):
    r = requests.get(f"{BASE_URL}/{path}", headers=HEADERS,
                     params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def ado_post(path, body):
    r = requests.post(f"{BASE_URL}/{path}", headers=HEADERS,
                      json=body, timeout=30)
    r.raise_for_status()
    return r.json()


# ── Data fetchers ─────────────────────────────────────────────────────────────
def fetch_bugs():
    """Active bugs in current sprint via WIQL."""
    wiql = {
        "query": f"""
        SELECT [System.Id]
        FROM WorkItems
        WHERE [System.TeamProject]   = '{PROJECT}'
          AND [System.WorkItemType]  = 'Bug'
          AND [System.State]        NOT IN ('Closed', 'Resolved')
          AND [System.IterationPath] = {SPRINT_NAME}
        ORDER BY [Microsoft.VSTS.Common.Priority] ASC,
                 [System.CreatedDate] DESC
        """
    }
    result = ado_post("wit/wiql?api-version=7.1", wiql)
    ids = [str(i["id"]) for i in result.get("workItems", [])]
    if not ids:
        return []

    fields = ",".join([
        "System.Id", "System.Title", "System.State",
        "System.AssignedTo", "Microsoft.VSTS.Common.Priority",
        "System.CreatedDate", "System.AreaPath",
        "Microsoft.VSTS.Common.Severity",
    ])

    bugs = []
    for chunk in [ids[i:i+200] for i in range(0, len(ids), 200)]:
        data = ado_get(
            f"wit/workitems?ids={','.join(chunk)}&fields={fields}&api-version=7.1"
        )
        for wi in data.get("value", []):
            f  = wi["fields"]
            ao = f.get("System.AssignedTo", {})
            bugs.append({
                "id":       wi["id"],
                "title":    f.get("System.Title", ""),
                "state":    f.get("System.State", ""),
                "priority": str(f.get("Microsoft.VSTS.Common.Priority", "-")),
                "severity": f.get("Microsoft.VSTS.Common.Severity", "-"),
                "assigned": ao.get("displayName", "Unassigned") if isinstance(ao, dict) else str(ao),
                "created":  f.get("System.CreatedDate", "")[:10],
                "area":     f.get("System.AreaPath", ""),
                "url":      f"https://dev.azure.com/{ORG}/{PROJECT}/_workitems/edit/{wi['id']}",
            })
    return bugs


def fetch_test_runs():
    """Test runs from the past DAYS_BACK days."""
    since = (TODAY - datetime.timedelta(days=DAYS_BACK)).isoformat()
    data  = ado_get(f"test/runs?minLastUpdatedDate={since}&api-version=7.1")
    runs  = []
    for r in data.get("value", []):
        total   = r.get("totalTests", 0)
        passed  = r.get("passedTests", 0)
        failed  = r.get("failedTests", 0)
        skipped = total - passed - failed
        runs.append({
            "id":        r["id"],
            "name":      r.get("name", ""),
            "state":     r.get("state", ""),
            "passed":    passed,
            "failed":    failed,
            "skipped":   max(skipped, 0),
            "total":     total,
            "pass_pct":  round(passed / total * 100) if total else 0,
            "started":   r.get("startedDate", "")[:10],
            "completed": r.get("completedDate", "")[:10],
            "url":       r.get("webAccessUrl", ""),
        })
    return runs


def fetch_failed_test_details(run_id, max_results=50):
    """Individual failed test cases inside a run."""
    data = ado_get(
        f"test/runs/{run_id}/results"
        f"?outcomes=Failed&$top={max_results}&api-version=7.1"
    )
    return [
        {
            "name":    r.get("testCaseTitle", ""),
            "error":   (r.get("errorMessage") or "").strip()[:400],
            "dur_ms":  r.get("durationInMs", 0),
        }
        for r in data.get("value", [])
    ]


# ── HTML template ─────────────────────────────────────────────────────────────
TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>DevOps Report — {{ date }}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f4f4f5;color:#18181b;padding:32px 16px}
.wrap{max-width:960px;margin:0 auto}
header{margin-bottom:32px}
h1{font-size:20px;font-weight:600;margin-bottom:2px}
.sub{font-size:13px;color:#71717a}
h2{font-size:15px;font-weight:600;margin:32px 0 12px;
   padding-bottom:8px;border-bottom:1px solid #e4e4e7}
/* stat cards */
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px}
.card{background:#fff;border:1px solid #e4e4e7;border-radius:10px;
      padding:14px 18px;min-width:110px}
.card-label{font-size:11px;color:#71717a;margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em}
.card-val{font-size:24px;font-weight:600;line-height:1}
.red{color:#dc2626}.green{color:#16a34a}.amber{color:#d97706}.blue{color:#2563eb}
/* table */
table{width:100%;border-collapse:collapse;background:#fff;
      border:1px solid #e4e4e7;border-radius:10px;overflow:hidden;
      font-size:13px;margin-bottom:24px}
th{background:#fafafa;padding:9px 14px;text-align:left;
   font-weight:600;border-bottom:1px solid #e4e4e7;white-space:nowrap}
td{padding:9px 14px;border-bottom:1px solid #f4f4f5;vertical-align:top}
tr:last-child td{border-bottom:none}
a{color:#2563eb;text-decoration:none}a:hover{text-decoration:underline}
/* badges */
.badge{display:inline-block;padding:2px 7px;border-radius:4px;
       font-size:11px;font-weight:600;white-space:nowrap}
.p1{background:#fee2e2;color:#b91c1c}
.p2{background:#fef3c7;color:#92400e}
.p3{background:#dbeafe;color:#1e40af}
.p4{background:#f3f4f6;color:#4b5563}
/* pass bar */
.bar-wrap{display:flex;align-items:center;gap:8px}
.bar{background:#e4e4e7;border-radius:99px;height:7px;width:100px;overflow:hidden}
.bar-fill{background:#16a34a;height:100%}
.bar-fill.warn{background:#d97706}
.bar-fill.fail{background:#dc2626}
/* failed test list */
.fail-list{background:#fff;border:1px solid #e4e4e7;border-radius:10px;
           overflow:hidden;margin-bottom:24px}
.fail-item{padding:10px 14px;border-bottom:1px solid #f4f4f5}
.fail-item:last-child{border-bottom:none}
.fail-name{font-weight:500;font-size:13px;margin-bottom:2px}
.fail-err{font-size:12px;color:#71717a;font-family:monospace;white-space:pre-wrap;word-break:break-all}
footer{text-align:center;font-size:12px;color:#a1a1aa;margin-top:48px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Bug &amp; Test Report &mdash; {{ project }}</h1>
    <p class="sub">Generated {{ date }} &middot; Sprint: {{ sprint }} &middot; Test window: last {{ days_back }} days</p>
  </header>

  <!-- ── Bugs ── -->
  {% set p1 = bugs | selectattr('priority','eq','1') | list %}
  {% set p2 = bugs | selectattr('priority','eq','2') | list %}
  <h2>Active bugs ({{ bugs | length }})</h2>
  <div class="cards">
    <div class="card"><div class="card-label">Total active</div>
      <div class="card-val">{{ bugs | length }}</div></div>
    <div class="card"><div class="card-label">P1 Critical</div>
      <div class="card-val red">{{ p1 | length }}</div></div>
    <div class="card"><div class="card-label">P2 High</div>
      <div class="card-val amber">{{ p2 | length }}</div></div>
    <div class="card"><div class="card-label">P3 / P4</div>
      <div class="card-val blue">{{ bugs | length - p1 | length - p2 | length }}</div></div>
  </div>

  {% if bugs %}
  <table>
    <thead>
      <tr><th>ID</th><th>Title</th><th>Priority</th><th>Severity</th>
          <th>State</th><th>Assigned to</th><th>Area</th><th>Created</th></tr>
    </thead>
    <tbody>
    {% for b in bugs %}
      <tr>
        <td><a href="{{ b.url }}" target="_blank">#{{ b.id }}</a></td>
        <td>{{ b.title }}</td>
        <td><span class="badge p{{ b.priority | lower | replace('-','4') }}">
            P{{ b.priority }}</span></td>
        <td>{{ b.severity }}</td>
        <td>{{ b.state }}</td>
        <td>{{ b.assigned }}</td>
        <td style="font-size:12px;color:#71717a">{{ b.area }}</td>
        <td style="white-space:nowrap">{{ b.created }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p style="color:#71717a;font-style:italic;margin-bottom:24px">No active bugs in current sprint 🎉</p>
  {% endif %}

  <!-- ── Test runs ── -->
  {% set total_p = runs | sum(attribute='passed') %}
  {% set total_f = runs | sum(attribute='failed') %}
  {% set total_t = runs | sum(attribute='total') %}
  {% set overall_pct = ((total_p / total_t * 100) | round | int) if total_t else 0 %}
  <h2>Test runs &mdash; last {{ days_back }} days ({{ runs | length }} runs)</h2>
  <div class="cards">
    <div class="card"><div class="card-label">Runs</div>
      <div class="card-val">{{ runs | length }}</div></div>
    <div class="card"><div class="card-label">Passed</div>
      <div class="card-val green">{{ total_p }}</div></div>
    <div class="card"><div class="card-label">Failed</div>
      <div class="card-val {% if total_f > 0 %}red{% endif %}">{{ total_f }}</div></div>
    <div class="card"><div class="card-label">Overall pass rate</div>
      <div class="card-val {% if overall_pct >= 90 %}green{% elif overall_pct >= 70 %}amber{% else %}red{% endif %}">
        {{ overall_pct }}%</div></div>
  </div>

  {% if runs %}
  <table>
    <thead>
      <tr><th>Run name</th><th>State</th><th>Pass</th><th>Fail</th><th>Skip</th>
          <th>Pass rate</th><th>Completed</th></tr>
    </thead>
    <tbody>
    {% for r in runs %}
      <tr>
        <td><a href="{{ r.url }}" target="_blank">{{ r.name }}</a></td>
        <td>{{ r.state }}</td>
        <td class="green">{{ r.passed }}</td>
        <td class="{% if r.failed > 0 %}red{% endif %}">{{ r.failed }}</td>
        <td style="color:#71717a">{{ r.skipped }}</td>
        <td>
          <div class="bar-wrap">
            <div class="bar"><div class="bar-fill {% if r.pass_pct < 70 %}fail{% elif r.pass_pct < 90 %}warn{% endif %}"
                 style="width:{{ r.pass_pct }}%"></div></div>
            <span>{{ r.pass_pct }}%</span>
          </div>
        </td>
        <td style="white-space:nowrap">{{ r.completed or r.started }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>

  <!-- failed test detail per run -->
  {% for r in runs %}
    {% if r.failed > 0 and r.details %}
    <h2 style="font-size:14px">Failed tests — {{ r.name }}</h2>
    <div class="fail-list">
      {% for t in r.details %}
      <div class="fail-item">
        <div class="fail-name red">{{ t.name }}</div>
        {% if t.error %}<div class="fail-err">{{ t.error }}</div>{% endif %}
        <div style="font-size:11px;color:#a1a1aa;margin-top:4px">{{ t.dur_ms }} ms</div>
      </div>
      {% endfor %}
    </div>
    {% endif %}
  {% endfor %}

  {% else %}
  <p style="color:#71717a;font-style:italic">No test runs in the last {{ days_back }} days.</p>
  {% endif %}

  <footer>Auto-generated by azure_devops_report.py &middot; {{ date }}</footer>
</div>
</body>
</html>
"""


# ── Output: save HTML ─────────────────────────────────────────────────────────
def save_html(html: str) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"report_{TODAY}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[✓] Report saved → {path}")
    return path


# ── Output: email ─────────────────────────────────────────────────────────────
def send_email(html: str):
    host  = os.environ.get("SMTP_HOST", "smtp.office365.com")
    port  = int(os.environ.get("SMTP_PORT", "587"))
    user  = os.environ["SMTP_USER"]
    pw    = os.environ["SMTP_PASS"]
    to    = [a.strip() for a in os.environ["EMAIL_TO"].split(",")]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"DevOps Report — {TODAY}"
    msg["From"]    = user
    msg["To"]      = ", ".join(to)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(host, port) as s:
        s.ehlo()
        s.starttls()
        s.login(user, pw)
        s.sendmail(user, to, msg.as_string())
    print(f"[✓] Email sent → {to}")


# ── Output: Microsoft Teams webhook ──────────────────────────────────────────
def send_teams(bugs: list, runs: list):
    url = os.environ.get("TEAMS_WEBHOOK", "")
    if not url:
        return

    total_p = sum(r["passed"] for r in runs)
    total_t = sum(r["total"]  for r in runs)
    total_f = sum(r["failed"] for r in runs)
    pct     = round(total_p / total_t * 100) if total_t else 0
    p1      = sum(1 for b in bugs if b["priority"] == "1")

    color = "DC2626" if p1 > 0 or total_f > 0 else "16A34A"

    card = {
        "@type":    "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": color,
        "summary":    f"DevOps Report {TODAY}",
        "sections": [{
            "activityTitle":    f"Bug & Test Report — {PROJECT}",
            "activitySubtitle": str(TODAY),
            "facts": [
                {"name": "Active bugs",    "value": str(len(bugs))},
                {"name": "P1 critical",    "value": str(p1)},
                {"name": "Test runs",      "value": str(len(runs))},
                {"name": "Tests failed",   "value": str(total_f)},
                {"name": "Pass rate",      "value": f"{pct}%"},
            ],
        }],
        "potentialAction": [{
            "@type": "OpenUri",
            "name":  "Open Azure DevOps",
            "targets": [{"os": "default",
                         "uri": f"https://dev.azure.com/{ORG}/{PROJECT}"}],
        }],
    }
    requests.post(url, json=card, timeout=10)
    print("[✓] Teams notification sent.")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"Fetching data for {ORG}/{PROJECT} …")

    bugs = fetch_bugs()
    print(f"  Bugs: {len(bugs)}")

    runs = fetch_test_runs()
    print(f"  Test runs: {len(runs)}")

    # Enrich runs with per-run failed test details
    for r in runs:
        r["details"] = fetch_failed_test_details(r["id"]) if r["failed"] > 0 else []

    html = Template(TEMPLATE).render(
        date=str(TODAY),
        project=PROJECT,
        sprint=SPRINT_NAME,
        days_back=DAYS_BACK,
        bugs=bugs,
        runs=runs,
    )

    save_html(html)

    if os.environ.get("EMAIL_TO") and os.environ.get("SMTP_USER"):
        send_email(html)

    send_teams(bugs, runs)

    print("Done.")


if __name__ == "__main__":
    main()