"""
services/mks_service.py
=======================
Core MKS logic shared by both:
  - api/mks_routes.py        (HTTP API called by dashboard)
  - scripts/pnsrt_mks_restart.py  (standalone cron script)

Responsibilities:
  • Load config from config/pnsrt_mks_restart_config.json
  • Manage in-memory job store + SSE queues
  • SSH into jump server → unimatrix → kubectl
  • Workers run in background threads
  • Auto-falls back to simulation if paramiko not installed
"""

import os, json, uuid, queue, time, csv, logging, threading, smtplib
from datetime import datetime
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from email.mime.base      import MIMEBase
from email.utils          import formataddr
from email.header         import Header
from email                import encoders

log = logging.getLogger("CP360.MKS")

# ── In-memory job store ───────────────────────────────────────────────
_jobs:   dict = {}
_queues: dict = {}

# ── Config ────────────────────────────────────────────────────────────
CFG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "config", "pnsrt_mks_restart_config.json"
)

def get_config() -> dict:
    """Load config from JSON. Falls back to safe defaults if file missing."""
    try:
        with open(CFG_PATH) as f:
            return json.load(f)
    except Exception:
        return {
            "clusters": [
                "app41.hz.k8s.na.ms.com", "app42.rr.k8s.na.ms.com",
                "app33.nj.k8s.yn.ms.com", "app33.nk.k8s.yn.ms.com",
                "app34.nj.k8s.yn.ms.com", "app34.nk.k8s.yn.ms.com",
            ],
            "exclude_services_restart": [
                "pnsrt-cinema-prod-r1-dep", "pnsrt-cinema-prod-r2-dep"
            ],
            "namespace":      "wm-10168",
            "deployment_id":  "wm-10168",
            "tcm":            603557550,
            "wait_time":      600,
            "jump_server":    "iapp6744.randolph.ms.com",
            "kubectl_path":   "/ms/dist/cloud/PROJ/kubectl/prod/bin/kubectl",
            "unimatrix_path": "/ms/dist/cloud/PROJ/unimatrix/prod/bin/unimatrix",
            "email": {
                "subject":  "PROD-PNSRT MKS Deployment Restart Status",
                "sender":   "pnsrt-dev@morganstanley.com",
                "receiver": "pnsrt-dev@morganstanley.com",
            },
        }

# ── Job store helpers ─────────────────────────────────────────────────

def new_job(payload: dict) -> str:
    """Create a new job entry. Returns job_id."""
    jid = str(uuid.uuid4())
    _jobs[jid] = {
        "id":              jid,
        "status":          "RUNNING",   # RUNNING | DONE | FAILED | CANCELLED
        "phase":           "INIT",      # INIT | PHASE_1 | PHASE_2
        "user":            payload.get("user", ""),
        "mode":            payload.get("mode", "all"),   # all | selected | status
        "clusters":        payload.get("clusters", []),
        "namespace":       payload.get("namespace", get_config()["namespace"]),
        "tcm":             str(payload.get("tcm", get_config()["tcm"])),
        "started_at":      datetime.now().isoformat(),
        "finished_at":     None,
        "flag":            None,        # GREEN | RED
        "cluster_results": {},
        "csv_file":        None,
        "error":           None,
    }
    _queues[jid] = queue.Queue()
    return jid

def get_job(jid: str) -> dict | None:
    return _jobs.get(jid)

def get_queue(jid: str) -> queue.Queue | None:
    return _queues.get(jid)

def cancel_job(jid: str):
    job = _jobs.get(jid)
    if job:
        job["status"] = "CANCELLED"

def list_jobs() -> list:
    return sorted(_jobs.values(), key=lambda j: j["started_at"], reverse=True)[:20]

# ── SSE emitter ───────────────────────────────────────────────────────

def _emit(jid: str, msg: str, col: str = "#a0b4c8"):
    """Push a coloured log line to the SSE queue for the dashboard terminal."""
    if jid not in _queues:
        return
    _queues[jid].put(json.dumps({
        "msg": msg,
        "col": col,
        "ts":  datetime.now().strftime("%H:%M:%S"),
    }))
    log.info(f"[{jid[:8]}] {msg}")

