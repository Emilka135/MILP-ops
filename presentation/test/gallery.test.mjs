import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { gallery } from '../gallery.mjs';
import { loadDeck } from '../lib.mjs';

test('PNG-галерея без скриптов: безопасные номера, экранирование, ссылки редактора',async()=>{
 const deck=await loadDeck();
 for(const value of ['bad',Infinity,-3,0,1]) assert.ok(gallery(deck,value).includes('/slides/slide-01.png'));
 assert.ok(gallery(deck,999).includes(`/slides/slide-${String(deck.slides.length).padStart(2,'0')}.png`));
 deck.slides[0].title='<script>alert("x")</script>';
 deck.slides[0].notes='</p><img src=x onerror=alert(1)>';
 const out=gallery(deck);
 assert.ok(!out.includes('<script>'));
 assert.ok(!out.includes('<img src=x'));
 assert.ok(out.includes('&lt;script&gt;'));
 assert.ok(out.includes('href="/editor#1"'));
 assert.ok(out.includes('href="/?slide=2"'));
});

test('региональный аудит: одинаковые специальности, отдельные контуры, пропуски не нули',async()=>{
 const data=JSON.parse(await readFile(new URL('../comparison.json',import.meta.url),'utf8'));
 assert.equal(data.observations.length,20);
 const regions=[...new Set(data.observations.map(o=>o.region))];assert.equal(regions.length,2);
 const sets=regions.map(r=>data.observations.filter(o=>o.region===r).map(o=>o.specialty).sort());
 assert.deepEqual(sets[0],sets[1]);assert.equal(new Set(sets[0]).size,10);
 for(const o of data.observations){
  assert.ok(o.source_url.startsWith('https://'));
  assert.equal(o.next_free_at,null);assert.equal(o.waiting_days,null);
  assert.ok(o.missing_reason);
  assert.equal(o.patient_group,o.specialty==='Педиатр'?'child':'adult');
 }
 assert.equal(data.comparison_of_waiting,'not_estimable');
 assert.equal(data.voice_assistant['Ингушетия'].status,'not_established');
});
