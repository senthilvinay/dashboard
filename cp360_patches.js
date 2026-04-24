/*
=================================================================
CP360° DASHBOARD — EXACT PATCH SNIPPETS
=================================================================
These are the ONLY sections you need to add/change in your
existing index.html JS block.

Find each "FIND THIS:" section in your file and replace it
with the corresponding "REPLACE WITH:" section.
=================================================================
*/


/* ═══════════════════════════════════════════════════════════════
   PATCH 1 — Sample Data Population
   Problem : KPI strip shows "—" and ticker shows "Initialising"
             because dashTableData is empty on page load.
   Fix     : Call updateKpiHero() and buildDynamicTicker()
             AFTER _buildDashboardUI() runs with sample data.
   Where   : Inside async function loadDashboard()
═══════════════════════════════════════════════════════════════ */

// FIND THIS:
async function loadDashboard() {
  try {
    const res = await fetch('/api/dashboard/sections');
    if (!res.ok) throw new Error('');
    const data = await res.json();
    dashSections = data.sections || [];
  } catch {
    dashSections = SAMPLE_SECTIONS;
    document.getElementById('sampleBadge').classList.add('visible');
    notify('Dashboard using sample data','warn');
  }
  _buildDashboardUI(dashSections);
}

// REPLACE WITH:
async function loadDashboard() {
  try {
    const res = await fetch('/api/dashboard/sections');
    if (!res.ok) throw new Error('');
    const data = await res.json();
    dashSections = data.sections || [];
  } catch {
    dashSections = SAMPLE_SECTIONS;
    // Pre-load SAMPLE rows into dashTableData so KPIs and ticker
    // have real numbers immediately without waiting for API
    SAMPLE_SECTIONS.forEach(function(sec) {
      (sec.tables || []).forEach(function(tbl) {
        var key = sec.id + '/' + tbl.id;
        dashTableData[key] = {
          allRows:      tbl.rows || [],
          filteredRows: tbl.rows || [],
          sortCol: null, sortDir: 'asc', page: 0
        };
      });
    });
    var badge = document.getElementById('sampleBadge');
    if (badge) badge.classList.add('visible');
    notify('Dashboard using sample data — Flask not connected','warn');
  }
  _buildDashboardUI(dashSections);
  // Update KPI strip and flow pipeline now that data is loaded
  if (typeof updateKpiHero === 'function') updateKpiHero();
}


/* ═══════════════════════════════════════════════════════════════
   PATCH 2 — Status Check Toggle (pill switch)
   Problem : "Status Check Only — no Restart" looks like static
             text, not a clickable toggle. No visual ON/OFF state.
   Fix     : Replace the old checkbox div with a proper pill switch.
   Where   : Inside page-mks-restart HTML, Step 4 configuration
═══════════════════════════════════════════════════════════════ */

// FIND THIS (in HTML, not JS):
<!-- Status Check — real checkbox -->
<div style="display:flex;align-items:center;gap:12px;padding:10px 14px;
     background:var(--bg);border:1px solid var(--border);border-radius:10px;
     cursor:pointer;margin-top:2px;" onclick="rstToggleStatusCheck(this)">
  <div id="rstStatusCheckbox" style="width:20px;height:20px;border-radius:5px;
       border:2px solid var(--border);background:var(--bg);display:flex;
       align-items:center;justify-content:center;flex-shrink:0;
       transition:all .2s;font-size:.75rem;"></div>
  <div>
    <div style="font-weight:800;font-size:.8rem;">👁️ Status Check Only — no restart</div>
    <div style="font-size:.67rem;color:var(--txt-muted);">Capture current pod status and generate report without restarting</div>
  </div>
</div>

