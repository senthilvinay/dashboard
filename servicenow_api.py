#!/usr/bin/env python3
"""
=================================================================
servicenow_api.py  —  CP360° ServiceNow Intelligence Backend
=================================================================
Endpoints:
  GET  /api/snow/health            → health check
  GET  /api/snow/data              → incidents + problems (live or demo)
  GET  /api/snow/incidents         → incidents with filters
  GET  /api/snow/problems          → problems with filters
  GET  /api/snow/metrics           → KPI summary
  GET  /api/snow/weekly-heatmap    → week × day ticket counts
  POST /api/snow/analyse           → AI root-cause analysis
  POST /api/snow/create-incident   → create incident in ServiceNow

ServiceNow API Docs:
  https://developer.servicenow.com/dev.do#!/reference/api/utah/rest/c_TableAPI

Run:
  pip install flask flask-cors requests
  export SNOW_INSTANCE=yourcompany.service-now.com
  export SNOW_USER=your_user
  export SNOW_PASS=your_password
  python servicenow_api.py
=================================================================
"""

import os, json, logging, math
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from flask      import Flask, request, jsonify
from flask_cors import CORS

try:
    import requests
    from requests.auth import HTTPBasicAuth
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False
    logging.warning("requests not installed — demo mode only. pip install requests")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("SNOW")

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ── ServiceNow Config ─────────────────────────────────────────────────
SNOW_INSTANCE = os.getenv("SNOW_INSTANCE", "")          # e.g. company.service-now.com
SNOW_USER     = os.getenv("SNOW_USER", "")
SNOW_PASS     = os.getenv("SNOW_PASS", "")
SNOW_BASE     = f"https://{SNOW_INSTANCE}/api/now/table"
DEMO_MODE     = not (SNOW_INSTANCE and SNOW_USER and SNOW_PASS)

# Severity map: ServiceNow impact/urgency → S1-S5
SNOW_SEV_MAP = {
    "1": "S1",  # Critical
    "2": "S2",  # High
    "3": "S3",  # Medium
    "4": "S4",  # Low
    "5": "S5",  # Info / Planning
}

# Application filter — map CI names to app names
APP_MAP = {
    "JAWS":    ["jaws", "journal", "aprpos"],
    "PNSRT":   ["pnsrt", "trade processing", "figuration"],
    "MMNG":    ["mmng", "money market", "monitoring"],
    "EUT":     ["eut", "equities"],
    "CONFIRM": ["confirm"],
    "RISK":    ["risk", "var"],
}

# =================================================================
#  SERVICENOW API CLIENT
# =================================================================

def _snow_get(table: str, params: dict) -> list:
    """Call ServiceNow Table API and return list of records."""
    if DEMO_MODE or not REQUESTS_OK:
        return []
    url     = f"{SNOW_BASE}/{table}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    auth    = HTTPBasicAuth(SNOW_USER, SNOW_PASS)
    try:
        resp = requests.get(url, params=params, headers=headers, auth=auth, timeout=30)
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as ex:
        log.error(f"ServiceNow API error: {ex}")
        return []

def _snow_post(table: str, payload: dict) -> dict:
    if DEMO_MODE or not REQUESTS_OK:
        return {}
    url     = f"{SNOW_BASE}/{table}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    auth    = HTTPBasicAuth(SNOW_USER, SNOW_PASS)
    try:
        resp = requests.post(url, json=payload, headers=headers, auth=auth, timeout=30)
        resp.raise_for_status()
        return resp.json().get("result", {})
    except Exception as ex:
        log.error(f"ServiceNow POST error: {ex}")
        return {}

def _parse_sev(rec: dict) -> str:
    """Extract S1-S5 from a ServiceNow record."""
    for field in ("severity", "impact", "urgency", "priority"):
        val = str(rec.get(field, "")).strip().lstrip("0")
        if val in SNOW_SEV_MAP:
            return SNOW_SEV_MAP[val]
    return "S3"

def _parse_app(rec: dict) -> str:
    """Guess application from category/subcategory/CI fields."""
    text = " ".join([
        str(rec.get("category", "")),
        str(rec.get("subcategory", "")),
        str(rec.get("cmdb_ci", {}).get("display_value", "") if isinstance(rec.get("cmdb_ci"), dict) else ""),
        str(rec.get("business_service", {}).get("display_value", "") if isinstance(rec.get("business_service"), dict) else ""),
        str(rec.get("assignment_group", {}).get("display_value", "") if isinstance(rec.get("assignment_group"), dict) else ""),
    ]).lower()
    for app, keywords in APP_MAP.items():
        if any(kw in text for kw in keywords):
            return app
    return "OTHER"

