
import copy,json,unittest,tempfile,subprocess,sys,hashlib
from pathlib import Path
from basket_rules.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_top_field(self):
        d=sample();d['unexpected']=1;self.bad(d)

    def test_rule(self):
        r=analyze(sample());rule=r['rules'][0];self.assertEqual(rule['antecedent'],['bread']);self.assertEqual((rule['support'],rule['confidence'],rule['lift'],rule['leverage']),('1/2','2/3','8/9','-1/16'))
    def test_order(self):
        d=sample();d['baskets'].reverse()
        for b in d['baskets']:b['items'].reverse()
        self.assertEqual(analyze(d),analyze(sample()))
    def test_empty_basket_denominator(self):
        d=sample();d['baskets'].append({'id':'empty','items':[]});r=analyze(d);self.assertEqual(r['rules'][0]['support'],'2/5')
    def test_confidence_filter(self):
        d=sample();d['min_confidence']={'numerator':1,'denominator':1};self.assertEqual(analyze(d)['rules'],[])
    def test_singletons(self):
        d=sample();d['max_itemset_size']=1;self.assertEqual(analyze(d)['rules'],[])
    def test_duplicate_item(self):
        d=sample();d['baskets'][0]['items'].append('bread');self.bad(d)
    def test_duplicate_basket(self):
        d=sample();d['baskets'].append(d['baskets'][0]);self.bad(d)
    def test_three_item_partition(self):
        d=sample();d['min_support_count']=1;d['min_confidence']={'numerator':0,'denominator':1};d['baskets']=[{'id':'a','items':['a','b','c']}];r=analyze(d);self.assertEqual(len(r['frequent_itemsets']),7);self.assertEqual(len(r['rules']),12)

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','basket_rules.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_installed_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();report=json.loads(first);self.assertEqual(report['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"x":1,"x":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
