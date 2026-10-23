"""Source references point only into the regulation export already tracked in Git.

The original DOCX remains local. Presence/provenance is separate from execution.
"""
import hashlib
import json
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[2]
INDEX=json.loads((ROOT/'data/acceptance/docx_index.json').read_text())
RAW=(ROOT/INDEX['export']).read_bytes()
TEXT=' '.join(RAW.decode().split())


def source_text(paragraph):
    reference=INDEX['references'][str(paragraph)]
    return TEXT[reference['start']:reference['end']]


def test_source_export_is_the_published_and_reviewed_input():
    assert hashlib.sha256(RAW).hexdigest()==INDEX['export_sha256']
    assert INDEX['source_sha256']=='0d39c2570371e499f177d91241a59009de54dfb431aea23045d21a0eb95364fc'
    assert len(INDEX['scenarios'])==15 and len(INDEX['control_dialogues'])==16
    assert len(INDEX['source_examples'])==205
    assert INDEX['missing_export_paragraphs']==list(range(22,93)) # Generated table of contents, not dialogue examples.


@pytest.mark.parametrize('scenario',INDEX['scenarios'],ids=lambda s:s['id']+'.source.index')
def test_source_every_scenario_has_dialogue_and_branch_contracts(scenario):
    items=[i for i in INDEX['source_examples'] if i['scenario_id']==scenario['id']]
    assert any(i['kind']=='branch_contract' for i in items),scenario
    assert any(i['kind']=='assistant_phrase' for i in items),scenario
    assert source_text(scenario['source_paragraph']).startswith(scenario['source_id'])
    for item in items:
        assert str(item['source_paragraph']) in INDEX['references'],item
        if 'expected_paragraph' in item:assert str(item['expected_paragraph']) in INDEX['references'],item


@pytest.mark.parametrize('dialogue',INDEX['control_dialogues'],ids=lambda d:d['id']+'.source.control')
def test_source_control_rows_match_runtime_catalog(dialogue):
    from clinic.scenario_catalog import catalog
    runtime=catalog()['control_dialogues'][int(dialogue['id'][2:])-1]
    assert source_text(dialogue['source_paragraph'])==' '.join(runtime['input'].split())
    assert source_text(dialogue['expected_paragraph'])==' '.join(runtime['expected'].split())
    assert runtime['input_paragraph']==dialogue['source_paragraph']
    assert runtime['expected_paragraph']==dialogue['expected_paragraph']
    assert runtime['paragraph']==dialogue['catalog_paragraph']


def test_source_all_indexed_content_hashes_match_published_export():
    for paragraph,reference in INDEX['references'].items():
        assert hashlib.sha256(source_text(paragraph).encode()).hexdigest()==reference['sha256'],paragraph