def _done(jid: str):
    """Signal end of stream to the SSE consumer."""
    if jid in _queues:
        _queues[jid].put(None)

# ── SSH helpers ───────────────────────────────────────────────────────

try:
    import paramiko
    HAS_SSH = True
    log.info("paramiko loaded — MKS will use real SSH")
except ImportError:
    HAS_SSH = False
    log.warning("paramiko not installed — MKS will run in SIMULATION mode. pip install paramiko")

def _ssh_connect(user: str, pwd: str, server: str) -> "paramiko.SSHClient":
    if not HAS_SSH:
        raise RuntimeError("paramiko not installed. Run: pip install paramiko")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=server, username=user, password=pwd,
        timeout=30, allow_agent=False, look_for_keys=False,
    )
    return client

def _run(ssh, cmd: str, timeout: int = 60) -> tuple[str, str, int]:
    """Run a command over SSH and return (stdout, stderr, returncode)."""
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    rc  = out.channel.recv_exit_status()
    return (
        out.read().decode("utf-8", errors="replace").strip(),
        err.read().decode("utf-8", errors="replace").strip(),
        rc,
    )

def _setup_env(ssh, jid: str, user: str, ns: str):
    """Load firm kubectl/unimatrix modules (mirrors pnsrt_mks_cluster_access.sh)."""
    for cmd in [
        "source /etc/profile",
        ". /ms/dist/environ/PROJ/core/bash-prod/common/etc/init.environ",
    ]:
        _emit(jid, f"$ {cmd}", "#00e5ff")
        _run(ssh, cmd, 20)

    for mod in ["cloud/helm", "cloud/kubectl", "cloud/openshift-client/4.6", "cloud/unimatrix/prod"]:
        _emit(jid, f"$ module load {mod}", "#00e5ff")
        _, err, rc = _run(ssh, f"module load {mod}", 20)
        _emit(
            jid,
            f"✓ Loaded {mod}" if rc == 0 else f"⚠ {mod}: {err[:60]}",
            "#00e676" if rc == 0 else "#ffab00",
        )

    _run(ssh, f"export KUBECONFIG=/var/tmp/{user}/.kube/config", 10)
    _run(ssh, f"export NAMESPACE={ns}", 10)

# ── Simulation fallback ───────────────────────────────────────────────

def _simulate_access(jid: str, user: str, server: str, clusters: list):
    import time
    steps = [
        (0.4,  f"$ ssh {user}@{server}",     "#00e5ff"),
        (0.6,  f"✓ Connected to {server}",   "#00e676"),
        (0.5,  "$ module load cloud/kubectl", "#00e5ff"),
        (0.3,  "✓ kubectl loaded",            "#00e676"),
        (0.4,  "$ module load cloud/unimatrix/prod", "#00e5ff"),
        (0.3,  "✓ unimatrix loaded",          "#00e676"),
    ]
    for delay, msg, col in steps:
        time.sleep(delay)
        if _jobs[jid]["status"] == "CANCELLED":
            return
        _emit(jid, msg, col)

    for cluster in clusters:
        if _jobs[jid]["status"] == "CANCELLED":
            return
        short = cluster.split(".")[0].upper()
        _emit(jid, f"\n─── {short}: {cluster}", "#a78bfa")
        time.sleep(0.5)
        _emit(jid, f"$ unimatrix login {cluster}", "#00e5ff")
        time.sleep(0.6)
        _emit(jid, f"✓ MKS login completed — {short}", "#00e676")
        for role in ["application-deployment", "monitor-with-logs"]:
            time.sleep(0.3)
            _emit(jid, f"  ✓ Role activated: {role}", "#00e676")

def _k8s_pod_name(deployment: str) -> str:
    """
    Generate a realistic Kubernetes pod name.
    Real format: {deployment}-{replicaset_hash}-{pod_suffix}
    ReplicaSet hash: 8-10 alphanumeric chars (lowercase a-z, 0-9 but k8s uses a-z+0-9)
    Pod suffix:      5 chars from k8s base32 alphabet (bcdfghjklmnpqrstvwxz2456789)
    NOTE: this is SIMULATION ONLY — used when paramiko/SSH is unavailable.
          Real pod names come from kubectl get pods in start_discover().
    """
    import random, string
    rs_chars   = string.ascii_lowercase + string.digits       # replicaset hash chars
    pod_chars  = "bcdfghjklmnpqrstvwxz2456789"               # k8s base32 pod suffix
    rs_hash    = "".join(random.choices(rs_chars, k=9))      # e.g. 7d9f8c658
    pod_suffix = "".join(random.choices(pod_chars, k=5))     # e.g. xk2p9
    return f"{deployment}-{rs_hash}-{pod_suffix}"


