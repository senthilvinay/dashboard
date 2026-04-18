#!/usr/bin/env python3
"""
=================================================================
mks_restart_api.py
-----------------------------------------------------------------
Flask backend for PNSRT MKS Pod Restart Dashboard

Endpoints:
  GET  /api/mks/restart/config              → config JSON
  POST /api/mks/restart/execute             → start restart job
  GET  /api/mks/restart/stream/<job_id>     → SSE live log stream
  GET  /api/mks/restart/status/<job_id>     → poll job + pod results
  POST /api/mks/restart/cancel/<job_id>     → cancel running job
  GET  /api/mks/restart/jobs                → last 20 jobs history
  GET  /api/mks/restart/health              → health check

Restart flow (matches pnsrt_mks_restart.py exactly):
  Phase 1: login → restart deployments → logout  (per cluster)
  Wait:    wait_time seconds (from config)
  Phase 2: login → get_pods → write_csv → logout (per cluster)
  Email:   send HTML report + CSV attachment
=================================================================
"""

import os
import sys
import json
import time
import csv
import uuid
import queue
import logging
import smtplib
import threading
from datetime import datetime
from email.mime.multipart  import MIMEMultipart
from email.mime.text       import MIMEText
from email.mime.base       import MIMEBase
from email.utils           import formataddr
from email.header          import Header
from email                 import encoders

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS

try:
    import paramiko
except ImportError:
    raise ImportError("pip install paramiko")

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# =================================================================
#  LOGGING
# =================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("MKS-Restart")

# =================================================================
#  APP
# =================================================================
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

CONFIG_PATH = os.path.join(os.path.dirname(__file__),
                            "pnsrt_mks_restart_config.json")

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)

CFG = load_config()

SMTP_HOST  = os.environ.get("SMTP_HOST", "msa-hub.ms.com")
SMTP_PORT  = int(os.environ.get("SMTP_PORT", "25"))
CSV_DIR    = os.environ.get("CSV_DIR", "/var/tmp")

# kubectl / unimatrix paths (firm standard)
KUBECTL    = os.environ.get("KUBECTL_BIN",
             "/ms/dist/cloud/PROJ/kubectl/prod/bin/kubectl")
UNIMATRIX  = os.environ.get("UNIMATRIX_BIN",
             "/ms/dist/cloud/PROJ/unimatrix/prod/bin/unimatrix")

# =================================================================
#  JOB STORE
# =================================================================
_jobs:       dict = {}
_job_queues: dict = {}


class RestartJobState:
    def __init__(self, job_id, clusters, namespace, restart_required,
                 wait_time, exclude, username):
        self.job_id           = job_id
        self.clusters         = clusters
        self.namespace        = namespace
        self.restart_required = restart_required
        self.wait_time        = wait_time
        self.exclude          = exclude
        self.username         = username
        self.status           = "PENDING"
        self.phase            = "INIT"
        self.started_at       = datetime.now().isoformat()
        self.finished_at      = None
        self.status_flag      = None
        self.cluster_results  = {}
        self.csv_file         = None
        self.error            = None

    def to_dict(self):
        return {
            "job_id":          self.job_id,
            "status":          self.status,
            "phase":           self.phase,
            "status_flag":     self.status_flag,
            "clusters":        self.clusters,
            "namespace":       self.namespace,
            "restart_required":self.restart_required,
            "wait_time":       self.wait_time,
            "username":        self.username,
            "started_at":      self.started_at,
            "finished_at":     self.finished_at,
            "cluster_results": self.cluster_results,
            "csv_file":        self.csv_file,
            "error":           self.error,
        }


# =================================================================
#  HELPERS
# =================================================================
def _emit(q: queue.Queue, msg: str, level: str = "info"):
    entry = json.dumps({
        "msg":   msg,
        "level": level,
        "ts":    datetime.now().strftime("%H:%M:%S"),
    })
    q.put(entry)
    log.info(f"[RESTART] {msg}")