// REPLACE WITH:
<!-- Status Check — pill toggle switch -->
<div style="display:flex;align-items:center;gap:12px;padding:10px 14px;
     background:var(--bg);border:1px solid var(--border);border-radius:10px;
     cursor:pointer;margin-top:2px;transition:all .2s;"
     id="rstStatusCheckRow"
     onclick="rstToggleStatusCheck(this)">
  <!-- Toggle pill -->
  <div style="position:relative;width:42px;height:24px;flex-shrink:0;">
    <div id="rstStatusCheckbox"
         style="position:absolute;inset:0;border-radius:12px;
                background:var(--border);transition:all .25s;"></div>
    <div id="rstStatusCheckThumb"
         style="position:absolute;top:3px;left:3px;width:16px;height:16px;
                border-radius:50%;background:#fff;
                box-shadow:0 1px 4px rgba(0,0,0,.3);transition:all .25s;"></div>
  </div>
  <div style="flex:1;">
    <div style="font-weight:800;font-size:.8rem;display:flex;align-items:center;gap:8px;">
      👁️ Status Check Only — no restart
      <span id="rstStatusCheckLabel"
            style="font-size:.62rem;font-family:'Space Mono',monospace;
                   padding:2px 8px;border-radius:8px;font-weight:700;
                   background:rgba(160,180,200,.15);color:var(--txt-muted);">OFF</span>
    </div>
    <div style="font-size:.67rem;color:var(--txt-muted);margin-top:2px;">
      When ON — captures pod status &amp; report only. No kubectl restart triggered.
    </div>
  </div>
</div>


/* ═══════════════════════════════════════════════════════════════
   PATCH 3 — rstToggleStatusCheck() with pill animation
   Problem : Old function used innerHTML='✓' on a box div.
             Doesn't animate. Doesn't show ON/OFF label.
   Fix     : Animate the pill thumb, update label, grey out mode buttons.
   Where   : In JS, replace the existing rstToggleStatusCheck function
═══════════════════════════════════════════════════════════════ */

// FIND THIS:
function rstToggleStatusCheck(rowEl) {
  _rstState.statusCheckOnly = !_rstState.statusCheckOnly;
  var cb  = document.getElementById('rstStatusCheckbox');
  var btn = document.getElementById('rstExecBtn');

  if (_rstState.statusCheckOnly) {
    if (cb) { cb.style.background='var(--accent)'; cb.style.borderColor='var(--accent)'; cb.innerHTML='✓'; }
    if (rowEl) { rowEl.style.borderColor='var(--accent)'; rowEl.style.background='rgba(0,229,255,.06)'; }
    ['all','selected'].forEach(function(m) {
      var b = document.getElementById('rstMode' + m.charAt(0).toUpperCase() + m.slice(1));
      if (b) { b.style.opacity='0.4'; b.style.pointerEvents='none'; }
    });
    if (btn) btn.innerHTML = '<i class="fas fa-eye"></i> Run Status Check';
    var wRow = document.getElementById('rstWaitRow');
    if (wRow) wRow.style.display = 'none';
  } else {
    if (cb) { cb.style.background='var(--bg)'; cb.style.borderColor='var(--border)'; cb.innerHTML=''; }
    if (rowEl) { rowEl.style.borderColor='var(--border)'; rowEl.style.background='var(--bg)'; }
    ['all','selected'].forEach(function(m) {
      var b = document.getElementById('rstMode' + m.charAt(0).toUpperCase() + m.slice(1));
      if (b) { b.style.opacity=''; b.style.pointerEvents=''; }
    });
    if (btn) btn.innerHTML = '<i class="fas fa-play"></i> Execute Restart';
    var wRow = document.getElementById('rstWaitRow');
    if (wRow) wRow.style.display = '';
  }
  rstBuildSummary();
}

