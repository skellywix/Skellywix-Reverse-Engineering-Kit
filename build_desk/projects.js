import { projectHistory } from './feedback.js';

(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const select = el('project-profile'), file = el('project-input'), submit = el('project-submit');
  const status = el('project-status'), problem = el('project-error'), link = el('project-download');
  const update = el('update-all'), idPattern = /^[a-f0-9]{32}$/, hashPattern = /^[a-f0-9]{64}$/;
  const profiles = new Map();
  let id = new URL(location.href).searchParams.get('project'), selectedName = '', epoch = 0, maxBytes = 0;
  let sending = false, restoring = id !== null, timer, updateTimer, applying = false;
  const tracking = projectHistory(request, () => { file.value = ''; deselectRun(); });

  async function request(path, options) {
    const response = await fetch(path, {cache: 'no-store', ...options});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `Request failed (${response.status}).`);
    return body;
  }
  function list(node, labels) {
    node.replaceChildren(...labels.map(label => {
      const li = document.createElement('li'); li.textContent = label; return li;
    }));
  }
  function unlink() {
    link.hidden = true; link.removeAttribute('href'); link.removeAttribute('download');
    el('project-receipt').textContent = '';
  }
  function pin(value) {
    const url = new URL(location.href);
    url.searchParams.delete('project');
    if (value) { url.searchParams.delete('game'); url.searchParams.set('project', value); }
    history.replaceState(null, '', url);
  }
  function sync() {
    const profile = profiles.get(select.value), input = file.files[0];
    const invalid = input && (!input.size || (maxBytes && input.size > maxBytes));
    el('profile-info').textContent = profile ? (profile.reason || (profile.available ? 'Available locally' : 'Unavailable')) : 'No build profile available.';
    el('project-input-hint').textContent = profile?.description || 'The worker must publish its available profiles.';
    el('project-selection').textContent = input ? `${input.name} · ${input.size.toLocaleString()} bytes${invalid ? ' · Choose a nonempty file within the upload limit.' : ''}` : '';
    submit.disabled = sending || restoring || !profile?.available || !input || !!invalid;
    file.disabled = select.disabled = sending || restoring;
    tracking.lock(sending);
  }
  async function catalog() {
    try {
      const result = await request('/project/profiles');
      const old = select.value;
      profiles.clear(); select.replaceChildren();
      for (const p of result.profiles) {
        profiles.set(p.id, p); select.add(new Option(p.label, p.id));
      }
      if (profiles.has(old)) select.value = old;
      else if (result.profiles.some(p => p.available)) select.value = result.profiles.find(p => p.available).id;
      maxBytes = result.max_bytes;
      document.querySelectorAll('[data-workspace]').forEach(n => { n.hidden = !result.workspace; });
      el('inspect-fallback').hidden = !!result.workspace;
      sync();
    } catch (e) { profiles.clear(); sync(); el('profile-info').textContent = `Profiles unavailable: ${e.message}`; }
  }
  function deselectRun() {
    ++epoch; clearTimeout(timer); id = null; restoring = false; selectedName = ''; pin(null); unlink();
    list(el('project-events'), []); problem.textContent = '';
    status.textContent = 'Selection changed. No result belongs to this selection yet.'; sync();
  }
  async function load() {
    clearTimeout(timer);
    const generation = ++epoch;
    if (!id) return;
    try {
      if (!idPattern.test(id)) throw new Error('Invalid project run ID.');
      const job = await request('/project/status/' + id);
      if (generation !== epoch) return;
      if (job.id !== id || !Array.isArray(job.events)) throw new Error('The worker returned a different or invalid run.');
      const changed = tracking.bind(job);
      restoring = false; sync();
      unlink(); problem.textContent = '';
      const label = profiles.get(job.profile)?.label || job.profile;
      status.textContent = `${selectedName || label} · ${job.message}`;
      list(el('project-events'), job.events.map(e => e.message));
      el('project-receipt').textContent = `Run ${id} · ${label}` + (hashPattern.test(job.input_sha256) ? `\nInput SHA-256: ${job.input_sha256}` : '');
      if (job.state === 'built' && job.artifact && hashPattern.test(job.sha256)) {
        link.href = '/project/package/' + id; link.download = job.artifact;
        link.textContent = `Download ${job.artifact}`; link.hidden = false;
        el('project-receipt').textContent += `\nOutput SHA-256: ${job.sha256}`;
      }
      const terminal = ['failed', 'built'].includes(job.state);
      if (terminal || changed) await tracking.follow(job, () => generation === epoch);
      if (!terminal && generation === epoch) timer = setTimeout(load, 900);
    } catch (e) {
      if (generation !== epoch) return;
      unlink(); status.textContent = 'Run status unavailable. Refresh to reconnect.'; problem.textContent = e.message;
    }
  }
  el('project-form').addEventListener('submit', async event => {
    event.preventDefault(); if (submit.disabled) return;
    const source = file.files[0]; deselectRun(); sending = true; sync();
    selectedName = source.name; status.textContent = `Submitting ${selectedName}…`;
    try {
      const reply = await request('/project/build', {method: 'POST', headers: {'Content-Type': 'application/octet-stream',
        'X-Candidate-Action': '1', 'X-Project-Profile': select.value, ...tracking.headers(source.name)}, body: source});
      if (!idPattern.test(reply.id)) throw new Error('No valid run ID returned.');
      id = reply.id; pin(id); await load();
    } catch (e) { status.textContent = 'No build was confirmed.'; problem.textContent = e.message; }
    finally { sending = false; sync(); }
  });
  file.addEventListener('change', deselectRun); select.addEventListener('change', deselectRun);
  el('project-refresh').addEventListener('click', async () => { await catalog(); await load(); });
  function showUpdate(s) {
    el('update-status').textContent = s.message || s.state;
    list(el('update-components'), s.components.map(c => `${c.name} · ${c.version || 'not installed'} · ${c.state}`));
    applying = ['updating','running','checking','installing','staging'].includes(s.state);
    update.disabled = applying;
    clearTimeout(updateTimer);
    if (applying) updateTimer = setTimeout(checkUpdate, 1100);
  }
  async function checkUpdate() {
    try { const wasApplying = applying; showUpdate(await request('/maintenance/status')); if (wasApplying && !applying) await catalog(); }
    catch (e) { update.disabled = false; el('update-status').textContent = `Update status unavailable: ${e.message}`; }
  }
  update.addEventListener('click', async () => {
    if (update.disabled) return;
    clearTimeout(updateTimer); update.disabled = true; el('update-status').textContent = 'Checking the complete dependency set…';
    try { showUpdate(await request('/maintenance/update', {method: 'POST', headers: {'X-Candidate-Action': '1'}, body: ''})); if (!applying) await catalog(); }
    catch (e) { update.disabled = false; el('update-status').textContent = `Update not confirmed: ${e.message}`; }
  });
  sync();
  Promise.all([catalog(), tracking.refresh()]).then(load); checkUpdate();
})();
