#!/ms/dist/python/PROJ/core/3.11.4-1/bin/python
"""
=================================================================
pnsrt_mks_restart.py
-----------------------------------------------------------------
PNSRT MKS Cluster — Pod Restart & Status Monitor

What this script does:
  1. Loops through all clusters in config
  2. Logs into each cluster via unimatrix
  3. Restarts all deployments (except excluded ones)
  4. Waits 10 min for pods to come up (if restart_required=true)
  5. Captures pod status per cluster
  6. Writes CSV report to /var/tmp/
  7. Sends HTML email with pod status + CSV attachment

Usage:
  python pnsrt_mks_restart.py pnsrt_mks_restart_config.json

Config: pnsrt_mks_restart_config.json
=================================================================
"""

import os
import sys
import json
import time
import csv
import app_base
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


# =================================================================
#  CLUSTER LOGIN / LOGOUT
# =================================================================

def login_mks_cluster(cluster: str) -> str:
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"🔐 Logging in [{current_time}] : {cluster}")

    cmd = [
        "/ms/dist/cloud/PROJ/unimatrix/prod/bin/unimatrix",
        "login", cluster
    ]
    res = app_base.execute_command(cmd, 60)
    stdout = res.stdout.decode("utf-8")

    if "MKS login completed" in stdout:
        print(f"   ✓ Login successful: {cluster}")
        return "Login successful"
    elif "already logged-in" in stdout:
        print(f"   ✓ Already logged in: {cluster}")
        return "Already logged in"
    else:
        print(f"   ✗ Login failed: {cluster}")
        print(f"     stdout: {stdout[:200]}")
        return "Login failed"


def logout_mks_cluster(cluster: str) -> str:
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"🔓 Logging out [{current_time}] : {cluster}")

    cmd = [
        "/ms/dist/cloud/PROJ/unimatrix/prod/bin/unimatrix",
        "logout", cluster
    ]
    res = app_base.execute_command(cmd, 60)
    print(f"   LOGOUT: {res.stdout.decode('utf-8').strip()}")
    return "MKS logout completed"


# =================================================================
#  TIME HELPERS
# =================================================================

def to_est(utc_time_str: str) -> str:
    """Convert UTC ISO string to EST timezone string."""
    if not utc_time_str or utc_time_str == "N/A":
        return "-"
    try:
        utc_dt = datetime.fromisoformat(utc_time_str.replace("Z", "+00:00"))
        est_tz = ZoneInfo("America/New_York")
        return utc_dt.astimezone(est_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return utc_time_str


# =================================================================
#  PODS / DEPLOYMENTS
# =================================================================

def get_deployments(namespace: str) -> list:
    """Get list of deployment names in given namespace."""
    deployments = []
    cmd = [
        "/ms/dist/cloud/PROJ/kubectl/prod/bin/kubectl",
        "get", "deployments", "-o", "json", "-n", namespace
    ]
    raw  = app_base.execute_command(cmd, 30)
    data = json.loads(raw.stdout.decode("utf-8"))

    for item in data.get("items", []):
        deployments.append(item["metadata"]["name"])

    print(f"   Found {len(deployments)} deployments in namespace: {namespace}")
    return deployments


def restart_deployment(namespace: str, deployment: str) -> bool:
    """Rollout restart a single deployment."""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"♻️  Restarting [{current_time}] : {deployment}")

    cmd = [
        "/ms/dist/cloud/PROJ/kubectl/prod/bin/kubectl",
        "rollout", "restart", "deployment",
        deployment, "-n", namespace
    ]
    res = app_base.execute_command(cmd, 300)

    if res.returncode == 0:
        print(f"   ✓ Restart triggered: {deployment}")
        return True
    else:
        print(f"   ✗ Restart failed: {deployment}")
        print(f"     stderr: {res.stderr.decode('utf-8')[:200]}")
        return False