def to_est(utc_time_str: str) -> str:
    if not utc_time_str or utc_time_str in ("-", "N/A"):
        return "-"
    try:
        utc_dt = datetime.fromisoformat(utc_time_str.replace("Z", "+00:00"))
        return utc_dt.astimezone(ZoneInfo("America/New_York")).strftime(
            "%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return utc_time_str


def _run_ssh(ssh: paramiko.SSHClient, cmd: str,
             timeout: int = 60) -> tuple:
    """Run command on SSH client, return (stdout, stderr, returncode)."""
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    rc  = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return out, err, rc


def _ssh_connect(username: str, password: str,
                 server: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=server, username=username, password=password,
        timeout=30, allow_agent=False, look_for_keys=False,
    )
    return client


def _source_env(ssh, q):
    """Source MS environment on remote server."""
    _emit(q, "── Sourcing MS environment ──", "head")
    for cmd in [
        "source /etc/profile",
        ". /ms/dist/environ/PROJ/core/bash-prod/common/etc/init.environ",
    ]:
        _emit(q, f"$ {cmd}", "cmd")
        _run_ssh(ssh, cmd, timeout=20)

    _emit(q, "── Loading modules ──", "head")
    for mod in ["cloud/helm", "cloud/kubectl",
                "cloud/openshift-client/4.6", "cloud/unimatrix/prod"]:
        _emit(q, f"$ module load {mod}", "cmd")
        out, err, rc = _run_ssh(ssh, f"module load {mod}", timeout=20)
        if rc == 0:
            _emit(q, f"✓ Loaded {mod}", "ok")
        else:
            _emit(q, f"⚠ Warning: {mod} — {err[:80]}", "warn")


# =================================================================
#  SSH WORKER
# =================================================================
def restart_worker(job_id: str, job: RestartJobState,
                   password: str, server: str, q: queue.Queue):
    """
    Background thread replicating pnsrt_mks_restart.py flow:
      Phase 1: login + restart deployments
      Wait:    wait_time seconds
      Phase 2: login + capture pods + write CSV
      Email:   send report
    """
    job.status  = "RUNNING"
    csv_file    = f"pods_status_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    job.csv_file = csv_file
    all_results  = {}

    try:
        _emit(q, f"Connecting to {server}...", "info")
        _emit(q, f"$ ssh {job.username}@{server}", "cmd")

        ssh = _ssh_connect(job.username, password, server)
        _emit(q, f"✓ Connected — {job.username}@{server}", "ok")
        _source_env(ssh, q)

        # ── PHASE 1 — Restart deployments ─────────────────────────
        _emit(q, "", "info")
        _emit(q, "═"*52, "head")
        _emit(q, "  PHASE 1 — Restarting deployments", "head")
        _emit(q, "═"*52, "head")
        job.phase = "PHASE_1"

        for ci, cluster in enumerate(job.clusters, 1):
            if job.status == "CANCELLED":
                break

            short = cluster.split(".")[0].upper()
            _emit(q, f"\n─── Cluster {ci}/{len(job.clusters)}: {cluster} ───", "head")
            job.cluster_results[cluster] = {
                "phase1": "pending", "phase2": "pending", "pods": []}

            # Login
            _emit(q, f"$ {UNIMATRIX} login {cluster}", "cmd")
            out, err, rc = _run_ssh(ssh, f"{UNIMATRIX} login {cluster}", timeout=60)

            if "MKS login completed" in out or "already logged-in" in out:
                _emit(q, f"✓ Logged in to {cluster}", "ok")
            else:
                _emit(q, f"✗ Login failed: {cluster} — skipping", "err")
                job.cluster_results[cluster]["phase1"] = "login_failed"
                continue

            # Set env
            _run_ssh(ssh, f"export CLUSTER={cluster}", 10)
            _run_ssh(ssh, f"export NAMESPACE={job.namespace}", 10)
            _run_ssh(ssh, f"export KUBECONFIG=/var/tmp/{job.username}/.kube/config", 10)

            if job.restart_required:
                # Get deployments
                _emit(q, f"$ {KUBECTL} get deployments -n {job.namespace}", "cmd")
                dep_out, _, _ = _run_ssh(
                    ssh,
                    f"{KUBECTL} get deployments -o json -n {job.namespace}",
                    timeout=30)

                try:
                    dep_data = json.loads(dep_out)
                    deployments = [
                        d["metadata"]["name"]
                        for d in dep_data.get("items", [])
                    ]
                    _emit(q, f"Found {len(deployments)} deployments", "info")
                except Exception:
                    deployments = []
                    _emit(q, "⚠ Could not parse deployments", "warn")

                restarted = 0
                for dep in deployments:
                    if job.status == "CANCELLED":
                        break
                    if dep in job.exclude:
                        _emit(q, f"   ⏭ Skipped (excluded): {dep}", "info")
                        continue
                    _emit(q, f"$ {KUBECTL} rollout restart deployment {dep} -n {job.namespace}", "cmd")
                    out, err, rc = _run_ssh(
                        ssh,
                        f"{KUBECTL} rollout restart deployment {dep} -n {job.namespace}",
                        timeout=300)
                    if rc == 0:
                        _emit(q, f"   ♻️  Restarted: {dep}", "ok")
                        restarted += 1
                    else:
                        _emit(q, f"   ✗ Failed: {dep} — {err[:80]}", "err")

                _emit(q, f"Phase 1 done: {restarted} restarted on {short}", "ok")
                job.cluster_results[cluster]["phase1"] = f"{restarted}_restarted"
            else:
                _emit(q, "restart_required=false — skipping restarts", "info")
                job.cluster_results[cluster]["phase1"] = "skipped"

            # Logout
            _run_ssh(ssh, f"{UNIMATRIX} logout {cluster}", timeout=30)
            _emit(q, f"✓ Logged out from {cluster}", "ok")

        # ── WAIT ──────────────────────────────────────────────────
        if job.status == "CANCELLED":
            raise Exception("Cancelled by user")

        if job.restart_required:
            wait = job.wait_time
            _emit(q, "", "info")
            _emit(q, f"⏳ Waiting {wait}s ({wait//60} min) for pods to stabilise...", "warn")
            _emit(q, "   Restart process underway — please wait before checking pod status.", "info")

            # Emit countdown every 30s so frontend stays alive
            elapsed = 0
            while elapsed < wait:
                if job.status == "CANCELLED":
                    break
                chunk = min(30, wait - elapsed)
                time.sleep(chunk)
                elapsed += chunk
                remaining = wait - elapsed
                if remaining > 0:
                    _emit(q, f"   ⏳ Waiting... {remaining}s remaining", "info")
        else:
            time.sleep(10)

        if job.status == "CANCELLED":
            raise Exception("Cancelled by user")

        # ── PHASE 2 — Capture pod status ──────────────────────────
        _emit(q, "", "info")
        _emit(q, "═"*52, "head")
        _emit(q, "  PHASE 2 — Capturing POD status", "head")
        _emit(q, "═"*52, "head")
        job.phase = "PHASE_2"

        # Write CSV header
        csv_path = os.path.join(CSV_DIR, csv_file)
        with open(csv_path, "w", newline="") as f:
            csv.writer(f).writerow(
                ["Cluster", "PodName", "Status", "RestartTime(EST)"])

        for ci, cluster in enumerate(job.clusters, 1):
            if job.status == "CANCELLED":
                break

            short = cluster.split(".")[0].upper()
            _emit(q, f"\n─── Capturing pods from {cluster} ───", "head")

            # Login
            _emit(q, f"$ {UNIMATRIX} login {cluster}", "cmd")
            out, err, rc = _run_ssh(ssh, f"{UNIMATRIX} login {cluster}", 60)

            if "MKS login completed" not in out and "already logged-in" not in out:
                _emit(q, f"✗ Login failed — skipping pod capture: {cluster}", "err")
                all_results[cluster] = []
                job.cluster_results[cluster]["phase2"] = "login_failed"
                continue

            _emit(q, f"✓ Logged in to {cluster}", "ok")
            _run_ssh(ssh, f"export CLUSTER={cluster}", 10)
            _run_ssh(ssh, f"export NAMESPACE={job.namespace}", 10)
            _run_ssh(ssh, f"export KUBECONFIG=/var/tmp/{job.username}/.kube/config", 10)

            # Get pods
            _emit(q, f"$ {KUBECTL} get pods -n {job.namespace}", "cmd")
            pod_out, _, _ = _run_ssh(
                ssh,
                f"{KUBECTL} get pods -o json -n {job.namespace}",
                timeout=30)

            pods = []
            try:
                pod_data = json.loads(pod_out)
                for item in pod_data.get("items", []):
                    pod_name   = item["metadata"]["name"]
                    status     = item["status"].get("phase", "UNKNOWN")
                    start_time = item["status"].get("startTime", "N/A")
                    pods.append({
                        "pod":         pod_name,
                        "status":      status,
                        "reboot_time": to_est(start_time),
                    })
            except Exception as e:
                _emit(q, f"⚠ Could not parse pods: {e}", "warn")

            all_results[cluster] = pods
            job.cluster_results[cluster]["pods"] = pods

            running = sum(1 for p in pods if p["status"] == "Running")
            _emit(q, f"✓ {len(pods)} pods — {running} Running, "
                  f"{len(pods)-running} Other on {short}", "ok")

            # Colour-code pods in terminal
            for p in pods:
                lvl = "ok" if p["status"] == "Running" else "err"
                _emit(q, f"   {'✅' if lvl=='ok' else '🔴'} {p['pod']} "
                      f"— {p['status']} | {p['reboot_time']}", lvl)

            # Write CSV
            with open(csv_path, "a", newline="") as f:
                w = csv.writer(f)
                for p in pods:
                    w.writerow([cluster, p["pod"], p["status"], p["reboot_time"]])

            job.cluster_results[cluster]["phase2"] = "ok"

            # Logout
            _run_ssh(ssh, f"{UNIMATRIX} logout {cluster}", 30)
            _emit(q, f"✓ Logged out from {cluster}", "ok")

        ssh.close()

        # ── Status flag ────────────────────────────────────────────
        all_pods = [p for pods in all_results.values() for p in pods]
        status_flag = (
            "RED" if any(p["status"] != "Running" for p in all_pods)
            else "GREEN"
        )
        job.status_flag = status_flag

        _emit(q, "", "info")
        _emit(q, "═"*52, "head")
        _emit(q, f"  ✅ COMPLETE — Overall Status: {status_flag}", "ok")
        _emit(q, f"  CSV: {csv_path}", "ok")
        _emit(q, "═"*52, "head")

        # ── Email ──────────────────────────────────────────────────
        _emit(q, "📧 Sending email report...", "info")
        try:
            _send_email(all_results, csv_path, status_flag)
            _emit(q, f"✓ Email sent → {CFG['email']['receiver'][:50]}...", "ok")
        except Exception as e:
            _emit(q, f"⚠ Email failed: {e}", "warn")

        job.status      = "DONE"
        job.finished_at = datetime.now().isoformat()

    except paramiko.AuthenticationException:
        msg = "SSH authentication failed — check username/password"
        _emit(q, f"✗ {msg}", "err")
        job.status = "FAILED"
        job.error  = msg
        job.finished_at = datetime.now().isoformat()

    except Exception as e:
        msg = str(e)
        if msg != "Cancelled by user":
            _emit(q, f"✗ Error: {msg}", "err")
            log.exception("Restart worker error")
        job.status = "CANCELLED" if "Cancelled" in msg else "FAILED"
        job.error  = msg
        job.finished_at = datetime.now().isoformat()

    finally:
        q.put(None)


# =================================================================
#  EMAIL
# =================================================================
def _send_email(all_results: dict, csv_path: str, status_flag: str):
    flag_color = "#de350b" if status_flag == "RED" else "#00875a"
    flag_icon  = "🔴" if status_flag == "RED" else "✅"
    report_time = to_est(datetime.utcnow().isoformat() + "Z")

    # Cluster summary
    sum_rows = ""
    for cluster, pods in all_results.items():
        total   = len(pods)
        running = sum(1 for p in pods if p["status"] == "Running")
        others  = total - running
        row_bg  = "#e8f5e9" if others == 0 else "#ffebee"
        sum_rows += (
            f"<tr style='background:{row_bg}'>"
            f"<td style='padding:7px 12px;font-family:monospace'>{cluster}</td>"
            f"<td style='padding:7px 12px;text-align:center'>{total}</td>"
            f"<td style='padding:7px 12px;text-align:center;color:green;font-weight:700'>{running}</td>"
            f"<td style='padding:7px 12px;text-align:center;color:{'red' if others else 'green'};font-weight:700'>{others}</td>"
            f"</tr>"
        )

    # Pod rows
    pod_rows = ""
    for cluster, pods in all_results.items():
        for p in pods:
            ok     = p["status"] == "Running"
            color  = "#00875a" if ok else "#de350b"
            icon   = "✅" if ok else "🔴"
            pod_rows += (
                f"<tr style='border-bottom:1px solid #f0f0f0'>"
                f"<td style='padding:6px 12px;font-family:monospace;font-size:12px'>{cluster}</td>"
                f"<td style='padding:6px 12px;font-family:monospace;font-size:12px'>{p['pod']}</td>"
                f"<td style='padding:6px 12px;color:{color};font-weight:700'>{icon} {p['status']}</td>"
                f"<td style='padding:6px 12px;font-family:monospace;font-size:12px'>{p['reboot_time']}</td>"
                f"</tr>"
            )

    html = f"""<!DOCTYPE html><html><body style="font-family:Arial;background:#f4f5f7;padding:20px">
<table width="680" cellpadding="0" cellspacing="0"
       style="background:#fff;border-radius:10px;overflow:hidden;
              box-shadow:0 2px 12px rgba(0,0,0,.12);margin:0 auto">
  <tr><td style="background:#001F5B;padding:14px 24px">
    <span style="color:#fff;font-size:15px;font-weight:700">Morgan Stanley</span>
    <span style="color:#ffffff44;margin:0 8px">|</span>
    <span style="color:#ffffffbb;font-size:13px">PNSRT MKS Pod Restart Report</span>
  </td></tr>
  <tr><td style="background:{flag_color};padding:14px 24px">
    <div style="color:#fff;font-size:18px;font-weight:700">
      {flag_icon} Overall Status: {status_flag}</div>
    <div style="color:rgba(255,255,255,.8);font-size:12px;margin-top:4px">
      Generated: {report_time}</div>
  </td></tr>
  <tr><td style="padding:20px 24px">
    <p>Hi All,</p>
    <p>Please find the PODs restart status with respect to each cluster.
    All timestamps are in <strong>EST timezone</strong>.</p>
    <h3 style="color:#001F5B">Cluster-wise Summary</h3>
    <table border="1" cellspacing="0" cellpadding="5"
           style="border-collapse:collapse;width:100%;margin-bottom:16px">
      <tr style="background:#001F5B;color:white">
        <th style="padding:9px 14px">Cluster</th>
        <th style="padding:9px 14px">Total</th>
        <th style="padding:9px 14px">Running</th>
        <th style="padding:9px 14px">Others</th>
      </tr>{sum_rows}
    </table>
    <h3 style="color:#001F5B">Detailed POD Status</h3>
    <table border="1" cellspacing="0" cellpadding="5"
           style="border-collapse:collapse;width:100%">
      <tr style="background:#f4f5f7">
        <th style="padding:8px 12px">Cluster</th>
        <th style="padding:8px 12px">Pod Name</th>
        <th style="padding:8px 12px">Status</th>
        <th style="padding:8px 12px">Restart Time (EST)</th>
      </tr>{pod_rows}
    </table>
    <p style="margin-top:20px">Regards,<br><strong>PNSRT ASG</strong></p>
  </td></tr>
  <tr><td style="background:#f4f5f7;padding:12px 24px">
    <div style="font-size:11px;color:#97a0af">
      Automated report — do not reply</div>
  </td></tr>
</table></body></html>"""

    msg            = MIMEMultipart()
    msg["From"]    = formataddr(
        (str(Header("PNSRT Cluster Monitor", "utf-8")), CFG["email"]["sender"]))
    msg["To"]      = CFG["email"]["receiver"]
    msg["Subject"] = (
        f"{status_flag} {CFG['email']['subject']} — {report_time}")
    msg.attach(MIMEText(html, "html"))

    if os.path.isfile(csv_path):
        with open(csv_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                f"attachment; filename={os.path.basename(csv_path)}")
            msg.attach(part)

    receivers = CFG["email"]["receiver"].split(",")
    smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
    smtp.sendmail(CFG["email"]["sender"], receivers, msg.as_string())
    smtp.close()


# =================================================================
#  ROUTES
# =================================================================

@app.route("/api/mks/restart/config", methods=["GET"])
def get_config():
    return jsonify({
        "clusters":                 CFG.get("clusters", []),
        "exclude_services_restart": CFG.get("exclude_services_restart", []),
        "namespace":                CFG.get("deployment_id", "wm-10168"),
        "restart_required":         CFG.get("restart_required", False),
        "wait_time":                CFG.get("wait_time", 600),
        "jump_server":              CFG.get("jump_server", ""),
        "email_subject":            CFG.get("email", {}).get("subject", ""),
    })


@app.route("/api/mks/restart/execute", methods=["POST"])
def execute_restart():
    """
    Request body:
    {
      "username": "svinayag",
      "password": "...",
      "server":   "iapp6744.randolph.ms.com",   (optional)
      "clusters": [...],                         (optional — uses config default)
      "restart_required": true,                  (optional — uses config default)
      "wait_time": 600                           (optional — uses config default)
    }
    """
    body = request.get_json(force=True) or {}

    username         = body.get("username", "").strip()
    password         = body.get("password", "")
    server           = body.get("server", CFG.get("jump_server", "")).strip()
    clusters         = body.get("clusters", CFG.get("clusters", []))
    restart_required = body.get("restart_required",
                                str(CFG.get("restart_required", True))
                                ).lower() in ("true", "1", True)
    wait_time        = int(body.get("wait_time", CFG.get("wait_time", 600)))
    exclude          = CFG.get("exclude_services_restart", [])
    namespace        = CFG.get("deployment_id", "wm-10168")

    if not username: return jsonify({"error": "username required"}), 400
    if not password: return jsonify({"error": "password required"}), 400
    if not clusters: return jsonify({"error": "clusters required"}), 400

    job_id = str(uuid.uuid4())
    job    = RestartJobState(job_id, clusters, namespace,
                              restart_required, wait_time, exclude, username)
    q      = queue.Queue()
    _jobs[job_id]       = job
    _job_queues[job_id] = q

    log.info(f"Restart job {job_id} — user={username} "
             f"clusters={len(clusters)} restart={restart_required}")

    t = threading.Thread(
        target=restart_worker,
        args=(job_id, job, password, server, q),
        daemon=True,
        name=f"restart-{job_id[:8]}",
    )
    t.start()

    return jsonify({
        "job_id":     job_id,
        "status":     "PENDING",
        "message":    f"Restart job started — {len(clusters)} clusters",
        "stream_url": f"/api/mks/restart/stream/{job_id}",
        "status_url": f"/api/mks/restart/status/{job_id}",
    }), 202


@app.route("/api/mks/restart/stream/<job_id>", methods=["GET"])
def stream_logs(job_id: str):
    if job_id not in _job_queues:
        return jsonify({"error": "Job not found"}), 404

    q = _job_queues[job_id]

    def generate():
        while True:
            try:
                item = q.get(timeout=90)
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"
                continue
            if item is None:
                job    = _jobs.get(job_id)
                status = job.status if job else "DONE"
                flag   = job.status_flag if job else None
                yield (f"event: done\n"
                       f"data: {json.dumps({'status': status, 'flag': flag})}\n\n")
                break
            yield f"data: {item}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.route("/api/mks/restart/status/<job_id>", methods=["GET"])
def get_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job.to_dict())


