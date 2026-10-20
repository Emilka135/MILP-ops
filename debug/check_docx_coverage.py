"""A collected source paragraph is not accepted as an executed behavior contract."""
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
STAGES={'assistant_phrase':['fsm','audio'],'patient_utterance':['label','entities','fsm'],
        'mis_instruction':['mis','fsm'],'branch_contract':['fsm'],
        'human_instruction':['fsm'],'registrar_phrase':['fsm']}


def main():
    source=json.loads((ROOT/'data/acceptance/docx_index.json').read_text())
    folder=ROOT/'debug/runs/docx-contracts';folder.mkdir(parents=True,exist_ok=True)
    observed=set()
    for path in folder.glob('*/results.xml'):
        for case in ET.parse(path).iter('testcase'):
            if any(case.find(tag) is not None for tag in ['failure','error','skipped']):continue
            for prop in case.findall('./properties/property'):
                if prop.get('name')=='source_case':observed.add((prop.get('value'),path.parent.name))
    checks=[]
    for example in source['source_examples']:
        for stage in STAGES[example['kind']]:
            code=example['id']+'.'+stage+'.coverage'
            checks.append({'id':code,'source_case':example['id'],'module':stage,
                           'passed':(example['id'],stage) in observed})
    missing=[c for c in checks if not c['passed']]
    report={'passed':not missing,'checks':checks,'uncovered':missing,
            'note':'Presence is not execution. Source IDs must be attached to actual behavior assertions using the source_case JUnit property.',
            'external_requirement_inventory':len(source['external_source_examples']),
            'external_requirement_behavior_coverage_verified':False}
    (folder/'coverage.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    for item in missing:print(item['id']+': UNCOVERED')
    print(f'SOURCE_BEHAVIOR_COVERAGE: {len(checks)-len(missing)}/{len(checks)}')
    if os.environ.get('GITHUB_ACTIONS') and missing:
        print(f'::error title=DOCX_SOURCE_UNCOVERED::{len(missing)} source/module bindings lack successful execution; see docx-contract-coverage artifact')
    return int(bool(missing))


if __name__=='__main__':raise SystemExit(main())