def get_pods(namespace: str) -> list:
    """Get pod name, status and start time for all pods in namespace."""
    cmd = [
        "/ms/dist/cloud/PROJ/kubectl/prod/bin/kubectl",
        "get", "pods", "-o", "json", "-n", namespace
    ]
    raw  = app_base.execute_command(cmd, 30)
    data = json.loads(raw.stdout.decode("utf-8"))

    pods = []
    for item in data.get("items", []):
        pod_name  = item["metadata"]["name"]
        status    = item["status"].get("phase", "UNKNOWN")
        start_time = item["status"].get("startTime", "N/A")

        pods.append({
            "pod":         pod_name,
            "status":      status,
            "reboot_time": to_est(start_time)
        })

    return pods


# =================================================================
#  CSV WRITER
# =================================================================

def write_csv(file: str, cluster: str, pods: list):
    """Append pod data to CSV in /var/tmp/ — creates header on first write."""
    full_path = os.path.join("/var/tmp/", file)
    exists    = os.path.isfile(full_path)

    with open(full_path, "a", newline="") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(["Cluster", "PodName", "Status", "RestartTime(EST)"])
        for p in pods:
            writer.writerow([cluster, p["pod"], p["status"], p["reboot_time"]])

    print(f"   ✓ CSV updated: {full_path}")


# =================================================================
#  MAIN CLUSTER PROCESSOR
# =================================================================

def process_cluster(cluster: str, config: dict, csv_file: str) -> list:
    """
    Login → restart deployments (if required) → logout.
    Note: Pod status capture happens in the second loop (after wait).
    """
    login_result = login_mks_cluster(cluster)
    if login_result == "Login failed":
        print(f"❌ Login failed — skipping cluster: {cluster}")
        return []

    namespace = config["deployment_id"]

    if config.get("restart_required", "false").lower() == "true":
        deployments = get_deployments(namespace)
        exclude     = config.get("exclude_services_restart", [])

        restarted = 0
        skipped   = 0
        for dep in deployments:
            if dep in exclude:
                print(f"   ⏭  Skipped (excluded): {dep}")
                skipped += 1
                continue
            restart_deployment(namespace, dep)
            restarted += 1

        print(f"   Summary: {restarted} restarted, {skipped} skipped")

    logout_mks_cluster(cluster)
    return []


# =================================================================
#  EMAIL REPORT
# =================================================================

