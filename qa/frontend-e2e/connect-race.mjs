// Deterministic reproduction of the intermittent "Connect Nodes modal stays open" bug.
//
// Sequence (all through the UI): add a node (its post-save sync gets a slow /demands
// response), immediately connect X1-X2 (a newer sync completes first), let the slow
// sync finish, then connect X2-X3. With the old syncState the slow, stale response
// overwrote the link list, the second connect reused ID L1, the backend rejected it
// and the modal stayed open. Run against OLD_HTML (pre-fix dashboard) and the served one.
//
// Usage: BASE_URL=http://127.0.0.1:8003 [OLD_HTML=/path/old.html] node connect-race.mjs
import { chromium } from 'playwright-core';
import fs from 'node:fs';

const BASE = process.env.BASE_URL || 'http://127.0.0.1:8003';
if (/:8000(\/|$)/.test(BASE)) { console.error('Refusing port 8000'); process.exit(2); }

async function api(method, p, body) {
  const r = await fetch(BASE + p, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  return r.json().catch(() => null);
}

async function scenario(label, html) {
  await api('POST', '/simulation/reset');
  for (const id of ['X1', 'X2', 'X3']) await api('POST', '/nodes', { id, name: id, x: 100 + 80 * id.charCodeAt(1), y: 200 });
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.route(/:8000\//, r => r.abort());
  if (html) await page.route(BASE + '/', r => r.fulfill({ status: 200, contentType: 'text/html', body: html }));
  page.on('dialog', d => d.accept());
  await page.goto(BASE + '/');
  await page.waitForTimeout(1200);

  let delayNext = false;
  await page.route('**/demands', async route => {
    if (route.request().method() === 'GET' && delayNext) {
      delayNext = false;
      await new Promise(res => setTimeout(res, 1500)); // slow response for the first sync only
    }
    route.continue();
  });

  // 1. Add node X4: its follow-up sync will receive the slow /demands response.
  delayNext = true;
  await page.click('.left-toolbar button:has-text("Add Node")');
  await page.fill('#node-input-id', 'X4');
  await page.click('#node-submit-btn');
  await page.waitForTimeout(150);
  // 2. Connect X1-X2 while that sync is still waiting.
  await page.click('.left-toolbar button:has-text("Connect Nodes")');
  await page.selectOption('#link-input-src', 'X1');
  await page.selectOption('#link-input-dst', 'X2');
  await page.click('#link-submit-btn');
  // 3. Let the slow sync complete, then connect X2-X3.
  await page.waitForTimeout(2200);
  await page.click('.left-toolbar button:has-text("Connect Nodes")');
  await page.selectOption('#link-input-src', 'X2');
  await page.selectOption('#link-input-dst', 'X3');
  await page.click('#link-submit-btn');
  await page.waitForTimeout(1200);

  const modalOpen = await page.locator('#modal-connect.active').count() > 0;
  const toast = await page.locator('#toast-text').innerText();
  const links = ((await api('GET', '/topology')).links || []).map(l => `${l.id}:${l.source}-${l.target}`).sort();
  await browser.close();
  const ok = !modalOpen && links.length === 2;
  console.log(`${label}: modalOpen=${modalOpen} links=${JSON.stringify(links)} toast="${toast}" -> ${ok ? 'OK' : 'BUG'}`);
  return ok;
}

const results = {};
if (process.env.OLD_HTML) results.old = await scenario('OLD dashboard', fs.readFileSync(process.env.OLD_HTML, 'utf8'));
results.current = [];
for (let i = 0; i < 3; i++) results.current.push(await scenario(`CURRENT dashboard run ${i + 1}`, null));
fs.writeFileSync(new URL('../frontend-connect-race.json', import.meta.url), JSON.stringify(results, null, 2));
process.exit(results.current.every(Boolean) ? 0 : 1);
