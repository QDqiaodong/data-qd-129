
import itertools, json, os, signal, sqlite3, stat, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PARAMS={'min_support_count':1,'max_itemset_size':3,'min_confidence':{'numerator':1,'denominator':1}}

def vou(*args,env=None):
    e=os.environ.copy()
    if env:e.update(env)
    return subprocess.run([sys.executable,'-m','basket_rules.vouchcli',*[str(a) for a in args]],
                          capture_output=True,text=True,env=e)

def snapshot(baskets,**over):
    data=dict(PARAMS);data['baskets']=[{'id':bid,'items':list(items)} for bid,items in baskets]
    data.update(over);return data

def shuffled_snapshot(baskets):
    # Same semantic content, different object/basket/item order.
    import random
    rng=random.Random(7)
    bs=[{'items':rng.sample(list(items),len(items)),'id':bid} for bid,items in baskets]
    rng.shuffle(bs)
    data=dict(PARAMS);data['baskets']=bs
    # Reorder top-level keys too.
    return {'baskets':data['baskets'],'min_confidence':data['min_confidence'],
            'max_itemset_size':data['max_itemset_size'],'min_support_count':data['min_support_count']}

def brute_force(baskets,activities,remaining):
    """Exhaustive reference: every basket picks null or one eligible activity with
    residual quota; maximize total discount, then lexicographically smallest rank
    vector (null rank = k, larger than every activity id)."""
    k=len(activities);choices=[]
    for _,items in baskets:
        itemset=set(items);opts=[k]
        for j,a in enumerate(activities):
            if remaining.get(a['id'],0)>0 and set(a['required_items']).issubset(itemset):opts.append(j)
        choices.append(opts)
    best=None
    for vector in itertools.product(*choices):
        used=[0]*k
        for rank in vector:
            if rank!=k:used[rank]+=1
        if any(used[j]>remaining.get(activities[j]['id'],0) for j in range(k)):continue
        score=sum(0 if rank==k else activities[rank]['discount_cents'] for rank in vector)
        key=(-score,tuple(vector))
        if best is None or key<best[0]:best=(key,list(vector))
    if best is None:return [None]*len(baskets)
    return [None if rank==k else activities[rank]['id'] for rank in best[1]]

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.dir=Path(self.tmp.name)
        self.ledger=self.dir/'vouch.db';self.out=self.dir/'out.json'
    def tearDown(self):self.tmp.cleanup()
    def write(self,name,data):
        path=self.dir/name;path.write_text(json.dumps(data,ensure_ascii=False));return path
    def init(self,activities,name='acts.json'):
        path=self.write(name,activities)
        r=vou('init','--ledger',self.ledger,'--activities',path)
        self.assertEqual(r.returncode,0,r.stderr);return path
    def allocate(self,rid,source,output=None,env=None):
        return vou('allocate','--ledger',self.ledger,'--request-id',rid,
                   '--source',source,'--output',output or self.out,env=env)
    def cancel(self,rid,output=None,env=None):
        return vou('cancel','--ledger',self.ledger,'--request-id',rid,
                   '--output',output or self.dir/f'cancel-{rid}.json',env=env)
    def db(self):
        conn=sqlite3.connect(str(self.ledger))
        self.addCleanup(conn.close);return conn
    def report(self,path=None):return json.loads((path or self.out).read_text())
    def assert_invariants(self):
        conn=self.db()
        # Quota never exceeded; activity_usage matches occupied basket counts.
        for aid,quota,used in conn.execute(
                'SELECT a.activity_id,a.quota,COALESCE(u.used,0) FROM activities a '
                'LEFT JOIN activity_usage u ON a.activity_id=u.activity_id'):
            self.assertLessEqual(used,quota)
            counted=conn.execute('SELECT COUNT(*) FROM occupied_baskets WHERE activity_id=?',(aid,)).fetchone()[0]
            self.assertEqual(used,counted)
        # Occupied baskets belong only to non-cancelled requests, one row each.
        rows=conn.execute('SELECT o.basket_id,o.request_id,r.state FROM occupied_baskets o '
                          'JOIN requests r ON r.request_id=o.request_id').fetchall()
        self.assertTrue(all(state=='reserved' for _,_,state in rows))
        # Allocations and request archived rows agree.
        for rid,state,blob in conn.execute('SELECT request_id,state,report_bytes FROM requests'):
            report=json.loads(blob)
            n_coupon=sum(1 for b in report['baskets'] if b['activity_id'] is not None)
            n_alloc=conn.execute('SELECT COUNT(*) FROM allocations WHERE request_id=? AND activity_id IS NOT NULL',
                                 (rid,)).fetchone()[0]
            self.assertEqual(n_coupon,n_alloc)
            n_null=conn.execute('SELECT COUNT(*) FROM allocations WHERE request_id=? AND activity_id IS NULL',
                                (rid,)).fetchone()[0]
            self.assertEqual(n_null,len(report['baskets'])-n_coupon)
            if state=='cancelled':
                self.assertEqual(conn.execute(
                    'SELECT COUNT(*) FROM occupied_baskets WHERE request_id=?',(rid,)).fetchone()[0],0)
        return conn

