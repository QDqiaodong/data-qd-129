import hashlib, json, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
from basket_rules import normalize as N
from basket_rules.model import analyze

ROOT=Path(__file__).resolve().parents[1]

def norm(*args):
    return subprocess.run([sys.executable,'-m','basket_rules.normcli',*[str(a) for a in args]],
                          capture_output=True,text=True)

def mine(*args):
    return subprocess.run([sys.executable,'-m','basket_rules.cli',*[str(a) for a in args]],
                          capture_output=True,text=True)

def snapshot(baskets,minimum=1,max_size=3,num=0,den=1):
    return {'min_support_count':minimum,'max_itemset_size':max_size,
            'min_confidence':{'numerator':num,'denominator':den},'baskets':baskets}

def alias(alias,target,start,end=None):
    return {'alias':alias,'target':target,'valid_from':start,'valid_to':end}

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.dir=Path(self.tmp.name)
        self.source=self.dir/'input.json';self.canon=self.dir/'canonical.json'
        self.aliases=self.dir/'aliases.jsonl';self.out=self.dir/'normalized.json'
    def tearDown(self):self.tmp.cleanup()
    def build(self,baskets,canonical,records,minimum=1,max_size=3,num=0,den=1):
        self.source.write_text(json.dumps(snapshot(baskets,minimum,max_size,num,den),ensure_ascii=False))
        self.write_canonical(canonical)
        self.write_aliases(records)
    def write_canonical(self,canonical):
        self.canon.write_text(json.dumps(canonical,ensure_ascii=False))
    def write_aliases(self,records):
        self.aliases.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records))
    def run_norm(self,as_of,source=None,canonical=None,aliases=None,output=None):
        return norm('--source',source or self.source,'--canonical',canonical or self.canon,
                    '--aliases',aliases or self.aliases,'--as-of',as_of,'--output',output or self.out)
    def receipt(self):
        return json.loads(self.out.read_text())

class DateScoped(Base):
    def setUp(self):
        super().setUp()
        self.build([{'id':'b1','items':['ap','pear']},{'id':'b2','items':[]}],
                   ['apple','pear'],
                   [alias('ap','apple','2026-01-01','2026-07-01'),
                    alias('ap','pear','2026-07-01',None)])

    def test_left_closed_right_open_switch(self):
        for day,expected in [('2026-01-01','apple'),('2026-06-30','apple'),('2026-07-01','pear'),
                             ('2027-12-31','pear')]:
            r=self.run_norm(day)
            self.assertEqual(r.returncode,0,r.stderr)
            receipt=json.loads(r.stdout)
            self.assertEqual(receipt['as_of'],day)
            self.assertEqual(receipt['basket_count'],2)
            self.assertEqual(receipt['effective_aliases'],1)
            data=self.receipt()
            # On/after 2026-07-01 the alias folds onto the basket's existing 'pear'.
            expected_items=[expected] if expected=='pear' else [expected,'pear']
            self.assertEqual(data['baskets'],[{'id':'b1','items':expected_items},{'id':'b2','items':[]}])
            self.assertEqual(set(data),{'min_support_count','max_itemset_size','min_confidence','baskets'})

    def test_before_first_validity_is_not_effective(self):
        r=self.run_norm('2025-12-31')
        self.assertEqual(r.returncode,2)
        self.assertIn('neither a canonical SKU nor an effective alias',r.stderr)
        self.assertFalse(self.out.exists())

    def test_repeat_is_byte_identical_and_sha_matches(self):
        self.assertEqual(self.run_norm('2026-03-01').returncode,0)
        first=self.out.read_bytes();receipt=json.loads(self.run_norm('2026-03-01').stdout)
        self.assertEqual(first,self.out.read_bytes())
        for field,path in (('source_sha256',self.source),('canonical_sha256',self.canon),
                           ('aliases_sha256',self.aliases)):
            self.assertEqual(receipt[field],hashlib.sha256(path.read_bytes()).hexdigest(),field)
        self.assertEqual(receipt['resolved_items'],1);self.assertEqual(receipt['folded_items'],0)

class ChainResolution(Base):
    def test_long_chain_never_recurses(self):
        depth=60000
        records=[alias(f'a{k}',f'a{k+1}','2020-01-01',None) for k in range(depth)]
        records.append(alias(f'a{depth}','sku','2020-01-01',None))
        self.build([{'id':'deep','items':['a0']}],['sku'],records)
        r=self.run_norm('2026-01-01')
        self.assertEqual(r.returncode,0,r.stderr)
        receipt=json.loads(r.stdout)
        self.assertEqual(receipt['resolved_items'],1)
        self.assertEqual(self.receipt()['baskets'],[{'id':'deep','items':['sku']}])

    def test_branches_converge_and_fold(self):
        records=[alias('left','mid','2026-01-01',None),alias('right','mid','2026-01-01',None),
                 alias('mid','final','2026-01-01',None),alias('final','sku','2026-01-01',None)]
        self.build([{'id':'b','items':['left','right','sku']}],['sku'],records)
        r=self.run_norm('2026-01-01')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(self.receipt()['baskets'][0]['items'],['sku'])
        self.assertEqual(json.loads(r.stdout)['folded_items'],2)

