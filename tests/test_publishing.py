
import json, os, sqlite3, stat, subprocess, sys, tempfile, time, unittest
from collections import Counter
from pathlib import Path
from basket_rules.publishing import (SystemSecureBits, geometric_draw,
    symmetric_geometric, init_ledger, publish_report)

ROOT=Path(__file__).resolve().parents[1]
WINDOW,MINIMUM,MAX_SIZE,NUM,DEN=100,2,3,1,2
UNIVERSE=['bread','milk','sugar','tea']

def pub(*args,env=None):
    e=os.environ.copy()
    if env:e.update(env)
    return subprocess.run([sys.executable,'-m','basket_rules.pubcli',*[str(a) for a in args]],
                          capture_output=True,text=True,env=e)

def roll(*args):
    return subprocess.run([sys.executable,'-m','basket_rules.rollcli',*[str(a) for a in args]],
                          capture_output=True,text=True)

class ScriptedBits:
    """Deterministic secure-bit replacement for per-cell noise assertions."""
    def __init__(self,stream):self.stream=list(stream);self.i=0
    def bit(self):
        b=self.stream[self.i];self.i+=1;return b

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.dir=Path(self.tmp.name)
        self.src=self.dir/'roll.db';self.ledger=self.dir/'pub.db';self.out=self.dir/'out.json'
        r=roll('init','--db',self.src,'--window-seconds',WINDOW,'--min-support-count',MINIMUM,
               '--max-itemset-size',MAX_SIZE,'--min-confidence',NUM,DEN)
        self.assertEqual(r.returncode,0,r.stderr)
        self.universe_file=self.dir/'universe.json'
        self.universe_file.write_text(json.dumps(UNIVERSE))
    def tearDown(self):self.tmp.cleanup()
    def write_jsonl(self,name,rows):
        path=self.dir/name;path.write_text(''.join(json.dumps(row)+'\n' for row in rows));return path
    def load(self,rows,w=1000,name='batch.jsonl'):
        r=roll('load','--db',self.src,'--watermark',w,'--source',self.write_jsonl(name,rows))
        self.assertEqual(r.returncode,0,r.stderr);return r
    def init_ledger(self,quota=3):
        r=pub('init','--ledger',self.ledger,'--source',self.src,
              '--universe',self.universe_file,'--quota',quota)
        self.assertEqual(r.returncode,0,r.stderr);return r
    def load_seed(self):
        rows=[{'id':'a','rev':1,'time':950,'items':['bread','milk']},
              {'id':'b','rev':1,'time':960,'items':['bread','milk']},
              {'id':'c','rev':1,'time':970,'items':['bread']},
              {'id':'d','rev':1,'time':980,'items':['milk','tea']},
              {'id':'e','rev':1,'time':985,'items':[]}]
        self.load(rows);return rows

class Sampling(Base):
    def test_geometric_distribution_from_secure_source(self):
        bits=SystemSecureBits()
        # P(G=k)=2^(-k-1): empirical distribution must match the theoretical pmf.
        n=20000;samples=[geometric_draw(bits) for _ in range(n)]
        freq=Counter(samples)
        for k in range(6):
            self.assertLess(abs(freq[k]/n-2**(-k-1)),0.02,(k,freq[k]/n))
        self.assertEqual(max(samples)<25,True)
        # Z=G1-G2 symmetric around zero.
        zs=[symmetric_geometric(bits) for _ in range(n)]
        zc=Counter(zs)
        for k in range(1,5):
            self.assertLess(abs(zc[k]-zc[-k]),n*0.03,(k,zc[k],zc[-k]))
        self.assertAlmostEqual(sum(zs)/n,0,delta=0.05)
        # Z=G1-G2: P(Z=0)=sum_k 4^(-k-1)=1/3; for k>=1 each side has P(Z=k)=2^(-k)/3.
        self.assertLess(abs(zc[0]/n-1/3),0.02)
        for k in (1,2):
            self.assertLess(abs(zc[k]/n-(2**(-k)/3)),0.02)

