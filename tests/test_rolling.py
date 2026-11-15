
import json, os, random, signal, sqlite3, subprocess, sys, tempfile, time, unittest
from collections import Counter
from fractions import Fraction
from itertools import combinations
from pathlib import Path
from basket_rules.mining import summarize

ROOT=Path(__file__).resolve().parents[1]
WINDOW,MINIMUM,MAX_SIZE,NUM,DEN=100,2,3,1,2

def roll(*args):
    return subprocess.run([sys.executable,'-m','basket_rules.rollcli',*[str(a) for a in args]],
                          capture_output=True,text=True)

def reference(batches,window=WINDOW,minimum=MINIMUM,max_size=MAX_SIZE,num=NUM,den=DEN):
    """Step-by-step full recompute: fold every committed batch, then mine the active set from scratch."""
    state={};w=0
    for wm,rows in batches:
        w=wm
        for row in rows:
            items=row['items'];norm=None if items is None else sorted(items)
            current=state.get(row['id'])
            if current and row['rev']<current[0]:continue
            if current and row['rev']==current[0]:
                assert current[1]==row['time'] and current[2]==norm,'conflicting revision committed'
                continue
            state[row['id']]=(row['rev'],row['time'],norm)
    active=[items for _,ts,items in state.values() if items is not None and w-window<ts<=w]
    counts=Counter()
    for items in active:
        for size in range(1,min(max_size,len(items))+1):counts.update(combinations(sorted(items),size))
    if active:frequent,rules=summarize(counts,len(active),minimum,max_size,Fraction(num,den))
    else:frequent,rules=[],[]
    return w,{'basket_count':len(active),'item_universe':sorted({i for items in active for i in items}),
              'frequent_itemsets':frequent,'rules':rules}

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.dir=Path(self.tmp.name)
        self.db=self.dir/'roll.db';self.out=self.dir/'report.json'
        r=roll('init','--db',self.db,'--window-seconds',WINDOW,'--min-support-count',MINIMUM,
               '--max-itemset-size',MAX_SIZE,'--min-confidence',NUM,DEN)
        self.assertEqual(r.returncode,0,r.stderr)
    def tearDown(self):self.tmp.cleanup()
    def write(self,name,rows):
        path=self.dir/name
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        return path
    def load(self,path,w):return roll('load','--db',self.db,'--watermark',w,'--source',path)
    def check(self,batches):
        w,expected=reference(batches)
        r=roll('report','--db',self.db,'--output',self.out)
        self.assertEqual(r.returncode,0,r.stderr)
        report=json.loads(self.out.read_text())
        self.assertEqual(json.loads(r.stdout),report)
        self.assertEqual(report['watermark'],w)
        self.assertEqual(report['parameters'],{'window_seconds':WINDOW,'min_support_count':MINIMUM,
            'max_itemset_size':MAX_SIZE,'min_confidence':{'numerator':NUM,'denominator':DEN}})
        for key in ('basket_count','item_universe','frequent_itemsets','rules'):
            self.assertEqual(report['result'][key],expected[key],key)
        self.assertIn('not causation',report['result']['scope'])
        return report
    def invariants(self):
        db=sqlite3.connect(str(self.db))
        (w,active),=db.execute('SELECT watermark,active_count FROM meta')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM counts WHERE count<=0').fetchone()[0],0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM baskets WHERE active=1').fetchone()[0],active)
        window,=db.execute("SELECT value FROM config WHERE key='window_seconds'").fetchone()
        self.assertEqual(db.execute('SELECT COUNT(*) FROM baskets WHERE active=1 AND (items IS NULL OR time<=? OR time>?)',
                                    (w-int(window),w)).fetchone()[0],0)
        db.close()

