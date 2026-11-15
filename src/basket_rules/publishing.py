"""Public statistics publishing: bounded noisy histograms in an independent SQLite ledger.

This is the third delivery chain. It never writes the source rolling database:
the source is read once inside a single read-only SQLite transaction so the
parameters and the active baskets come from one consistent snapshot. A separate
ledger pins the source database, the public item universe (<=12 items) and a
positive integer publication quota.

Noise: each histogram cell independently gets Z = G1 - G2 where G1, G2 are
independent geometric P(G=k)=2^(-k-1), k>=0, drawn from the operating system
secure random source; negative post-noise counts are clipped to zero. New
request ids are only charged after the complete report bytes are stored in the
ledger transaction; old ids replay the archived bytes byte-for-byte, never read
the source, never redraw noise and never spend quota again.

See docs/publishing.md for the contract and the crash/recovery boundary.
"""
from collections import Counter
from fractions import Fraction
from itertools import combinations
import json, os, secrets, sqlite3, time
from pathlib import Path
from .common import canonical_bytes, ident, number, rows, save_bytes, strict_loads
from .mining import summarize
from .rolling import load_config, MAX_ITEMS_PER_BASKET

SCHEMA_VERSION='1'
MAX_REQUEST_ID=128
RECEIPT_SCOPE=('Noisy public histogram over active baskets; symmetric two-sided geometric noise '
               'P(G=k)=2^(-k-1), negatives clipped to zero; basket identities and true counts are '
               'never stored or emitted; association is not causation.')

SCHEMA=[
 'CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
 # One row per request id; the archived blob is the exact report bytes.
 'CREATE TABLE publications(request_id TEXT PRIMARY KEY,created_at INTEGER NOT NULL,report_bytes BLOB NOT NULL)',
]

def connect(path):
    db=sqlite3.connect(str(path))
    db.isolation_level=None
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA busy_timeout=30000')
    return db

def parse_universe(source):
    with Path(source).open('rb') as stream:
        raw=stream.read(8_000_001)
    if len(raw)>8_000_000:raise ValueError('universe file exceeds 8 MB')
    data=strict_loads(raw)
    if not isinstance(data,list):raise ValueError('universe must be a JSON array of item identifiers')
    universe=[];seen=set()
    for item in rows(data,MAX_ITEMS_PER_BASKET):
        name=ident(item)
        if name in seen:raise ValueError('duplicate item in public universe: '+name)
        seen.add(name);universe.append(name)
    if not universe:raise ValueError('public item universe must be non-empty')
    if len(universe)>MAX_ITEMS_PER_BASKET:raise ValueError('public item universe exceeds 12 items')
    return sorted(universe)

def _source_config(db):
    try:
        schema=db.execute("SELECT value FROM config WHERE key='schema_version'").fetchone()
        if schema is None:raise ValueError('not a rolling source database')
        return load_config(db)
    except sqlite3.Error:raise ValueError('not a rolling source database') from None

