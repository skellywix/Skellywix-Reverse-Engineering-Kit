'use strict';
/*
 * Fourmilab Base64 1.5 candidate desk.
 * Same-origin only: GET /state[?run=<id>], GET /package/<run>, POST /action. The page carries no credential:
 * it reads at once, keeps polling, and shows the last known state with an honest connection label.
 * GETs never write. POST /action is sent only from an explicit click, with the X-Candidate-Action marker.
 * All server text reaches the page through textContent / createTextNode.
 */
(() => {
  const STAGES = ['intake', 'analyze', 'rebuild', 'verify', 'package'];
  const POLL_MS = 1000;
  const FETCH_TIMEOUT_MS = 8000;
  const ACTION_TIMEOUT_MS = 30000;
  const DOWNLOAD_TIMEOUT_MS = 120000;
  const OBJECT_URL_TTL_MS = 10000;
  const PAGE = 100;
  const CLIP = 4000;
  const KNOWN_DETAIL = new Set([
    'worker', 'skill', 'image', 'tool', 'generation', 'requested_model', 'requested_effort',
    'actual_model', 'actual_effort', 'deterministic', 'reason', 'completed',
    'required', 'evidence',
  ]);
  const REL_ROLES = [['active', 'Active'], ['staged', 'Staged'], ['previous', 'Previous']];
  const PAIRED_ORIGIN = 'http://127.0.0.1:48784';

  const $ = (id) => document.getElementById(id);
  const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

  const app = {
    selectedId: readHash(),
    data: null,
    link: 'connecting',
    lastOk: '',
    acting: false,
    downloading: false,
    alerts: { action: null, poll: null },
    filters: { stage: '', status: '', attempt: '', q: '' },
    limit: PAGE,
    expanded: new Set(),
    openDetails: new Set(),
    confirm: null,
    confirmDigest: null,
    sig: {},
    runEls: new Map(),
    evEls: new Map(),
    stageCards: null,
    detailSeq: 0,
    pollToken: 0,
    timer: 0,
    ctrl: null,
    noticeTimer: 0,
    role: null,
    proj: null,
    brief: null,
    feed: blankFeed(),
  };

  /* ---------- tiny helpers ---------- */

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else el.setAttribute(k, v === true ? '' : String(v));
      }
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  function setText(el, text) {
    if (el.textContent !== text) el.textContent = text;
  }

  function setHidden(el, hidden) {
    if (el.hidden !== hidden) el.hidden = hidden;
  }

  function safeJson(v, pretty) {
    try {
      const s = JSON.stringify(v, null, pretty ? 2 : 0);
      return s === undefined ? String(v) : s;
    } catch (_) {
      return '[unserializable value]';
    }
  }

  function clip(s, n) {
    return s.length > n ? s.slice(0, n) + `\n… truncated, ${s.length - n} more characters` : s;
  }

  function fmt(v) {
    if (v === null || v === undefined) return '';
    if (typeof v === 'string') return v;
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
    const s = safeJson(v, false);
    return s.length > 200 ? s.slice(0, 200) + '…' : s;
  }

  function short(s, n) {
    s = fmt(s);
    return s.length > n ? s.slice(0, n) + '…' : s;
  }

  function numeric(v) {
    return typeof v === 'number' && Number.isFinite(v) ? v : null;
  }

  function fmtTime(at) {
    if (at === null || at === undefined || at === '') return { text: '—', iso: '' };
    let d;
    if (typeof at === 'number') d = new Date(at < 1e12 ? at * 1000 : at);
    else d = new Date(at);
    if (Number.isNaN(d.getTime())) return { text: String(at), iso: '' };
    const text = d.toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    });
    return { text, iso: d.toISOString() };
  }

  function timeEl(at) {
    const t = fmtTime(at);
    return h('time', { datetime: t.iso || null, title: t.iso || null, text: t.text });
  }

  function tone(status) {
    const s = String(status === null || status === undefined ? '' : status).toLowerCase();
    if (/(fail|error|reject|block|cancel|abort|timeout|timed|denied|invalid|mismatch|unverif|incomplete)/.test(s)) return 'bad';
    if (/(pass|\bok\b|verified|complete|done|success|ready|promoted|accepted|match)/.test(s)) return 'good';
    if (/(run|start|progress|active|work|pending|queue|wait|busy|analy|build)/.test(s)) return 'live';
    return 'idle';
  }

  function chipClass(status) {
    return 'chip chip-' + tone(status);
  }

  function chip(status) {
    return h('span', { class: chipClass(status), text: status === null || status === undefined || status === '' ? 'Unknown' : fmt(status) });
  }

  function setChip(el, status) {
    const cls = chipClass(status);
    if (el.className !== cls) el.className = cls;
    setText(el, status === null || status === undefined || status === '' ? 'Unknown' : fmt(status));
  }

  function readHash() {
    try {
      return new URLSearchParams(location.hash.slice(1)).get('run') || null;
    } catch (_) {
      return null;
    }
  }

  function pinRun(id) {
    app.selectedId = id || null;
    const url = id ? '#run=' + encodeURIComponent(id) : location.pathname + location.search;
    try { history.replaceState(null, '', url); } catch (_) { /* hash only is cosmetic */ }
  }

  function setSelected(id) {
    pinRun(id);
    app.limit = PAGE;
    app.expanded.clear();
    app.filters = { stage: '', status: '', attempt: '', q: '' };
    app.sig.run = null;
    chatReset();
    feedReset();
    app.role = null;
  }

  /* Keep focus and scroll stable across a render pass. */
  function stable(fn) {
    const active = document.activeElement;
    const fkey = active && active.dataset ? active.dataset.fkey : null;
    const x = window.scrollX;
    const y = window.scrollY;
    fn();
    if (fkey && !active.isConnected) {
      const again = document.querySelector('[data-fkey="' + CSS.escape(fkey) + '"]');
      if (again) again.focus({ preventScroll: true });
    }
    if (window.scrollX !== x || window.scrollY !== y) window.scrollTo(x, y);
  }

  /* Keyed in-place list reconcile: existing nodes are updated, never recreated. */
  function reconcile(parent, items, keyOf, make, update, cache) {
    const seen = new Set();
    let cursor = parent.querySelector(':scope > [data-key]');
    for (const item of items) {
      const key = keyOf(item);
      seen.add(key);
      let el = cache.get(key);
      if (!el) {
        el = make(item, key);
        el.dataset.key = key;
        cache.set(key, el);
      } else {
        update(el, item, key);
      }
      if (el === cursor) {
        cursor = cursor.nextElementSibling;
        while (cursor && !cursor.dataset.key) cursor = cursor.nextElementSibling;
      } else {
        parent.insertBefore(el, cursor);
      }
    }
    for (const [key, el] of cache) {
      if (!seen.has(key)) {
        el.remove();
        cache.delete(key);
      }
    }
  }

  /* ---------- network ---------- */

  async function readError(res) {
    let msg = 'HTTP ' + res.status;
    try {
      const body = await res.json();
      if (body && body.error) msg = String(body.error);
    } catch (_) { /* keep the status text */ }
    return msg;
  }

  /* Same-origin, uncached, credential-free, no redirects. The page holds no key, cookie or token. */
  function request(url, opts) {
    return fetch(url, Object.assign({}, opts, {
      cache: 'no-store', credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer',
    }));
  }

  /* True only while the latest read succeeded; otherwise any data on screen is last known, not current. */
  function isLive() {
    return app.link === 'live' && app.data !== null;
  }

  function schedule(ms) {
    clearTimeout(app.timer);
    app.timer = setTimeout(poll, ms);
  }

  function pollNow() {
    clearTimeout(app.timer);
    poll();
  }

  async function poll() {
    const token = ++app.pollToken;
    if (app.ctrl) app.ctrl.abort();
    const ctrl = new AbortController();
    app.ctrl = ctrl;
    let timedOut = false;
    const to = setTimeout(() => { timedOut = true; ctrl.abort(); }, FETCH_TIMEOUT_MS);
    const url = app.selectedId ? '/state?run=' + encodeURIComponent(app.selectedId) : '/state';
    try {
      const res = await request(url, { headers: { Accept: 'application/json' }, signal: ctrl.signal });
      if (token !== app.pollToken) return;
      if (!res.ok) throw new Error(await readError(res));
      const data = await res.json();
      if (token !== app.pollToken) return;
      if (!isObj(data)) throw new Error('unexpected response shape');
      app.link = 'live';
      app.alerts.poll = null;
      app.lastOk = new Date().toLocaleTimeString([], { hour12: false });
      render(data);
    } catch (err) {
      if (token !== app.pollToken) return;
      if (ctrl.signal.aborted && !timedOut) return;
      const why = timedOut ? 'no response within ' + FETCH_TIMEOUT_MS / 1000 + ' s' : (err && err.message) || 'request failed';
      app.link = 'lost';
      app.alerts.poll = 'Can’t reach the desk (' + why + '). '
        + (app.data ? 'Showing the last known state; ' : 'Nothing has been read yet; ') + 'retrying every second.';
      stable(() => { renderConn(); renderAlerts(); renderPending(); renderStale(); syncControls(); });
    } finally {
      clearTimeout(to);
      if (token === app.pollToken) schedule(POLL_MS);
    }
  }

  /*
   * One bounded attempt per click. If no reply arrives the outcome is unknown: the request is never
   * replayed automatically and the operator is told to check state before trying again.
   */
  async function act(payload, label) {
    if (app.acting || !isLive()) return;
    app.acting = true;
    app.alerts.action = null;
    stable(() => { renderAlerts(); syncControls(); renderConn(); });
    const ctrl = new AbortController();
    let timedOut = false;
    const unknown = why => label + ' outcome unknown: ' + why + '. It may have taken effect. Check the run list and release state before trying again; the request has not been retried.';
    const to = setTimeout(() => { timedOut = true; ctrl.abort(); }, ACTION_TIMEOUT_MS);
    try {
      const res = await request('/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Candidate-Action': '1', Accept: 'application/json' },
        body: JSON.stringify(payload),
        signal: ctrl.signal,
      });
      let body = null;
      try { body = await res.json(); } catch (_) { /* handled below */ }
      if (res.ok && (!isObj(body) || (!body.error && body.ok !== true))) {
        app.alerts.action = unknown('the desk returned no usable action receipt');
        return;
      }
      if (!res.ok || body.error) {
        throw new Error(body && body.error ? String(body.error) : 'HTTP ' + res.status);
      }
      if (payload.action === 'retry') { chatReset(); feedReset(); }
      if (body.run && (payload.action === 'start' || payload.action === 'retry')) setSelected(String(body.run));
      showNotice(label + ' accepted' + (body.run ? ' (run ' + short(body.run, 24) + ')' : '') + '.');
    } catch (err) {
      if (ctrl.signal.aborted || err instanceof TypeError) {
        const why = timedOut ? 'no reply within ' + ACTION_TIMEOUT_MS / 1000 + ' s' : ctrl.signal.aborted ? 'the request was interrupted' : 'the connection failed before a reply';
        app.alerts.action = unknown(why);
      } else {
        app.alerts.action = label + ' failed: ' + ((err && err.message) || 'unknown error') + '. Check the message, then try again; the run list refreshes automatically.';
      }
    } finally {
      clearTimeout(to);
      app.acting = false;
      stable(() => { renderAlerts(); syncControls(); renderConn(); });
      pollNow();
    }
  }

  /* Fetch the package into a temporary object URL; the link never leaves the page. */
  async function download(run) {
    if (app.downloading || !isLive()) return;
    app.downloading = true;
    app.alerts.action = null;
    stable(() => { renderAlerts(); syncControls(); });
    const ctrl = new AbortController();
    let timedOut = false;
    const to = setTimeout(() => { timedOut = true; ctrl.abort(); }, DOWNLOAD_TIMEOUT_MS);
    try {
      const res = await request('/package/' + encodeURIComponent(run.id), { headers: { Accept: 'application/zip' }, signal: ctrl.signal });
      if (!res.ok) throw new Error(await readError(res));
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = h('a', { href: url, download: 'fourmilab-base64-verified.zip', hidden: true });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), OBJECT_URL_TTL_MS);
      showNotice('Package downloaded (' + blob.size + ' bytes).');
    } catch (err) {
      const why = ctrl.signal.aborted ? (timedOut ? 'no complete reply within ' + DOWNLOAD_TIMEOUT_MS / 1000 + ' s' : 'the request was interrupted') : (err && err.message) || 'unknown error';
      app.alerts.action = 'Package download failed: ' + why + '. Check the run is still verified, then try again.';
    } finally {
      clearTimeout(to);
      app.downloading = false;
      stable(() => { renderAlerts(); syncControls(); });
    }
  }

  function showNotice(text) {
    const el = $('notice');
    setText(el, text);
    clearTimeout(app.noticeTimer);
    app.noticeTimer = setTimeout(() => setText(el, ''), 6000);
  }

  /* ---------- derived data ---------- */

  function currentRun() {
    const d = app.data;
    if (!d || !isObj(d.run)) return null;
    if (app.selectedId && d.run.id !== app.selectedId) return null;
    return d.run;
  }

  function sortedEvents(run) {
    const evs = Array.isArray(run.events) ? run.events.filter(isObj) : [];
    return evs
      .map((e, i) => ({ e, i }))
      .sort((a, b) => {
        const aa = numeric(a.e.attempt), ba = numeric(b.e.attempt);
        if (aa !== null && ba !== null && aa !== ba) return aa - ba;
        const as = numeric(a.e.seq), bs = numeric(b.e.seq);
        if (as !== null && bs !== null && as !== bs) return as - bs;
        return a.i - b.i;
      })
      .map((x) => x.e);
  }

  function runAttempt(run, events) {
    const a = numeric(run.attempt);
    if (a !== null) return a;
    let max = null;
    for (const e of events) {
      const v = numeric(e.attempt);
      if (v !== null && (max === null || v > max)) max = v;
    }
    return max;
  }

  const ATTRIBUTION = ['worker', 'skill', 'image', 'tool', 'deterministic', 'requested_model', 'requested_effort', 'actual_model', 'actual_effort'];
  const detailOf = (ev) => (isObj(ev.detail) ? ev.detail : {});
  const present = (v) => v !== null && v !== undefined && v !== '';

  /*
   * One card per stage for the selected attempt only, scoped to the latest generation for rebuild, verify and package
   * (see scopeEvents). Invocation, model and effort are grouped per (worker, generation): a field one worker or
   * generation reported is never shown as another's. Earlier generations stay in the event table.
   */
  function deriveStages(scope) {
    const out = {};
    for (const name of STAGES) {
      out[name] = null;
      const all = scope.all.filter((r) => r.stage === name);
      if (!all.length) continue;
      const scoped = scope.rows.filter((r) => r.stage === name);
      const cases = stageCases(scoped);
      const s = {
        count: all.length,
        gen: GEN_STAGES.has(name) ? scope.gen : null,
        last: scoped.length ? scoped[scoped.length - 1].ev : null,
        cases: { completed: cases.done, required: cases.req },
        reason: undefined,
        invs: [],
      };
      const groups = new Map();
      for (const { ev, gen } of scoped) {
        const d = detailOf(ev);
        if (present(d.reason)) s.reason = d.reason;
        const key = (present(d.worker) ? fmt(d.worker) : '') + '\u0000' + gen;
        let inv = groups.get(key);
        if (!inv) {
          inv = { worker: present(d.worker) ? d.worker : null, gen, d: {} };
          groups.set(key, inv);
        }
        for (const k of ATTRIBUTION) if (d[k] !== null && d[k] !== undefined) inv.d[k] = d[k];
      }
      s.invs = [...groups.values()].filter((i) => i.worker !== null || ATTRIBUTION.some((k) => own(i.d, k)));
      out[name] = s;
    }
    return out;
  }

  function modelJoin(m, e) {
    const show = (v) => (v !== null && v !== undefined ? fmt(v) : 'unknown');
    return 'model ' + show(m) + ', effort ' + show(e);
  }

  /*
   * Requested and actual are always reported separately; a value no event reported reads "unknown".
   * "No model" is claimed only for a deterministic step that reported no model or effort at all.
   */
  function modelReport(d) {
    const am = d.actual_model, rm = d.requested_model, ae = d.actual_effort, re = d.requested_effort;
    const nn = (v) => v !== null && v !== undefined;
    const hasAny = nn(am) || nn(rm) || nn(ae) || nn(re);
    const det = d.deterministic === true;
    if (!hasAny) {
      return det
        ? { text: 'No model (deterministic step)', tone: 'none' }
        : { text: 'Model and effort: unknown (not reported by events)', tone: 'unknown' };
    }
    let text = 'Requested ' + modelJoin(rm, re) + ' · Actual ' + modelJoin(am, ae);
    const differs = (nn(rm) && nn(am) && fmt(rm) !== fmt(am)) || (nn(re) && nn(ae) && fmt(re) !== fmt(ae));
    let tone = 'ok';
    if (!(nn(rm) && nn(am) && nn(re) && nn(ae))) tone = 'unknown';
    if (differs) tone = 'warn';
    if (det) {
      text += ' · conflicts with deterministic flag';
      tone = 'warn';
    }
    return { text, tone };
  }

  function invokedText(inv) {
    const d = inv.d;
    const parts = [];
    for (const [k, label] of [['worker', 'Worker'], ['skill', 'Skill'], ['image', 'Image'], ['tool', 'Tool']]) {
      if (present(d[k])) parts.push(label + ' ' + short(d[k], 60));
    }
    if (inv.gen !== null) parts.push('Generation ' + inv.gen);
    return parts.length ? 'Reported by events: ' + parts.join(' · ') : 'Invocation: Not reported';
  }

  function eventSummary(ev) {
    const d = isObj(ev.detail) ? ev.detail : {};
    const parts = [];
    const cp = casePair(ev);
    const c = cp.done, r = cp.req;
    if (c !== null && r !== null) parts.push(c + '/' + r + ' cases');
    else if (c !== null) parts.push(c + ' cases');
    for (const k of ['tool', 'skill']) if (d[k]) parts.push(short(d[k], 40));
    if (d.reason) parts.push(short(d.reason, 120));
    if (!isObj(ev.detail) && ev.detail !== null && ev.detail !== undefined) parts.push(short(ev.detail, 120));
    return parts.join(' · ') || '—';
  }

  /* ---------- run story: a read-only projection of events the desk already reports ---------- */

  const STAGE_LABEL = { intake: 'Intake', analyze: 'Analyze', rebuild: 'Rebuild', verify: 'Verify', package: 'Package' };
  const GEN_STAGES = new Set(['rebuild', 'verify', 'package']);
  const TERMINAL = new Set(['verified', 'failed', 'blocked', 'cancelled']);
  const STOPPED = new Set(['failed', 'blocked', 'cancelled']);
  const STOP_WORD = { failed: 'Failed', blocked: 'Blocked', cancelled: 'Cancelled' };
  const CASE_LABEL = { analyze: 'reference observation', verify: 'candidate comparison' };
  /* Declared roles in pipeline order. The kind is the declared contract, not evidence that a role ran. */
  const WORKERS = [
    { name: 'File analyst', stage: 'intake', kind: 'Deterministic', task: 'Identify the reference file', activity: 'Identifying the reference file' },
    { name: 'Behavior analyst', stage: 'analyze', kind: 'Deterministic', task: 'Record reference outputs', activity: 'Recording reference behavior' },
    { name: 'Rust builder', stage: 'rebuild', kind: 'Model', task: 'Write the Rust source', activity: 'Writing the Rust source' },
    { name: 'Compiler', stage: 'rebuild', kind: 'Deterministic', task: 'Compile the Rust source', activity: 'Compiling the Rust build' },
    { name: 'Verifier', stage: 'verify', kind: 'Deterministic', task: 'Compare candidate outputs', activity: 'Checking the Rust build against the reference' },
    { name: 'Packager', stage: 'package', kind: 'Deterministic', task: 'Bundle the verified build', activity: 'Packaging the verified build' },
  ];
  /* Handoffs drawn in the team chart: [link id, from, to]. The coordinator and Resolution are not roles and have no events of their own. */
  const COORD = 'Run coordinator';
  const HANDOFFS = [
    ['dispatch', COORD, 'File analyst'],
    ['observe', 'File analyst', 'Behavior analyst'],
    ['build', 'Behavior analyst', 'Rust builder'],
    ['compile', 'Rust builder', 'Compiler'],
    ['verify', 'Compiler', 'Verifier'],
    ['package', 'Verifier', 'Packager'],
    ['resolve', 'Packager', 'Resolution'],
  ];
  const STUBS = [['stub-coord', COORD], ['stub-file', 'File analyst'], ['stub-behavior', 'Behavior analyst'],
    ['stub-builder', 'Rust builder'], ['stub-compiler', 'Compiler'], ['stub-verifier', 'Verifier'], ['stub-packager', 'Packager']];
  const countText = new Intl.NumberFormat().format;
  const CHAT_TURNS = 8;
  const QUESTION_MAX = 240;
  const ANSWER_MAX = 900;
  const TOPICS = [
    ['failure', /\b(fail\w*|error\w*|why|block\w*|stop\w*|reject\w*|cancel\w*|problem|wrong|crash\w*)\b/],
    ['next', /\b(next|expect\w*|what happens|afterwards?|then|wait\w*)\b/],
    ['progress', /\b(progress|how far|how many|cases?|percent\w*|eta|remaining|counts?|bars?)\b|%/],
    ['specialist', /\b(model|work|doing|did|status|reported|using|use|finished|activity)\b/],
    ['verified', /\b(verif\w*|packages?|trust\w*|passed?|download)\b/],
    ['worker', /\b(who|workers?|roles?|models?|agents?|reported|reports?|last|which|effort)\b/],
    ['evidence', /\b(evidence|events?|proof|supports?|records?|sequence|seq|ledger|source)\b/],
    ['status', /\b(status|happening|doing|state|now|going|running|done|finished?|complete\w*|summary|overview|where)\b/],
  ];
  const UNSUPPORTED = 'I can only answer from this run’s recorded events, and only about: what is happening, who reported last and with which model, what happens next, progress, why it failed or stopped, whether it is verified, or which event supports an answer. Try one of the suggested questions.';

  const lower = (v) => String(v === null || v === undefined ? '' : v).toLowerCase();
  const stageOf = (ev) => lower(ev.stage);
  const statusOf = (ev) => lower(ev.status);
  const workerOf = (ev) => { const w = detailOf(ev).worker; return present(w) ? fmt(w) : null; };
  const isRejected = (ev) => lower(detailOf(ev).outcome) === 'rejected';
  const seqOf = (ev) => (present(ev.seq) ? 'event #' + fmt(ev.seq) : 'an unnumbered event');
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many);
  const stageName = (ev) => STAGE_LABEL[stageOf(ev)] || fmt(ev.stage) || 'an unreported step';
  const put = (id, text) => { const el = $(id); if (el) setText(el, text); };

  function setAttr(el, name, value) {
    if (!el) return;
    if (value === null || value === undefined) {
      if (el.hasAttribute(name)) el.removeAttribute(name);
    } else if (el.getAttribute(name) !== String(value)) {
      el.setAttribute(name, String(value));
    }
  }

  function lastRow(rows, pred) {
    for (let i = rows.length - 1; i >= 0; i--) if (pred(rows[i])) return rows[i];
    return null;
  }

  /* Keep each measurement with its own denominator. Matches and cases observed are different units. */
  function casePair(ev) {
    const d = detailOf(ev);
    const matched = isRejected(ev) && numeric(d.matched) !== null;
    return { done: numeric(matched ? d.matched : d.completed), req: numeric(d.required), matched };
  }

  function stageCases(rows) {
    const last = lastRow(rows, ({ ev }) => {
      const d = detailOf(ev);
      return numeric(d.completed) !== null || numeric(d.required) !== null || numeric(d.matched) !== null;
    });
    return last ? casePair(last.ev) : { done: null, req: null, matched: false };
  }

  /*
   * Selected attempt only. A rebuild, verify or package event without its own generation joins the generation
   * of the preceding event of the attempt (the pipeline is sequential). `rows` keeps the reference stages (intake,
   * analyze) plus the latest generation of the rest; `all` also holds earlier generations.
   */
  function scopeEvents(events, attempt) {
    const all = [];
    let carry = null;
    for (const ev of events) {
      if (attempt !== null && Number(ev.attempt) !== attempt) continue;
      const stage = stageOf(ev);
      const g = numeric(detailOf(ev).generation);
      if (g !== null) carry = g;
      all.push({ ev, stage, gen: GEN_STAGES.has(stage) ? carry : null });
    }
    let latest = null;
    for (const r of all) if (r.gen !== null && (latest === null || r.gen > latest)) latest = r.gen;
    const rows = all.filter((r) => !GEN_STAGES.has(r.stage) || r.gen === latest);
    return { attempt, all, rows, gen: latest, former: all.length - rows.length };
  }

  function ordinalOf(id) {
    const runs = app.data && Array.isArray(app.data.runs) ? app.data.runs.filter(isObj) : [];
    const i = runs.findIndex((r) => r.id === id);
    return i < 0 ? null : runs.length - i;
  }

  /* Whole-run wording from a raw event status; the last raw status alone is never the outcome. */
  function runWord(status, live) {
    const s = lower(status);
    if (TERMINAL.has(s)) return s;
    if (s === 'running') return live ? 'running' : 'last known: running';
    if (s === 'completed') return live ? 'in progress' : 'last known: in progress';
    return s || 'unknown';
  }

  /* Everything the story's live/stale wording depends on besides the run itself. */
  function liveKey(run) {
    const d = app.data || {};
    return [isLive() ? 'conn' : 'lost', d.running === run.id ? 'run' : 'idle', d.builder_connected ? 'b' : '-',
      d.busy ? 'busy' : '-', ordinalOf(run.id), safeJson(d.catalog, false)].join(':');
  }

  function stepModel(name, rows, ctx) {
    const rs = rows.filter((r) => r.stage === name);
    const cases = stageCases(rs);
    if (!rs.length) {
      return { state: 'idle', text: ctx.terminal ? 'Not reached' : ctx.live ? 'Waiting' : 'Not reported', cases, gen: null };
    }
    const row = rs[rs.length - 1];
    const ev = row.ev, st = statusOf(ev);
    const out = (state, text) => ({ state, text, cases, gen: row.gen });
    if (st === 'verified') return name === 'package' && workerOf(ev) === 'Packager'
      ? out('good', 'Verified') : out('idle', 'Unconfirmed result');
    if (STOPPED.has(st)) return out('bad', STOP_WORD[st]);
    if (isRejected(ev)) return out('bad', 'Rejected');
    if (st === 'running') {
      const now = ctx.live && ev === ctx.last;
      return out(now ? 'live' : 'idle', now ? 'In progress' : 'Last reported running');
    }
    if (st !== 'completed') return out('idle', short(ev.status, 24) || 'Unknown');
    if (name === 'rebuild' && workerOf(ev) === 'Rust builder') {
      return out('idle', ctx.live ? 'Awaiting compiler' : 'Source returned');
    }
    if (name === 'rebuild' && workerOf(ev) === 'Compiler') return out('good', 'Built');
    if (name === 'verify') {
      const receipt = detailOf(ev).evidence;
      return out('good', isObj(receipt) && numeric(receipt.required) > 0 && receipt.matched === receipt.required
        ? 'All cases matched' : 'Comparison finished');
    }
    return out('good', 'Done');
  }

  function builderModel(out, rows, ctx) {
    const by = (name) => lastRow(rows, (r) => workerOf(r.ev) === name);
    const comp = by('Compiler'), ver = by('Verifier');
    if (comp && isRejected(comp.ev)) return out('bad', 'Source rejected by Compiler');
    if (ver && isRejected(ver.ev)) return out('bad', 'Source rejected by Verifier');
    if (comp) return out('good', 'Source returned');
    return out('idle', ctx.live ? 'Handoff: awaiting Compiler' : 'Source returned · Compiler not reported');
  }

  /* A role is credited only with events that name it; workerless events are never assigned by guess. */
  function roleModel(w, rows, ctx) {
    const mine = rows.filter((r) => workerOf(r.ev) === w.name);
    const stoppedHere = !!ctx.stop && stageOf(ctx.stop) === w.stage;
    const d = {};
    for (const { ev } of mine) {
      const x = detailOf(ev);
      for (const k of ATTRIBUTION) if (x[k] !== null && x[k] !== undefined) d[k] = x[k];
    }
    const label = STAGE_LABEL[w.stage];
    const out = (state, text, extra) => Object.assign({ state, text, mine, d, absence: '' }, extra);
    if (!mine.length) {
      const unnamed = rows.some((r) => r.stage === w.stage && workerOf(r.ev) === null);
      let absence;
      if (stoppedHere) absence = 'The run stopped at ' + label + ' before this role reported. The stop record names no role, so it is attributed to the coordinator, not to this role.';
      else if (unnamed) absence = 'Events exist for ' + label + ' but none names this role, so nothing is attributed to it.';
      else if (ctx.terminal) absence = 'The run ended before this role’s step (' + label + ') reported.';
      else if (ctx.live) absence = 'No event from this role yet; it is expected once ' + label + ' begins.';
      else absence = 'No event from this role is recorded, and the desk is not confirming a live run.';
      return out('idle', stoppedHere ? 'No report before stop' : ctx.terminal ? 'Not reached' : ctx.live ? 'Waiting' : 'Not reported', { absence });
    }
    const ev = mine[mine.length - 1].ev, st = statusOf(ev);
    if (st === 'verified') return out('good', 'Verified package');
    if (STOPPED.has(st)) return out('bad', STOP_WORD[st]);
    if (st === 'running') {
      if (ev === ctx.last) return ctx.live ? out('live', 'Working') : out('idle', 'Last reported running');
      return out('idle', stoppedHere ? 'Stopped after running' : 'Running record superseded');
    }
    if (isRejected(ev)) return out('bad', 'Rejected');
    if (st !== 'completed') return out('idle', short(ev.status, 24) || 'Unknown');
    if (w.name === 'Rust builder') return builderModel(out, rows, ctx);
    return out('good', w.name === 'Compiler' ? 'Built' : 'Done');
  }

  /*
   * Connector states of the team chart, read from the same recorded events as the rest of the story.
   * done: the receiving role has a recorded event. live: the receiver is working in a run the desk confirms is executing.
   * wait: a confirmed live run has not yet heard from the receiver. bad: the attempt stopped here. idle: nothing recorded;
   * the text says why. A disconnected desk never yields live or wait.
   */
  function flowOf(p) {
    const stopStage = p.kind === 'stopped' && p.last ? stageOf(p.last) : null;
    const reached = (name) => (name === COORD ? p.count > 0
      : name === 'Resolution' ? p.kind === 'verified' || p.kind === 'stopped' : p.roles[name].mine.length > 0);
    const finished = (name) => {
      if (name === COORD) return p.count > 0;
      const ev = p.roles[name].mine.at(-1)?.ev;
      return !!ev && !isRejected(ev) && ['completed', 'verified'].includes(statusOf(ev));
    };
    const ended = p.kind === 'verified' || p.kind === 'stopped';
    const links = {};
    for (const [id, from, to] of HANDOFFS) {
      const target = p.roles[to] || null;
      let state = 'idle', text = '';
      if (!target) {
        if (p.kind === 'verified') { state = 'done'; text = 'The Packager recorded the verified package.'; }
        else if (p.kind === 'stopped') { state = 'bad'; text = 'The attempt stopped before a package was verified.'; }
      } else if (target.state === 'live') {
        state = 'live';
        text = 'Handed off; ' + to + ' is working.';
      } else if (target.mine.length) {
        state = 'done';
        text = 'Handoff recorded; ' + to + ' reported.';
      } else if (stopStage && stopStage === WORKERS.find((w) => w.name === to).stage && finished(from)) {
        state = 'bad';
        text = 'The attempt stopped here; ' + to + ' never reported.';
      } else if (p.live && finished(from)) {
        state = 'wait';
        text = 'Handed off; waiting for ' + to + ' to report.';
      }
      if (!text) {
        text = ended ? 'Not reached before the attempt ended.'
          : p.live ? from + ' has not finished.' : 'No handoff recorded; the desk is not confirming a live run.';
      }
      links[id] = { state, text };
    }
    const stubs = {};
    for (const name of [COORD, ...WORKERS.map((w) => w.name), 'Resolution']) stubs[name] = reached(name) ? 'done' : 'idle';

    const rej = lastRow(p.all, (r) => isRejected(r.ev));
    const retried = p.gen !== null && p.gen > 1;
    const builder = p.roles['Rust builder'];
    let retry = {
      state: 'idle',
      text: 'No rejection recorded. A rejected build returns to the Rust builder with a diagnostic'
        + (p.limit !== null ? ' (generation limit ' + p.limit + ')' : '') + '.',
    };
    if (rej && p.kind === 'stopped') {
      retry = { state: 'bad', text: (workerOf(rej.ev) || 'A step') + ' rejected a build (' + seqOf(rej.ev) + ') and the attempt stopped.' };
    } else if (retried && builder.state === 'live') {
      retry = { state: 'live', text: 'Retrying: the Rust builder is working on generation ' + p.gen + '.' };
    } else if (rej || retried) {
      retry = {
        state: 'retry',
        text: (rej ? (workerOf(rej.ev) || 'A step') + ' rejected a build (' + seqOf(rej.ev) + '). ' : '')
          + (retried ? 'Latest generation: ' + p.gen + (p.limit !== null ? ' of ' + p.limit : '') + '.'
            : 'The coordinator asks the Rust builder for another generation.'),
      };
    }

    const stale = p.connected ? '' : ' · last known';
    let resolve;
    if (p.kind === 'verified') resolve = { state: 'good', text: 'Verified package ready' + stale };
    else if (p.kind === 'stopped') {
      resolve = { state: 'bad', text: STOP_WORD[p.lastStatus] + ' at ' + stageName(p.last) + ' · no verified package' + stale };
    } else if (p.live) resolve = { state: 'idle', text: 'Not resolved yet' };
    else resolve = { state: 'idle', text: p.last ? 'Not resolved · last known ' + stageName(p.last) : 'Not started' };

    const n = p.attempt === null ? '?' : p.attempt;
    const state = p.live ? { state: 'live', text: 'Live · following attempt ' + n }
      : !p.connected ? { state: 'bad', text: 'Disconnected · last known recorded state, not updating' }
      : { state: p.kind === 'verified' ? 'good' : p.kind === 'stopped' ? 'bad' : 'idle', text: 'Recorded · this attempt is not executing' };
    const record = {
      count: 'Attempt ' + n + ' · ' + plural(p.count, 'event', 'events') + (p.gen !== null ? ' · latest generation ' + p.gen : ''),
      last: p.last ? 'Latest ' + seqOf(p.last) + ' · ' + stageName(p.last) + ' · ' + p.lastStatus : 'No event recorded for this attempt',
    };
    return { links, stubs, retry, resolve, state, record };
  }

  function headline(p) {
    const stage = p.last ? stageName(p.last) : '';
    if (p.kind === 'verified') return 'Verified package ready';
    if (p.kind === 'stopped') return STOP_WORD[p.lastStatus] + ' at ' + stage;
    if (p.kind === 'working') return WORKERS.find(w => w.name === workerOf(p.last))?.activity || stage + ' in progress';
    if (p.kind === 'between' && p.lastStatus === 'queued') return 'Queued for intake';
    if (p.kind === 'between' && isRejected(p.last)) return 'Build rejected at ' + stage;
    if (p.kind === 'between') return 'Waiting for the next step';
    if (p.kind === 'stale') return 'Last known: ' + stage;
    return 'No events yet';
  }

  function summaryText(p) {
    const ev = p.last;
    if (!ev) return p.live ? 'Waiting for the first event from this attempt.' : 'No event has been recorded for this attempt.';
    const d = detailOf(ev), worker = workerOf(ev), stage = stageName(ev);
    const reason = present(d.reason) ? short(d.reason, 240) : 'No reason was recorded.';
    if (p.kind === 'stale') return 'Last report: ' + (worker || 'the coordinator') + ' reported ' + p.lastStatus + ' at ' + stage + '. '
      + (p.connected ? 'This run is not currently executing.' : 'The desk is disconnected; current activity is unknown.');
    if (p.kind === 'verified') {
      const receipt = d.evidence;
      return isObj(receipt) && numeric(receipt.required) > 0 && receipt.matched === receipt.required
        ? 'The Rust build matched the reference on all ' + countText(receipt.required) + ' named cases. The packager recorded the verified build.'
        : 'The packager recorded a verified build. This event does not report a complete match count.';
    }
    if (p.kind === 'stopped') return (worker ? worker + ' reported the stop' : 'The coordinator stopped the attempt') + ' at ' + stage + ': ' + reason;
    if (p.lastStatus === 'queued') return 'The coordinator queued this attempt. It is waiting for the first specialist report.';
    const c = casePair(ev);
    if (isRejected(ev)) return (worker || 'The coordinator') + ' rejected this build. ' + reason;
    if (p.kind === 'between') return (worker || 'The coordinator') + ' finished its step. No new worker has reported yet.';
    if (c.done !== null && c.req !== null) return (worker || 'The coordinator') + ' recorded outputs for ' + countText(c.done)
      + ' of ' + countText(c.req) + (stageOf(ev) === 'analyze' ? ' reference cases.' : ' candidate cases. This count is not a match result.');
    return (worker || 'The coordinator') + ' reported work in progress' + (p.gen === null ? '' : ' on build ' + p.gen) + '. No case count was reported.';
  }

  function expectation(ev, p) {
    const w = workerOf(ev), st = statusOf(ev);
    const left = p.gen !== null && p.limit !== null ? p.limit - p.gen : null;
    const retry = left === 0
      ? 'No generation remains, so the attempt will stop.'
      : 'The Rust builder is asked for another generation' + (p.gen !== null ? ' (' + (p.gen + 1) + (p.limit !== null ? ' of ' + p.limit : '') + ')' : '') + ' using the diagnostic.';
    if (st === 'queued') return 'The file analyst identifies the reference file.';
    if (st === 'running') {
      if (w === 'File analyst') return 'The File analyst finishes identifying the reference, then the Behavior analyst observes it.';
      if (w === 'Behavior analyst') return 'Reference observation completes, then the Rust builder is asked for source.';
      if (w === 'Rust builder') return 'The Rust builder returns source, then the Compiler builds it.';
      if (w === 'Compiler') return 'The Compiler finishes; if it builds, the Verifier compares the program with the reference cases.';
      if (w === 'Verifier') return 'If every case matches, the packager bundles the Rust source, executable, and verification receipt.';
      return 'The current step finishes and the coordinator schedules the next one.';
    }
    if (isRejected(ev)) return 'The result was rejected. ' + retry;
    if (w === 'File analyst') return 'The Behavior analyst observes the reference.';
    if (w === 'Behavior analyst') {
      return p.builder === false
        ? 'The Rust builder is asked for source. No builder is connected, so the run will block at rebuild.'
        : 'The Rust builder is asked for source (generation 1).';
    }
    if (w === 'Rust builder') return 'The Compiler builds the returned source.';
    if (w === 'Compiler') return 'The Verifier compares the built program with the reference cases.';
    if (w === 'Verifier') return 'The Packager creates the package. Only its verified record counts as verification.';
    return 'The coordinator schedules the next step.';
  }

  function nextText(p) {
    if (p.kind === 'verified') return 'Download the package to inspect the source, executable, and verification receipt.';
    if (p.kind === 'stopped') return 'No package was verified. A retry starts a new attempt.';
    if (!p.last) return p.live ? 'Waiting for the file analyst.' : 'No next step is confirmed.';
    return (p.live ? '' : 'Last expected step: ') + expectation(p.last, p);
  }

  function mapText(p) {
    let state = 'idle', text;
    if (p.kind === 'verified') { state = 'good'; text = 'Finished'; }
    else if (p.kind === 'stopped') {
      state = 'bad';
      text = 'Stopped (' + p.lastStatus + ') at ' + stageName(p.last) + (workerOf(p.last) === null ? '; the stop record names no worker' : '');
    } else if (p.kind === 'working') { state = 'live'; text = 'Assigned: ' + (workerOf(p.last) || stageName(p.last)); }
    else if (p.kind === 'between') { text = p.lastStatus === 'queued' ? 'Queued' : 'Awaiting handoff'; }
    else if (p.kind === 'stale') text = 'Not confirmed running; last known ' + stageName(p.last) + ' ' + p.lastStatus;
    else text = p.live ? 'Waiting for the first report' : 'No events recorded';
    if (!p.connected && p.kind !== 'verified' && p.kind !== 'stopped') text = 'Disconnected: showing last known state. ' + text;
    if (p.busy) text += '. Operator action in progress';
    return { state, text };
  }

  function project(run, events, scope) {
    const d = app.data || {};
    const rows = scope.rows;
    const last = rows.length ? rows[rows.length - 1].ev : null;
    const lastStatus = last ? statusOf(last) : lower(run.status);
    const connected = isLive();
    const terminal = TERMINAL.has(lastStatus);
    const live = connected && d.running === run.id && !terminal;
    let kind;
    if (!last) kind = 'empty';
    else if (lastStatus === 'verified' && stageOf(last) === 'package' && workerOf(last) === 'Packager') kind = 'verified';
    else if (STOPPED.has(lastStatus)) kind = 'stopped';
    else if (!live) kind = 'stale';
    else kind = lastStatus === 'running' ? 'working' : 'between';
    const manifest = isObj(run.manifest) ? run.manifest : {};
    const p = {
      id: run.id, attempt: scope.attempt, gen: scope.gen, former: scope.former, all: scope.all, rows, last, lastStatus,
      count: scope.all.length, prior: events.length - scope.all.length,
      lastWorker: lastRow(rows, (r) => workerOf(r.ev) !== null),
      kind, live, connected, working: kind === 'working' && workerOf(last) !== null,
      builder: d.builder_connected, busy: !!d.busy, manifest, limit: numeric(manifest.generation_limit),
      catalog: Array.isArray(d.catalog) ? d.catalog.filter(isObj) : [],
      currentStage: last && !terminal ? stageOf(last) : null,
      steps: {}, roles: {},
    };
    const ctx = { live, terminal, last, stop: last && STOPPED.has(lastStatus) ? last : null };
    for (const name of STAGES) p.steps[name] = stepModel(name, rows, ctx);
    for (const w of WORKERS) p.roles[w.name] = roleModel(w, rows, ctx);
    p.word = lastStatus === 'verified' && kind !== 'verified' ? 'Not confirmed'
      : last && isRejected(last) ? 'Build rejected' : last ? runWord(lastStatus, live) : (live ? 'starting' : 'no events');
    const ord = ordinalOf(run.id);
    p.title = (ord === null ? 'Run ' + short(run.id, 8) : 'Run ' + ord) + ' · ' + headline(p);
    p.summary = summaryText(p);
    p.next = nextText(p);
    p.map = mapText(p);
    p.flow = flowOf(p);
    return p;
  }

  /* One-line recorded facts shown on each chart node. */
  function roleMeta(r) {
    if (!r.mine.length) return 'No events this attempt';
    const ev = r.mine[r.mine.length - 1].ev;
    return plural(r.mine.length, 'event', 'events') + ' · latest ' + (present(ev.seq) ? '#' + fmt(ev.seq) : 'unnumbered') + ' ' + statusOf(ev);
  }

  /* Model vs deterministic: the declared kind plus only what events reported; nothing is guessed. */
  function roleModelLine(w, r) {
    const d = r.d;
    if (w.kind === 'Model') {
      if (present(d.actual_model)) return 'Model reported: ' + short(d.actual_model, 40);
      if (present(d.requested_model)) return 'Requested ' + short(d.requested_model, 40) + ' · actual not reported';
      return r.mine.length ? 'Model not reported' : 'No model reported yet';
    }
    return present(d.actual_model) || present(d.requested_model) ? 'Check: reports a model, but this role is deterministic' : 'No model';
  }

  function paintFlow(p, b) {
    const f = p.flow;
    for (const [id, from] of HANDOFFS) {
      const state = f.links[id].state;
      for (const el of b.links[id]) setAttr(el, 'data-state', state);
      setAttr(from === COORD ? $('flow-coord') : b.roles[from].li, 'data-out', state);
    }
    for (const el of b.links.compile) setAttr(el, 'data-retry', f.retry.state);
    for (const [id, name] of STUBS) setAttr(b.links[id][0], 'data-state', f.stubs[name]);
    setAttr(b.links['stub-resolve'][0], 'data-state', f.stubs.Resolution);
    put('flow-state', f.state.text);
    setAttr($('flow-state'), 'data-state', f.state.state);
    put('flow-record-count', f.record.count);
    put('flow-record-last', f.record.last);
    put('flow-resolve-state', f.resolve.text);
    setAttr($('flow-resolve'), 'data-state', f.resolve.state);
    const lines = HANDOFFS.map(([id, from, to]) => from + ' → ' + to + ': ' + f.links[id].text);
    lines.push('Retry path: ' + f.retry.text, 'Run record: ' + f.record.count + '. ' + f.record.last + '.');
    lines.forEach((text, i) => setText(b.flowText[i], text));
  }

  function buildBriefing() {
    const steps = {}, roles = {};
    for (const name of STAGES) {
      const e = { name: h('span', { class: 'step-name', text: STAGE_LABEL[name] }), status: h('span', { class: 'step-status' }),
        note: h('small', { class: 'step-note' }), bar: h('progress', { max: '1', value: '0', hidden: true }) };
      e.li = h('li', { 'data-stage': name }, e.name, e.status, e.note, e.bar);
      $('step-track').append(e.li);
      steps[name] = e;
    }
    for (const [i, w] of WORKERS.entries()) {
      const status = h('span', { class: 'role-status' });
      const meta = h('span', { class: 'role-meta' });
      const model = h('span', { class: 'role-model' });
      const btn = h('button', { type: 'button', class: 'role-node', 'data-fkey': 'role:' + w.name, 'aria-controls': 'role-detail' },
        h('span', { class: 'role-step', 'aria-hidden': 'true', text: String(i + 1) }),
        h('span', { class: 'role-name', text: w.name }),
        h('span', { class: 'role-kind' },
          h('span', { class: 'kind-tag', 'data-kind': w.kind === 'Model' ? 'model' : 'det', text: w.kind }), ' ' + w.task),
        status, meta, model);
      const li = h('li', { 'data-worker': w.name, role: 'listitem' }, btn);
      $('role-map').append(li);
      roles[w.name] = { li, btn, status, meta, model };
    }
    /* Connector cells are static markup; group them by link id so one state paints every cell of a link. */
    const links = {};
    for (const el of document.querySelectorAll('#flow-board [data-link]')) {
      (links[el.getAttribute('data-link')] ||= []).push(el);
    }
    const flowText = [];
    for (let i = 0; i < HANDOFFS.length + 2; i++) {
      const li = h('li');
      $('flow-text').append(li);
      flowText.push(li);
    }
    return { steps, roles, links, flowText };
  }

  function inspectedRole() {
    const last = app.proj?.lastWorker;
    return app.role || (last && workerOf(last.ev));
  }

  function syncRoleSelection() {
    if (!app.brief) return;
    for (const w of WORKERS) setAttr(app.brief.roles[w.name].btn, 'aria-pressed', inspectedRole() === w.name ? 'true' : 'false');
  }

  function paintBriefing(p) {
    if (!app.brief) app.brief = buildBriefing();
    const b = app.brief;
    if (!b) return;
    for (const name of STAGES) {
      const m = p.steps[name], e = b.steps[name];
      setAttr(e.li, 'data-state', m.state);
      setAttr(e.li, 'aria-current', p.currentStage === name ? 'step' : null);
      setText(e.status, m.text);
      const paired = own(CASE_LABEL, name) && m.cases.done !== null && m.cases.req !== null && m.cases.req > 0;
      const note = [];
      if (own(CASE_LABEL, name) && (m.cases.done !== null || m.cases.req !== null)) {
        note.push((m.cases.done === null ? '?' : countText(m.cases.done)) + ' of ' + (m.cases.req === null ? '?' : countText(m.cases.req))
          + (m.cases.matched ? ' matched' : name === 'analyze' ? ' recorded' : ' checked'));
      }
      if (m.gen !== null) note.push('generation ' + m.gen);
      setText(e.note, note.join(' · '));
      setHidden(e.note, note.length === 0);
      setHidden(e.bar, !paired);
      if (paired) {
        e.bar.max = m.cases.req;
        e.bar.value = Math.min(m.cases.done, m.cases.req);
        setAttr(e.bar, 'aria-label', (m.cases.matched ? 'Cases matched: ' : 'Cases observed: ') + CASE_LABEL[name]);
      }
    }
    for (const w of WORKERS) {
      const r = p.roles[w.name], e = b.roles[w.name];
      setAttr(e.li, 'data-state', r.state);
      setText(e.status, r.text);
      setText(e.meta, roleMeta(r));
      setText(e.model, roleModelLine(w, r));
    }
    paintFlow(p, b);
    syncRoleSelection();
  }

  function roleRows(w, p) {
    const r = p.roles[w.name];
    const rows = [];
    const num = (ev) => (present(ev.seq) ? '#' + fmt(ev.seq) : '?');
    rows.push(['Stage', STAGE_LABEL[w.stage] + ' (step ' + (STAGES.indexOf(w.stage) + 1) + ' of ' + STAGES.length + ')']);
    rows.push(['Assignment', w.task]);
    rows.push(['State', r.text + (!p.connected ? ' · last known' : '')]);
    let scopeText = 'Attempt ' + (p.attempt === null ? '?' : p.attempt);
    if (GEN_STAGES.has(w.stage) && p.gen !== null) scopeText += ' · latest generation ' + p.gen;
    if (p.former) scopeText += ' · ' + plural(p.former, 'earlier-generation event', 'earlier-generation events') + ' excluded (see Event evidence)';
    rows.push(['Attempt', scopeText]);
    if (!r.mine.length) {
      rows.push(['Recorded events', 'None naming this role in the scope shown.']);
      rows.push(['Why', r.absence]);
    } else {
      const seqs = r.mine.map((x) => num(x.ev));
      rows.push(['Recorded events', (seqs.length > 12 ? '… ' : '') + seqs.slice(-12).join(', ')]);
      const ev = r.mine[r.mine.length - 1].ev;
      rows.push(['Latest event', num(ev) + ' · ' + statusOf(ev) + ' · ' + fmtTime(ev.at).text]);
      const reasonRow = lastRow(r.mine, (x) => present(detailOf(x.ev).reason));
      if (reasonRow) rows.push(['Reason', short(detailOf(reasonRow.ev).reason, 300)]);
      if (r.mine.some((x) => present(detailOf(x.ev).worker) && ['skill', 'tool', 'image'].some((k) => present(detailOf(x.ev)[k])))) {
        rows.push(['Reported by events', invokedText({ d: r.d, gen: null }).replace(/^Reported by events: /, '')]);
      }
    }
    const m = r.mine.length ? modelReport(r.d) : null;
    if (m || w.name === 'Rust builder') rows.push(['Model', m ? m.text : 'No model reported.']);
    if (m && m.tone === 'warn') rows.push(['Check', 'The reported model or effort differs from what was requested, or conflicts with the deterministic flag.']);
    if (w.name === 'Rust builder' && present(p.manifest.requested_model)) {
      rows.push(['Manifest request', 'model ' + short(p.manifest.requested_model, 60) + (present(p.manifest.requested_effort) ? ', effort ' + short(p.manifest.requested_effort, 30) : '') + ' (a request made before the run, not a report)']);
    }
    const cat = p.catalog.find((c) => c.role === w.name);
    if (cat) rows.push(['Declared assignment', short(cat.skill, 80) + ' (assignment only; not proof of invocation)']);
    return rows;
  }

  function renderRoleDetail() {
    const box = $('role-detail');
    if (!box) return;
    const w = app.proj && WORKERS.find(x => x.name === inspectedRole());
    if (!w) {
      if (box.firstChild) box.replaceChildren();
      box._sig = '';
      return;
    }
    const rows = roleRows(w, app.proj);
    const sig = safeJson([w.name, rows], false);
    if (box._sig === sig) return;
    box._sig = sig;
    box.replaceChildren(
      h('p', null, h('b', { text: w.name }), ' · ' + STAGE_LABEL[w.stage] + ' step'),
      h('dl', { class: 'kv' }, rows.map(([k, v]) => h('div', null, h('dt', { text: k }), h('dd', { text: v })))));
  }

  function renderStory(p) {
    app.proj = p;
    put('run-h', p.title);
    put('run-summary', p.summary);
    put('run-next', p.next);
    put('map-state', p.map.text);
    setAttr($('map-state'), 'data-state', p.map.state);
    setAttr($('run-view'), 'data-working', p.working ? 'true' : 'false');
    paintBriefing(p);
    renderRoleDetail();
  }

  /* Clear everything a changed or absent run could leave on screen. */
  function resetBriefing() {
    for (const id of ['run-h', 'run-summary', 'run-next', 'map-state']) put(id, '');
    setAttr($('map-state'), 'data-state', null);
    setAttr($('run-view'), 'data-working', 'false');
    if (app.brief) {
      for (const e of Object.values(app.brief.steps)) {
        setAttr(e.li, 'data-state', 'idle');
        setAttr(e.li, 'aria-current', null);
        setText(e.status, '');
        setText(e.note, '');
        setHidden(e.bar, true);
      }
      for (const e of Object.values(app.brief.roles)) {
        setAttr(e.li, 'data-state', 'idle');
        setAttr(e.li, 'data-out', null);
        setAttr(e.btn, 'aria-pressed', 'false');
        setText(e.status, '');
        setText(e.meta, '');
        setText(e.model, '');
      }
      for (const els of Object.values(app.brief.links)) {
        for (const el of els) {
          setAttr(el, 'data-state', 'idle');
          if (el.hasAttribute('data-retry')) setAttr(el, 'data-retry', 'idle');
        }
      }
      for (const li of app.brief.flowText) setText(li, '');
    }
    for (const id of ['flow-state', 'flow-record-count', 'flow-record-last', 'flow-resolve-state']) put(id, '');
    setAttr($('flow-state'), 'data-state', null);
    setAttr($('flow-resolve'), 'data-state', 'idle');
    setAttr($('flow-coord'), 'data-out', null);
    renderRoleDetail();
  }

  /* A poll failure or reconnect changes what "live" means even when the data is identical. */
  function renderStale() {
    if (!app.data) return;
    renderRuns();
    renderRunView();
    renderFeedState();
  }

  /* --- read-only Q&A: composed here from the projection; nothing is sent anywhere --- */

  function chatReset() {
    const log = $('chat-log'), input = $('chat-question');
    if (log && log.firstChild) log.replaceChildren();
    if (input && input.value) input.value = '';
  }

  function answer(question, p) {
    const text = question.toLowerCase();
    const named = WORKERS.find(w => text.includes(w.name.toLowerCase()));
    const hit = TOPICS.find(([topic, re]) => (topic !== 'specialist' || named) && re.test(text));
    if (!hit) return UNSUPPORTED;
    let body, ev = p.last;
    const w = ev ? workerOf(ev) : null;
    switch (hit[0]) {
      case 'specialist': {
        const r = p.roles[named.name];
        ev = r.mine.at(-1)?.ev;
        body = ev ? named.name + ' — ' + r.text + '. ' + modelReport(r.d).text + '.'
          : named.name + ': no event from this specialist in the current attempt and build.';
        break;
      }
      case 'status':
        body = summaryText(p);
        break;
      case 'next':
        body = p.next;
        break;
      case 'worker': {
        const lw = p.lastWorker;
        if (!lw) { body = 'No worker has reported in this attempt yet.'; break; }
        const lev = lw.ev, name = workerOf(lev);
        ev = lev;
        body = (p.working ? '' : 'No worker is confirmed active. ') + name + ' last reported ' + statusOf(lev) + ' at ' + stageName(lev)
          + (lw.gen !== null ? ' (generation ' + lw.gen + ')' : '') + '. '
          + (p.roles[name] ? modelReport(p.roles[name].d).text + '.' : '');
        if (p.last && p.last !== lev) body += ' The latest event (' + seqOf(p.last) + ', ' + p.lastStatus + ') names no worker.';
        break;
      }
      case 'progress': {
        const parts = STAGES.map((n) => STAGE_LABEL[n] + ': ' + p.steps[n].text.toLowerCase());
        const pairs = [];
        for (const n of Object.keys(CASE_LABEL)) {
          const c = p.steps[n].cases;
          if (c.done !== null && c.req !== null) pairs.push(CASE_LABEL[n] + ': ' + countText(c.done) + ' of ' + countText(c.req) + (c.matched ? ' matched' : ' checked'));
        }
        body = 'Steps — ' + parts.join('; ') + '. ' + (pairs.length ? 'Recorded counts: ' + pairs.join('; ') + '. ' : '')
          + 'No percentage or time estimate is recorded.';
        break;
      }
      case 'failure': {
        const rej = lastRow(p.all, (r) => isRejected(r.ev) && (!named || workerOf(r.ev) === named.name));
        if (p.kind === 'stopped' && !(named && rej)) { body = summaryText(p); break; }
        if (rej) {
          ev = rej.ev;
          const d = detailOf(rej.ev);
          const outcome = p.kind === 'stopped' ? 'The attempt stopped.' : p.kind === 'verified' ? 'The attempt was later verified.' : 'The attempt has not ended in failure.';
          body = outcome + ' The latest rejection: ' + (workerOf(rej.ev) || 'a step') + ' result'
            + (rej.gen !== null ? ' for generation ' + rej.gen : '') + (present(d.reason) ? ' — ' + short(d.reason, 240) : '') + '.'
            + (rej.gen !== null && rej.gen !== p.gen ? ' That generation is no longer the latest.' : '');
        } else {
          body = named ? 'No rejection from ' + named.name + ' is recorded for this attempt.'
            : 'No failure, block, cancellation or rejection is recorded for this attempt.';
        }
        break;
      }
      case 'verified':
        body = p.kind === 'verified'
          ? 'Yes. ' + summaryText(p)
          : 'Not verified. Only a Packager “verified” event counts; matched cases alone do not. Current state: ' + p.word + '.';
        break;
      default: {
        if (!p.last) { body = 'No event is recorded for this attempt.'; break; }
        body = 'The latest recorded event is ' + seqOf(p.last) + ' (' + stageName(p.last) + ', ' + p.lastStatus
          + (w ? ', ' + w : ', no worker named') + '): ' + eventSummary(p.last) + '. Open Event evidence for the full record.';
      }
    }
    const label = ev
      ? ' Evidence: ' + short(seqOf(ev), 60) + ', attempt ' + short(ev.attempt, 24) + '.'
      : ' No recorded event supports an answer yet, so I can’t say more.';
    const statement = (p.connected ? '' : 'Last known (desk disconnected): ') + body;
    const room = ANSWER_MAX - label.length;
    return (statement.length > room ? statement.slice(0, room - 1) + '…' : statement) + label;
  }

  function ask(raw) {
    const log = $('chat-log');
    if (!log) return;
    const q = String(raw).trim().slice(0, QUESTION_MAX);
    if (!q) return;
    const run = currentRun();
    const p = run && app.proj && app.proj.id === run.id ? app.proj : null;
    const a = p ? answer(q, p) : 'Open a run first; answers come only from the selected run’s recorded events.';
    log.append(h('div', { class: 'chat-turn' },
      h('p', { class: 'chat-q' }, h('b', { text: 'You: ' }), q),
      h('p', { class: 'chat-a' }, h('b', { text: 'Desk: ' }), a)));
    while (log.childElementCount > CHAT_TURNS) log.firstElementChild.remove();
    log.scrollTop = log.scrollHeight;
  }

  /* ---------- run activity feed: recorded events of the CURRENT run and attempt only, read-only ---------- */
  /*
   * Fed only by accepted polls (render) and by the operator's own Resume / Latest clicks, which re-read the last
   * accepted poll. A failed poll never touches the feed. Entries are built from structured fields and curated text
   * only: no prompt, model reasoning, raw detail payload or free-form server string is ever shown here. Q&A is separate.
   */
  const FEED_MAX = 300;
  const FEED_EDGE = 24;
  const FEED_STATUS = new Set(['queued', 'running', 'completed', 'verified', 'failed', 'blocked', 'cancelled']);
  /* Exact reason strings the desk is known to emit that carry no model or tool output. */
  const FEED_REASONS = new Set([
    'Queued on the pinned release.', 'Original manifest retained.',
    'Coordinator restarted. Retry with the original manifest.',
    'Artifact changed during verification.', 'Reference failed the predeclared live-pipe outcome.',
  ]);

  function blankFeed() {
    return { key: null, opened: false, seen: new Set(), trimmed: 0, fresh: 0, unseen: 0, paused: false, pending: 0, terminal: false };
  }

  /* Purge entries and every counter; a new run, attempt, retry or empty state starts clean (and un-paused). */
  function feedReset() {
    const log = $('feed-log');
    const f = app.feed;
    if (f.key === null && !f.paused && !f.seen.size && !(log && log.firstChild)) return;
    app.feed = blankFeed();
    if (log && log.firstChild) log.replaceChildren();
    renderFeedState();
  }

  function feedAtBottom(log) {
    return log.scrollHeight - log.scrollTop - log.clientHeight <= FEED_EDGE;
  }

  /* Only a recognised reason is shown; anything else (compiler output, exception text, model text) stays out. */
  function curatedReason(raw) {
    if (typeof raw !== 'string') return null;
    const s = raw.trim();
    if (FEED_REASONS.has(s)) return s;
    if (/^Run could not start \([A-Za-z]{1,40}\)\.$/.test(s)) return s;
    const m = /^(\d{1,4}) case\(s\) differed(?::|$)/.exec(s);
    return m ? m[1] + ' case(s) differed.' : null;
  }

  function feedReport(ev, worker, role) {
    const st = statusOf(ev);
    const who = worker || 'A step';
    let text;
    if (isRejected(ev)) text = who + ' rejected this result.';
    else if (st === 'verified') text = stageOf(ev) === 'package' && workerOf(ev) === 'Packager' ? 'The Packager recorded a verified build.' : 'A verified status was recorded.';
    else if (st === 'queued') text = 'The attempt was queued.';
    else if (st === 'running') text = role ? role.activity + '.' : who + ' reported work in progress.';
    else if (st === 'completed') text = who + ' finished its step.';
    else if (STOPPED.has(st)) text = STOP_WORD[st] + ' at ' + (STAGE_LABEL[stageOf(ev)] || 'an unreported step') + '.';
    else text = 'The status is not recognised.';
    const d = detailOf(ev);
    if (!present(d.reason)) return text;
    const reason = curatedReason(d.reason);
    return text + ' ' + (reason ? 'Reason: ' + reason : 'A reason was recorded; open Event evidence to read it.');
  }

  function feedItem(x, origin) {
    const ev = x.r.ev;
    const stage = STAGE_LABEL[stageOf(ev)] || 'Unreported step';
    const st = statusOf(ev);
    const status = isRejected(ev) ? 'rejected' : FEED_STATUS.has(st) ? st : 'unrecognised';
    const w = workerOf(ev);
    const role = WORKERS.find((k) => k.name === w && k.stage === stageOf(ev));
    const worker = role ? role.name : w === null ? null : 'Unlisted worker';
    const seq = numeric(ev.seq);
    const att = numeric(ev.attempt);
    const meta = [];
    if (att !== null) meta.push('Attempt ' + att);
    if (x.r.gen !== null) meta.push('Generation ' + x.r.gen);
    const c = casePair(ev);
    const word = c.matched ? 'matched' : stageOf(ev) === 'analyze' ? 'recorded' : 'checked';
    if (c.done !== null && c.req !== null) meta.push(countText(c.done) + ' of ' + countText(c.req) + ' cases ' + word);
    else if (c.done !== null) meta.push(countText(c.done) + ' cases ' + word);
    const li = h('li', { 'data-feed-key': x.k, 'data-origin': origin, 'data-stage': stageOf(ev) || null, 'data-state': tone(status) },
      h('div', { class: 'feed-head' },
        h('span', { class: 'feed-seq', text: seq === null ? 'Event (unnumbered)' : 'Event #' + countText(seq) }),
        h('span', { class: 'feed-origin', text: origin === 'live' ? 'New update' : 'Recorded' }),
        timeEl(ev.at)),
      h('p', { class: 'feed-line' }, stage + ' · ' + (worker || 'no worker named') + ' · ',
        h('span', { class: chipClass(status), text: status })));
    if (meta.length) li.append(h('div', { class: 'feed-meta', text: meta.join(' · ') }));
    li.append(h('p', { class: 'feed-report', text: feedReport(ev, worker, role) }));
    return li;
  }

  /* Append in observed order (newest last). Follows the bottom only if the reader is already there. */
  function feedAppend(log, f, list, origin) {
    const follow = !log.firstChild || feedAtBottom(log);
    const cut = Math.max(0, list.length - FEED_MAX);
    for (const x of list) f.seen.add(x.k);
    const frag = document.createDocumentFragment();
    for (const x of list.slice(cut)) frag.append(feedItem(x, origin));
    log.append(frag);
    f.trimmed += cut;
    while (log.childElementCount > FEED_MAX) {
      log.firstElementChild.remove();
      f.trimmed++;
    }
    if (origin === 'live') f.fresh += list.length;
    if (follow) {
      log.scrollTop = log.scrollHeight;
      f.unseen = 0;
    } else {
      f.unseen += list.length - cut;
    }
  }

  /* Called on an accepted poll, and when the operator resumes or asks for the latest. Never duplicates an entry. */
  function feedSync() {
    const log = $('feed-log');
    if (!log) return;
    const p = app.proj;
    if (!p) { feedReset(); return; }
    const { attempt, all: rows } = p;
    const key = p.id + '\u0000' + (attempt === null ? '' : attempt);
    if (app.feed.key !== key) { feedReset(); app.feed.key = key; }
    let f = app.feed;
    if (!f.paused && f.applied === rows) { renderFeedState(); return; }
    const keyed = rows.map((r, i) => ({ r, k: attempt + ':' + (numeric(r.ev.seq) !== null ? r.ev.seq : 'u' + i) }));
    const fresh = () => {
      const mark = new Set();
      return keyed.filter((x) => {
        if (f.seen.has(x.k) || mark.has(x.k)) return false;
        mark.add(x.k);
        return true;
      });
    };
    let list = fresh();
    if (f.paused) {
      f.pending = list.length;
      renderFeedState();
      return;
    }
    f.pending = 0;
    if (f.seen.size) {
      const have = new Set(keyed.map((x) => x.k));
      for (const k of f.seen) {
        if (have.has(k)) continue;
        // The desk no longer reports an entry already shown: purge and rebuild from what it reports now.
        feedReset();
        app.feed.key = key;
        f = app.feed;
        list = fresh();
        break;
      }
    }
    f.terminal = rows.length > 0 && TERMINAL.has(statusOf(rows[rows.length - 1].ev));
    if (list.length) feedAppend(log, f, list, f.opened ? 'live' : 'recorded');
    f.opened = true;
    f.applied = rows;
    renderFeedState();
  }

  function feedSetPaused(paused) {
    const f = app.feed;
    if (!f.key || f.paused === paused) return;
    f.paused = paused;
    f.pending = 0;
    if (paused) renderFeedState();
    else stable(feedSync);
  }

  /* Catch up the feed only (resuming it if paused) and scroll the feed, never the page. */
  function feedLatest() {
    const log = $('feed-log');
    const f = app.feed;
    if (!log || !f.key) return;
    if (f.paused) f.paused = false;
    stable(feedSync);
    log.scrollTop = log.scrollHeight;
    app.feed.unseen = 0;
    renderFeedState();
  }

  function renderFeedState() {
    const f = app.feed;
    const run = currentRun();
    const live = isLive();
    const n = f.seen.size;
    let state, text;
    if (!f.key) {
      state = 'idle';
      text = 'Select a run to follow its recorded activity.';
    } else {
      const executing = live && !!run && app.data.running === run.id && !f.terminal;
      const counts = plural(n, 'event', 'events') + (f.fresh ? ' · ' + f.fresh + ' new since opened' : '')
        + (f.trimmed ? ' · ' + f.trimmed + ' earlier not shown (see Event evidence)' : '');
      if (f.paused) {
        state = 'paused';
        text = 'Paused · ' + (f.pending ? plural(f.pending, 'new event', 'new events') + ' waiting; resume to backfill' : 'nothing new waiting')
          + (live ? '' : ' · desk disconnected') + ' · ' + counts;
      } else if (!live) {
        state = 'stale';
        text = 'Disconnected · last known activity, not updating' + (app.lastOk ? ' (last read ' + app.lastOk + ')' : '') + ' · ' + counts;
      } else if (executing) {
        state = 'live';
        text = n ? 'Live · following this attempt · ' + counts : 'Live · waiting for the first event from this attempt';
      } else {
        state = 'recorded';
        text = n ? 'Recorded · this attempt is not executing · ' + counts : 'Recorded · no event has been recorded for this attempt';
      }
    }
    const stateEl = $('feed-state');
    if (stateEl) {
      setText(stateEl, text);
      setAttr(stateEl, 'data-state', state);
    }
    setAttr($('feed-log'), 'data-state', state);
    const tog = $('feed-toggle');
    if (tog) {
      setText(tog, f.paused ? 'Resume feed' : 'Pause feed');
      setDisabled(tog, !f.key, 'Select a run first.');
    }
    const latest = $('feed-latest');
    if (latest) {
      const waiting = f.paused ? f.pending : f.unseen;
      setText(latest, waiting ? 'Jump to latest (' + waiting + ' new)' : 'Jump to latest');
      setDisabled(latest, !f.key || (!n && !f.pending), 'No activity to show yet.');
    }
  }

  /* ---------- render ---------- */

  function render(data) {
    app.data = data;
    const runs = Array.isArray(data.runs) ? data.runs : [];
    if (app.selectedId && data.run === null && !runs.some((r) => r && r.id === app.selectedId)) setSelected(null);
    stable(() => {
      renderConn();
      renderAlerts();
      renderRuns();
      renderRunView();
      feedSync();
      renderReleases();
      renderCatalog();
      syncControls();
    });
  }

  function renderConn() {
    const el = $('conn');
    const d = app.data;
    let state, text;
    if (app.link === 'lost') { state = 'lost'; text = 'Disconnected'; }
    else if (!isLive()) { state = 'connecting'; text = 'Connecting…'; }
    else if (app.acting || d.busy) { state = 'busy'; text = 'Busy: operator action in progress'; }
    else if (d.running) { state = 'busy'; text = 'Run in progress'; }
    else { state = 'ready'; text = 'Connected'; }
    if (el.dataset.state !== state) el.dataset.state = state;
    setText(el, text);
    setText($('updated'), !app.lastOk ? '' : app.link === 'lost' ? 'Last read ' + app.lastOk : 'Updated ' + app.lastOk);
  }

  /* Before the first successful read there is no run data; say why the list is empty. */
  function renderPending() {
    if (app.data) return;
    const pick = $('run-pick');
    setText(pick, app.link === 'lost'
      ? 'Run history is unavailable until the desk reconnects.'
      : 'Loading run history…');
    setHidden(pick, false);
  }

  function renderAlerts() {
    const msgs = [];
    if (app.alerts.poll) msgs.push(['poll', app.alerts.poll]);
    if (app.alerts.action) msgs.push(['action', app.alerts.action]);
    const ue = app.data && app.data.update_error;
    if (ue) msgs.push(['update', 'Release update problem: ' + fmt(ue) + '. Press Stage update to retry; if it repeats, check the host’s update log.']);
    const sig = safeJson(msgs, false);
    if (sig === app.sig.alerts) return;
    app.sig.alerts = sig;
    const box = $('alerts');
    box.replaceChildren(...msgs.map(([kind, text]) => {
      const p = h('p', { class: 'error', 'data-kind': kind }, text);
      if (kind === 'action') {
        const b = h('button', { type: 'button', 'data-fkey': 'dismiss-action' }, 'Dismiss');
        b.addEventListener('click', () => {
          app.alerts.action = null;
          stable(renderAlerts);
          $('main').focus({ preventScroll: true });
        });
        p.append(' ', b);
      }
      return p;
    }));
  }

  function renderRuns() {
    const d = app.data;
    const runs = (Array.isArray(d.runs) ? d.runs : []).filter(isObj);
    const run = currentRun();
    const shown = run ? run.id : app.selectedId;
    setHidden($('zero'), runs.length > 0);
    setHidden($('run-list'), runs.length === 0);
    const ctx = {
      shown,
      ord: new Map(runs.map((r, i) => [r.id, runs.length - i])),
      running: d.running,
      connected: isLive(),
    };
    reconcile($('run-list'), runs, (r) => 'run:' + r.id,
      (r) => {
        const btn = h('button', { type: 'button', class: 'run-btn', 'data-fkey': 'run:' + r.id },
          h('b', { class: 'run-title' }), h('code', { class: 'rid' }), chip(r.status), h('small', { class: 'ratt' }));
        btn.addEventListener('click', () => {
          if (app.selectedId === r.id) return;
          if (currentRun()?.id === r.id) {
            pinRun(r.id);
          } else {
            setSelected(r.id);
            app.data = Object.assign({}, app.data, { run: null });
            stable(() => { renderRuns(); renderRunView(); syncControls(); });
          }
          pollNow();
        });
        const li = h('li', null, btn);
        updateRun(li, r, ctx);
        return li;
      },
      (li, r) => updateRun(li, r, ctx),
      app.runEls);
    const pick = $('run-pick');
    if (runs.length && !run) {
      setText(pick, app.selectedId ? 'Loading run ' + short(app.selectedId, 24) + '…' : 'Select a run in History to see its progress and evidence.');
      setHidden(pick, false);
    } else {
      setHidden(pick, true);
    }
  }

  function updateRun(li, r, ctx) {
    const btn = li.firstElementChild;
    const on = r.id === ctx.shown;
    if (btn.getAttribute('aria-pressed') !== String(on)) btn.setAttribute('aria-pressed', String(on));
    setText(btn.querySelector('.run-title'), 'Run ' + (ctx.ord.get(r.id) || '?'));
    const id = btn.querySelector('.rid');
    setText(id, fmt(r.id).slice(0, 8));
    if (id.title !== fmt(r.id)) id.title = fmt(r.id);
    setChip(btn.querySelector('.chip'), runWord(r.status, ctx.connected && ctx.running === r.id));
    setText(btn.querySelector('.ratt'), r.attempt !== undefined && r.attempt !== null ? 'attempt ' + fmt(r.attempt) : '');
  }

  /* --- run view --- */

  function buildStageCards() {
    const ol = $('timeline');
    const cards = {};
    ol.replaceChildren(...STAGES.map((name, i) => {
      const c = {
        chip: h('span', { class: 'chip chip-idle' }),
        time: h('small', { class: 'st-time' }),
        invs: h('div', { class: 'invs' }),
        counter: h('span', { class: 'st-count' }),
        bar: h('progress', { max: '1', value: '0', hidden: true }),
        reason: h('p', { class: 'reason' }),
        n: h('small', { class: 'st-n' }),
        sig: '',
      };
      c.li = h('li', { class: 'stage', 'data-stage': name },
        h('div', { class: 'stage-head' }, h('span', { class: 'ord', 'aria-hidden': 'true', text: String(i + 1) }), h('h4', { text: name }), c.chip),
        c.time, c.n, c.invs, h('div', { class: 'st-cases' }, c.counter, c.bar), c.reason);
      cards[name] = c;
      return c.li;
    }));
    app.stageCards = cards;
  }

  /* One block per (worker, generation) invocation; nothing is shared between blocks. */
  function renderInvocations(c, invs) {
    const sig = safeJson(invs, false);
    if (sig === c.sig) return;
    c.sig = sig;
    const list = invs.length ? invs : [{ worker: null, gen: null, d: {} }];
    c.invs.replaceChildren(...list.map((inv) => {
      const m = modelReport(inv.d);
      return h('div', { class: 'inv' },
        h('p', { class: 'invoked', text: invokedText(inv) }),
        h('p', { class: 'model m-' + m.tone, text: m.text }));
    }));
  }

  function updateStageCard(c, s, step) {
    if (!s) {
      c.chip.className = 'chip chip-idle';
      setText(c.chip, 'Not started');
      for (const el of [c.time, c.n, c.invs, c.counter, c.reason]) setHidden(el, true);
      setHidden(c.bar, true);
      c.li.dataset.state = 'idle';
      return;
    }
    if (!s.last) {
      c.chip.className = 'chip chip-idle';
      setText(c.chip, 'Not started in the latest generation');
      for (const el of [c.time, c.invs, c.counter, c.reason]) setHidden(el, true);
      setHidden(c.bar, true);
      setText(c.n, plural(s.count, 'event', 'events') + ' from an earlier generation only; see Event evidence');
      setHidden(c.n, false);
      c.li.dataset.state = 'idle';
      return;
    }
    c.chip.className = 'chip chip-' + step.state;
    setText(c.chip, step.text);
    c.li.dataset.state = step.state;
    const t = fmtTime(s.last.at);
    setText(c.time, t.text);
    setHidden(c.time, false);
    setText(c.n, s.count + (s.count === 1 ? ' event' : ' events') + (s.gen !== null ? ' · latest generation ' + s.gen : '') + ' · last event ' + fmt(s.last.status) + (present(s.last.seq) ? ' #' + fmt(s.last.seq) : ''));
    setHidden(c.n, false);
    renderInvocations(c, s.invs);
    setHidden(c.invs, false);
    const done = numeric(s.cases.completed), req = numeric(s.cases.required);
    if (done !== null || req !== null) {
      setText(c.counter, 'Cases ' + (done === null ? '?' : done) + ' / ' + (req === null ? '?' : req) + (own(CASE_LABEL, c.li.dataset.stage) ? ' (' + CASE_LABEL[c.li.dataset.stage] + ')' : ''));
      setHidden(c.counter, false);
      const showBar = done !== null && req !== null && req > 0;
      setHidden(c.bar, !showBar);
      if (showBar) {
        c.bar.max = req;
        c.bar.value = Math.min(done, req);
        c.bar.setAttribute('aria-label', 'Cases completed in ' + c.li.dataset.stage);
      }
    } else {
      setHidden(c.counter, true);
      setHidden(c.bar, true);
    }
    if (s.reason !== undefined) {
      setText(c.reason, short(s.reason, 300));
      setHidden(c.reason, false);
    } else {
      setHidden(c.reason, true);
    }
  }

  function filterEvents(events) {
    const f = app.filters;
    const q = f.q.trim().toLowerCase();
    return events.filter((ev) => {
      if (f.stage && String(ev.stage) !== f.stage) return false;
      if (f.status && String(ev.status) !== f.status) return false;
      if (f.attempt && String(ev.attempt) !== f.attempt) return false;
      if (q && !safeJson(ev, false).toLowerCase().includes(q)) return false;
      return true;
    });
  }

  function syncSelect(sel, values, current, allLabel, fieldName) {
    const sig = safeJson(values, false);
    if (sel.dataset.sig !== sig) {
      sel.dataset.sig = sig;
      sel.replaceChildren(h('option', { value: '', text: allLabel }), ...values.map((v) => h('option', { value: v, text: v })));
    }
    if (current && !values.includes(current)) {
      app.filters[fieldName] = '';
      current = '';
    }
    if (sel.value !== current) sel.value = current;
  }

  function renderRunView() {
    const run = currentRun();
    const view = $('run-view');
    if (!run) {
      setHidden(view, true);
      app.sig.run = null;
      app.proj = null;
      app.role = null;
      chatReset();
      feedReset();
      resetBriefing();
      return;
    }
    setHidden(view, false);
    const sig = safeJson(run, false) + '|' + safeJson(app.filters, false) + '|' + app.limit + '|' + liveKey(run);
    if (sig === app.sig.run) return;
    app.sig.run = sig;

    const events = sortedEvents(run);
    const attempt = runAttempt(run, events);
    const scope = scopeEvents(events, attempt);
    const stages = deriveStages(scope);
    if (!app.stageCards) buildStageCards();
    const p = project(run, events, scope);
    if (!app.proj || app.proj.id !== run.id || app.proj.attempt !== attempt) {
      chatReset();
      app.role = null;
    }

    setChip($('c-status'), p.word);
    setText($('c-attempt'), attempt === null ? 'Not reported' : String(attempt));
    setText($('c-events'), String(events.length));
    const reached = STAGES.filter((n) => stages[n]).length;
    setText($('c-stages'), reached + ' of ' + STAGES.length);
    setText($('r-id'), fmt(run.id));
    setText($('r-hash'), run.manifest_hash ? fmt(run.manifest_hash) : 'Not reported');
    const thisAttempt = events.filter((ev) => attempt === null || Number(ev.attempt) === attempt);
    const lastEv = thisAttempt.length ? thisAttempt[thisAttempt.length - 1] : null;
    setText($('r-latest'), lastEv
      ? fmt(lastEv.stage) + ' · ' + fmt(lastEv.status) + ' · ' + fmtTime(lastEv.at).text + ' · ' + seqOf(lastEv)
        + (workerOf(lastEv) ? ' · ' + workerOf(lastEv) : ' · no worker named')
      : 'No events yet for this attempt');

    // Case counter: the latest stage that reports both numbers, labelled with its source.
    let cases = null;
    for (const name of STAGES) {
      const s = stages[name];
      if (!s || !own(CASE_LABEL, name)) continue;
      const done = numeric(s.cases.completed), req = numeric(s.cases.required);
      if (done !== null && req !== null) cases = { done, req, src: name };
    }
    const bar = $('c-cases-bar');
    if (cases) {
      setText($('c-cases'), countText(cases.done) + ' / ' + countText(cases.req));
      setText($('c-cases-src'), '(' + (p.steps[cases.src].cases.matched ? 'matched' : CASE_LABEL[cases.src])
        + (cases.src === 'verify' && p.gen !== null ? ', build ' + p.gen : '') + ')');
      setHidden(bar, cases.req <= 0);
      if (cases.req > 0) {
        bar.max = cases.req;
        bar.value = Math.min(cases.done, cases.req);
        bar.setAttribute('aria-label', p.steps[cases.src].cases.matched ? 'Cases matched' : 'Cases observed');
      }
    } else {
      setText($('c-cases'), 'Not reported');
      setText($('c-cases-src'), '');
      setHidden(bar, true);
    }

    const manifestSig = safeJson(run.manifest, false);
    if (manifestSig !== app.sig.manifest) {
      app.sig.manifest = manifestSig;
      setText($('r-manifest'), run.manifest === undefined ? 'No manifest reported.' : clip(safeJson(run.manifest, true), CLIP));
    }

    setHidden($('pkg'), p.kind !== 'verified' || lower(run.status) !== 'verified');

    for (const name of STAGES) updateStageCard(app.stageCards[name], stages[name], p.steps[name]);
    renderStory(p);

    renderEvents(events);
  }

  function renderEvents(events) {
    const stageNames = STAGES.slice();
    const statuses = new Set();
    const attempts = new Set();
    for (const e of events) {
      const st = String(e.stage === undefined ? '' : e.stage);
      if (st && !stageNames.includes(st)) stageNames.push(st);
      if (e.status !== undefined) statuses.add(String(e.status));
      if (e.attempt !== undefined) attempts.add(String(e.attempt));
    }
    syncSelect($('f-stage'), stageNames, app.filters.stage, 'All stages', 'stage');
    syncSelect($('f-status'), [...statuses].sort(), app.filters.status, 'All statuses', 'status');
    syncSelect($('f-attempt'), [...attempts].sort((a, b) => Number(a) - Number(b)), app.filters.attempt, 'All attempts', 'attempt');
    if ($('f-q').value !== app.filters.q) $('f-q').value = app.filters.q;

    const matched = filterEvents(events);
    const visible = matched.slice(-app.limit);
    const keys = new Set();
    const keyed = visible.map((ev, i) => {
      let key = 'ev:' + fmt(ev.attempt) + ':' + fmt(ev.seq);
      if (keys.has(key)) key += '#' + i;
      keys.add(key);
      return { ev, key };
    });

    const table = $('ev-table');
    setHidden(table, keyed.length === 0);
    reconcile(table, keyed, (x) => x.key,
      (x, key) => {
        const body = h('tbody', { class: 'ev' });
        fillEvent(body, x.ev, key);
        return body;
      },
      (body, x, key) => {
        const sig = safeJson(x.ev, false);
        if (body._sig !== sig) fillEvent(body, x.ev, key);
      },
      app.evEls);

    const filtering = !!(app.filters.stage || app.filters.status || app.filters.attempt || app.filters.q.trim());
    let count = 'Showing ' + keyed.length + ' of ' + matched.length + (filtering ? ' matching' : '') + ' events';
    if (filtering) count += ' (' + events.length + ' total)';
    setText($('f-count'), events.length ? count : '');

    const empty = $('ev-empty');
    if (!events.length) {
      setText(empty, 'No events recorded for this run yet. Events appear here as each stage reports.');
      setHidden(empty, false);
    } else if (!matched.length) {
      setText(empty, 'No events match these filters. Press Reset to see all ' + events.length + ' events.');
      setHidden(empty, false);
    } else {
      setHidden(empty, true);
    }
    const more = $('ev-more');
    setHidden(more, matched.length <= visible.length);
    setText(more, 'Show earlier events (' + (matched.length - visible.length) + ' hidden)');
    setHidden($('ev-wrap'), events.length === 0);
    setHidden($('ev-help'), keyed.length === 0);
  }

  function fillEvent(body, ev, key) {
    body._sig = safeJson(ev, false);
    body._ev = ev;
    body._built = false;
    if (!body._detailId) body._detailId = 'ev-detail-' + (++app.detailSeq);
    const open = app.expanded.has(key);
    const toggle = h('button', {
      type: 'button', class: 'ev-toggle', 'data-fkey': 'tog:' + key,
      'aria-expanded': String(open), 'aria-controls': body._detailId,
      'aria-label': 'Evidence for ' + fmt(ev.stage) + ' event ' + fmt(ev.seq) + ', attempt ' + fmt(ev.attempt),
    }, open ? 'Hide' : 'Details');
    const main = h('tr', { class: 'ev-row' },
      h('td', { 'data-label': 'Seq', class: 'c-seq', text: fmt(ev.seq) }),
      h('td', { 'data-label': 'Attempt', class: 'c-att', text: fmt(ev.attempt) }),
      h('td', { 'data-label': 'Time', class: 'c-at' }, timeEl(ev.at)),
      h('td', { 'data-label': 'Stage', class: 'c-stage', text: fmt(ev.stage) }),
      h('td', { 'data-label': 'Status', class: 'c-status' }, chip(ev.status)),
      h('td', { 'data-label': 'Summary', class: 'c-sum', text: eventSummary(ev) }),
      h('td', { 'data-label': 'Evidence', class: 'c-ev' }, toggle));
    const cell = h('td', { colspan: '7' });
    const detail = h('tr', { class: 'ev-detail', id: body._detailId, hidden: !open }, cell);
    body._cell = cell;
    body._detail = detail;
    body.replaceChildren(main, detail);
    if (open) buildDetail(body);
  }

  function buildDetail(body) {
    if (body._built) return;
    body._built = true;
    const ev = body._ev;
    const d = isObj(ev.detail) ? ev.detail : null;
    const rows = [];
    const add = (label, value) => rows.push(h('div', null, h('dt', { text: label }), h('dd', { text: value })));
    add('Stage', fmt(ev.stage) || 'Not reported');
    add('Status', fmt(ev.status) || 'Not reported');
    add('Attempt / seq', fmt(ev.attempt) + ' / ' + fmt(ev.seq));
    const extras = [];
    if (d) {
      for (const [k, label] of [['worker', 'Worker'], ['skill', 'Skill'], ['image', 'Image'], ['tool', 'Tool']]) {
        if (d[k] !== null && d[k] !== undefined && d[k] !== '') add(label + ' (reported)', fmt(d[k]));
      }
      if (present(d.generation)) add('Generation (reported)', fmt(d.generation));
      add('Model', modelReport(d).text);
      if (d.reason !== undefined) add('Reason', fmt(d.reason));
      const c = numeric(d.completed), r = numeric(d.required);
      if (c !== null || r !== null) add('Cases', (c === null ? '?' : c) + ' / ' + (r === null ? '?' : r));
      for (const [k, v] of Object.entries(d)) if (!KNOWN_DETAIL.has(k)) extras.push([k, v]);
    } else if (ev.detail === null || ev.detail === undefined) {
      add('Detail', 'Not reported');
    } else {
      add('Detail', fmt(ev.detail));
    }
    const kids = [h('dl', { class: 'kv' }, rows)];
    if (d && d.evidence !== undefined) {
      kids.push(h('h5', { text: 'Evidence' }),
        h('pre', { tabindex: '0', 'aria-label': 'Evidence JSON', text: typeof d.evidence === 'string' ? clip(d.evidence, CLIP) : clip(safeJson(d.evidence, true), CLIP) }));
    }
    if (extras.length) {
      kids.push(h('h5', { text: 'Other fields' }),
        h('pre', { tabindex: '0', 'aria-label': 'Other detail fields JSON', text: clip(safeJson(Object.fromEntries(extras), true), CLIP) }));
    }
    body._cell.replaceChildren(...kids);
  }

  function toggleEvent(body) {
    const key = body.dataset.key;
    const open = !app.expanded.has(key);
    if (open) app.expanded.add(key); else app.expanded.delete(key);
    if (open) buildDetail(body);
    body._detail.hidden = !open;
    const btn = body.querySelector('.ev-toggle');
    btn.setAttribute('aria-expanded', String(open));
    btn.textContent = open ? 'Hide' : 'Details';
  }

  /* --- releases / catalog --- */

  function relInfoRow(label, value) {
    return h('div', null, h('dt', { text: label }), h('dd', { text: value }));
  }

  function filesText(f) {
    if (typeof f === 'number') return f + (f === 1 ? ' file' : ' files');
    if (Array.isArray(f)) return f.length + (f.length === 1 ? ' file' : ' files');
    if (isObj(f)) { const n = Object.keys(f).length; return n + (n === 1 ? ' file' : ' files'); }
    return fmt(f);
  }

  function renderReleases() {
    const rel = (app.data && isObj(app.data.releases)) ? app.data.releases : {};
    const sig = safeJson(rel, false);
    if (sig === app.sig.rel) { renderConfirm(); return; }
    app.sig.rel = sig;
    const items = Array.isArray(rel.items) ? rel.items.filter(isObj) : [];
    const cards = REL_ROLES.map(([role, label]) => {
      const digest = rel[role];
      const head = h('div', { class: 'rel-head' }, h('h3', { text: label }),
        digest ? h('code', { class: 'digest', title: fmt(digest), text: short(digest, 19) }) : h('span', { class: 'none', text: 'None' }));
      const card = h('article', { class: 'rel rel-' + role }, head);
      if (!digest) return card;
      const item = items.find((i) => i.digest === digest);
      const info = item && isObj(item.info) ? item.info : null;
      if (!info) {
        card.append(h('p', { class: 'none', text: 'No release info reported for this digest.' }));
        return card;
      }
      const rows = [];
      if (info.kit !== undefined) rows.push(relInfoRow('Kit', fmt(info.kit)));
      if (info.rea !== undefined) rows.push(relInfoRow('REA', fmt(info.rea)));
      if (info.rea_commit !== undefined) rows.push(relInfoRow('REA commit', short(info.rea_commit, 12)));
      if (info.files !== undefined) rows.push(relInfoRow('Files', filesText(info.files)));
      card.append(h('dl', { class: 'kv' }, rows));
      const det = h('details', { 'data-key': 'rel:' + role, open: app.openDetails.has('rel:' + role) },
        h('summary', { text: 'Full info' }),
        h('p', null, 'Digest: ', h('code', { class: 'digest', text: fmt(digest) })),
        h('pre', { tabindex: '0', 'aria-label': label + ' release info JSON', text: clip(safeJson(info, true), CLIP) }));
      card.append(det);
      return card;
    });
    $('release-list').replaceChildren(...cards);
    renderConfirm();
  }

  function renderCatalog() {
    const cat = Array.isArray(app.data.catalog) ? app.data.catalog.filter(isObj) : [];
    const sig = safeJson(cat, false);
    if (sig === app.sig.cat) return;
    app.sig.cat = sig;
    setText($('catalog-summary'), cat.length ? 'Catalog (' + cat.length + (cat.length === 1 ? ' reference)' : ' references)') : 'Catalog (empty)');
    $('catalog-body').replaceChildren(...(cat.length
      ? cat.map((c) => h('tr', null, h('td', { text: fmt(c.role) }), h('td', { text: fmt(c.skill) }), h('td', { text: fmt(c.scope) })))
      : [h('tr', null, h('td', { colspan: '3', text: 'No catalog references reported.' }))]));
  }

  /* --- controls --- */

  function setDisabled(btn, off, why) {
    const v = off ? 'true' : null;
    if (btn.getAttribute('aria-disabled') !== v) {
      if (v) btn.setAttribute('aria-disabled', 'true'); else btn.removeAttribute('aria-disabled');
    }
    if (off && why) { if (btn.title !== why) btn.title = why; }
    else if (btn.title) btn.removeAttribute('title');
  }

  function isOff(btn) {
    return btn.getAttribute('aria-disabled') === 'true';
  }

  function syncControls() {
    const d = app.data;
    const live = isLive();
    const busy = app.acting || !!(d && d.busy);
    const run = currentRun();
    const rel = d && isObj(d.releases) ? d.releases : {};

    const base = !d
      ? (app.link === 'lost' ? 'Disconnected: no state has been read yet.' : 'Connecting: waiting for the desk to answer.')
      : !live ? 'Disconnected: showing the last known state. Actions are unavailable until the desk answers.'
      : busy ? 'Operator action in progress.' : '';
    const running = !!(d && d.running);
    const isCurrent = live && !!run && running && d.running === run.id;
    const stopped = !!run && ['blocked', 'failed', 'cancelled'].includes(run.status);
    const cancelWhy = !live ? base : app.acting ? 'Operator action in progress.' : !isCurrent ? 'No selected run is executing.' : '';
    const retryWhy = base || (running ? 'A run is executing.' : !stopped ? 'Retry a blocked, failed, or cancelled run.' : '');
    const startWhy = base || (running ? 'A run is executing.' : !rel.active
      ? (rel.staged ? 'In Releases and updates, promote the staged release first.'
        : 'In Releases and updates, stage an update and promote it first.') : '');
    setDisabled($('btn-start'), !!startWhy, startWhy);
    setDisabled($('btn-cancel'), !!cancelWhy, cancelWhy);
    setDisabled($('btn-retry'), !!retryWhy, retryWhy);
    const dlWhy = app.downloading ? 'Download in progress.' : !live ? base : '';
    setDisabled($('pkg'), !!dlWhy, dlWhy);
    setText($('run-hint'), !d ? '' : startWhy || (d.builder_connected ? 'Subscription builder connected.' : 'No builder connected. A run can collect reference evidence but will stop before rebuilding.'));

    const staged = rel.staged || null;
    const promoteWhy = base || (!staged ? 'Nothing is staged. Use Stage update first.' : staged === rel.active ? 'The staged release is already active.' : '');
    const rollbackWhy = base || (!rel.previous ? 'No previous release to roll back to.' : '');
    setDisabled($('btn-stage'), !!base, base);
    setDisabled($('btn-promote'), !!promoteWhy, promoteWhy);
    setDisabled($('btn-rollback'), !!rollbackWhy, rollbackWhy);
    setText($('release-hint'), base || [promoteWhy && 'Promote: ' + promoteWhy, rollbackWhy && 'Roll back: ' + rollbackWhy].filter(Boolean).join(' '));
    renderConfirm();
  }

  function renderConfirm() {
    const box = $('confirm');
    const d = app.data;
    const rel = d && isObj(d.releases) ? d.releases : {};
    let text = '';
    if (app.confirm === 'promote' && rel.staged && rel.staged === app.confirmDigest && rel.staged !== rel.active) {
      text = 'Promote staged release ' + short(rel.staged, 19) + ' to active?';
    } else if (app.confirm === 'rollback' && rel.previous && rel.previous === app.confirmDigest) {
      text = 'Roll back to previous release ' + short(rel.previous, 19) + '?';
    } else {
      app.confirm = null;
      app.confirmDigest = null;
    }
    setHidden(box, !app.confirm);
    if (app.confirm) {
      const why = !isLive() ? 'The desk is disconnected; nothing can be sent until it answers.'
        : app.acting || d.busy ? 'Operator action in progress.' : '';
      setText($('confirm-text'), why ? text + ' ' + why : text);
      setText($('confirm-yes'), app.confirm === 'promote' ? 'Confirm promote' : 'Confirm roll back');
      setDisabled($('confirm-yes'), !!why, why);
    }
  }

  function askConfirm(kind) {
    const rel = app.data && isObj(app.data.releases) ? app.data.releases : {};
    app.confirm = kind;
    app.confirmDigest = kind === 'promote' ? rel.staged : rel.previous;
    stable(renderConfirm);
    if (app.confirm) $('confirm-yes').focus();
  }

  function closeConfirm(returnTo) {
    app.confirm = null;
    app.confirmDigest = null;
    stable(renderConfirm);
    if (returnTo) $(returnTo).focus();
  }

  function wire() {
    $('btn-start').addEventListener('click', () => { if (!isOff($('btn-start'))) act({ action: 'start' }, 'Start run'); });
    $('btn-cancel').addEventListener('click', () => {
      const run = currentRun();
      if (run && !isOff($('btn-cancel'))) act({ action: 'cancel', run: run.id }, 'Cancel run');
    });
    $('btn-retry').addEventListener('click', () => {
      const run = currentRun();
      if (run && !isOff($('btn-retry'))) act({ action: 'retry', run: run.id }, 'Retry run');
    });
    $('btn-stage').addEventListener('click', () => { if (!isOff($('btn-stage'))) act({ action: 'stage' }, 'Stage update'); });
    $('btn-promote').addEventListener('click', () => { if (!isOff($('btn-promote'))) askConfirm('promote'); });
    $('btn-rollback').addEventListener('click', () => { if (!isOff($('btn-rollback'))) askConfirm('rollback'); });
    $('confirm-no').addEventListener('click', () => closeConfirm(app.confirm === 'promote' ? 'btn-promote' : 'btn-rollback'));
    $('confirm-yes').addEventListener('click', () => {
      if (!isLive() || app.acting || app.data.busy) return;
      const rel = app.data && isObj(app.data.releases) ? app.data.releases : {};
      const kind = app.confirm;
      const digest = app.confirmDigest;
      if (kind === 'promote') {
        if (!digest || rel.staged !== digest) {
          app.alerts.action = 'Promote failed: the staged release changed; review it and confirm again.';
          closeConfirm('btn-promote');
          stable(renderAlerts);
          return;
        }
        closeConfirm('btn-promote');
        act({ action: 'promote', digest }, 'Promote');
      } else if (kind === 'rollback') {
        closeConfirm('btn-rollback');
        act({ action: 'rollback' }, 'Roll back');
      }
    });
    $('confirm').addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        closeConfirm(app.confirm === 'promote' ? 'btn-promote' : 'btn-rollback');
      }
    });

    const refilter = () => stable(() => { app.sig.run = null; renderRunView(); });
    $('f-stage').addEventListener('change', (e) => { app.filters.stage = e.target.value; app.limit = PAGE; refilter(); });
    $('f-status').addEventListener('change', (e) => { app.filters.status = e.target.value; app.limit = PAGE; refilter(); });
    $('f-attempt').addEventListener('change', (e) => { app.filters.attempt = e.target.value; app.limit = PAGE; refilter(); });
    $('f-q').addEventListener('input', (e) => { app.filters.q = e.target.value; app.limit = PAGE; refilter(); });
    $('f-reset').addEventListener('click', () => {
      app.filters = { stage: '', status: '', attempt: '', q: '' };
      app.limit = PAGE;
      refilter();
    });
    $('ev-more').addEventListener('click', () => { app.limit += PAGE; refilter(); });

    const table = $('ev-table');
    table.addEventListener('click', (e) => {
      const btn = e.target.closest('.ev-toggle');
      if (btn) toggleEvent(btn.closest('tbody'));
    });
    table.addEventListener('keydown', (e) => {
      const btn = e.target.closest('.ev-toggle');
      if (!btn || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
      const all = [...table.querySelectorAll('.ev-toggle')];
      const i = all.indexOf(btn);
      let next = i;
      if (e.key === 'ArrowDown') next = Math.min(all.length - 1, i + 1);
      else if (e.key === 'ArrowUp') next = Math.max(0, i - 1);
      else if (e.key === 'Home') next = 0;
      else next = all.length - 1;
      e.preventDefault();
      all[next].focus();
    });

    $('release-list').addEventListener('toggle', (e) => {
      const det = e.target;
      if (!(det instanceof HTMLDetailsElement)) return;
      const key = det.dataset.key;
      if (!key) return;
      if (det.open) app.openDetails.add(key); else app.openDetails.delete(key);
    }, true);

    $('pkg').addEventListener('click', () => {
      const run = currentRun();
      if (run && !isOff($('pkg'))) download(run);
    });

    const chatForm = $('chat-form');
    if (chatForm) {
      chatForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const input = $('chat-question');
        ask(input ? input.value : '');
        if (input) input.value = '';
      });
    }
    for (const b of document.querySelectorAll('button[data-question]')) {
      b.addEventListener('click', () => ask(b.getAttribute('data-question') || ''));
    }
    const clear = $('chat-clear');
    if (clear) clear.addEventListener('click', chatReset);
    const feedLog = $('feed-log');
    if (feedLog) {
      feedLog.addEventListener('scroll', () => {
        if (app.feed.unseen && feedAtBottom(feedLog)) { app.feed.unseen = 0; renderFeedState(); }
      }, { passive: true });
    }
    const feedToggle = $('feed-toggle');
    if (feedToggle) feedToggle.addEventListener('click', () => { if (!isOff(feedToggle)) feedSetPaused(!app.feed.paused); });
    const feedLatestBtn = $('feed-latest');
    if (feedLatestBtn) feedLatestBtn.addEventListener('click', () => { if (!isOff(feedLatestBtn)) feedLatest(); });
    const roleMap = $('role-map');
    if (roleMap) {
      roleMap.addEventListener('click', (e) => {
        const btn = e.target.closest('button.role-node');
        const li = btn && btn.closest('li[data-worker]');
        if (!li) return;
        const name = li.getAttribute('data-worker');
        app.role = name;
        syncRoleSelection();
        renderRoleDetail();
        /* On a narrow screen the evidence sits below the chart; bring it into view only if it is not already. */
        const box = $('role-detail');
        if (box && box.firstChild) box.scrollIntoView({ block: 'nearest' });
      });
    }

    /* The fixed local deployment pairs the two operator views through ordinary links, never an API proxy. */
    const inspect = $('nav-inspect');
    if (inspect && location.origin === PAIRED_ORIGIN) inspect.hidden = false;
    const navTeam = $('nav-team');
    if (navTeam) {
      navTeam.addEventListener('click', (e) => {
        e.preventDefault();
        const section = $('team');
        if (section && !$('run-view').hidden) {
          section.scrollIntoView({ block: 'start' });
          $('team-h').focus({ preventScroll: true });
        } else {
          $('main').focus({ preventScroll: true });
        }
      });
    }

    window.addEventListener('hashchange', () => {
      const id = readHash();
      if (id !== app.selectedId) { setSelected(id); pollNow(); }
    });
  }

  wire();
  stable(() => { renderConn(); renderPending(); syncControls(); });
  pollNow();
})();