class Failures(Base):
    def setUp(self):
        super().setUp()
        self.build([{'id':'b','items':['a','sku']}],['sku'],[])
        self.out.write_text('prior report')
        self.before=self.out.read_bytes()
    def expect_failure(self,records,as_of='2026-01-01',message=None):
        self.write_aliases(records)
        r=self.run_norm(as_of)
        self.assertEqual(r.returncode,2,r.stdout)
        if message:self.assertIn(message,r.stderr)
        self.assertEqual(self.out.read_bytes(),self.before)
        return r

    def test_overlap_fails_but_other_days_scope_out(self):
        records=[alias('a','sku','2026-01-01','2026-03-01'),
                 alias('a','other','2026-02-01','2026-05-01')]
        self.expect_failure(records,'2026-02-15',message='overlapping validity')
        self.expect_failure(records,'2026-04-01',message='dangling')

    def test_cycle_fails(self):
        self.expect_failure([alias('a','b','2026-01-01',None),alias('b','a','2026-01-01',None)],message='cycle')
        self.expect_failure([alias('a','a','2026-01-01',None)],message='cycle')

    def test_cycle_unreachable_from_any_basket_still_fails(self):
        # The contract constrains the whole effective graph, not just chains a basket touches.
        self.expect_failure([alias('a','sku','2026-01-01',None),
                             alias('q','r','2026-01-01',None),alias('r','q','2026-01-01',None)],
                            message='cycle')

    def test_structural_error_outside_as_of_still_fails(self):
        # Empty intervals are file-level defects; they fail even when the record is not in force.
        self.expect_failure([alias('a','sku','2026-01-01',None),
                             alias('z','sku','2025-01-01','2025-01-01')],message='valid_to must be after')

    def test_dangling_fails(self):
        self.expect_failure([alias('a','ghost','2026-01-01',None)],message='dangling')

    def test_alias_cannot_shadow_canonical_sku(self):
        self.expect_failure([alias('sku','a','2026-01-01',None)],message='canonical SKU')
        # The same shadowing record outside its validity is scoped out; the basket
        # then fails only because its alias item has no effective mapping.
        self.expect_failure([alias('sku','a','2026-01-01',None)],'2025-01-01',
                            message='neither a canonical SKU')

    def test_basket_item_outside_graph_fails(self):
        self.expect_failure([],message='neither a canonical SKU nor an effective alias')

class Robustness(Base):
    def test_bad_lines_fail_whole_run(self):
        self.build([{'id':'b','items':['sku']}],['sku'],[])
        bad=[[],[''],['not json'],
             ['{"alias":"a","alias":"b","target":"sku","valid_from":"2026-01-01","valid_to":null}'],
             [json.dumps({'alias':'a','target':'sku','valid_from':'2026-01-01'})],
             [json.dumps({'alias':'a','target':'sku','valid_from':'2026-01-01','valid_to':None,'x':1})],
             [json.dumps({'alias':' ','target':'sku','valid_from':'2026-01-01','valid_to':None})],
             [json.dumps({'alias':'a','target':'sku','valid_from':'2026-1-1','valid_to':None})],
             [json.dumps({'alias':'a','target':'sku','valid_from':'2026-02-30','valid_to':None})],
             [json.dumps({'alias':'a','target':'sku','valid_from':'2026-01-02','valid_to':'2026-01-02'})],
             [json.dumps({'alias':'a','target':'sku','valid_from':None,'valid_to':None})],
             ['{"alias":"a","target":"sku","valid_from":"2026-01-01","valid_to":NaN}'],
             [json.dumps({'alias':'a','target':'sku','valid_from':'2026-01-01','valid_to':None}),
              json.dumps({'alias':'b','target':'sku','valid_from':'2026-01-02','valid_to':'2026-01-01'})]]
        for lines in bad:
            self.aliases.write_text('\n'.join(lines)+'\n')
            r=self.run_norm('2026-01-01')
            self.assertEqual(r.returncode,2,lines)
        self.write_aliases([alias('a','sku','2026-01-01',None)])
        for day in ['2026/01/01','2026-1-1','20260101','2026-02-30','0000-01-01','']:
            r=self.run_norm(day)
            self.assertEqual(r.returncode,2,day)

    def test_file_limits_are_enforced(self):
        self.write_aliases([alias('a','sku','2026-01-01',None),alias('b','sku','2026-01-01',None)])
        N.parse_aliases(self.aliases,N.date(2026,1,1),['sku'])
        with mock.patch.object(N,'MAX_ALIAS_LINES',1):
            with self.assertRaisesRegex(ValueError,'500000 lines'):N.parse_aliases(self.aliases,N.date(2026,1,1),['sku'])
        with mock.patch.object(N,'MAX_ALIAS_BYTES',10):
            with self.assertRaisesRegex(ValueError,'64 MB'):N.parse_aliases(self.aliases,N.date(2026,1,1),['sku'])
        self.assertLessEqual(N.MAX_ALIAS_BYTES,64000000)
        self.assertLessEqual(N.MAX_ALIAS_LINES,500000)

    def test_output_safety(self):
        self.build([{'id':'b','items':['sku']}],['sku'],[])
        self.out.write_text('prior');self.before=self.out.read_bytes()
        self.assertEqual(self.run_norm('2026-01-01',output=self.source).returncode,2)
        self.assertEqual(self.source.read_text().startswith('{'),True)
        link=self.dir/'link.json';link.symlink_to(self.out)
        self.assertEqual(self.run_norm('2026-01-01',output=link).returncode,2)
        self.assertEqual(self.out.read_bytes(),self.before)
        self.assertEqual(self.run_norm('2026-01-01',canonical=self.dir/'missing.json').returncode,2)
        self.assertEqual(self.out.read_bytes(),self.before)

