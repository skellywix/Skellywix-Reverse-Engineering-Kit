/* Browser regression for the fixed compatibility desk. Requires Playwright with Chromium. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');

const assets = path.join(__dirname, '..', 'fourmilab');
const workspaceOrigin = 'http://127.0.0.1:48784';
const id = '1'.repeat(32), otherId = '2'.repeat(32);
const ev = (seq, stage, status, detail, attempt = 1) => ({seq, stage, status, detail, attempt, at: '2025-01-23T12:00:00Z'});
const job = (events, attempt = 1, runId = id) => ({id: runId, attempt, events, status: events.at(-1)?.status ?? 'queued', manifest: {profile: 'Fourmilab Base64 1.5'}});
const initial = [ev(1, 'intake', 'completed', {worker: 'File analyst', deterministic: true}),
  ev(2, 'analyze', 'running', {worker: 'Behavior analyst', deterministic: true, completed: 256, required: 2557})];
const model = ev(4, 'rebuild', 'running', {worker: 'Rust builder', requested_model: 'requested-only', generation: 1});
const rebuilt = ev(5, 'rebuild', 'completed', {worker: 'Rust builder', actual_model: 'reported-model', generation: 1});
const compiled = ev(6, 'rebuild', 'completed', {worker: 'Compiler', deterministic: true, generation: 1});
const reference = [...initial, ev(3, 'analyze', 'completed', {worker: 'Behavior analyst', deterministic: true, completed: 2557, required: 2557})];
const compared = [...reference, model, rebuilt, compiled, ev(7, 'verify', 'running', {worker: 'Verifier', generation: 1, deterministic: true, completed: 2557, required: 2557})];

(async () => {
  const browser = await chromium.launch({channel: 'chromium'});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 960}});
    page.setDefaultTimeout(4500);
    const writes = [], errors = []; let run = null, running = null, lost = true;
    let releases = {}, otherRun = null, hold = null;
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      const headers = await req.allHeaders();
      if (headers.authorization || headers.cookie) { errors.push('Unexpected browser credential'); return route.abort(); }
      if (req.method() !== 'GET' || url.origin !== workspaceOrigin) { writes.push(req.method() + ' ' + url.pathname); return route.abort(); }
      if (url.search && !(url.pathname === '/state' && /^\?run=[a-f0-9]{32}$/.test(url.search))) { writes.push('unexpected query on ' + url.pathname); return route.abort(); }
      if (url.pathname === '/state') {
        if (lost) return route.fulfill({status: 503, json: {error: 'Test disconnect'}});
        const runs = [run, otherRun].filter(Boolean).map(({id, attempt, status}) => ({id, attempt, status}));
        const selected = otherRun && url.searchParams.get('run') === otherRun.id ? otherRun : run;
        const data = {run: selected, runs, running, releases, catalog: [], busy: false, builder_connected: true};
        if (hold && url.searchParams.get('run') === id) {
          const captured = hold; hold = null;
          captured(() => route.fulfill({json: data}));
          return;
        }
        return route.fulfill({json: data});
      }
      const asset = {'/': ['index.html', 'text/html'], '/app.js': ['app.js', 'text/javascript'], '/desk.css': ['desk.css', 'text/css']}[url.pathname];
      return asset ? route.fulfill({contentType: asset[1], body: await fs.readFile(path.join(assets, asset[0]))}) : route.fulfill({status: 404, body: ''});
    });
    const show = async (events, active, attempt = 1, runId = id) => { run = job(events, attempt, runId); running = active ? runId : null; };
    const visible = s => page.locator(s).waitFor({state: 'visible'});
    const text = (s, pat) => page.waitForFunction(([sel, re]) => new RegExp(re, 'i').test(document.querySelector(sel)?.innerText || ''), [s, pat]);
    const empty = s => page.waitForFunction(sel => !document.querySelector(sel)?.textContent.trim(), s);
    const node = w => `#role-map [data-worker="${w}"]`;
    const live = w => visible(node(w) + '[data-state="live"]');
    const ask = async q => { await page.locator('#chat-question').fill(q); await page.locator('#chat-question').press('Enter'); };
    const lastAnswer = () => page.locator('#chat-log .chat-a').last().innerText().then(s => s.replace(/^Desk: /, ''));
    const snap = async name => { if (process.env.DESK_PROOF_DIR) await page.screenshot({path: path.join(process.env.DESK_PROOF_DIR, name + '.png')}); };
    await page.goto(workspaceOrigin);
    assert.equal(await page.locator('#main').isVisible(), true, 'The homelab desk opens directly');
    assert.equal(await page.locator('input[type=password], #btn-lock').count(), 0);
    await text('#conn', 'disconnected');
    assert.equal(await page.locator('#btn-start').getAttribute('aria-disabled'), 'true');
    assert.equal(await page.locator('#zero').isVisible(), false, 'A failed read is not an empty history');
    await snap('offline'); lost = false;
    await visible('#zero'); assert.equal(await page.locator('#run-view').isVisible(), false);
    await text('#run-hint', 'Releases and updates');
    assert.doesNotMatch(await page.locator('#zero').innerText(), /press start run/i);
    await snap('empty');
    releases = {staged: 'a'.repeat(64)};
    await page.locator('#release-details > summary').click();
    await page.locator('#btn-promote').click();
    const confirmFocused = () => page.locator('#confirm-yes').evaluate(el => el === document.activeElement);
    assert.equal(await confirmFocused(), true);
    lost = true; await text('#conn', 'disconnected');
    assert.equal(await confirmFocused(), true);
    await page.keyboard.press('Enter');
    lost = false; await text('#conn', '^connected$');
    await page.locator('#confirm-no').click(); await page.locator('#release-details > summary').click();
    assert.deepEqual(writes, [], 'No confirmation was sent or replayed across the outage');
    await show(initial, true); await visible('#run-view'); await live('Behavior analyst');
    assert.equal(await page.title(), 'Skellywix RE Kit', 'The browser tab identifies the kit, not the specimen');
    assert.equal(await page.getByRole('heading', {level: 1}).innerText(), 'Skellywix RE Kit');
    const files = page.getByRole('navigation', {name: 'Workspace'}).getByRole('link', {name: 'File inspection'});
    assert.equal(await files.getAttribute('href'), 'http://127.0.0.1:48730/index.html');
    const mapBeforeActivity = await page.locator('#role-map').evaluate(map =>
      Boolean(map.compareDocumentPosition(document.querySelector('#chat')) & Node.DOCUMENT_POSITION_FOLLOWING));
    assert.ok(mapBeforeActivity, 'See the team and its handoffs before the activity log');
    const feed = page.locator('#feed-log > li');
    await text('#feed-log', 'Behavior analyst');
    assert.equal(await feed.count(), 2, 'Show both recorded reports before anyone asks a question');
    await text('#feed-log', '256');
    assert.equal(await page.locator('html').evaluate(el => getComputedStyle(el).colorScheme), 'dark');
    assert.equal(await page.locator('#step-track > li').count(), 5); assert.equal(await page.locator('#role-map > li').count(), 6);
    assert.doesNotMatch(await page.locator('#run-h').innerText(), /11111111/);
    await text('#run-summary', 'observ|reference|behavior|case');
    assert.equal(await page.locator('#run-view').evaluate(el => el.getAnimations({subtree:true}).some(a => a.playState === 'running')), true);
    assert.equal(await page.locator('#c-cases-bar').evaluate(e => [e.value, e.max].join('/')), '256/2557');
    await page.locator(node('Behavior analyst') + ' button').focus(); await page.keyboard.press('Enter'); await text('#role-detail', 'Behavior analyst');
    await snap('live-desktop');
    await ask('Who is working?'); await text('#chat-log', 'Behavior analyst');
    await page.locator('#chat-question').fill('unfinished question');
    lost = true; await text('#conn', 'disconnected'); await text('#run-summary', 'last|disconnect|offline|stale');
    await text('#feed-state', 'disconnected'); assert.equal(await feed.count(), 2);
    assert.equal(await page.locator('#btn-cancel').getAttribute('aria-disabled'), 'true');
    assert.equal(await page.locator('#role-map [data-state="live"]').count(), 0); assert.equal(await page.locator('#chat-question').inputValue(), 'unfinished question');
    assert.equal(await page.locator('#run-view').evaluate(el => el.getAnimations({subtree:true}).some(a => a.playState === 'running')), false);
    lost = false; await live('Behavior analyst');
    running = otherId;
    await page.waitForFunction(() => !document.querySelector('#role-map [data-state="live"]'));
    assert.notEqual(await page.locator('#run-view').getAttribute('data-working'), 'true');
    running = id; await live('Behavior analyst');
    await page.locator('#feed-toggle').click();
    const frozen = await feed.allTextContents();
    await show([...reference, model], true); await live('Rust builder');
    assert.deepEqual(await feed.allTextContents(), frozen, 'Pausing chat must not pause the worker map');
    await text('#feed-state', '2 new events waiting');
    await page.locator('#feed-toggle').click();
    await text('#feed-log', 'Rust builder'); assert.equal(await feed.count(), 4);
    await page.locator('#feed-toggle').click();
    await page.locator(node('Rust builder') + ' button').click(); await text('#role-detail', 'requested-only');
    assert.doesNotMatch(await page.locator('#role-detail').innerText(), /actual(?: model)?\s*[:=]?\s*requested-only/i);
    await show([...reference, {...model, detail: {...model.detail, actual_model: 'long-name-' + 'x'.repeat(1100)}}], true);
    await text('#role-detail', 'long-name-'); await ask('Which model did the Rust builder use?');
    const longAnswer = await lastAnswer();
    await show(compared, true); await live('Verifier'); assert.equal(await page.locator('#pkg').isVisible(), false);
    await page.locator('#feed-latest').click();
    await text('#feed-log', 'Verifier'); assert.equal(await feed.count(), 7, 'Catch up without duplicate reports');
    assert.deepEqual(await feed.locator('.feed-seq').allTextContents(), [1,2,3,4,5,6,7].map(n => `Event #${n}`));
    assert.deepEqual(await feed.locator('.feed-origin').allTextContents(), ['Recorded','Recorded',...Array(5).fill('New update')]);
    const feedLog = page.locator('#feed-log');
    const fromBottom = () => feedLog.evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop);
    await feedLog.evaluate(el => { el.scrollTop = 0; });
    assert.ok(await fromBottom() > 24, 'There must be older entries to scroll away to');
    await page.locator('#feed-latest').click(); assert.ok(await fromBottom() <= 1);
    await page.locator('#feed-log').scrollIntoViewIfNeeded(); await snap('feed-desktop');
    await show([...compared, ev(8, 'verify', 'completed', {worker:'Verifier', generation:1, deterministic:true, outcome:'rejected', reason:'One output differed.'})], true);
    run.manifest.generation_limit = 1;
    await visible(node('Verifier') + '[data-state="bad"]'); assert.equal(await page.locator('#pkg').isVisible(), false);
    await text('#run-next', 'no generation remains');
    await text('#feed-log', 'rejected this result');
    assert.equal(await feed.last().locator('.chip').innerText(), 'rejected');
    assert.equal(await feed.last().getAttribute('data-state'), 'bad');
    const finalBuildStep = await page.locator('#step-track [data-stage="verify"]').innerText();
    await ask('Why did the Verifier reject this build?'); const rejectionAnswer = await lastAnswer();
    await show([...compared, ev(8, 'verify', 'completed', {worker:'Verifier', generation:1, deterministic:true, outcome:'rejected'}),
      ev(9, 'rebuild', 'running', {worker:'Rust builder', generation:2, requested_model:'requested-only'})], true);
    await live('Rust builder'); await visible(node('Verifier') + '[data-state="idle"]');
    await page.locator(node('Rust builder') + ' button').click(); assert.doesNotMatch(await page.locator('#role-detail').innerText(), /reported-model/);
    await show([...compared, ev(8, 'verify', 'blocked', {reason:'Stopped before comparison.'})], false);
    await text('#run-summary', 'Stopped before comparison'); assert.equal(await page.locator('#role-map [data-state="live"]').count(), 0);
    await ask('Why did it stop?'); await text('#chat-log', 'Stopped before comparison');
    await show([...compared, ev(8,'verify','blocked',{reason:'Stopped before comparison.'}), ev(9, 'intake','queued',{reason:'Original manifest retained.', internal_note:'private diagnostic sentinel'},2)], true, 2);
    await text('#c-attempt','^2$'); await empty('#chat-log'); assert.equal(await page.locator('#chat-question').inputValue(), '');
    assert.equal(await feed.count(), 1, 'Do not show prior-attempt events as current activity');
    await text('#feed-log', 'Original manifest retained');
    assert.doesNotMatch(await page.locator('#feed-log').innerText(), /private diagnostic sentinel/);
    assert.doesNotMatch(await page.locator('#feed-log').innerText(), /Verifier|Behavior analyst/);
    await visible(node('Verifier') + '[data-state="idle"]');
    assert.match(await page.locator('#run-summary').innerText(), /queue|waiting for.*first/i);
    assert.doesNotMatch(await page.locator('#run-summary').innerText(), /reported completed/i);
    await show([...compared, ev(8, 'verify','completed',{worker:'Verifier', generation:1, deterministic:true, completed:2557, required:2557}),
      ev(9,'package','verified',{worker:'Packager',deterministic:true, completed:2557, required:2557, evidence:{matched:2557,required:2557}})], false, 1, otherId);
    await visible('#pkg'); await text('#run-h','verified|matched|ready'); assert.equal(await page.locator('#role-map [data-state="live"]').count(), 0);
    await ask('What is next?'); await text('#chat-log', 'package|download|complete|finished|verified');
    await text('#feed-log', 'Packager');
    await ask('<b>launch an unrelated task</b>'); assert.equal(await page.locator('#chat-log b').filter({hasText:'launch an unrelated task'}).count(), 0);
    await page.locator('#chat-clear').click(); await empty('#chat-log');
    assert.equal(await feed.count(), 9, 'Clearing questions keeps the activity record');
    await ask('Which model did the Rust builder use?'); await text('#chat-log', 'reported-model');
    await page.keyboard.press('Control+Home');
    await snap('verified-desktop');
    await page.setViewportSize({width:390,height:844}); await page.emulateMedia({reducedMotion:'reduce'});
    await show(initial, true); await live('Behavior analyst');
    await empty('#chat-log');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(await page.locator('#run-view').evaluate(el => el.getAnimations({subtree:true}).filter(a=>a.playState === 'running').map(a=>a.id)), []);
    await page.keyboard.press('Control+Home');
    await snap('live-mobile');
    await page.locator(node('File analyst') + ' button').click();
    await ask('Who is working?'); const conversation = await lastAnswer();
    await page.locator('#chat-question').fill('private draft');
    await page.locator('#run-list button[aria-pressed="true"]').click(); await visible('#run-view');
    assert.equal(new URL(page.url()).hash, '#run=' + id);
    await page.locator('#feed-log').scrollIntoViewIfNeeded(); await snap('feed-mobile');
    assert.deepEqual({
      draft: await page.locator('#chat-question').inputValue(),
      sameConversation: (await page.locator('#chat-log').innerText()).includes(conversation),
      sameWorker: await page.locator(node('File analyst') + ' button').getAttribute('aria-pressed'),
      promisedAnotherBuild: /next generation|pending/i.test(finalBuildStep),
      citedRejectReason: rejectionAnswer.includes('One output differed.') && rejectionAnswer.includes('event #8'),
      boundedAndCited: longAnswer.length <= 900 && longAnswer.includes('event #4'),
    }, {draft: 'private draft', sameConversation: true, sameWorker: 'true', promisedAnotherBuild: false, citedRejectReason: true, boundedAndCited: true});
    for (const width of [320, 768]) {
      await page.setViewportSize({width, height: 860});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `No page overflow at ${width}px`);
      assert.equal(await page.locator(node('File analyst') + ' button').isVisible(), true);
      await page.keyboard.press('Control+Home'); await snap('live-' + width);
    }
    for (const [selector, shot] of [['#btn-stage', 'release-controls'], ['#timeline', 'technical-record'], ['#ev-table', 'event-ledger']]) {
      const disclosure = page.locator(selector).locator('xpath=ancestor::details[1]');
      assert.equal(await disclosure.getAttribute('open'), null, `Hide ${selector} details until requested`);
      await disclosure.locator(':scope > summary').click();
      await visible(selector);
      await snap(shot);
      await disclosure.locator(':scope > summary').click();
    }
    await page.reload(); await live('Behavior analyst');
    assert.equal(await page.locator('#chat-question').inputValue(), ''); await empty('#chat-log');

    otherRun = job([ev(1, 'verify', 'blocked', {reason: 'Comparison stopped on the second run.'})], 1, otherId);
    const otherButton = page.locator(`#run-list [data-fkey="run:${otherId}"]`);
    await visible(`#run-list [data-fkey="run:${otherId}"]`);
    const releaseRead = await new Promise((resolve, reject) => {
      const deadline = setTimeout(() => reject(new Error('The selected run was not polled')), 4500);
      hold = reply => { clearTimeout(deadline); resolve(reply); };
    });
    try {
      await otherButton.click();
      await text('#run-summary', 'Comparison stopped on the second run');
    } finally { await releaseRead(); }
    assert.equal(new URL(page.url()).hash, '#run=' + otherId);
    assert.equal(await otherButton.getAttribute('aria-pressed'), 'true');
    assert.match(await page.locator('#run-summary').innerText(), /Comparison stopped on the second run/);
    assert.notEqual(await page.locator('#conn').getAttribute('data-state'), 'lost');
    assert.equal(await feed.count(), 1, 'An older poll cannot restore the previous run’s activity');
    assert.deepEqual(writes, []); assert.deepEqual(errors, []);
    console.log('PASS: live coordinator reports, pause, catch-up, latest, retry and run isolation, independent Q&A, Gruvbox dark layout, keyless reconnects, worker evidence, keyboard and responsive views; zero writes, credentials or page errors.');
  } finally { await browser.close(); }
})().catch(err => { console.error(err); process.exitCode = 1; });
