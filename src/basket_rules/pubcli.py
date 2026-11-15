DESCRIPTION='Public noisy statistics publication on an independent SQLite ledger with quota and byte-exact archives'
import argparse, json, sqlite3, sys
from pathlib import Path
from .publishing import init_ledger, publish_report

def main(argv=None):
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init',help='create an independent ledger pinned to one rolling source, public universe and quota')
    init.add_argument('--ledger',type=Path,required=True)
    init.add_argument('--source',type=Path,required=True,help='existing basket-rules-roll SQLite database (never modified)')
    init.add_argument('--universe',type=Path,required=True,help='JSON array of <=12 public item identifiers')
    init.add_argument('--quota',type=int,required=True,help='positive integer publication quota for new request ids')
    pub=commands.add_parser('publish',help='publish a noisy histogram for a new id, or replay archived bytes for an old id')
    pub.add_argument('--ledger',type=Path,required=True)
    pub.add_argument('--request-id',required=True)
    pub.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='init':
            result=init_ledger(args.ledger,source=args.source,universe_file=args.universe,quota=args.quota)
        else:
            result=publish_report(args.ledger,args.request_id,args.output)
        print(json.dumps(result,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError,sqlite3.Error) as error:
        print(str(error),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
