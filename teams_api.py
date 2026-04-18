#!/usr/bin/env python3
"""
=================================================================
teams_api.py  —  CP360° Teams Screenshot Share Backend
=================================================================
Receives screenshot (base64 PNG) from dashboard and posts
an Adaptive Card + image to a Microsoft Teams channel via
Incoming Webhook.

Why use a backend?
  - Browser cannot POST directly to Teams webhooks (CORS blocked)
  - Backend hosts the image temporarily so Teams can embed it
  - Backend can enrich the card with live metrics

Setup:
  1. In Teams: Apps → Incoming Webhook → Create → copy URL
  2. Set env vars below
  3. python teams_api.py   (port 5004)

Teams Incoming Webhook docs:
  https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook

Run:
  pip install flask flask-cors requests
  export TEAMS_WEBHOOK_OPS="https://outlook.office.com/webhook/..."
  python teams_api.py
=================================================================
"""

import os, json, uuid, base64, logging, tempfile, threading, time
from datetime import datetime
from pathlib   import Path

from flask      import Flask, request, jsonify, send_file
from flask_cors import CORS

try:
    import requests
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("TEAMS")

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ── Teams webhook URLs — set via env vars ─────────────────────────────
WEBHOOKS = {
    "ops":  os.getenv("TEAMS_WEBHOOK_OPS",  ""),   # #alerts / Operations
    "sre":  os.getenv("TEAMS_WEBHOOK_SRE",  ""),   # #incidents / SRE
    "mgmt": os.getenv("TEAMS_WEBHOOK_MGMT", ""),   # #dashboard-reports / Management
    "dev":  os.getenv("TEAMS_WEBHOOK_DEV",  ""),   # #general / Dev
}

# ── Temp image store ─────────────────────────────────────────────────
SNAP_DIR = Path(tempfile.gettempdir()) / "cp360_snapshots"
SNAP_DIR.mkdir(exist_ok=True)
SNAP_TTL = 3600  # seconds before cleanup (1hr)

# Your server's external URL — Teams needs this to download the image
# Set SERVER_URL env var, or fallback to localhost (only works on internal network)
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:5004")

# ── Severity / theme colors per page ─────────────────────────────────
PAGE_COLORS = {
    "dashboard":   "001F5B",
    "stat":        "0052CC",
    "jaws-stat":   "0052CC",
    "snow":        "00875A",
    "mks-restart": "FF8B00",
    "mks-ssh":     "7B5EA7",
    "jaws-yearend":"403294",
    "logintel":    "0078D4",
}

def _page_color(page_id: str) -> str:
    return PAGE_COLORS.get(page_id, "001F5B")


# =================================================================
#  IMAGE HOSTING
# =================================================================

def _save_snapshot(b64_str: str) -> str:
    """Save base64 PNG to temp file, return public URL."""
    snap_id  = str(uuid.uuid4())[:8]
    filename = f"snap_{snap_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    filepath = SNAP_DIR / filename

    img_bytes = base64.b64decode(b64_str)
    filepath.write_bytes(img_bytes)
    log.info(f"Snapshot saved: {filepath} ({len(img_bytes):,} bytes)")

    # Schedule cleanup
    def _cleanup():
        time.sleep(SNAP_TTL)
        try:
            filepath.unlink(missing_ok=True)
            log.info(f"Snapshot cleaned up: {filename}")
        except Exception:
            pass

    threading.Thread(target=_cleanup, daemon=True).start()

    return f"{SERVER_URL}/api/teams/snapshot/{filename}"


def _cleanup_old_snaps():
    """Remove snapshots older than TTL on startup."""
    now = time.time()
    for f in SNAP_DIR.glob("snap_*.png"):
        if now - f.stat().st_mtime > SNAP_TTL:
            f.unlink(missing_ok=True)


# =================================================================
#  TEAMS CARD BUILDER
# =================================================================