// REPLACE WITH:
function rstToggleStatusCheck(rowEl) {
  _rstState.statusCheckOnly = !_rstState.statusCheckOnly;
  var isOn  = _rstState.statusCheckOnly;
  var pill  = document.getElementById('rstStatusCheckbox');
  var thumb = document.getElementById('rstStatusCheckThumb');
  var label = document.getElementById('rstStatusCheckLabel');
  var row   = document.getElementById('rstStatusCheckRow');
  var btn   = document.getElementById('rstExecBtn');
  var wRow  = document.getElementById('rstWaitRow');

  if (isOn) {
    // ── Toggle ON: Status check only, NO restart ─────────────────
    if (pill)  { pill.style.background  = 'var(--accent)'; }
    if (thumb) { thumb.style.left       = '22px'; }
    if (label) {
      label.textContent        = 'ON';
      label.style.background   = 'rgba(0,229,255,.15)';
      label.style.color        = 'var(--accent)';
    }
    if (row) {
      row.style.borderColor = 'var(--accent)';
      row.style.background  = 'rgba(0,229,255,.05)';
    }
    // Grey out restart mode buttons — not applicable
    ['all','selected'].forEach(function(m) {
      var b = document.getElementById('rstMode'+m[0].toUpperCase()+m.slice(1));
      if (b) { b.style.opacity='0.35'; b.style.pointerEvents='none'; }
    });
    if (btn)  btn.innerHTML      = '<i class="fas fa-eye"></i> Run Status Check Only';
    if (wRow) wRow.style.display = 'none';   // hide wait-time row
    if (typeof notify==='function')
      notify('Status Check ON — no kubectl restart will run','info');

  } else {
    // ── Toggle OFF: Normal restart mode ──────────────────────────
    if (pill)  { pill.style.background  = 'var(--border)'; }
    if (thumb) { thumb.style.left       = '3px'; }
    if (label) {
      label.textContent        = 'OFF';
      label.style.background   = 'rgba(160,180,200,.15)';
      label.style.color        = 'var(--txt-muted)';
    }
    if (row) {
      row.style.borderColor = 'var(--border)';
      row.style.background  = 'var(--bg)';
    }
    // Re-enable restart mode buttons
    ['all','selected'].forEach(function(m) {
      var b = document.getElementById('rstMode'+m[0].toUpperCase()+m.slice(1));
      if (b) { b.style.opacity=''; b.style.pointerEvents=''; }
    });
    if (btn)  btn.innerHTML      = '<i class="fas fa-play"></i> Execute Restart';
    if (wRow) wRow.style.display = '';       // show wait-time row
    if (typeof notify==='function')
      notify('Restart mode ON — kubectl restart will execute','info');
  }
  rstBuildSummary();
}


/* ═══════════════════════════════════════════════════════════════
   PATCH 4 — _rstStreamLogs (MISSING function — must add)
   Problem : "ReferenceError: _rstStreamLogs is not defined"
             The SSE consumer function was never in your file.
   Fix     : Add this function before rstGrantAccess()
   Where   : In JS, paste before "function rstGrantAccess()"
═══════════════════════════════════════════════════════════════ */

// ADD THIS (paste before function rstGrantAccess):
function _rstStreamLogs(streamUrl, termEl, onDone) {
  if (!termEl) termEl = document.getElementById('rst-terminal');
  if (!termEl) {
    var content = document.querySelector('.page.active') || document.body;
    termEl = document.createElement('div');
    termEl.id = 'rst-terminal-dynamic';
    content.appendChild(termEl);
  }
  termEl.innerHTML = '';
  termEl.style.cssText = [
    'display:block','background:#0a0e1a',
    'border:1px solid rgba(0,229,255,.2)','border-radius:10px',
    'padding:12px 14px','font-family:"Space Mono",monospace',
    'font-size:.7rem','line-height:1.7','color:#a0b4c8',
    'max-height:320px','overflow-y:auto','margin-top:12px',
  ].join(';');

  var connectLine = document.createElement('div');
  connectLine.style.color = '#00e5ff';
  connectLine.textContent = '[Connecting to SSH stream…]';
  termEl.appendChild(connectLine);

  try {
    var es = new EventSource(streamUrl);

    es.onmessage = function(evt) {
      try {
        var d = JSON.parse(evt.data);
        var line = document.createElement('div');
        line.style.color = d.col || '#a0b4c8';
        line.innerHTML =
          '<span style="color:rgba(160,180,200,.4);font-size:.65rem;margin-right:8px;">'
          + (d.ts || '') + '</span>'
          + (d.msg || '').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        termEl.appendChild(line);
        termEl.scrollTop = termEl.scrollHeight;
      } catch(_) {}
    };

    es.addEventListener('done', function(evt) {
      es.close();
      try   { if (onDone) onDone(JSON.parse(evt.data)); }
      catch (_) { if (onDone) onDone(null); }
    });

    es.addEventListener('ping', function() {});

    es.onerror = function() {
      es.close();
      var errLine = document.createElement('div');
      errLine.style.color = '#ff5252';
      errLine.textContent = '[Stream disconnected]';
      termEl.appendChild(errLine);
      if (onDone) onDone(null);
    };

    // 15-minute safety timeout
    setTimeout(function() {
      if (es.readyState !== EventSource.CLOSED) {
        es.close(); if (onDone) onDone(null);
      }
    }, 15 * 60 * 1000);

  } catch(ex) {
    if (onDone) onDone(null);
  }
}