class NoiseMechanics(Base):
    def test_scripted_noise_per_cell_and_negative_clip(self):
        self.load_seed();self.init_ledger()
        # Patterns in sorted order: ([],1), (['bread'],1), (['bread','milk'],2),
        # (['milk','tea'],1). Four cells consume eight geometric draws (G1,G2 each).
        # G1 bits then G2 bits per cell; script each draw explicitly.
        # cell []      : G1=0 (bit 0), G2=2 (1,1,0) -> Z=-2, true 1 -> clip 0
        # cell bread   : G1=3 (1,1,1,0), G2=0        -> Z=3,  true 1 -> 4
        # cell bread/m : G1=1 (1,0), G2=1 (1,0)      -> Z=0,  true 2 -> 2
        # cell milk/tea: G1=0, G2=0                  -> Z=0,  true 1 -> 1
        stream=[0, 1,1,0,
                1,1,1,0, 0,
                1,0, 1,0,
                0, 0]
        receipt=publish_report(self.ledger,'r1',self.out,bits=ScriptedBits(stream))
        self.assertEqual(receipt['status'],'published')
        report=json.loads(self.out.read_text())
        hist={tuple(c['items']):c['noisy_count'] for c in report['noisy_histogram']}
        self.assertEqual(hist,{():0,('bread',):4,('bread','milk'):2,('milk','tea'):1})
        # Derived numbers come only from the noisy cells.
        self.assertEqual(report['derived']['basket_count'],7)
        combos={tuple(f['items']):f['count'] for f in report['derived']['frequent_itemsets']}
        self.assertEqual(combos[('bread',)],6)            # 4 + 2
        self.assertEqual(combos[('bread','milk')],2)
        # milk/tea noisy cell is 1, below min_support_count=2, so it is correctly filtered.
        self.assertNotIn(('milk','tea'),combos)
        self.assertEqual(hist[('milk','tea')],1)
        self.assertEqual(report['noise']['secure_random'],True)
        self.assertEqual(report['noise']['per_cell_independent'],True)
        # The true counts (empty-basket 1, bread singleton 1) never appear as labels.
        blob=self.out.read_text()
        self.assertNotIn('"true_count"',blob)

    def test_empty_basket_is_its_own_cell(self):
        # Only empty active baskets: the histogram keeps the empty pattern cell.
        self.load([{'id':'e1','rev':1,'time':950,'items':[]},
                   {'id':'e2','rev':1,'time':951,'items':[]},
                   {'id':'e3','rev':1,'time':952,'items':[]}])
        self.init_ledger()
        # Force noise zero on the single cell: G1=0,G2=0.
        publish_report(self.ledger,'e',self.out,bits=ScriptedBits([0,0]))
        report=json.loads(self.out.read_text())
        self.assertEqual([c['items'] for c in report['noisy_histogram']],[[]])
        self.assertEqual(report['noisy_histogram'][0]['noisy_count'],3)
        self.assertEqual(report['derived']['basket_count'],3)
        self.assertEqual(report['derived']['frequent_itemsets'],[])
        self.assertEqual(report['derived']['rules'],[])

    def test_no_active_baskets_publishes_empty_histogram(self):
        self.init_ledger()
        report=json.loads(publish_report(self.ledger,'zero',self.out) and self.out.read_text())
        self.assertEqual(report['noisy_histogram'],[])
        self.assertEqual(report['derived']['basket_count'],0)
        self.assertEqual(report['derived']['frequent_itemsets'],[])
        self.assertEqual(report['derived']['rules'],[])

