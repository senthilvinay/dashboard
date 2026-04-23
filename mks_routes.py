"""
api/mks_routes.py  —  /api/mks/*
Consolidates MKS Access + MKS Restart into one blueprint.

Delegates heavy logic to:
  services/mks_service.py  →  SSH, unimatrix, kubectl

Endpoints:
  POST /api/mks/access              → Grant access (Step 1)
  POST /api/mks/pods/discover       → Discover pods (Step 3)
  POST /api/mks/restart/execute     → Execute restart (Step 4)
  GET  /api/mks/stream/<job_id>     → SSE live log stream
  GET  /api/mks/status/<job_id>     → Poll job status
  POST /api/mks/cancel/<job_id>     → Cancel running job
  GET  /api/mks/jobs                → Last 20 jobs
  GET  /api/mks/config              → Cluster config (safe)
  GET  /api/mks/health
"""
import os, json, queue, logging
from datetime import datetime
from flask import Blueprint, request, jsonify, Response, stream_with_context

log = logging.getLogger("CP360.MKS")
mks_bp = Blueprint("mks", __name__)

# Job store — imported from mks_service to keep state in one place
from services.mks_service import (
    get_config, new_job, get_job, list_jobs,
    start_access, start_discover, start_restart,
    get_queue, cancel_job,
)

@mks_bp.route("/api/mks/health")
def health():
    cfg = get_config()
    return jsonify({"status":"ok","namespace":cfg.get("namespace"),
                    "clusters":len(cfg.get("clusters",[])),"ts":datetime.now().isoformat()})

@mks_bp.route("/api/mks/config")
def mks_config():
    cfg = get_config()
    return jsonify({"clusters":cfg.get("clusters",[]),"namespace":cfg.get("namespace","wm-10168"),
                    "jump_server":cfg.get("jump_server",""),"tcm":str(cfg.get("tcm","")),
                    "wait_time":cfg.get("wait_time",600),"exclude":cfg.get("exclude_services_restart",[])})

@mks_bp.route("/api/mks/access", methods=["POST"])
def mks_access():
    b = request.get_json(force=True) or {}
    user = b.get("user","").strip(); pwd = b.get("password","")
    if not user: return jsonify({"error":"user required"}),400
    if not pwd:  return jsonify({"error":"password required"}),400
    cfg = get_config()
    jid = new_job({"user":user,"mode":"access",
                   "clusters":b.get("clusters",cfg.get("clusters",[])),
                   "tcm":str(b.get("tcm",cfg.get("tcm","")))})
    start_access(jid, user, pwd,
                 b.get("server",cfg.get("jump_server","")),
                 str(b.get("tcm",cfg.get("tcm",""))),
                 b.get("clusters",cfg.get("clusters",[])))
    return jsonify({"job_id":jid,"stream":f"/api/mks/stream/{jid}",
                    "status":f"/api/mks/status/{jid}"}),202

@mks_bp.route("/api/mks/pods/discover", methods=["POST"])
def pods_discover():
    b = request.get_json(force=True) or {}
    user = b.get("user","").strip(); pwd = b.get("password","")
    if not user: return jsonify({"error":"user required"}),400
    if not pwd:  return jsonify({"error":"password required"}),400
    cfg = get_config()
    jid = new_job({"user":user,"mode":"discover",
                   "clusters":b.get("clusters",cfg.get("clusters",[]))})
    start_discover(jid, user, pwd,
                   b.get("server",cfg.get("jump_server","")),
                   b.get("clusters",cfg.get("clusters",[])))
    return jsonify({"job_id":jid,"stream":f"/api/mks/stream/{jid}",
                    "status":f"/api/mks/status/{jid}"}),202

@mks_bp.route("/api/mks/restart/execute", methods=["POST"])
def restart_execute():
    b = request.get_json(force=True) or {}
    user = b.get("user","").strip(); pwd = b.get("password","")
    if not user: return jsonify({"error":"user required"}),400
    if not pwd:  return jsonify({"error":"password required"}),400
    cfg = get_config()
    jid = new_job({"user":user,"mode":b.get("mode","all"),
                   "clusters":b.get("clusters",cfg.get("clusters",[]))})
    start_restart(jid, user, pwd,
                  b.get("server",cfg.get("jump_server","")),
                  b.get("clusters",cfg.get("clusters",[])),
                  b.get("mode","all"),
                  int(b.get("wait_time",cfg.get("wait_time",600))),
                  b.get("sel_pods",{}),
                  cfg.get("exclude_services_restart",[]))
    return jsonify({"job_id":jid,"stream":f"/api/mks/stream/{jid}",
                    "status":f"/api/mks/status/{jid}"}),202

@mks_bp.route("/api/mks/stream/<jid>")
def stream(jid):
    q = get_queue(jid)
    if not q: return jsonify({"error":"job not found"}),404
    def gen():
        while True:
            try: item = q.get(timeout=60)
            except: yield "event: ping\ndata: {}\n\n"; continue
            if item is None:
                job = get_job(jid) or {}
                yield f"event: done\ndata: {json.dumps({'status':job.get('status'),'flag':job.get('flag'),'cluster_results':job.get('cluster_results',{})})}\n\n"
                break
            yield f"data: {item}\n\n"
    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no",
                             "Access-Control-Allow-Origin":"*"})

@mks_bp.route("/api/mks/status/<jid>")
def status(jid):
    job = get_job(jid)
    return (jsonify(job),200) if job else (jsonify({"error":"not found"}),404)

@mks_bp.route("/api/mks/cancel/<jid>", methods=["POST"])
def cancel(jid):
    cancel_job(jid)
    return jsonify({"message":"cancelled"})

@mks_bp.route("/api/mks/jobs")
def jobs():
    return jsonify(list_jobs())
