/* Local UI emulation only: no Telegram account, token, Codex process, or external network. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');
const { chromium, webkit } = require('playwright');

const root = path.resolve(__dirname, '..');
const staticRoot = path.join(root, 'src', 'codex_telegram', 'static');
const reportRoot = path.resolve(process.env.MOBILE_REPORT_DIR || path.join(root, '.test-results', 'mobile'));
const hostile = '<img src=x onerror="window.__injected=true">';
const original = Buffer.from('Синтетический файл\nOriginal bytes.\n', 'utf8');
const profiles = [
  { name: 'android-compact', width: 360, height: 780, keyboardHeight: 390, platform: 'android' },
  { name: 'android-wide', width: 412, height: 915, keyboardHeight: 430, platform: 'android' },
  { name: 'ios-size', width: 390, height: 844, keyboardHeight: 400, platform: 'ios' },
];
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
const report = { schema: 'mobile-ui-emulation/1', real_hardware: false, live_telegram: false,
  codex_process_started: false, external_network_used: false, cases: [] };

function fixture() {
  return {
    connected: true, error: null, telegram_status: 'running', telegram_error: null,
    selected: 'synthetic-alpha', sandbox: 'read-only', uncertain_operations: 0,
    threads: [
      { id: 'synthetic-alpha', title: 'Тестовый чат ' + hostile, status: 'inProgress' },
      { id: 'synthetic-beta', title: 'Второй тестовый чат', status: 'notLoaded' },
    ],
    messages: Array.from({ length: 36 }, (_, i) => ({ id: `synthetic-message-${i}`,
      role: i % 2 ? 'assistant' : 'user', text: `Строка ${i}: ${'Длинная синтетическая история. '.repeat(7)}${i === 35 ? hostile : ''}` })),
    pending: [
      { key: 'synthetic-question', method: 'item/tool/requestUserInput', submitted: false,
        params: { threadId: 'synthetic-alpha', turnId: 'synthetic-turn', questions: [
          { id: 'synthetic-choice', isOther: true, question: 'Выберите вариант или напишите свой: ' + hostile,
            options: [{ label: 'Первый вариант', description: 'Синтетический вариант' }, { label: 'Второй вариант' }] },
        ] } },
      { key: 'synthetic-approval', method: 'item/commandExecution/requestApproval', submitted: false, reviewable: false,
        params: { threadId: 'synthetic-alpha', turnId: 'synthetic-turn',
          command: 'synthetic-harmless-command ' + hostile, reason: 'Проверка отображения', availableDecisions: ['decline', 'accept'] } },
    ],
  };
}

async function visibleWithinViewport(page, selector) {
  const box = await page.locator(selector).boundingBox();
  const size = page.viewportSize();
  assert(box && box.width > 0 && box.height > 0, `${selector}: visible size`);
  assert(box.x >= -1 && box.y >= -1 && box.x + box.width <= size.width + 1 && box.y + box.height <= size.height + 1,
    `${selector}: clipped outside ${size.width}x${size.height}`);
}

async function runProfile(origin, profile) {
  const requested = profile.platform === 'ios' && process.env.MOBILE_USE_WEBKIT === '1' ? 'webkit' : 'chromium';
  const engine = requested === 'webkit' ? webkit : chromium;
  const launch = { headless: true };
  if (requested === 'chromium' && process.env.MOBILE_BROWSER_CHANNEL) launch.channel = process.env.MOBILE_BROWSER_CHANNEL;
  if (requested === 'chromium' && process.env.MOBILE_BROWSER_EXECUTABLE) launch.executablePath = process.env.MOBILE_BROWSER_EXECUTABLE;
  const browser = await engine.launch(launch);
  const context = await browser.newContext({ viewport: { width: profile.width, height: profile.height },
    isMobile: true, hasTouch: true, deviceScaleFactor: 1,
    userAgent: profile.platform === 'android'
      ? 'Mozilla/5.0 (Linux; Android 14; SyntheticDevice) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36'
      : 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
  });
  const row = { profile: profile.name, viewport: `${profile.width}x${profile.height}`,
    keyboard_viewport: `${profile.width}x${profile.keyboardHeight}`, engine: requested,
    browser_channel: launch.channel || (launch.executablePath ? 'explicit-executable' : 'playwright-default'),
    browser_version: browser.version(), checks: [], status: 'running' };
  report.cases.push(row);
  const state = fixture(), actions = [], errors = [], unexpected = [];
  if(process.env.MOBILE_BACKEND==='desktop'){state.backend='desktop';state.sandbox='desktop-managed';state.capabilities={new_chat:false};}
  let stage = 'initial-load';
  const allMessages = structuredClone(state.messages), allPending = structuredClone(state.pending);
  await context.addInitScript(() => {
    window.__injected = false;
    const events = {};
    window.Telegram = { WebApp: { initData: 'synthetic-test-initdata', viewportHeight: window.innerHeight,
      ready() {}, expand() {}, onEvent(name, fn) { events[name] = fn; } } };
    window.__syntheticTelegramResize = () => {
      window.Telegram.WebApp.viewportHeight = window.innerHeight;
      events.viewportChanged?.({ isStateStable: true });
    };
  });
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin === 'https://telegram.org' && url.pathname === '/js/telegram-web-app.js')
      return route.fulfill({ status: 200, contentType: 'text/javascript', body: '/* Telegram SDK mocked locally. */' });
    if (url.origin !== origin) { unexpected.push(url.origin); return route.abort(); }
    if (!url.pathname.startsWith('/api/')) return route.continue();
    assert.equal(request.method(), 'POST', 'API uses POST');
    assert.equal(request.headers()['x-telegram-init-data'], 'synthetic-test-initdata', 'initData header present');
    const body = request.postDataJSON();
    let result;
    if (url.pathname === '/api/state') result = state;
    else if (url.pathname === '/api/models') result = { models: [
      { id: 'synthetic-model', model: 'synthetic-model', name: 'Тестовая модель', efforts: ['low', 'high'] },
    ] };
    else if (url.pathname === '/api/files') result = { files: [
      { name: 'Синтетический отчёт.txt', size: original.length }, { name: hostile + '.txt', size: 0 },
    ] };
    else if (url.pathname === '/api/download') {
      assert.equal(body.name, 'Синтетический отчёт.txt');
      return route.fulfill({ status: 200, contentType: 'application/octet-stream', body: original });
    } else if (url.pathname === '/api/action') {
      assert.match(body.request_id, /^[A-Za-z0-9_-]{8,100}$/);
      actions.push(body);
      if (body.action === 'send') state.messages.push({ id: 'synthetic-sent', role: 'user', text: body.payload.text });
      if (body.action === 'select') {
        state.selected = body.payload.thread_id;
        state.messages = state.selected === 'synthetic-alpha' ? structuredClone(allMessages) : [{ id: 'beta-only', role: 'assistant', text: 'Только второй чат' }];
        state.pending = state.selected === 'synthetic-alpha' ? structuredClone(allPending) : [];
      }
      if (body.action === 'decision') {
        const item = state.pending.find(x => x.key === body.payload.key);
        assert(item, 'known synthetic decision'); item.submitted = true;
      }
      result = { status: 'completed', result: body.action === 'upload' ? { file_id: 'synthetic-upload' } : { accepted: true } };
    } else throw new Error(`Unexpected API route ${url.pathname}`);
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(result) });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(12000);
  page.on('pageerror', e => errors.push(e.message));
  try {
    await page.goto(origin, { waitUntil: 'networkidle' });
    await page.waitForFunction(() => document.querySelectorAll('#messages article').length === 36);
    assert.match(await page.locator('#status').innerText(), /Codex подключён/);
    await visibleWithinViewport(page, '#composer');
    await visibleWithinViewport(page, '#input');
    await visibleWithinViewport(page, '#send');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, 'no horizontal document overflow');
    assert.equal(await page.locator('#messages img,#decisions img').count(), 0);
    assert.equal(await page.evaluate(() => window.__injected), false);
    assert((await page.locator('#messages').innerText()).includes(hostile));
    row.checks.push('initial-layout', 'hostile-content-rendered-as-text');
    if(process.env.MOBILE_BACKEND==='desktop'){assert.equal(await page.locator('#new').isVisible(),false);assert.match(await page.locator('#status').innerText(),/Существующие чаты Desktop/);assert.equal(await page.locator('#selected-id').inputValue(),'synthetic-alpha');row.checks.push('existing-desktop-mode-and-original-id');}
    const scrolling = await page.locator('#messages').evaluate(el => {
      el.scrollTop = 0; const top = el.scrollTop; el.scrollTop = el.scrollHeight;
      return { top, bottom: el.scrollTop, clientHeight: el.clientHeight, scrollHeight: el.scrollHeight };
    });
    assert(scrolling.bottom > scrolling.top && scrolling.scrollHeight > scrolling.clientHeight);
    row.checks.push('long-history-scroll');
    await page.screenshot({ path: path.join(reportRoot, `${profile.name}-initial.png`) });

    stage = 'keyboard-layout';
    await page.locator('#input').fill('Проверка точного текста 🧪\nВторая строка.');
    await page.setViewportSize({ width: profile.width, height: profile.keyboardHeight });
    await page.evaluate(() => window.__syntheticTelegramResize());
    await page.waitForFunction(() => Math.abs(parseFloat(document.documentElement.style.getPropertyValue('--height')) - visualViewport.height) < 2);
    await visibleWithinViewport(page, '#input');
    await visibleWithinViewport(page, '#send');
    assert((await page.locator('#messages').boundingBox()).height >= 69, 'history remains visible with keyboard');
    await page.screenshot({ path: path.join(reportRoot, `${profile.name}-keyboard.png`) });
    row.checks.push('keyboard-resize', 'input-and-send-visible', 'history-remains-visible');
    stage = 'send-with-keyboard';
    await page.locator('#send').click();
    await page.waitForFunction(() => document.getElementById('input').value === '', undefined, { timeout: 8000 });
    assert.equal(actions.filter(x => x.action === 'send').length, 1);
    assert.equal(actions.find(x => x.action === 'send').payload.text, 'Проверка точного текста 🧪\nВторая строка.');
    row.checks.push('single-exact-text-send');

    stage = 'decision-input';
    await page.locator('#input').blur();
    await page.setViewportSize({ width: profile.width, height: profile.height });
    await page.evaluate(() => window.__syntheticTelegramResize());
    await page.locator('#decisions textarea').fill('Свой ответ без изменения текста 🧪');
    await page.getByRole('button', { name: 'Отправить ответ', exact: true }).click();
    await page.waitForFunction(() => [...document.querySelectorAll('#decisions button')].some(x => x.textContent === 'Отправить ответ' && x.disabled));
    assert.equal(actions.find(x => x.action === 'decision').payload.answers['synthetic-choice'], 'Свой ответ без изменения текста 🧪');
    assert.equal(await page.getByRole('button', { name: 'Разрешить один раз', exact: true }).isDisabled(), true,
      'approval with missing review context must not be accepted');
    await page.getByRole('button', { name: 'Отклонить', exact: true }).click();
    assert.equal(actions.filter(x => x.action === 'decision').length, 2);
    row.checks.push('custom-decision-text', 'nonreviewable-approval-disabled', 'explicit-decline', 'submitted-decision-disabled');

    stage = 'settings-and-download';
    await page.locator('#settings').click();
    await page.locator('#models-refresh').click();
    await page.locator('#model').selectOption('synthetic-model');
    await page.locator('#effort').selectOption('high');
    assert.equal(await page.locator('#effort').inputValue(), 'high');
    await page.locator('#files-refresh').click();
    await page.waitForFunction(() => document.querySelectorAll('#files .file-row').length === 2);
    assert.equal(await page.locator('#files img').count(), 0);
    assert.equal(await page.evaluate(() => window.__injected), false);
    const downloadPromise = page.waitForEvent('download');
    await page.locator('#files button').first().click();
    const download = await downloadPromise;
    const downloadName = download.suggestedFilename();
    // macOS WebKit can return canonical decomposition (NFD). Compare spelling
    // in NFC only; keep case, accents, extension and the file bytes exact.
    assert.equal(downloadName.normalize('NFC'), 'Синтетический отчёт.txt');
    row.download_filename = { raw: downloadName, nfc: downloadName.normalize('NFC') };
    assert.deepEqual(await fs.readFile(await download.path()), original);
    row.checks.push('model-and-effort-controls', 'file-name-safety', 'unicode-download-original-bytes');
    await page.locator('#settings').click();
    stage = 'chat-isolation';
    await page.locator('#thread-list').selectOption('synthetic-beta');
    await page.waitForFunction(() => document.getElementById('messages').textContent.includes('Только второй чат'));
    assert(!(await page.locator('#messages').innerText()).includes('Длинная синтетическая история'));
    assert.equal(await page.locator('#decisions article').count(), 0);
    row.checks.push('chat-state-isolation');
    assert.deepEqual(errors, [], 'no JavaScript errors');
    assert.deepEqual(unexpected, [], 'no unexpected external requests');
    row.checks.push('no-page-errors', 'no-external-network');
    row.status = 'pass';
  } catch (e) {
    row.status = 'fail'; row.failure = e.message; row.failed_stage = stage;
    row.observed_actions = actions.map(x => x.action);
    row.page_errors = errors;
    row.unexpected_external_origins = unexpected;
    await page.screenshot({ path: path.join(reportRoot, `${profile.name}-failure.png`) }).catch(() => {});
    throw e;
  } finally {
    await context.close(); await browser.close();
  }
}

