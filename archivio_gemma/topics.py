"""Local, extractive topic labels for an existing embedding partition.

Text only supplies labels; it never determines cluster membership. Counts are
balanced per document so a long essay and its overlapping chunks cannot drown
out the other documents. No generative model or external service is required.
"""
from collections import Counter, defaultdict
import math
import re
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

STOP = set(ENGLISH_STOP_WORDS) | set('''
alla alle allo agli anche ancora avere aveva avevano abbiamo avete che chi con
come cui cosa cose dal dalla dalle dallo dagli dei del della delle dello degli
dentro deve devono dove dopo durante ecco era erano essere fino fra gli hai hanno
ho loro lui lei ma mai mentre mio miei molto nei nel nella nelle nello negli non
noi ogni oltre per perché piu più poi prima proprio quale quali quando quanto
quasi questo questa questi queste quello quella quelli quelle quindi qui senza
sia siamo siete solo sono sopra sotto sta stato stati stata stesso stessa stessi
stesse sua sue suo suoi sul sulla sulle sullo sugli tra troppo tutto tutti tutte
una uno un voi vorrei può puo possono fare fatto fanno meglio meno invece anni
anno già gia così cosi c'è cè dell all nell sull dall un'altra altro altri altre
altra esempio esempio spesso sempre tanto tanto davvero qualche oppure cioè
people things thing really just like don't doesn't didn't isn isn't aren't wasn
weren doesn didn don ll ve re mr mrs said say says think way make want know good
bad new time years year lot lots probably actually usually going come got does
did having getting get making made let need use used using uses look looking
different example course certain certainly simply especially particular case
cases reason kind fact possible right left fact yes better best true great long
short hard big small world tend tends notes thanks thank copyright january february march april june july august
september october november december http https www com org html txt document
documento pagina page vista completa testo text untitled figure figura image
'''.split())
TOKEN = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?|[^\w\s]|[\r\n]", re.UNICODE)
LANGUAGE = r'(?:arabic|armenian|bulgarian|catalan|chinese|czech|danish|dutch|english|estonian|finnish|french|georgian|german|greek|hebrew|hungarian|irish|italian|japanese|korean|persian|polish|portuguese|romanian|russian|serbian|slovak|slovenian|spanish|swedish|telugu|turkish|ukrainian|uzbek|vietnamese|italiano|francese|inglese|spagnolo|tedesco)'
TRANSLATION_LINK = re.compile(rf'^(?:{LANGUAGE}\s+(?:translation|version)|(?:translation|traduzione)(?:\s+in)?\s+{LANGUAGE}|translations|traduzioni)\s*[.!:]?$', re.I)
TRANSLATION_LABEL = re.compile(rf'\b(?:(?:Simplified|Traditional)\s+)?(?i:{LANGUAGE})\s+(?:Translation|Version)\b')
PHRASE_MODIFIERS = {'good', 'great', 'new', 'big', 'small'}


