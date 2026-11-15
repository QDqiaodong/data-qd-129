"""Rolling-window basket statistics: revisioned JSONL batches committed into SQLite.

One transaction per batch covers basket revisions, the watermark, basket window
state and combination counts, so a failed batch or a pre-commit abort leaves no
partial state. See docs/rolling.md for the contract, index strategy and the
recovery boundary.
"""
from collections import Counter
from fractions import Fraction
from itertools import combinations
import json, sqlite3
from pathlib import Path
from .common import ident, number, require, rows, strict_loads
from .mining import summarize

SCHEMA_VERSION='1'
MAX_ACTIVE_BASKETS=100000
MAX_ITEMS_PER_BASKET=12
MAX_BATCH_ROWS=1000000
MAX_BATCH_BYTES=64000000
MAX_LINE_BYTES=1000000
MAX_TIME=2**62
FLUSH_EVERY=65536
SCOPE='Rolling window over latest non-deleted basket revisions; exact bounded co-occurrence; association is not causation.'

SCHEMA=[
 'CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
 'CREATE TABLE meta(id INTEGER PRIMARY KEY CHECK(id=1),watermark INTEGER NOT NULL,active_count INTEGER NOT NULL)',
 # Latest revision per basket id; rows are never deleted. items is a JSON array
 # of sorted unique items, NULL for a deletion tombstone. active=1 iff the latest
 # revision is non-deleted and inside the committed watermark window.
 'CREATE TABLE baskets(id TEXT PRIMARY KEY,rev INTEGER NOT NULL,time INTEGER NOT NULL,items TEXT,active INTEGER NOT NULL)',
 # Partial index: watermark expiry range-scans only currently active rows.
 'CREATE INDEX idx_baskets_expiry ON baskets(time) WHERE active=1',
 # Persisted combination counts, sizes 1..max_itemset_size; zero rows are removed.
 'CREATE TABLE counts(combo TEXT PRIMARY KEY,size INTEGER NOT NULL,count INTEGER NOT NULL)',
]

def connect(path):
    db=sqlite3.connect(str(path))
    db.isolation_level=None
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA busy_timeout=5000')
    return db

def init_db(path,*,window_seconds,min_support_count,max_itemset_size,conf_numerator,conf_denominator):
    dest=Path(path)
    if dest.exists() or dest.is_symlink():raise ValueError('database already exists: '+str(dest))
    window=number(window_seconds,1,MAX_TIME);minimum=number(min_support_count,1,MAX_TIME)
    maximum=number(max_itemset_size,1,3)
    numerator=number(conf_numerator,0,MAX_TIME);denominator=number(conf_denominator,1,MAX_TIME)
    if numerator>denominator:raise ValueError('confidence must be <=1')
    dest.parent.mkdir(parents=True,exist_ok=True)
    db=connect(dest)
    try:
        db.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA:db.execute(statement)
        config={'schema_version':SCHEMA_VERSION,'window_seconds':str(window),'min_support_count':str(minimum),
                'max_itemset_size':str(maximum),'min_confidence_numerator':str(numerator),
                'min_confidence_denominator':str(denominator)}
        db.executemany('INSERT INTO config(key,value) VALUES(?,?)',sorted(config.items()))
        db.execute('INSERT INTO meta(id,watermark,active_count) VALUES(1,0,0)')
        db.execute('COMMIT')
    except (sqlite3.Error,OSError):
        try:db.execute('ROLLBACK')
        except sqlite3.Error:pass
        db.close()
        for suffix in ('','-wal','-shm'):Path(str(dest)+suffix).unlink(missing_ok=True)
        raise
    db.close()
    return config

def load_config(db):
    try:config=dict(db.execute('SELECT key,value FROM config'))
    except sqlite3.Error:raise ValueError('not an initialized rolling database') from None
    if config.get('schema_version')!=SCHEMA_VERSION:raise ValueError('unsupported rolling schema version')
    try:
        return {'window_seconds':int(config['window_seconds']),'min_support_count':int(config['min_support_count']),
                'max_itemset_size':int(config['max_itemset_size']),
                'min_confidence_numerator':int(config['min_confidence_numerator']),
                'min_confidence_denominator':int(config['min_confidence_denominator'])}
    except (KeyError,ValueError):raise ValueError('corrupt rolling database config') from None

def parse_row(line):
    data=strict_loads(line)
    require(data,'id rev time items')
    bid=ident(data['id']);rev=number(data['rev'],1,MAX_TIME);ts=number(data['time'],0,MAX_TIME)
    raw=data['items']
    if raw is None:return bid,rev,ts,None
    items=[ident(i) for i in rows(raw,MAX_ITEMS_PER_BASKET)]
    if len(items)!=len(set(items)):raise ValueError('repeated item within basket')
    return bid,rev,ts,sorted(set(items))

def parse_batch(source):
    with Path(source).open('rb') as stream:raw=stream.read(MAX_BATCH_BYTES+1)
    if len(raw)>MAX_BATCH_BYTES:raise ValueError('batch file exceeds 64 MB')
    lines=raw.splitlines()
    if len(lines)>MAX_BATCH_ROWS:raise ValueError('batch exceeds 1000000 rows')
    parsed=[]
    for lineno,line in enumerate(lines,1):
        if len(line)>MAX_LINE_BYTES:raise ValueError('line %d: exceeds 1 MB'%lineno)
        if not line.strip():raise ValueError('line %d: blank line'%lineno)
        try:parsed.append(parse_row(line))
        except ValueError as error:raise ValueError('line %d: %s'%(lineno,error)) from None
    return parsed

