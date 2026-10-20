"""Rebuild a read-only source snapshot from the three supplied root exports."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime/src'))
from clinic.source_data import import_sources
from check_space import require_space

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-root',type=Path,default=ROOT.parent)
    parser.add_argument('--output',type=Path,default=ROOT/'imports/clinic-source.sqlite')
    args=parser.parse_args();require_space(args.output.parent,256*1024*1024)
    print(json.dumps(import_sources(args.source_root,args.output),ensure_ascii=False,indent=2))
    require_space()