class QuotaAndReplay(Base):
    def test_quota_one_concurrent_same_and_different_ids(self):
        self.load_seed();self.init_ledger(quota=1)
        # Two different ids race: exactly one wins, the other is rejected exhausted.
        procs=[]
        for rid in ('race-a','race-b'):
            procs.append(subprocess.Popen(
                [sys.executable,'-m','basket_rules.pubcli','publish','--ledger',self.ledger,
                 '--request-id',rid,'--output',self.dir/f'{rid}.json'],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        results=[p.communicate()+(p.returncode,) for p in procs]
        codes=sorted(r[2] for r in results)
        self.assertEqual(codes,[0,2])
        winner=[r for r in results if r[2]==0][0]
        self.assertEqual(json.loads(winner[0])['quota_used'],1)
        self.assertEqual(json.loads(winner[0])['quota_remaining'],0)
        # Ledger holds exactly one publication: quota was never overdrawn.
        db=sqlite3.connect(str(self.ledger))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM publications').fetchone()[0],1)
        winner_id=db.execute('SELECT request_id FROM publications').fetchone()[0]
        db.close()
        # A third new id is refused as well.
        r=pub('publish','--ledger',self.ledger,'--request-id','race-c','--output',self.dir/'c.json')
        self.assertEqual(r.returncode,2);self.assertIn('quota exhausted',r.stderr)
        self.assertFalse((self.dir/'c.json').exists())
        # Same id concurrent replays: both succeed, both byte-identical, still one archive row.
        out2=self.dir/'replay.json'
        ps=[subprocess.Popen(
            [sys.executable,'-m','basket_rules.pubcli','publish','--ledger',self.ledger,
             '--request-id',winner_id,'--output',p],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True) for p in (self.dir/'again.json',out2)]
        rs=[p.communicate()+(p.returncode,) for p in ps]
        self.assertEqual(sorted(r[2] for r in rs),[0,0])
        self.assertTrue(all(json.loads(r[0])['status']=='replayed' for r in rs))
        self.assertEqual((self.dir/'again.json').read_bytes(),out2.read_bytes())
        db=sqlite3.connect(str(self.ledger))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM publications').fetchone()[0],1)
        db.close()

    def test_quota_deduction_once_on_replay_with_new_output_path(self):
        self.load_seed();self.init_ledger(quota=2)
        r=pub('publish','--ledger',self.ledger,'--request-id','q1','--output',self.out)
        self.assertEqual(r.returncode,0,r.stderr)
        first=json.loads(r.stdout);self.assertEqual(first['quota_used'],1)
        first_bytes=self.out.read_bytes()
        other=self.dir/'copied.json'
        r=pub('publish','--ledger',self.ledger,'--request-id','q1','--output',other)
        self.assertEqual(r.returncode,0,r.stderr)
        second=json.loads(r.stdout)
        self.assertEqual(second['status'],'replayed');self.assertEqual(second['quota_used'],1)
        self.assertEqual(other.read_bytes(),first_bytes)
        # Quota still allows exactly one more new id, then refuses.
        r=pub('publish','--ledger',self.ledger,'--request-id','q2','--output',self.dir/'q2.json')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['quota_used'],2)
        r=pub('publish','--ledger',self.ledger,'--request-id','q3','--output',self.dir/'q3.json')
        self.assertEqual(r.returncode,2);self.assertIn('quota exhausted',r.stderr)
        self.assertFalse((self.dir/'q3.json').exists())

    def test_replay_does_not_read_source_after_source_changes(self):
        self.load_seed();self.init_ledger(quota=2)
        r=pub('publish','--ledger',self.ledger,'--request-id','stable','--output',self.out)
        self.assertEqual(r.returncode,0,r.stderr);before=self.out.read_bytes()
        # Source advances: new revision, new watermark and changed active set.
        self.load([{'id':'c','rev':2,'time':1040,'items':['sugar','tea']},
                   {'id':'f','rev':1,'time':1045,'items':['bread']}],w=1050,name='b2.jsonl')
        # Even with the source file moved away, the old id replays byte-for-byte.
        moved=self.dir/'roll-moved.db';self.src.rename(moved)
        other=self.dir/'stable-copy.json'
        r=pub('publish','--ledger',self.ledger,'--request-id','stable','--output',other)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['status'],'replayed')
        self.assertEqual(other.read_bytes(),before)
        moved.rename(self.src)
        # A new id reads the fresh snapshot and gets different watermark content.
        r=pub('publish','--ledger',self.ledger,'--request-id','fresh','--output',self.dir/'fresh.json')
        self.assertEqual(r.returncode,0,r.stderr)
        fresh=json.loads(r.stdout)
        self.assertEqual(json.loads((self.dir/'fresh.json').read_text())['source_watermark'],1050)
        self.assertEqual(fresh['status'],'published')

