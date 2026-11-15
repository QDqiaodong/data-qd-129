DESCRIPTION='Rolling-window basket statistics on SQLite with revisioned JSONL batch imports'
import argparse, json, sqlite3, sys
from pathlib import Path
from .common import save
from .rolling import init_db, load_batch, build_report

def main(argv=None):
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init',help='create a database with fixed window and mining parameters')
    init.add_argument('--db',type=Path,required=True)
    init.add_argument('--window-seconds',type=int,required=True)
    init.add_argument('--min-support-count',type=int,required=True)
    init.add_argument('--max-itemset-size',type=int,required=True)
    init.add_argument('--min-confidence',type=int,required=True,nargs=2,metavar=('NUMERATOR','DENOMINATOR'))
    load=commands.add_parser('load',help='commit one JSONL batch of {id,rev,time,items} at a non-decreasing watermark')
    load.add_argument('--db',type=Path,required=True)
    load.add_argument('--watermark',type=int,required=True)
    load.add_argument('--source',type=Path,required=True)
    report=commands.add_parser('report',help='publish the report from persisted counts only')
    report.add_argument('--db',type=Path,required=True)
    report.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='init':
            numerator,denominator=args.min_confidence
            config=init_db(args.db,window_seconds=args.window_seconds,min_support_count=args.min_support_count,
                           max_itemset_size=args.max_itemset_size,conf_numerator=numerator,conf_denominator=denominator)
            print(json.dumps({'initialized':str(args.db),'parameters':config},sort_keys=True));return 0
        if args.command=='load':
            print(json.dumps(load_batch(args.db,args.source,args.watermark),sort_keys=True));return 0
        if args.db.resolve()==args.output.resolve():raise ValueError('database and output must differ')
        result=build_report(args.db);save(args.output,result)
        print(json.dumps(result,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError,sqlite3.Error) as error:
        print(str(error),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