class InitValidation(Base):
    ACTS=lambda acts:[{'id':a,'required_items':['x'],'quota':1,'discount_cents':1} for a in acts]
    def test_valid_init_persists_sorted_catalogue(self):
        acts=[{'id':'B','required_items':['y','x'],'quota':200,'discount_cents':10**9},
              {'id':'A','required_items':['x'],'quota':1,'discount_cents':1}]
        path=self.init(acts)
        r=json.loads(vou('init','--ledger',self.dir/'other.db','--activities',path).stdout)
        self.assertEqual([a['id'] for a in r['activities']],['A','B'])
        self.assertEqual(r['activities'][1]['required_items'],['x','y'])
        conn=self.db()
        # ord is assigned after validation sorts the catalogue by id; required items are
        # normalized to sorted order regardless of input order.
        self.assertEqual([row[0] for row in conn.execute('SELECT activity_id FROM activities ORDER BY ord')],['A','B'])
        self.assertEqual(json.loads(conn.execute(
            "SELECT required_items FROM activities WHERE activity_id='B'").fetchone()[0]),['x','y'])

    def test_same_required_items_distinct_activities_allowed(self):
        path=self.init([{'id':'A1','required_items':['x','y'],'quota':1,'discount_cents':5},
                        {'id':'A2','required_items':['y','x'],'quota':2,'discount_cents':9}])
        r=vou('init','--ledger',self.dir/'o.db','--activities',path)
        self.assertEqual(r.returncode,0,r.stderr)

    def test_activity_bounds(self):
        def bad(acts):
            path=self.write('bad.json',acts)
            r=vou('init','--ledger',self.dir/f'b{len(os.listdir(self.dir))}.db','--activities',path)
            self.assertEqual(r.returncode,2,r.stdout);return r
        base=lambda:{'id':'A','required_items':['x'],'quota':1,'discount_cents':1}
        a=base();a['quota']=0;bad([a])
        a=base();a['quota']=201;bad([a])
        a=base();a['discount_cents']=0;bad([a])
        a=base();a['discount_cents']=10**9+1;bad([a])
        a=base();a['required_items']=[];bad([a])
        a=base();a['required_items']=[f'i{i}' for i in range(13)];bad([a])
        a=base();a['required_items']=['x','x'];bad([a])
        a=base();a['extra']=1;bad([a])
        bad([{'id':f'A{i}','required_items':['x'],'quota':1,'discount_cents':1} for i in range(31)])
        two=[base(),base()];two[1]['id']='A';bad(two)
        bad({'not':'array'})
        # Blank identifier rejected via shared ident() rule.
        a=base();a['id']=' ';bad([a])

    def test_reinit_and_path_safety(self):
        path=self.init([{'id':'A','required_items':['x'],'quota':1,'discount_cents':1}])
        # Reusing an existing ledger path is refused before any file comparison.
        r=vou('init','--ledger',self.ledger,'--activities',path)
        self.assertEqual(r.returncode,2);self.assertIn('ledger already exists',r.stderr)
        # A fresh path that points at an existing activities file fails the same-path guard.
        r=vou('init','--ledger',path,'--activities',path)
        self.assertEqual(r.returncode,2);self.assertIn('must differ',r.stderr)
        missing=self.dir/'nope.json'
        r=vou('init','--ledger',self.dir/'m.db','--activities',missing)
        self.assertEqual(r.returncode,2)

class SnapshotValidation(Base):
    def test_snapshot_contract_reused(self):
        self.init([{'id':'A','required_items':['x'],'quota':1,'discount_cents':1}])
        good=snapshot([('b1',['x'])])
        def bad(data):
            path=self.write('s.json',data)
            r=self.allocate('r',path)
            self.assertEqual(r.returncode,2,r.stdout);return r
        d=json.loads(json.dumps(good));d['unknown']=1;bad(d)
        d=json.loads(json.dumps(good));d['baskets'].append({'id':'b1','items':['y']});bad(d)
        d=json.loads(json.dumps(good));d['baskets'][0]['items']=['x','x'];bad(d)
        d=json.loads(json.dumps(good));d['baskets']=[];bad(d)
        d=json.loads(json.dumps(good));d['baskets']=[{'id':f'b{i}','items':[]} for i in range(201)];bad(d)
        d=json.loads(json.dumps(good));d['min_support_count']=0;bad(d)
        d=json.loads(json.dumps(good));d['min_confidence']={'numerator':2,'denominator':1};bad(d)
        d=json.loads(json.dumps(good));d['baskets']=[{'id':f'b{i}','items':[f'i{i}']} for i in range(13)];bad(d)

