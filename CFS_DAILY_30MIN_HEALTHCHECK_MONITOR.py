#!/usr/bin/env python3
"""
=================================================================
CFS_DAILY_30MIN_HEALTHCHECK_MONITOR.py  v2.0
-----------------------------------------------------------------
30-Minute Health Check Monitor — JAWS MMNG
(Journal Management & Orchestration System)

Flow:
  1. Bootstrap unixODBC / FreeTDS env (exact firm paths)
  2. Connect to JAWS_SEC_ORCH via FreeTDS + pyodbc
  3. Load & execute monitoring SQL from config/mmng_monitor.sql
  4. Evaluate health:  queue > 0 | pending > 15 min | warehouse > 1000
  5. Post to Teams channel via internal istasgWS + Kerberos auth
  6. Send HTML email (GOOD = green header | BAD = red header + alerts)

All parameters → config/settings.py
DB connection   → exact pattern from pydbc_conn.py (firm standard)
Teams posting   → istasgWS Kerberos method from Image 3
=================================================================
"""

import sys
import os
import re
import logging
import smtplib
import platform
import json
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass, field
from typing import List, Optional

# ------------------------------------------------------------------
# Config path
# ------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "config"))
import settings

# ------------------------------------------------------------------
# LOGGING  — set up early so every step is captured
# ------------------------------------------------------------------
_handlers = [logging.StreamHandler(sys.stdout)]
try:
    _handlers.append(logging.FileHandler(settings.LOG_FILE, encoding="utf-8"))
except Exception:
    pass

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s  [%(levelname)-8s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_handlers,
)
log = logging.getLogger("MMNG-Monitor")


# ==================================================================
# STEP 1 — Bootstrap unixODBC + FreeTDS environment
#           (mirrors the firm-standard pydbc_conn.py pattern exactly)
# ==================================================================
def bootstrap_odbc():
    """
    1. Preload libodbc.so.2  (must happen BEFORE import pyodbc)
    2. Set FREETDSCONF / FREETDS env vars
    3. Set ODBCINSTINI / ODBCINI env vars
    4. Write odbcinst.ini to the db/ folder if it doesn't exist
    """
    import ctypes

    # ── Preload unixODBC shared library ──────────────────────────
    log.debug(f"Loading unixODBC: {settings.UNIXODBC_LIB}")
    try:
        ctypes.cdll.LoadLibrary(settings.UNIXODBC_LIB)
    except OSError as e:
        log.warning(f"Could not preload unixODBC lib ({e}) — continuing anyway")

    # ── FreeTDS config env vars ───────────────────────────────────
    os.environ.setdefault("FREETDSCONF", settings.FREETDS_CONF)
    os.environ.setdefault("FREETDS",     os.environ["FREETDSCONF"])
    log.debug(f"FREETDSCONF = {os.environ['FREETDSCONF']}")

    # ── ODBC ini env vars ─────────────────────────────────────────
    os.environ["ODBCINSTINI"] = settings.ODBCINST_INI_PATH
    os.environ["ODBCINI"]     = settings.ODBCINI_PATH

    # ── Write odbcinst.ini if missing ─────────────────────────────
    _ensure_odbcinst()


def _ensure_odbcinst():
    """Write odbcinst.ini with correct firm driver paths if missing."""
    path = settings.ODBCINST_INI_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.isfile(path):
        content = (
            "[FreeTDS]\n"
            "Description = FreeTDS ODBC Driver\n"
            f"Driver      = {settings.FREETDS_DRIVER_SO}\n"
            f"Setup       = {settings.FREETDS_SETUP_SO}\n"
            "UsageCount  = 1\n"
        )
        with open(path, "w") as f:
            f.write(content)
        log.info(f"Created odbcinst.ini at {path}")
    else:
        log.debug(f"odbcinst.ini exists: {path}")


# Bootstrap BEFORE importing pyodbc
bootstrap_odbc()

try:
    import pyodbc  # noqa: E402