class CrashAndDisk(Base):
    def test_kill_after_archive_commit_retry_replays_same_bytes(self):
        self.load_seed();self.init_ledger(quota=2)
        proc=subprocess.Popen(
            [sys.executable,'-m','basket_rules.pubcli','publish','--ledger',self.ledger,
             '--request-id','killed','--output',self.out],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
            env={**os.environ,'BASKET_RULES_PUBLISH_DELAY_SECONDS':'30'})
        deadline=time.monotonic()+30
        db=sqlite3.connect(str(self.ledger),timeout=1)
        archived=None
        while time.monotonic()<deadline:
            row=db.execute('SELECT report_bytes FROM publications WHERE request_id=?',
                           ('killed',)).fetchone()
            if row is not None:archived=row[0];break
            time.sleep(0.02)
        if archived is None:self.fail('archive never committed')
        db.close()
        self.assertFalse(self.out.exists())
        proc.send_signal(__import__('signal').SIGKILL)
        out,err=proc.communicate();self.assertNotEqual(proc.returncode,0)
        # Quota was spent exactly once and the archive survived the kill.
        db=sqlite3.connect(str(self.ledger))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM publications').fetchone()[0],1)
        stored=db.execute('SELECT report_bytes FROM publications WHERE request_id=?',
                          ('killed',)).fetchone()[0]
        db.close();self.assertEqual(stored,archived)
        # Retry without the delay hook: replay, no redraw, no second charge.
        r=pub('publish','--ledger',self.ledger,'--request-id','killed','--output',self.out)
        self.assertEqual(r.returncode,0,r.stderr)
        receipt=json.loads(r.stdout)
        self.assertEqual(receipt['status'],'replayed');self.assertEqual(receipt['quota_used'],1)
        self.assertEqual(self.out.read_bytes(),bytes(archived))
        # One quota slot remains usable by a different id.
        r=pub('publish','--ledger',self.ledger,'--request-id','other','--output',self.dir/'o.json')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['quota_used'],2)

    def test_disk_publish_failure_keeps_archive_and_retry_succeeds(self):
        if os.geteuid()==0:self.skipTest('root bypasses directory write permissions')
        self.load_seed();self.init_ledger(quota=1)
        rodir=self.dir/'readonly';rodir.mkdir()
        target=rodir/'out.json';os.chmod(rodir,stat.S_IRUSR|stat.S_IXUSR)
        try:
            r=pub('publish','--ledger',self.ledger,'--request-id','disk','--output',target)
            self.assertEqual(r.returncode,2)
            self.assertFalse(target.exists())
        finally:
            os.chmod(rodir,stat.S_IRWXU)
        # The archive committed before the failed file write; retry replays it, no extra quota.
        r=pub('publish','--ledger',self.ledger,'--request-id','disk','--output',self.out)
        self.assertEqual(r.returncode,0,r.stderr)
        receipt=json.loads(r.stdout)
        self.assertEqual(receipt['status'],'replayed');self.assertEqual(receipt['quota_used'],1)
        # Quota exhausted for genuinely new ids.
        r=pub('publish','--ledger',self.ledger,'--request-id','new','--output',self.dir/'n.json')
        self.assertEqual(r.returncode,2);self.assertIn('quota exhausted',r.stderr)