class ExactOptimization(Base):
    def test_greedy_loses_overlapping_activities(self):
        # Per-basket greedy (highest discount first) takes A on b1 and starves b2: 100.
        # The exact optimum puts B on b1 and A on b2: 160.
        self.init([{'id':'A','required_items':['x'],'quota':1,'discount_cents':100},
                   {'id':'B','required_items':['x','y'],'quota':1,'discount_cents':60}])
        src=self.write('s.json',snapshot([('b1',['x','y']),('b2',['x'])]))
        r=self.allocate('r1',src);self.assertEqual(r.returncode,0,r.stderr)
        report=self.report()
        self.assertEqual(report['total_discount_cents'],160)
        self.assertEqual({b['id']:b['activity_id'] for b in report['baskets']},{'b1':'B','b2':'A'})
        self.assertEqual({o['activity_id']:o['reserved'] for o in report['activity_occupancy']},
                         {'A':1,'B':1})

    def test_exact_tie_smallest_activity_id(self):
        self.init([{'id':'A2','required_items':['x'],'quota':1,'discount_cents':10},
                   {'id':'A1','required_items':['x'],'quota':1,'discount_cents':10}])
        src=self.write('s.json',snapshot([('b1',['x'])]))
        r=self.allocate('t',src);self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual([b['activity_id'] for b in self.report()['baskets']],['A1'])

    def test_exact_tie_null_is_greater_than_every_id(self):
        # One quota, two eligible baskets: equal score either way; [A,null] < [null,A].
        self.init([{'id':'A1','required_items':['x'],'quota':1,'discount_cents':10}])
        src=self.write('s.json',snapshot([('z2',['x']),('a1',['x'])]))
        r=self.allocate('t',src);self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual([(b['id'],b['activity_id']) for b in self.report()['baskets']],
                         [('a1','A1'),('z2',None)])

    def test_empty_solution_is_zero_score_success(self):
        self.init([{'id':'A','required_items':['x'],'quota':2,'discount_cents':10}])
        # No eligible basket and one empty basket: the request still succeeds with nulls only.
        src=self.write('s.json',snapshot([('e1',[]),('e2',['y'])]))
        r=self.allocate('e',src);self.assertEqual(r.returncode,0,r.stderr)
        receipt=json.loads(r.stdout)
        self.assertEqual(receipt['status'],'reserved');self.assertEqual(receipt['total_discount_cents'],0)
        report=self.report()
        self.assertTrue(all(b['activity_id'] is None and b['discount_cents']==0 for b in report['baskets']))
        self.assertTrue(all(o['reserved']==0 for o in report['activity_occupancy']))
        conn=self.assert_invariants()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM occupied_baskets').fetchone()[0],0)
        # Null baskets are recorded in the ledger but occupy nothing.
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM allocations WHERE request_id=?',('e',)).fetchone()[0],2)

    def test_quota_exhausted_still_succeeds_with_nulls(self):
        self.init([{'id':'A','required_items':['x'],'quota':1,'discount_cents':10}])
        s1=self.write('s1.json',snapshot([('b1',['x'])]))
        self.assertEqual(self.allocate('r1',s1).returncode,0)
        s2=self.write('s2.json',snapshot([('b2',['x'])]))
        r=self.allocate('r2',s2);self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['total_discount_cents'],0)
        self.assertEqual([b['activity_id'] for b in self.report()['baskets']],[None])

    def test_solver_matches_brute_force_on_random_instances(self):
        import random
        rng=random.Random(20261115)
        items=['i%d'%d for d in range(6)]
        for trial in range(60):
            acts=[]
            for j in range(rng.randint(1,4)):
                k=rng.randint(1,3)
                acts.append({'id':'A%d'%j,'required_items':rng.sample(items,k),
                             'quota':rng.randint(1,3),'discount_cents':rng.randint(1,50)})
            self.init(acts,name=f'acts{trial}.json')
            baskets=[('b%d'%i,rng.sample(items,rng.randint(0,4))) for i in range(rng.randint(1,7))]
            src=self.write(f's{trial}.json',snapshot(baskets))
            # Vary remaining quota by pre-occupying some activities with disjoint basket ids.
            remaining={a['id']:a['quota'] for a in acts}
            pre=[];seq=0
            for a in acts:
                for _ in range(rng.randint(0,a['quota'])):
                    pre.append(('pre-%d-%d'%(trial,seq),a['required_items']));seq+=1
            if pre:
                psrc=self.write(f'p{trial}.json',snapshot(pre))
                rr=self.allocate(f'pre{trial}',psrc)
                self.assertEqual(rr.returncode,0,rr.stderr)
                conn=self.db()
                used=dict(conn.execute('SELECT activity_id,used FROM activity_usage'))
                remaining={a['id']:a['quota']-used.get(a['id'],0) for a in acts}
            expected=brute_force(sorted(baskets),sorted(acts,key=lambda a:a['id']),remaining)
            rr=self.allocate(f'r{trial}',src)
            self.assertEqual(rr.returncode,0,rr.stderr)
            got=[b['activity_id'] for b in self.report()['baskets']]
            self.assertEqual(got,expected,(trial,baskets,acts,remaining))
            self.assert_invariants()
            # Fresh ledger per trial.
            self.ledger=self.dir/f'v{trial+1}.db'

    def test_large_bounds_and_integer_discounts(self):
        # 30 activities, 200 baskets, 1e9 discounts: exact integer arithmetic, no float.
        acts=[{'id':'A%02d'%j,'required_items':['i%d'%(j%12)],'quota':200,
               'discount_cents':10**9-j} for j in range(30)]
        self.init(acts)
        baskets=[('b%03d'%i,['i%d'%(i%12)]) for i in range(200)]
        src=self.write('big.json',snapshot(baskets))
        t0=time.monotonic()
        r=self.allocate('big',src);self.assertEqual(r.returncode,0,r.stderr)
        self.assertLess(time.monotonic()-t0,30)
        report=self.report()
        self.assertEqual(report['coupon_baskets'],200)
        self.assertEqual(report['total_discount_cents'],
                         sum(b['discount_cents'] for b in report['baskets']))
        blob=(self.out).read_text()
        self.assertNotIn('.0',blob)
        self.assert_invariants()

