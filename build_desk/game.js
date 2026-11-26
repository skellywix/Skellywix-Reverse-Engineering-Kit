/* Fixed Neverball profile. The server admits the upload and decides whether the download exists and how it is labelled. */
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const form = el('game-form'), file = el('game-file'), prompt = el('game-prompt');
  const start = el('game-start'), refresh = el('game-refresh'), status = el('game-status');
  const error = el('game-error'), events = el('game-events'), download = el('game-download'), receipt = el('game-receipt');
  const validId = /^[a-f0-9]{32}$/, validHash = /^[a-f0-9]{64}$/;
  const LEGACY_READY = 'Windows candidate built. No play test was recorded at this stage.';
  const url = new URL(location.href);
  let id = url.searchParams.get('game'), request = '', size = 0;
  let timer, version = 0, sending = false, busy = false, eventKey = '';
  if (id !== null) el('windows-profile').open = true;

  function sync() {
    const selected = file.files[0];
    file.setCustomValidity(selected && selected.size !== size && size ? 'Choose the original Neverball 1.6.0 Windows ZIP.' : '');
    el('file-hint').textContent = selected && size && selected.size !== size
      ? `${selected.name} is not the original Neverball 1.6.0 Windows ZIP. Use the project builder for other source or Inspect for binary metadata.`
      : 'Use the original, unmodified Windows ZIP. A selection does not change the recorded run below.';
    prompt.setCustomValidity(request && prompt.value !== request ? 'Use the supported request: ' + request : '');
    start.disabled = sending || busy || !request || !selected || !file.validity.valid || !prompt.validity.valid;
    refresh.disabled = sending;
    file.disabled = sending;
    prompt.disabled = sending || !request;
  }
  function noDownload() {
    download.hidden = true;
    download.removeAttribute('href');
    receipt.textContent = '';
  }
  function pin(run) {
    id = run;
    const next = new URL(location.href);
    next.searchParams.delete('project');
    next.searchParams.set('game', run);
    history.replaceState(history.state, '', next);
  }
  function show(job) {
    if (!job || typeof job.message !== 'string' || !Array.isArray(job.events)
        || (job.id !== null && !validId.test(job.id)) || (id !== null && job.id !== id)) {
      throw new Error('The desk returned an invalid or different game run.');
    }
    busy = ['queued', 'build', 'package'].includes(job.state);
    status.textContent = job.state === 'idle' ? 'Ready for the original Windows ZIP.' : job.message;
    const key = JSON.stringify([job.id, job.events]);
    if (key !== eventKey) {
      events.replaceChildren(...job.events.map(entry => {
        const li = document.createElement('li');
        // Runs journaled before "built" existed carry text saying the download waits for Windows; it no longer does.
        li.textContent = entry.state === 'waiting_windows' ? LEGACY_READY : entry.message; return li;
      }));
      eventKey = key;
    }
    noDownload();
    const ready = job.download_ready === true && validHash.test(job.download_sha256) && job.id;
    const verified = ready && job.state === 'proven' && job.play_tested === true && job.verified_sha256 === job.download_sha256;
    if (verified) {
      receipt.textContent = `Neverball run ${job.id} · Independent Windows evidence accepted · ZIP SHA-256: ${job.download_sha256}`;
      download.textContent = 'Download verified Neverball for Windows';
      download.href = '/game/package/' + job.id; download.hidden = false;
    } else if (ready && job.state !== 'proven' && job.verified_sha256 === undefined) {
      receipt.textContent = `Neverball run ${job.id} · Built, download ready, not play-tested · ZIP SHA-256: ${job.download_sha256}`;
      download.textContent = 'Download built Neverball for Windows (not play-tested)';
      download.href = '/game/package/' + job.id; download.hidden = false;
    } else if (job.download_ready === false && typeof job.download_problem === 'string') {
      receipt.textContent = 'No download is available for this run: ' + job.download_problem;
    }
    sync();
    if (busy) timer = setTimeout(load, 2400);
  }
  async function json(path, options) {
    const res = await fetch(path, { cache: 'no-store', ...options });
    let body;
    try { body = await res.json(); } catch { throw new Error('The desk did not return a readable response.'); }
    if (!res.ok) throw new Error(typeof body.error === 'string' ? body.error : 'The desk refused this request.');
    return body;
  }
  async function load() {
    clearTimeout(timer);
    const current = ++version;
    try {
      if (id !== null && !validId.test(id)) throw new Error('This link has an invalid game run ID.');
      const data = await json('/game/status' + (id ? '/' + id : ''));
      if (current !== version) return;
      if (typeof data.prompt !== 'string' || !Number.isSafeInteger(data.reference_bytes) || data.reference_bytes <= 0) {
        throw new Error('The desk did not provide the installed game profile.');
      }
      if (!prompt.value) prompt.value = data.prompt;
      request = data.prompt; size = data.reference_bytes; error.textContent = '';
      show(data.job);
    } catch (e) {
      if (current !== version) return;
      request = ''; busy = false; noDownload(); sync();
      status.textContent = 'Game status unavailable.'; error.textContent = e.message;
    }
  }
  file.addEventListener('change', sync); prompt.addEventListener('input', sync);
  refresh.addEventListener('click', load);
  form.addEventListener('submit', async e => {
    e.preventDefault();
    if (start.disabled || !form.reportValidity()) return;
    clearTimeout(timer); ++version; sending = true; sync(); noDownload();
    error.textContent = ''; status.textContent = 'Checking the original ZIP and the installed source…';
    try {
      const result = await json('/game/build', {method: 'POST', headers: {'Content-Type': 'application/zip',
        'X-Candidate-Action': '1', 'X-Game-Request': prompt.value}, body: file.files[0]});
      if (!validId.test(result.id)) throw new Error('The desk did not return a game run ID. Refresh before trying again.');
      pin(result.id); sending = false; await load();
    } catch (err) {
      sending = false; status.textContent = 'Build not confirmed. Refresh before trying again.';
      error.textContent = err.message; sync();
    }
  });
  load();
})();