def _build_teams_card(data: dict, image_url: str | None) -> dict:
    """
    Build a MessageCard payload for Teams Incoming Webhook.
    Uses the legacy MessageCard format (works with all webhook types).
    """
    page_name  = data.get("page_name",  "Dashboard")
    page_id    = data.get("page_id",    "dashboard")
    message    = data.get("message",    "")
    metrics    = data.get("metrics",    "")
    shared_by  = data.get("shared_by",  "PNSRT Team")
    timestamp  = data.get("timestamp",  datetime.now().isoformat())
    incl_link  = data.get("incl_link",  True)
    color      = _page_color(page_id)

    # Format timestamp
    try:
        ts_dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        ts_str = ts_dt.strftime("%d %b %Y %H:%M:%S")
    except Exception:
        ts_str = timestamp[:19]

    # Build facts
    facts = [
        {"name": "📋 Page",      "value": page_name},
        {"name": "🕐 Captured",  "value": ts_str},
        {"name": "👤 Shared by", "value": shared_by},
        {"name": "🏢 Team",      "value": "Morgan Stanley · PNSRT ASG"},
    ]
    if metrics:
        facts.append({"name": "📊 Metrics", "value": metrics})

    # Actions
    actions = []
    if incl_link:
        actions.append({
            "@type": "OpenUri",
            "name":  "🔗 Open CP360° Dashboard",
            "targets": [{"os": "default", "uri": os.getenv("DASHBOARD_URL", "http://localhost:5000")}]
        })
    if image_url:
        actions.append({
            "@type": "OpenUri",
            "name":  "📥 Download Full Screenshot",
            "targets": [{"os": "default", "uri": image_url}]
        })

    # Sections
    sections = [{
        "activityTitle":    f"**{page_name}** — Dashboard Snapshot",
        "activitySubtitle": f"CP360° Core Processing 360° · Morgan Stanley",
        "activityText":     message or "Snapshot shared from CP360° Dashboard.",
        "facts":            facts,
        "markdown":         True,
    }]

    # Add image section if URL available
    if image_url:
        sections.append({
            "title":  "📸 Screenshot",
            "images": [{"image": image_url, "title": f"{page_name} screenshot"}],
        })

    # Adaptive card payload
    card = {
        "@type":         "MessageCard",
        "@context":      "https://schema.org/extensions",
        "summary":       f"CP360° {page_name} Snapshot — {ts_str}",
        "themeColor":    color,
        "title":         f"📊 CP360° — {page_name} Snapshot",
        "sections":      sections,
    }
    if actions:
        card["potentialAction"] = actions

    return card


