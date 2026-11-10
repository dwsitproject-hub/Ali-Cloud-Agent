/* Cloud Agent Monitoring - shared frontend runtime.

   No build step and no dependencies on purpose: the pages are plain files under
   nginx, and nothing here is fetched from a CDN this network may not reach.

   What lives here is everything two pages would otherwise each write their own
   copy of - the API client, the top bar, the theme, and above all analyse():
   the single place that decides what counts as a problem. The Overview badge,
   the triage queue, the fleet map and the inventory all read that one answer,
   so they cannot disagree about whether something is wrong. */
(function () {
  'use strict';

  const API = (window.APP_CONFIG && window.APP_CONFIG.apiBase) || '';
  let CSRF = null;

  const $ = (s, r) => (r || document).querySelector(s);

  function esc(s) {
    return (s == null ? '' : String(s)).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // ---- API ----------------------------------------------------------------
  // Every call goes to the backend origin and carries the session cookie.
  function api(path, opts) { opts = opts || {}; opts.credentials = 'include'; return fetch(API + path, opts); }
  async function loadCsrf() {
    try { const r = await api('/api/csrf'); if (r.ok) CSRF = (await r.json()).csrf_token; } catch (e) { /* retried on next write */ }
  }
  function postHeaders(json) {
    const h = {};
    if (json) h['Content-Type'] = 'application/json';
    if (CSRF) h['X-CSRFToken'] = CSRF;
    return h;
  }
  function authGuard(r) { if (r.status === 401) { window.location = 'login.html'; return true; } return false; }

  let toastTimer = null;
  function toast(msg) {
    let t = $('#toast');
    if (!t) { t = document.createElement('div'); t.id = 'toast'; t.setAttribute('role', 'status'); document.body.appendChild(t); }
    t.textContent = msg; t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 3500);
  }

  // ---- time and numbers ---------------------------------------------------
  const pad = n => String(n).padStart(2, '0');
  // Scan stamps are UTC, and some arrive without a zone suffix. Read as-is they
  // would be taken for local time and every "next scan in" would be hours off.
  function parseUtc(iso) {
    if (!iso) return null;
    let s = String(iso);
    if (!/([zZ]|[+-]\d\d:?\d\d)$/.test(s)) s += 'Z';
    const d = new Date(s);
    return isNaN(d) ? null : d;
  }
  function utcClock(d, secs) {
    if (!d) return '-';
    return pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + (secs === false ? '' : ':' + pad(d.getUTCSeconds()));
  }
  function utcStamp(iso) {
    const d = parseUtc(iso);
    if (!d) return '-';
    return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) + ' ' + utcClock(d);
  }
  function span(ms) {
    const m = Math.max(1, Math.round(ms / 60000));
    if (m < 90) return m + ' min';
    const h = Math.floor(m / 60);
    if (h < 48) return h + ' h' + (m % 60 ? ' ' + (m % 60) + ' min' : '');
    return Math.floor(h / 24) + ' d';
  }
  function num(v) { return v == null ? '-' : String(Math.round(v * 10) / 10); }
  function ctrNum(v) { return v == null ? '-' : v.toFixed(v < 10 ? 1 : 0); }
  function pct(v) { return v == null ? '-' : ctrNum(v) + '%'; }
  function fmtMem(mb) { return mb >= 1024 ? (mb / 1024).toFixed(mb % 1024 ? 1 : 0) + ' GB' : mb + ' MB'; }
  // Summed across every attached disk, so this reaches TB on data-heavy boxes.
  function fmtDisk(gb) { return gb >= 1024 ? (gb / 1024).toFixed(1) + ' TB' : gb + ' GB'; }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }

  // ---- theme --------------------------------------------------------------
  // The <head> of each page sets data-theme before first paint; this only flips it.
  function theme() { return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark'; }
  function setTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('cam-theme', t); } catch (e) { /* private mode: lasts for this page only */ }
    paintThemeBtn();
  }
  const THEME_ICON = '<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"></circle><path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor" stroke="none"></path></svg>';
  function paintThemeBtn() {
    const b = $('#themeBtn');
    if (b) b.innerHTML = THEME_ICON + (theme() === 'light' ? 'Dark theme' : 'Light theme');
  }

  // ---- roles --------------------------------------------------------------
  const ICON = {
    backend: 'M4 4.5h16v6.5H4zM4 13h16v6.5H4zM7.5 7.75h.01M7.5 16.25h.01M11 7.75h5.5M11 16.25h5.5',
    frontend: 'M3 5h18v14H3zM3 9h18M6 7h.01M8.5 7h.01M7 13h6M7 16h9',
    db: 'M4 6.5C4 4.6 7.6 3 12 3s8 1.6 8 3.5S16.4 10 12 10 4 8.4 4 6.5zM4 6.5v11C4 19.4 7.6 21 12 21s8-1.6 8-3.5v-11M4 12c0 1.9 3.6 3.5 8 3.5s8-1.6 8-3.5',
    rds: 'M7 18.5a4.5 4.5 0 0 1-.9-8.9 6 6 0 0 1 11.6 1.1A4 4 0 0 1 17 18.5zM9 12.5c0-.8 1.3-1.5 3-1.5s3 .7 3 1.5-1.3 1.5-3 1.5-3-.7-3-1.5zM9 12.5v2.5c0 .8 1.3 1.5 3 1.5s3-.7 3-1.5v-2.5',
    web: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c2.4 2.5 3.6 5.5 3.6 9s-1.2 6.5-3.6 9c-2.4-2.5-3.6-5.5-3.6-9S9.6 5.5 12 3z',
    mail: 'M3 6h18v12H3zM3.5 6.5 12 13l8.5-6.5',
    router: 'M3 14h18v6H3zM7 17h.01M10.5 17h.01M16 14v-4M13 7.6a4.2 4.2 0 0 1 6 0M10.6 5.2a7.6 7.6 0 0 1 10.8 0'
  };
  const ROLE_NAME = { frontend: 'Frontend', backend: 'Backend', db: 'Database', rds: 'Managed DB', web: 'Web', mail: 'Mail', router: 'Router' };
  const ROLE_ABBR = { frontend: 'FE', backend: 'BE', db: 'DB', rds: 'RDS', web: 'WEB', mail: 'MAIL', router: 'RTR' };
  function roleName(r) { return ROLE_NAME[r] || (r ? r.charAt(0).toUpperCase() + r.slice(1) : 'Other'); }
  function roleAbbr(r) { return ROLE_ABBR[r] || (r || '?').slice(0, 4).toUpperCase(); }
  function iconPath(r) { return ICON[r] || ICON.backend; }
  function chip(role, tone, size) {
    const px = size === 'sm' ? 20 : size === 'lg' ? 28 : 24;
    return '<span class="chipg ' + (tone || '') + ' ' + (size || '') + '" aria-hidden="true"><svg class="glyph ' + (tone || '') +
      '" width="' + px + '" height="' + px + '" viewBox="0 0 24 24" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="' +
      iconPath(role) + '"></path></svg></span>';
  }

  // ---- gauges -------------------------------------------------------------
  // A metric with no value is NOT a healthy metric: it is never compared with
  // its threshold, so it gets its own dashed, labelled state rather than an
  // empty ring that reads as "0%, fine".
  function ring(m) {
    const has = m.value != null, pct = (m.unit || '') === '%', over = !!m.breached, C = 226.19;
    const v = has && pct ? Math.max(0, Math.min(100, m.value)) : 0;
    const tick = (has && pct && m.threshold != null)
      ? '<line class="rg-tick' + (over ? ' over' : '') + '" x1="44" y1="2" x2="44" y2="14" stroke-width="2" transform="rotate(' + (m.threshold * 3.6) + ' 44 44)"></line>' : '';
    const arc = (has && pct)
      ? '<circle class="rg-val' + (over ? ' over' : '') + '" cx="44" cy="44" r="36" stroke-width="8" stroke-dasharray="' + (C * v / 100).toFixed(1) + ' ' + C + '" transform="rotate(-90 44 44)"></circle>' : '';
    const text = has ? num(m.value) + (m.unit || '') : 'no data';
    const cmp = (m.comparison || '>');
    const sub = !has ? (m.agent_required ? 'needs agent' : 'not reporting')
      : (m.threshold != null ? 'limit ' + (cmp === '>' ? '' : cmp + ' ') + m.threshold + (m.unit || '') : '');
    const title = m.error ? m.error : ('alerts ' + cmp + ' ' + m.threshold + (m.unit || '') + (m.source ? ' - ' + m.source : ''));
    return '<div class="ring' + (over ? ' over' : '') + (has ? '' : ' nodata') + '" title="' + esc(title) + '"><div class="dial">' +
      '<svg aria-hidden="true" width="88" height="88" viewBox="0 0 88 88" fill="none"><circle class="rg-track' + (has ? '' : ' dashed') +
      '" cx="44" cy="44" r="36" stroke-width="8"></circle>' + arc + tick + '</svg><span>' + esc(text) + '</span></div><b>' +
      esc(m.label) + '</b><small>' + esc(sub) + '</small></div>';
  }

  // The metric nearest its limit: what the fleet map draws for an instance.
  function closest(inst) {
    let best = null, ratio = -1;
    (inst.metrics || []).forEach(m => {
      if (m.value == null || (m.unit || '') !== '%' || !m.threshold || (m.comparison || '>').charAt(0) !== '>') return;
      const r = m.value / m.threshold;
      if (r > ratio) { ratio = r; best = m; }
    });
    return best;
  }
  const SHORT = { CPU: 'CPU', Memory: 'MEM', Disk: 'DISK' };
  function shortLabel(l) { return SHORT[l] || l; }
  function nodeRing(inst) {
    const m = closest(inst), C = 157.08, v = m ? Math.max(0, Math.min(100, m.value)) : 0;
    return '<svg aria-hidden="true" width="60" height="60" viewBox="0 0 60 60" fill="none" style="flex:none"><circle class="rg-track' + (m ? '' : ' dashed') +
      '" cx="30" cy="30" r="25" stroke-width="5"></circle>' +
      (m ? '<circle class="rg-val' + (m.breached ? ' over' : '') + '" cx="30" cy="30" r="25" stroke-width="5" stroke-linecap="round" stroke-dasharray="' +
        (C * v / 100).toFixed(1) + ' ' + C + '" transform="rotate(-90 30 30)"></circle>' : '') +
      '<g class="glyph ' + inst.tone + '" transform="translate(18 18)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="' +
      iconPath(inst.role) + '"></path></g></svg>';
  }

  function probe(s) {
    const cls = s.up === true ? '' : (s.up === false ? ' down' : ' unknown');
    const val = s.up === false ? 'DOWN' : (s.latency_ms != null ? s.latency_ms + ' ms' : (s.up === true ? 'up' : 'unknown'));
    const title = s.error || s.detail || s.target || s.name;
    return '<span class="probe' + cls + '" title="' + esc(title) + '"><i aria-hidden="true"></i>' + esc(s.name + portOf(s)) + '<b>' + esc(val) + '</b></span>';
  }
  function portOf(s) {
    const m = /:(\d+)(?:\/|$)/.exec(s.target || '');
    return m ? ' :' + m[1] : '';
  }
  function shortErr(s) {
    const t = ((s && (s.error || s.detail)) || '').toLowerCase();
    const code = /unexpected status (\d+)/.exec(t);
    if (code) return ' ' + code[1];
    if (t.includes('refused')) return ' refused';
    if (t.includes('timed out') || t.includes('timeout')) return ' timeout';
    return '';
  }

  function specBits(spec) {
    // The instance's size, so a percentage can be read as an absolute. "95% of
    // 4 GB" is a different conversation from "95% of 64 GB".
    const b = [];
    if (!spec) return b;
    if (spec.vcpu != null) b.push(esc(spec.vcpu) + ' vCPU');
    if (spec.memory_mb != null) b.push(esc(fmtMem(spec.memory_mb)) + ' RAM');
    if (spec.disk_gb != null) b.push(esc(fmtDisk(spec.disk_gb)) + ' disk');
    if (spec.instance_type) b.push(esc(spec.instance_type));
    if (spec.platform) b.push(esc(spec.platform.length > 28 ? spec.platform.slice(0, 27) + '…' : spec.platform));
    return b;
  }

  // The container panel and the gauges above it are NOT simultaneous: metrics
  // come from CloudMonitor (an average over the agent's window), container usage
  // from a `docker stats` sample taken later in the same cycle over SSH. A short
  // spike can land in one and miss the other, which looks like a bug unless the
  // page says when each was taken.
  function sampledAt(det) {
    const d = parseUtc(det && det.containers_updated_at);
    if (!d) return '';
    const stale = !((Date.now() - d.getTime()) / 1000 < 600);
    const title = 'docker stats sampled at ' + det.containers_updated_at + ' - the gauges come from CloudMonitor and describe a slightly ' +
      'different moment, so a brief spike can appear in one and not the other';
    return '<span class="stamp' + (stale ? ' stale' : '') + '" title="' + esc(title) + '">usage @ ' + utcClock(d) + ' UTC</span>';
  }

  function heaviest(list, label) {
    return (list || []).filter(c => c.cpu_pct != null)
      .sort((a, b) => (label === 'Memory' ? b.mem_pct - a.mem_pct : b.cpu_pct - a.cpu_pct))[0] || null;
  }
  const BAD_HEALTH = ['unhealthy', 'stopped', 'restarting'];
  function ctrBad(c) { return BAD_HEALTH.includes(c.health); }

  // ---- what is wrong ------------------------------------------------------
  // "Connection refused" and "timed out" are opposite diagnoses, so the runbook
  // says which one rather than offering both.
  function serviceVariant(why) {
    const t = (why || '').toLowerCase();
    if (t.includes('refused')) return 'refused';
    if (t.includes('timed out') || t.includes('timeout')) return 'timeout';
    if (t.includes('unexpected status')) return 'status';
    return null;
  }

  // One flat, ranked list of everything wrong right now, so triage does not mean
  // scanning every card. Ordered by how much each explains the others: a stopped
  // instance accounts for its own dead metrics, so it goes first.
  function attentionItems(scan, details, insts) {
    const items = [];
    if (!scan) return items;
    const groupOf = id => (insts[id] && insts[id].group) || '';
    Object.entries(details).forEach(([id, det]) => {
      const ps = det.spec && det.spec.provider_status;
      if (ps && ps !== 'Running') {
        items.push({ sev: 0, kind: 'stopped', tone: 'hot', tag: 'STOPPED', instId: id, inst: det.name || id, group: groupOf(id), sub: ps,
          what: 'Instance is ' + ps, why: 'Reported by the provider, not by a probe', title: (det.name || id) + ' is ' + ps });
      }
    });
    (scan.results || []).filter(r => r.breached).forEach(r => {
      const det = details[r.instance_id] || {};
      const hot = heaviest(det.containers, r.label);
      const culprit = hot
        ? 'Heaviest service on this host: ' + hot.name + ' (' + ctrNum(hot.cpu_pct) + '% cpu, ' + ctrNum(hot.mem_pct) + '% mem' + (hot.mem_usage ? ', ' + hot.mem_usage : '') + ')'
        : 'No per-container figures for this host - diagnostics may not be set up on it';
      const under = (r.comparison || '>').charAt(0) === '<';
      items.push({ sev: 1, kind: r.label, tone: 'hot', tag: 'BREACH', instId: r.instance_id, inst: r.instance_name, group: r.group || '', sub: r.label, metric: r,
        what: r.label + ' ' + num(r.value) + (r.unit || '') + (under ? ' is under the ' : ' is over the ') + r.comparison + ' ' + r.threshold + (r.unit || '') + ' limit',
        why: culprit, title: r.label + ' breach on ' + r.instance_name });
    });
    (scan.services_down || []).forEach(sv => {
      const why = ((sv.target || '') + ' - ' + (sv.error || sv.detail || 'no detail')).trim();
      items.push({ sev: 2, kind: 'service', variant: serviceVariant(why), tone: 'hot', tag: 'DOWN', instId: sv.instance_id, inst: sv.instance_name,
        group: sv.group || '', sub: sv.name, svc: sv, what: sv.name + ' is unreachable', why: why, title: sv.name + ' probe failing on ' + sv.instance_name });
    });
    Object.entries(details).forEach(([id, det]) => {
      (det.containers || []).filter(ctrBad).forEach(c => {
        items.push({ sev: 3, kind: 'container', tone: 'warn', tag: 'CONTAINER', instId: id, inst: det.name || id, group: groupOf(id), sub: c.name, ctr: c.name, ctrObj: c,
          what: c.name + ' is ' + c.health, why: c.status || '', title: 'Container ' + c.name + ' ' + c.health + ' on ' + (det.name || id) });
      });
    });
    items.sort((a, b) => a.sev - b.sev);
    items.forEach((it, i) => { it.rank = pad(i + 1); it.key = [it.kind, it.instId, it.sub].join('|'); });
    return items;
  }

  function analyse(status) {
    const scan = status.last_scan || null, details = status.instances || {};
    const insts = {}, groups = [], gmap = {};
    function get(id, name, group) {
      const key = id || name;
      let i = insts[key];
      if (!i) {
        const det = details[id] || null;
        i = insts[key] = { id: id || '', name: name || (det && det.name) || key, group: group || 'Ungrouped', role: (det && det.role) || '',
          det: det, metrics: [], svcs: [] };
        let g = gmap[i.group];
        if (!g) { g = gmap[i.group] = { name: i.group, insts: [] }; groups.push(g); }
        g.insts.push(i);
      }
      return i;
    }
    if (scan) {
      (scan.results || []).forEach(r => get(r.instance_id, r.instance_name, r.group).metrics.push(r));
      (scan.services || []).forEach(s => get(s.instance_id, s.instance_name, s.group).svcs.push(s));
    }
    const counts = { instances: 0, attention: 0, stopped: 0, breaches: 0, metrics: 0, down: 0, probes: 0, ctrBad: 0, ctrs: 0 };
    Object.values(insts).forEach(i => {
      const det = i.det, ps = det && det.spec && det.spec.provider_status;
      i.stopped = (ps && ps !== 'Running') ? ps : '';
      i.breaches = i.metrics.filter(m => m.breached);
      i.down = i.svcs.filter(s => s.up === false);
      // A container docker calls unhealthy, or one in a restart loop, is a real
      // problem even when every metric is green and the port still answers.
      i.ctrBad = (det && det.containers_attention) || 0;
      if (i.stopped) { i.tone = 'hot'; i.tag = 'STOPPED'; }
      else if (i.breaches.length) { i.tone = 'hot'; i.tag = 'BREACH'; }
      else if (i.down.length) { i.tone = 'hot'; i.tag = 'DOWN'; }
      else if (i.ctrBad) { i.tone = 'warn'; i.tag = 'CONTAINER'; }
      else { i.tone = ''; i.tag = 'NOMINAL'; }
      i.problem = i.tone !== '';
      counts.instances++;
      if (i.problem) counts.attention++;
      if (i.stopped) counts.stopped++;
      counts.ctrBad += i.ctrBad;
      counts.ctrs += ((det && det.containers) || []).length;
    });
    // Instances that need a human sort to the front of their group.
    groups.forEach(g => {
      g.insts.sort((a, b) => (b.problem ? 1 : 0) - (a.problem ? 1 : 0) || a.name.localeCompare(b.name));
      g.hot = g.insts.filter(i => i.tone === 'hot').length;
      g.warn = g.insts.filter(i => i.tone === 'warn').length;
      g.tone = g.hot ? 'hot' : (g.warn ? 'warn' : '');
      g.note = g.hot ? g.hot + (g.hot === 1 ? ' needs attention' : ' need attention') : (g.warn ? g.warn + ' degraded' : 'all nominal');
    });
    if (scan) {
      counts.breaches = scan.breach_count || 0;
      counts.metrics = scan.total || 0;
      counts.down = scan.services_down_count || 0;
      counts.probes = scan.services_total || 0;
    }
    return { status: status, scan: scan, details: details, insts: insts, groups: groups, counts: counts,
      items: attentionItems(scan, details, insts), guidance: status.guidance || {}, config: status.config || {},
      alert: status.last_alert || null, history: status.history || [] };
  }

  // ---- how long has it been over ------------------------------------------
  // /api/status says a metric is over its limit now; only the history series can
  // say since when. One small fetch per breached instance, cached per scan.
  const histCache = {};
  async function breachInfo(instId, label, scanAt) {
    const ck = instId + '|' + scanAt;
    if (!histCache[ck]) {
      histCache[ck] = api('/api/metrics/history?' + new URLSearchParams({ instance_id: instId, hours: '6', points: '72' }))
        .then(r => (r.ok ? r.json() : null)).catch(() => null);
    }
    const d = await histCache[ck];
    const s = d && (d.series || []).find(x => x.label === label);
    if (!s || !s.points || !s.points.length) return null;
    const pts = s.points, under = (s.comparison || '>').charAt(0) === '<';
    const over = v => v != null && s.threshold != null && (under ? v < s.threshold : v > s.threshold);
    let k = pts.length - 1, skipped = 0, first = -1;
    while (k >= 0 && pts[k].v == null && skipped < 2) { k--; skipped++; }   // the newest bucket is often still empty
    for (; k >= 0 && over(pts[k].v); k--) first = k;
    const pct = (s.unit || '') === '%';
    const vals = pts.map(p => p.v).filter(v => v != null);
    const lo = pct ? 0 : Math.min.apply(null, vals.concat(s.threshold != null ? [s.threshold] : []));
    const hi = pct ? 100 : Math.max.apply(null, vals.concat(s.threshold != null ? [s.threshold] : []));
    const Y = v => (30 - ((v - lo) / ((hi - lo) || 1)) * 28).toFixed(1);
    let path = '', pen = false;
    pts.forEach((p, i) => {
      if (p.v == null) { pen = false; return; }             // a gap is a hole, never a line drawn across it
      path += (pen ? 'L' : 'M') + (i / Math.max(pts.length - 1, 1) * 120).toFixed(1) + ' ' + Y(p.v) + ' ';
      pen = true;
    });
    const out = { path: path.trim(), limitY: s.threshold != null ? Y(s.threshold) : null, dur: '', since: '' };
    if (first >= 0) {
      const since = parseUtc(pts[first].t), end = parseUtc(scanAt) || new Date();
      if (since) {
        const whole = first === 0;                           // over for the whole window: only a lower bound is known
        out.dur = (whole ? 'over ' : '') + span(Math.max(end - since, (d.bucket_seconds || 300) * 1000));
        out.since = (whole ? 'since before ' : 'since ') + utcClock(since, false) + ' UTC';
      }
    }
    return out;
  }
  function sparkSvg(info) {
    if (!info || !info.path) return '';
    return '<svg class="spark" aria-hidden="true" width="120" height="32" viewBox="0 0 120 32">' +
      (info.limitY != null ? '<line x1="0" y1="' + info.limitY + '" x2="120" y2="' + info.limitY + '"></line>' : '') +
      '<path d="' + info.path + '"></path></svg>';
  }

  // ---- runbook ------------------------------------------------------------
  // Cause and steps come from the backend (guidance.py) - the same copy the alert
  // email carries. Only the evidence on top is composed here, from the live scan.
  const COPY_ICON = '<svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"></rect><path d="M10.5 3.5v-1a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h1"></path></svg>';
  const ARROW = '<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"></path></svg>';

  function evidenceHtml(item, an) {
    const det = an.details[item.instId] || {};
    const row = (k, v, w) => '<div><span class="k">' + esc(k) + '</span>' +
      (w != null ? '<span class="bar" aria-hidden="true"><b style="width:' + Math.max(0, Math.min(100, w)) + '%"></b></span>' : '') +
      '<span class="v">' + esc(v) + '</span></div>';
    let title = 'What we know', rows = '', note = '';
    if (item.metric) {
      const key = item.kind === 'Memory' ? 'mem_pct' : 'cpu_pct', word = item.kind === 'Memory' ? 'memory' : 'CPU';
      const top = (det.containers || []).filter(c => c[key] != null).sort((a, b) => b[key] - a[key]).slice(0, 3);
      if (top.length && item.kind !== 'Disk') {
        title = 'Heaviest containers by ' + word;
        rows = top.map(c => row(c.name, ctrNum(c[key]) + '%' + (key === 'mem_pct' && c.mem_usage ? ' · ' + c.mem_usage : ''), c[key])).join('');
        const d = parseUtc(det.containers_updated_at);
        note = 'docker stats' + (d ? ' sampled ' + utcClock(d) + ' UTC' : '') + '. The ' + num(item.metric.value) + (item.metric.unit || '') +
          ' comes from CloudMonitor and describes a slightly different moment.';
      } else {
        title = 'What the scan saw';
        rows = row('Value', num(item.metric.value) + (item.metric.unit || '')) + row('Limit', item.metric.comparison + ' ' + item.metric.threshold + (item.metric.unit || '')) +
          row('Source', item.metric.source || '-');
        if (item.kind !== 'Disk') note = det.containers_error || 'No per-container figures for this host - diagnostics may not be set up on it.';
      }
    } else if (item.svc) {
      const reading = { refused: 'host reached, nothing listening on that port', timeout: 'no answer: instance down or packets dropped',
        status: 'host reached, something else answered' }[item.variant];
      title = 'What the probe saw';
      rows = row('Target', item.svc.target || '-') + row('Result', item.svc.error || item.svc.detail || 'no detail') + (reading ? row('Reading', reading) : '');
    } else if (item.ctrObj) {
      title = 'What docker reports';
      rows = row('Container', item.ctrObj.name) + row('Status', item.ctrObj.status || item.ctrObj.health) + (item.ctrObj.image ? row('Image', item.ctrObj.image) : '');
      note = 'A restart loop burns host CPU that no container is charged for, so host CPU can read high while every container reads 0%.';
    } else {
      title = 'What the provider reports';
      rows = row('Status', item.sub || '-');
    }
    return '<div class="block"><h3 class="cap">' + esc(title) + '</h3><div class="kv">' + rows + '</div>' +
      (note ? '<p class="tiny mut">' + esc(note) + '</p>' : '') + '</div>';
  }

  function diagnosticsHtml(an, instName, open) {
    const d = an.scan && an.scan.diagnostics && an.scan.diagnostics[instName];
    if (!d || !d.output) return '';
    return '<details class="ev"' + (open ? ' open' : '') + '><summary>Live evidence from the host' + (d.focus ? ' (' + esc(d.focus) + ')' : '') +
      '</summary><pre class="evidence">' + esc(d.output) + '</pre></details>';
  }

  function runbook(item, an, opts) {
    opts = opts || {};
    const g = an.guidance[item.kind];
    const cause = g ? ((item.variant && g.variants && g.variants[item.variant]) || g.cause) : '';
    const steps = ((g && g.steps) || []).map((st, i) => {
      const cmd = (st.cmd || '').replace(/<name>/g, item.ctr || '<name>');
      return '<li><span class="n" aria-hidden="true">' + (i + 1) + '</span><div class="s"><span class="w">' + esc(st.what) + '</span>' +
        (cmd ? '<div class="cmd"><code>' + esc(cmd) + '</code><button type="button" class="copy" data-copy="' + esc(cmd) + '" aria-label="Copy command">' + COPY_ICON + '</button></div>' : '') +
        '</div></li>';
    }).join('');
    return '<div class="block"><span class="eyebrow">Runbook for issue ' + item.rank + '</span><h2 class="runbook-title">' + esc(item.title) + '</h2></div>' +
      (cause ? '<div class="block"><h3 class="cap">Likely cause</h3><p class="cause">' + esc(cause) + '</p></div>' : '') +
      evidenceHtml(item, an) +
      (steps ? '<div class="block"><h3 class="cap">Do this next</h3><ol class="steps">' + steps + '</ol></div>' : '') +
      (opts.noLink ? '' : diagnosticsHtml(an, item.inst, false)) +
      (opts.noLink || !item.instId ? '' : '<a class="btn solid block" href="instance.html?id=' + encodeURIComponent(item.instId) + '">Open ' + esc(item.inst) + ' ' + ARROW + '</a>');
  }

  // Works over plain HTTP too, where navigator.clipboard does not exist.
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text).then(() => true, () => false);
    const ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return Promise.resolve(ok);
  }
  document.addEventListener('click', ev => {
    const b = ev.target.closest('[data-copy]');
    if (!b) return;
    copyText(b.getAttribute('data-copy')).then(ok => {
      toast(ok ? 'Command copied' : 'Could not copy - select the text instead');
      if (ok) { b.classList.add('done'); setTimeout(() => b.classList.remove('done'), 1500); }
    });
  });

  // ---- top bar ------------------------------------------------------------
  const NAV = [['overview', 'index.html', 'Overview'], ['instances', 'instances.html', 'Instances'], ['activity', 'activity.html', 'Activity']];
  const LOGO = '<svg aria-hidden="true" width="26" height="26" viewBox="0 0 26 26" fill="none" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2.5 22 7.75v10.5L13 23.5 4 18.25V7.75z"></path><path d="M8 14.5 11 11l3 3.5 4-5"></path></svg>';
  const clock = { at: null, interval: 5 };

  function topbar(active) {
    const el = $('#topbar');
    if (!el) return;
    document.body.classList.add('has-nav');
    el.innerHTML = '<a class="brand" href="index.html">' + LOGO + 'Cloud Agent Monitoring</a>' +
      '<nav class="nav" aria-label="Primary">' + NAV.map(n => '<a href="' + n[1] + '"' + (n[0] === active ? ' aria-current="page"' : '') + '>' + n[2] +
        (n[0] === 'overview' ? '<span class="badge" id="navBadge" hidden></span>' : '') + '</a>').join('') + '</nav>' +
      '<div class="tools"><span class="pill" id="modePill"><i class="dot" aria-hidden="true"></i><span>connecting</span></span>' +
      '<span class="pill" id="autoPill">auto-alert -</span>' +
      '<span class="scanclock" id="scanClock"><span id="scanText">no scan yet</span><i aria-hidden="true"><b id="scanBar"></b></i></span>' +
      '<button type="button" class="btn primary" id="scanBtn">Scan now</button>' +
      '<button type="button" class="btn quiet" id="themeBtn"></button>' +
      '<a class="link-plain" href="#" id="logoutLink">Sign out</a></div>';
    paintThemeBtn();
    $('#themeBtn').addEventListener('click', () => setTheme(theme() === 'light' ? 'dark' : 'light'));
    $('#logoutLink').addEventListener('click', e => { e.preventDefault(); window.location = API + '/auth/logout'; });
    $('#scanBtn').addEventListener('click', scanNow);
  }

  function setMode(cls, text) {
    const p = $('#modePill');
    if (!p) return;
    p.className = 'pill ' + cls;
    p.lastElementChild.textContent = text;
  }

  function topbarUpdate(an) {
    const b = $('#navBadge');
    if (b) { b.hidden = !an.items.length; b.textContent = an.items.length; b.setAttribute('aria-label', plural(an.items.length, 'open issue')); }
    setMode(an.config.mode === 'mock' ? 'mock' : '', an.config.mode === 'mock' ? 'MOCK MODE' : 'LIVE');
    const a = $('#autoPill');
    if (a) a.textContent = 'auto-alert ' + (an.config.auto_alert_on_breach ? 'on' : 'off');
    clock.at = an.scan ? parseUtc(an.scan.scanned_at) : null;
    clock.interval = an.config.interval_minutes || 5;
    tick();
  }

  function tick() {
    const t = $('#scanText'), bar = $('#scanBar'), box = $('#scanClock');
    if (!t) return;
    if (!clock.at) { t.textContent = 'no scan yet'; bar.style.width = '0'; return; }
    const total = clock.interval * 60000, gone = Date.now() - clock.at.getTime(), left = total - gone;
    // Three missed intervals is what /api/ready calls stale; say so here too.
    const stale = gone > total * 3;
    box.classList.toggle('stale', stale);
    const next = stale ? 'last scan ' + span(gone) + ' ago'
      : (left > 0 ? 'next in ' + pad(Math.floor(left / 60000)) + ':' + pad(Math.floor(left % 60000 / 1000)) : 'next scan due');
    t.textContent = 'scanned ' + utcClock(clock.at) + ' UTC · ' + next;
    bar.style.width = Math.max(0, Math.min(100, gone / total * 100)) + '%';
  }

  // ---- page lifecycle -----------------------------------------------------
  let lastRaw = null, renderFn = null, lastAn = null;

  async function refresh(force) {
    let r;
    try { r = await api('/api/status'); } catch (e) { setMode('bad', 'API unreachable'); return; }
    if (authGuard(r)) return;
    if (!r.ok) { setMode('bad', 'API error ' + r.status); return; }
    const raw = await r.text();
    // Polled every 15 s but only changes once a scan: repainting an identical
    // page would throw away a text selection or an open evidence block.
    if (!force && raw === lastRaw) return;
    lastRaw = raw;
    lastAn = analyse(JSON.parse(raw));
    topbarUpdate(lastAn);
    if (renderFn) renderFn(lastAn);
  }

  async function scanNow() {
    const b = $('#scanBtn');
    b.disabled = true;
    try {
      if (!CSRF) await loadCsrf();
      const r = await api('/api/scan', { method: 'POST', headers: postHeaders() });
      if (authGuard(r)) return;
      if (!r.ok) { toast('Scan failed (' + r.status + ')'); return; }
      const s = await r.json();
      toast('Scan done - ' + plural(s.breach_count || 0, 'breach', 'breaches') + ', ' + plural(s.services_down_count || 0, 'service') + ' down');
      await refresh(true);
    } catch (e) { toast('Scan failed - the API did not answer'); }
    finally { b.disabled = false; }
  }

  async function sendAlert(btn) {
    if (btn) btn.disabled = true;
    try {
      if (!CSRF) await loadCsrf();
      const r = await api('/api/send', { method: 'POST', headers: postHeaders() });
      if (authGuard(r)) return;
      if (!r.ok) { toast('Email not sent (' + r.status + ')'); return; }
      const a = await r.json();
      toast(a.sent ? 'Alert email sent' : (a.reason || 'Email not sent'));
      await refresh(true);
    } catch (e) { toast('Email not sent - the API did not answer'); }
    finally { if (btn) btn.disabled = false; }
  }

  async function start(active, render) {
    topbar(active);
    renderFn = render;
    await refresh(true);
    if (!CSRF) await loadCsrf();
    setInterval(refresh, 15000);
    setInterval(tick, 1000);
  }

  window.CAM = {
    $: $, esc: esc, api: api, loadCsrf: loadCsrf, postHeaders: postHeaders, authGuard: authGuard, toast: toast,
    parseUtc: parseUtc, utcClock: utcClock, utcStamp: utcStamp, span: span, num: num, ctrNum: ctrNum, pct: pct, plural: plural,
    roleName: roleName, roleAbbr: roleAbbr, chip: chip, ring: ring, nodeRing: nodeRing, closest: closest, shortLabel: shortLabel,
    probe: probe, portOf: portOf, shortErr: shortErr, specBits: specBits, sampledAt: sampledAt, heaviest: heaviest, ctrBad: ctrBad,
    breachInfo: breachInfo, sparkSvg: sparkSvg, runbook: runbook, diagnosticsHtml: diagnosticsHtml, ARROW: ARROW,
    start: start, refresh: refresh, sendAlert: sendAlert, current: () => lastAn
  };
})();
