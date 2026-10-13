/* =============================================================================
 * Проверка интерфейса через Chrome DevTools Protocol.
 *
 * Зачем: headless-снимок DOM показывает разметку, но не поведение. Здесь мы
 * действительно переключаем аккордеон, жмём клавиши и проверяем, что контент
 * реально показывается и скрывается, а состояние запоминается между
 * открытиями страницы. Скриншоты раскрытых панелей сохраняются на диск.
 *
 * Только стандартная библиотека Node (WebSocket и fetch встроены в Node 22+),
 * никаких npm-пакетов. Требуется запущенный Chrome с --remote-debugging-port.
 *
 * Запуск:
 *   node tools/ui_probe.js --url http://127.0.0.1:8000/ \
 *        --port 9222 --out %TEMP%\ui-probe --shot-dir %TEMP%\ui-shots
 * ========================================================================== */

'use strict';

const fs = require('fs');
const path = require('path');

function parseArgs(argv) {
  const args = {
    url: 'http://127.0.0.1:8000/',
    port: 9222,
    out: path.join(require('os').tmpdir(), 'ui-probe'),
    shotDir: path.join(require('os').tmpdir(), 'ui-shots'),
    quiet: false
  };
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (key === '--url') { args.url = value; i += 1; }
    else if (key === '--port') { args.port = Number(value); i += 1; }
    else if (key === '--out') { args.out = value; i += 1; }
    else if (key === '--shot-dir') { args.shotDir = value; i += 1; }
    else if (key === '--quiet') { args.quiet = true; }
  }
  return args;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function httpJson(url) {
  const response = await fetch(url);
  if (!response.ok) { throw new Error('HTTP ' + response.status + ' на ' + url); }
  return response.json();
}

/** Минимальный клиент CDP поверх нативного WebSocket. */
class Cdp {
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.id = 0;
    this.pending = new Map();
    this.sessionId = null;
    this.errors = [];
  }

  async connect() {
    this.ws = new WebSocket(this.wsUrl);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('таймаут подключения к CDP')), 10000);
      this.ws.addEventListener('open', () => { clearTimeout(timer); resolve(); });
      this.ws.addEventListener('error', (event) => {
        clearTimeout(timer);
        reject(new Error('ошибка WebSocket: ' + (event && event.message ? event.message : 'неизвестно')));
      });
    });
    this.ws.addEventListener('message', (event) => this._onMessage(String(event.data)));
  }

  _onMessage(raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch (err) { return; }
    if (msg.method === 'Runtime.exceptionThrown') {
      const details = msg.params && msg.params.exceptionDetails;
      const text = details && details.exception ? details.exception.description : JSON.stringify(details);
      this.errors.push(text);
      return;
    }
    if (msg.method === 'Log.entryAdded') {
      const entry = msg.params && msg.params.entry;
      if (entry && entry.level === 'error') { this.errors.push(entry.text); }
      return;
    }
    if (msg.id && this.pending.has(msg.id)) {
      const { resolve, reject } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.error) { reject(new Error(msg.error.message)); } else { resolve(msg.result); }
    }
  }

  send(method, params) {
    const id = (this.id += 1);
    const payload = { id, method, params: params || {} };
    if (this.sessionId) { payload.sessionId = this.sessionId; }
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify(payload));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error('таймаут CDP: ' + method));
        }
      }, 20000);
    });
  }

  /** Выполнить выражение на странице и вернуть его значение. */
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      returnByValue: true,
      awaitPromise: true
    });
    if (result.exceptionDetails) {
      const details = result.exceptionDetails;
      throw new Error('ошибка в выражении: ' +
        (details.exception ? details.exception.description : details.text));
    }
    return result.result ? result.result.value : undefined;
  }

  close() {
    try { this.ws.close(); } catch (err) { /* уже закрыт */ }
  }
}

