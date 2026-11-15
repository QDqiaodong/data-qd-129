"""Offline promotion voucher pre-reservation on an independent SQLite ledger.

The fifth delivery chain. It is deliberately separate from the rolling store,
the mining reports, the public ledger and the alias graph: it consumes one full
static basket snapshot with the same fields and the same identifier/parameter
validation as ``basket-rules`` (via ``model.parse_snapshot``), fixes an
immutable activity catalogue at ``init`` and records reservations, semantic
input identities, allocations and activity occupancy in its own ledger.

Optimization is an exact integer min-cost flow, never a per-basket greedy and
never floating point. One write transaction per command serializes on
``BEGIN IMMEDIATE``: remaining quota and basket occupancy are read at one
committed state, the optimum is solved inside that transaction, and request
rows, basket occupations and quota deductions commit together. See
docs/vouchers.md for the contract, the tie-break encoding, linearization and
the crash/recovery boundary.
"""
from collections import Counter
from pathlib import Path
import hashlib, heapq, json, os, sqlite3, time
from .common import canonical_bytes, ident, number, read, rows, save_bytes
from .model import MAX_ITEMS, parse_snapshot

SCHEMA_VERSION='1'
MAX_REQUEST_ID=128
MAX_ACTIVITIES=30
MAX_BASKETS=200
CHAIN='basket-rules-vouchers'
SCOPE=('Exact integer voucher pre-reservation over a static basket snapshot; one coupon per basket, '
       'per-activity quota enforced across active requests; association is not causation.')
# Test seam: when set, a new allocation raises inside its transaction immediately
# before COMMIT, so every insert is rolled back. Production runs never set it.
ABORT_ENV='BASKET_RULES_VOUCHERS_ABORT'
# Test seam: widen the post-commit/pre-output window so a SIGKILL can land after
# the ledger commit but before file publication. Production runs never set it.
DELAY_ENV='BASKET_RULES_VOUCHERS_DELAY_SECONDS'

SCHEMA=[
 'CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
 'CREATE TABLE activities(activity_id TEXT PRIMARY KEY, required_items TEXT NOT NULL, '
 'quota INTEGER NOT NULL, discount_cents INTEGER NOT NULL, ord INTEGER NOT NULL)',
 # One row per request id; report_bytes is the immutable, byte-exact allocation report.
 'CREATE TABLE requests(request_id TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN (\'reserved\',\'cancelled\')), '
 'identity_hash TEXT NOT NULL, source_sha256 TEXT NOT NULL, report_bytes BLOB NOT NULL, '
 'created_at INTEGER NOT NULL, cancelled_at INTEGER)',
 # Every basket of every request, including null (no-coupon) baskets.
 'CREATE TABLE allocations(request_id TEXT NOT NULL, basket_id TEXT NOT NULL, '
 'activity_id TEXT, discount_cents INTEGER NOT NULL, ord INTEGER NOT NULL, '
 'PRIMARY KEY(request_id,basket_id))',
 # Only coupon-bearing baskets of non-cancelled requests occupy a basket id.
 'CREATE TABLE occupied_baskets(basket_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, activity_id TEXT NOT NULL)',
 'CREATE TABLE activity_usage(activity_id TEXT PRIMARY KEY, used INTEGER NOT NULL)',
]

def connect(path):
    db=sqlite3.connect(str(path))
    db.isolation_level=None
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
    return db