class Lifecycle(Base):
    def test_scripted_revision_expiry_restore(self):
        committed=[]
        b1=[{'id':'a','rev':1,'time':950,'items':['x','y']},
            {'id':'b','rev':1,'time':999,'items':['y','x']},
            {'id':'c','rev':1,'time':1000,'items':['x']},
            {'id':'d','rev':1,'time':951,'items':[]},
            {'id':'e','rev':1,'time':900,'items':['x','y']},
            {'id':'f','rev':2,'time':980,'items':['z']},
            {'id':'f','rev':1,'time':970,'items':['x']}]
        r=self.load(self.write('b1.jsonl',b1),1000)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout),{'active_count':5,'applied':6,'expired':0,'idempotent':0,'ignored':1,'watermark':1000})
        committed.append((1000,b1));report=self.check(committed);self.invariants()
        rule={tuple(x['antecedent']):x for x in report['result']['rules']}[('x',)]
        self.assertEqual((rule['consequent'],rule['confidence'],rule['lift'],rule['leverage']),(['y'],'2/3','5/3','4/25'))

        b2=[{'id':'b','rev':2,'time':1040,'items':['y','z']},
            {'id':'c','rev':2,'time':1000,'items':None},
            {'id':'g','rev':1,'time':1049,'items':['x','y','z']},
            {'id':'f','rev':2,'time':980,'items':['z']}]
        r=self.load(self.write('b2.jsonl',b2),1050)
        self.assertEqual(json.loads(r.stdout),{'active_count':4,'applied':3,'expired':1,'idempotent':1,'ignored':0,'watermark':1050})
        committed.append((1050,b2));self.check(committed);self.invariants()

        b3=[{'id':'c','rev':3,'time':1045,'items':['y','x']},
            {'id':'a','rev':1,'time':950,'items':['x','y']}]
        r=self.load(self.write('b3.jsonl',b3),1050)
        self.assertEqual(json.loads(r.stdout)['active_count'],5)
        committed.append((1050,b3));self.check(committed);self.invariants()

        before_db=self.db.read_bytes();before_report=self.out.read_bytes()
        conflict=self.write('conflict.jsonl',[{'id':'b','rev':2,'time':1040,'items':['y','z','x']}])
        self.assertEqual(self.load(conflict,1050).returncode,2)
        future=self.write('future.jsonl',[{'id':'h','rev':1,'time':2000,'items':['x']}])
        self.assertEqual(self.load(future,1050).returncode,2)
        self.assertEqual(self.load(self.write('replay.jsonl',b3),1040).returncode,2)
        self.assertEqual(self.db.read_bytes(),before_db)
        self.assertEqual(self.out.read_bytes(),before_report)
        self.check(committed)

        replay=self.load(self.write('replay2.jsonl',b1+b2+b3),1050)
        self.assertEqual(json.loads(replay.stdout),{'active_count':5,'applied':0,'expired':0,'idempotent':9,'ignored':4,'watermark':1050})
        self.check(committed);self.invariants()

        b4=[{'id':'z','rev':1,'time':1150,'items':['q']}]
        self.assertEqual(self.load(self.write('b4.jsonl',b4),1200).returncode,0)
        committed.append((1200,b4));report=self.check(committed);self.invariants()
        self.assertEqual(report['result']['basket_count'],1)
        self.assertEqual(report['result']['frequent_itemsets'],[]);self.assertEqual(report['result']['rules'],[])

        b5=[{'id':'a','rev':2,'time':1199,'items':['x','y']},{'id':'h','rev':1,'time':1200,'items':[]}]
        self.assertEqual(self.load(self.write('b5.jsonl',b5),1200).returncode,0)
        committed.append((1200,b5));self.check(committed);self.invariants()

    def test_empty_report(self):
        report=self.check([])
        self.assertEqual(report['watermark'],0)
        self.assertEqual(report['result']['basket_count'],0)
        self.assertEqual(report['result']['item_universe'],[])
        self.assertEqual(report['result']['frequent_itemsets'],[]);self.assertEqual(report['result']['rules'],[])