(async () => {
  await fs.mkdir(reportRoot, { recursive: true });
  const start = Date.now();
  const server = http.createServer(async (request, response) => {
    const assets = { '/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css' };
    const asset = assets[new URL(request.url, 'http://localhost').pathname];
    if (!asset) { response.writeHead(404); response.end(); return; }
    try { response.writeHead(200, { 'Content-Type': mime[path.extname(asset)], 'Cache-Control': 'no-store' }); response.end(await fs.readFile(path.join(staticRoot, asset))); }
    catch { response.writeHead(500); response.end('Test asset unavailable'); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const origin = `http://127.0.0.1:${server.address().port}`;
    for (const profile of profiles) {
      try { await runProfile(origin, profile); }
      catch (e) {
        if (!report.cases.some(x => x.profile === profile.name))
          report.cases.push({ profile: profile.name, status: 'fail', failed_stage: 'browser-launch', failure: e.message });
        report.failure ||= e.message;
      }
    }
    report.status = report.cases.every(x => x.status === 'pass') ? 'pass' : 'fail';
    if (report.status !== 'pass') process.exitCode = 1;
  } catch (e) {
    report.status = 'fail'; report.failure = e.message; process.exitCode = 1;
  } finally {
    await new Promise(resolve => server.close(resolve));
    report.elapsed_seconds = Number(((Date.now() - start) / 1000).toFixed(3));
    await fs.writeFile(path.join(reportRoot, 'report.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report, null, 2));
  }
})();