def _parse_mttr(rec: dict) -> int:
    """Return MTTR in minutes."""
    try:
        opened  = datetime.fromisoformat(rec["opened_at"].replace("Z", "+00:00"))
        closed  = rec.get("resolved_at") or rec.get("closed_at")
        if closed:
            closed_dt = datetime.fromisoformat(closed.replace("Z", "+00:00"))
            return max(0, int((closed_dt - opened).total_seconds() / 60))
    except Exception:
        pass
    return 0

def _normalize_incident(rec: dict) -> dict:
    dt = rec.get("opened_at", rec.get("sys_created_on", ""))
    created = datetime.fromisoformat(dt.replace("Z", "+00:00")) if dt else datetime.now()
    now     = datetime.now(timezone.utc)
    age_days = (now - created.replace(tzinfo=timezone.utc)).days
    return {
        "id":       rec.get("number", "INC?"),
        "sys_id":   rec.get("sys_id", ""),
        "type":     "incident",
        "sev":      _parse_sev(rec),
        "app":      _parse_app(rec),
        "title":    rec.get("short_description", "No description"),
        "state":    "open" if str(rec.get("state","")).strip() in ("1","2","3") else "resolved",
        "created":  created.isoformat(),
        "mttr_min": _parse_mttr(rec),
        "assignee": rec.get("assignment_group", {}).get("display_value", "Unassigned") if isinstance(rec.get("assignment_group"), dict) else "Unassigned",
        "category": rec.get("category", ""),
        "week":     age_days // 7,
        "day":      created.weekday(),  # 0=Mon
    }

def _normalize_problem(rec: dict) -> dict:
    return {
        "id":             rec.get("number", "PRB?"),
        "sys_id":         rec.get("sys_id", ""),
        "type":           "problem",
        "sev":            _parse_sev(rec),
        "app":            _parse_app(rec),
        "title":          rec.get("short_description", "No description"),
        "state":          rec.get("problem_state", "open"),
        "created":        rec.get("opened_at", rec.get("sys_created_on", "")),
        "incident_count": int(rec.get("cause_notes", "0") or "0"),
        "root_cause":     rec.get("root_cause", rec.get("work_notes", "Under investigation"))[:120],
        "workaround":     bool(rec.get("workaround")),
        "assignee":       rec.get("assignment_group", {}).get("display_value", "Unassigned") if isinstance(rec.get("assignment_group"), dict) else "Unassigned",
    }

# =================================================================
#  DEMO DATA GENERATOR (matches JS _snowGenDemo exactly)
# =================================================================

def _demo_data(days: int = 30) -> dict:
    import random
    now    = datetime.now()
    titles = [
        "Trade processing pipeline failure",
        "Journal validation timeout",
        "Database connection pool exhausted",
        "Kafka consumer lag exceeded threshold",
        "APRPOS status stuck in processing",
        "Exception handler not responding",
        "MQ message backlog growing",
        "EOD reconciliation failed",
        "API gateway timeout",
        "Pod restart loop detected",
        "Memory leak in validation service",
        "Network latency spike on cluster",
        "Batch job failed to complete",
        "Authentication service degraded",
        "Report generation error",
    ]
    apps  = ["JAWS","PNSRT","MMNG","EUT","CONFIRM","RISK"]
    sevs  = ["S1","S2","S3","S4","S5"]
    weights = [0.05,0.10,0.25,0.35,0.25]

    def pick_sev():
        r, cum = random.random(), 0
        for s, w in zip(sevs, weights):
            cum += w
            if r < cum: return s
        return "S3"

    incidents = []
    for d in range(days):
        dt = now - timedelta(days=d)
        dow = dt.weekday()  # 0=Mon
        base = 1 if dow >= 5 else (8 if dow==0 else 6 if dow==4 else 4)
        count = base + random.randint(0, 3)
        week  = d // 7
        if week == 2 and dow in (0,1,2): count += 5
        if week == 5 and dow == 0:       count += 8
        for _ in range(count):
            sev  = pick_sev()
            mttr = ({"S1":30,"S2":60,"S3":150,"S4":360,"S5":720}[sev]
                    + random.randint(0,120))
            created_dt = dt - timedelta(seconds=random.randint(0,86400))
            incidents.append({
                "id":       f"INC{100000+len(incidents):06d}",
                "type":     "incident",
                "sev":      sev,
                "app":      random.choice(apps),
                "title":    random.choice(titles),
                "state":    "resolved" if random.random() > 0.15 else "open",
                "created":  created_dt.isoformat(),
                "mttr_min": mttr,
                "assignee": random.choice(["SRE Team","Ops Team","Dev Team","DBA Team"]),
                "week":     week,
                "day":      dow,
            })

    prob_titles = [
        "Repeated APRPOS validation failures in JAWS",
        "DB connection pool not auto-recovering",
        "Kafka consumer lag on wm-10168 namespace",
        "Memory leak in PNSRT input processor",
        "EOD batch intermittently failing on Fridays",
        "MQ message ordering issue after restart",
        "MMNG monitoring gap during pod recycle",
        "Trade figuration timeout under high load",
        "EUT reconciliation discrepancy",
        "Authentication token expiry not handled",
    ]
    problems = []
    for pi, title in enumerate(prob_titles):
        problems.append({
            "id":             f"PRB{10000+pi:05d}",
            "type":           "problem",
            "sev":            random.choice(["S1","S2","S3"]),
            "app":            random.choice(apps),
            "title":          title,
            "state":          random.choice(["known_error","in_progress"]),
            "created":        (now - timedelta(days=pi*7+random.randint(0,6))).isoformat(),
            "incident_count": 3 + random.randint(0,11),
            "root_cause":     random.choice([
                "Configuration drift after deployment",
                "Under-provisioned resource limits",
                "Missing retry logic on transient failures",
                "Race condition in concurrent processing",
                "Third-party API rate limiting",
            ]),
            "workaround":     random.random() > 0.5,
            "assignee":       random.choice(["SRE Team","Platform Team","Dev Team"]),
        })

    incidents.sort(key=lambda x: x["created"], reverse=True)
    problems.sort(key=lambda x: x["incident_count"], reverse=True)
    return {"incidents": incidents, "problems": problems,
            "source": "demo", "generated": now.isoformat()}