class Randomized(Base):
    def test_differential_vs_full_recompute(self):
        rng=random.Random(20261115)
        universe=[f'item{i}' for i in range(8)]
        state={};committed=[];w=900
        for step in range(30):
            w+=rng.randint(0,25);rows=[]
            for _ in range(rng.randint(1,20)):
                bid=f'b{rng.randrange(40)}';current=state.get(bid);kind=rng.random()
                if current and kind<0.10:
                    rev,ts,items=current
                    rows.append({'id':bid,'rev':rev,'time':ts,'items':None if items is None else list(reversed(items))})
                    continue
                if current and kind<0.20 and current[0]>1:
                    rows.append({'id':bid,'rev':rng.randint(1,current[0]-1),'time':rng.randint(0,w),'items':['item0']})
                    continue
                rev=current[0]+1 if current else 1
                ts=w-rng.randint(0,WINDOW+30)
                roll_items=rng.random()
                if roll_items<0.15:items=None
                elif roll_items<0.25:items=[]
                else:
                    items=rng.sample(universe,rng.randint(1,5));rng.shuffle(items)
                rows.append({'id':bid,'rev':rev,'time':ts,'items':items})
                state[bid]=(rev,ts,None if items is None else sorted(items))
            r=self.load(self.write(f'rand{step}.jsonl',rows),w)
            self.assertEqual(r.returncode,0,r.stderr)
            committed.append((w,rows))
            if step%7==3:
                victim=rng.choice(list(state))
                bad=[{'id':victim,'rev':state[victim][0],'time':w,'items':['item9','item8']}]
                self.assertEqual(self.load(self.write(f'conflict{step}.jsonl',bad),w).returncode,2)
            self.check(committed);self.invariants()

class Robustness(Base):
    def bad_rows(self):
        return [['not json'],['{"id":"a","id":"b","rev":1,"time":1,"items":null}'],
            ['{"id":"a","rev":1,"time":1}'],[json.dumps({'id':'a','rev':1,'time':1,'items':None,'extra':1})],
            [json.dumps({'id':' ','rev':1,'time':1,'items':None})],[json.dumps({'id':'a','rev':0,'time':1,'items':None})],
            [json.dumps({'id':'a','rev':-3,'time':1,'items':None})],[json.dumps({'id':'a','rev':'1','time':1,'items':None})],
            [json.dumps({'id':'a','rev':1.5,'time':1,'items':None})],[json.dumps({'id':'a','rev':1,'time':-1,'items':None})],
            [json.dumps({'id':'a','rev':1,'time':'5','items':None})],[json.dumps({'id':'a','rev':1,'time':1,'items':'x'})],
            [json.dumps({'id':'a','rev':1,'time':1,'items':['x','x']})],
            [json.dumps({'id':'a','rev':1,'time':1,'items':[f'i{k}' for k in range(13)]})],
            [json.dumps({'id':'a','rev':1,'time':1,'items':['']})],
            [json.dumps({'id':'a','rev':1,'time':1,'items':None}),''],
            ['{"id":"a","rev":1,"time":NaN,"items":null}']]
    def test_bad_rows_leave_no_partial_batch(self):
        good=self.write('good.jsonl',[{'id':'a','rev':1,'time':10,'items':['x']}])
        self.assertEqual(self.load(good,100).returncode,0)
        before=self.db.read_bytes()
        for i,lines in enumerate(self.bad_rows()):
            path=self.dir/f'bad{i}.jsonl';path.write_text('\n'.join(lines)+'\n')
            self.assertEqual(self.load(path,100).returncode,2,lines)
        mixed=self.dir/'mixed.jsonl'
        mixed.write_text('{"id":"b","rev":1,"time":20,"items":["y"]}\n{"id":"c","rev":0,"time":20,"items":null}\n')
        self.assertEqual(self.load(mixed,200).returncode,2)
        self.assertEqual(self.db.read_bytes(),before)
        self.assertEqual(self.check([(100,[{'id':'a','rev':1,'time':10,'items':['x']}])])['result']['basket_count'],1)
    def test_init_validation(self):
        for extra in [('--window-seconds',0),('--min-support-count',0),('--max-itemset-size',4),('--max-itemset-size',0)]:
            args=['init','--db',self.dir/'x.db','--window-seconds',100,'--min-support-count',2,
                  '--max-itemset-size',3,'--min-confidence',1,2]
            args[args.index(extra[0])+1]=extra[1]
            self.assertEqual(roll(*args).returncode,2,extra)
        args=['init','--db',self.dir/'x.db','--window-seconds',100,'--min-support-count',2,
              '--max-itemset-size',3,'--min-confidence',3,2]
        self.assertEqual(roll(*args).returncode,2)
        self.assertEqual(roll('init','--db',self.db,'--window-seconds',100,'--min-support-count',2,
                              '--max-itemset-size',3,'--min-confidence',1,2).returncode,2)
        missing=self.dir/'missing.db'
        self.assertEqual(roll('report','--db',missing,'--output',self.out).returncode,2)
        self.assertFalse(missing.exists())
        self.assertEqual(roll('load','--db',missing,'--watermark',1,'--source',self.write('g.jsonl',[])).returncode,2)
        self.assertFalse(missing.exists())
    def test_report_output_safety(self):
        self.assertEqual(roll('report','--db',self.db,'--output',self.db).returncode,2)
        link=self.dir/'link.json';link.symlink_to(self.out)
        self.assertEqual(roll('report','--db',self.db,'--output',link).returncode,2)
        self.assertFalse(self.out.exists())
    def test_active_cap(self):
        rows=[{'id':f'k{i}','rev':1,'time':5,'items':[]} for i in range(100000)]
        r=self.load(self.write('cap.jsonl',rows),10)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['active_count'],100000)
        over=self.write('over.jsonl',[{'id':'overflow','rev':1,'time':5,'items':[]}])
        self.assertEqual(self.load(over,10).returncode,2)
        swap=self.write('swap.jsonl',[{'id':'k0','rev':2,'time':5,'items':None},
                                      {'id':'overflow','rev':1,'time':5,'items':[]}])
        r=self.load(swap,10)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['active_count'],100000)
        self.invariants()

