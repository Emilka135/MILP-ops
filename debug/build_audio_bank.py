"""Prepare real static Silero recordings once, for subsequent reuse from disk."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'runtime/src')]
from clinic.modules import Speech

if __name__=='__main__':
    speech=Speech('tts')
    print('Recorded prompts:',len(speech.bank.manifest),'generated now:',speech.bank.generated)
