/* Real workspace, not a mock server. Use --exercise only with a new, disposable workspace. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { chromium } = require('playwright');

const [origin, output, mode] = process.argv.slice(2);
assert(/^http:\/\/127\.0\.0\.1:[1-9]\d{3,4}$/.test(origin || '') && output && (!mode || mode === '--exercise'),
  'Usage: node tests/workspace-browser.cjs http://127.0.0.1:PORT NEW_EVIDENCE_DIR [--exercise]');
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const views = {Build: '/', Inspect: '/inspect', Reconstruct: '/reconstruct', Convert: '/convert', Runtime: '/runtime', Releases: '/releases'};
const report = {views: [], observations: []};

async function layout(browser) {
  for (const width of [320, 390, 1440]) {
    const page = await browser.newPage({viewport: {width, height: 900}, reducedMotion: 'reduce'});
    try {
      for (const [label, route] of Object.entries(views)) {
        const response = await page.goto(origin + route);
        assert.equal(response.status(), 200, route);
        await page.getByRole('heading', {name: label === 'Build' ? 'Build a project' : label, exact: true}).waitFor();
        if (label === 'Build') await page.locator('#project-profile option').first().waitFor({state: 'attached'});
        if (label === 'Inspect') await page.getByRole('heading', {name: 'File intake'}).waitFor();
        if (label === 'Convert') await page.getByRole('link', {name: /^Line count /}).waitFor();
        if (label === 'Runtime') await page.getByText(/^Runtime state loaded:/).waitFor({state:'attached'});
        if (label === 'Releases') await page.getByRole('heading', {name:'Fixed Fourmilab engine releases'}).waitFor();
        assert.equal(new URL(page.url()).pathname, route);
        const contentWidth = await page.evaluate(() => document.documentElement.scrollWidth);
        await page.screenshot({path: path.join(output, `${label.toLowerCase()}-${width}.png`)});
        assert(contentWidth <= width, `${label} content ${contentWidth}px at ${width}px`);
        report.views.push({label, width, contentWidth});
      }
      await page.goto(origin + '/inspect');
      await page.keyboard.press('Tab');
      assert.match(await page.locator(':focus').innerText(), /Skip to content|Open navigation/);
      if (width === 320) {
        if ((await page.locator(':focus').innerText()) === 'Skip to content') await page.keyboard.press('Tab');
        assert.equal(await page.locator(':focus').innerText(), 'Open navigation');
        await page.keyboard.press('Enter');
        await page.getByRole('link', {name:'Runtime',exact:true}).click();
        await page.getByRole('heading', {name:'Runtime',exact:true}).waitFor();
        assert.equal(new URL(page.url()).pathname, '/runtime');
        report.observations.push('Mobile drawer navigated to Runtime');
      }
    } finally { await page.close(); }
  }
}

async function inspection(browser) {
  const context = await browser.newContext({viewport:{width: 390, height:900}});
  const page = await context.newPage();
  const text = value => page.getByText(value, {exact:true});
  const button = name => page.getByRole('button',{name,exact:true});
  const file = (name, buffer) => ({name, mimeType:'application/octet-stream', buffer});
  const a = Buffer.from('first\nrecord\n'), b = Buffer.from('first\nrecord\nextra\n');
  try {
    await page.goto(origin + '/inspect');
    // Never replace an operator's files. This test owns only a brand-new case.
    await text('Revision 0').waitFor({timeout:5000});
    assert(await text('Specimen A is empty.').isVisible());
    const slot = letter => page.getByRole('region', {name: `Specimen ${letter}`, exact: true}).locator('input[type="file"]');
    await slot('A').setInputFiles(file('original.txt', a));
    await text('Loaded: original.txt').waitFor();
    await slot('B').setInputFiles(file('changed.txt', b));
    await text('Loaded: changed.txt').waitFor();
    await page.getByRole('tab',{name:'Compare files'}).click();
    await button('Compare specimens').click();
    await page.getByRole('heading',{name:'Files differ'}).waitFor();
    await slot('A').setInputFiles(file('next.txt', b));
    await button('Keep case').waitFor();
    assert.equal(await page.locator(':focus').innerText(), 'Keep case');
    await page.keyboard.press('Enter');
    assert(await text('Loaded: original.txt').isVisible());
    assert(await text('1 entry recorded.').isVisible());
    await slot('A').setInputFiles(file('next.txt', b));
    const download = page.waitForEvent('download');
    await button('Export evidence, then replace').click();
    const caseFile = path.join(output,'before-replacement.json');
    await (await download).saveAs(caseFile);
    const prior = JSON.parse(await fs.readFile(caseFile,'utf8'));
    assert.equal(prior.slots.a.sha256,sha(a)); assert.equal(prior.slots.b.sha256,sha(b));
    assert.equal(prior.events.length,1); assert.equal(prior.events[0].result.size_delta,b.length-a.length);
    await text('Loaded: next.txt').waitFor(); await text('Specimen B is empty.').waitFor();
    await text('0 entries recorded.').waitFor();
    await slot('B').setInputFiles(file('same-bytes.txt', b));
    await button('Compare specimens').click();
    await page.getByRole('heading',{name:'Files match by SHA-256'}).waitFor();
    await page.getByRole('tab',{name:'Hash check'}).click();
    const digest = page.getByRole('textbox',{name:'Expected SHA-256'});
    await digest.fill('a1'); assert(await button('Verify checksum').isDisabled());
    await digest.fill('0'.repeat(64)); await button('Verify checksum').click();
    await page.getByRole('heading',{name:'Checksum mismatch'}).waitFor();
    await digest.fill(sha(b)); await button('Verify checksum').click();
    await page.getByText(/A later match does not erase an earlier mismatch/).waitFor();
    // Real transport interruption in this browser only. No synthetic response and no worker restart.
    await button('Advanced inspection controls').click();
    await context.setOffline(true);
    try {
      await button('Refresh inspector').click();
      await page.getByText(/^Files unavailable while offline/).waitFor();
      assert.equal(await text('Specimen A is empty.').count(),0);
      await page.screenshot({path:path.join(output,'inspect-offline.png')});
    } finally { await context.setOffline(false); }
    await button('Refresh inspector').click();
    await page.getByText(/3 entries recorded\./).first().waitFor();
    report.observations.push('Compare, retain, export-then-replace, matching bytes, invalid digest, mismatch then match, transport loss and recovery');
  } finally { await context.close(); }
}

(async () => {
  await fs.mkdir(output,{recursive:false});
  const browser = await chromium.launch({channel:'chromium'});
  try {
    await layout(browser);
    if (mode) await inspection(browser);
  } finally {
    await browser.close();
    await fs.writeFile(path.join(output,'browser-results.json'),JSON.stringify(report,null,2));
  }
  console.log(`${report.views.length} route/viewport checks passed; ${report.observations.join('; ')}`);
})().catch(error => {console.error(error);process.exitCode = 1;});
