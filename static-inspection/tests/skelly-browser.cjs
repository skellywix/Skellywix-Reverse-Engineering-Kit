const assert = require('node:assert/strict');

// The care check never uploads a sample, starts a workflow, or resets a case.
async function checkCare(page) {
  const pet = page.getByRole('group', { name: 'Skellywix', exact: true });
  const button = name => pet.getByRole('button', { name, exact: true });
  const sprite = button('Pet Skellywix');
  const options = button('Skellywix options');
  const actions = pet.getByRole('group', { name: 'Skellywix actions' });
  const speech = pet.getByRole('status');
  const writes = [];
  const watch = req => { if (req.method() !== 'GET' && req.method() !== 'HEAD') writes.push(req.method()); };
  page.on('request', watch);
  try {
    assert.equal(await pet.count(), 1, 'Expected one floating Skelly, not a section to visit');
    assert(await pet.evaluate(el => getComputedStyle(el).position === 'fixed' && !el.closest('main')));
    const before = await page.locator('#workspace').boundingBox();
    await options.focus(); await page.keyboard.press('Enter'); await actions.waitFor();
    await page.keyboard.press('Escape'); await actions.waitFor({ state: 'hidden' });
    assert(await options.evaluate(b => b === document.activeElement), 'Escape from the opener must work immediately');
    await sprite.focus(); await page.keyboard.press('Enter');
    const first = (await speech.innerText()).trim(); assert(first);
    await page.keyboard.press('Enter'); assert.notEqual((await speech.innerText()).trim(), first);
    await page.keyboard.press('Escape'); assert.equal((await speech.innerText()).trim(), '');
    const spot = await sprite.boundingBox(); const view = page.viewportSize();
    await page.mouse.move(spot.x + spot.width/2, spot.y + spot.height/2); await page.mouse.down();
    await page.mouse.move(35, view.height - 65, { steps: 12 }); await page.mouse.up();
    assert((await sprite.boundingBox()).x < view.width/2, 'Drag should dock her on the left');
    assert.equal((await speech.innerText()).trim(), '', 'Dragging must not pet her');
    await options.click(); await button('Move to next corner').click();
    assert((await sprite.boundingBox()).y < view.height/2, 'Keyboard-accessible move should change the dock');
    await options.click(); await button('Minimize Skellywix').click();
    const show = button('Show Skellywix'); assert(await show.evaluate(b => b === document.activeElement));
    await page.mouse.move(view.width / 2, view.height / 2);
    const restingWidth = (await show.boundingBox()).width;
    await show.hover();
    assert.equal((await show.boundingBox()).width, restingWidth, 'Hover must not expand the minimized pet');
    assert.equal(await show.evaluate(b => getComputedStyle(b).cursor), 'pointer');
    await page.keyboard.press('Enter'); assert(await sprite.isVisible());
    await options.click(); await button('Let her nap').click(); const wake = button('Wake Skellywix');
    assert(await wake.isVisible()); await options.click(); assert(await button('Give a treat').isDisabled());
    await page.keyboard.press('Escape'); await wake.click(); assert(await sprite.isVisible());
    assert.deepEqual(await page.locator('#workspace').boundingBox(), before, 'Pet actions must not move the desk');
    assert.deepEqual(writes, [], 'Care must not issue a state-changing request');
  } finally { page.off('request', watch); }
}

// A focused text field on a narrow screen hides the pet's controls, and they return on blur.
// Selecting the hash tool is a local view change; nothing is uploaded or written.
async function checkYield(page) {
  const pet = page.getByRole('group', { name: 'Skellywix', exact: true });
  const sprite = pet.getByRole('button', { name: 'Pet Skellywix', exact: true });
  const original = page.viewportSize();
  const writes = [];
  const watch = req => { if (req.method() !== 'GET' && req.method() !== 'HEAD') writes.push(req.method()); };
  page.on('request', watch);
  try {
    await page.setViewportSize({ width: 390, height: 800 });
    await page.getByRole('button', { name: 'Check hash', exact: true }).click();
    const field = page.getByLabel('Expected SHA-256');
    await field.focus();
    await page.waitForFunction(() => document.querySelector('.skelly-pet')?.dataset.yield === 'true');
    assert.equal(await sprite.evaluate(el => getComputedStyle(el).visibility), 'hidden', 'Pet must not cover the focused field');
    await field.evaluate(el => el.blur());
    await page.waitForFunction(() => document.querySelector('.skelly-pet')?.dataset.yield === 'false');
    assert.equal(await sprite.evaluate(el => getComputedStyle(el).visibility), 'visible', 'Pet returns after the field loses focus');
    assert.deepEqual(writes, [], 'The yield check must not issue a state-changing request');
  } finally { page.off('request', watch); await page.setViewportSize(original); }
}
module.exports = checkCare;
module.exports.checkYield = checkYield;

if (require.main === module) {
  const { chromium } = require('playwright');
  (async () => {
    const browser = await chromium.launch({ channel: 'chromium' });
    try {
      const page = await browser.newPage({ viewport: { width: 1320, height: 900 }, reducedMotion: 'reduce' });
      await page.goto(process.env.DESK_URL || 'http://127.0.0.1:48730/index.html');
      await page.locator('footer small').waitFor();
      await checkCare(page); console.log('PASS: floating Skelly, keyboard, drag, care, and unchanged desk.');
      await checkYield(page); console.log('PASS: floating Skelly yields to a focused field on a narrow screen.');
    } finally { await browser.close(); }
  })().catch(error => { console.error(error); process.exitCode = 1; });
}