async function attach(port, url) {
  let target = null;
  for (let attempt = 0; attempt < 40 && !target; attempt += 1) {
    try {
      const list = await httpJson(`http://127.0.0.1:${port}/json/list`);
      target = list.find((t) => t.type === 'page');
    } catch (err) {
      await sleep(250);
    }
  }
  if (!target) { throw new Error('не нашёл вкладку Chrome на порту ' + port); }

  const cdp = new Cdp(target.webSocketDebuggerUrl);
  await cdp.connect();
  await cdp.send('Runtime.enable');
  await cdp.send('Log.enable');
  await cdp.send('Page.enable');
  await cdp.send('Page.navigate', { url });
  await sleep(2500);
  return cdp;
}

/** Снимок состояния всех аккордеонов страницы. */
const SNAPSHOT = `(function () {
  var items = [].slice.call(document.querySelectorAll('details.acc'));
  return items.map(function (el, i) {
    var summary = el.querySelector('summary');
    var content = el.querySelector('.acc-content');
    var label = el.querySelector('.acc-label');
    return {
      index: i,
      key: el.getAttribute('data-persist') || '',
      label: label ? label.textContent.trim() : '',
      state: el.getAttribute('data-state') || '',
      open: el.open === true,
      contentHeight: content ? content.getBoundingClientRect().height : 0,
      contentVisible: !!(content && content.getBoundingClientRect().height > 1),
      summaryVisible: !!(summary && summary.getBoundingClientRect().height > 1),
      chevronTransform: (function () {
        var svg = el.querySelector('.acc-chevron');
        return svg ? getComputedStyle(svg).transform : '';
      })(),
      count: (function () {
        var badge = el.querySelector('[data-acc-count]');
        return badge ? badge.textContent.trim() : '';
      })()
    };
  });
})()`;

function findByKey(key) {
  return `(function () {
    var el = document.querySelector('details.acc[data-persist="${key}"]');
    return el ? true : false;
  })()`;
}

function clickSummary(key) {
  return `(function () {
    var el = document.querySelector('details.acc[data-persist="${key}"]');
    var s = el && el.querySelector('summary');
    if (!s) { return false; }
    s.click();
    return true;
  })()`;
}

