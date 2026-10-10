// NetRescue dashboard browser E2E suite.
// Drives the real dashboard in headless Google Chrome against an ISOLATED server.
// Usage: BASE_URL=http://127.0.0.1:8003 node run-e2e.mjs
// Any request to port 8000 (the user's live dashboard) is aborted and fails the run.
import { chromium } from 'playwright-core';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const BASE = process.env.BASE_URL || 'http://127.0.0.1:8003';
if (/:8000(\/|$)/.test(BASE)) {
  console.error('Refusing to run against port 8000 (live dashboard).');
  process.exit(2);
}
const HERE = path.dirname(fileURLToPath(import.meta.url));
const SHOTS = path.resolve(HERE, '..', 'frontend-screenshots');
fs.mkdirSync(SHOTS, { recursive: true });

const results = [];
const consoleErrors = [];
const failedRequests = [];
const blocked = [];
let current = '';

function check(cond, msg, detail = '') {
  results.push({ step: current, ok: !!cond, msg, detail: cond ? '' : String(detail) });
  console.log(`${cond ? 'PASS' : 'FAIL'}  [${current}] ${msg}${cond ? '' : '  -> ' + detail}`);
}

async function api(method, p, body) {
  const res = await fetch(BASE + p, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined
  });
  return { status: res.status, json: await res.json().catch(() => null) };
}

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
const page = await context.newPage();

