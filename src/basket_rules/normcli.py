DESCRIPTION='Normalize item aliases onto canonical SKUs for one as-of date, emitting a basket-rules source snapshot'
import argparse, json, sys
from pathlib import Path
from .normalize import normalize

def main(argv=None):
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source',type=Path,required=True,help='existing basket snapshot JSON consumed by basket-rules')
    parser.add_argument('--canonical',type=Path,required=True,help='JSON array of canonical SKU identifiers')
    parser.add_argument('--aliases',type=Path,required=True,help='JSONL records {alias,target,valid_from,valid_to} (left-closed, right-open)')
    parser.add_argument('--as-of',required=True,metavar='YYYY-MM-DD',help='day whose effective alias graph is applied')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        receipt=normalize(args.source,args.canonical,args.aliases,args.as_of,args.output)
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(receipt,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

if __name__=='__main__':raise SystemExit(main())