# =================================================================
#  ROUTES
# =================================================================

@app.route("/api/snow/health")
def health():
    return jsonify({
        "status":      "ok",
        "mode":        "demo" if DEMO_MODE else "live",
        "instance":    SNOW_INSTANCE or "not configured",
        "requests_ok": REQUESTS_OK,
        "ts":          datetime.now().isoformat(),
    })

# ─────────────────────────────────────────────────────────────────────
# GET /api/snow/data?range=30d&sev=all&app=all&type=all
# Main endpoint — returns everything the dashboard needs in one call
# ─────────────────────────────────────────────────────────────────────
@app.route("/api/snow/data")
def snow_data():
    range_  = request.args.get("range",  "30d")
    sev_f   = request.args.get("sev",    "all")
    app_f   = request.args.get("app",    "all")
    type_f  = request.args.get("type",   "all")

    days = {"7d":7,"30d":30,"90d":90,"1y":365}.get(range_, 30)

    if DEMO_MODE:
        data = _demo_data(days)
    else:
        # ── Live ServiceNow query ──────────────────────────────────
        since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        # Incidents
        inc_params = {
            "sysparm_query":  f"opened_at>={since}^ORDERBYDESCopened_at",
            "sysparm_fields": "number,sys_id,short_description,state,severity,impact,urgency,priority,"
                              "opened_at,resolved_at,closed_at,category,subcategory,cmdb_ci,"
                              "assignment_group,business_service",
            "sysparm_limit":  "5000",
        }
        raw_incs = _snow_get("incident", inc_params)
        incidents = [_normalize_incident(r) for r in raw_incs]

        # Problems
        prob_params = {
            "sysparm_query":  f"opened_at>={since}^ORDERBYDESCopened_at",
            "sysparm_fields": "number,sys_id,short_description,problem_state,severity,impact,"
                              "opened_at,root_cause,workaround,cause_notes,assignment_group,"
                              "work_notes,cmdb_ci,category",
            "sysparm_limit":  "1000",
        }
        raw_probs = _snow_get("problem", prob_params)
        problems = [_normalize_problem(r) for r in raw_probs]

        data = {"incidents": incidents, "problems": problems,
                "source": "live", "generated": datetime.now().isoformat()}

    # ── Apply filters ─────────────────────────────────────────────
    incs  = data["incidents"]
    probs = data["problems"]

    if sev_f != "all":
        incs  = [i for i in incs  if i["sev"] == sev_f]
        probs = [p for p in probs if p["sev"] == sev_f]
    if app_f != "all":
        incs  = [i for i in incs  if i["app"] == app_f]
        probs = [p for p in probs if p["app"] == app_f]
    if type_f == "incident":
        probs = []
    elif type_f == "problem":
        incs  = []

    return jsonify({
        "incidents":  incs,
        "problems":   probs,
        "source":     data["source"],
        "generated":  data["generated"],
        "filters":    {"range": range_, "sev": sev_f, "app": app_f, "type": type_f},
    })