class IdentityReplayConflict(Base):
    ACTS=[{'id':'A','required_items':['x'],'quota':2,'discount_cents':10},
          {'id':'B','required_items':['x','y'],'quota':1,'discount_cents':7}]
    BASKETS=[('b1',['y','x']),('b2',['x']),('b3',[])]
    def test_shuffled_structure_replays_byte_identical(self):
        self.init(self.ACTS)
        s1=self.write('s1.json',snapshot(self.BASKETS))
        r=self.allocate('r1',s1);self.assertEqual(r.returncode,0,r.stderr)
        first=json.loads(r.stdout);self.assertFalse(first['replayed'])
        before=self.out.read_bytes()
        s2=self.write('s2.json',shuffled_snapshot(self.BASKETS))
        out2=self.dir/'o2.json'
        r=self.allocate('r1',s2,out2);self.assertEqual(r.returncode,0,r.stderr)
        replay=json.loads(r.stdout)
        self.assertTrue(replay['replayed']);self.assertTrue(replay['allocation_in_effect'])
        self.assertEqual(out2.read_bytes(),before)
        # Replay does not occupy baskets or spend quota a second time. b1 and b2 both take A
        # (discount 10 > B's 7); B stays unused, A holds two of its two quota slots.
        conn=self.db()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0],1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM occupied_baskets').fetchone()[0],2)
        self.assertEqual(dict(conn.execute('SELECT activity_id,used FROM activity_usage')),{'A':2})

    def test_different_identity_same_id_rejected_without_mutation(self):
        self.init(self.ACTS)
        s1=self.write('s1.json',snapshot(self.BASKETS))
        self.assertEqual(self.allocate('r1',s1).returncode,0)
        before=self.out.read_bytes()
        changed=[('b1',['x','y','z']),('b2',['x']),('b3',[])]
        s2=self.write('s2.json',snapshot(changed))
        r=self.allocate('r1',s2);self.assertEqual(r.returncode,2)
        self.assertIn('different snapshot identity',r.stderr)
        # Old record and old output are untouched; no new occupancy appeared.
        self.assertEqual(self.out.read_bytes(),before)
        conn=self.assert_invariants()
        self.assertEqual(conn.execute('SELECT state FROM requests').fetchone()[0],'reserved')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM occupied_baskets').fetchone()[0],2)
        # A parameter change is also an identity change.
        s3=self.write('s3.json',snapshot(self.BASKETS,min_support_count=2))
        r=self.allocate('r1',s3);self.assertEqual(r.returncode,2)
        # Replay with the original identity still works.
        r=self.allocate('r1',s1,self.dir/'again.json');self.assertEqual(r.returncode,0,r.stderr)
        self.assertTrue(json.loads(r.stdout)['replayed'])

    def test_output_path_is_not_part_of_identity(self):
        self.init(self.ACTS)
        s1=self.write('s1.json',snapshot(self.BASKETS))
        self.assertEqual(self.allocate('r1',s1,self.dir/'a.json').returncode,0)
        r=self.allocate('r1',s1,self.dir/'b.json');self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual((self.dir/'a.json').read_bytes(),(self.dir/'b.json').read_bytes())