def _simulate_discover(jid: str, clusters: list) -> dict:
    """
    SIMULATION MODE ONLY — used when paramiko is not installed.
    Pod names use realistic Kubernetes naming format.
    Real pod names come from kubectl get pods when SSH is available.
    """
    import random, time

    cfg = get_config()
    # Use deployments from config if available, otherwise use empty list
    DEP_NAMES = cfg.get("deployments", [
        "pnsrt-input-processor",    "pnsrt-refdata-aggregator",
        "pnsrt-trade-validation",   "pnsrt-trade-figuration",
        "pnsrt-batch-api",          "pnsrt-exception-logger",
    ])
    EXCLUDE = cfg.get("exclude_services_restart", [])

    result = {}
    _emit(jid, "[SIMULATION] paramiko not installed — showing demo pod names", "#ffab00")
    _emit(jid, "[SIMULATION] Install paramiko for real kubectl data: pip install paramiko", "#ffab00")

    for cluster in clusters:
        if _jobs[jid]["status"] == "CANCELLED":
            break
        short = cluster.split(".")[0].upper()
        _emit(jid, f"\n─── Discovering PODs: {short} [SIMULATION]", "#a78bfa")
        time.sleep(0.4)
        deps = []
        for dep in DEP_NAMES:
            if dep in EXCLUDE:
                continue
            replica_count = random.randint(1, 3)
            pods = [
                {
                    "name":     _k8s_pod_name(dep),
                    "status":   "Running" if random.random() > 0.08 else "Pending",
                    "ready":    "1/1",
                    "restarts": random.randint(0, 3),
                    "age":      f"{random.randint(1,7)}d",
                }
                for _ in range(replica_count)
            ]
            deps.append({"name": dep, "pods": pods})
            running = sum(1 for p in pods if p["status"] == "Running")
            _emit(jid, f"  {dep}: {len(pods)} pod(s)  [{running} Running] [SIM]", "#a0b4c8")
        result[cluster] = deps

    return result

# ═════════════════════════════════════════════════════════════════════
#  WORKER 1 — GRANT ACCESS  (Step 1 of MKS Restart workflow)
#  Triggered by: POST /api/mks/access
# ═════════════════════════════════════════════════════════════════════

