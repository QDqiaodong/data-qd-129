DESCRIPTION='Offline promotion voucher pre-reservation on an independent SQLite ledger with exact integer optimization'
import argparse, json, sqlite3, sys
from pathlib import Path
from .vouchers import allocate, cancel, init_ledger

def main(argv=None):
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init',help='create an independent voucher ledger with a fixed immutable activity catalogue')
    init.add_argument('--ledger',type=Path,required=True)
    init.add_argument('--activities',type=Path,required=True,
                      help='JSON array of {id,required_items,quota,discount_cents}, at most 30 activities')
    alloc=commands.add_parser('allocate',help='solve and reserve the exact optimal allocation for one static basket snapshot')
    alloc.add_argument('--ledger',type=Path,required=True)
    alloc.add_argument('--request-id',required=True)
    alloc.add_argument('--source',type=Path,required=True,
                       help='full static basket snapshot JSON with the basket-rules contract (1..200 baskets)')
    alloc.add_argument('--output',type=Path,required=True)
    can=commands.add_parser('cancel',help='release the baskets and quota held by one request (idempotent; unknown id fails)')
    can.add_argument('--ledger',type=Path,required=True)
    can.add_argument('--request-id',required=True)
    can.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='init':
            result=init_ledger(args.ledger,args.activities)
        elif args.command=='allocate':
            result=allocate(args.ledger,args.request_id,args.source,args.output)
        else:
            result=cancel(args.ledger,args.request_id,args.output)
        print(json.dumps(result,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError,RuntimeError,
            sqlite3.Error) as error:
        print(str(error),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
