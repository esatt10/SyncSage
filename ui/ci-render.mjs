/**
 * Render the tuning page against a real region and fail on any console error.
 *
 * Typechecking proves the page compiles against the API's shapes. It does not
 * prove the page *renders*: `api/types.ts` is hand-written, so a field that is
 * actually nullable and typed as a number produces a type-clean build and a
 * blank panel in a browser. A chart dividing by zero on an empty sweep is the
 * same class of thing.
 *
 * So this boots the real server over a real region, opens the page, and treats
 * a console error as a failure. It also asserts the sections that must exist
 * are present — a page that renders nothing renders without errors too.
 */
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium } from 'playwright';

const ROOT = new URL('..', import.meta.url).pathname;
const work = mkdtempSync(join(tmpdir(), 'pheasant-ci-'));

function run(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { cwd: ROOT, stdio: 'inherit', ...opts });
    child.on('exit', (code) => (code === 0 ? resolve() : reject(new Error(`${cmd} exited ${code}`))));
  });
}

// A region with indexed content, recorded proof, sampled traffic and one
// completed batch — everything the page has a panel for.
await run('python', ['-c', `
import sys; sys.path.insert(0, "${ROOT}")
from pathlib import Path
import yaml, logging
logging.disable(logging.INFO)
from tests.test_tuning_batch import _write_config, _seed, QUERIES
from pheasant.config.schema import PheasantConfig
work = Path("${work}")
cfg, path = _write_config(work)
raw = yaml.safe_load(path.read_text())
raw["observability"] = {"interactions": {"enabled": True, "stage_sample_rate": 1.0}}
raw.setdefault("server", {})["port"] = 8799
path.write_text(yaml.safe_dump(raw))
from pheasant.api.app import create_app
app = create_app(PheasantConfig.model_validate(raw), config_path=str(path))
e = app.state.engine
e.sync_source("docs", "full")
_seed(e)
from pheasant.tuning.runner import run_tuning
print("batch:", run_tuning(e, force=True).status)
from fastapi.testclient import TestClient
c = TestClient(app)
for _ in range(4):
    for q in QUERIES:
        c.post("/search", json={"query": q, "max_results": 2})
buf = getattr(app.state, "interaction_buffer", None)
if buf is not None and hasattr(buf, "flush"): buf.flush()
e.close()
`]);

const server = spawn('python', ['-m', 'pheasant', 'serve', '--config', join(work, 'pheasant.yaml')], {
  cwd: ROOT,
  stdio: 'inherit',
});
process.on('exit', () => server.kill());

const base = 'http://127.0.0.1:8765';
for (let i = 0; i < 60; i += 1) {
  try {
    const r = await fetch(`${base}/health`);
    if (r.ok) break;
  } catch { /* not up yet */ }
  await new Promise((r) => setTimeout(r, 1000));
}

const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM || undefined });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', (e) => errors.push(String(e)));

await page.goto(`${base}/tuning`, { waitUntil: 'networkidle' });
await page.waitForTimeout(3000);

const body = await page.textContent('body');
const required = [
  'Retrieval tuning',
  'Live pipeline health',
  'What this region ranks with',
  'Where retrieval loses documents',
];
const missing = required.filter((text) => !body.includes(text));

// A sweep chart must actually have drawn geometry — an empty <svg> passes a
// text assertion and tells a reader nothing.
const marks = await page.locator('.sweep svg circle').count();

await browser.close();
server.kill();

if (errors.length) {
  console.error('console errors:', errors);
  process.exit(1);
}
if (missing.length) {
  console.error('sections missing from the rendered page:', missing);
  process.exit(1);
}
if (marks === 0) {
  console.error('no sweep marks were drawn; the charts rendered empty');
  process.exit(1);
}
console.log(`tuning page rendered clean: ${marks} sweep marks, no console errors`);