def _send_to_teams(webhook_url: str, card: dict) -> tuple[bool, str]:
    """POST card to Teams webhook. Returns (success, message)."""
    if not REQUESTS_OK:
        return False, "requests library not installed"
    if not webhook_url:
        return False, "webhook URL not configured"

    try:
        resp = requests.post(
            webhook_url,
            json=card,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code in (200, 202):
            log.info(f"Teams: message sent ({resp.status_code})")
            return True, "sent"
        else:
            log.warning(f"Teams: unexpected status {resp.status_code} — {resp.text[:200]}")
            return False, f"Teams returned {resp.status_code}: {resp.text[:100]}"
    except requests.exceptions.ConnectionError:
        return False, "Cannot reach Teams webhook — check network/URL"
    except requests.exceptions.Timeout:
        return False, "Teams webhook timed out"
    except Exception as ex:
        log.exception("Teams send error")
        return False, str(ex)


# =================================================================
#  ROUTES
# =================================================================

@app.route("/api/teams/health")
def health():
    configured = {k: bool(v) for k, v in WEBHOOKS.items()}
    return jsonify({
        "status":      "ok",
        "webhooks":    configured,
        "any_configured": any(configured.values()),
        "server_url":  SERVER_URL,
        "requests_ok": REQUESTS_OK,
        "snap_dir":    str(SNAP_DIR),
        "ts":          datetime.now().isoformat(),
    })

# ─────────────────────────────────────────────────────────────────────
# GET /api/teams/snapshot/<filename>  — serve hosted screenshot image
# ─────────────────────────────────────────────────────────────────────
@app.route("/api/teams/snapshot/<filename>")
def serve_snapshot(filename: str):
    """Teams downloads the image from here to embed in card."""
    filepath = SNAP_DIR / filename
    if not filepath.exists() or not filepath.suffix == ".png":
        return jsonify({"error": "not found"}), 404
    return send_file(str(filepath), mimetype="image/png")

# ─────────────────────────────────────────────────────────────────────
# POST /api/teams/share-snapshot
# Body: {
#   image_b64, page_id, page_name, message, metrics,
#   channel, webhook_url, incl_link, timestamp, shared_by
# }
# ─────────────────────────────────────────────────────────────────────
@app.route("/api/teams/share-snapshot", methods=["POST"])
def share_snapshot():
    data = request.get_json(force=True) or {}

    b64_img     = data.get("image_b64", "")
    channel     = data.get("channel", "ops")
    custom_url  = data.get("webhook_url", "").strip()

    # Resolve webhook URL
    webhook_url = custom_url or WEBHOOKS.get(channel, "")
    if not webhook_url:
        log.warning(f"No webhook URL for channel '{channel}' — card not sent to Teams")
        # Still return success so user gets download fallback
        return jsonify({
            "status":  "no_webhook",
            "message": f"No webhook configured for '{channel}'. "
                       "Set TEAMS_WEBHOOK_{channel.upper()} env var.",
        })

    # Save screenshot to disk → get public URL
    image_url = None
    if b64_img:
        try:
            image_url = _save_snapshot(b64_img)
        except Exception as ex:
            log.warning(f"Snapshot save failed: {ex}")

    # Build and send Teams card
    card = _build_teams_card(data, image_url)
    ok, msg = _send_to_teams(webhook_url, card)

    if ok:
        return jsonify({
            "status":    "sent",
            "channel":   channel,
            "image_url": image_url,
            "message":   "Successfully shared to Teams",
        })
    else:
        # Send failed — let frontend fall back to download
        return jsonify({
            "status":  "failed",
            "message": msg,
        }), 502

# ─────────────────────────────────────────────────────────────────────
# POST /api/teams/configure-webhook
# Save webhook URLs (in memory — use DB/env for production)
# Body: { channel, url }
# ─────────────────────────────────────────────────────────────────────
@app.route("/api/teams/configure-webhook", methods=["POST"])
def configure_webhook():
    data    = request.get_json(force=True) or {}
    channel = data.get("channel", "").lower()
    url     = data.get("url", "").strip()

    if channel not in WEBHOOKS:
        return jsonify({"error": f"Unknown channel: {channel}"}), 400
    if not url.startswith("https://"):
        return jsonify({"error": "URL must start with https://"}), 400

    WEBHOOKS[channel] = url
    log.info(f"Webhook configured for channel: {channel}")
    return jsonify({"status": "ok", "channel": channel})

# ─────────────────────────────────────────────────────────────────────
# POST /api/teams/send-alert
# Send a rich alert card (no image) — for automated notifications
# Body: { title, message, severity, page, metrics[], actions[] }
# ─────────────────────────────────────────────────────────────────────
@app.route("/api/teams/send-alert", methods=["POST"])
def send_alert():
    data      = request.get_json(force=True) or {}
    title     = data.get("title", "CP360° Alert")
    message   = data.get("message", "")
    severity  = data.get("severity", "info")   # info | warning | critical
    page      = data.get("page", "dashboard")
    metrics   = data.get("metrics", [])         # list of {name, value}
    channel   = data.get("channel", "ops")

    webhook_url = WEBHOOKS.get(channel, "")
    if not webhook_url:
        return jsonify({"status": "no_webhook"}), 200

    sev_colors = {"info": "0078D4", "warning": "FF8B00", "critical": "DE350B"}
    sev_icons  = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
    color = sev_colors.get(severity, "001F5B")
    icon  = sev_icons.get(severity, "📊")

    facts = [{"name": m["name"], "value": str(m["value"])} for m in metrics]
    facts.append({"name": "🕐 Time",    "value": datetime.now().strftime("%d %b %Y %H:%M:%S")})
    facts.append({"name": "📋 Page",    "value": page.upper()})
    facts.append({"name": "🏢 Source",  "value": "CP360° · Morgan Stanley PNSRT"})

    card = {
        "@type":      "MessageCard",
        "@context":   "https://schema.org/extensions",
        "summary":    f"{icon} {title}",
        "themeColor": color,
        "title":      f"{icon} {title}",
        "text":       message,
        "sections":   [{"facts": facts, "markdown": True}],
        "potentialAction": [{
            "@type":  "OpenUri",
            "name":   "🔗 Open Dashboard",
            "targets":[{"os":"default","uri": os.getenv("DASHBOARD_URL","http://localhost:5000")}]
        }]
    }

    ok, msg = _send_to_teams(webhook_url, card)
    return jsonify({"status": "sent" if ok else "failed", "message": msg})


# =================================================================
#  MAIN
# =================================================================
if __name__ == "__main__":
    _cleanup_old_snaps()
    port = int(os.getenv("TEAMS_PORT", "5004"))

    log.info("=" * 58)
    log.info("  CP360° Teams Share API")
    log.info(f"  Port:        {port}")
    log.info(f"  Server URL:  {SERVER_URL}")
    log.info(f"  Snap dir:    {SNAP_DIR}")
    log.info(f"  Webhooks configured:")
    for ch, url in WEBHOOKS.items():
        log.info(f"    {ch:8s}: {'✅ ' + url[:40] + '...' if url else '❌ Not set'}")
    log.info("")
    log.info("  Set webhook URLs:")
    log.info("    export TEAMS_WEBHOOK_OPS='https://outlook.office.com/webhook/...'")
    log.info("    export TEAMS_WEBHOOK_SRE='https://outlook.office.com/webhook/...'")
    log.info("    export TEAMS_WEBHOOK_MGMT='https://...'")
    log.info("    export TEAMS_WEBHOOK_DEV='https://...'")
    log.info("=" * 58)

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