def _delta(deltas,items,sign,max_size):
    for size in range(1,min(max_size,len(items))+1):
        for combo in combinations(items,size):deltas[combo]+=sign

def _flush(db,deltas):
    pending=[(combo,delta) for combo,delta in deltas.items() if delta]
    if not pending:deltas.clear();return
    keys=[json.dumps(list(combo),ensure_ascii=False,separators=(',',':')) for combo,_ in pending]
    db.executemany('INSERT INTO counts(combo,size,count) VALUES(?,?,?) '
                   'ON CONFLICT(combo) DO UPDATE SET count=count+excluded.count',
                   [(key,len(combo),delta) for key,(combo,delta) in zip(keys,pending)])
    db.executemany('DELETE FROM counts WHERE combo=? AND count<=0',[(key,) for key in keys])
    deltas.clear()

def load_batch(path,source,watermark):
    if not Path(path).exists():raise ValueError('database not found: '+str(path))
    w=number(watermark,0,MAX_TIME)
    parsed=parse_batch(source)
    db=connect(path)
    try:
        config=load_config(db)
        window,max_size=config['window_seconds'],config['max_itemset_size']
        cur_w,active=db.execute('SELECT watermark,active_count FROM meta WHERE id=1').fetchone()
        if w<cur_w:raise ValueError('watermark regression: %d < committed %d'%(w,cur_w))
        stats={'applied':0,'idempotent':0,'ignored':0,'expired':0}
        deltas=Counter()
        db.execute('BEGIN IMMEDIATE')
        try:
            for bid,rev,ts,items in parsed:
                if ts>w:raise ValueError('future record: id %r time %d exceeds watermark %d'%(bid,ts,w))
                current=db.execute('SELECT rev,time,items,active FROM baskets WHERE id=?',(bid,)).fetchone()
                if current:
                    crev,ctime,citems_json,cactive=current
                    citems=None if citems_json is None else json.loads(citems_json)
                    if rev<crev:stats['ignored']+=1;continue
                    if rev==crev:
                        if ctime==ts and citems==items:stats['idempotent']+=1;continue
                        raise ValueError('conflicting revision %d for id %r'%(rev,bid))
                    if cactive:_delta(deltas,citems,-1,max_size);active-=1
                new_active=items is not None and ts>w-window
                db.execute('INSERT OR REPLACE INTO baskets(id,rev,time,items,active) VALUES(?,?,?,?,?)',
                           (bid,rev,ts,None if items is None else json.dumps(items,ensure_ascii=False,separators=(',',':')),
                            1 if new_active else 0))
                if new_active:
                    _delta(deltas,items,1,max_size);active+=1
                    if active>MAX_ACTIVE_BASKETS:raise ValueError('active basket cap %d exceeded'%MAX_ACTIVE_BASKETS)
                stats['applied']+=1
                if len(deltas)>=FLUSH_EVERY:_flush(db,deltas)
            if w>cur_w:
                expiring=db.execute('SELECT id,items FROM baskets WHERE active=1 AND time<=?',(w-window,)).fetchall()
                for bid,items_json in expiring:
                    _delta(deltas,json.loads(items_json),-1,max_size)
                    db.execute('UPDATE baskets SET active=0 WHERE id=?',(bid,))
                    active-=1;stats['expired']+=1
                    if len(deltas)>=FLUSH_EVERY:_flush(db,deltas)
            _flush(db,deltas)
            db.execute('UPDATE meta SET watermark=?,active_count=? WHERE id=1',(w,active))
            db.execute('COMMIT')
        except:
            db.execute('ROLLBACK')
            raise
    finally:
        db.close()
    stats['watermark']=w;stats['active_count']=active
    return stats

def build_report(path):
    if not Path(path).exists():raise ValueError('database not found: '+str(path))
    db=connect(path)
    try:
        config=load_config(db)
        w,active=db.execute('SELECT watermark,active_count FROM meta WHERE id=1').fetchone()
        counts=Counter();universe=[]
        for combo_json,size,count in db.execute('SELECT combo,size,count FROM counts'):
            combo=tuple(json.loads(combo_json));counts[combo]=count
            if size==1:universe.append(combo[0])
        if active:
            frequent,rules=summarize(counts,active,config['min_support_count'],config['max_itemset_size'],
                                     Fraction(config['min_confidence_numerator'],config['min_confidence_denominator']))
        else:frequent,rules=[],[]
        return {'schema_version':1,'chain':'basket-rules-rolling','watermark':w,
                'parameters':{'window_seconds':config['window_seconds'],'min_support_count':config['min_support_count'],
                              'max_itemset_size':config['max_itemset_size'],
                              'min_confidence':{'numerator':config['min_confidence_numerator'],
                                                'denominator':config['min_confidence_denominator']}},
                'result':{'basket_count':active,'item_universe':sorted(universe),
                          'frequent_itemsets':frequent,'rules':rules,'scope':SCOPE}}
    finally:
        db.close()