def send_email_report(config: dict, all_results: dict, csv_file: str):
    """Send HTML email with cluster summary table + pod detail table + CSV attachment."""
    from email.mime.text       import MIMEText
    from email.mime.multipart  import MIMEMultipart
    from email.mime.base       import MIMEBase
    from email.utils           import formataddr
    from email.header          import Header
    from email                 import encoders
    import smtplib

    # ── Cluster summary table ─────────────────────────────────────
    metrics_rows = ""
    for cluster, pods in all_results.items():
        total   = len(pods)
        running = sum(1 for p in pods if p["status"] == "Running")
        others  = total - running
        row_color = "#e8f5e9" if others == 0 else "#ffebee"
        metrics_rows += (
            f"<tr style='background:{row_color}'>"
            f"<td style='padding:7px 12px;font-family:monospace'>{cluster}</td>"
            f"<td style='padding:7px 12px;text-align:center'>{total}</td>"
            f"<td style='padding:7px 12px;text-align:center;color:green;font-weight:700'>{running}</td>"
            f"<td style='padding:7px 12px;text-align:center;color:{'red' if others else 'green'};font-weight:700'>{others}</td>"
            f"</tr>"
        )

    metrics_html = f"""
    <table border="1" cellspacing="0" cellpadding="5"
           style="border-collapse:collapse;font-family:Arial;width:100%;margin-bottom:20px">
      <tr style="background:#001F5B;color:white">
        <th style="padding:9px 14px">Cluster</th>
        <th style="padding:9px 14px">Total Pods</th>
        <th style="padding:9px 14px">Running</th>
        <th style="padding:9px 14px">Others</th>
      </tr>
      {metrics_rows}
    </table>"""

    # ── Detailed pod table ────────────────────────────────────────
    pod_rows = ""
    for cluster, pods in all_results.items():
        for p in pods:
            ok    = p["status"] == "Running"
            color = "#00875a" if ok else "#de350b"
            icon  = "✅" if ok else "🔴"
            pod_rows += (
                f"<tr style='border-bottom:1px solid #f0f0f0'>"
                f"<td style='padding:6px 12px;font-family:monospace;font-size:12px'>{cluster}</td>"
                f"<td style='padding:6px 12px;font-family:monospace;font-size:12px'>{p['pod']}</td>"
                f"<td style='padding:6px 12px;color:{color};font-weight:700'>{icon} {p['status']}</td>"
                f"<td style='padding:6px 12px;font-family:monospace;font-size:12px'>{p['reboot_time']}</td>"
                f"</tr>"
            )

    pod_html = f"""
    <table border="1" cellspacing="0" cellpadding="5"
           style="border-collapse:collapse;font-family:Arial;width:100%">
      <tr style="background:#f4f5f7">
        <th style="padding:8px 12px">Cluster</th>
        <th style="padding:8px 12px">Pod Name</th>
        <th style="padding:8px 12px">Status</th>
        <th style="padding:8px 12px">Restart Time (EST)</th>
      </tr>
      {pod_rows}
    </table>"""

    report_time = to_est(datetime.utcnow().isoformat() + "Z")

    # ── Overall status flag ───────────────────────────────────────
    all_pods = [p for pods in all_results.values() for p in pods]
    status_flag = "RED" if any(p["status"] != "Running" for p in all_pods) else "GREEN"
    flag_color  = "#de350b" if status_flag == "RED" else "#00875a"
    flag_icon   = "🔴" if status_flag == "RED" else "✅"

    html = f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;background:#f4f5f7;padding:20px;margin:0">
<table width="680" cellpadding="0" cellspacing="0"
       style="background:#fff;border-radius:10px;overflow:hidden;
              box-shadow:0 2px 12px rgba(0,0,0,.12);margin:0 auto;">

  <!-- MS Navy Header -->
  <tr><td style="background:#001F5B;padding:14px 24px">
    <span style="color:#fff;font-size:15px;font-weight:700">Morgan Stanley</span>
    <span style="color:#ffffff44;margin:0 8px">|</span>
    <span style="color:#ffffffbb;font-size:13px">PNSRT MKS Pod Restart Report</span>
  </td></tr>

  <!-- Status Banner -->
  <tr><td style="background:{flag_color};padding:14px 24px">
    <div style="color:#fff;font-size:18px;font-weight:700">
      {flag_icon} Overall Status: {status_flag}
    </div>
    <div style="color:rgba(255,255,255,.8);font-size:12px;margin-top:4px">
      Report generated: {report_time}
    </div>
  </td></tr>

  <!-- Intro -->
  <tr><td style="padding:20px 24px">
    <p style="margin:0 0 10px;color:#333">Hi All,</p>
    <p style="margin:0;color:#555">Please find the PODs restart status with respect to each cluster.
    All timestamps are shown in <strong>EST timezone</strong>.</p>
  </td></tr>

  <!-- Cluster Summary -->
  <tr><td style="padding:0 24px 16px">
    <h3 style="margin:0 0 10px;color:#001F5B;font-size:14px;
               text-transform:uppercase;letter-spacing:1px">
      Cluster-wise Summary
    </h3>
    {metrics_html}
  </td></tr>

  <!-- Pod Detail -->
  <tr><td style="padding:0 24px 20px">
    <h3 style="margin:0 0 10px;color:#001F5B;font-size:14px;
               text-transform:uppercase;letter-spacing:1px">
      Detailed POD Status
    </h3>
    {pod_html}
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#f4f5f7;padding:14px 24px;border-top:1px solid #dfe1e6">
    <p style="margin:0;color:#666;font-size:12px">
      Regards,<br><strong>PNSRT ASG</strong><br>
      <em style="color:#999">Automated notification — do not reply</em>
    </p>
  </td></tr>