def init_ledger(path,*,source,universe_file,quota):
    dest=Path(path);src=Path(source)
    if dest.exists() or dest.is_symlink():raise ValueError('ledger already exists: '+str(dest))
    if not src.is_file():raise ValueError('source rolling database not found: '+str(src))
    q=number(quota,1,2**62)
    universe=parse_universe(universe_file)
    # Pin the source parameters at init; later publications must match this snapshot.
    sdb=sqlite3.connect(src.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        sdb.execute('BEGIN')
        params=_source_config(sdb)
        sdb.execute('COMMIT')
    except:
        try:sdb.execute('ROLLBACK')
        except sqlite3.Error:pass
        raise
    finally:
        sdb.close()
    if dest.resolve()==src.resolve():raise ValueError('ledger and source database must differ')
    dest.parent.mkdir(parents=True,exist_ok=True)
    db=connect(dest)
    try:
        db.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA:db.execute(statement)
        config={'schema_version':SCHEMA_VERSION,
                'source_path':str(src.resolve()),
                'item_universe':json.dumps(universe,ensure_ascii=False,separators=(',',':')),
                'quota':str(q),
                'source_window_seconds':str(params['window_seconds']),
                'source_min_support_count':str(params['min_support_count']),
                'source_max_itemset_size':str(params['max_itemset_size']),
                'source_conf_numerator':str(params['min_confidence_numerator']),
                'source_conf_denominator':str(params['min_confidence_denominator'])}
        db.executemany('INSERT INTO config(key,value) VALUES(?,?)',sorted(config.items()))
        db.execute('COMMIT')
    except (sqlite3.Error,OSError,ValueError):
        try:db.execute('ROLLBACK')
        except sqlite3.Error:pass
        db.close()
        for suffix in ('','-wal','-shm'):Path(str(dest)+suffix).unlink(missing_ok=True)
        raise
    db.close()
    return {'source_path':str(src.resolve()),'item_universe':universe,'quota':q,'parameters':params}

def _load_ledger_config(db):
    try:config=dict(db.execute('SELECT key,value FROM config'))
    except sqlite3.Error:raise ValueError('not an initialized publication ledger') from None
    if config.get('schema_version')!=SCHEMA_VERSION:raise ValueError('unsupported publication schema version')
    try:
        return {'source_path':config['source_path'],
                'item_universe':json.loads(config['item_universe']),
                'quota':int(config['quota']),
                'window_seconds':int(config['source_window_seconds']),
                'min_support_count':int(config['source_min_support_count']),
                'max_itemset_size':int(config['source_max_itemset_size']),
                'min_confidence_numerator':int(config['source_conf_numerator']),
                'min_confidence_denominator':int(config['source_conf_denominator'])}
    except (KeyError,ValueError):raise ValueError('corrupt publication ledger config') from None

class SystemSecureBits:
    """Fair random bits from the operating system secure source (secrets -> os.urandom)."""
    def bit(self):
        return secrets.randbits(1)

def geometric_draw(bits):
    """G with P(G=k)=2^(-k-1), k>=0: number of consecutive fair 1-bits before the first 0."""
    k=0
    while bits.bit():k+=1
    return k

def symmetric_geometric(bits):
    """Z = G1 - G2; independent draws per cell. P(Z=0)=1/3, P(Z=k)=2^(-|k|)/3 for k!=0, E[Z]=0."""
    return geometric_draw(bits)-geometric_draw(bits)

def _read_source_snapshot(src,config):
    """One read-only transaction: parameters and active baskets from a single snapshot."""
    db=sqlite3.connect(Path(src).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('BEGIN')
        try:
            params=_source_config(db)
            if (params['window_seconds'],params['min_support_count'],params['max_itemset_size'],
                    params['min_confidence_numerator'],params['min_confidence_denominator'])!=(
                    config['window_seconds'],config['min_support_count'],config['max_itemset_size'],
                    config['min_confidence_numerator'],config['min_confidence_denominator']):
                raise ValueError('source parameters no longer match the pinned ledger parameters')
            watermark,=db.execute('SELECT watermark FROM meta WHERE id=1').fetchone()
            active=[json.loads(items) for items, in db.execute(
                'SELECT items FROM baskets WHERE active=1 AND items IS NOT NULL ORDER BY id')]
            db.execute('COMMIT')
        except:
            db.execute('ROLLBACK');raise
    finally:
        db.close()
    return watermark,active

def _noisy_histogram(baskets,bits):
    """True histogram over every basket pattern (including the empty basket), then per-cell noise."""
    true_counts=Counter()
    for items in baskets:
        true_counts[tuple(sorted(items))]+=1
    cells=[]
    for pattern in sorted(true_counts):
        true=true_counts[pattern]
        z=symmetric_geometric(bits)
        cells.append({'items':list(pattern),'noisy_count':max(0,true+z)})
    return cells

def _derive(histogram,config):
    """Basket count, <=3-item combinations and exact rules derived solely from noisy cells."""
    counts=Counter();basket_count=0
    for cell in histogram:
        noisy=cell['noisy_count'];basket_count+=noisy
        pattern=tuple(cell['items'])
        for size in range(1,min(config['max_itemset_size'],len(pattern))+1):
            for combo in combinations(pattern,size):counts[combo]+=noisy
    if basket_count:
        frequent,rules=summarize(counts,basket_count,config['min_support_count'],
                                 config['max_itemset_size'],
                                 Fraction(config['min_confidence_numerator'],
                                          config['min_confidence_denominator']))
    else:
        frequent,rules=[],[]
    return basket_count,frequent,rules

def _check_output(output,ledger_path,source_path):
    out=Path(output)
    if out.is_symlink():raise ValueError('output cannot be a symlink')
    resolved=out.resolve()
    if resolved==ledger_path:raise ValueError('output must not overwrite the ledger')
    if resolved==source_path:raise ValueError('output must not overwrite the source database')

def publish_report(ledger_path,request_id,output,bits=None):
    rid=ident(request_id)
    if len(rid)>MAX_REQUEST_ID:raise ValueError('request id exceeds 128 characters')
    ledger=Path(ledger_path)
    if not ledger.is_file():raise ValueError('ledger not found: '+str(ledger))
    db=connect(ledger)
    try:
        config=_load_ledger_config(db)
        src=Path(config['source_path'])
        _check_output(output,ledger.resolve(),src)
        # BEGIN IMMEDIATE serializes concurrent publishers on this ledger: same-id and
        # different-id races alike resolve inside one write transaction.
        db.execute('BEGIN IMMEDIATE')
        try:
            archived=db.execute('SELECT report_bytes FROM publications WHERE request_id=?',(rid,)).fetchone()
            if archived is not None:
                # Old id: replay exact bytes. No source read, no noise redraw, no quota spend.
                raw=archived[0];status='replayed'
            else:
                used,=db.execute('SELECT COUNT(*) FROM publications').fetchone()
                if used>=config['quota']:
                    raise ValueError('publication quota exhausted: %d of %d spent'%(used,config['quota']))
                if not src.is_file():raise ValueError('source rolling database not found: '+str(src))
                watermark,baskets=_read_source_snapshot(src,config)
                allowed=set(config['item_universe'])
                for items in baskets:
                    outside=[item for item in items if item not in allowed]
                    if outside:raise ValueError('active basket contains item outside the public universe: '+outside[0])
                if bits is None:bits=SystemSecureBits()
                histogram=_noisy_histogram(baskets,bits)
                basket_count,frequent,rules=_derive(histogram,config)
                report={'schema_version':1,'chain':'basket-rules-publish','request_id':rid,
                        'source_watermark':watermark,
                        'parameters':{'window_seconds':config['window_seconds'],
                                      'min_support_count':config['min_support_count'],
                                      'max_itemset_size':config['max_itemset_size'],
                                      'min_confidence':{'numerator':config['min_confidence_numerator'],
                                                        'denominator':config['min_confidence_denominator']}},
                        'item_universe':config['item_universe'],
                        'noise':{'distribution':'symmetric two-sided geometric','z':'G1-G2',
                                 'geometric_pmf':'P(G=k)=2^(-k-1), k>=0','per_cell_independent':True,
                                 'secure_random':True,'negative_clip':0},
                        'noisy_histogram':histogram,
                        'derived':{'basket_count':basket_count,
                                   'frequent_itemsets':frequent,'rules':rules},
                        'scope':RECEIPT_SCOPE}
                raw=canonical_bytes(report)
                # Full bytes inside the transaction before the single quota deduction;
                # a crash before COMMIT discards both, so noise is never reused nor double-charged.
                db.execute('INSERT INTO publications(request_id,created_at,report_bytes) VALUES(?,?,?)',
                           (rid,time.time_ns(),raw))
                status='published'
            db.execute('COMMIT')
        except:
            db.execute('ROLLBACK');raise
    finally:
        db.close()
    # Test seam (unset by default): widen the post-commit/pre-output window so a
    # SIGKILL can land after the archive transaction but before file publication.
    delay=os.environ.get('BASKET_RULES_PUBLISH_DELAY_SECONDS')
    if delay:
        time.sleep(float(delay))
    # Outside the transaction: a failure here leaves the committed archive untouched and
    # the caller retries with the same request id (byte-for-byte replay, no extra charge).
    save_bytes(output,raw)
    used_after=0
    check=sqlite3.connect(str(ledger))
    try:used_after=check.execute('SELECT COUNT(*) FROM publications').fetchone()[0]
    finally:check.close()
    return {'request_id':rid,'status':status,'output':str(Path(output)),
            'bytes':len(raw),'quota_used':used_after,'quota_total':config['quota'],
            'quota_remaining':config['quota']-used_after}
