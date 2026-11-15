from collections import Counter
from itertools import combinations
from fractions import Fraction

def summarize(counts,n,minimum,max_size,min_confidence):
    frequent=[];rules=[]
    for items,count in sorted(counts.items(),key=lambda x:(len(x[0]),x[0])):
        if count<minimum:continue
        frequent.append({'items':list(items),'count':count,'support':str(Fraction(count,n))})
        if len(items)<2:continue
        for size in range(1,len(items)):
            for lhs in combinations(items,size):
                rhs=tuple(i for i in items if i not in lhs);confidence=Fraction(count,counts[lhs])
                if confidence<min_confidence:continue
                support_rhs=Fraction(counts[rhs],n);lift=confidence/support_rhs
                rules.append({'antecedent':list(lhs),'consequent':list(rhs),'joint_count':count,
                              'support':str(Fraction(count,n)),'confidence':str(confidence),'lift':str(lift),
                              'leverage':str(Fraction(count,n)-Fraction(counts[lhs],n)*support_rhs)})
    return frequent,sorted(rules,key=lambda r:(r['antecedent'],r['consequent']))

def mine(baskets,minimum,max_size,min_confidence):
    counts=Counter()
    for basket in baskets:
        for size in range(1,min(max_size,len(basket))+1):counts.update(combinations(sorted(basket),size))
    return summarize(counts,len(baskets),minimum,max_size,min_confidence)