except ImportError:
    log.critical("pyodbc not installed. Run: pip install pyodbc")
    sys.exit(1)


# ==================================================================
# DATA MODELS
# ==================================================================
@dataclass
class MonitorRow:
    item:  str
    dt:    str
    value: str


@dataclass
class HealthState:
    status:          str  = "GOOD"
    color:           str  = "#00875a"
    alerts:          List[str] = field(default_factory=list)
    warnings:        List[str] = field(default_factory=list)
    queues:          dict = field(default_factory=dict)
    pending_times:   dict = field(default_factory=dict)
    warehouse_count: int  = 0


# ==================================================================
# STEP 2 — Database connection
#           Connection string format from Image 1:
#           DRIVER=FreeTDS;SERVERNAME={server};DATABASE={dbname}
# ==================================================================
def get_connection():
    conn_str = settings.DB_CONN_TEMPLATE.format(
        server=settings.DB_SERVER,
        dbname=settings.DB_NAME,
    )
    log.info(f"Connecting → SERVERNAME={settings.DB_SERVER}  DATABASE={settings.DB_NAME}")
    log.debug(f"conn_str: {conn_str}")
    conn = pyodbc.connect(conn_str, timeout=settings.QUERY_TIMEOUT)
    conn.autocommit = True
    return conn


# ==================================================================
# STEP 3 — Load & execute monitoring SQL
# ==================================================================
def load_sql() -> str:
    if not os.path.isfile(settings.SQL_PATH):
        raise FileNotFoundError(f"SQL not found: {settings.SQL_PATH}")
    with open(settings.SQL_PATH, "r", encoding="utf-8") as f:
        return f.read()


def run_monitor_sql(conn) -> List[MonitorRow]:
    """
    Split SQL on semicolons (temp table + multiple UNION queries),
    execute each batch, collect every result set.
    """
    raw_sql = load_sql()
    batches = _split_batches(raw_sql)

    cursor = conn.cursor()
    rows: List[MonitorRow] = []

    for idx, batch in enumerate(batches, 1):
        if not batch.strip():
            continue
        try:
            cursor.execute(batch)
            if cursor.description:
                for r in cursor.fetchall():
                    rows.append(MonitorRow(
                        item  = str(r[0]).strip() if r[0] else "",
                        dt    = str(r[1]).strip() if len(r) > 1 and r[1] else "",
                        value = str(r[2]).strip() if len(r) > 2 and r[2] else "",
                    ))
        except Exception as e:
            log.warning(f"Batch {idx} error: {e}  SQL: {batch[:100]}…")

    cursor.close()
    log.info(f"SQL returned {len(rows)} rows")
    return rows


def _split_batches(sql: str) -> List[str]:
    sql = re.sub(r"--[^\n]*", "", sql)        # strip comments
    return [s.strip() for s in sql.split(";") if s.strip()]


