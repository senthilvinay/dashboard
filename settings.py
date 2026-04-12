# =================================================================
# config/settings.py  —  MMNG 30-Min Health Check Monitor
# All paths, credentials and thresholds live here.
# The main script reads ONLY from this file — nothing hard-coded there.
# =================================================================

import os

# -----------------------------------------------------------------
# ── ENVIRONMENT TAG
# -----------------------------------------------------------------
ENVIRONMENT    = os.environ.get("ENV", "PROD")     # PROD | QA | DEV
SCRIPT_VERSION = "2.0.0"

# -----------------------------------------------------------------
# ── DATABASE  (JAWS_SEC_ORCH)
#    Server logical name must match the [section] in freetds.conf
# -----------------------------------------------------------------
DB_SERVER  = os.environ.get("DB_SERVER", "NYP_WM_JAWS_SEC_ORCH")
DB_NAME    = os.environ.get("DB_NAME",   "JAWS_SEC_ORCH")

# Connection string template — exact pattern from pydbc_conn.py (Image 1)
DB_CONN_TEMPLATE = "DRIVER=FreeTDS;SERVERNAME={server};DATABASE={dbname}"
QUERY_TIMEOUT    = int(os.environ.get("QUERY_TIMEOUT", "30"))

# -----------------------------------------------------------------
# ── UNIXODBC / FREETDS PATHS  (from Image 1 + Image 2)
# -----------------------------------------------------------------
UNIXODBC_LIB = os.environ.get(
    "UNIXODBC_LIB",
    "/ms/dist/fsf/PROJ/unixodbc/2.3.7-0/exec/lib/libodbc.so.2"
)
FREETDS_CONF = os.environ.get(
    "FREETDSCONF",
    "/ms/dist/syb/PROJ/config/incr/dba/files/mssql/freetds.conf"
)
# odbcinst.ini driver paths (from `cat odbcinst.ini` in Image 2)
FREETDS_DRIVER_SO = "/ms/dist/fsf/PROJ/freetds/1.1.12/lib/libtdsodbc.so"
FREETDS_SETUP_SO  = "/ms/dist/fsf/PROJ/freetds/1.1.12/lib/libtdsS.so"

# odbcinst.ini / odbc.ini written by script at runtime if missing
ODBCINST_INI_PATH = os.environ.get(
    "ODBCINSTINI",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "db", "odbcinst.ini")
)
ODBCINI_PATH = os.environ.get(
    "ODBCINI",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "db", "odbc.ini")
)

# -----------------------------------------------------------------
# ── SQL FILE PATH
# -----------------------------------------------------------------
SQL_PATH = os.path.join(os.path.dirname(__file__), "mmng_monitor.sql")

# -----------------------------------------------------------------
# ── ALERT THRESHOLDS
# -----------------------------------------------------------------
ALERT_QUEUE_THRESHOLD     = int(os.environ.get("ALERT_QUEUE_THRESHOLD",     "0"))
ALERT_PENDING_MINUTES     = int(os.environ.get("ALERT_PENDING_MINUTES",     "15"))
ALERT_WAREHOUSE_THRESHOLD = int(os.environ.get("ALERT_WAREHOUSE_THRESHOLD", "1000"))
ALERT_PROC_TIME_THRESHOLD = int(os.environ.get("ALERT_PROC_TIME_THRESHOLD", "300"))

# -----------------------------------------------------------------
# ── MICROSOFT TEAMS  (internal istasgWS mechanism from Image 3)
# -----------------------------------------------------------------
TEAMS_ENABLED = True

# Step 1 — Kerberos login to get session cookie
TEAMS_KRB_LOGIN_URL = os.environ.get(
    "TEAMS_KRB_LOGIN_URL",
    "http://krb2sm-v2-prod.ms.com/login"
)
# Kerberos principal — blank = use current kinit ticket
TEAMS_KRB_PRINCIPAL = os.environ.get("TEAMS_KRB_PRINCIPAL", "")

# Step 2 — Internal Teams posting service
TEAMS_POST_URL = os.environ.get(
    "TEAMS_POST_URL",
    "http://istasg-2.webfarm.ms.com/istasgWS/Teams2.py"
)
# Channel name that appears in ?channel= param
TEAMS_CHANNEL = os.environ.get("TEAMS_CHANNEL", "YOUR_TEAMS_CHANNEL_NAME")

# GRN identifier for User-Agent header (from Image 3 line 403)
TEAMS_GRN = os.environ.get("TEAMS_GRN", "grn://ms/Apica")

# MS internal CA cert for HTTPS requests
MS_CA_CERT_BUNDLE = os.environ.get(
    "CURL_CA_BUNDLE",
    "/etc/pki/ms/certs/ms-ca-chain.crt"
)
# istasgWS message chunk size (msg param is sliced to 1000 in Image 3)
TEAMS_MSG_CHUNK_SIZE = int(os.environ.get("TEAMS_MSG_CHUNK_SIZE", "1000"))
TEAMS_HTTP_TIMEOUT   = int(os.environ.get("TEAMS_HTTP_TIMEOUT",   "30"))

# -----------------------------------------------------------------
# ── EMAIL
# -----------------------------------------------------------------
EMAIL_ENABLED      = True
SMTP_HOST          = os.environ.get("SMTP_HOST",   "msa-hub.ms.com")
SMTP_PORT          = int(os.environ.get("SMTP_PORT", "25"))
EMAIL_FROM         = os.environ.get("EMAIL_FROM",  "jaws-monitor@morganstanley.com")
EMAIL_TO           = [e.strip() for e in os.environ.get(
                          "EMAIL_TO", "your-team-dl@morganstanley.com").split(",")]
EMAIL_CC           = [e.strip() for e in os.environ.get("EMAIL_CC", "").split(",") if e.strip()]
EMAIL_SUBJECT_GOOD = "[JAWS MMNG] ✅ GOOD — 30-Min Health Check  |  {env}  |  {ts}"
EMAIL_SUBJECT_BAD  = "[JAWS MMNG] 🚨 BAD/RED ALERT — 30-Min Health Check  |  {env}  |  {ts}"
EMAIL_SEND_ON_GOOD = True     # False = only send email when BAD

# -----------------------------------------------------------------
# ── LOGGING
# -----------------------------------------------------------------
LOG_FILE  = os.environ.get("LOG_FILE",  "mmng_monitor.log")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# -----------------------------------------------------------------
# ── DASHBOARD LINK  (Teams card + email footer)
# -----------------------------------------------------------------
DASHBOARD_URL = os.environ.get(
    "DASHBOARD_URL", "http://jaws-dashboard.morganstanley.com"
)
