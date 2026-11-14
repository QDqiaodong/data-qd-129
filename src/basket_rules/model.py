from fractions import Fraction
from .common import require,ident,number,rows
from .mining import mine

def analyze(data):
    require(data,'min_support_count max_itemset_size min_confidence baskets');minimum=number(data['min_support_count'],1,1000)
    maximum=number(data['max_itemset_size'],1,3);c=data['min_confidence'];require(c,'numerator denominator')
    numerator=number(c['numerator'],0);denominator=number(c['denominator'],1)
    if numerator>denominator:raise ValueError('confidence must be <=1')
    baskets=[];seen=set();universe=set()
    for b in rows(data['baskets'],1000):
        require(b,'id items');bid=ident(b['id']);items=[ident(i) for i in rows(b['items'],12)]
        if bid in seen or len(items)!=len(set(items)):raise ValueError('duplicate basket ID or repeated item within basket')
        seen.add(bid);universe.update(items);baskets.append(items)
    if not baskets or len(universe)>12:raise ValueError('requires 1..1000 baskets and <=12 unique items')
    frequent,rules=mine(baskets,minimum,maximum,Fraction(numerator,denominator))
    return {'basket_count':len(baskets),'item_universe':sorted(universe),'frequent_itemsets':frequent,'rules':rules,
            'scope':'Exact bounded basket co-occurrence; association is not causation.'}
