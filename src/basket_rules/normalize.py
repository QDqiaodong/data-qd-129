"""Date-scoped alias normalization onto canonical SKUs.

The fourth delivery chain: rewrite the item identifiers of an existing basket
snapshot for one as-of date, producing a snapshot of exactly the same structure
that ``basket-rules --source`` already consumes. Mapping records are
``{alias,target,valid_from,valid_to}`` with left-closed/right-open validity
(``valid_to = null`` means unbounded). Only the mappings effective on the as-of
date form the graph:

* every alias has exactly one outgoing edge (two effective records for one alias
  are overlapping validity and fail the whole run),
* an alias may not shadow a canonical SKU,
* every chain must terminate at a canonical SKU (dangling targets and cycles fail).

Resolution is iterative and memoized, so a long chain never depends on Python's
recursion limit. Basket IDs, empty baskets and the mining parameters are
preserved, items are sorted, and several identifiers folding onto the same SKU
within one basket keep a single item. The output is published by atomic replace,
so a failed run leaves the previous output byte-for-byte intact.

See docs/normalize.md for the contract, complexity and failure boundaries.
"""
from datetime import date
import hashlib, re
from pathlib import Path
from .common import ident, read, require, save, strict_loads
from .model import MAX_BASKETS, MAX_ITEMS, parse_snapshot

# Canonical coverage may exceed the 12-item basket universe, so the file gets the
# same generous ceiling as the alias log. Both are read once into memory.
MAX_CANONICAL_BYTES=64000000
MAX_CANONICAL_SKUS=500000
MAX_ALIAS_BYTES=64000000
MAX_ALIAS_LINES=500000
MAX_ALIAS_LINE_BYTES=1000000
_DAY=re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}\Z')

def parse_day(value,label):
    """Strict YYYY-MM-DD calendar date; no other ISO spellings and no times."""
    if not isinstance(value,str) or _DAY.fullmatch(value) is None:
        raise ValueError('%s must be a YYYY-MM-DD date'%label)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError('%s is not a valid calendar date: %r'%(label,value)) from None

