// Execute the actual dashboard handler/API helper with controlled DOM and HTTP responses.
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const html = fs.readFileSync(input.dashboard, 'utf8');
function section(start, end) {
  return html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
}
const box = { style: {}, innerHTML: '', textContent: '' };
Object.defineProperty(box, 'textContent', {
  get() { return this.text || ''; },
  set(value) { this.text = value; this.innerHTML = ''; },
});
const calls = [];
const context = {
  API_BASE: '',
  document: {
    querySelectorAll: () => input.links.map(value => ({ value })),
    getElementById: () => box,
  },
  showToast: message => { box.textContent = message; },
  fetch: async (path, opts) => {
    calls.push({ path, body: JSON.parse(opts.body) });
    if (input.networkError) throw new Error(input.networkError);
    return { ok: (input.status || 200) < 400, status: input.status || 200,
      statusText: 'Backend error', json: async () => input.response };
  },
};
vm.createContext(context);
vm.runInContext(section('    async function parseApiError(', '    async function apiGet('), context);
vm.runInContext(section('    async function apiPost(', '    async function apiPut('), context);
vm.runInContext(section('    async function executeWhatIf(', '    // EVENT LOG'), context);
(async () => {
  await context.executeWhatIf();
  process.stdout.write(JSON.stringify({ html: box.innerHTML, text: box.textContent, calls }));
})().catch(error => { process.stderr.write(error.stack); process.exitCode = 1; });
