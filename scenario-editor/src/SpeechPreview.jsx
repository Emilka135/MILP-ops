import React,{useEffect,useRef,useState} from 'react';

export default function SpeechPreview({text,rate=1.5,api,url,route='workshop/preview'}){
 const [src,setSrc]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');const player=useRef(null);
 useEffect(()=>{if(src)player.current?.play().catch(()=>setError('Нажмите ▶ на плеере, чтобы начать прослушивание'));return()=>{if(src.startsWith('blob:'))URL.revokeObjectURL(src)}},[src]);
 async function play(){setBusy(true);setError('');try{
  if(url){const response=await fetch(url);if(!response.ok){const result=await response.json();throw Error(result.error)}setSrc(URL.createObjectURL(await response.blob()))}
  else{const voice=await api(route,{text,rate});if(!voice.pcm_b64)throw Error('Модуль не вернул аудио');const pcm=Uint8Array.from(atob(voice.pcm_b64),c=>c.charCodeAt(0));const buffer=new ArrayBuffer(44+pcm.length),view=new DataView(buffer);function str(at,s){for(let i=0;i<s.length;i++)view.setUint8(at+i,s.charCodeAt(i))}str(0,'RIFF');view.setUint32(4,36+pcm.length,true);str(8,'WAVEfmt ');view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);view.setUint32(24,voice.sample_rate||8000,true);view.setUint32(28,(voice.sample_rate||8000)*2,true);view.setUint16(32,2,true);view.setUint16(34,16,true);str(36,'data');view.setUint32(40,pcm.length,true);new Uint8Array(buffer,44).set(pcm);setSrc(URL.createObjectURL(new Blob([buffer],{type:'audio/wav'})))}
 }catch(e){setError(e.message)}finally{setBusy(false)}}
 return <div className="speech-preview"><button type="button" className="secondary" disabled={busy||(!text&&!url)} onClick={play}>{busy?'Готовим звук…':'▶ Озвучить сообщение'}</button><audio ref={player} controls hidden={!src} src={src||undefined}/>{error&&<p role="alert">{error}</p>}</div>
}
