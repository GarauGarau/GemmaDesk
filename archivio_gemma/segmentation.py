"""Lossless, tokenizer-aware segmentation at paragraph/sentence boundaries."""
from dataclasses import dataclass
import re

# Retrieval units are intentionally smaller than the model's native context.
# The budget includes the title/prefix and the repeated context.
TEXT_LIMIT = 1024
OVERLAP_LIMIT = 96


@dataclass(frozen=True)
class Segment:
    text: str
    start: int  # offsets of NEW content, excluding the repeated context
    end: int
    overlap: int


def tail(text, count, limit=OVERLAP_LIMIT):
    # A suffix only; no inserted separators or lost source characters.
    candidate=text[-2048:]
    while candidate:
        while candidate and count(candidate)>limit:
            candidate=candidate[max(1,len(candidate)//8):]
        boundary=re.search(r'\s+',candidate)
        if boundary:candidate=candidate[boundary.end():]
        # Removing a partial word or leading space can increase the token count.
        # Recheck the actual suffix after aligning it to a word boundary.
        if not candidate or count(candidate)<=limit:return candidate
    return ''


def segments(blocks, count, prefix='', context='', limit=TEXT_LIMIT, check=lambda:None):
    """Read ahead until the real token budget is exceeded, then split.

    Every source character occurs exactly once outside the explicit overlaps.
    Blocks are IO buffers, never forced semantic boundaries. Short documents
    are emitted whole even when they span many buffers.
    """
    carry=tail(context,count) if context else ''
    buffer='';offset=0
    def fits(value):
        check();return count(prefix+value)<=limit
    if not fits(''):raise ValueError('Il titolo supera il contesto del modello.')
    for block in blocks:
        check();buffer+=block
        while buffer and not fits(carry+buffer):
            while carry and not fits(carry+buffer[:1]):carry=carry[max(1,len(carry)//4):]
            lo=0;hi=len(buffer)
            while lo+1<hi:
                mid=(lo+hi)//2
                if fits(carry+buffer[:mid]):lo=mid
                else:hi=mid
            if not lo:raise ValueError('Un carattere non entra nel contesto del modello.')
            # Prefer a paragraph, then a sentence, line or word near the budget.
            # Very long sentences/code fall back to a Unicode character boundary.
            cut=lo;minimum=max(1,int(lo*.60))
            for pattern in (r'\n[ \t\r]*\n+',r'[.!?。！？][\"»”\x27)]*\s+',r'\n',r'\s+'):
                positions=[m.end() for m in re.finditer(pattern,buffer[:lo]) if m.end()>=minimum]
                # Recheck: tokenizer counts need not be strictly monotonic.
                found=next((end for end in reversed(positions) if fits(carry+buffer[:end])),None)
                if found is not None:cut=found;break
            fresh=buffer[:cut];passage=carry+fresh
            if not fits(passage):raise ValueError('Suddivisione oltre il contesto del modello.')
            yield Segment(passage,offset,offset+cut,len(carry))
            offset+=cut;buffer=buffer[cut:];carry=tail(fresh,count)
    if buffer:
        check()
        if not fits(carry+buffer):raise ValueError('Ultimo passaggio oltre il contesto del modello.')
        yield Segment(carry+buffer,offset,offset+len(buffer),len(carry))
