"""Disposable real E5/editor/SQLite for the text-browser CI; no TTS/LLM/MIS calls."""
import argparse,json,os,shutil,signal,tempfile,threading
from pathlib import Path

def main():
 p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);args=p.parse_args();root=Path(__file__).resolve().parents[1];args.out.mkdir(parents=True,exist_ok=True)
 os.environ.update(CLINIC_ACCEPTANCE_TEST='1',MODULE_SECRETS_DIR='/no-secrets',STAND_URL='',CLINIC_SOURCE_DB='',EDITOR_BOOTSTRAP_PASSWORD='admin')
 from clinic.modules import Nlu
 from clinic.module_settings import ModuleSettings
 from clinic.scenario_editor import Repository,make_server
 import clinic.demo_call,clinic.workshop
 nlu=Nlu('e5')
 def rpc(module,op,data):
  if module=='nlu':return nlu.handle(op,data)[1]
  if module=='crm' and op=='learning.calls':return {'calls':[]}
  if module=='crm' and op=='learning.cases':return {'cases':[],'badges':{},'labelled_examples':0}
  raise AssertionError('Forbidden test dependency: '+module+'/'+op)
 clinic.demo_call.module_call=rpc;clinic.workshop.module_call=rpc
 stop=threading.Event()
 for signum in (signal.SIGTERM,signal.SIGINT):signal.signal(signum,lambda *_:stop.set())
 with tempfile.TemporaryDirectory(prefix='clinic-text-http-') as folder:
  folder=Path(folder);shutil.copytree(root/'scenarios',folder/'scenarios');settings=ModuleSettings(folder/'data/modules.json');values=settings.read();values['tts']['enabled']=False;values['dialogue']['enabled']=True;settings.save(values)
  os.environ['MODULE_SETTINGS_FILE']=str(settings.path);os.environ['EDITOR_PASSWORD_FILE']=str(folder/'data/password.json')
  server=make_server(Repository(folder,folder/'data'),root/'scenario-editor/dist',host='127.0.0.1',port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  (args.out/'ready.json').write_text(json.dumps({'base':f'http://127.0.0.1:{server.server_port}','scope':'actual E5, editor HTTP, FSM, disposable SQLite; TTS disabled, LLM blocked in CI'}))
  try:stop.wait(180)
  finally:server.shutdown();server.server_close();thread.join(5)
if __name__=='__main__':main()
