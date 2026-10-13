/* Диагностика аккордеона: смотрим, что реально происходит с <details> при клике.
 * Запуск: node tools/ui_debug.js --port 9335 --url http://127.0.0.1:8000/
 */
'use strict';

const args = { port: 9335, url: 'http://127.0.0.1:8000/' };
for (let i = 2; i < process.argv.length; i += 2) {
  if (process.argv[i] === '--port') { args.port = Number(process.argv[i + 1]); }
  if (process.argv[i] === '--url') { args.url = process.argv[i + 1]; }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const list = await (await fetch(`http://127.0.0.1:${args.port}/json/list`)).json();
  const page = list.find((t) => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res) => ws.addEventListener('open', res));

  let id = 0;
  const pending = new Map();
  const logs = [];
  ws.addEventListener('message', (e) => {
    const m = JSON.parse(String(e.data));
    if (m.method === 'Runtime.consoleAPICalled') {
      logs.push(m.params.type + ': ' + m.params.args.map((a) => a.value || a.description).join(' '));
    }
    if (m.method === 'Runtime.exceptionThrown') {
      logs.push('ИСКЛЮЧЕНИЕ: ' + (m.params.exceptionDetails.exception || {}).description);
    }
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
  });
  const send = (method, params) => new Promise((res) => {
    const myId = ++id;
    pending.set(myId, res);
    ws.send(JSON.stringify({ id: myId, method, params: params || {} }));
  });

  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.navigate', { url: args.url });
  await sleep(2500);

  const ev = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) { return 'ОШИБКА: ' + JSON.stringify(r.exceptionDetails.exception); }
    return r.result.value;
  };

  await ev(`Object.keys(localStorage).filter(k=>k.indexOf('acc:')===0).forEach(k=>localStorage.removeItem(k))`);
  await send('Page.reload');
  await sleep(2200);

  console.log('=== ДО клика ===');
  console.log(await ev(`(function(){
    var el = document.querySelector('details.acc[data-persist="scenarios"]');
    var c = el.querySelector('.acc-content');
    return JSON.stringify({open: el.open, attr: el.hasAttribute('open'), state: el.getAttribute('data-state'), h: Math.round(c.getBoundingClientRect().height)});
  })()`));

  console.log('=== слушатели на summary (через CDP getEventListeners недоступен, проверяем флаг) ===');
  console.log(await ev(`(function(){
    var s = document.querySelector('details.acc[data-persist="scenarios"] summary');
    return JSON.stringify({tag: s.tagName, hasOnclick: !!s.onclick, accordionApi: typeof window.Accordion});
  })()`));

  console.log('=== вызываем s.click() и сразу смотрим ===');
  console.log(await ev(`(function(){
    var el = document.querySelector('details.acc[data-persist="scenarios"]');
    var s = el.querySelector('summary');
    s.click();
    return JSON.stringify({open: el.open, attr: el.hasAttribute('open'), state: el.getAttribute('data-state')});
  })()`));

  await sleep(600);
  console.log('=== через 600 мс ===');
  console.log(await ev(`(function(){
    var el = document.querySelector('details.acc[data-persist="scenarios"]');
    var c = el.querySelector('.acc-content');
    return JSON.stringify({open: el.open, attr: el.hasAttribute('open'), state: el.getAttribute('data-state'), h: Math.round(c.getBoundingClientRect().height), cls: el.className});
  })()`));

  console.log('=== что в localStorage ===');
  console.log(await ev(`JSON.stringify(Object.keys(localStorage).filter(k=>k.indexOf('acc:')===0).map(k=>k+'='+localStorage.getItem(k)))`));

  console.log('=== пробуем через публичный API ===');
  console.log(await ev(`(function(){
    var el = document.querySelector('details.acc[data-persist="transfer"]');
    window.Accordion.expand(el);
    return JSON.stringify({open: el.open, state: el.getAttribute('data-state')});
  })()`));
  await sleep(400);
  console.log(await ev(`(function(){
    var el = document.querySelector('details.acc[data-persist="transfer"]');
    var c = el.querySelector('.acc-content');
    return 'после expand: open=' + el.open + ' state=' + el.getAttribute('data-state') + ' h=' + Math.round(c.getBoundingClientRect().height);
  })()`));

  console.log('=== логи консоли страницы ===');
  console.log(logs.length ? logs.join('\n') : '(пусто)');
  ws.close();
}

main().catch((e) => { console.error('ОШИБКА:', e.message); process.exit(2); });