def _read_raw(path,limit,label):
    with Path(path).open('rb') as stream:raw=stream.read(limit+1)
    if len(raw)>limit:raise ValueError('%s exceeds %d MB'%(label,limit//1000000))
    return raw,hashlib.sha256(raw).hexdigest()

def parse_canonical(path):
    """JSON array of unique canonical SKU identifiers."""
    raw,digest=_read_raw(path,MAX_CANONICAL_BYTES,'canonical SKU file')
    data=strict_loads(raw)
    if not isinstance(data,list):raise ValueError('canonical SKU file must be a JSON array')
    canonical=[];seen=set()
    if len(data)>MAX_CANONICAL_SKUS:raise ValueError('canonical SKU file exceeds %d SKUs'%MAX_CANONICAL_SKUS)
    for value in data:
        sku=ident(value)
        if sku in seen:raise ValueError('duplicate canonical SKU: '+sku)
        seen.add(sku);canonical.append(sku)
    return canonical,digest

def parse_aliases(path,as_of,canonical):
    """Validate the whole JSONL file; return only the graph effective on as_of.

    Structural checks (fields, identifiers, strict dates, non-empty interval) run
    on every line. Date-scoped checks (duplicate alias, canonical shadowing) run
    on the effective graph alone, as the contract examines only mappings in force
    on the requested day.
    """
    canonical=set(canonical)
    with Path(path).open('rb') as stream:raw=stream.read(MAX_ALIAS_BYTES+1)
    if len(raw)>MAX_ALIAS_BYTES:raise ValueError('alias file exceeds 64 MB')
    digest=hashlib.sha256(raw).hexdigest()
    lines=raw.splitlines()
    if len(lines)>MAX_ALIAS_LINES:raise ValueError('alias file exceeds 500000 lines')
    edges={}
    for lineno,line in enumerate(lines,1):
        if len(line)>MAX_ALIAS_LINE_BYTES:raise ValueError('line %d: exceeds 1 MB'%lineno)
        if not line.strip():raise ValueError('line %d: blank line'%lineno)
        try:
            record=strict_loads(line)
            require(record,'alias target valid_from valid_to')
            alias=ident(record['alias']);target=ident(record['target'])
            start=parse_day(record['valid_from'],'valid_from')
            end=None if record['valid_to'] is None else parse_day(record['valid_to'],'valid_to')
            if end is not None and end<=start:raise ValueError('valid_to must be after valid_from')
        except ValueError as error:
            raise ValueError('line %d: %s'%(lineno,error)) from None
        if not (start<=as_of and (end is None or as_of<end)):continue
        if alias in canonical:raise ValueError('line %d: alias %r is also a canonical SKU'%(lineno,alias))
        if alias in edges:raise ValueError('line %d: overlapping validity for alias %r'%(lineno,alias))
        edges[alias]=target
    return edges,digest

def resolve_aliases(edges,canonical):
    """Iterative memoized chain resolution; O(V+E) time, no recursion."""
    canonical=set(canonical);resolved={}
    for start in sorted(edges):
        if start in resolved:continue
        path=[];seen=set();node=start
        while True:
            if node in canonical:root=node;break
            if node in resolved:root=resolved[node];break
            if node in seen:
                raise ValueError('alias cycle: '+' -> '.join(path[-8:]+[node]))
            seen.add(node);path.append(node)
            nxt=edges.get(node)
            if nxt is None:
                raise ValueError('dangling alias chain from %r: %r is neither an alias nor a canonical SKU'
                                 %(start,node))
            node=nxt
        for name in path:resolved[name]=root
    return resolved

def normalize_baskets(snapshot,canonical,resolved):
    """Resolve and fold items; basket ids, empty baskets and parameters are unchanged."""
    minimum,maximum,numerator,denominator,baskets=parse_snapshot(snapshot)
    canonical=set(canonical)
    output=[];universe=set()
    stats={'resolved_items':0,'folded_items':0,'items_before':0,'items_after':0}
    for bid,items in baskets:
        normalized=[];present=set()
        for item in items:
            stats['items_before']+=1
            if item in canonical:
                sku=item
            elif item in resolved:
                sku=resolved[item];stats['resolved_items']+=1
            else:
                raise ValueError('basket %r item %r is neither a canonical SKU nor an effective alias'
                                 %(bid,item))
            if sku in present:stats['folded_items']+=1
            else:
                present.add(sku);normalized.append(sku)
        stats['items_after']+=len(normalized);universe.update(normalized)
        output.append({'id':bid,'items':sorted(normalized)})
    if not output:raise ValueError('requires 1..%d baskets'%MAX_BASKETS)
    if len(universe)>MAX_ITEMS:raise ValueError('normalized basket universe exceeds %d unique SKUs'%MAX_ITEMS)
    normalized={'min_support_count':minimum,'max_itemset_size':maximum,
                'min_confidence':{'numerator':numerator,'denominator':denominator},
                'baskets':output}
    return normalized,stats

def normalize(source,canonical_file,alias_file,as_of_text,output):
    day=parse_day(as_of_text,'--as-of')
    out=Path(output)
    if out.is_symlink():raise ValueError('output cannot be a symlink')
    target=out.resolve()
    for label,path in (('source',source),('canonical',canonical_file),('alias',alias_file)):
        if target==Path(path).resolve():raise ValueError('output must differ from the %s input'%label)
    snapshot,source_digest=read(source)
    canonical,canonical_digest=parse_canonical(canonical_file)
    edges,alias_digest=parse_aliases(alias_file,day,canonical)
    resolved=resolve_aliases(edges,canonical)
    normalized,stats=normalize_baskets(snapshot,canonical,resolved)
    save(out,normalized)
    return {'as_of':as_of_text,
            'source_sha256':source_digest,
            'canonical_sha256':canonical_digest,
            'aliases_sha256':alias_digest,
            'basket_count':len(normalized['baskets']),
            'effective_aliases':len(edges),
            'resolved_items':stats['resolved_items'],
            'folded_items':stats['folded_items'],
            'items_before':stats['items_before'],
            'items_after':stats['items_after'],
            'output':str(out)}