@app.route("/api/mks/restart/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.status not in ("PENDING", "RUNNING"):
        return jsonify({"message": f"Already {job.status}"}), 200
    job.status = "CANCELLED"
    log.info(f"Job {job_id} cancelled")
    return jsonify({"message": "Cancellation requested"})


@app.route("/api/mks/restart/jobs", methods=["GET"])
def list_jobs():
    jobs = sorted(_jobs.values(), key=lambda j: j.started_at, reverse=True)[:20]
    return jsonify([j.to_dict() for j in jobs])


@app.route("/api/mks/restart/health", methods=["GET"])
def health():
    return jsonify({
        "status":    "ok",
        "namespace": CFG.get("deployment_id"),
        "clusters":  len(CFG.get("clusters", [])),
        "timestamp": datetime.now().isoformat(),
    })


# =================================================================
#  MAIN
# =================================================================
if __name__ == "__main__":
    log.info("="*60)
    log.info("  PNSRT MKS Restart API")
    log.info(f"  Server     : {CFG.get('jump_server')}")
    log.info(f"  Namespace  : {CFG.get('deployment_id')}")
    log.info(f"  Clusters   : {len(CFG.get('clusters', []))}")
    log.info(f"  Wait time  : {CFG.get('wait_time')}s")
    log.info("="*60)

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("MKS_RESTART_PORT", "5002")),
        debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true",
        threaded=True,
    )