# ==================================================================
# STEP 4 — Health evaluation
# ==================================================================
def evaluate_health(rows: List[MonitorRow]) -> HealthState:
    state = HealthState()
    now   = datetime.now()

    for row in rows:
        item  = row.item.upper()
        value = (row.value or "").strip()
        dt_s  = (row.dt   or "").strip()

        # ── Queue counts ──────────────────────────────────────────
        if any(k in item for k in ["SUBMITTED QUEUE", "APPROVED QUEUE",
                                    "OTHER EVENTS QUEUE"]):
            if dt_s and re.match(r"\d{4}-\d{2}-\d{2}", dt_s):
                continue   # skip date-breakdown sub-rows
            try:
                count = int(value)
            except ValueError:
                count = 0
            label = _short_label(item)
            state.queues[label] = count
            if count > settings.ALERT_QUEUE_THRESHOLD:
                state.alerts.append(
                    f"Queue backlog — {label}: {count} journal(s) pending"
                )

        # ── Pending since ─────────────────────────────────────────
        if "PENDING SINCE" in item and value not in ("", "Nothing New Pending"):
            try:
                pending_dt  = datetime.strptime(value[:16], "%Y-%m-%d %H:%M")
                lag_minutes = (now - pending_dt).total_seconds() / 60
                label       = _short_label(item)
                state.pending_times[label] = pending_dt
                if lag_minutes > settings.ALERT_PENDING_MINUTES:
                    state.alerts.append(
                        f"SLA breach — {label}: pending {int(lag_minutes)} min "
                        f"(since {value[:16]})"
                    )
            except ValueError:
                pass

        # ── Warehouse count ───────────────────────────────────────
        if "13. MMNG WAREHOUSED" in item and not dt_s:
            try:
                wh = int(value)
                state.warehouse_count = wh
                if wh > settings.ALERT_WAREHOUSE_THRESHOLD:
                    state.alerts.append(
                        f"Warehouse backlog — {wh} journals in FEWRH/MSOWRH "
                        f"(threshold: {settings.ALERT_WAREHOUSE_THRESHOLD})"
                    )
            except ValueError:
                pass

        # ── Processing time warnings ──────────────────────────────
        if any(k in item for k in ["90TH", "90 PCTL", "PERCENTILE", "PROCESSING TIME"]):
            try:
                secs = float(value)
                if secs > settings.ALERT_PROC_TIME_THRESHOLD:
                    state.warnings.append(
                        f"Slow processing — {_short_label(item)}: "
                        f"{int(secs)}s (90th pctl, threshold: "
                        f"{settings.ALERT_PROC_TIME_THRESHOLD}s)"
                    )
            except ValueError:
                pass

    # ── Final verdict ─────────────────────────────────────────────
    if state.alerts:
        state.status = "BAD"
        state.color  = "#de350b"

    return state


def _short_label(item: str) -> str:
    lbl = re.sub(r"^\d+[\.\d]*[\.\-\s]+", "", item)
    lbl = re.sub(r"(MMNG_?|QUEUE|PENDING SINCE|:)", "", lbl, flags=re.I)
    return lbl.strip().title()[:38]


# ==================================================================
# TEAMS MESSAGE FORMATTER  (plain-text table for istasgWS)
# ==================================================================
def build_teams_text(rows: List[MonitorRow], state: HealthState) -> str:
    """
    Build a plain-text table message compatible with istasgWS Teams2.py.
    istasgWS accepts plain text (up to TEAMS_MSG_CHUNK_SIZE chars per call).
    """
    now = datetime.now().strftime("%d %b %Y  %H:%M:%S")
    icon   = "🚨" if state.status == "BAD" else "✅"
    status = f"{icon} {state.status}"

    lines = [
        f"{'='*62}",
        f"  JAWS MMNG — 30-Min Health Check   {settings.ENVIRONMENT}",
        f"  Status : {status}",
        f"  Run at : {now}",
        f"{'='*62}",
    ]

    if state.alerts:
        lines.append("")
        lines.append("⚠ ACTIVE ALERTS:")
        for a in state.alerts:
            lines.append(f"  ▶ {a}")

    if state.warnings:
        lines.append("")
        lines.append("⚡ WARNINGS:")
        for w in state.warnings:
            lines.append(f"  ▷ {w}")

    lines.append("")
    lines.append(f"{'─'*62}")
    lines.append(f"{'Reporting Item':<42} {'Date/Time':<12} {'Count/Timing'}")
    lines.append(f"{'─'*62}")

    for row in rows:
        if row.dt == "Date/Time":        # section header rows
            lines.append(f"\n  ── {row.item.replace('--','').strip()} ──")
        else:
            lines.append(
                f"  {row.item:<40} {row.dt:<12} {row.value}"
            )

    lines.append(f"{'─'*62}")
    lines.append(f"  Dashboard: {settings.DASHBOARD_URL}")
    lines.append(f"{'='*62}")

    return "\n".join(lines)