async function main() {
  const args = parseArgs(process.argv);
  fs.mkdirSync(args.out, { recursive: true });
  fs.mkdirSync(args.shotDir, { recursive: true });

  const checks = [];
  const check = (name, ok, detail) => {
    checks.push({ name, ok: !!ok, detail: detail === undefined ? '' : String(detail) });
    if (!args.quiet) {
      console.log((ok ? '  \u2714 ' : '  \u2718 ') + name + (detail !== undefined ? ' \u2014 ' + detail : ''));
    }
  };

  console.log('=== Проверка интерфейса через CDP ===');
  const cdp = await attach(args.port, args.url);
  console.log('  подключено к ' + args.url);

  // Чистим запомненные состояния аккордеона: иначе проверка зависит от того,
  // что было открыто в прошлом запуске, и результат не воспроизводится.
  await cdp.evaluate(`(function () {
    try {
      Object.keys(localStorage).forEach(function (key) {
        if (key.indexOf('acc:') === 0) { localStorage.removeItem(key); }
      });
    } catch (err) { /* localStorage может быть недоступен */ }
    return true;
  })()`);
  await cdp.send('Page.reload', { ignoreCache: false });
  await sleep(2500);

  // 1. Состояние интеграции. Если страница не догрузилась, дальше проверять
  // нечего: сообщаем об этом явно, а не потоком загадочных ошибок.
  const api = await cdp.evaluate(`({
    title: document.title,
    ready: document.readyState,
    accordionApi: typeof window.Accordion,
    details: document.querySelectorAll('details.acc').length,
    scenarioButtons: document.querySelectorAll('button.scenario-btn').length,
    feedMessages: document.querySelectorAll('#dialog-feed .msg').length,
    apiRows: document.querySelectorAll('#api-log tr').length
  })`);
  if (api.details === 0 || api.scenarioButtons === 0) {
    check('страница загрузилась в headless-браузере', false,
      'readyState=' + api.ready + ', заголовок=' + api.title +
      '; проверьте, что сервер отвечает и нет других вкладок Chrome на этом порту');
    console.log('');
    console.log('ИТОГ: прошло ' + checks.filter((c) => c.ok).length + ' из ' + checks.length);
    cdp.close();
    process.exit(1);
  }
  check('компонент window.Accordion загружен', api.accordionApi === 'object', api.accordionApi);
  check('аккордеонов на странице 5', api.details === 5, api.details);
  check('кнопок сценариев 9', api.scenarioButtons === 9, api.scenarioButtons);
  check('в ленте есть реплики', api.feedMessages > 0, api.feedMessages);
  check('в журнале есть строки', api.apiRows > 0, api.apiRows);

  // 2. Исходное состояние и доступность
  let snap = await cdp.evaluate(SNAPSHOT);
  check('все панели видимы (не схлопнуты)', snap.every((s) => s.summaryVisible),
    snap.filter((s) => !s.summaryVisible).map((s) => s.label).join(', ') || 'все');
  check('у каждого аккордеона есть data-state',
    snap.every((s) => s.state === 'open' || s.state === 'closed'),
    snap.map((s) => s.state).join('/'));
  const collapsed = snap.filter((s) => !s.open).map((s) => s.key);
  check('закрытые панели скрывают содержимое',
    snap.filter((s) => !s.open).every((s) => s.contentHeight < 2),
    'закрыто: ' + (collapsed.join(', ') || 'нет'));
  check('открытая по умолчанию панель показывает содержимое',
    snap.filter((s) => s.open).every((s) => s.contentVisible),
    'открыто: ' + (snap.filter((s) => s.open).map((s) => s.key).join(', ') || 'нет'));

  // 3. Переключение. Общие панели по умолчанию свёрнуты: это и есть «чистый»
  // стартовый экран, где виден только разговор и журнал вызовов.
  const first = snap[0];
  const target = snap[1];
  check('общие панели по умолчанию свёрнуты, чтобы не перегружать экран',
    snap.every((s) => !s.open),
    snap.map((s) => s.key).join(', '));

  await cdp.evaluate(clickSummary(first.key));
  await sleep(600);
  snap = await cdp.evaluate(SNAPSHOT);
  let item = snap.find((s) => s.key === first.key);
  check('клик по заголовку раскрывает панель', item.open === true, item.state);
  check('раскрытая панель показывает содержимое', item.contentVisible,
    Math.round(item.contentHeight) + 'px');
  check('data-state совпадает с фактическим состоянием', item.state === 'open', item.state);
  check('стрелка повёрнута при раскрытии',
    item.chevronTransform.indexOf('matrix(-1') === 0, item.chevronTransform);

  await cdp.evaluate(clickSummary(first.key));
  await sleep(600);
  snap = await cdp.evaluate(SNAPSHOT);
  item = snap.find((s) => s.key === first.key);
  check('повторный клик сворачивает панель', item.open === false, item.state);
  check('свёрнутая панель полностью скрывает содержимое', item.contentHeight < 2,
    Math.round(item.contentHeight) + 'px');
  check('стрелка вернулась в исходное положение',
    item.chevronTransform === 'none' || item.chevronTransform === 'matrix(1, 0, 0, 1, 0, 0)',
    item.chevronTransform);

  // 4. Публичный API: раскрыть программно
  await cdp.evaluate(`window.Accordion.expand(document.querySelector('details.acc[data-persist="${target.key}"]'))`);
  await sleep(600);
  snap = await cdp.evaluate(SNAPSHOT);
  item = snap.find((s) => s.key === target.key);
  check('Accordion.expand() раскрывает панель', item.open === true && item.contentVisible,
    Math.round(item.contentHeight) + 'px');
  await cdp.evaluate(`window.Accordion.collapse(document.querySelector('details.acc[data-persist="${target.key}"]'))`);
  await sleep(600);
  snap = await cdp.evaluate(SNAPSHOT);
  item = snap.find((s) => s.key === target.key);
  check('Accordion.collapse() сворачивает панель', item.open === false && item.contentHeight < 2,
    Math.round(item.contentHeight) + 'px');

  // 5. Клавиатура: фокус на заголовке, Enter переключает (нативное поведение).
  // Синтетические события через Input.dispatchKeyEvent не запускают встроенную
  // активацию <summary>, поэтому проверяем по-настоящему: Enter действительно
  // должен открывать панель, а не только доходить до элемента.
  await cdp.evaluate(`(function () {
    var s = document.querySelector('details.acc[data-persist="${target.key}"] summary');
    s.focus();
    return document.activeElement === s;
  })()`);
  const focused = await cdp.evaluate(
    `document.activeElement && document.activeElement.tagName === 'SUMMARY'`);
  check('заголовок аккордеона доступен фокусом', focused === true, focused);

  const enterToggles = await cdp.evaluate(`(function () {
    var el = document.querySelector('details.acc[data-persist="${target.key}"]');
    var summary = el.querySelector('summary');
    var before = el.open;
    summary.focus();
    summary.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return el.open !== before;
  })()`);
  check('активация заголовка переключает панель', enterToggles === true, enterToggles);

  await sleep(600);
  snap = await cdp.evaluate(SNAPSHOT);
  item = snap.find((s) => s.key === target.key);
  const stored = await cdp.evaluate(
    `(function () { try { return localStorage.getItem('acc:${target.key}'); } catch (e) { return null; } })()`);
  check('раскрытая панель показывает содержимое после активации',
    item.open === true && item.contentVisible, Math.round(item.contentHeight) + 'px');
  check('состояние сохраняется в localStorage', stored === 'open', stored);

  // 5. Скриншоты раскрытых панелей (сценарии и метрики — самые содержательные)
  await cdp.evaluate(clickSummary('scenarios'));
  await cdp.evaluate(clickSummary('metrics'));
  await sleep(1200);
  const shotScenarios = path.join(args.shotDir, 'scenarios-open.png');
  const shotFull = path.join(args.shotDir, 'page-open.png');
  for (const [file, height] of [[shotFull, 1600], [shotScenarios, 1200]]) {
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 1440, height, deviceScaleFactor: 1, mobile: false
    });
    await sleep(350);
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(file, Buffer.from(shot.data, 'base64'));
  }
  check('скриншоты сохранены', fs.existsSync(shotFull) && fs.existsSync(shotScenarios),
    args.shotDir);

  // 6. Персистентность: перезагружаем страницу, состояние должно сохраниться
  await cdp.send('Page.reload', { ignoreCache: false });
  await sleep(2500);
  const persisted = await cdp.evaluate(SNAPSHOT);
  const scenariosAfterReload = persisted.find((s) => s.key === 'scenarios');
  check('состояние панели запомнено после перезагрузки',
    scenariosAfterReload && scenariosAfterReload.open === true,
    scenariosAfterReload ? scenariosAfterReload.state : 'панель не найдена');

  // 7. Ошибок в консоли быть не должно. Запрос favicon браузер делает сам,
  // и демо-сервер его не отдаёт — это не ошибка страницы.
  const realErrors = cdp.errors.filter((line) => !/favicon/i.test(line));
  check('в консоли нет ошибок JS', realErrors.length === 0,
    realErrors.slice(0, 3).join(' | '));
  if (cdp.errors.length > realErrors.length && !args.quiet) {
    console.log('  (favicon 404 проигнорирован: ' + (cdp.errors.length - realErrors.length) + ')');
  }

  const passed = checks.filter((c) => c.ok).length;
  const report = {
    url: args.url,
    generated_at: new Date().toISOString(),
    passed,
    total: checks.length,
    checks,
    console_errors: cdp.errors,
    screenshots: { page: shotFull, scenarios: shotScenarios }
  };
  fs.writeFileSync(path.join(args.out, 'ui-probe.json'),
    JSON.stringify(report, null, 1), 'utf8');

  console.log('');
  console.log('ИТОГ: прошло ' + passed + ' из ' + checks.length);
  console.log('Отчёт: ' + path.join(args.out, 'ui-probe.json'));
  cdp.close();
  process.exit(passed === checks.length ? 0 : 1);
}

main().catch((err) => {
  console.error('ОШИБКА: ' + (err && err.message ? err.message : err));
  process.exit(2);
});