class CancelLifecycle(Base):
    ACTS=[{'id':'A','required_items':['x'],'quota':3,'discount_cents':10},
          {'id':'B','required_items':['x','y'],'quota':1,'discount_cents':7}]
    def test_cancel_releases_only_its_own_baskets_and_quota_once(self):
        self.init(self.ACTS)
        s1=self.write('s1.json',snapshot([('b1',['x','y']),('b2',['x'])]))
        r1_out=self.dir/'r1.json'
        self.assertEqual(self.allocate('r1',s1,r1_out).returncode,0)
        before_cancel=r1_out.read_bytes()
        s2=self.write('s2.json',snapshot([('c1',['x'])]))
        self.assertEqual(self.allocate('r2',s2).returncode,0)
        conn=self.db()
        # r1: b1->A (A is worth more than B), b2->A; r2: c1->A -> A used three times.
        # B is never occupied, so it has no usage row at all.
        self.assertEqual(dict(conn.execute('SELECT activity_id,used FROM activity_usage')),{'A':3})
        r=self.cancel('r1');self.assertEqual(r.returncode,0,r.stderr)
        receipt=json.loads(r.stdout)
        self.assertEqual(receipt['status'],'cancelled');self.assertFalse(receipt['allocation_in_effect'])
        self.assertTrue(receipt['released'])
        # r2's basket and quota share survive; B was never used, so it has no usage row.
        conn=self.assert_invariants()
        self.assertEqual([row[0] for row in conn.execute('SELECT basket_id FROM occupied_baskets')],['c1'])
        self.assertEqual(dict(conn.execute('SELECT activity_id,used FROM activity_usage')),{'A':1})
        # The cancel output is the original allocation report, byte-stable.
        self.assertEqual(Path(receipt['output']).read_bytes(),before_cancel)
        # Repeated cancel is idempotent and releases nothing the second time.
        r=self.cancel('r1');self.assertEqual(r.returncode,0,r.stderr)
        receipt=json.loads(r.stdout);self.assertFalse(receipt['released'])
        self.assert_invariants()

    def test_unknown_cancel_fails(self):
        self.init(self.ACTS)
        r=self.cancel('ghost');self.assertEqual(r.returncode,2)
        self.assertIn('unknown request id',r.stderr)

    def test_same_id_after_cancel_never_revives(self):
        self.init(self.ACTS)
        s1=self.write('s1.json',snapshot([('b1',['x','y'])]))
        self.assertEqual(self.allocate('r1',s1).returncode,0)
        original=self.out.read_bytes()
        self.assertEqual(self.cancel('r1').returncode,0)
        # Same id + same identity: returns cancelled and the original allocation; no re-occupancy.
        r=self.allocate('r1',s1,self.dir/'replay.json');self.assertEqual(r.returncode,0,r.stderr)
        receipt=json.loads(r.stdout)
        self.assertEqual(receipt['status'],'cancelled');self.assertFalse(receipt['allocation_in_effect'])
        self.assertTrue(receipt['replayed']);self.assertEqual(receipt['coupon_baskets'],1)
        self.assertEqual((self.dir/'replay.json').read_bytes(),original)
        conn=self.assert_invariants()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM occupied_baskets').fetchone()[0],0)
        # The zero-usage row persists with used=0; nothing is reserved.
        self.assertEqual(dict(conn.execute('SELECT activity_id,used FROM activity_usage')),{'A':0})
        row=conn.execute('SELECT state,cancelled_at FROM requests').fetchone()
        self.assertEqual(row,('cancelled',row[1]));self.assertIsNotNone(row[1])
        # A genuinely new id must be used to reserve the basket again.
        r=self.allocate('r2',s1,self.dir/'new.json');self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['status'],'reserved')
        self.assert_invariants()

