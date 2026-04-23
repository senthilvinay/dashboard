#!/usr/bin/env python3
"""
=================================================================
app.py  —  CP360° Core Processing 360° — Main Flask Entry Point
=================================================================
Single server on port 5000 serving all blueprints:

  /                    → index.html (dashboard SPA)
  /api/config/*        → app branding, nav, themes
  /api/jaws/*          → JAWS widgets, STAT, dashboard sections
  /api/dashboard/*     → journal query, sections
  /api/mks/*           → MKS access + pod restart (SSE)
  /api/snow/*          → ServiceNow incidents/problems
  /api/teams/*         → Teams screenshot share
  /api/user/me         → current Kerberos user

Run:
  pip install -r requirements.txt
  python app.py
=================================================================
"""

import os
import logging
from flask import Flask, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

# Load .env file
load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("CP360")

# ── App ───────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ── Register blueprints ───────────────────────────────────────────────
from api.config_routes    import config_bp
from api.jaws_routes      import jaws_bp
from api.dashboard_routes import dashboard_bp
from api.mks_routes       import mks_bp
from api.snow_routes      import snow_bp
from api.teams_routes     import teams_bp
from api.user_routes      import user_bp

app.register_blueprint(config_bp)
app.register_blueprint(jaws_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(mks_bp)
app.register_blueprint(snow_bp)
app.register_blueprint(teams_bp)
app.register_blueprint(user_bp)

# ── Serve dashboard SPA ───────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("templates", "index.html")

@app.route("/static/snapshots/<filename>")
def serve_snapshot(filename):
    return send_from_directory("static/snapshots", filename)

# ── Health ────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    from datetime import datetime
    return {"status": "ok", "app": "CP360°", "ts": datetime.now().isoformat()}

# ── Main ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    log.info("=" * 55)
    log.info("  CP360° Core Processing 360° Dashboard")
    log.info(f"  http://localhost:{port}")
    log.info(f"  Mode: {'DEMO' if not os.getenv('SNOW_INSTANCE') else 'LIVE'}")
    log.info("=" * 55)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
