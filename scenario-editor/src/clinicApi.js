// The workbench depends on this transport, never on a filesystem or provider URL.
export const apiUrl=route=>(window.CLINIC_CONFIG?.apiBase||'/api').replace(/\/$/,'')+'/'+route;
export async function clinicApi(route,body,{timeout=30000}={}){
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeout);
 try{
  const response=await fetch(apiUrl(route),{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:controller.signal});
  const data=await response.json();if(!response.ok)throw Error(data.error||'Не удалось выполнить действие');return data;
 }catch(error){if(error.name==='AbortError')throw Error('Сервис не ответил за отведённое время.');throw error}finally{clearTimeout(timer)}
}
export const workbench={
 connections:()=>clinicApi('workshop/connections'),
 sql:(sql,params,confirmWrite)=>clinicApi('workshop/sql',{sql,params,confirm_write:confirmWrite}),
 request:(method,path,query,body,confirmWrite)=>clinicApi('workshop/request',{method,path,query,body,confirm_write:confirmWrite}),
};