class CrossRequestConflicts(Base):
    ACTS=[{'id':'A','required_items':['x'],'quota':5,'discount_cents':10}]
    def test_any_overlapping_basket_conflicts_the_whole_request(self):
        self.init(self.ACTS)
        s1=self.write('s1.json',snapshot([('b1',['x']),('b2',['x'])]))
        self.assertEqual(self.allocate('r1',s1).returncode,0)
        # New request shares one basket id but also has disjoint baskets: whole request fails.
        s2=self.write('s2.json',snapshot([('b2',['x']),('b9',['x'])]))
        r=self.allocate('r2',s2);self.assertEqual(r.returncode,2)
        self.assertIn('already reserved',r.stderr)
        conn=self.assert_invariants()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0],1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM occupied_baskets').fetchone()[0],2)
        # A fully disjoint request succeeds.
        s3=self.write('s3.json',snapshot([('b3',['x'])]))
        r=self.allocate('r3',s3);self.assertEqual(r.returncode,0,r.stderr)
        self.assert_invariants()
        # After r1 cancels, its basket ids become available again.
        self.assertEqual(self.cancel('r1').returncode,0)
        r=self.allocate('r4',s2,self.dir/'r4.json');self.assertEqual(r.returncode,0,r.stderr)
        self.assert_invariants()

    def test_null_only_request_does_not_block_basket_ids(self):
        # The only activity requires z; basket b1 gets null and therefore occupies no basket id.
        self.init([{'id':'A','required_items':['z'],'quota':1,'discount_cents':10}])
        s1=self.write('s1.json',snapshot([('b1',['x'])]))
        self.assertEqual(self.allocate('r1',s1).returncode,0)
        conn=self.db()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM occupied_baskets').fetchone()[0],0)
        # A second request referencing the same basket id is not in conflict (it still scores
        # zero here, but the request is accepted rather than rejected as occupied).
        r=self.allocate('r2',s1,self.dir/'o2.json')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['total_discount_cents'],0)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0],2)