</table>
</body></html>"""

    # ── Compose email ─────────────────────────────────────────────
    msg = MIMEMultipart()
    msg["From"]    = formataddr(
        (str(Header("PNSRT Cluster Monitor", "utf-8")),
         config["email"]["sender"])
    )
    msg["To"]      = config["email"]["receiver"]
    msg["Subject"] = (
        f"{status_flag} {config['email']['subject']} "
        f"- {report_time}"
    )
    msg.attach(MIMEText(html, "html"))

    # Attach CSV
    full_csv_path = os.path.join("/var/tmp/", csv_file)
    if os.path.isfile(full_csv_path):
        with open(full_csv_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename={os.path.basename(csv_file)}"
            )
            msg.attach(part)

    # Send
    receivers = config["email"]["receiver"].split(",")
    server    = smtplib.SMTP("msa-hub.ms.com")
    server.sendmail(config["email"]["sender"], receivers, msg.as_string())
    server.close()
    print(f"✉  Email sent → {config['email']['receiver']}")


# =================================================================
#  MAIN  (merged from image — lines 204–237)
# =================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pnsrt_mks_restart.py <config_file.json>")
        sys.exit(1)

    config_file = sys.argv[1]

    with open(config_file, "r") as f:
        config = json.load(f)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_file  = f"pods_status_{timestamp}.csv"
    all_results: dict = {}

    # ── Phase 1: Login + Restart deployments on all clusters ─────
    print("\n" + "="*60)
    print("  PHASE 1 — Restarting deployments on all clusters")
    print("="*60)

    for cluster in config["clusters"]:
        print(f"\n=== Processing Cluster: {cluster} ===")
        process_cluster(cluster, config, csv_file)

    # ── Wait for pods to come up ──────────────────────────────────
    if config.get("restart_required", "false").lower() == "true":
        wait_secs = int(config.get("wait_time", 600))
        print(f"\n⏳ Restart in progress. Waiting {wait_secs}s "
              f"({wait_secs//60} min) for pods to stabilise...")
        print("   We kindly request you wait before checking pod status.")
        time.sleep(wait_secs)
    else:
        time.sleep(10)

    # ── Phase 2: Login + Capture pod status on all clusters ───────
    print("\n" + "="*60)
    print("  PHASE 2 — Capturing POD status from all clusters")
    print("="*60)

    for cluster in config["clusters"]:
        login_result = login_mks_cluster(cluster)
        if login_result in ("Login successful", "Already logged in"):
            print(f"\n=== Capturing POD status for Cluster: {cluster} ===")
            pods = get_pods(config["deployment_id"])
            all_results[cluster] = pods
            write_csv(csv_file, cluster, pods)
            logout_mks_cluster(cluster)
        else:
            print(f"❌ Cannot capture pods — login failed: {cluster}")
            all_results[cluster] = []

    # ── Set email subject with status flag ────────────────────────
    all_pods    = [p for pods in all_results.values() for p in pods]
    status_flag = "RED" if any(
        p["status"] != "Running" for p in all_pods
    ) else "GREEN"
    config["email"]["subject"] = (
        f"{status_flag} {config['email']['subject']}"
    )

    # ── Send report ───────────────────────────────────────────────
    print("\n📧 Sending email report...")
    send_email_report(config, all_results, csv_file)
    print("[✉] Email report sent.")

    # ── Final summary ─────────────────────────────────────────────
    print("\n" + "="*60)
    print(f"  COMPLETE — Status: {status_flag}")
    print(f"  CSV saved: /var/tmp/{csv_file}")
    print("="*60)
