#!/usr/bin/env python3
"""
scripts/pnsrt_mks_api.py
=========================
OPTIONAL — Only use this if you want to run MKS as a
SEPARATE micro-service on its own port (5001).

In most cases you do NOT need this file.
The recommended approach is to use app.py (port 5000)
which already registers api/mks_routes.py.

Use this ONLY if:
  - You want MKS on a different server / port
  - You want to isolate MKS from the main dashboard process

Run:
    python scripts/pnsrt_mks_api.py
    # → MKS API at http://localhost:5001/api/mks/*
"""

import sys, os, logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

from api.mks_routes import mks_bp
app.register_blueprint(mks_bp)

@app.route("/")
def index():
    return {"service": "CP360° MKS API", "status": "running",
            "endpoints": ["/api/mks/health", "/api/mks/access",
                          "/api/mks/pods/discover", "/api/mks/restart/execute",
                          "/api/mks/stream/<job_id>", "/api/mks/status/<job_id>"]}

if __name__ == "__main__":
    port = int(os.getenv("MKS_PORT", "5001"))
    print(f"\n  CP360° MKS Standalone API → http://localhost:{port}/api/mks/\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