class Concurrency(Base):
    ACTS=[{'id':'A','required_items':['x'],'quota':1,'discount_cents':10}]
    def _race(self,snap_name,baskets,rids):
        self.init(self.ACTS)
        src=self.write(snap_name,snapshot(baskets))
        procs=[subprocess.Popen(
            [sys.executable,'-m','basket_rules.vouchcli','allocate','--ledger',self.ledger,
             '--request-id',rid,'--source',src,'--output',self.dir/f'{rid}.json'],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for rid in rids]
        results=[p.communicate()+(p.returncode,) for p in procs]
        return results
    def test_quota_one_concurrent_requests_single_effective_reservation(self):
        # Same basket id raced by distinct ids: exactly one reservation survives and the
        # quota is never overdrawn; the losers fail the basket conflict.
        results=self._race('s.json',[('b1',['x'])],['q1','q2','q3'])
        codes=sorted(r[2] for r in results)
        self.assertEqual(codes.count(0),1);self.assertEqual(codes.count(2),2)
        winner=[json.loads(r[0]) for r in results if r[2]==0][0]
        self.assertEqual(winner['status'],'reserved');self.assertEqual(winner['coupon_baskets'],1)
        conn=self.assert_invariants()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0],1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM occupied_baskets').fetchone()[0],1)
        self.assertEqual(conn.execute('SELECT used FROM activity_usage').fetchone()[0],1)
    def test_quota_one_disjoint_baskets_only_one_coupon(self):
        # Disjoint basket ids but quota 1: one request wins the coupon, the other must still
        # succeed (zero-score null allocation) without overdraw.
        self.init(self.ACTS)
        s1=self.write('s1.json',snapshot([('b1',['x'])]))
        s2=self.write('s2.json',snapshot([('b2',['x'])]))
        procs=[subprocess.Popen(
            [sys.executable,'-m','basket_rules.vouchcli','allocate','--ledger',self.ledger,
             '--request-id',rid,'--source',src,'--output',self.dir/f'{rid}.json'],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            for rid,src in (('q1',s1),('q2',s2))]
        results=[p.communicate()+(p.returncode,) for p in procs]
        self.assertTrue(all(r[2]==0 for r in results))
        receipts=[json.loads(r[0]) for r in results]
        self.assertEqual(sorted(r['total_discount_cents'] for r in receipts),[0,10])
        conn=self.assert_invariants()
        self.assertEqual(conn.execute('SELECT used FROM activity_usage').fetchone()[0],1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM occupied_baskets').fetchone()[0],1)

    def test_cancel_then_new_id_changes_the_optimum(self):
        # Two overlapping activities with tight quotas: after the first request cancels,
        # the new request observes released quota and the feasible graph/optimum change.
        self.init([{'id':'A','required_items':['x'],'quota':1,'discount_cents':100},
                   {'id':'B','required_items':['x','y'],'quota':1,'discount_cents':60}])
        s1=self.write('s1.json',snapshot([('b1',['x','y']),('b2',['x'])]))
        self.assertEqual(self.allocate('r1',s1).returncode,0)
        self.assertEqual(self.report()['total_discount_cents'],160)
        # A concurrent-looking second request on disjoint baskets finds A exhausted.
        s2=self.write('s2.json',snapshot([('c1',['x'])]))
        r=self.allocate('r2',s2);self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['total_discount_cents'],0)
        self.assertEqual(self.cancel('r1').returncode,0)
        # New id after release: quota restored and the optimum is solved on the new graph.
        r=self.allocate('r3',s2,self.dir/'r3.json');self.assertEqual(r.returncode,0,r.stderr)
        report=json.loads((self.dir/'r3.json').read_text())
        self.assertEqual(report['total_discount_cents'],100)
        self.assertEqual([b['activity_id'] for b in report['baskets']],['A'])
        # The old id never resurrects.
        r=self.allocate('r1',s1,self.dir/'r1-again.json');self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['status'],'cancelled')
        self.assert_invariants()

    def test_concurrent_cancel_and_allocate_linearize(self):
        # Fire cancels and allocates against overlapping basket ids; every terminal state
        # must correspond to some serial order: invariants hold and no id resurrects.
        self.init(self.ACTS)
        s1=self.write('s1.json',snapshot([('b1',['x'])]))
        self.assertEqual(self.allocate('hold',s1).returncode,0)
        s2=self.write('s2.json',snapshot([('b1',['x']),('b2',['x'])]))
        procs=[
            subprocess.Popen([sys.executable,'-m','basket_rules.vouchcli','cancel','--ledger',self.ledger,
                              '--request-id','hold','--output',self.dir/'c.json'],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True),
            subprocess.Popen([sys.executable,'-m','basket_rules.vouchcli','allocate','--ledger',self.ledger,
                              '--request-id','next','--source',s2,'--output',self.dir/'n.json'],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)]
        rs=[p.communicate()+(p.returncode,) for p in procs]
        for out,err,code in rs:self.assertIn(code,(0,2),err)
        conn=self.assert_invariants()
        state=dict(conn.execute('SELECT request_id,state FROM requests'))
        self.assertEqual(state['hold'],'cancelled')
        if 'next' in state:
            self.assertEqual(state['next'],'reserved')
            # 'next' conflicts on b1, so it can only survive by linearizing after the
            # cancel. With quota 1 exactly one of its two eligible baskets is occupied.
            occupied=sorted(r[0] for r in conn.execute('SELECT basket_id FROM occupied_baskets'))
            self.assertEqual(occupied,['b1'])
            self.assertEqual(conn.execute('SELECT used FROM activity_usage').fetchone()[0],1)
        else:
            # 'next' linearized first, hit the b1 conflict and rolled back; the cancel then
            # released hold, leaving no occupied baskets. Both serial orders are valid.
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM occupied_baskets').fetchone()[0],0)