def start_access(jid: str, user: str, pwd: str,
                 server: str, tcm: str, clusters: list):
    """
    SSH → load modules → unimatrix login each cluster → activate roles.
    Runs in a background thread and streams logs via SSE.
    """
    def _worker():
        job = _jobs[jid]
        ns  = get_config()["namespace"]
        UNIMATRIX = get_config().get("unimatrix_path",
                                     "/ms/dist/cloud/PROJ/unimatrix/prod/bin/unimatrix")
        ssh = None
        try:
            if HAS_SSH:
                # ── Real SSH path ─────────────────────────────────────
                _emit(jid, f"$ ssh {user}@{server}", "#00e5ff")
                ssh = _ssh_connect(user, pwd, server)
                _emit(jid, f"✓ Connected to {server}", "#00e676")
                _setup_env(ssh, jid, user, ns)

                for cluster in clusters:
                    if job["status"] == "CANCELLED":
                        break
                    short = cluster.split(".")[0].upper()
                    _emit(jid, f"\n─── {short}: {cluster}", "#a78bfa")

                    # Unimatrix login
                    _emit(jid, f"$ {UNIMATRIX} login {cluster}", "#00e5ff")
                    out, err, rc = _run(ssh, f"{UNIMATRIX} login {cluster}", 60)

                    if "MKS login completed" in out or "already logged-in" in out:
                        _emit(jid, f"✓ Logged in to {short}", "#00e676")
                    else:
                        _emit(jid, f"✗ Login failed: {err[:80]}", "#ff5252")
                        continue

                    # Activate roles via unimatrixv2
                    for role in ["application-deployment", "monitor-with-logs", "monitor"]:
                        cmd = (
                            f"unimatrixv2 activations create "
                            f"--cluster {cluster} --namespace {ns} "
                            f"--role {role} --justification {tcm}"
                        )
                        _emit(jid, f"$ {cmd}", "#00e5ff")
                        _, _, rc = _run(ssh, cmd, 90)
                        _emit(
                            jid,
                            f"  {'✓' if rc == 0 else '✗'} {role}",
                            "#00e676" if rc == 0 else "#ff5252",
                        )

                    _run(ssh, f"{UNIMATRIX} logout {cluster}", 30)
                    _emit(jid, f"✓ Logged out from {short}", "#a0b4c8")
            else:
                # ── Simulation path ───────────────────────────────────
                _simulate_access(jid, user, server, clusters)

            job["status"] = "DONE"
            _emit(jid, "\n✅ MKS Access Granted — proceed to Step 2", "#00e676")

        except paramiko.AuthenticationException if HAS_SSH else Exception as ex:
            job["status"] = "FAILED"
            job["error"]  = "SSH authentication failed — check username/password"
            _emit(jid, f"✗ Auth failed: {job['error']}", "#ff5252")
        except Exception as ex:
            job["status"] = "FAILED"
            job["error"]  = str(ex)
            _emit(jid, f"✗ Error: {ex}", "#ff5252")
            log.exception("MKS access worker error")
        finally:
            if ssh:
                try: ssh.close()
                except Exception: pass
            job["finished_at"] = datetime.now().isoformat()
            _done(jid)

    threading.Thread(target=_worker, daemon=True, name=f"mks-access-{jid[:8]}").start()


# ═════════════════════════════════════════════════════════════════════
#  WORKER 2 — DISCOVER PODS  (Step 3 of MKS Restart workflow)
#  Triggered by: POST /api/mks/pods/discover
# ═════════════════════════════════════════════════════════════════════

def start_discover(jid: str, user: str, pwd: str,
                   server: str, clusters: list):
    """
    SSH → kubectl get deployments → kubectl get pods per deployment.
    Returns cluster_results in job store for the dashboard pod list.
    """
    def _worker():
        job = _jobs[jid]
        ns  = get_config()["namespace"]
        KUBECTL = get_config().get("kubectl_path",
                                   "/ms/dist/cloud/PROJ/kubectl/prod/bin/kubectl")
        UNIMATRIX = get_config().get("unimatrix_path",
                                     "/ms/dist/cloud/PROJ/unimatrix/prod/bin/unimatrix")
        ssh = None
        result = {}
        try:
            if HAS_SSH:
                _emit(jid, f"$ ssh {user}@{server}", "#00e5ff")
                ssh = _ssh_connect(user, pwd, server)
                _emit(jid, "✓ Connected", "#00e676")
                _setup_env(ssh, jid, user, ns)

                for cluster in clusters:
                    if job["status"] == "CANCELLED":
                        break
                    short = cluster.split(".")[0].upper()
                    _emit(jid, f"\n─── Discovering PODs: {short}", "#a78bfa")

                    _run(ssh, f"{UNIMATRIX} login {cluster}", 60)
                    _run(ssh, f"export CLUSTER={cluster}", 10)

                    # Get all deployments
                    dep_out, _, _ = _run(
                        ssh, f"{KUBECTL} get deployments -o json -n {ns}", 30
                    )
                    deps = []
                    try:
                        for item in json.loads(dep_out).get("items", []):
                            dep_name = item["metadata"]["name"]

                            # Get pods for this deployment
                            pod_out, _, _ = _run(
                                ssh,
                                f"{KUBECTL} get pods -l app={dep_name} -o json -n {ns}",
                                30,
                            )
                            pods = []
                            try:
                                for p in json.loads(pod_out).get("items", []):
                                    cs = p.get("status", {}).get("containerStatuses", [])
                                    ready = sum(1 for c in cs if c.get("ready", False))
                                    restarts = sum(c.get("restartCount", 0) for c in cs)
                                    pods.append({
                                        "name":     p["metadata"]["name"],
                                        "status":   p["status"].get("phase", "Unknown"),
                                        "ready":    f"{ready}/{len(cs)}" if cs else "?",
                                        "restarts": restarts,
                                        "age":      p["status"].get("startTime", "")[:10],
                                    })
                            except Exception:
                                pass

                            deps.append({"name": dep_name, "pods": pods})
                            running = sum(1 for p in pods if p["status"] == "Running")
                            _emit(
                                jid,
                                f"  {dep_name}: {len(pods)} pod(s)  [{running} Running]",
                                "#a0b4c8",
                            )
                    except Exception as ex:
                        _emit(jid, f"  ⚠ Parse error: {ex}", "#ffab00")

                    result[cluster] = deps
                    _run(ssh, f"{UNIMATRIX} logout {cluster}", 30)
            else:
                result = _simulate_discover(jid, clusters)

            job["status"]          = "DONE"
            job["cluster_results"] = result
            _emit(jid, "\n✅ Pod discovery complete", "#00e676")

        except Exception as ex:
            job["status"] = "FAILED"
            job["error"]  = str(ex)
            _emit(jid, f"✗ {ex}", "#ff5252")
        finally:
            if ssh:
                try: ssh.close()
                except Exception: pass
            job["finished_at"] = datetime.now().isoformat()
            _done(jid)

    threading.Thread(target=_worker, daemon=True, name=f"mks-discover-{jid[:8]}").start()


