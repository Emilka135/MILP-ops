"""Read-only source passports. No LLM-created business scenarios."""
import json
from pathlib import Path

def catalog():
    return json.loads(Path(__file__).with_suffix('.json').read_text())
