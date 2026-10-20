"""Measured regression paraphrases; clarification is reported separately from accuracy."""
import json,time
from pathlib import Path
from clinic.modules import Nlu

def main():
 nlu=Nlu('e5');rows=[]
 for line in Path('data/eval/semantic-dialogue.jsonl').read_text().splitlines():
  case=json.loads(line);began=time.perf_counter();_,payload=nlu.handle('interpret',{'text':case['text']})
  if 'intent' in case:correct=payload['intent']==case['intent']
  elif 'yes_no' in case:correct=payload['yes_no'] is case['yes_no']
  elif 'specialty' in case:correct=payload['vars'].get('specialty')==case['specialty']
  elif 'excluded' in case:correct=case['excluded'] not in (payload['vars'].get('specialty'),payload['vars'].get('specialty_candidate'))
  elif 'human' in case:correct=payload['intent'] in {'medical','emergency','operator','complaint'}
  else:correct=not {'patient_id','slot_id','confirmed'}&payload['vars'].keys()
  rows.append({'case':case,'correct':correct,'latency_ms':(time.perf_counter()-began)*1000,'result':payload})
 result={'cases':len(rows),'strict_correct':sum(r['correct'] for r in rows),'clarification_instead_of_intent':sum(not r['correct'] and r['result']['intent']=='unknown' for r in rows),'model':'multilingual-e5-small-onnx-int8','records':rows}
 out=Path('debug/runs/dialogue-contracts');out.mkdir(parents=True,exist_ok=True);(out/'semantic-eval.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
 print(json.dumps({k:v for k,v in result.items() if k!='records'}))
if __name__=='__main__':main()
