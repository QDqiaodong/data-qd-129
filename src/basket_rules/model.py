from fractions import Fraction
from .common import require,ident,number,rows
from .mining import mine

MAX_BASKETS=1000
MAX_ITEMS=12

def parse_snapshot(data):
    """Validate the snapshot contract shared by mining and alias normalization.

    Returns (minimum, maximum, numerator, denominator, [(basket_id, items), ...]).
    Structural rules only: field sets, integer bounds, identifier shape, unique
    basket IDs and no repeated item within one basket. Universe size is a
    caller concern because normalization can fold items first.
    """
    require(data,'min_support_count max_itemset_size min_confidence baskets');minimum=number(data['min_support_count'],1,1000)
    maximum=number(data['max_itemset_size'],1,3);c=data['min_confidence'];require(c,'numerator denominator')
    numerator=number(c['numerator'],0);denominator=number(c['denominator'],1)
    if numerator>denominator:raise ValueError('confidence must be <=1')
    baskets=[];seen=set()
    for b in rows(data['baskets'],MAX_BASKETS):
        require(b,'id items');bid=ident(b['id']);items=[ident(i) for i in rows(b['items'],MAX_ITEMS)]
        if bid in seen or len(items)!=len(set(items)):raise ValueError('duplicate basket ID or repeated item within basket')
        seen.add(bid);baskets.append((bid,items))
    return minimum,maximum,numerator,denominator,baskets

def analyze(data):
    minimum,maximum,numerator,denominator,baskets=parse_snapshot(data)
    universe=set()
    for _,items in baskets:universe.update(items)
    if not baskets or len(universe)>MAX_ITEMS:raise ValueError('requires 1..1000 baskets and <=12 unique items')
    frequent,rules=mine([items for _,items in baskets],minimum,maximum,Fraction(numerator,denominator))
    return {'basket_count':len(baskets),'item_universe':sorted(universe),'frequent_itemsets':frequent,'rules':rules,
            'scope':'Exact bounded basket co-occurrence; association is not causation.'}
