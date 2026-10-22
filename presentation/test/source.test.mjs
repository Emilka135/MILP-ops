import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { loadDeck, validate, html, outputs } from '../lib.mjs';

test('источник содержит валидные слайды и редактируемые текстовые элементы',async()=>{
 const d=await loadDeck();assert.ok(d.slides.length>0);assert.ok(d.slides.every(s=>s.elements.some(e=>e.type==='text')));
});
test('валидатор отклоняет выход за сцену, неверные цвета и дубликаты',async()=>{
 for(const edit of [d=>d.slides[0].elements[0].x=-1,d=>d.slides[0].background='invalid',d=>d.slides[1].id=d.slides[0].id]){
  const d=await loadDeck();edit(d);assert.throws(()=>validate(d));
 }
});
test('текст из JSON не может закрыть script HTML-экспорта',async()=>{
 const d=await loadDeck();d.slides[0].elements.find(e=>e.type==='text').text='</script><script>alert(1)</script>';
 const output=await html(d);assert.ok(output.includes('\\u003c/script>'));assert.ok(!output.includes('<script>alert(1)</script>'));
});
test('все форматы присутствуют, старые slides-vN не нужны для сборки',async()=>{
 const count=(await loadDeck()).slides.length;
 for(const name of ['index.html','clinic-voice-assistant.pdf','clinic-voice-assistant.pptx','clinic-voice-assistant-editable.pptx',`slides/slide-${String(count).padStart(2,'0')}.png`]){
  assert.ok((await readFile(path.join(outputs,name))).length>100);
 }
});