class CrashAndDisk(Base):
    ACTS=[{'id':'A','required_items':['x'],'quota':1,'discount_cents':10}]
    def test_pre_commit_abort_rolls_back_and_retry_is_clean(self):
        self.init(self.ACTS)
        src=self.write('s.json',snapshot([('b1',['x'])]))
        env={'BASKET_RULES_VOUCHERS_ABORT':'1'}
        r=self.allocate('r1',src,env=env);self.assertEqual(r.returncode,2)
        self.assertFalse(self.out.exists())
        conn=self.db()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0],0)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM occupied_baskets').fetchone()[0],0)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM activity_usage').fetchone()[0],0)
        r=self.allocate('r1',src);self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['status'],'reserved')
        self.assert_invariants()

    def test_kill_after_commit_retry_replays_without_double_charge(self):
        self.init(self.ACTS)
        src=self.write('s.json',snapshot([('b1',['x'])]))
        proc=subprocess.Popen(
            [sys.executable,'-m','basket_rules.vouchcli','allocate','--ledger',self.ledger,
             '--request-id','killed','--source',src,'--output',self.out],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
            env={**os.environ,'BASKET_RULES_VOUCHERS_DELAY_SECONDS':'30'})
        deadline=time.monotonic()+30;conn=self.db();committed=False
        while time.monotonic()<deadline:
            row=conn.execute('SELECT state FROM requests WHERE request_id=?',('killed',)).fetchone()
            if row is not None:
                self.assertEqual(row[0],'reserved');committed=True;break
            time.sleep(0.02)
        conn.close()
        self.assertTrue(committed)
        self.assertFalse(self.out.exists())
        proc.send_signal(signal.SIGKILL);proc.communicate()
        self.assertNotEqual(proc.returncode,0)
        # Ledger state survived the kill: exactly one reservation, quota spent once.
        conn=self.db()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM occupied_baskets').fetchone()[0],1)
        self.assertEqual(conn.execute('SELECT used FROM activity_usage').fetchone()[0],1)
        archived=conn.execute('SELECT report_bytes FROM requests').fetchone()[0];conn.close()
        # Retry replays and never spends quota again.
        r=self.allocate('killed',src);self.assertEqual(r.returncode,0,r.stderr)
        receipt=json.loads(r.stdout);self.assertTrue(receipt['replayed'])
        self.assertEqual(self.out.read_bytes(),bytes(archived))
        conn=self.db()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0],1)
        self.assertEqual(conn.execute('SELECT used FROM activity_usage').fetchone()[0],1)

    def test_disk_failure_keeps_old_output_and_retry_replays(self):
        if os.geteuid()==0:self.skipTest('root bypasses directory write permissions')
        self.init(self.ACTS)
        src=self.write('s.json',snapshot([('b1',['x'])]))
        rodir=self.dir/'readonly';rodir.mkdir()
        target=rodir/'o.json';os.chmod(rodir,stat.S_IRUSR|stat.S_IXUSR)
        try:
            r=self.allocate('disk',src,output=target);self.assertEqual(r.returncode,2)
            self.assertFalse(target.exists())
        finally:
            os.chmod(rodir,stat.S_IRWXU)
        # The reservation committed before the failed write; retry replays without double charge.
        r=self.allocate('disk',src);self.assertEqual(r.returncode,0,r.stderr)
        self.assertTrue(json.loads(r.stdout)['replayed'])
        self.assert_invariants()
        # A prior output file is preserved when a later command fails.
        self.out.write_text('prior')
        bad=self.write('bad.json',{'unexpected':1})
        r=self.allocate('disk',bad,self.out);self.assertEqual(r.returncode,2)
        self.assertEqual(self.out.read_text(),'prior')

    def test_state_consistent_after_restart(self):
        self.init([{'id':'A','required_items':['x'],'quota':2,'discount_cents':10},
                   {'id':'B','required_items':['y'],'quota':1,'discount_cents':5}])
        s1=self.write('s1.json',snapshot([('b1',['x']),('b2',['y']),('b3',[])]))
        self.assertEqual(self.allocate('r1',s1).returncode,0)
        s2=self.write('s2.json',snapshot([('c1',['x'])]))
        self.assertEqual(self.allocate('r2',s2).returncode,0)
        self.assertEqual(self.cancel('r1').returncode,0)
        # Reopen the file as brand-new connections (simulated restart): invariants still hold.
        self.assert_invariants()
        conn=sqlite3.connect(str(self.ledger))
        self.assertEqual(dict(conn.execute('SELECT request_id,state FROM requests')),
                         {'r1':'cancelled','r2':'reserved'})
        self.assertEqual([r[0] for r in conn.execute('SELECT basket_id FROM occupied_baskets')],['c1'])
        # r1 released B (and one A share); released rows persist at used=0.
        self.assertEqual(dict(conn.execute('SELECT activity_id,used FROM activity_usage')),{'A':1,'B':0})
        # A new request after restart sees the released capacity.
        conn.close()
        s3=self.write('s3.json',snapshot([('b1',['x']),('b2',['y'])]))
        r=self.allocate('r3',s3,self.dir/'r3.json');self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['total_discount_cents'],15)
        self.assert_invariants()

class OutputSafety(Base):
    def test_output_cannot_overwrite_inputs_and_rejects_symlink(self):
        acts=self.write('acts.json',[{'id':'A','required_items':['x'],'quota':1,'discount_cents':1}])
        self.assertEqual(vou('init','--ledger',self.ledger,'--activities',acts).returncode,0)
        src=self.write('s.json',snapshot([('b1',['x'])]))
        self.assertEqual(self.allocate('r',src,output=self.ledger).returncode,2)
        self.assertEqual(self.allocate('r',src,output=src).returncode,2)
        self.assertEqual(self.allocate('r',src,output=acts).returncode,2)
        target=self.dir/'target.json';target.write_text('keep')
        link=self.dir/'link.json';link.symlink_to(target)
        self.assertEqual(self.allocate('r',src,output=link).returncode,2)
        self.assertEqual(target.read_text(),'keep')

if __name__=='__main__':unittest.main()
