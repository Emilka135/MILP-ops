(() => {
  const deck = JSON.parse(document.getElementById('deck-data').textContent), $ = s => document.querySelector(s);
  window.EDITABLE = document.body.dataset.editable === 'true';
  const exporting = new URLSearchParams(location.search).has('export');
  function slideFromHash() {
    const match = /^#(?:slide-)?(\d+)$/.exec(location.hash);
    return match ? Math.max(0, Math.min(deck.slides.length-1, Number(match[1])-1)) : 0;
  }
  let current = slideFromHash(), editing = false, selected = null, dirty = false;
  const resolve = c => '#' + (c.startsWith('$') ? deck.theme[c.slice(1)] : c);
  if (exporting) document.body.classList.add('export');
  function renderSlide(s) {
    const node = document.createElement('section');node.className='slide';node.id=s.id;node.style.background=resolve(s.background);
    for (const e of s.elements) {
      const el=document.createElement('div');el.className=`element ${e.type}`;el.dataset.id=e.id;
      for(const k of ['x','y','w','h'])el.style[{x:'left',y:'top',w:'width',h:'height'}[k]]=e[k]+'px';
      if(e.type==='text'){
        el.style.fontSize=e.size*96/72+'px';el.style.fontWeight=e.bold?'700':'400';el.style.color=resolve(e.color);
        e.text.split('\n').forEach(t=>{const p=document.createElement('p');p.textContent=t||'\u00a0';el.append(p);});
      }else{el.style.background=resolve(e.fill);el.style.borderRadius=e.radius+'px';}
      el.addEventListener('click',event=>{if(editing){event.stopPropagation();select(e.id);}});node.append(el);
    }return node;
  }
  function fit(){
    document.querySelectorAll('.text').forEach(el=>{
      const e=deck.slides.flatMap(s=>s.elements).find(e=>e.id===el.dataset.id);let px=e.size*96/72;
      el.style.fontSize=px+'px';let guard=0;
      while((el.scrollHeight>el.clientHeight+1||el.scrollWidth>el.clientWidth+1)&&guard++<80){px-=.25;el.style.fontSize=px+'px';}
      el.dataset.fitted=String(px*72/96);
    });
  }
  function scale(){
    if(exporting)return;
    const viewport=$('#viewport'), width=viewport.clientWidth;
    if(width<=0)return; // A hidden preview iframe will become measurable later.
    const factor=width/1280;
    $('#slides').style.transform=`scale(${factor})`;
    viewport.style.height=720*factor+'px';
  }
  function show(index){current=(index+deck.slides.length)%deck.slides.length;selected=null;document.querySelectorAll('.slide').forEach((s,i)=>s.classList.toggle('active',i===current));document.querySelectorAll('#rail button').forEach((b,i)=>b.classList.toggle('active',i===current));$('#counter').textContent=`${current+1} / ${deck.slides.length}`;$('#notes p').textContent=deck.slides[current].notes||'Нет заметок';if(!exporting){try{history.replaceState(null,'',`#${current+1}`);}catch{/* sandboxed frames can disallow history; rendering must still work */}}fit();scale();}
  function select(id){selected=id;document.querySelectorAll('.element').forEach(el=>el.classList.toggle('selected',el.dataset.id===id));const e=deck.slides[current].elements.find(e=>e.id===id),form=$('#inspector form');form.hidden=false;for(const field of ['text','size','x','y','w','h','bold','color']){const input=form.elements[field];input.closest('label').hidden=e.type==='rect'&&['text','size','bold'].includes(field);if(field==='bold')input.checked=!!e.bold;else input.value=field==='color'?(e.color||e.fill):(e[field]??'');}}
  function render(){const slides=$('#slides');slides.replaceChildren(...deck.slides.map(renderSlide));$('#rail').replaceChildren(...deck.slides.map((s,i)=>{const b=document.createElement('button'),small=document.createElement('small');small.textContent=`СЛАЙД ${i+1}`;b.append(small,document.createTextNode(s.title));b.onclick=()=>show(i);return b;}));show(current);}
  function download(){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(deck,null,2)+'\n'],{type:'application/json'}));a.download='deck.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}
  async function save(){if(!window.EDITABLE){download();return;}try{const response=await fetch('/api/deck',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(deck)});const result=await response.json();if(!response.ok)throw Error(result.error);dirty=false;$('#status').textContent='Сохранено в deck.json';return true;}catch(e){$('#status').textContent='Не сохранено: '+e.message;return false;}}
  $('#edit').onclick=()=>{editing=!editing;document.body.classList.toggle('editing',editing);$('#inspector').hidden=!editing;$('#edit').textContent=editing?'Готово':'Править';scale();};
  $('#inspector form').onsubmit=e=>e.preventDefault();
  $('#inspector form').oninput=event=>{
    if(!selected)return;const e=deck.slides[current].elements.find(e=>e.id===selected),field=event.target.name;let value=field==='bold'?event.target.checked:['size','x','y','w','h'].includes(field)?Number(event.target.value):event.target.value;
    if(field==='color'&&!(/^\$\w+$/.test(value)&&deck.theme[value.slice(1)])&&!/^[0-9a-f]{6}$/i.test(value))return;
    if(typeof value==='number'&&(!Number.isFinite(value)||(['w','h','size'].includes(field)&&value<=0)))return;
    e[field==='color'&&e.type==='rect'?'fill':field]=value;
    const node=document.getElementById(deck.slides[current].id);node.replaceWith(renderSlide(deck.slides[current]));document.getElementById(deck.slides[current].id).classList.add('active');document.querySelector(`[data-id="${selected}"]`).classList.add('selected');fit();dirty=true;$('#status').textContent='Есть несохранённые изменения';
  };
  $('#save').onclick=save;
  if($('#build'))$('#build').onclick=async()=>{if(!await save())return;const button=$('#build');button.disabled=true;$('#status').textContent='Сборка PDF / PPTX / PNG…';try{const response=await fetch('/api/build',{method:'POST'});const result=await response.json();if(!response.ok)throw Error(result.error);$('#status').textContent='Экспорт обновлён. Ссылки под слайдом.';}catch(e){$('#status').textContent='Ошибка сборки: '+e.message;}finally{button.disabled=false;}};$('#download').onclick=download;$('#prev').onclick=()=>show(current-1);$('#next').onclick=()=>show(current+1);
  $('#fullscreen').onclick=()=>document.fullscreenElement?document.exitFullscreen():$('#viewport').requestFullscreen();
  document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();save();}if(['INPUT','TEXTAREA'].includes(e.target.tagName))return;if(e.key==='ArrowRight'||e.key==='PageDown')show(current+1);if(e.key==='ArrowLeft'||e.key==='PageUp')show(current-1);});
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});window.addEventListener('resize',scale);
  window.addEventListener('pageshow',scale);
  window.addEventListener('hashchange',()=>show(slideFromHash()));
  document.addEventListener('fullscreenchange',scale);
  document.addEventListener('visibilitychange',scale);
  if(typeof ResizeObserver!=='undefined')new ResizeObserver(scale).observe($('#viewport'));
  window.deckReady=(async()=>{
    try{
      render();
      await Promise.all([document.fonts.load('400 16px Deck'),document.fonts.load('700 16px Deck')]);
      await document.fonts.ready;fit();scale();return true;
    }catch(error){
      console.error('Presentation startup:',error);
      $('#status').textContent='Ошибка запуска: '+error.message;
      if(!document.querySelector('.slide.active'))document.querySelector('.slide')?.classList.add('active');
      scale();return false;
    }
  })();
})();