# ==================================================================
# STEP 5 — Teams channel posting
#           Exact mechanism from Image 3:
#           1. Kerberos login via HTTPKerberosAuth → get session cookie
#           2. GET istasgWS/Teams2.py?channel=X&msg=<chunk>
# ==================================================================
def send_teams(rows: List[MonitorRow], state: HealthState) -> bool:
    if not settings.TEAMS_ENABLED:
        log.info("Teams disabled — skipping")
        return True

    # Attempt to import requests + kerberos — both must be available
    try:
        import requests
        from requests_kerberos import HTTPKerberosAuth
    except ImportError as e:
        log.error(
            f"Missing package: {e}. "
            "Install: pip install requests requests-kerberos"
        )
        return False

    full_msg = build_teams_text(rows, state)
    channel  = settings.TEAMS_CHANNEL

    log.info(f"Authenticating with KRB2SM for Teams posting…")
    os.environ["CURL_CA_BUNDLE"] = settings.MS_CA_CERT_BUNDLE

    # Build User-Agent (matches line 404 in Image 3)
    useragent = "Python-requests/%s;%s" % (platform.python_version(),
                                            settings.TEAMS_GRN)

    session = requests.Session()
    session.headers.update({"User-Agent": useragent})
    if os.path.isfile(settings.MS_CA_CERT_BUNDLE):
        session.verify = settings.MS_CA_CERT_BUNDLE
    else:
        session.verify = False
        log.warning(f"CA bundle not found at {settings.MS_CA_CERT_BUNDLE} — TLS verify OFF")

    # ── Step 1: Kerberos auth to get session cookie ───────────────
    try:
        _auth = HTTPKerberosAuth(principal=settings.TEAMS_KRB_PRINCIPAL)
        krb_resp = session.get(
            settings.TEAMS_KRB_LOGIN_URL,
            auth=_auth,
            timeout=settings.TEAMS_HTTP_TIMEOUT,
        )
        if len(krb_resp.cookies) == 0:
            log.error("KRB2SM returned no cookies — authentication failed")
            return False
        log.info(f"KRB2SM auth OK — HTTP {krb_resp.status_code}")
    except Exception as e:
        log.error(f"KRB2SM authentication error: {e}")
        return False

    # ── Step 2: Post message in chunks ────────────────────────────
    chunk_size  = settings.TEAMS_MSG_CHUNK_SIZE
    chunks      = [full_msg[i:i+chunk_size]
                   for i in range(0, len(full_msg), chunk_size)]
    success_all = True

    log.info(f"Posting {len(chunks)} chunk(s) to Teams channel: {channel}")

    for i, chunk in enumerate(chunks, 1):
        try:
            post_url = (
                f"{settings.TEAMS_POST_URL}"
                f"?channel={channel}"
                f"&msg={requests.utils.quote(chunk)}"
            )
            resp = session.get(post_url, timeout=settings.TEAMS_HTTP_TIMEOUT)
            log.info(f"Teams chunk {i}/{len(chunks)} → HTTP {resp.status_code}: "
                     f"{resp.text[:80]}")
            if resp.status_code not in (200, 201):
                log.warning(f"Unexpected status {resp.status_code} for chunk {i}")
                success_all = False
        except Exception as e:
            log.error(f"Teams chunk {i} failed: {e}")
            success_all = False

    return success_all