def label_text(text):
    """Remove navigation/acknowledgment lines only for labels, not the index."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if TRANSLATION_LINK.fullmatch(stripped):
            lines.append('')
        elif re.match(r'^(?:thanks to|thank you to|ringrazio|ringraziamenti\b)', stripped, re.I):
            lines.append('')
        else:
            line=TRANSLATION_LABEL.sub(' ',line)
            lines.append(re.sub(r'https?://\S+|www\.\S+', ' ', line))
    return '\n'.join(lines)


def terms(text):
    """Real adjacent phrases of 1–3 words, bounded by stopwords/punctuation."""
    previous=[];end=0
    text=label_text(text).lower()
    for match in TOKEN.finditer(text):
        if text[end:match.start()].strip():previous=[]
        end=match.end()
        word=match.group().replace('’',"'")
        if word.endswith("'s"):word=word[:-2]
        word=re.sub(r"^(?:l|dell|all|dall|nell|sull|un)'",'',word)
        if len(word)<3 or len(word)>32 or (word in STOP and word not in PHRASE_MODIFIERS) or "'" in word:
            previous=[];continue
        if word not in PHRASE_MODIFIERS:
            yield word
            for length in range(1,min(2,len(previous))+1):
                yield ' '.join(previous[-length:]+[word])
        previous=(previous+[word])[-2:]


def term_key(term):
    return ' '.join(word[:-3]+'y' if len(word)>4 and word.endswith('ies') else
                    word[:-1] if len(word)>4 and word.endswith('s') and not word.endswith(('ss','us','is')) else word
                    for word in term.split())


def canonical(term):
    # Suppress near-duplicates such as startup/startups and programming languages/
    # language. Keep display text exactly as extracted, including accents.
    def root(word):
        if len(word)>5 and word.endswith('ing'):
            word=word[:-3]
            if len(word)>3 and word[-1]==word[-2]:word=word[:-1]
        elif len(word)>5 and word.endswith('er'):
            word=word[:-2]
            if len(word)>3 and word[-1]==word[-2]:word=word[:-1]
        if len(word)>4 and word.endswith('e'):word=word[:-1]
        return word
    return {root(word) for word in term_key(term).split()}


def cluster_topics(rows,check=lambda:None,titles=None,references=None):
    """rows yields (cluster id, document id, text[, point id]), ordered by document.

    Per-document TF-IDF, coverage and enrichment against the whole corpus.
    The background does not depend on the number of groups, so splitting an
    unrelated group cannot rename an unchanged group. Up to three labels.
    Empty/visual-only clusters deliberately receive no inferred text topics.
    """
    counts=defaultdict(Counter);coverage=defaultdict(Counter);documents=Counter()
    corpus=Counter();document_frequency=Counter();forms=defaultdict(Counter)
    title_coverage=defaultdict(Counter)
    titles=titles or {}
    reference_groups=defaultdict(list)
    for group,points in (references or {}).items():
        for point in points:reference_groups[point].append(group)
    reference_coverage=defaultdict(Counter);reference_documents=Counter()
    reference_pending=defaultdict(set)
    total_documents=0
    current=None;pending=defaultdict(Counter)
    def flush():
        nonlocal total_documents
        combined=Counter()
        for frequencies in pending.values():combined.update(frequencies)
        if not combined:
            pending.clear();reference_pending.clear();return
        total_documents+=1
        normalizer=math.fsum(math.sqrt(min(count,64)) for count in combined.values())
        for term,count in combined.items():
            corpus[term]+=math.sqrt(min(count,64))/normalizer
            document_frequency[term]+=1
        for group,frequencies in pending.items():
            documents[group]+=1
            # Titles can support a label only when the same term occurs in this
            # group's actual text, including in passage view.
            title_coverage[group].update(set(term_key(term) for term in terms(titles.get(current,''))) & frequencies.keys())
            for term,count in frequencies.items():
                counts[group][term]+=math.sqrt(min(count,64))/normalizer
                coverage[group][term]+=1
        for group,words in reference_pending.items():
            if words:
                reference_documents[group]+=1
                reference_coverage[group].update(words)
        reference_pending.clear()
        pending.clear()
    for row in rows:
        group,document,text=row[:3]
        point=row[3] if len(row)>3 else document
        check()
        if current is not None and document!=current:flush()
        current=document
        frequencies=Counter(terms(text))
        for term,count in frequencies.items():
            key=term_key(term)
            pending[group][key]+=count
            forms[key][term]+=count
            for reference in reference_groups[point]:reference_pending[reference].add(key)
    flush()
    if not counts:return {}
    display={key:min(variants,key=lambda word:(-variants[word],word)) for key,variants in forms.items()}
    result={}
    for group,frequencies in counts.items():
        check();scores={}
        for term,count in frequencies.items():
            # Require recurring phrases in larger groups; one-off prose snippets
            # are less useful as topics. Small groups can still be described.
            recurring=max(2,math.ceil(.15*documents[group]))
            if documents[group]>=4 and coverage[group][term]<recurring:continue
            cover=coverage[group][term]/documents[group]
            frequency=count/documents[group]
            background=corpus[term]/total_documents
            enrichment=math.sqrt(frequency/background)
            idf=.1+math.log((1+total_documents)/(1+document_frequency[term]))
            title_boost=1+min(1.,2*title_coverage[group][term]/coverage[group][term])
            # A stable sibling reference distinguishes subtopics such as funding
            # and starting a company, instead of naming both just "startup".
            contrast=1.
            if reference_documents[group]:
                outside=reference_coverage[group][term]/reference_documents[group]
                contrast=.7+.3*max(0.,(cover-outside)/(cover+.05))
            scores[term]=frequency*idf*math.sqrt(cover)*enrichment*(1+.5*term.count(' '))*title_boost*contrast
        chosen=[];seen=set()
        for term in sorted(scores,key=lambda term:(-scores[term],term)):
            words=canonical(term)
            if words & seen:continue
            if ' ' not in term:
                phrases=[phrase for phrase in scores if ' ' in phrase and term in phrase.split()
                         and scores[phrase]>=.3*scores[term]
                         and coverage[group][phrase]>=.25*coverage[group][term]
                         and not canonical(phrase)&seen]
                if phrases:
                    term=min(phrases,key=lambda phrase:(-scores[phrase],phrase));words=canonical(term)
            chosen.append(display[term]);seen.update(words)
            if len(chosen)==3:break
        result[group]=chosen
    return result