def parse_activities(data):
    """Validate the fixed activity catalogue: <=30 unique ids, 1..12 unique
    required items, quota 1..200, discount 1..10^9. Returns rows sorted by id."""
    if not isinstance(data,list):raise ValueError('activities must be a JSON array')
    if len(data)>MAX_ACTIVITIES:raise ValueError('at most %d activities'%MAX_ACTIVITIES)
    activities=[];seen=set()
    for obj in rows(data,MAX_ACTIVITIES):
        if not isinstance(obj,dict) or set(obj)!={'id','required_items','quota','discount_cents'}:
            raise ValueError('expected activity object fields: id required_items quota discount_cents')
        aid=ident(obj['id'])
        if aid in seen:raise ValueError('duplicate activity id: '+aid)
        seen.add(aid)
        items=[ident(item) for item in rows(obj['required_items'],MAX_ITEMS)]
        if not 1<=len(items)<=MAX_ITEMS:raise ValueError('activity %r requires 1..12 items'%aid)
        if len(set(items))!=len(items):raise ValueError('activity %r has repeated required items'%aid)
        quota=number(obj['quota'],1,200)
        discount=number(obj['discount_cents'],1,10**9)
        activities.append({'id':aid,'required_items':sorted(set(items)),
                           'quota':quota,'discount_cents':discount})
    activities.sort(key=lambda a:a['id'])
    return activities

def parse_voucher_snapshot(data):
    """Reuse the full snapshot contract (fields, identifiers, parameter bounds,
    unique basket ids, unique items per basket) and add the voucher chain bound
    of 1..200 baskets with at most 12 unique items. Returns the order-insensitive
    canonical structure and baskets sorted by id with sorted items."""
    minimum,maximum,numerator,denominator,baskets=parse_snapshot(data)
    if not 1<=len(baskets)<=MAX_BASKETS:
        raise ValueError('voucher allocation requires 1..%d baskets'%MAX_BASKETS)
    normalized=[];universe=set()
    for bid,items in baskets:
        sitems=sorted(set(items));universe.update(sitems)
        normalized.append((bid,sitems))
    if len(universe)>MAX_ITEMS:raise ValueError('basket universe exceeds %d unique items'%MAX_ITEMS)
    normalized.sort(key=lambda pair:pair[0])
    structure={'min_support_count':minimum,'max_itemset_size':maximum,
               'min_confidence':{'numerator':numerator,'denominator':denominator},
               'baskets':[{'id':bid,'items':items} for bid,items in normalized]}
    return structure,normalized