# ==================================================================
# STEP 6 — HTML Email
# ==================================================================
def build_html_email(rows: List[MonitorRow], state: HealthState) -> str:
    run_time  = datetime.now().strftime("%d %b %Y  %H:%M:%S")
    is_bad    = state.status == "BAD"
    hdr_bg    = "#de350b" if is_bad else "#0052cc"
    icon      = "🚨" if is_bad else "✅"
    status_lbl = "BAD / RED — Immediate Action Required" if is_bad else "GOOD — All Systems Healthy"
    st_color  = "#de350b" if is_bad else "#00875a"
    st_bg     = "#fff0ee" if is_bad else "#e3fcef"
    st_border = "#de350b" if is_bad else "#00875a"

    # Alert block
    alert_html = ""
    if state.alerts:
        items = "".join(
            f'<li style="margin:6px 0;font-size:13px;color:#de350b;">⚠&nbsp; {a}</li>'
            for a in state.alerts
        )
        alert_html = f"""
        <div style="margin:14px 0;padding:14px 18px;background:#fff5f5;
                    border-left:4px solid #de350b;border-radius:6px;">
          <div style="font-weight:800;color:#de350b;margin-bottom:8px;">
            🚨 Active Alerts ({len(state.alerts)})
          </div>
          <ul style="margin:0;padding-left:16px;">{items}</ul>
        </div>"""

    warn_html = ""
    if state.warnings:
        items = "".join(
            f'<li style="margin:5px 0;font-size:13px;color:#974f0c;">⚡&nbsp; {w}</li>'
            for w in state.warnings
        )
        warn_html = f"""
        <div style="margin:10px 0;padding:12px 18px;background:#fffae6;
                    border-left:4px solid #ff8b00;border-radius:6px;">
          <div style="font-weight:700;color:#974f0c;margin-bottom:7px;">
            ⚡ Warnings ({len(state.warnings)})
          </div>
          <ul style="margin:0;padding-left:16px;">{items}</ul>
        </div>"""

    # Queue cards
    cards = ""
    for label, count in state.queues.items():
        cc = "#de350b" if count > 0 else "#00875a"
        cb = "#fff5f5" if count > 0 else "#e3fcef"
        cards += f"""
        <div style="flex:1;min-width:140px;text-align:center;padding:14px 12px;
                    background:{cb};border:1px solid {cc}44;border-radius:8px;">
          <div style="font-size:28px;font-weight:900;color:{cc};">{count}</div>
          <div style="font-size:11px;color:#5e6c84;margin-top:4px;">{label}</div>
        </div>"""
    wh_c = "#de350b" if state.warehouse_count > settings.ALERT_WAREHOUSE_THRESHOLD else "#0052cc"
    wh_b = "#fff5f5" if state.warehouse_count > settings.ALERT_WAREHOUSE_THRESHOLD else "#e6f0ff"
    cards += f"""
    <div style="flex:1;min-width:140px;text-align:center;padding:14px 12px;
                background:{wh_b};border:1px solid {wh_c}44;border-radius:8px;">
      <div style="font-size:28px;font-weight:900;color:{wh_c};">{state.warehouse_count}</div>
      <div style="font-size:11px;color:#5e6c84;margin-top:4px;">Warehoused</div>
    </div>"""

    # Result table rows
    trows = ""
    for row in rows:
        if row.dt == "Date/Time":
            section = row.item.replace("--", "").strip()
            trows += f"""<tr><td colspan="3" style="background:#0052cc;color:#fff;
                font-size:11px;font-weight:800;padding:8px 14px;
                letter-spacing:.8px;text-transform:uppercase;">{section}</td></tr>"""
        else:
            val = row.value or "—"
            vc, vbg = "#172b4d", "transparent"
            if "nothing new pending" in val.lower():
                vc, vbg = "#00875a", "#e3fcef"
            elif re.match(r"^\d+$", val) and int(val) > 0 and "pending" not in row.item.lower():
                vc, vbg = "#de350b", "#fff5f5"
            trows += f"""<tr style="border-bottom:1px solid #f4f5f7;">
              <td style="padding:7px 14px;font-size:12px;color:#172b4d;">{row.item}</td>
              <td style="padding:7px 14px;font-size:12px;color:#5e6c84;white-space:nowrap;">{row.dt}</td>
              <td style="padding:7px 14px;font-size:12px;font-weight:700;
                  color:{vc};background:{vbg};">{val}</td>
            </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>JAWS MMNG Health Check</title></head>
<body style="margin:0;padding:0;background:#f4f5f7;
             font-family:'Segoe UI',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#f4f5f7;padding:28px 0;">
<tr><td align="center">
<table width="700" cellpadding="0" cellspacing="0"
       style="background:#fff;border-radius:12px;overflow:hidden;
              box-shadow:0 4px 24px rgba(0,0,0,.13);">

  <!-- MORGAN STANLEY NAVY STRIP -->
  <tr><td style="background:#001f5b;padding:10px 28px;">
    <table width="100%"><tr>
      <td><span style="color:#fff;font-size:14px;font-weight:900;
          letter-spacing:.12em;text-transform:uppercase;">Morgan Stanley</span>
          <span style="color:#ffffff55;margin:0 10px;">|</span>
          <span style="color:#ffffffbb;font-size:12px;">
          JAWS — Core Processing 360° Dashboard</span></td>
      <td align="right"><span style="color:#ffffff66;font-size:11px;">
          {settings.ENVIRONMENT} · v{settings.SCRIPT_VERSION}</span></td>
    </tr></table>
  </td></tr>

  <!-- COLOUR HEADER -->
  <tr><td style="background:{hdr_bg};padding:22px 28px 20px;">
    <div style="color:#ffffff88;font-size:11px;text-transform:uppercase;
         letter-spacing:2px;margin-bottom:6px;">30-Min Health Check Monitor</div>
    <div style="color:#fff;font-size:24px;font-weight:900;">
      {icon}&nbsp; MMNG System Status</div>
    <div style="color:#ffffff99;font-size:12px;margin-top:7px;">
      🕐 {run_time}&nbsp;&nbsp;·&nbsp;&nbsp;
      DB: {settings.DB_SERVER} / {settings.DB_NAME}</div>
  </td></tr>

  <!-- STATUS BADGE -->
  <tr><td style="padding:20px 28px 10px;">
    <div style="display:inline-block;padding:10px 24px;
         background:{st_bg};border:2px solid {st_border};
         border-radius:30px;font-size:15px;font-weight:900;
         color:{st_color};">{icon}&nbsp; {status_lbl}</div>
  </td></tr>

  <!-- ALERTS + WARNINGS -->
  <tr><td style="padding:0 28px;">{alert_html}{warn_html}</td></tr>

  <!-- QUEUE SUMMARY CARDS -->
  <tr><td style="padding:12px 28px;">
    <div style="display:flex;gap:10px;flex-wrap:wrap;">{cards}</div>
  </td></tr>

  <tr><td style="padding:4px 28px;">
    <hr style="border:none;border-top:1px solid #dfe1e6;margin:8px 0;">
  </td></tr>

  <!-- FULL RESULTS TABLE -->
  <tr><td style="padding:8px 28px 24px;">
    <div style="font-size:12px;font-weight:800;color:#172b4d;
         text-transform:uppercase;letter-spacing:1.2px;margin-bottom:12px;">
      📋 Full Monitoring Report</div>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;border:1px solid #dfe1e6;border-radius:8px;">
      <thead><tr style="background:#f4f5f7;">
        <th style="text-align:left;padding:9px 14px;font-size:11px;color:#5e6c84;
            font-weight:800;text-transform:uppercase;letter-spacing:.8px;
            border-bottom:2px solid #dfe1e6;">Reporting Item</th>
        <th style="text-align:left;padding:9px 14px;font-size:11px;color:#5e6c84;
            font-weight:800;text-transform:uppercase;letter-spacing:.8px;
            border-bottom:2px solid #dfe1e6;white-space:nowrap;">Date / Time</th>
        <th style="text-align:left;padding:9px 14px;font-size:11px;color:#5e6c84;
            font-weight:800;text-transform:uppercase;letter-spacing:.8px;
            border-bottom:2px solid #dfe1e6;">Counts / Timings</th>
      </tr></thead>
      <tbody>{trows}</tbody>
    </table>
  </td></tr>

  <!-- FOOTER -->
  <tr><td style="background:#f4f5f7;padding:14px 28px;border-top:1px solid #dfe1e6;">
    <div style="font-size:11px;color:#97a0af;">
      Automated alert from <strong>CFS_DAILY_30MIN_HEALTHCHECK_MONITOR v{settings.SCRIPT_VERSION}</strong>
      &nbsp;·&nbsp; SQL: {settings.SQL_PATH}
      &nbsp;·&nbsp; <a href="{settings.DASHBOARD_URL}" style="color:#0052cc;">
      Dashboard</a>
      &nbsp;·&nbsp; Do not reply.
    </div>
  </td></tr>

</table></td></tr></table>
</body></html>"""