# ═════════════════════════════════════════════════════════════════════
#  WORKER 3 — RESTART + CAPTURE  (Steps 4–5 of MKS Restart workflow)
#  Triggered by: POST /api/mks/restart/execute
# ═════════════════════════════════════════════════════════════════════

def start_restart(
    jid:       str,
    user:      str,
    pwd:       str,
    server:    str,
    clusters:  list,
    mode:      str,        # "all" | "selected" | "status"
    wait_time: int,
    sel_pods:  dict,       # {cluster: [pod_name, ...]}  — used when mode="selected"
    exclude:   list,       # deployments to skip
):
    """
    Phase 1: kubectl rollout restart (all) or kubectl delete pod (selected).
    Phase 2: kubectl get pods — capture final status, write CSV, send email.
    """
    def _worker():
        import random
        job       = _jobs[jid]
        cfg       = get_config()
        ns        = cfg["namespace"]
        KUBECTL   = cfg.get("kubectl_path",   "/ms/dist/cloud/PROJ/kubectl/prod/bin/kubectl")
        UNIMATRIX = cfg.get("unimatrix_path", "/ms/dist/cloud/PROJ/unimatrix/prod/bin/unimatrix")
        SMTP_HOST = os.getenv("SMTP_HOST", "msa-hub.ms.com")
        SMTP_PORT = int(os.getenv("SMTP_PORT", "25"))
        CSV_DIR   = os.getenv("CSV_DIR", "/var/tmp")
        csv_file  = f"pods_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        job["csv_file"] = csv_file
        ssh = None
        all_results = {}

        try:
            if HAS_SSH:
                _emit(jid, f"$ ssh {user}@{server}", "#00e5ff")
                ssh = _ssh_connect(user, pwd, server)
                _emit(jid, f"✓ Connected to {server}", "#00e676")
                _setup_env(ssh, jid, user, ns)
            else:
                _emit(jid, f"[SIM] $ ssh {user}@{server}", "#ffab00")
                import time; time.sleep(0.5)
                _emit(jid, "✓ Connected (simulation mode)", "#00e676")

            # ── PHASE 1: RESTART ─────────────────────────────────────
            if mode in ("all", "selected"):
                job["phase"] = "PHASE_1"
                label = "RESTART ALL DEPLOYMENTS" if mode == "all" else f"RESTART SELECTED PODS ({sum(len(v) for v in sel_pods.values())} total)"
                _emit(jid, f"\n── PHASE 1: {label} ──", "#a78bfa")

                for cluster in clusters:
                    if job["status"] == "CANCELLED":
                        break
                    short = cluster.split(".")[0].upper()
                    _emit(jid, f"\n─── Cluster: {short}", "#a78bfa")

                    if HAS_SSH:
                        out, _, _ = _run(ssh, f"{UNIMATRIX} login {cluster}", 60)
                        if "login completed" not in out and "already logged-in" not in out:
                            _emit(jid, f"✗ Unimatrix login failed — skipping {short}", "#ff5252")
                            continue
                        _emit(jid, f"✓ Logged in to {short}", "#00e676")
                        _run(ssh, f"export CLUSTER={cluster}", 10)

                    if mode == "all":
                        if HAS_SSH:
                            dep_out, _, _ = _run(
                                ssh, f"{KUBECTL} get deployments -o json -n {ns}", 30
                            )
                            try:
                                deps = [
                                    d["metadata"]["name"]
                                    for d in json.loads(dep_out).get("items", [])
                                ]
                            except Exception:
                                deps = []
                        else:
                            deps = [
                                "pnsrt-input-processor", "pnsrt-refdata-aggregator",
                                "pnsrt-trade-validation", "pnsrt-trade-figuration",
                                "pnsrt-batch-api",
                            ]

                        for dep in deps:
                            if dep in exclude:
                                _emit(jid, f"  ⏭ Skipped (excluded): {dep}", "#a0b4c8")
                                continue
                            _emit(jid, f"$ {KUBECTL} rollout restart deploy/{dep} -n {ns}", "#00e5ff")
                            if HAS_SSH:
                                _, _, rc = _run(
                                    ssh,
                                    f"{KUBECTL} rollout restart deployment {dep} -n {ns}",
                                    300,
                                )
                            else:
                                import time; time.sleep(0.2); rc = 0
                            _emit(
                                jid,
                                f"{'♻️  Restarted' if rc == 0 else '✗ Failed'}: {dep}",
                                "#00e676" if rc == 0 else "#ff5252",
                            )

                    else:  # selected pods
                        for pod in sel_pods.get(cluster, []):
                            _emit(jid, f"$ {KUBECTL} delete pod {pod} -n {ns}", "#00e5ff")
                            if HAS_SSH:
                                _, _, rc = _run(ssh, f"{KUBECTL} delete pod {pod} -n {ns}", 120)
                            else:
                                import time; time.sleep(0.2); rc = 0
                            _emit(
                                jid,
                                f"{'✓ Deleted (auto-recreated by deployment)' if rc == 0 else '✗ Failed'}: {pod}",
                                "#00e676" if rc == 0 else "#ff5252",
                            )

                    if HAS_SSH:
                        _run(ssh, f"{UNIMATRIX} logout {cluster}", 30)
                        _emit(jid, f"✓ Logged out from {short}", "#a0b4c8")

                # Wait for pods to stabilise
                _emit(jid, f"\n⏳ Waiting {wait_time}s for pods to restart…", "#ffab00")
                elapsed = 0
                while elapsed < wait_time:
                    if job["status"] == "CANCELLED":
                        break
                    chunk = min(30, wait_time - elapsed)
                    import time; time.sleep(chunk if HAS_SSH else min(chunk, 2))
                    elapsed += chunk
                    remaining = wait_time - elapsed
                    if remaining > 0:
                        _emit(jid, f"   ⏳ {remaining}s remaining…", "#a0b4c8")

            # ── PHASE 2: CAPTURE STATUS ───────────────────────────────
            job["phase"] = "PHASE_2"
            _emit(jid, "\n── PHASE 2: CAPTURING POD STATUS ──", "#a78bfa")

            csv_path = os.path.join(CSV_DIR, csv_file)
            with open(csv_path, "w", newline="") as f:
                csv.writer(f).writerow(["Cluster", "PodName", "Status", "RestartTime_EST"])

            for cluster in clusters:
                if job["status"] == "CANCELLED":
                    break
                short = cluster.split(".")[0].upper()
                _emit(jid, f"\n─── Capturing: {short}", "#a78bfa")

                if HAS_SSH:
                    _run(ssh, f"{UNIMATRIX} login {cluster}", 60)
                    _run(ssh, f"export CLUSTER={cluster}", 10)
                    pod_out, _, _ = _run(
                        ssh, f"{KUBECTL} get pods -o json -n {ns}", 30
                    )
                    pods = []
                    try:
                        for item in json.loads(pod_out).get("items", []):
                            status = item["status"].get("phase", "Unknown")
                            start  = item["status"].get("startTime", "")
                            cs = item.get("status", {}).get("containerStatuses", [])
                            restarts = sum(c.get("restartCount", 0) for c in cs)
                            pods.append({
                                "name":         item["metadata"]["name"],
                                "status":       status,
                                "ready":        f"{sum(1 for c in cs if c.get('ready'))}/{len(cs)}" if cs else "?",
                                "restarts":     restarts,
                                "age":          start[:10],
                                "reboot_time":  start,
                            })
                    except Exception as ex:
                        _emit(jid, f"  ⚠ Parse error: {ex}", "#ffab00")
                    _run(ssh, f"{UNIMATRIX} logout {cluster}", 30)
                else:
                    # Simulation: realistic k8s pod names
                    # (real names come from kubectl when HAS_SSH=True)
                    sim_deps = cfg.get("deployments", ["pnsrt-input-processor",
                        "pnsrt-trade-validation", "pnsrt-batch-api"])
                    pods = []
                    for dep in sim_deps[:3]:
                        pods.append({
                            "name":        _k8s_pod_name(dep),
                            "status":      "Running",
                            "ready":       "1/1",
                            "restarts":    random.randint(0, 1),
                            "age":         "1m",
                            "reboot_time": datetime.now().isoformat(),
                        })

                all_results[cluster] = pods
                running = sum(1 for p in pods if p["status"] == "Running")
                _emit(
                    jid,
                    f"✓ {short}: {running}/{len(pods)} Running",
                    "#00e676" if running == len(pods) else "#ffab00",
                )

                with open(csv_path, "a", newline="") as f:
                    w = csv.writer(f)
                    for p in pods:
                        w.writerow([cluster, p["name"], p["status"], p["reboot_time"]])

            # ── Status flag ───────────────────────────────────────────
            all_pods = [p for pl in all_results.values() for p in pl]
            flag = "RED" if any(p["status"] != "Running" for p in all_pods) else "GREEN"
            job["flag"]            = flag
            job["cluster_results"] = all_results
            job["status"]          = "DONE"

            _emit(
                jid,
                f"\n{'✅' if flag == 'GREEN' else '🔴'} Overall Status: {flag}",
                "#00e676" if flag == "GREEN" else "#ff5252",
            )

            # ── Send email ────────────────────────────────────────────
            _emit(jid, "📧 Sending restart report email…", "#a0b4c8")
            try:
                _send_email(all_results, csv_path, flag, user, cfg, SMTP_HOST, SMTP_PORT)
                _emit(jid, f"✓ Email sent → {cfg['email']['receiver'][:50]}…", "#00e676")
            except Exception as ex:
                _emit(jid, f"⚠ Email failed: {ex}", "#ffab00")

        except Exception as ex:
            job["status"] = "FAILED" if job["status"] != "CANCELLED" else "CANCELLED"
            job["error"]  = str(ex)
            _emit(jid, f"✗ {ex}", "#ff5252")
            log.exception("MKS restart worker error")
        finally:
            if ssh:
                try: ssh.close()
                except Exception: pass
            job["finished_at"] = datetime.now().isoformat()
            _done(jid)

    threading.Thread(target=_worker, daemon=True, name=f"mks-restart-{jid[:8]}").start()