# ─────────────────────────────────────────────────────────────────────
# GET /api/snow/metrics?range=30d
# KPI summary — pre-aggregated for dashboard widgets
# ─────────────────────────────────────────────────────────────────────
@app.route("/api/snow/metrics")
def snow_metrics():
    range_   = request.args.get("range", "30d")
    days     = {"7d":7,"30d":30,"90d":90,"1y":365}.get(range_, 30)
    data     = _demo_data(days) if DEMO_MODE else None
    if not data:
        data = {"incidents": [], "problems": []}

    incs  = data["incidents"]
    probs = data["problems"]

    open_  = sum(1 for i in incs if i["state"] == "open")
    s1s2   = sum(1 for i in incs if i["sev"] in ("S1","S2"))
    s4s5   = sum(1 for i in incs if i["sev"] in ("S4","S5"))
    mttr   = (sum(i["mttr_min"] for i in incs) // len(incs)) if incs else 0

    # Week-over-week change
    half   = days // 2
    older  = [i for i in incs if i["week"] >= half // 7]
    newer  = [i for i in incs if i["week"] <  half // 7]
    wow    = round((len(newer)-len(older))/max(len(older),1)*100, 1)

    # Top app
    app_counts = {}
    for i in incs:
        app_counts[i["app"]] = app_counts.get(i["app"],0)+1
    top_app = max(app_counts, key=app_counts.get) if app_counts else "N/A"

    return jsonify({
        "total_incidents":   len(incs),
        "open_incidents":    open_,
        "s1_s2_critical":    s1s2,
        "s4_s5_low":         s4s5,
        "avg_mttr_min":      mttr,
        "total_problems":    len(probs),
        "top_app":           top_app,
        "wow_change_pct":    wow,
        "range":             range_,
    })

# ─────────────────────────────────────────────────────────────────────
# GET /api/snow/weekly-heatmap?range=90d&sev=S4
# Returns week × day grid of ticket counts
# ─────────────────────────────────────────────────────────────────────
@app.route("/api/snow/weekly-heatmap")
def weekly_heatmap():
    range_ = request.args.get("range", "90d")
    sev_f  = request.args.get("sev",   "all")
    days   = {"7d":7,"30d":30,"90d":90,"1y":365}.get(range_, 90)
    data   = _demo_data(days) if DEMO_MODE else {"incidents":[]}

    incs = data["incidents"]
    if sev_f != "all":
        incs = [i for i in incs if i["sev"] == sev_f]

    weeks = math.ceil(days/7)
    grid  = [[0]*7 for _ in range(weeks)]

    for i in incs:
        w = min(i["week"], weeks-1)
        d = i["day"] % 7
        grid[w][d] += 1

    # Find peak week for S4/S5
    week_totals = [sum(row) for row in grid]
    peak_week   = week_totals.index(max(week_totals)) if week_totals else 0
    peak_count  = max(week_totals) if week_totals else 0

    return jsonify({
        "grid":        grid,
        "weeks":       weeks,
        "peak_week":   peak_week,
        "peak_count":  peak_count,
        "day_names":   ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"],
        "total":       sum(week_totals),
    })

# ─────────────────────────────────────────────────────────────────────
# POST /api/snow/analyse
# Body: { incidents, problems }  — returns AI analysis text
# In production wire to Anthropic API or ServiceNow NLP
# ─────────────────────────────────────────────────────────────────────
@app.route("/api/snow/analyse", methods=["POST"])
def analyse():
    b     = request.get_json(force=True) or {}
    incs  = b.get("incidents", [])
    probs = b.get("problems",  [])

    if not incs and not probs:
        return jsonify({"error": "No data to analyse"}), 400

    # Quick statistical analysis (no external AI needed)
    s1s2   = sum(1 for i in incs if i.get("sev") in ("S1","S2"))
    open_  = sum(1 for i in incs if i.get("state") == "open")
    avg_mttr = (sum(i.get("mttr_min",0) for i in incs)//max(len(incs),1))

    app_counts = {}
    for i in incs: app_counts[i.get("app","?")] = app_counts.get(i.get("app","?"),0)+1
    top_app = max(app_counts, key=app_counts.get) if app_counts else "N/A"

    top_prob = probs[0] if probs else {}

    sev_dist = {s:sum(1 for i in incs if i.get("sev")==s) for s in ["S1","S2","S3","S4","S5"]}

    # Pattern: Mon/Fri spike?
    day_counts = [0]*7
    for i in incs: day_counts[i.get("day",0)%7] += 1
    peak_day = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"][
        day_counts.index(max(day_counts))]

    analysis = {
        "root_cause_summary": (
            f"Primary root cause linked to {top_prob.get('root_cause','N/A')} "
            f"(Problem {top_prob.get('id','N/A')}, {top_prob.get('incident_count',0)} incidents)."
        ) if top_prob else "No problem records available for root-cause correlation.",
        "pattern_insights": [
            f"Peak incident day: {peak_day} ({max(day_counts)} tickets) — "
            "suggests correlation with deployment or business activity cycle.",
            f"{top_app} accounts for {app_counts.get(top_app,0)}/{len(incs)} incidents "
            f"({round(app_counts.get(top_app,0)/max(len(incs),1)*100)}%).",
            f"Average MTTR: {avg_mttr}min — {'within' if avg_mttr<240 else 'exceeding'} typical SLA.",
            f"S1/S2 critical: {s1s2} ({round(s1s2/max(len(incs),1)*100)}%) — "
            f"{'critical attention needed' if s1s2>10 else 'manageable level'}.",
        ],
        "recommendations": [
            f"Implement auto-remediation for recurring {top_app} S3+ incidents.",
            "Add pre-deployment health checks to reduce Monday spike.",
            "Set up proactive alerting when S4/S5 volume exceeds weekly baseline by 30%.",
            f"Assign dedicated SRE resource to Problem {top_prob.get('id','N/A')} — "
            "highest incident linkage." if top_prob else "Create problem records for recurring incidents.",
        ],
        "risk_level": "HIGH" if s1s2 > 10 or open_ > 20 else "MEDIUM" if s1s2 > 3 else "LOW",
        "sev_distribution": sev_dist,
        "stats": {
            "total": len(incs), "open": open_,
            "s1s2": s1s2, "avg_mttr_min": avg_mttr,
        },
    }
    return jsonify(analysis)

# ─────────────────────────────────────────────────────────────────────
# POST /api/snow/create-incident
# Body: { title, severity, app, description, assignee? }
# ─────────────────────────────────────────────────────────────────────
@app.route("/api/snow/create-incident", methods=["POST"])
def create_incident():
    b = request.get_json(force=True) or {}
    title    = b.get("title", "").strip()
    severity = b.get("severity", "S3")
    app_name = b.get("app", "")
    desc     = b.get("description", "")

    if not title:
        return jsonify({"error": "title required"}), 400

    sev_rev  = {v:k for k,v in SNOW_SEV_MAP.items()}
    sev_num  = sev_rev.get(severity, "3")

    payload = {
        "short_description": title,
        "description":       desc,
        "severity":          sev_num,
        "impact":            sev_num,
        "urgency":           sev_num,
        "category":          "Software",
        "subcategory":       app_name,
        "caller_id":         SNOW_USER,
    }

    if DEMO_MODE:
        # Return a fake response
        return jsonify({
            "number":  f"INC{100000+hash(title)%99999:06d}",
            "sys_id":  "DEMO_SYS_ID",
            "state":   "New",
            "created": datetime.now().isoformat(),
            "mode":    "demo",
            "message": "Demo mode — no real ticket created",
        })

    result = _snow_post("incident", payload)
    if not result:
        return jsonify({"error": "Failed to create incident in ServiceNow"}), 500

    return jsonify({
        "number":  result.get("number"),
        "sys_id":  result.get("sys_id"),
        "state":   result.get("state", {}).get("display_value","New"),
        "created": result.get("opened_at"),
    }), 201

# =================================================================
#  MAIN
# =================================================================
if __name__ == "__main__":
    port = int(os.getenv("SNOW_PORT", "5003"))
    log.info("="*55)
    log.info("  CP360° ServiceNow Intelligence API")
    log.info(f"  Port:     {port}")
    log.info(f"  Mode:     {'⚠ DEMO (set SNOW_INSTANCE/USER/PASS for live)' if DEMO_MODE else '✅ LIVE — '+SNOW_INSTANCE}")
    log.info("="*55)
    if DEMO_MODE:
        log.info("  To go live, set environment variables:")
        log.info("    export SNOW_INSTANCE=yourcompany.service-now.com")
        log.info("    export SNOW_USER=your_api_user")
        log.info("    export SNOW_PASS=your_password")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