def send_email(rows: List[MonitorRow], state: HealthState) -> bool:
    if not settings.EMAIL_ENABLED:
        log.info("Email disabled — skipping")
        return True
    if state.status == "GOOD" and not settings.EMAIL_SEND_ON_GOOD:
        log.info("GOOD state + EMAIL_SEND_ON_GOOD=False — skipping email")
        return True

    ts  = datetime.now().strftime("%Y-%m-%d %H:%M")
    env = settings.ENVIRONMENT
    subj = (settings.EMAIL_SUBJECT_BAD if state.status == "BAD"
            else settings.EMAIL_SUBJECT_GOOD).format(env=env, ts=ts)

    html_body = build_html_email(rows, state)
    plain     = (
        f"JAWS MMNG 30-Min Health Check\n"
        f"Status : {state.status}\n"
        f"Run at : {ts}\n"
        f"Alerts : {len(state.alerts)}\n"
        + "\n".join(f"  {a}" for a in state.alerts)
        + f"\n\nDashboard: {settings.DASHBOARD_URL}"
    )

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subj
    msg["From"]    = settings.EMAIL_FROM
    msg["To"]      = ", ".join(settings.EMAIL_TO)
    if settings.EMAIL_CC:
        msg["Cc"]  = ", ".join(settings.EMAIL_CC)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    recipients = settings.EMAIL_TO + settings.EMAIL_CC
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as s:
            s.sendmail(settings.EMAIL_FROM, recipients, msg.as_string())
        log.info(f"Email sent [{state.status}] → {', '.join(settings.EMAIL_TO)}")
        return True
    except Exception as e:
        log.error(f"Email failed: {e}")
        return False