class SnapshotAndSafety(Base):
    def test_source_load_concurrent_with_publish_never_tears(self):
        self.load_seed();self.init_ledger(quota=20)
        # Keep publishing while another process revises the source; every report must be
        # internally consistent (cells sum to the derived basket count) and carry a real
        # committed watermark, while the source database stays intact.
        watermarks=[]
        for i in range(8):
            batch=self.write_jsonl(f'live{i}.jsonl',
                [{'id':f'live{i}','rev':1,'time':990+i,'items':['bread','tea']}])
            loader=subprocess.Popen([sys.executable,'-m','basket_rules.rollcli','load',
                '--db',self.src,'--watermark',str(1000+i),'--source',batch],
                stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            r=pub('publish','--ledger',self.ledger,'--request-id',f'snap{i}',
                  '--output',self.dir/f'snap{i}.json')
            self.assertEqual(r.returncode,0,r.stderr)
            lo,le=loader.communicate();self.assertEqual(loader.returncode,0,le)
            report=json.loads((self.dir/f'snap{i}.json').read_text())
            self.assertEqual(report['derived']['basket_count'],
                             sum(c['noisy_count'] for c in report['noisy_histogram']))
            self.assertIn(report['source_watermark'],[1000+j for j in range(i+1)])
            watermarks.append(report['source_watermark'])
        # Source remains a healthy rolling db with the final watermark committed.
        db=sqlite3.connect(str(self.src))
        self.assertEqual(db.execute('SELECT watermark FROM meta').fetchone()[0],1007)
        db.close()

    def test_source_database_is_never_modified(self):
        before=self.src.read_bytes()
        self.load_seed();after_load=self.src.read_bytes();self.assertNotEqual(before,after_load)
        self.init_ledger()
        r=pub('publish','--ledger',self.ledger,'--request-id','ro','--output',self.out)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(self.src.read_bytes(),after_load)
        r=pub('publish','--ledger',self.ledger,'--request-id','ro','--output',self.dir/'r2.json')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(self.src.read_bytes(),after_load)

    def test_items_outside_universe_rejected_without_quota_charge(self):
        self.load([{'id':'x','rev':1,'time':950,'items':['bread','ghost']}])
        self.init_ledger(quota=1)
        r=pub('publish','--ledger',self.ledger,'--request-id','bad','--output',self.out)
        self.assertEqual(r.returncode,2);self.assertIn('outside the public universe',r.stderr)
        self.assertFalse(self.out.exists())
        db=sqlite3.connect(str(self.ledger))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM publications').fetchone()[0],0)
        db.close()
        # Nothing was spent: removing the offending basket allows publication.
        self.load([{'id':'x','rev':2,'time':950,'items':None}],name='del.jsonl')
        r=pub('publish','--ledger',self.ledger,'--request-id','good','--output',self.out)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['quota_used'],1)

    def test_output_safety_and_no_overwrite(self):
        self.load_seed();self.init_ledger()
        # Output cannot be the ledger or the source.
        self.assertEqual(pub('publish','--ledger',self.ledger,'--request-id','x',
                             '--output',self.ledger).returncode,2)
        self.assertEqual(pub('publish','--ledger',self.ledger,'--request-id','x',
                             '--output',self.src).returncode,2)
        # Symlink outputs are refused and their targets stay untouched.
        target=self.dir/'target.json';target.write_text('keep');link=self.dir/'link.json'
        link.symlink_to(target)
        self.assertEqual(pub('publish','--ledger',self.ledger,'--request-id','x',
                             '--output',link).returncode,2)
        self.assertEqual(target.read_text(),'keep')
        # Failed publication leaves any pre-existing output in place.
        self.out.write_text('prior')
        self.load([{'id':'x','rev':3,'time':950,'items':['ghost']}],name='oob.jsonl')
        self.assertEqual(pub('publish','--ledger',self.ledger,'--request-id','x',
                             '--output',self.out).returncode,2)
        self.assertEqual(self.out.read_text(),'prior')