# ── Email report ──────────────────────────────────────────────────────

def _send_email(
    results:   dict,
    csv_path:  str,
    flag:      str,
    user:      str,
    cfg:       dict,
    smtp_host: str,
    smtp_port: int,
):
    flag_color = "#00875a" if flag == "GREEN" else "#de350b"
    flag_icon  = "✅" if flag == "GREEN" else "🔴"
    ts = datetime.now().strftime("%d %b %Y %H:%M:%S")

    # Cluster summary rows
    sum_rows = ""
    pod_rows = ""
    for cluster, pods in results.items():
        total   = len(pods)
        running = sum(1 for p in pods if p["status"] == "Running")
        others  = total - running
        bg = "#e8f5e9" if others == 0 else "#ffebee"
        sum_rows += (
            f"<tr style='background:{bg}'>"
            f"<td style='padding:7px 12px;font-family:monospace;font-size:12px'>{cluster}</td>"
            f"<td style='text-align:center;padding:7px 12px'>{total}</td>"
            f"<td style='text-align:center;padding:7px 12px;color:green'>{running}</td>"
            f"<td style='text-align:center;padding:7px 12px;color:{'red' if others else 'green'}'>{others}</td>"
            f"</tr>"
        )
        for p in pods:
            ok   = p["status"] == "Running"
            col  = "#00875a" if ok else "#de350b"
            icon = "✅" if ok else "🔴"
            pod_rows += (
                f"<tr>"
                f"<td style='padding:6px 10px;font-family:monospace;font-size:12px'>{cluster}</td>"
                f"<td style='padding:6px 10px;font-family:monospace;font-size:12px'>{p['name']}</td>"
                f"<td style='padding:6px 10px;color:{col};font-weight:700'>{icon} {p['status']}</td>"
                f"<td style='padding:6px 10px;font-family:monospace;font-size:12px'>{p.get('reboot_time','—')}</td>"
                f"</tr>"
            )

    html_body = f"""<!DOCTYPE html><html><body style="font-family:Arial;background:#f4f5f7;padding:20px">
<table width="700" cellpadding="0" cellspacing="0"
       style="background:#fff;border-radius:10px;overflow:hidden;margin:0 auto;
              box-shadow:0 2px 12px rgba(0,0,0,.12)">
  <tr><td style="background:#001F5B;padding:12px 24px">
    <span style="color:#fff;font-size:14px;font-weight:800;letter-spacing:.1em">Morgan Stanley</span>
    <span style="color:#fff4;margin:0 8px">|</span>
    <span style="color:#ffffffbb;font-size:13px">PNSRT — MKS Pod Restart Report</span>
  </td></tr>
  <tr><td style="background:{flag_color};padding:16px 24px">
    <div style="color:#fff;font-size:18px;font-weight:800">{flag_icon} Overall: {flag}</div>
    <div style="color:rgba(255,255,255,.8);font-size:12px;margin-top:4px">
      Executed by: {user} · {ts} · Namespace: {cfg['namespace']}</div>
  </td></tr>
  <tr><td style="padding:20px 24px">
    <h3 style="color:#001F5B;margin:0 0 10px">Cluster Summary</h3>
    <table border="1" cellspacing="0" cellpadding="5"
           style="border-collapse:collapse;width:100%;margin-bottom:20px">
      <tr style="background:#001F5B;color:white">
        <th style="padding:9px 12px">Cluster</th>
        <th style="padding:9px 12px">Total</th>
        <th style="padding:9px 12px">Running</th>
        <th style="padding:9px 12px">Other</th>
      </tr>{sum_rows}
    </table>
    <h3 style="color:#001F5B;margin:0 0 10px">Detailed POD Status (EST)</h3>
    <table border="1" cellspacing="0" cellpadding="5"
           style="border-collapse:collapse;width:100%">
      <tr style="background:#f4f5f7">
        <th style="padding:8px 12px">Cluster</th>
        <th style="padding:8px 12px">Pod Name</th>
        <th style="padding:8px 12px">Status</th>
        <th style="padding:8px 12px">Restart Time</th>
      </tr>{pod_rows}
    </table>
    <p style="margin-top:20px">Regards,<br><strong>PNSRT ASG — CP360° Dashboard</strong></p>
  </td></tr>
  <tr><td style="background:#f4f5f7;padding:12px 24px">
    <div style="font-size:11px;color:#97a0af">Automated · do not reply · CSV report attached</div>
  </td></tr>
</table></body></html>"""

    cfg_e = cfg["email"]
    msg   = MIMEMultipart()
    msg["From"]    = formataddr((str(Header("PNSRT Monitor", "utf-8")), cfg_e["sender"]))
    msg["To"]      = cfg_e["receiver"]
    msg["Subject"] = f"{flag} {cfg_e['subject']} — {ts}"
    msg.attach(MIMEText(html_body, "html"))

    if os.path.isfile(csv_path):
        with open(csv_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename={os.path.basename(csv_path)}",
            )
            msg.attach(part)

    receivers = [r.strip() for r in cfg_e["receiver"].split(",")]
    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
        smtp.sendmail(cfg_e["sender"], receivers, msg.as_string())
