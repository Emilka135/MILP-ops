import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
export const root = path.dirname(fileURLToPath(import.meta.url));
export const outputs = path.resolve(root, '../docs/presentations');
export function validate(deck) {
  if (!deck || !Array.isArray(deck.slides) || !deck.slides.length || deck.slides.length > 100) throw Error('Нужно от 1 до 100 слайдов');
  if (deck.width !== 1280 || deck.height !== 720) throw Error('Размер сцены: 1280 × 720');
  const ids = new Set();
  const color = c => typeof c === 'string' && (c.startsWith('$') ? /^[0-9a-f]{6}$/i.test(deck.theme?.[c.slice(1)] || '') : /^[0-9a-f]{6}$/i.test(c));
  for (const slide of deck.slides) {
    if (!/^[a-zA-Z0-9_-]+$/.test(slide.id || '') || ids.has(slide.id)) throw Error('У слайда должен быть уникальный id');
    ids.add(slide.id);
    if (!color(slide.background) || !Array.isArray(slide.elements)) throw Error('Неверный фон или элементы');
    const elementIds = new Set();
    for (const e of slide.elements) {
      if (!/^[a-zA-Z0-9_-]+$/.test(e.id || '') || elementIds.has(e.id)) throw Error('Повтор id элемента');
      elementIds.add(e.id);
      if (![e.x,e.y,e.w,e.h].every(Number.isFinite) || e.x < 0 || e.y < 0 || e.w <= 0 || e.h <= 0 || e.x+e.w>1280.1 || e.y+e.h>720.1) throw Error(`Элемент за границей слайда: ${e.id}`);
      if (e.type === 'text') {
        if (typeof e.text !== 'string' || e.text.length > 20000 || !Number.isFinite(e.size) || e.size < 5 || e.size > 150 || !color(e.color)) throw Error(`Неверный текст: ${e.id}`);
      } else if (e.type !== 'rect' || !color(e.fill) || !Number.isFinite(e.radius) || e.radius < 0) throw Error(`Неверная фигура: ${e.id}`);
    }
  }
  return deck;
}
export async function loadDeck() { return validate(JSON.parse(await readFile(path.join(root,'deck.json'),'utf8'))); }
const escape = text => String(text).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
function initialSlides(deck) {
  const color=c=>'#'+(c.startsWith('$')?deck.theme[c.slice(1)]:c);
  return deck.slides.map((slide,index)=>`<section class="slide${index===0?' active':''}" id="${slide.id}" style="background:${color(slide.background)}">${slide.elements.map(e=>{
    const style=`left:${e.x}px;top:${e.y}px;width:${e.w}px;height:${e.h}px;`;
    if(e.type==='rect')return `<div class="element rect" data-id="${e.id}" style="${style}background:${color(e.fill)};border-radius:${e.radius}px"></div>`;
    return `<div class="element text" data-id="${e.id}" style="${style}font-size:${e.size*96/72}px;font-weight:${e.bold?700:400};color:${color(e.color)}">${e.text.split('\n').map(t=>`<p>${escape(t)||'&nbsp;'}</p>`).join('')}</div>`;
  }).join('')}</section>`).join('');
}
export async function html(deck, { editable = false } = {}) {
  validate(deck);
  const [css, js, regular, bold] = await Promise.all([
    readFile(path.join(root,'src/styles.css'),'utf8'),readFile(path.join(root,'src/viewer.js'),'utf8'),
    readFile(path.join(root,'assets/NotoSans-Regular.ttf')),readFile(path.join(root,'assets/NotoSans-Bold.ttf'))
  ]);
  const fonts=`@font-face{font-family:Deck;src:url(data:font/ttf;base64,${regular.toString('base64')});font-weight:400}@font-face{font-family:Deck;src:url(data:font/ttf;base64,${bold.toString('base64')});font-weight:700}`;
  const resources = editable
    ? '<link rel="stylesheet" href="/assets/styles.css"><link rel="stylesheet" href="/assets/fonts.css">'
    : `<style>${fonts}\n${css}</style>`;
  const script = editable ? '<script src="/assets/viewer.js" defer></script>' : `<script>${js}</script>`;
  return `<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Презентация · Регистратура</title>${resources}</head><body data-editable="${editable}"><header><strong>Регистратура / презентация</strong><span id="status">Один источник → все форматы</span><button id="edit">Править</button><button id="save">${editable?'Сохранить':'Скачать JSON'}</button>${editable?'<button id="build">Экспорт</button>':''}<button id="fullscreen">На весь экран</button></header><aside id="rail"></aside><main><div id="viewport"><div id="slides">${initialSlides(deck)}</div></div><nav><button id="prev">←</button><span id="counter"></span><button id="next">→</button></nav>${editable?'<div id="downloads"><a href="/downloads/clinic-voice-assistant.pdf" target="_blank">PDF</a> · <a href="/downloads/clinic-voice-assistant.pptx">PPTX</a> · <a href="/downloads/clinic-voice-assistant-editable.pptx">Редактируемый PPTX</a></div>':''}<details id="notes"><summary>Заметки докладчика</summary><p></p></details></main><aside id="inspector" hidden><h2>Элемент</h2><p>Нажмите на текст или фигуру на слайде.</p><form hidden><label>Текст<textarea name="text" rows="5"></textarea></label><div class="fields"><label>Размер, pt<input name="size" type="number" min="5" max="150"></label><label>Цвет / $тема<input name="color"></label><label>X<input name="x" type="number" step="1"></label><label>Y<input name="y" type="number" step="1"></label><label>Ширина<input name="w" type="number" step="1"></label><label>Высота<input name="h" type="number" step="1"></label></div><label><input name="bold" type="checkbox"> Жирный</label><p class="hint">Размер сцены: 1280 × 720. Сохранение: Ctrl/Cmd+S. Выделение и панель не входят в экспорт.</p></form><button id="download">Скачать исходник JSON</button></aside><noscript><p class="startup-error">JavaScript выключен. Первый слайд доступен; для навигации включите JavaScript или откройте PDF.</p></noscript><script id="deck-data" type="application/json">${JSON.stringify(deck).replaceAll('<','\\u003c')}</script>${script}</body></html>`;
}
