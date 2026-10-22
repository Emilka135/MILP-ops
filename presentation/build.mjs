import { mkdir, writeFile, readdir, unlink } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import pptxgen from 'pptxgenjs';
import { html, loadDeck, outputs } from './lib.mjs';
import { launch } from './browser.mjs';
const deck=await loadDeck();
await mkdir(outputs,{recursive:true});
const slidesDir=path.join(outputs,'slides');await mkdir(slidesDir,{recursive:true});
const index=path.join(outputs,'index.html');await writeFile(index,await html(deck));
const browser=await launch();
try{
 const page=await browser.newPage({viewport:{width:1280,height:720},deviceScaleFactor:1.5});
 await page.goto(pathToFileURL(index).href+'?export=1');await page.evaluate(()=>window.deckReady);
 const overflow=await page.locator('.text').evaluateAll(els=>els.filter(e=>e.scrollHeight>e.clientHeight+1||e.scrollWidth>e.clientWidth+1).map(e=>e.dataset.id));
 if(overflow.length)throw Error('Переполнение текста: '+overflow.join(', '));
 const sizes=await page.locator('.text').evaluateAll(els=>Object.fromEntries(els.map(e=>[e.dataset.id,Number(e.dataset.fitted)])));
 await page.pdf({path:path.join(outputs,'clinic-voice-assistant.pdf'),preferCSSPageSize:true,printBackground:true});
 const fixed=new pptxgen(),editable=new pptxgen();
 for(const p of [fixed,editable]){p.layout='LAYOUT_WIDE';p.author='MILP-ops';p.subject='Производительность процесса регистратуры';p.title=deck.title;p.lang='ru-RU';}
 const resolve=c=>c.startsWith('$')?deck.theme[c.slice(1)]:c;
 let reduced=0;
 for(let i=0;i<deck.slides.length;i++){
  const s=deck.slides[i],image=path.join(slidesDir,`slide-${String(i+1).padStart(2,'0')}.png`);
  await page.locator('.slide').nth(i).screenshot({path:image});
  const raster=fixed.addSlide();raster.addImage({path:image,x:0,y:0,w:13.333333,h:7.5});if(s.notes)raster.addNotes(s.notes);
  const vector=editable.addSlide();vector.background={color:resolve(s.background)};if(s.notes)vector.addNotes(s.notes);
  for(const e of s.elements){
   const geom={x:e.x/96,y:e.y/96,w:e.w/96,h:e.h/96};
   if(e.type==='rect')vector.addShape(e.radius?editable.ShapeType.roundRect:editable.ShapeType.rect,{...geom,rectRadius:e.radius/96,line:{color:resolve(e.fill),transparency:100},fill:{color:resolve(e.fill)}});
   else {const fontSize=sizes[e.id];if(fontSize<e.size-.1)reduced++;vector.addText(e.text,{...geom,fontFace:'Noto Sans',fontSize,bold:e.bold,color:resolve(e.color),margin:0,breakLine:false,paraSpaceAfterPt:5,lineSpacingMultiple:1.18,valign:'top',lang:'ru-RU'});}
  }
 }
 await fixed.writeFile({fileName:path.join(outputs,'clinic-voice-assistant.pptx')});
 await editable.writeFile({fileName:path.join(outputs,'clinic-voice-assistant-editable.pptx')});
 // Remove obsolete generated screenshots when the source deck gets shorter.
 for(const name of await readdir(slidesDir)){
  const match=/^slide-(\d+)\.png$/.exec(name);
  if(match&&Number(match[1])>deck.slides.length)await unlink(path.join(slidesDir,name));
 }
 console.log(`Built ${deck.slides.length} slides: HTML, PDF, fixed PPTX, editable PPTX, PNG. Auto-fit adjusted ${reduced} text boxes; no clipping.`);
}finally{await browser.close();}