def canonical_identity(structure):
    """Order-insensitive semantic identity: validated structure with sorted keys,
    sorted baskets and sorted items. Object key order, basket order and in-basket
    item order are ignored; every parameter and value participates."""
    raw=json.dumps(structure,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return raw,hashlib.sha256(raw).hexdigest()

def solve_allocation(baskets,activities,remaining):
    """Exact optimum via integer min-cost flow with an embedded lexicographic key.

    ``baskets`` are (id, sorted items) sorted by id; ``activities`` are sorted
    by id (Python string order); ``remaining`` maps activity id to its residual
    quota in the current committed state. Returns a list aligned with baskets
    where each entry is an activity id or None.

    A basket is eligible for an activity when its items contain every required
    item. Each basket routes exactly one flow unit: to one eligible activity
    (capacity shared via the activity->sink edge capped at residual quota) or to
    a null edge. Edge cost encodes both objectives in one exact integer with
    base M = k+1 (null gets rank k, larger than every activity rank):

        cost(choice for basket i) = -discount * M**n + rank(choice) * M**(n-1-i)

    The positional term is a base-M number strictly below M**n, so maximizing
    total discount always dominates while equal scores resolve to the
    lexicographically smallest rank vector, i.e. smallest activity id per basket
    with None treated as larger than every id. All arithmetic is exact integer.
    """
    n=len(baskets);k=len(activities)
    M=max(2,k+1);base=M**n
    S=0;B0=1;A0=1+n;T=1+n+k;N=T+1
    graph=[[] for _ in range(N)]
    def add_edge(u,v,cap,cost):
        graph[u].append([v,len(graph[v]),cap,cost])
        graph[v].append([u,len(graph[u])-1,0,-cost])
    basket_edges=[]
    for i,(bid,items) in enumerate(baskets):
        add_edge(S,B0+i,1,0)
        itemset=set(items);refs=[];place=M**(n-1-i)
        for j,activity in enumerate(activities):
            rem=remaining.get(activity['id'],0)
            if rem>0 and set(activity['required_items']).issubset(itemset):
                add_edge(B0+i,A0+j,1,-activity['discount_cents']*base+j*place)
                refs.append((activity['id'],graph[B0+i][-1]))
        add_edge(B0+i,T,1,k*place)
        basket_edges.append((refs,graph[B0+i][-1]))
    for j,activity in enumerate(activities):
        add_edge(A0+j,T,max(0,remaining.get(activity['id'],0)),0)
    # Initial potentials by Bellman-Ford because eligibility edges have negative cost.
    inf=float('inf');pot=[inf]*N;pot[S]=0
    for _ in range(N-1):
        changed=False
        for u in range(N):
            if pot[u]==inf:continue
            for v,rev,cap,cost in graph[u]:
                if cap>0 and pot[u]+cost<pot[v]:
                    pot[v]=pot[u]+cost;changed=True
        if not changed:break
    flow=0
    while flow<n:
        dist=[inf]*N;dist[S]=0;prev=[None]*N;heap=[(0,S)]
        while heap:
            d,u=heapq.heappop(heap)
            if d!=dist[u]:continue
            for ei,(v,rev,cap,cost) in enumerate(graph[u]):
                if cap<=0:continue
                nd=d+cost+pot[u]-pot[v]
                if nd<dist[v]:
                    dist[v]=nd;prev[v]=(u,ei);heapq.heappush(heap,(nd,v))
        if dist[T]==inf:raise RuntimeError('voucher flow became infeasible')
        for v in range(N):
            if dist[v]!=inf:pot[v]+=dist[v]
        v=T
        while v!=S:
            u,ei=prev[v];edge=graph[u][ei]
            edge[2]-=1;graph[v][edge[1]][2]+=1;v=u
        flow+=1
    assignment=[None]*n
    for i,(refs,null_edge) in enumerate(basket_edges):
        if null_edge[2]==0:continue
        for aid,edge in refs:
            if edge[2]==0:
                assignment[i]=aid;break
        else:raise RuntimeError('voucher flow left a basket without an outgoing choice')
    return assignment

def init_ledger(path,activities_file):
    dest=Path(path);source=Path(activities_file)
    if not source.is_file():raise ValueError('activities file not found: '+str(source))
    if dest.resolve()==source.resolve():raise ValueError('ledger and activities file must differ')
    if dest.exists() or dest.is_symlink():raise ValueError('ledger already exists: '+str(dest))
    data,digest=read(source)
    activities=parse_activities(data)
    dest.parent.mkdir(parents=True,exist_ok=True)
    db=connect(dest)
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            for statement in SCHEMA:db.execute(statement)
            config={'schema_version':SCHEMA_VERSION,'activities_path':str(source.resolve()),
                    'activities_sha256':digest}
            db.executemany('INSERT INTO config(key,value) VALUES(?,?)',sorted(config.items()))
            db.executemany('INSERT INTO activities(activity_id,required_items,quota,discount_cents,ord) '
                           'VALUES(?,?,?,?,?)',
                           [(a['id'],json.dumps(a['required_items'],ensure_ascii=False,separators=(',',':')),
                             a['quota'],a['discount_cents'],j) for j,a in enumerate(activities)])
            db.execute('COMMIT')
        except:
            db.execute('ROLLBACK');raise
    except (sqlite3.Error,OSError,ValueError):
        db.close()
        for suffix in ('','-wal','-shm'):Path(str(dest)+suffix).unlink(missing_ok=True)
        raise
    db.close()
    return {'ledger':str(dest),'activities_sha256':digest,'activity_count':len(activities),
            'activities':[{'id':a['id'],'required_items':a['required_items'],'quota':a['quota'],
                           'discount_cents':a['discount_cents']} for a in activities]}

def _load(db):
    try:config=dict(db.execute('SELECT key,value FROM config'))
    except sqlite3.Error:raise ValueError('not an initialized voucher ledger') from None
    if config.get('schema_version')!=SCHEMA_VERSION:raise ValueError('unsupported voucher schema version')
    activities=[{'id':aid,'required_items':json.loads(items),'quota':quota,'discount_cents':discount}
                for aid,items,quota,discount,_ in
                db.execute('SELECT activity_id,required_items,quota,discount_cents,ord FROM activities ORDER BY ord')]
    return config,activities

def _check_output(output,ledger_path,*forbidden):
    out=Path(output)
    if out.is_symlink():raise ValueError('output cannot be a symlink')
    resolved=out.resolve()
    if resolved==Path(ledger_path).resolve():raise ValueError('output must not overwrite the ledger')
    for path in forbidden:
        if path is not None and resolved==Path(path).resolve():
            raise ValueError('output must not overwrite an input file')

def _usage_snapshot(db,activities):
    used=dict(db.execute('SELECT activity_id,used FROM activity_usage'))
    return [{'activity_id':a['id'],'used':used.get(a['id'],0),'quota':a['quota']} for a in activities]

def _build_report(rid,source_digest,identity_hash,baskets,assignment,activities):
    discount={a['id']:a['discount_cents'] for a in activities}
    rows_out=[];total=0;counts=Counter()
    for (bid,_),aid in zip(baskets,assignment):
        d=0 if aid is None else discount[aid]
        rows_out.append({'id':bid,'activity_id':aid,'discount_cents':d})
        if aid is not None:total+=d;counts[aid]+=1
    occupancy=[{'activity_id':a['id'],'quota':a['quota'],'discount_cents':a['discount_cents'],
                'reserved':counts.get(a['id'],0)} for a in activities]
    return {'schema_version':1,'chain':CHAIN,'request_id':rid,'source_sha256':source_digest,
            'identity_sha256':identity_hash,'total_discount_cents':total,
            'coupon_baskets':sum(1 for aid in assignment if aid is not None),
            'baskets':rows_out,'activity_occupancy':occupancy,'scope':SCOPE}

def allocate(ledger_path,request_id,source_path,output):
    rid=ident(request_id)
    if len(rid)>MAX_REQUEST_ID:raise ValueError('request id exceeds 128 characters')
    ledger=Path(ledger_path);source=Path(source_path)
    if not ledger.is_file():raise ValueError('ledger not found: '+str(ledger))
    if not source.is_file():raise ValueError('snapshot source not found: '+str(source))
    data,source_digest=read(source)
    structure,baskets=parse_voucher_snapshot(data)
    _,identity_hash=canonical_identity(structure)
    db=connect(ledger)
    try:
        config,activities=_load(db)
        _check_output(output,ledger.resolve(),source.resolve(),Path(config['activities_path']))
        db.execute('BEGIN IMMEDIATE')
        try:
            existing=db.execute('SELECT state,identity_hash,report_bytes FROM requests WHERE request_id=?',
                                (rid,)).fetchone()
            if existing is not None:
                state,stored_hash,report_bytes=existing
                if stored_hash!=identity_hash:
                    raise ValueError('request id %r already exists with a different snapshot identity'%rid)
                # Same id + same identity: replay the archived allocation. Never re-solve,
                # never re-occupy, even when the request was cancelled.
                replayed=True
            else:
                basket_ids=[bid for bid,_ in baskets]
                conflicts=[row[0] for row in db.execute(
                    'SELECT basket_id FROM occupied_baskets WHERE basket_id IN (%s)'
                    %','.join('?'*len(basket_ids)),basket_ids)]
                if conflicts:
                    raise ValueError('basket already reserved by another active request: '+conflicts[0])
                used=dict(db.execute('SELECT activity_id,used FROM activity_usage'))
                remaining={a['id']:a['quota']-used.get(a['id'],0) for a in activities}
                assignment=solve_allocation(baskets,activities,remaining)
                report=_build_report(rid,source_digest,identity_hash,baskets,assignment,activities)
                report_bytes=canonical_bytes(report)
                now=time.time_ns()
                db.execute('INSERT INTO requests(request_id,state,identity_hash,source_sha256,'
                           'report_bytes,created_at) VALUES(?,?,?,?,?,?)',
                           (rid,'reserved',identity_hash,source_digest,report_bytes,now))
                db.executemany('INSERT INTO allocations(request_id,basket_id,activity_id,discount_cents,ord) '
                               'VALUES(?,?,?,?,?)',
                               [(rid,row['id'],row['activity_id'],row['discount_cents'],i)
                                for i,row in enumerate(report['baskets'])])
                occupied=[(row['id'],rid,row['activity_id']) for row in report['baskets']
                          if row['activity_id'] is not None]
                if occupied:
                    db.executemany('INSERT INTO occupied_baskets(basket_id,request_id,activity_id) '
                                   'VALUES(?,?,?)',occupied)
                    counts=Counter(aid for _,_,aid in occupied)
                    db.executemany('INSERT INTO activity_usage(activity_id,used) VALUES(?,?) '
                                   'ON CONFLICT(activity_id) DO UPDATE SET used=used+excluded.used',
                                   list(counts.items()))
                state='reserved';replayed=False
            if os.environ.get(ABORT_ENV):
                raise RuntimeError('forced pre-commit abort via %s'%ABORT_ENV)
            db.execute('COMMIT')
        except:
            db.execute('ROLLBACK');raise
        usage=_usage_snapshot(db,activities)
        occupied_total=db.execute('SELECT COUNT(*) FROM occupied_baskets').fetchone()[0]
    finally:
        db.close()
    # Atomic publication after commit: a failure here keeps the committed record and
    # a same-id retry replays the archived bytes without a second reservation.
    delay=os.environ.get(DELAY_ENV)
    if delay:time.sleep(float(delay))
    save_bytes(output,report_bytes)
    report=json.loads(report_bytes)
    return {'request_id':rid,'status':state,'allocation_in_effect':state=='reserved',
            'replayed':replayed,'output':str(Path(output)),
            'total_discount_cents':report['total_discount_cents'],'coupon_baskets':report['coupon_baskets'],
            'occupied_baskets_total':occupied_total,'activity_usage':usage}

def cancel(ledger_path,request_id,output):
    rid=ident(request_id)
    if len(rid)>MAX_REQUEST_ID:raise ValueError('request id exceeds 128 characters')
    ledger=Path(ledger_path)
    if not ledger.is_file():raise ValueError('ledger not found: '+str(ledger))
    db=connect(ledger)
    try:
        config,activities=_load(db)
        _check_output(output,ledger.resolve(),Path(config['activities_path']))
        db.execute('BEGIN IMMEDIATE')
        try:
            existing=db.execute('SELECT state,report_bytes FROM requests WHERE request_id=?',
                                (rid,)).fetchone()
            if existing is None:raise ValueError('unknown request id: '+rid)
            state,report_bytes=existing;released=False
            if state=='reserved':
                # Release only this request's occupied baskets and quota shares.
                db.execute('DELETE FROM occupied_baskets WHERE request_id=?',(rid,))
                shares=db.execute('SELECT activity_id,COUNT(*) FROM allocations WHERE request_id=? '
                                  'AND activity_id IS NOT NULL GROUP BY activity_id',(rid,)).fetchall()
                db.executemany('UPDATE activity_usage SET used=used-? WHERE activity_id=?',
                               [(count,aid) for aid,count in shares])
                db.execute('UPDATE requests SET state=?,cancelled_at=? WHERE request_id=?',
                           ('cancelled',time.time_ns(),rid))
                released=True
            db.execute('COMMIT')
        except:
            db.execute('ROLLBACK');raise
        usage=_usage_snapshot(db,activities)
        occupied_total=db.execute('SELECT COUNT(*) FROM occupied_baskets').fetchone()[0]
    finally:
        db.close()
    delay=os.environ.get(DELAY_ENV)
    if delay:time.sleep(float(delay))
    save_bytes(output,report_bytes)
    report=json.loads(report_bytes)
    return {'request_id':rid,'status':'cancelled','allocation_in_effect':False,'released':released,
            'output':str(Path(output)),'total_discount_cents':report['total_discount_cents'],
            'coupon_baskets':report['coupon_baskets'],
            'occupied_baskets_total':occupied_total,'activity_usage':usage}