class InitValidation(Base):
    def test_init_rejects_bad_arguments(self):
        self.load_seed()
        def call(**kw):
            args=['init','--ledger',kw.get('ledger',self.dir/'x.db'),'--source',self.src,
                  '--universe',kw.get('universe',self.universe_file),'--quota',kw.get('quota',3)]
            return pub(*args)
        self.assertEqual(call(quota=0).returncode,2)
        self.assertEqual(call(quota=-1).returncode,2)
        # Reinitialize is refused.
        self.init_ledger()
        self.assertEqual(pub('init','--ledger',self.ledger,'--source',self.src,
                             '--universe',self.universe_file,'--quota',1).returncode,2)
        # Missing source.
        self.assertEqual(pub('init','--ledger',self.dir/'m.db','--source',self.dir/'nope.db',
                             '--universe',self.universe_file,'--quota',1).returncode,2)
        # Ledger path equal to the source path.
        self.assertEqual(pub('init','--ledger',self.src,'--source',self.src,
                             '--universe',self.universe_file,'--quota',1).returncode,2)
        # Universe bounds and shape.
        big=self.dir/'big.json';big.write_text(json.dumps([f'i{k}' for k in range(13)]))
        self.assertEqual(pub('init','--ledger',self.dir/'b.db','--source',self.src,
                             '--universe',big,'--quota',1).returncode,2)
        empty=self.dir/'empty.json';empty.write_text('[]')
        self.assertEqual(pub('init','--ledger',self.dir/'e.db','--source',self.src,
                             '--universe',empty,'--quota',1).returncode,2)
        dup=self.dir/'dup.json';dup.write_text('["a","a"]')
        self.assertEqual(pub('init','--ledger',self.dir/'d.db','--source',self.src,
                             '--universe',dup,'--quota',1).returncode,2)
        notarray=self.dir/'na.json';notarray.write_text('{"items":["a"]}')
        self.assertEqual(pub('init','--ledger',self.dir/'na.db','--source',self.src,
                             '--universe',notarray,'--quota',1).returncode,2)

    def test_pinned_parameters_drift_is_rejected(self):
        self.load_seed();self.init_ledger()
        r=pub('publish','--ledger',self.ledger,'--request-id','p','--output',self.out)
        self.assertEqual(r.returncode,0,r.stderr)
        # Tampering with the pinned source parameters inside the source db must be detected
        # inside the read-only snapshot transaction.
        db=sqlite3.connect(str(self.src))
        db.execute("UPDATE config SET value='999' WHERE key='window_seconds'");db.commit();db.close()
        r=pub('publish','--ledger',self.ledger,'--request-id','p2','--output',self.dir/'p2.json')
        self.assertEqual(r.returncode,2);self.assertIn('pinned ledger parameters',r.stderr)
        # Old ids still replay without touching the source.
        r=pub('publish','--ledger',self.ledger,'--request-id','p','--output',self.dir/'p-copy.json')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['status'],'replayed')

class NoLeakage(Base):
    def test_report_contains_neither_true_counts_nor_identities(self):
        rows=self.load_seed();self.init_ledger()
        r=pub('publish','--ledger',self.ledger,'--request-id','priv','--output',self.out)
        self.assertEqual(r.returncode,0,r.stderr)
        blob=self.out.read_text()
        for row in rows:
            if row['items'] is not None:
                # Exact quoted token: basket ids must not appear (substring 'a' would match 'bread').
                self.assertNotIn('"%s"'%row['id'],blob)
        report=json.loads(blob)
        # Explicit structural check: only noisy counts leave the system.
        for cell in report['noisy_histogram']:
            self.assertEqual(set(cell),{'items','noisy_count'})
        self.assertEqual(set(report['derived']),{'basket_count','frequent_itemsets','rules'})
        # Rules keep the original exact fraction format from the shared summarizer
        # (Fraction(1) serializes as "1" with no slash; floats are never used).
        from fractions import Fraction
        for rule in report['derived']['rules']:
            for key in ('support','confidence','lift','leverage'):
                self.assertNotIn('.',rule[key])
                self.assertEqual(str(Fraction(rule[key])),rule[key])

if __name__=='__main__':unittest.main()