await page.route(/:8000\//, r => { blocked.push(r.request().method() + ' ' + r.request().url()); r.abort(); });
page.on('console', m => { if (m.type() === 'error') consoleErrors.push(`[${current}] ${m.text()}`); });
page.on('pageerror', e => consoleErrors.push(`[${current}] PAGEERROR ${e.message}`));
page.on('response', r => {
  const u = r.url();
  if (r.status() >= 400 && !u.includes('favicon')) failedRequests.push(`[${current}] ${r.status()} ${r.request().method()} ${u}`);
});
page.on('requestfailed', r => {
  if (!r.url().includes(':8000/') && !r.url().includes('fonts.g')) failedRequests.push(`[${current}] FAILED ${r.method()} ${r.url()} ${r.failure()?.errorText}`);
});
page.on('dialog', d => d.accept());

const shot = (name) => page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
const postCounts = {};
page.on('request', r => {
  if (r.method() !== 'GET') {
    const k = r.method() + ' ' + new URL(r.url()).pathname;
    postCounts[k] = (postCounts[k] || 0) + 1;
  }
});
const countOf = (k) => postCounts[k] || 0;

// Every number the dashboard shows must equal the live API response.
async function uiMatchesApi(label) {
  const prev = current;
  current = `${prev}:ui=api`;
  const topo = (await api('GET', '/topology')).json;
  const dem = (await api('GET', '/demands')).json.demands;
  const plan = (await api('GET', '/protection/plan')).json;
  const met = (await api('GET', '/metrics')).json;
  const labels = await page.$$eval('#links-layer text', ts => ts.map(t => t.textContent));
  const mism = [];
  for (const l of topo.links) {
    const want = l.status === 'failed' ? `${l.id} FAILED`
      : l.status === 'flapping' ? `${l.id} FLAPPING (held down)`
      : `${l.id} · ${l.allocated_load.toFixed(0)}/${l.capacity}M (${l.utilization.toFixed(0)}%)`;
    if (!labels.includes(want)) mism.push(`link label ${want} not shown`);
  }
  const rows = await page.$$eval('#demands-table-body tr', trs => trs.map(tr => Array.from(tr.cells).map(c => c.textContent.trim())));
  for (const d of dem) {
    const row = rows.find(r => r[0] === d.id);
    if (!row) { mism.push(`demand ${d.id} missing`); continue; }
    const delivered = d.status === 'pending' ? '—' : `${d.allocated_bandwidth.toFixed(1)} Mbps`;
    if (row[4] !== delivered) mism.push(`${d.id} delivered UI '${row[4]}' API '${delivered}'`);
    if (row[3] !== `${d.bandwidth.toFixed(0)} Mbps`) mism.push(`${d.id} requested UI '${row[3]}'`);
  }
  if (plan.active) {
    for (const f of plan.critical_flows.filter(f => f.planned)) {
      const cell = await page.$eval(`#protection-table-body tr[data-flow-id="${f.flow_id}"] .guaranteed-cell`, e => e.textContent.trim()).catch(() => null);
      const g = Math.round(f.guaranteed_mbps * 1e4) / 1e4;
      const want = Number.isInteger(g) ? `${g} Mbps` : `${Number(f.guaranteed_mbps.toFixed(2))} Mbps`;
      if (cell !== want) mism.push(`${f.flow_id} guarantee UI '${cell}' API '${want}'`);
    }
  }
  const delivBw = await page.$eval('#metric-deliv-bw', e => e.textContent.trim());
  if (dem.length && delivBw !== `${met.total_delivered.toFixed(0)} / ${met.total_requested.toFixed(0)} Mbps`) mism.push(`metrics '${delivBw}'`);
  check(mism.length === 0, `${label}: every displayed value equals the API`, mism.join('; '));
  current = prev;
}
const settle = (ms = 400) => page.waitForTimeout(ms);
const text = (sel) => page.locator(sel).innerText();
const tab = async (id) => { await page.click(`.tray-tab-btn[data-tab="${id}"]`); await settle(500); };
const toast = () => page.locator('#toast-text').innerText();
// Every dashboard action sets state.busy synchronously; wait until it clears.
const idle = () => page.waitForFunction(() => !state.busy, null, { timeout: 20000 });
async function launchDemo() {
  await page.click('#btn-protected-demo');
  await idle();
  await page.waitForFunction(() => document.querySelector('#badge-protection-status')?.textContent.includes('Verified'), null, { timeout: 15000 });
  await settle(300);
}

async function demandRow(id) {
  const row = page.locator('#demands-table-body tr', { has: page.locator(`td:first-child:text-is("${id}")`) });
  return (await row.count()) ? (await row.innerText()) : null;
}
async function protectionCell(id) {
  const row = page.locator(`#protection-table-body tr[data-flow-id="${id}"] .guaranteed-cell`);
  return (await row.count()) ? (await row.innerText()).trim() : null;
}

async function protectedDemoJourney(run) {
  current = `journey-${run}:launch`;
  await launchDemo();
  check((await text('#sim-btn-text')) === 'Stop Simulation', 'simulation started automatically');
  check((await text('#protection-total-guaranteed')).startsWith('50 / 50'), 'P1+P2 guaranteed = 50 / 50 Mbps', await text('#protection-total-guaranteed'));
  check((await protectionCell('P1_DEMO')) === '10 Mbps', 'P1_DEMO guarantee 10 Mbps', await protectionCell('P1_DEMO'));
  check((await protectionCell('D1')) === '40 Mbps', 'D1 guarantee 40 Mbps', await protectionCell('D1'));
  check((await text('#protection-scenarios')) === '6', 'six covered scenarios', await text('#protection-scenarios'));
  check((await text('#protection-checker')) === 'PASS', 'guarantee checker PASS');
  check(await page.locator('.notice-ok').count() === 1, 'shows positive full-protection notice');
  check((await page.locator('#link-picker').inputValue()) === 'L3', 'L3 pre-selected in link picker');
  check((await text('#fail-btn-text')) === 'Fail L3', 'fail button names L3 exactly');
  await shot(`journey${run}-01-protected-demo`);

  current = `journey-${run}:pre-failure`;
  await tab('tray-demands');
  const pre = { p1: await demandRow('P1_DEMO'), d1: await demandRow('D1') };
  check(pre.p1 && pre.p1.includes('10.0 Mbps') && pre.p1.includes('Fully Served'), 'P1_DEMO delivered 10 Mbps before failure', pre.p1);
  check(pre.d1 && pre.d1.includes('40.0 Mbps') && pre.d1.includes('Fully Served'), 'D1 delivered 40 Mbps before failure', pre.d1);
  await uiMatchesApi('pre-failure');
  await shot(`journey${run}-02-pre-failure-traffic`);

  current = `journey-${run}:fail-L3`;
  await page.click('#fail-btn');
  await page.waitForFunction(() => document.querySelector('#link-picker option[value="L3"]')?.textContent.includes('FAILED'), null, { timeout: 15000 });
  await settle(600);
  const t = await toast();
  check(t.includes('L3') && t.includes('Reflex used') && !t.includes('NOT'), 'toast reports Reflex used', t);
  check(t.includes('Validator violations: 0'), 'toast reports 0 validator violations', t);
  const insp = await text('#inspector-body');
  check(insp.includes('Reflex used') && insp.includes('YES'), 'inspector shows Reflex YES', insp);
  const svgLabel = await page.locator('#links-layer text', { hasText: 'L3 FAILED' }).count();
  check(svgLabel === 1, 'canvas labels L3 as FAILED');
  const l1 = await page.locator('#links-layer text', { hasText: /^L1 · 50\/50M \(100%\)$/ }).count();
  const l2 = await page.locator('#links-layer text', { hasText: /^L2 · 50\/50M \(100%\)$/ }).count();
  check(l1 === 1 && l2 === 1, 'L1 and L2 at 50/50 Mbps (100%)');
  await shot(`journey${run}-03-L3-failed`);

  current = `journey-${run}:post-failure`;
  const post = { p1: await demandRow('P1_DEMO'), d1: await demandRow('D1') };
  check(post.p1 && post.p1.includes('10.0 Mbps') && post.p1.includes('Fully Served'), 'P1_DEMO still 10 Mbps after L3 failure', post.p1);
  check(post.d1 && post.d1.includes('40.0 Mbps') && post.d1.includes('Fully Served'), 'D1 still 40 Mbps after L3 failure', post.d1);
  await tab('tray-metrics');
  const lf = await text('#last-failure-card');
  check(lf.includes('L3 failed') && /P1_DEMO\s+P1\s+10 Mbps\s+10 Mbps\s+10 Mbps\s+✓ kept/.test(lf), 'last-failure card: P1_DEMO 10→10, guarantee kept', lf);
  check(/D1\s+P2\s+40 Mbps\s+40 Mbps\s+40 Mbps\s+✓ kept/.test(lf), 'last-failure card: D1 40→40, guarantee kept', lf);
  check((await text('#metric-failed-ids')).includes('L3'), 'metrics list L3 as down');
  check((await text('#metric-crit-bw')).startsWith('50 / 50'), 'critical delivered 50/50 Mbps', await text('#metric-crit-bw'));
  await uiMatchesApi('post-failure');
  await shot(`journey${run}-04-metrics`);
  await tab('tray-protection');
  const pnote = await text('#protection-plan-summary-box');
  check(pnote.includes('Covered failure in progress: L3 is down') && pnote.includes('does not cover a further failure'),
        'protection tab says the guarantee is in force for L3 down but no further failure is covered', pnote.slice(0, 300));
  check((await text('#badge-protection-status')).includes('L3 down'), 'protection badge shows L3 down', await text('#badge-protection-status'));

  current = `journey-${run}:events`;
  await tab('tray-events');
  const ev = await text('#event-log-list');
  check(ev.includes('link_failure') && ev.includes('Link A↔C (L3) failed'), 'event log shows L3 failure', ev.slice(0, 300));
  check(ev.includes('reflex_recovery'), 'event log shows reflex_recovery');
  await shot(`journey${run}-05-events`);

  current = `journey-${run}:what-if`;
  await tab('tray-whatif');
  const topoBefore = await api('GET', '/topology');
  await page.check('.whatif-checkbox[value="L1"]');
  await page.click('#whatif-run-btn');
  await page.waitForSelector('#whatif-assessment', { timeout: 15000 });
  const wi = await text('#whatif-results-box');
  check(wi.includes('What-If Disaster Assessment (L1)') && wi.includes('Live network unchanged'), 'What-If L1 renders without mutating', wi);
  check(wi.includes('Controller response replayed'), 'What-If states it replays the real controller', wi);
  const topoAfter = await api('GET', '/topology');
  const st = (t) => t.json.links.map(l => `${l.id}:${l.status}`).join(',');
  check(st(topoBefore) === st(topoAfter), 'API confirms link states unchanged by What-If', `${st(topoBefore)} vs ${st(topoAfter)}`);
  await shot(`journey${run}-06-what-if`);
  await page.click('#whatif-clear-btn');

  current = `journey-${run}:compare`;
  await tab('tray-compare');
  await page.click('#compare-run-btn');
  await page.waitForSelector('#compare-grid', { timeout: 20000 });
  const cmp = await text('#compare-results-box');
  check(cmp.includes('PACAR') && cmp.includes('Reactive') && cmp.includes('failed link(s): L3'), 'comparison renders all three algorithms with L3 context', cmp.slice(0, 300));
  check(!cmp.includes('NaN') && !cmp.includes('undefined'), 'comparison has no NaN/undefined');
  check(cmp.includes('heuristic (no optimality guarantee)') && cmp.includes('optimum over candidate tunnels'),
        'comparison labels heuristic vs LP optimality honestly', cmp.slice(0, 400));
  await shot(`journey${run}-07-compare`);

  current = `journey-${run}:restore-L3`;
  await page.click('#restore-btn');
  await page.waitForFunction(() => !document.querySelector('#link-picker option[value="L3"]')?.textContent.includes('FAILED'), null, { timeout: 15000 });
  await settle(600);
  check((await text('#toast-text')).includes('L3') && (await text('#toast-text')).includes('restored'), 'restore toast names L3');
  await tab('tray-demands');
  const r1 = await demandRow('D1'), r2 = await demandRow('D2'), r3 = await demandRow('D3');
  check(r1.includes('40.0 Mbps') && r1.includes('Fully Served') && r2.includes('30.0 Mbps') && r2.includes('Fully Served') && /\t(5\d|6\d|7\d|80)\.0 Mbps/.test(r3), 'traffic restored (D1 40, D2 30, D3 >= 50 Mbps)', [r1, r2, r3].join(' | '));
  check(await page.locator('#links-layer text', { hasText: 'FAILED' }).count() === 0, 'no links shown as failed');
  await uiMatchesApi('restored');
  await shot(`journey${run}-08-restored`);
}

try {
  // ---------- Initial load ----------
  current = 'initial-load';
  await api('POST', '/simulation/reset');
  await page.goto(BASE + '/');
  await settle(1200);
  check(await page.title() !== '', 'dashboard loads');
  check(await page.locator('#empty-state-guide').isVisible(), 'empty state shown for empty network');
  check((await text('#metric-deliv-rate')) === '—', 'metrics show — (not 100%) with no data', await text('#metric-deliv-rate'));
  await shot('00-empty-state');

  // ---------- Positive demo journey, twice ----------
  await protectedDemoJourney(1);
  current = 'reset';
  await launchDemo();
  check((await text('#inspector-body')).includes('Guided Tips') || (await text('#inspector-title')).includes('L3'), 'demo reset cleanly');
  await page.reload();
  await settle(1500);
  current = 'refresh';
  check((await text('#sim-btn-text')) === 'Stop Simulation', 'after reload, simulation state recovered from backend');
  check((await protectionCell('D1')) === '40 Mbps', 'after reload, D1 guarantee still 40 Mbps', await protectionCell('D1'));
  await protectedDemoJourney(2);

  // ---------- Node click must not destroy the protection plan ----------
  current = 'node-click-keeps-plan';
  await launchDemo();
  const nodeA = page.locator('#nodes-layer g[data-node-id="A"]');
  await nodeA.click();
  await settle(500);
  const planAfterClick = await api('GET', '/protection/plan');
  check(planAfterClick.json.active === true, 'clicking a node keeps the protection plan active', JSON.stringify(planAfterClick.json).slice(0, 200));
  const box = await nodeA.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 40, box.y + box.height / 2 + 30, { steps: 5 });
  await page.mouse.up();
  await settle(1200);
  const planAfterDrag = await api('GET', '/protection/plan');
  check(planAfterDrag.json.active === true, 'dragging a node re-verifies the plan (still active)');
  await page.selectOption('#link-picker', 'L3');
  await page.click('#fail-btn');
  await page.waitForFunction(() => document.querySelector('#link-picker option[value="L3"]')?.textContent.includes('FAILED'), null, { timeout: 15000 });
  check((await toast()).includes('Reflex used') && !(await toast()).includes('NOT'), 'Reflex still used after node drag', await toast());
  await page.click('#restore-all-btn');
  await settle(800);
  check(await page.locator('#links-layer text', { hasText: 'FAILED' }).count() === 0, 'Restore All restores L3');

  // ---------- Repeated fail/restore cycles: flap damping is shown, demo reload resets it ----------
  current = 'repeat-cycles';
  await launchDemo();
  let sawFlapping = false;
  for (let i = 0; i < 3 && !sawFlapping; i++) {
    await page.selectOption('#link-picker', 'L3');
    await page.click('#fail-btn');
    await idle();
    await page.click('#restore-btn');
    await idle();
    await settle(300);
    sawFlapping = (await page.locator('#link-picker option[value="L3"]').innerText()).includes('FLAPPING');
  }
  check(sawFlapping, 'repeated fail/restore of L3 is shown as FLAPPING (held down)');
  check((await api('GET', '/topology')).json.links.find(l => l.id === 'L3').flap_count === 3,
        'hold-down happens on the restore after the third failure (not the second)');
  await page.selectOption('#link-picker', 'L3');
  const hold = await text('#hold-down-notice').catch(() => '');
  check(hold.includes('failed 3 times within 10 controller steps') && /after \d more controller step/.test(hold),
        'held-down link explains failure count and remaining controller steps', hold);
  await uiMatchesApi('held-down');
  check(await page.locator('#links-layer text', { hasText: 'L3 FLAPPING' }).count() === 1, 'canvas labels L3 FLAPPING');
  check(await page.locator('#fail-btn').isDisabled(), 'Fail is disabled for a held-down link');
  check((await toast()).includes('NOT returned to service'), 'toast explains the link was not restored', await toast());
  await shot('13-flapping-hold-down');
  await launchDemo();
  await page.click('#fail-btn');
  await idle();
  check((await toast()).includes('Reflex used') && !(await toast()).includes('NOT'), 'after reloading the demo, failing L3 uses Reflex again', await toast());
  const m = await api('GET', '/metrics');
  check(m.json.critical_delivered === 50, 'critical traffic 50 Mbps after re-run', JSON.stringify(m.json.critical_delivered));
  await page.click('#restore-btn');
  await idle();

  // ---------- Reload during a failure: UI must reflect the live backend state ----------
  current = 'reload-during-failure';
  await launchDemo();
  await page.click('#fail-btn');
  await idle();
  await page.reload();
  await settle(1500);
  check((await page.locator('#link-picker option[value="L3"]').innerText()).includes('FAILED'), 'after reload L3 is still shown as failed');
  check((await text('#badge-protection-status')).includes('L3 down'), 'after reload the plan shows the covered failure in progress', await text('#badge-protection-status'));
  check((await text('#sim-btn-text')) === 'Stop Simulation', 'after reload the simulation is shown as active');
  await uiMatchesApi('reloaded during failure');
  await page.selectOption('#link-picker', 'L3');
  await page.click('#restore-btn');
  await idle();

  // ---------- Pan and wheel zoom ----------
  current = 'pan-zoom';
  const tf0 = await page.locator('#canvas-world').getAttribute('transform');
  const vp = await page.locator('#canvas-viewport').boundingBox();
  await page.mouse.move(vp.x + 30, vp.y + vp.height - 30);
  await page.mouse.down();
  await page.mouse.move(vp.x + 130, vp.y + vp.height - 80, { steps: 5 });
  await page.mouse.up();
  const tf1 = await page.locator('#canvas-world').getAttribute('transform');
  check(tf1 !== tf0 && /scale\(/.test(tf1), 'dragging the empty canvas pans the view', `${tf0} -> ${tf1}`);
  await page.mouse.move(vp.x + vp.width / 2, vp.y + vp.height / 2);
  await page.mouse.wheel(0, -300);
  await settle(200);
  const tf2 = await page.locator('#canvas-world').getAttribute('transform');
  check(tf2 !== tf1, 'mouse wheel zooms the view', `${tf1} -> ${tf2}`);
  const planPan = (await api('GET', '/protection/plan')).json;
  check(planPan.active && planPan.in_force, 'panning and zooming do not touch the protection plan');
  await page.click('button[title="Fit View"]');

  // ---------- Second failure: plan no longer in force ----------
  current = 'second-failure';
  await launchDemo();
  await page.click('#fail-btn');
  await idle();
  await page.selectOption('#link-picker', 'L1');
  await page.click('#fail-btn');
  await idle();
  check((await toast()).includes('Reflex NOT used'), 'second failure is reported as not covered by Reflex', await toast());
  await tab('tray-protection');
  check((await text('#protection-plan-summary-box')).includes('Plan not in force'), 'protection tab says the plan is not in force');
  check((await text('#badge-protection-status')).includes('not in force'), 'badge says not in force', await text('#badge-protection-status'));
  await uiMatchesApi('two failures');
  await shot('14-second-failure-not-in-force');
  await page.click('#restore-all-btn');
  await idle();

  // ---------- Double clicks run an action once ----------
  current = 'double-click';
  await launchDemo();
  let before = countOf('POST /links/L3/fail');
  await page.dblclick('#fail-btn');
  await idle();
  await settle(500);
  check(countOf('POST /links/L3/fail') - before === 1, 'double-clicking Fail sends exactly one failure', String(countOf('POST /links/L3/fail') - before));
  before = countOf('POST /simulate');
  await page.dblclick('#sim-toggle-btn');
  await settle(1200);
  const simSent = countOf('POST /simulate') - before;
  check(simSent <= 1, 'double-clicking the simulation toggle never sends duplicate simulate requests', String(simSent));
  await launchDemo();

  // ---------- normal_campus: honest zero guarantees + What-If L1, L7 ----------
  current = 'sample-network';
  await page.click('#btn-sample-network');
  await settle(1000);
  check((await text('#sim-btn-text')) === 'Start Simulation', 'sample loads in draft mode');
  const d1Draft = await demandRow('D1');
  check(d1Draft.includes('Not simulated yet') && !d1Draft.includes('0.0 Mbps'), 'unsimulated demand shows — not 0 Mbps', d1Draft);
  await page.click('#sim-toggle-btn');
  await settle(1200);
  await tab('tray-protection');
  const warn = await page.locator('.notice-warn').innerText().catch(() => '');
  check(warn.includes('0 Mbps is guaranteed for every P1/P2 flow'), 'zero-guarantee warning shown for normal_campus', warn);
  check((await text('#badge-protection-status')).includes('0 Mbps'), 'tab badge says Verified · 0 Mbps', await text('#badge-protection-status'));
  check((await text('#protection-total-guaranteed')).startsWith('0 / 65'), 'total guaranteed 0 / 65 Mbps', await text('#protection-total-guaranteed'));
  const d8 = await page.locator('#protection-table-body tr[data-flow-id="D8"]').innerText();
  check(d8.includes('Not planned (P3–P5 policy)'), 'P5 flow labelled not planned (not NONE)', d8);
  await shot('10-normal-campus-zero-guarantee');

  current = 'what-if-L1';
  await tab('tray-whatif');
  await page.check('.whatif-checkbox[value="L1"]');
  await page.click('#whatif-run-btn');
  await page.waitForSelector('#whatif-assessment', { timeout: 15000 });
  let wi = await text('#whatif-results-box');
  check(wi.includes('Delivered Before:') && wi.includes('195.00 Mbps') && !wi.includes('undefined') && !wi.includes('failed'), 'What-If L1 shows numbers, no crash', wi);
  current = 'what-if-L1+L7';
  await page.check('.whatif-checkbox[value="L7"]');
  await page.click('#whatif-run-btn');
  await page.waitForFunction(() => document.querySelector('#whatif-assessment')?.innerText.includes('L1, L7'), null, { timeout: 15000 });
  wi = await text('#whatif-results-box');
  check(wi.includes('180.00 Mbps') && wi.includes('15.00 Mbps') && wi.includes('D6, D7'), 'What-If L1+L7: 195→180 Mbps, D6/D7 unreachable', wi);
  await shot('11-what-if-L1-L7');
  current = 'what-if-L7';
  await page.uncheck('.whatif-checkbox[value="L1"]');
  await page.click('#whatif-run-btn');
  await page.waitForFunction(() => /\(L7\)/.test(document.querySelector('#whatif-assessment')?.innerText || ''), null, { timeout: 15000 });
  wi = await text('#whatif-results-box');
  check(wi.includes('D6, D7') && !wi.includes('undefined'), 'What-If L7 alone: D6, D7 unreachable', wi);

  // ---------- Exact link failure from the canvas ----------
  current = 'canvas-link-select';
  await page.locator('#links-layer g[data-link-id="L7"] line').last().click({ force: true });
  await settle(300);
  check((await page.locator('#link-picker').inputValue()) === 'L7', 'clicking L7 on canvas selects L7 in picker');
  check((await text('#inspector-title')) === 'Link: L7', 'inspector shows L7');
  await page.click('#inspector-toggle-link');
  await page.waitForFunction(() => document.querySelector('#link-picker option[value="L7"]')?.textContent.includes('FAILED'), null, { timeout: 15000 });
  const fl = (await api('GET', '/failures')).json.failed_links.map(l => l.id);
  check(JSON.stringify(fl) === '["L7"]', 'exactly L7 failed (no other link)', JSON.stringify(fl));
  await shot('12-L7-failed-normal-campus');
  await page.click('#inspector-toggle-link');
  await settle(800);
  check((await api('GET', '/failures')).json.failed === 0, 'L7 restored from inspector');

  // ---------- CRUD controls ----------
  current = 'crud';
  await page.click('#btn-clear-network');
  await settle(800);
  check(Object.keys((await api('GET', '/topology')).json.nodes).length === 0, 'Clear empties the network');
  for (const id of ['X1', 'X2', 'X3']) {
    await page.click('.left-toolbar button:has-text("Add Node")');
    await page.fill('#node-input-id', id);
    await page.fill('#node-input-name', `Node ${id}`);
    await page.click('#node-submit-btn');
    await settle(400);
  }
  check((await api('GET', '/topology')).json.nodes.length === 3, 'created 3 nodes');
  await page.click('.left-toolbar button:has-text("Add Node")');
  await page.fill('#node-input-id', 'X1');
  await page.click('#node-submit-btn');
  await settle(300);
  check((await toast()).includes('already exists'), 'duplicate node ID rejected with message', await toast());
  await page.click('#modal-add-node button:has-text("Cancel")');

  // edit node
  await page.locator('#nodes-layer g[data-node-id="X2"]').click();
  await page.click('#inspector-body button:has-text("Edit Node")');
  await page.fill('#node-input-name', 'Renamed X2');
  await page.selectOption('#node-input-type', 'server');
  await page.click('#node-submit-btn');
  await settle(500);
  const x2 = (await api('GET', '/topology')).json.nodes.find(n => n.id === 'X2');
  check(x2.name === 'Renamed X2' && x2.node_type === 'server', 'edit node saved', JSON.stringify(x2));

  // connect nodes
  const connect = async (a, b, cap) => {
    await page.click('.left-toolbar button:has-text("Connect Nodes")');
    await page.selectOption('#link-input-src', a);
    await page.selectOption('#link-input-dst', b);
    await page.fill('#link-input-cap', String(cap));
    await page.click('#link-submit-btn');
    await settle(400);
    if (await page.locator('#modal-connect.active').count()) {
      const msg = await toast();
      if (!msg.includes('already exists')) check(false, `connect ${a}-${b} left modal open`, msg);
    }
  };
  await connect('X1', 'X2', 100);
  await connect('X2', 'X3', 100);
  await connect('X1', 'X3', 60);
  const links = (await api('GET', '/topology')).json.links.map(l => l.id).sort();
  check(JSON.stringify(links) === '["L1","L2","L3"]', 'links get short IDs L1..L3', JSON.stringify(links));
  await connect('X2', 'X1', 50);
  check((await toast()).includes('already exists'), 'parallel link rejected');
  await page.click('#modal-connect button:has-text("Cancel")');
  await page.click('.left-toolbar button:has-text("Connect Nodes")');
  await page.fill('#link-input-cap', '-5');
  await page.click('#link-submit-btn');
  await settle(200);
  check((await toast()).includes('positive'), 'negative capacity rejected client-side', await toast());
  await page.click('#modal-connect button:has-text("Cancel")');

  // edit link
  await page.selectOption('#link-picker', 'L3');
  await page.click('#inspector-body button:has-text("Edit Link")');
  await page.fill('#link-input-cap', '80');
  await page.fill('#link-input-lat', '7');
  await page.click('#link-submit-btn');
  await settle(500);
  const L3 = (await api('GET', '/topology')).json.links.find(l => l.id === 'L3');
  check(L3.capacity === 80 && L3.latency === 7, 'edit link saved capacity/latency', JSON.stringify(L3));

  // traffic
  await page.click('.left-toolbar button:has-text("Add Traffic")');
  await page.fill('#demand-input-id', 'T1');
  await page.selectOption('#demand-input-src', 'X1');
  await page.selectOption('#demand-input-dst', 'X3');
  await page.selectOption('#demand-input-cat', 'emergency');
  await page.fill('#demand-input-bw', '20');
  await page.click('#modal-demand button:has-text("Save Demand")');
  await settle(500);
  check(!!(await demandRow('T1')), 'traffic T1 added');
  await page.click('.left-toolbar button:has-text("Add Traffic")');
  await page.fill('#demand-input-bw', '0');
  await page.click('#modal-demand button:has-text("Save Demand")');
  await settle(200);
  check((await toast()).includes('positive'), 'zero bandwidth rejected', await toast());
  await page.click('#modal-demand button:has-text("Cancel")');
  await tab('tray-demands');
  await page.click('#demands-table-body button[title="Edit T1"]');
  await page.fill('#demand-input-bw', '25');
  await page.click('#modal-demand button:has-text("Save Demand")');
  await settle(500);
  check((await demandRow('T1')).includes('25 Mbps'), 'traffic T1 edited to 25 Mbps', await demandRow('T1'));
  // Recompute Plan before routing: the plan exists but cannot be in force yet.
  await tab('tray-protection');
  await page.click('#protection-replan-btn');
  await settle(800);
  check((await text('#protection-plan-summary-box')).includes('not yet in force') &&
        (await text('#badge-protection-status')).includes('not simulated yet'),
        'plan computed before routing is reported as not yet in force', await text('#protection-plan-summary-box'));

  // simulate custom network and protection
  await page.click('#sim-toggle-btn');
  await settle(1000);
  check((await demandRow('T1')).includes('Fully Served'), 'custom network simulated');
  await tab('tray-protection');
  await page.click('#protection-replan-btn');
  await settle(1000);
  check((await protectionCell('T1')) === '25 Mbps', 'recompute plan: T1 guaranteed 25 Mbps on triangle', await protectionCell('T1'));
  await shot('20-custom-network');

  // stop simulation
  await page.click('#sim-toggle-btn');
  await settle(500);
  check((await text('#sim-btn-text')) === 'Start Simulation', 'stop simulation toggles UI');

  // delete traffic, link, node
  await tab('tray-demands');
  await page.click('#demands-table-body button[title="Delete T1"]');
  await settle(500);
  check(!(await demandRow('T1')), 'traffic deleted');
  await page.selectOption('#link-picker', 'L3');
  await page.click('#inspector-body button:has-text("Delete Link")');
  await settle(500);
  check(!(await api('GET', '/topology')).json.links.some(l => l.id === 'L3'), 'link deleted');
  await page.locator('#nodes-layer g[data-node-id="X3"]').click();
  await page.click('#inspector-body button:has-text("Delete Node")');
  await settle(500);
  check(!(await api('GET', '/topology')).json.nodes.some(n => n.id === 'X3'), 'node deleted');

  // zoom / navigation
  current = 'zoom';
  const tf = () => page.locator('#canvas-world').getAttribute('transform');
  const t0 = await tf();
  await page.click('button[title="Zoom In"]');
  const t1 = await tf();
  await page.click('button[title="Zoom Out"]');
  await page.click('button[title="Fit View"]');
  const t2 = await tf();
  await page.click('button[title="Reset View"]');
  check(t0 !== t1 && t2 !== t1 && (await tf()).includes('scale(1)'), 'zoom in/out/fit/reset change the view', `${t0} | ${t1} | ${t2}`);

  // export
  current = 'export';
  const [download] = await Promise.all([
    page.waitForEvent('download', { timeout: 10000 }),
    page.click('#btn-export')
  ]);
  const dl = path.join(SHOTS, 'export-sample.json');
  await download.saveAs(dl);
  const ex = JSON.parse(fs.readFileSync(dl, 'utf8'));
  check(ex.topology && Array.isArray(ex.topology.nodes) && Array.isArray(ex.events), 'export downloads valid JSON', Object.keys(ex).join(','));
  fs.unlinkSync(dl);

  // tray collapse
  current = 'tray';
  await page.click('#tray-collapse-btn');
  check((await text('#tray-collapse-btn')).includes('Show'), 'tray collapses');
  await tab('tray-events');
  check((await text('#tray-collapse-btn')).includes('Hide'), 'switching tab re-opens tray');

  // API error surfaces (server-side validation)
  current = 'api-errors';
  await page.evaluate(() => setLinkState('L1', 'fail'));
  await settle(500);
  await page.evaluate(async () => { state.links['L1'].status = 'active'; await setLinkState('L1', 'fail'); });
  await settle(500);
  check((await toast()).includes('Error') || (await toast()).includes('FAILED') || (await toast()).includes('already'), 'double failure surfaces a message', await toast());

  // Backend unreachable message
  current = 'backend-down';
  await page.route('**/topology', r => r.abort());
  await page.evaluate(() => syncState());
  await settle(300);
  check((await toast()).includes('Cannot reach the NetRescue backend'), 'unreachable backend shows clear message', await toast());
  await page.unroute('**/topology');

  // Clean finish
  current = 'final';
  await launchDemo();

  // Projector-size layouts: no horizontal page overflow, key controls visible.
  current = 'responsive';
  for (const [w, h] of [[1280, 720], [1024, 768]]) {
    await page.setViewportSize({ width: w, height: h });
    await settle(900); // CSS height transition + debounced refit
    check(await page.locator('#sim-toggle-btn').isVisible(), `${w}x${h}: Start/Stop Simulation visible without scrolling`);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    check(overflow <= 0, `${w}x${h}: no horizontal overflow`, `overflow ${overflow}px`);
    check(await page.locator('#btn-protected-demo').isVisible() && await page.locator('#fail-btn').isVisible(), `${w}x${h}: demo and fail controls visible`);
    await shot(`30-responsive-${w}x${h}`);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await shot('99-final-protected-demo');
} catch (e) {
  check(false, 'unexpected exception', e.stack);
  await shot('zz-exception').catch(() => {});
} finally {
  await browser.close();
}

current = 'summary';
// The deliberately aborted /topology request (backend-down test) is expected.
const unexpectedConsole = consoleErrors.filter(e => !e.startsWith('[backend-down]') && !e.startsWith('[api-errors]'));
check(blocked.length === 0, 'no requests attempted to port 8000', blocked.join('\n'));
check(unexpectedConsole.length === 0, 'no unexpected console/page errors', unexpectedConsole.join('\n'));
const unexpectedHttp = failedRequests.filter(r => !r.startsWith('[backend-down]') && !r.startsWith('[api-errors]'));
check(unexpectedHttp.length === 0, 'no unexpected failed HTTP requests', unexpectedHttp.join('\n'));

const failed = results.filter(r => !r.ok);
const report = { base: BASE, at: new Date().toISOString(), total: results.length, passed: results.length - failed.length, failed: failed.length, results, consoleErrors, failedRequests, blocked };
fs.writeFileSync(path.resolve(HERE, '..', 'frontend-e2e-results.json'), JSON.stringify(report, null, 2));
console.log(`\n${report.passed}/${report.total} checks passed`);
process.exit(failed.length ? 1 : 0);
