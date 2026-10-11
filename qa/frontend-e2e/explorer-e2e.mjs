// NetRescue Algorithm Explorer: browser E2E suite.
// Drives the real dashboard in headless Google Chrome against an ISOLATED server.
// Usage: BASE_URL=http://127.0.0.1:8013 node explorer-e2e.mjs
// Writes qa/explorer-e2e-results.json and qa/frontend-screenshots/explorer-*.png (new files only).
import { chromium } from 'playwright-core';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const BASE = process.env.BASE_URL || 'http://127.0.0.1:8013';
if (/:8000(\/|$)/.test(BASE)) { console.error('Refusing to run against port 8000 (live dashboard).'); process.exit(2); }
const HERE = path.dirname(fileURLToPath(import.meta.url));
const SHOTS = process.env.SHOTS_DIR || path.resolve(HERE, '..', 'frontend-screenshots');
const RESULTS = process.env.RESULTS_FILE || path.resolve(HERE, '..', 'explorer-e2e-results.json');
fs.mkdirSync(SHOTS, { recursive: true });

const results = [];
const consoleErrors = [];
const failedRequests = [];
let current = '';
function check(cond, msg, detail = '') {
  results.push({ step: current, ok: !!cond, msg, detail: cond ? '' : String(detail).slice(0, 2000) });
  console.log(`${cond ? 'PASS' : 'FAIL'}  [${current}] ${msg}${cond ? '' : '  -> ' + String(detail).slice(0, 600)}`);
}
async function api(method, p, body) {
  const res = await fetch(BASE + p, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  return { status: res.status, json: await res.json().catch(() => null) };
}

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
await page.route(/:8000\//, r => r.abort());
page.on('console', m => { if (m.type() === 'error') consoleErrors.push(`[${current}] ${m.text()}`); });
page.on('pageerror', e => consoleErrors.push(`[${current}] PAGEERROR ${e.message}`));
page.on('response', r => { if (r.status() >= 400 && !r.url().includes('favicon')) failedRequests.push(`[${current}] ${r.status()} ${r.request().method()} ${r.url()}`); });
page.on('dialog', d => d.accept());
// Every non-GET request the page sends, so explorer use can be proven read-only.
const mutating = [];
page.on('request', r => { if (r.method() !== 'GET') mutating.push({ step: current, m: r.method(), p: new URL(r.url()).pathname }); });
const EXPLORER_POSTS = [/^\/explorer\/sessions$/, /^\/explorer\/sessions\/[^/]+\/(trace|compare)$/, /^\/explorer\/live-action\/trace$/];

const shot = async (name) => { await page.waitForTimeout(450); await page.screenshot({ path: path.join(SHOTS, `explorer-${name}.png`) }); };
const settle = (ms = 300) => page.waitForTimeout(ms);
const idle = () => page.waitForFunction(() => !state.busy, null, { timeout: 20000 });
const axIdle = () => page.waitForFunction(() => AX.trace && document.getElementById('ax-overlay-msg').hidden, null, { timeout: 30000 });
const counter = () => page.locator('#ax-counter').innerText();
const axState = () => page.evaluate(() => ({ index: AX.index, n: AX.steps.length, alg: AX.trace && AX.trace.algorithm, source: AX.source, playing: AX.playing }));

// Expected network state per step, computed from the API trace (carry-forward), independent of the page.
function expectedNetworks(trace, scenario) {
  let cur = { loads: Object.fromEntries(scenario.links.map(l => [l.id, l.allocated_load || 0])), failed: scenario.links.filter(l => l.status !== 'active').map(l => l.id) };
  return trace.steps.map(s => { if (s.network) cur = s.network; return cur; });
}
const r0 = v => String(Math.round(v));

// Verify DOM against the backend trace at every step (walks forward, then checks a few backward jumps).
async function verifyEveryStep(label, sessionId, algorithm, failedLink, scenario) {
  const t = (await api('POST', `/explorer/sessions/${sessionId}/trace`, { algorithm, failed_link: failedLink })).json;
  const nets = expectedNetworks(t, scenario);
  const mism = [];
  await page.click('#ax-restart').catch(() => {});
  await page.evaluate(() => { AX.tab = 'step'; axSetTab('step'); axPause(); axGoto(0); });
  const order = [...t.steps.keys(), t.steps.length - 1, Math.floor(t.steps.length / 2), 0];
  for (let k = 0; k < order.length; k++) {
    const i = order[k];
    if (k < t.steps.length) { if (i > 0) await page.click('#ax-next'); }
    else await page.evaluate(j => { axPause(); axGoto(j); }, i);
    const s = t.steps[i];
    const dom = await page.evaluate(() => ({
      counter: document.getElementById('ax-counter').textContent,
      title: document.getElementById('ax-step-title')?.textContent,
      summary: document.querySelector('.ax-step-summary')?.textContent,
      sections: Array.from(document.querySelectorAll('.ax-sections > div')).map(d => [d.querySelector('dt').textContent, d.querySelector('dd').textContent]),
      cells: Array.from(document.querySelectorAll('#ax-panel-body .ax-table td')).map(td => td.textContent),
      labels: Object.fromEntries(Array.from(document.querySelectorAll('#ax-link-labels text')).map(el => [el.textContent.split(' ')[0], el.textContent])),
      overlays: document.querySelectorAll('#ax-overlays polyline').length,
      selected: Array.from(document.querySelectorAll('#ax-overlays polyline')).filter(p => p.getAttribute('stroke') === '#22d3ee').length,
      rings: Object.fromEntries(Array.from(document.querySelectorAll('#ax-links g[data-ax-link]')).map(g => [g.dataset.axLink, g.querySelectorAll('line')[0].style.stroke])),
      fails: Object.entries(AX.linkEls).filter(([, el]) => el.mark.getAttribute('visibility') === 'visible').map(([id]) => id).sort(),
    }));
    if (dom.counter !== `Step ${i + 1} / ${t.steps.length}`) mism.push(`#${i} counter ${dom.counter}`);
    if (dom.title !== s.title) mism.push(`#${i} title '${dom.title}' != '${s.title}'`);
    if (dom.summary !== s.summary) mism.push(`#${i} summary differs`);
    const want = s.sections.filter(x => x.text).map(x => [x.label, x.text]);
    if (JSON.stringify(dom.sections) !== JSON.stringify(want)) mism.push(`#${i} sections differ`);
    const wantCells = s.tables.flatMap(tb => (tb.rows.length ? tb.rows : [['none']]).flatMap(r => r.map(String)));
    if (JSON.stringify(dom.cells) !== JSON.stringify(wantCells)) mism.push(`#${i} table cells differ`);
    const net = nets[i];
    for (const l of scenario.links) {
      const exp = net.failed.includes(l.id) ? `${l.id} ✖` : `${l.id} ${r0(net.loads[l.id] || 0)}/${r0(l.capacity)}`;
      if (dom.labels[l.id] !== exp) mism.push(`#${i} label ${l.id} '${dom.labels[l.id]}' != '${exp}'`);
    }
    if (JSON.stringify(dom.fails) !== JSON.stringify([...net.failed].sort())) mism.push(`#${i} failed marks ${dom.fails} != ${net.failed}`);
    const paths = (s.viz.paths || []).filter(p => p.nodes && p.nodes.length > 1);
    if (dom.overlays !== paths.length) mism.push(`#${i} overlays ${dom.overlays} != ${paths.length}`);
    if (dom.selected !== paths.filter(p => p.role === 'selected').length) mism.push(`#${i} selected paths differ`);
    for (const [lid, role] of Object.entries(s.viz.links || {})) {
      if (!dom.rings[lid] || dom.rings[lid] === 'transparent') mism.push(`#${i} link role ${lid}:${role} not highlighted`);
    }
  }
  check(mism.length === 0, `${label}: all ${t.steps.length} steps (and backward jumps) show exactly the backend trace`, mism.slice(0, 12).join('\n'));
  return t;
}

try {
  current = 'setup';
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => state.initialized, null, { timeout: 15000 });
  await page.click('#btn-protected-demo');
  await idle();
  await page.waitForFunction(() => document.querySelector('#badge-protection-status')?.textContent.includes('Verified'), null, { timeout: 15000 });
  const fpBefore = (await api('GET', '/explorer/live')).json.fingerprint;
  const nonExplorerBefore = mutating.length;

  // ── open ──
  current = 'open';
  await page.click('#btn-algorithm-explorer');
  await axIdle();
  check(await page.locator('#algorithm-explorer').isVisible(), 'explorer view opens from the header button');
  check(await page.evaluate(() => getComputedStyle(document.getElementById('app-workspace')).visibility) === 'hidden', 'dashboard is hidden behind the explorer (not removed)');
  const st = await axState();
  check(st.alg === 'pacar' && st.n > 10, 'PACAR trace loads by default', JSON.stringify(st));
  check((await counter()) === `Step 1 / ${st.n}`, 'step counter starts at 1', await counter());
  const session = await page.evaluate(() => AX.session);
  const scenario = session.scenario;
  check(await page.locator('#ax-nodes g[data-ax-node]').count() === scenario.nodes.length, 'graph draws every node');
  check(await page.locator('#ax-links g[data-ax-link]').count() === scenario.links.length, 'graph draws every link');
  check((await page.locator('#ax-snapshot-card').innerText()).includes('verified'), 'snapshot card shows the verified protection plan');
  check(await page.locator('#ax-stale').isHidden(), 'no stale banner on a fresh snapshot');
  check(await page.locator('#ax-overlay-msg').isHidden(), 'no loading overlay once the trace is shown');
  await shot('01-open-pacar');

  // ── every step equals the backend ──
  current = 'pacar-steps';
  const pacar = await verifyEveryStep('PACAR', session.session_id, 'pacar', null, scenario);
  current = 'pacar-candidates';
  const ci = pacar.steps.findIndex(s => s.kind === 'pacar.candidates');
  await page.evaluate(i => { axPause(); axGoto(i); }, ci);
  const src = await page.evaluate(() => Array.from(document.querySelectorAll('#ax-nodes g')).map(g => [g.dataset.axNode, g.querySelectorAll('text')[2].textContent]));
  check(src.some(([n, b]) => n === 'A' && b === 'SOURCE') && src.some(([n, b]) => n === 'E' && b === 'DEST'), 'source and destination are highlighted on a scoring step', JSON.stringify(src));
  check((await page.locator('#ax-panel-body .ax-table tr.ax-hl').count()) === 1, 'the selected candidate row is highlighted in the table');
  await shot('02-pacar-candidates');

  // ── navigation ──
  current = 'navigation';
  await page.click('#ax-end');
  check((await counter()) === `Step ${st.n} / ${st.n}`, 'End jumps to the last step');
  check(await page.locator('#ax-next').isDisabled(), 'Next is disabled at the last step');
  await page.click('#ax-restart');
  check((await counter()) === `Step 1 / ${st.n}`, 'Restart returns to step 1');
  check(await page.locator('#ax-prev').isDisabled(), 'Previous is disabled at step 1');
  await page.locator('#ax-svg').click({ position: { x: 5, y: 5 } }).catch(() => {});
  await page.keyboard.press('ArrowRight');
  check((await counter()) === `Step 2 / ${st.n}`, 'ArrowRight advances one step', await counter());
  await page.keyboard.press('ArrowLeft');
  check((await counter()) === `Step 1 / ${st.n}`, 'ArrowLeft goes back one step', await counter());
  await page.locator('#ax-timeline').fill('7');
  check((await counter()) === `Step 8 / ${st.n}`, 'timeline jumps to a chosen step', await counter());
  await page.locator('#ax-strip button').nth(3).click();
  check((await counter()) === `Step 4 / ${st.n}`, 'clicking the phase strip jumps to that step', await counter());

  // ── play / pause / speed ──
  current = 'playback';
  await page.click('#ax-restart');
  await page.selectOption('#ax-speed', '4');
  await page.click('#ax-play');
  check((await page.locator('#ax-play').innerText()).includes('Pause'), 'Play switches the button to Pause');
  await settle(1400);
  const fastIdx = (await axState()).index;
  check(fastIdx >= 2, 'playback advances automatically at 4×', fastIdx);
  await page.click('#ax-play');
  const pausedAt = (await axState()).index;
  await settle(1200);
  check((await axState()).index === pausedAt && !(await axState()).playing, 'Pause stops playback', pausedAt);
  await page.click('#ax-restart');
  await page.selectOption('#ax-speed', '1');
  await page.click('#ax-play');
  await settle(1400);
  const slowIdx = (await axState()).index;
  await page.click('#ax-play');
  check(slowIdx < fastIdx, 'speed changes the playback rate (1× slower than 4×)', `1×:${slowIdx} 4×:${fastIdx}`);
  await page.evaluate(() => axGoto(AX.steps.length - 2));
  await page.selectOption('#ax-speed', '4');
  await page.click('#ax-play');
  await settle(1500);
  const endState = await axState();
  check(endState.index === endState.n - 1 && !endState.playing, 'playback stops by itself at the last step', JSON.stringify(endState));

  // ── algorithm switching (including a rapid sequence) ──
  current = 'switching';
  for (const alg of ['baseline', 'reactive_lp', 'protection']) {
    await page.click(`.ax-alg[data-ax-alg="${alg}"]`);
    await axIdle();
    const s2 = await axState();
    check(s2.alg === alg && s2.index === 0, `${alg} trace loads and starts at step 1`, JSON.stringify(s2));
    const apiT = (await api('POST', `/explorer/sessions/${session.session_id}/trace`, { algorithm: alg })).json;
    check((await page.locator('#ax-step-title').innerText()) === apiT.steps[0].title, `${alg}: first step equals the backend`);
  }
  await page.click('.ax-alg[data-ax-alg="baseline"]');
  await page.click('.ax-alg[data-ax-alg="pacar"]');
  await page.click('.ax-alg[data-ax-alg="reactive_lp"]');
  await axIdle();
  await settle(600);
  check((await axState()).alg === 'reactive_lp', 'after rapid switching the last choice wins (stale responses ignored)', JSON.stringify(await axState()));
  current = 'lp-steps';
  await verifyEveryStep('Reactive LP', session.session_id, 'reactive_lp', null, scenario);
  const stageFid = await page.evaluate(() => { const i = AX.steps.findIndex(s => s.kind === 'lp.stage'); axGoto(i); return document.querySelector('.ax-fid').textContent; });
  check(stageFid === 'LP stage', 'LP stage steps are labelled "LP stage", not as recorded internals', stageFid);
  await shot('03-lp-stage');
  current = 'protection-steps';
  await page.click('.ax-alg[data-ax-alg="protection"]');
  await axIdle();
  await verifyEveryStep('Protection Planner', session.session_id, 'protection', null, scenario);
  await shot('04-protection');

  // ── failure recovery ──
  current = 'recovery';
  await page.click('.ax-alg[data-ax-alg="recovery"]');
  await axIdle();
  check((await page.locator('#ax-fail-select').inputValue()) === 'L3', 'Failure Recovery pre-selects L3 in the link picker');
  const rec = await verifyEveryStep('Failure Recovery L3', session.session_id, 'recovery', 'L3', scenario);
  await page.evaluate(() => axGoto(0));
  check(await page.evaluate(() => AX.linkEls.L3.mark.getAttribute('visibility')) === 'visible', 'failed link L3 shows the ✖ marker from the failure step');
  await shot('05-recovery-failure');
  await page.evaluate(() => { const i = AX.steps.findIndex(s => s.kind === 'reflex.critical_flow'); axGoto(i); });
  await shot('06-reflex');
  await page.click('.ax-tab[data-ax-tab="result"]');
  const resultText = await page.locator('#ax-panel-body').innerText();
  check(resultText.includes('Re-running without tracing gives an identical result'), 'Result tab shows the untraced re-run check');
  const delivered = Object.fromEntries(rec.result.demands.map(d => [d.id, d.delivered]));
  check(delivered.P1_DEMO === 10 && delivered.D1 === 40, 'recovery keeps P1_DEMO 10 and D1 40 Mbps (as in the runbook)', JSON.stringify(delivered));
  await shot('07-result');

  // ── routing with a hypothetical failure ──
  current = 'hypothetical';
  await page.click('.ax-tab[data-ax-tab="step"]');
  await page.click('.ax-alg[data-ax-alg="pacar"]');
  await axIdle();
  check((await page.locator('#ax-fail-select').inputValue()) === '', 'auto-picked recovery link is dropped when switching to PACAR');
  await page.selectOption('#ax-fail-select', 'L1');
  await axIdle();
  check((await page.locator('#ax-step-title').innerText()).startsWith('Scenario: L1'), 'PACAR with L1 failed starts with the scenario step', await page.locator('#ax-step-title').innerText());

  // ── comparison ──
  current = 'compare';
  await page.click('.ax-tab[data-ax-tab="compare"]');
  await page.click('#ax-compare-run');
  await page.waitForFunction(() => AX.compare && AX.compare.data, null, { timeout: 30000 });
  const cmpApi = (await api('POST', `/explorer/sessions/${session.session_id}/compare`, { failed_link: 'L1' })).json;
  const cmpText = await page.locator('#ax-panel-body').innerText();
  check(cmpApi.rows.every(r => cmpText.includes(r.label)), 'comparison shows all three algorithms');
  const firstRow = await page.$$eval('#ax-panel-body table tbody tr:first-child td', tds => tds.map(t => t.textContent));
  const wantDelivered = cmpApi.rows.map(r => r.metrics.total_delivered);
  check(wantDelivered.every((v, k) => firstRow[k + 1].startsWith(`${v} Mbps`)), 'delivered totals equal the backend comparison', `${firstRow} vs ${wantDelivered}`);
  check(cmpText.includes('Notes (not measurements)'), 'commentary is separated from measurements');
  await shot('08-compare');
  await page.selectOption('#ax-fail-select', '');
  await axIdle();

  // ── read-only proof ──
  current = 'read-only';
  const explorerReqs = mutating.slice(nonExplorerBefore).filter(r => r.step !== 'setup');
  const bad = explorerReqs.filter(r => !EXPLORER_POSTS.some(rx => rx.test(r.p)));
  check(bad.length === 0, 'the explorer sent only read-only explorer requests', JSON.stringify(bad));
  check((await api('GET', '/explorer/live')).json.fingerprint === fpBefore, 'live network fingerprint unchanged after using the explorer');

  // ── back to dashboard ──
  current = 'back';
  await page.click('#ax-close');
  check(await page.locator('#algorithm-explorer').isHidden(), 'Back to dashboard closes the explorer');
  check(await page.evaluate(() => getComputedStyle(document.getElementById('app-workspace')).visibility) === 'visible', 'dashboard is visible again');
  const topo = (await api('GET', '/topology')).json;
  const labels = await page.$$eval('#links-layer text', ts => ts.map(t => t.textContent));
  const missing = topo.links.filter(l => !labels.includes(l.status === 'failed' ? `${l.id} FAILED` : `${l.id} · ${l.allocated_load.toFixed(0)}/${l.capacity}M (${l.utilization.toFixed(0)}%)`));
  check(missing.length === 0, 'dashboard canvas still shows the live network exactly', missing.map(l => l.id).join(','));

  // ── live action replay ──
  current = 'replay';
  await page.evaluate(() => { state.pickedLinkId = 'L3'; });
  await page.evaluate(() => setLinkState('L3', 'fail'));
  await idle();
  const oldSession = await page.evaluate(() => AX.session.session_id);
  await page.click('#btn-algorithm-explorer');
  check(await page.locator('#ax-overlay-msg').isVisible(), 'while re-checking, the old trace is covered (never shown as current)');
  await page.waitForFunction(old => AX.session && AX.session.session_id !== old && AX.trace && document.getElementById('ax-overlay-msg').hidden, oldSession, { timeout: 30000 });
  await page.click('.ax-tab[data-ax-tab="step"]');
  const toastText = await page.locator('#toast-text').innerText();
  check(/new snapshot/.test(toastText), 'reopening after a live change captures a new snapshot', toastText);
  check((await page.locator('#ax-live-card').innerText()).includes('Link failure'), 'last live action card shows the link failure');
  await page.click('#ax-replay-btn');
  await axIdle();
  check((await axState()).source === 'live', 'replay trace loaded');
  check((await page.locator('#ax-step-title').innerText()).startsWith('Link L3'), 'replay starts with the L3 failure', await page.locator('#ax-step-title').innerText());
  await page.click('.ax-tab[data-ax-tab="result"]');
  check((await page.locator('#ax-panel-body').innerText()).includes('Replay is identical to what happened live'), 'replay verified identical to the live outcome');
  await shot('09-replay');
  await page.click('.ax-tab[data-ax-tab="step"]');

  // ── stale detection ──
  current = 'stale';
  await api('POST', '/links/L4/fail');
  await page.waitForFunction(() => !document.getElementById('ax-stale').hidden, null, { timeout: 8000 }).catch(() => {});
  check(await page.locator('#ax-stale').isVisible(), 'stale banner appears when the live network changes');
  const staleSession = await page.evaluate(() => AX.session.session_id);
  await page.click('#ax-stale-recapture');
  await page.waitForFunction(old => AX.session.session_id !== old && AX.trace && document.getElementById('ax-overlay-msg').hidden, staleSession, { timeout: 30000 });
  await page.waitForFunction(() => document.getElementById('ax-stale').hidden, null, { timeout: 6000 }).catch(() => {});
  const fps = await page.evaluate(() => ({ session: AX.session.fingerprint, live: AX.live && AX.live.fingerprint }));
  check(await page.locator('#ax-stale').isHidden(), 'Re-capture clears the stale banner',
    JSON.stringify({ ...fps, server: (await api('GET', '/explorer/live')).json.fingerprint }));
  await page.keyboard.press('Escape');
  check(await page.locator('#algorithm-explorer').isHidden(), 'Escape returns to the dashboard');

  // ── hard refresh: graphs render without any click or resize ──
  current = 'refresh';
  const cdp = await context.newCDPSession(page);
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
  for (let k = 1; k <= 3; k++) {
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => state.initialized, null, { timeout: 15000 });
    await settle(400);
    const g = await page.evaluate(() => {
      const r = document.getElementById('network-canvas').getBoundingClientRect();
      const nodes = Array.from(document.querySelectorAll('#nodes-layer g[data-node-id]')).map(n => n.getBoundingClientRect());
      return { count: nodes.length, inside: nodes.every(b => b.width > 0 && b.left >= r.left - 1 && b.right <= r.right + 1 && b.top >= r.top - 1 && b.bottom <= r.bottom + 1), loading: getComputedStyle(document.getElementById('canvas-loading')).display };
    });
    const nNodes = (await api('GET', '/topology')).json.nodes.length;
    check(g.count === nNodes && g.inside && g.loading === 'none', `hard refresh #${k}: dashboard graph fully rendered without interaction`, JSON.stringify(g));
  }
  await page.click('#btn-algorithm-explorer');
  await axIdle();
  const ag = await page.evaluate(() => {
    const r = document.getElementById('ax-svg').getBoundingClientRect();
    const nodes = Array.from(document.querySelectorAll('#ax-nodes g[data-ax-node]')).map(n => n.getBoundingClientRect());
    return { count: nodes.length, inside: nodes.every(b => b.width > 0 && b.left >= r.left - 1 && b.right <= r.right + 1 && b.top >= r.top - 1 && b.bottom <= r.bottom + 1) };
  });
  check(ag.count === (await page.evaluate(() => AX.session.scenario.nodes.length)) && ag.inside, 'after refresh the explorer graph is fully inside its view without interaction', JSON.stringify(ag));
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: false });

  // ── responsive layouts ──
  current = 'responsive';
  for (const [w, h] of [[1280, 720], [1024, 768], [760, 900]]) {
    await page.setViewportSize({ width: w, height: h });
    await settle(500);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    check(overflow <= 0, `${w}x${h}: explorer causes no horizontal page overflow`, `${overflow}px`);
    check(await page.locator('#ax-play').isVisible() && await page.locator('#ax-close').isVisible(), `${w}x${h}: playback and Back controls visible`);
    const inView = await page.evaluate(() => {
      const r = document.getElementById('ax-svg').getBoundingClientRect();
      return Array.from(document.querySelectorAll('#ax-nodes g[data-ax-node]')).every(n => { const b = n.getBoundingClientRect(); return b.left >= r.left - 1 && b.right <= r.right + 1; });
    });
    check(inView, `${w}x${h}: graph refits to the available width`);
    await shot(`10-responsive-${w}x${h}`);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.click('#ax-close');

  // ── disconnected network ──
  current = 'disconnected';
  await api('POST', '/simulation/reset');
  for (const id of ['A', 'B', 'C']) await api('POST', '/nodes', { id, x: { A: 100, B: 300, C: 500 }[id], y: 200 });
  await api('POST', '/links', { id: 'L1', source: 'A', target: 'B', capacity: 10, latency: 1 });
  await api('POST', '/demands', { id: 'D1', source: 'A', destination: 'C', bandwidth: 5, category: 'emergency' });
  await page.evaluate(() => syncState());
  await page.click('#btn-algorithm-explorer');
  await axIdle();
  const ui = await page.evaluate(() => { const i = AX.steps.findIndex(s => s.kind === 'pacar.unreachable'); axGoto(i); return { i, title: document.getElementById('ax-step-title').textContent, badge: Array.from(document.querySelectorAll('#ax-nodes g')).map(g => g.querySelectorAll('text')[2].textContent) }; });
  check(ui.i >= 0 && ui.title === 'D1: unreachable' && ui.badge.includes('UNREACHABLE'), 'disconnected demand is shown as unreachable with highlighted endpoints', JSON.stringify(ui));
  await shot('11-disconnected');
  await page.click('#ax-close');

  // ── empty network ──
  current = 'empty';
  await api('POST', '/simulation/reset');
  await page.evaluate(() => syncState());
  await page.click('#btn-algorithm-explorer');
  await axIdle().catch(() => {});
  await settle(500);
  check((await page.locator('#ax-overlay-text').innerText()).includes('no nodes'), 'empty network shows a clear empty state', await page.locator('#ax-overlay-text').innerText());
  await page.click('.ax-alg[data-ax-alg="recovery"]');
  await settle(800);
  check((await page.locator('#ax-overlay-text').innerText()).includes('no active link'), 'Failure Recovery on an empty network explains why it cannot run', await page.locator('#ax-overlay-text').innerText());
  await shot('12-empty');
  await page.click('#ax-close');
  current = 'restore-demo';
  await page.click('#btn-protected-demo');
  await idle();
} catch (e) {
  check(false, 'unexpected exception', e.stack);
  await shot('zz-exception').catch(() => {});
} finally {
  await browser.close();
}

current = 'summary';
check(consoleErrors.length === 0, 'no console or page errors', consoleErrors.join('\n'));
check(failedRequests.length === 0, 'no failed HTTP requests', failedRequests.join('\n'));
const failed = results.filter(r => !r.ok);
const report = { base: BASE, at: new Date().toISOString(), total: results.length, passed: results.length - failed.length, failed: failed.length, results, consoleErrors, failedRequests };
fs.writeFileSync(RESULTS, JSON.stringify(report, null, 2));
console.log(`\n${report.passed}/${report.total} checks passed`);
process.exit(failed.length ? 1 : 0);