/* ═══════════════════════════════════════════════════════════════
   PATCH 5 — mksLoadConfig() — fetch config from Flask
   Problem : MKS page uses hardcoded cluster/namespace values.
             Changes to pnsrt_mks_restart_config.json have no effect.
   Fix     : Fetch /api/mks/config on page open and merge into state.
   Where   : In JS, add this function (paste near other MKS functions)
             Then call it from initMksSsh() and initRstPage()
═══════════════════════════════════════════════════════════════ */

// ADD THIS function:
var _mksRemoteCfg = null;

async function mksLoadConfig() {
  try {
    var res = await fetch('/api/mks/config');
    if (!res.ok) throw new Error('no server');
    var cfg = await res.json();
    _mksRemoteCfg = cfg;

    // Merge clusters into MKS_APP_CONFIGS
    if (cfg.clusters && cfg.clusters.length) {
      Object.keys(MKS_APP_CONFIGS).forEach(function(key) {
        MKS_APP_CONFIGS[key].clusters    = cfg.clusters;
        MKS_APP_CONFIGS[key].namespace   = cfg.namespace   || MKS_APP_CONFIGS[key].namespace;
        MKS_APP_CONFIGS[key].jump_server = cfg.jump_server || MKS_APP_CONFIGS[key].jump_server;
        if (cfg.tcm) MKS_APP_CONFIGS[key].tcm = String(cfg.tcm);
      });
    }

    // Rebuild RST_SERVERS from live cluster list
    if (cfg.clusters) {
      RST_SERVERS = cfg.clusters.map(function(c) {
        var short  = c.split('.')[0].toUpperCase();
        var region = c.includes('.na.') ? 'NA · New York'
                   : c.includes('.yn.') ? 'YN · New Jersey'
                   : c.split('.')[1] ? c.split('.')[1].toUpperCase() : 'Region';
        return { id:c, short:short, region:region,
                 col: c.includes('.na.') ? '#00e5ff' : '#a78bfa' };
      });
      _rstState.selServers = new Set(cfg.clusters);
    }
    if (cfg.wait_time) _rstState.waitTime  = cfg.wait_time;
    if (cfg.namespace)  _rstState.namespace = cfg.namespace;
    if (cfg.tcm)        _rstState.tcm       = String(cfg.tcm);

  } catch(ex) {
    // Flask not running — use defaults already set in MKS_APP_CONFIGS
    console.log('[MKS] Using default config (server offline)');
  }
}

// FIND THIS (in initMksSsh):
function initMksSsh(appHint) {

// REPLACE WITH (adds config fetch before init):
function initMksSsh(appHint) {
  mksLoadConfig();   // ← fetch live config from Flask

// FIND THIS (initRstPage):
function initRstPage() { rstTab(0); rstBuildServerGrid(); }

// REPLACE WITH:
function initRstPage() {
  mksLoadConfig().then(function() {
    rstTab(0);
    rstBuildServerGrid();   // now built from live cluster list
  });
}