class Folding(Base):
    def test_fold_duplicate_sku_and_preserve_identity(self):
        canonical=['apple','milk','pear']
        records=[alias('ap1','apple','2026-01-01',None),
                 alias('ap2','ap1','2026-01-01',None),
                 alias('pear_alias','pear','2026-01-01',None)]
        baskets=[{'id':'z','items':['apple','ap1']},{'id':'a','items':['ap2','pear_alias','milk']},
                 {'id':'empty','items':[]}]
        self.build(baskets,canonical,records,minimum=2,max_size=3,num=1,den=2)
        r=self.run_norm('2026-06-01')
        self.assertEqual(r.returncode,0,r.stderr)
        receipt=json.loads(r.stdout)
        self.assertEqual(receipt['resolved_items'],3);self.assertEqual(receipt['folded_items'],1)
        self.assertEqual((receipt['items_before'],receipt['items_after']),(5,4))
        data=self.receipt()
        self.assertEqual(data['min_support_count'],2);self.assertEqual(data['max_itemset_size'],3)
        self.assertEqual(data['min_confidence'],{'numerator':1,'denominator':2})
        self.assertEqual(data['baskets'],
            [{'id':'z','items':['apple']},{'id':'a','items':['apple','milk','pear']},{'id':'empty','items':[]}])

    def test_old_mining_command_consumes_output(self):
        canonical=['apple','bread','milk']
        records=[alias('ap1','apple','2026-01-01',None),alias('ap2','ap1','2026-01-01',None),
                 alias('m','milk','2026-01-01',None)]
        baskets=[{'id':'0','items':['ap1','m']},{'id':'1','items':['apple','milk']},
                 {'id':'2','items':['ap2','bread']},{'id':'3','items':[]}]
        self.build(baskets,canonical,records,minimum=2,max_size=3,num=1,den=2)
        self.assertEqual(self.run_norm('2026-06-01').returncode,0)
        report=self.dir/'report.json'
        mining=mine('--source',self.out,'--output',report)
        self.assertEqual(mining.returncode,0,mining.stderr)
        normalized=self.receipt()
        self.assertEqual(analyze(normalized),json.loads(report.read_text())['result'])
        self.assertEqual(normalized['baskets'][2]['items'],['apple','bread'])
        self.assertEqual(analyze(normalized)['rules'][0]['antecedent'],['apple'])

class ModelChecks(Base):
    def test_normalized_snapshot_matches_direct_mining(self):
        canonical=['a','b','c']
        records=[alias('a1','a','2026-01-01',None),alias('b1','b','2026-01-01',None)]
        baskets=[{'id':'1','items':['a1','b1']},{'id':'2','items':['a1','b1']},{'id':'3','items':['a1','c']}]
        self.build(baskets,canonical,records,minimum=2,max_size=2,num=1,den=2)
        self.assertEqual(self.run_norm('2026-01-01').returncode,0)
        direct=snapshot([{'id':'1','items':['a','b']},{'id':'2','items':['a','b']},
                         {'id':'3','items':['a','c']}],2,2,1,2)
        self.assertEqual(analyze(self.receipt()),analyze(direct))

    def test_universe_cap_after_normalization(self):
        canonical=[f'c{k}' for k in range(13)]
        self.build([{'id':'b1','items':['c0','c1','c2','c3','c4','c5','c6','c7','c8','c9','c10','c11']},
                    {'id':'b2','items':['c12']}],canonical,[])
        r=self.run_norm('2026-01-01')
        self.assertEqual(r.returncode,2)
        self.assertIn('universe exceeds 12',r.stderr)

    def test_source_shape_validation_reuses_snapshot_contract(self):
        self.build([{'id':'b','items':['sku']}],['sku'],[])
        self.assertEqual(self.run_norm('2026-01-01').returncode,0)
        for mutate in [lambda d:d.update(unexpected=1),
                       lambda d:d['min_confidence'].update(numerator=2),
                       lambda d:d['baskets'][0].update(extra=1),
                       lambda d:d['baskets'][0]['items'].append('sku')]:
            good=json.loads(self.source.read_text());mutate(good)
            self.source.write_text(json.dumps(good))
            self.assertEqual(self.run_norm('2026-01-01').returncode,2)

if __name__=='__main__':unittest.main()
