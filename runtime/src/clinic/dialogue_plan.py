"""Versioned immutable plans. Optional extensions cannot remove mandatory gates."""
from dataclasses import dataclass
from functools import lru_cache
from .specialty_dictionary import SpecialtyDictionary

@dataclass(frozen=True)
class PlanNode:
    name: str
    next: int | None
    back: int | None

@lru_cache(maxsize=512)
def compile_plan(revision, specialty_id, referral_required):
    names=['identity_name','identity_birth','identity_lookup','specialty']
    if referral_required is None:names+=['policy_review']
    elif referral_required:names+=['referral_lookup']
    names+=['availability_lookup','offer','confirmation','commit','receipt']
    return tuple(PlanNode(name,i+1 if i+1<len(names) else None,i-1 if 0<i<names.index('commit') else None) for i,name in enumerate(names))

def plans(dictionary=None):
    doc=(dictionary or SpecialtyDictionary()).read()
    return {'revision':doc['revision'],'plans':{item['id']:[vars(n) for n in compile_plan(doc['revision'],item['id'],item.get('referral_required'))] for item in doc['specialties']}}
