#!/usr/bin/env python3
"""
scripts/pnsrt_mks_restart.py
==============================
Standalone MKS restart script — runs via cron or manually.
Shares ALL logic with the Flask API via services/mks_service.py.

Usage:
    python scripts/pnsrt_mks_restart.py --user svinayag --mode all
    python scripts/pnsrt_mks_restart.py --user svinayag --mode selected --pods app41=pod1,pod2

Environment vars (or pass as args):
    MKS_USER, MKS_PASSWORD
"""

import sys, os, argparse, getpass, logging, time, json, queue

# ── ensure project root is on path ──────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.mks_service import (
    get_config, new_job, get_job, get_queue,
    start_access, start_discover, start_restart,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("MKS-Script")


def stream_logs(jid: str):
    """Print SSE queue messages to console until job finishes."""
    q = get_queue(jid)
    if not q:
        return
    while True:
        try:
            item = q.get(timeout=120)
        except Exception:
            print("  [timeout waiting for log]")
            break
        if item is None:
            break
        data = json.loads(item)
        print(f"  {data['ts']}  {data['msg']}")


def wait_done(jid: str, timeout: int = 900):
    """Poll until job is no longer RUNNING."""
    elapsed = 0
    while elapsed < timeout:
        job = get_job(jid)
        if job and job["status"] != "RUNNING":
            return job
        time.sleep(2)
        elapsed += 2
    return get_job(jid)


def main():
    cfg = get_config()

    p = argparse.ArgumentParser(description="PNSRT MKS Pod Restart")
    p.add_argument("--user",      default=os.getenv("MKS_USER",  ""), help="SSH username")
    p.add_argument("--password",  default=os.getenv("MKS_PASSWORD", ""), help="SSH password (or set MKS_PASSWORD env var)")
    p.add_argument("--server",    default=cfg["jump_server"],           help="Jump server hostname")
    p.add_argument("--mode",      default="all", choices=["all","selected","status"], help="Restart mode")
    p.add_argument("--clusters",  default=",".join(cfg["clusters"]),    help="Comma-separated cluster list")
    p.add_argument("--wait-time", default=cfg["wait_time"], type=int,   help="Seconds to wait after restart")
    p.add_argument("--tcm",       default=str(cfg["tcm"]),              help="TCM ticket number")
    p.add_argument("--pods",      default="",                           help="For selected mode: cluster=pod1,pod2;cluster2=pod3")
    p.add_argument("--skip-access", action="store_true",                help="Skip Step 1 (access already granted)")
    args = p.parse_args()

    # Prompt for password if not provided
    user = args.user or input("Username: ").strip()
    pwd  = args.password or getpass.getpass(f"Password for {user}@{args.server}: ")

    clusters  = [c.strip() for c in args.clusters.split(",") if c.strip()]
    exclude   = cfg.get("exclude_services_restart", [])

    # Parse sel_pods for selected mode
    sel_pods = {}
    if args.pods:
        for part in args.pods.split(";"):
            if "=" in part:
                cluster, pods = part.split("=", 1)
                sel_pods[cluster.strip()] = [p.strip() for p in pods.split(",")]

    print()
    print("=" * 60)
    print("  PNSRT MKS Pod Restart")
    print(f"  User      : {user}")
    print(f"  Server    : {args.server}")
    print(f"  Clusters  : {len(clusters)}")
    print(f"  Mode      : {args.mode}")
    print(f"  Namespace : {cfg['namespace']}")
    print(f"  Exclude   : {exclude}")
    print("=" * 60)
    print()

    # ── Step 1: Grant Access ─────────────────────────────────────────
    if not args.skip_access:
        print("── STEP 1: Granting MKS Access ──")
        jid = new_job({"user": user, "mode": "access", "clusters": clusters, "tcm": args.tcm})
        start_access(jid, user, pwd, args.server, args.tcm, clusters)
        stream_logs(jid)
        job = wait_done(jid)
        if job["status"] != "DONE":
            print(f"\n❌ Access step failed: {job.get('error')}")
            sys.exit(1)
        print(f"\n✅ Access granted\n")

    # ── Step 2: Restart ──────────────────────────────────────────────
    print(f"── STEP 2: Executing Restart ({args.mode}) ──")
    jid = new_job({"user": user, "mode": args.mode, "clusters": clusters})
    start_restart(
        jid, user, pwd, args.server,
        clusters, args.mode, args.wait_time,
        sel_pods, exclude,
    )
    stream_logs(jid)
    job = wait_done(jid, timeout=args.wait_time + 300)

    print()
    print("=" * 60)
    flag = job.get("flag", "UNKNOWN") if job else "UNKNOWN"
    print(f"  Final Status : {'✅ GREEN' if flag == 'GREEN' else '🔴 RED' if flag == 'RED' else flag}")
    if job and job.get("csv_file"):
        print(f"  CSV Report   : {job['csv_file']}")
    print("=" * 60)

    sys.exit(0 if flag == "GREEN" else 1)


if __name__ == "__main__":
    main()
