"""Import existing scenario passports and acceptance dialogues, without generating prose."""
import argparse,hashlib,json,re,zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
PASSPORT_KEYS={'Цель','Тип','Триггеры','Параметры','Операции МИС','Правила','Результат','Когда звонить','Кому не звонить','Попытки','Идентификация','Маршрут','Приоритет'}

def extract(path):
    with zipfile.ZipFile(path) as archive:body=ET.fromstring(archive.read('word/document.xml')).find('w:body',NS)
    return [(i,''.join(t.text or '' for t in p.findall('.//w:t',NS)).strip()) for i,p in enumerate(body.findall('.//w:p',NS),1)]

def import_catalog(path):
    paragraphs=extract(path)
    starts=[(i,n,text) for i,(n,text) in enumerate(paragraphs) if re.match(r'^С-\d{2}\. ',text) and n>600]
    scenarios=[]
    for k,(index,n,title) in enumerate(starts):
        block=paragraphs[index+1:starts[k+1][0] if k+1<len(starts) else len(paragraphs)]
        end=next((i for i,(_,t) in enumerate(block) if t.startswith('Основной диалог')),len(block))
        block=block[:end];passport={}
        for i,(line,t) in enumerate(block):
            if t in PASSPORT_KEYS and i+1<len(block):
                values=[]
                for num,value in block[i+1:]:
                    if value in PASSPORT_KEYS:break
                    if value:values.append({'paragraph':num,'text':value})
                passport[t]=values
        code=title.split('.')[0];phase=2 if code in {'С-06','С-08','С-09','С-11','С-13'} else 1
        scenarios.append({'id':code,'title':title.split('. ',1)[1],'paragraph':n,'phase':phase,'passport':passport})
    tests=[]
    # DOCX appendix D table: index, caller utterance/situation, expected behavior.
    tail=[(n,t) for n,t in paragraphs if n>=1975 and t]
    for i,(n,t) in enumerate(tail):
        if re.fullmatch(r'\d+\s*!?',t) and i+2<len(tail):
            tests.append({'id':int(t.rstrip(' !')),'blocking':'!' in t,'paragraph':n,'input':tail[i+1][1],'expected':tail[i+2][1]})
            if len(tests)==16:break
    if len(scenarios)!=15 or len(tests)!=16:raise ValueError('Unexpected document structure; review the source manually')
    return {'source':path.name,'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'numbering':'word/document.xml body//w:p, including empty paragraphs and tables','scenarios':scenarios,'control_dialogues':tests}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,default=ROOT.parent/'28e5dadf-0f5f-4a36-9bca-d6e36e808b2e.docx');parser.add_argument('--output',type=Path,default=ROOT/'runtime/src/clinic/scenario_catalog.json');args=parser.parse_args()
    result=import_catalog(args.source);args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(f"Imported {len(result['scenarios'])} source passports and {len(result['control_dialogues'])} control dialogues")
if __name__=='__main__':main()