class Abort(Base):
    def dump(self):
        db=sqlite3.connect(str(self.db))
        state=(db.execute('SELECT watermark,active_count FROM meta').fetchall(),
               db.execute('SELECT id,rev,time,items,active FROM baskets ORDER BY id').fetchall(),
               db.execute('SELECT combo,size,count FROM counts ORDER BY combo').fetchall())
        db.close();return state
    def test_kill_mid_batch_leaves_no_partial_and_retry_counts_once(self):
        base=[{'id':'seed','rev':1,'time':50,'items':['a','b']}]
        self.assertEqual(self.load(self.write('seed.jsonl',base),100).returncode,0)
        committed=[(100,base)];baseline=self.dump()
        rng=random.Random(7);universe=[f'u{i}' for i in range(60)]
        big=[{'id':f'n{i}','rev':1,'time':150,'items':rng.sample(universe,6)} for i in range(30000)]
        source=self.write('big.jsonl',big)
        proc=subprocess.Popen([sys.executable,'-m','basket_rules.rollcli','load','--db',self.db,
                               '--watermark','200','--source',source],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        wal=Path(str(self.db)+'-wal');deadline=time.monotonic()+60;killed=False
        while proc.poll() is None and time.monotonic()<deadline:
            if wal.exists() and wal.stat().st_size>65536:break
            time.sleep(0.02)
        if proc.poll() is None:proc.send_signal(signal.SIGKILL);killed=True
        proc.communicate()
        self.assertNotEqual(proc.returncode,0)
        self.assertTrue(killed,'batch finished before the kill landed; rerun')
        self.assertEqual(self.dump(),baseline)
        self.check(committed)
        r=self.load(source,200)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['applied'],30000)
        committed.append((200,big));self.check(committed);self.invariants()
        again=self.load(source,200)
        self.assertEqual(json.loads(again.stdout)['idempotent'],30000)
        self.check(committed)

class IndexStrategy(Base):
    def test_expiry_and_revision_plans_use_indexes(self):
        db=sqlite3.connect(str(self.db))
        plan=str(db.execute('EXPLAIN QUERY PLAN SELECT id,items FROM baskets WHERE active=1 AND time<=?',(0,)).fetchall())
        self.assertIn('idx_baskets_expiry',plan)
        plan=str(db.execute('EXPLAIN QUERY PLAN SELECT rev,time,items,active FROM baskets WHERE id=?',('x',)).fetchall())
        self.assertIn('search baskets using index',plan.lower())
        db.close()

if __name__=='__main__':unittest.main()