# ==================================================================
# MAIN
# ==================================================================
def main():
    log.info("=" * 65)
    log.info(f"  JAWS MMNG 30-Min Health Check Monitor  v{settings.SCRIPT_VERSION}")
    log.info(f"  Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"  Env     : {settings.ENVIRONMENT}")
    log.info(f"  DB      : {settings.DB_SERVER} / {settings.DB_NAME}")
    log.info(f"  SQL     : {settings.SQL_PATH}")
    log.info("=" * 65)

    # Connect
    try:
        conn = get_connection()
        log.info("DB connection established ✓")
    except Exception as e:
        log.critical(f"DB connection failed: {e}")
        sys.exit(1)

    # Run SQL
    try:
        rows = run_monitor_sql(conn)
    except Exception as e:
        log.critical(f"SQL failed: {e}")
        conn.close()
        sys.exit(1)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not rows:
        log.error("No data returned from monitoring SQL")
        sys.exit(1)

    # Evaluate
    state = evaluate_health(rows)
    log.info(f"Health → {state.status}  |  Alerts: {len(state.alerts)}  "
             f"|  Warnings: {len(state.warnings)}")
    for a in state.alerts:
        log.warning(f"  ALERT: {a}")
    for w in state.warnings:
        log.warning(f"  WARN : {w}")

    # Teams (every run)
    log.info("─── Teams notification ───────────────────────")
    send_teams(rows, state)

    # Email
    log.info("─── Email ────────────────────────────────────")
    send_email(rows, state)

    log.info(f"Run complete — {state.status}")
    log.info("=" * 65)
    sys.exit(0 if state.status == "GOOD" else 1)


if __name__ == "__main__":
    main()
