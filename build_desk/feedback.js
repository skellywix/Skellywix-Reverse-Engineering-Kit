/* Reports are local observations. Only an explicit retest records a fix. */
const el = id => document.getElementById(id);
const isId = value => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);
const isHash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const short = id => id.slice(0, 8);
const partial = ' Some saved builds could not be read. Project and correction counts may be incomplete.';

function text(tag, value) {
  const node = document.createElement(tag);
  node.textContent = value;
  return node;
}
function issueRow(issue) {
  const li = document.createElement('li'), detail = document.createElement('details');
  detail.append(text('summary', `${issue.state === 'fixed' ? 'Confirmed fixed' : 'Open'} · ${issue.observed}`),
    text('p', `Expected: ${issue.expected}`), text('p', `Observed: ${issue.observed}`),
    text('p', `Reported ${issue.at} on build ${issue.run_id}. Output: ${issue.sha256 || 'no package'}.`));
  for (const attempt of issue.reviews) {
    detail.append(text('p', `${attempt.at} · ${attempt.outcome === 'fixed' ? 'Confirmed fixed' : 'Still occurs'} on ${attempt.run_id}: ${attempt.note}`),
      text('p', attempt.sha256 ? `Retested output: ${attempt.sha256}` : 'The retest build produced no package.'));
  }
  li.append(detail);
  return li;
}
export function projectHistory(request, changedProject) {
  const target = el('project-target'), name = el('project-name'), run = el('history-run');
  const message = el('history-message'), problem = el('history-error'), issueSelect = el('review-issue');
  let data = null, epoch = 0, building = false, saving = false;

  function controls() {
    target.disabled = name.disabled = run.disabled = el('history-refresh').disabled = building || saving;
    for (const button of document.querySelectorAll('#history-data button')) button.disabled = building || saving;
    el('project-name-row').hidden = !!target.value;
  }
  function resetDrafts() {
    for (const prefix of ['report', 'review']) {
      el(prefix + '-form').reset();
      el(prefix + '-panel').open = false;
    }
  }
  function retestOptions() {
    const r = data?.runs.find(item => item.id === run.value);
    const issue = data?.issues.find(item => item.id === issueSelect.value);
    const outcome = el('review-outcome'), fixed = outcome.querySelector('option[value="fixed"]');
    fixed.disabled = !r?.sha256 || !issue || [issue, ...issue.reviews].some(prior => prior.sha256 === r.sha256);
    if (fixed.disabled && outcome.value === 'fixed') outcome.value = 'still_open';
  }
  function showRun() {
    resetDrafts();
    const index = data.runs.findIndex(r => r.id === run.value), r = data.runs[index];
    if (!r) throw new Error('The selected build is not in this project.');
    el('history-digests').textContent = `Run ${r.id} · ${r.state}\nInput SHA-256: ${r.input_sha256}` +
      (r.sha256 ? `\nOutput SHA-256: ${r.sha256}` : '\nNo completed output.');
    el('history-link').href = '/?project=' + r.id;
    el('report-panel').hidden = !['built', 'failed'].includes(r.state);
    const earlier = new Set(data.runs.slice(index + 1).map(item => item.id));
    const eligible = data.issues.filter(issue => issue.state === 'open' && earlier.has(issue.run_id)
      && !issue.reviews.some(attempt => attempt.run_id === r.id));
    issueSelect.replaceChildren(...eligible.map(i => new Option(`${i.observed} · ${short(i.id)}`, i.id)));
    retestOptions();
    el('review-panel').hidden = !['built', 'failed'].includes(r.state) || !eligible.length;
  }
  function clear() {
    data = null;
    el('history-data').hidden = true;
    el('history-link').removeAttribute('href');
    el('history-issues').replaceChildren();
    run.replaceChildren(); issueSelect.replaceChildren();
    resetDrafts(); problem.textContent = '';
  }
  async function refresh(wanted = target.value, wantedRun = run.value, current = () => true, receipt = '') {
    const version = ++epoch;
    clear();
    message.textContent = 'Reading project history…';
    try {
      const library = await request('/project/library');
      if (version !== epoch || !current()) return;
      if (!Array.isArray(library.projects) || !library.projects.every(p => isId(p.id) && typeof p.name === 'string')) {
        throw new Error('Invalid project list.');
      }
      if (wanted && !library.projects.some(p => p.id === wanted)) throw new Error('The selected project is no longer available.');
      target.replaceChildren(new Option('New project', ''), ...library.projects.map(p => new Option(`${p.name} · ${short(p.id)}`, p.id)));
      target.value = wanted;
      controls();
      if (!wanted) {
        message.textContent = 'Build a new project or select a saved project to see its history.' + (library.incomplete ? partial : '');
        return;
      }
      const view = await request('/project/library/' + wanted);
      if (version !== epoch || !current()) return;
      if (view.id !== wanted || !Array.isArray(view.runs) || !view.runs.length || !Array.isArray(view.issues) ||
          !view.runs.every(r => isId(r.id) && isHash(r.input_sha256) && (r.sha256 === null || isHash(r.sha256)))) {
        throw new Error('The history does not identify the selected project and its builds.');
      }
      data = view;
      run.replaceChildren(...view.runs.map((r, n) => new Option(`Build ${view.runs.length - n} · ${r.profile} · ${r.state} · ${short(r.id)}`, r.id)));
      if (wantedRun && view.runs.some(r => r.id === wantedRun)) run.value = wantedRun;
      el('history-issues').replaceChildren(...view.issues.map(issueRow));
      el('history-empty').hidden = !!view.issues.length;
      showRun();
      message.textContent = `${receipt}${view.name} · ${view.runs.length} ${view.runs.length === 1 ? 'build' : 'builds'} · ${view.issues.filter(i => i.state === 'open').length} open reports.` +
        (view.incomplete || library.incomplete ? partial : '');
      el('history-data').hidden = false;
    } catch (e) {
      if (version !== epoch || !current()) return;
      clear(); message.textContent = receipt + 'History unavailable. Refresh history to reconnect.';
      problem.textContent = e.message;
    }
  }
  async function save(event, review) {
    event.preventDefault();
    if (building || saving || !data || data.id !== target.value) return;
    const project = data.id, selected = run.value, version = epoch;
    const path = review ? `/project/reviews/${selected}/${issueSelect.value}` : `/project/reports/${selected}`;
    const body = review ? {outcome: el('review-outcome').value, note: el('review-note').value.trim()} :
      {expected: el('report-expected').value.trim(), observed: el('report-observed').value.trim()};
    saving = true; controls(); problem.textContent = '';
    try {
      await request(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Candidate-Action': '1'}, body: JSON.stringify(body)});
      if (epoch === version) await refresh(project, selected, () => true, review ? 'Retest saved. ' : 'Mistake report saved. ');
    } catch (e) {
      if (epoch === version) problem.textContent = `Save not confirmed. ${e.message} Refresh history before submitting again.`;
    } finally { saving = false; controls(); }
  }
  target.addEventListener('change', () => { changedProject(); refresh(target.value, ''); });
  run.addEventListener('change', () => { if (data) { problem.textContent = ''; showRun(); } });
  issueSelect.addEventListener('change', retestOptions);
  el('history-refresh').addEventListener('click', () => refresh());
  el('report-form').addEventListener('submit', event => save(event, false));
  el('review-form').addEventListener('submit', event => save(event, true));
  return {
    refresh,
    bind(job) {
      if (!isId(job.project_id) || typeof job.project_name !== 'string') throw new Error('The saved build has no valid project identity.');
      if (target.value === job.project_id) return false;
      if (![...target.options].some(option => option.value === job.project_id)) {
        target.add(new Option(`${job.project_name} · ${short(job.project_id)}`, job.project_id));
      }
      target.value = job.project_id;
      controls();
      return true;
    },
    follow: (job, current) => refresh(job.project_id, job.id, current),
    lock: value => { building = value; controls(); },
    headers: filename => target.value ? {'X-Project-Id': target.value} :
      {'X-Project-Name': encodeURIComponent(name.value.trim() || [...filename].slice(0, 80).join(''))},
  };
}
