"""Content features and grounded labels for parliamentary debates.

This derivative is used only by the map. Source text and saved search embeddings
are never changed. Parliamentary speakers and procedural language cannot name
substantive topics merely because they recur across many unrelated debates.
"""
from collections import Counter,defaultdict
from functools import lru_cache
import bisect,gzip,hashlib,json,math,re
from pathlib import Path
import numpy as np
from nltk.stem.snowball import ItalianStemmer
from topics import STOP

EXTRA=set('''
pag pagg pagina pagine presidente presidenza vicepresidenza onorevole onorevoli
collega colleghi deputato deputata deputati deputate senatore senatori senato
camera parlamento parlamentare parlamentari assemblea aula gruppo gruppi
signor signora signori signore ministro ministra ministri ministero ministeri
sottosegretario sottosegretaria segretario segretaria relatore relatrice relatori
commissione commissioni governo maggioranza opposizione seduta sedute legislatura
interpellanza interpellanze interrogazione interrogazioni urgente urgenti
emendamento emendamenti subemendamento subemendamenti proposta proposte articolo
articoli comma commi disegno decreto decreti legislativo legislativi legge leggi
provvedimento provvedimenti deliberazione deliberazioni regolamento allegato
allegati votazione votazioni voto voti favorevole favorevoli contrario contrari
astenuto astenuti nominale elettronico procedimento personale facoltà parola
discussione esame dichiarazione dichiarazioni replica rispondere risposta risposte
passiamo passare indico indìco approvato approvata approvati respinto respinta
ritirato ritirata riformulazione raccomandazione presentato presentata presentati
presentate parere pareri mediante intenda illustrare illustrazione prego grazie
ringrazio applausi commenti interruzioni proteste deputazioni resoconto stenografico
odierna odierno comunicazioni comunicazione comunicato lettura verbale processo
precedente successivo successiva numero numeri punto punti questione questioni
tema temi argomento argomenti merito riferimento riguardo oggi ieri domani
infatti dunque tuttavia certamente ovviamente naturalmente appunto quindi
peraltro pertanto comunque purtroppo evidentemente esattamente assolutamente
qualcosa qualcuno nessuno nulla niente parte parti caso casi tipo modo modi
momento senso termini termine profilo punto aspetti aspetto particolare particolare
importante importanti necessario necessaria necessari necessarie opportuno
quanto quanti quante stessi soprattutto già ancora ora allora bene tanto
abbia abbiano abbiamo avrei avrebbe avrebbero avessero avrebbe avendo avuto
debba debbano dovrà dovranno dobbiamo dovremmo dovrebbero dovrebbe dovuto dover
possa possano potrà potranno possiamo potrebbe potrebbero potuto poter voglio
vogliamo vogliate vuole voglia vorrebbe vorrebbero fosse fossero fosse sarei
sarebbe sarebbero saranno sarà siano state possa era stati stato stata essendo
vedi vediamo vedremo dico diciamo dice dire detto direi fatto facciamo fare fanno
credo penso ritengo credo vorrei ricordare ricordo sottolineare sottolineo
decorrere decorre decorrenza previsto prevista previsti previste prevede prevedere
seguente seguenti presente presenti rispettivamente altresì nonché ovvero
gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre
novembre dicembre mila milioni miliardi euro mila cento centinaia percentuale
per cento ecc abb ac doc sed xvii xviii xix cfr lettera lettere successivamente
nostro nostra nostri nostre vostro vostra vostri vostre chiesto chiedere chiedo
parlare intervenire intervento interventi indizione dichiaro aperta aperto chiusa
chiuso approva respinge invitare invito ritiro ritirare accolto accolta accolti
accogliere accetta accettato formulato formulata riformulato riformulata
soddisfatto soddisfatta valutare opportunità ordine giorno minoranza segnalato
segnalazioni disciplina relative relativa relativi relativo attività materia
misura misure disposizioni disposizione sensi modalità circostanza
costituisce riguarda riguardano riguardante riguardanti riferimento
esprime espresso espressa esprimere ritiene ritenere risulta risulti risultano
dibattito italiano italiana italiani italiane italia paese paesi problema problemi
tale tali bis ter quater quinquies sexies aggiuntivo aggiuntivi conforme rimetto
procedura procedure voteremo voterà votiamo voteranno ripeto concludo possiamo
cio che qual cos tut att riservi riserva ritirarlo accantonate accantonato
iniziativa iniziative volta volte volto volti riferisce riferiscono ovviamente
però competenza competenze applicazione applicazioni norma norme normativa normative
partito democratico dichiarare
nuovo nuova nuovi nuove ulteriore ulteriori recante recanti attuazione esercizio
tre due quattro cinque sei sette otto nove dieci minuti minuto garantire
favore esercenti occasione ambito riferimento elementi riguardo adozione
alcune alcuni alcuna alcuno ciascuno ciascuna qualsiasi qualunque soltanto nemmeno
neppure sempre spesso talvolta anzitutto innanzitutto finché affinché sebbene
'''.split())
STOPWORDS=(STOP|EXTRA)-{'disposizioni','disposizione'}
CONNECTORS={'di','del','della','delle','dei','degli','dello','e','ed','per','in','a','al','alla','alle','ai','agli','da','con'}
WORDS=re.compile(r"[^\W\d_]+(?:[’'][^\W\d_]+)?",re.UNICODE)
SPEAKER=re.compile(r"(?m)(?:^|\n)\s*(?P<name>PRESIDENTE|(?:[A-ZÀ-Ü][A-ZÀ-Ü’'\-]+[ \t]+){1,7}[A-ZÀ-Ü][A-ZÀ-Ü’'\-]+)(?:,\s*[^.\n]{1,160}|\s*\([^\n]{1,160}\))?\.\s*")
ASIDE=re.compile(r'\((?:Applausi|Commenti|Si ride|Interruzioni|Proteste|Segue la votazione|Vedi votazione)[^)]*\)',re.I)
stemmer=ItalianStemmer()
VERB_STEMS={stemmer.stem(word) for word in ('votare','parlare','chiedere','ritenere','ritirare','illustrare','dichiarare','ringraziare','esprimere','approvare','decorrere','ricordare','volere')}
GENERIC_LABELS={'settore','settori','servizi','tale','volte','identità','politica','economico','economica','disposizione','pubblica','applicazione','opera','relazione'}
LABEL_FILLER={'finanziamento','esigenze','indifferibili','stipulati','avvio','esecuzione','ratifica','favorire'}
GENERIC_ROOTS={stemmer.stem(word) for word in GENERIC_LABELS}

@lru_cache(maxsize=60000)
def stem(word):return stemmer.stem(word)

def word_value(word):
    return re.sub(r"^(?:l|dell|all|dall|nell|sull|un|quest)'",'',word.lower().replace('’',"'"))

def content_text(text):
    # Keep adjacent speech turns separate even when a saved chunk has no blank
    # line between a procedural chair turn and a substantive intervention.
    text=SPEAKER.sub('\n\n',text)
    paragraphs=[]
    for paragraph in re.split(r'\n\s*\n',text):
        # These are identifiable chair/voting turns, not the substantive speeches.
        if re.match(r'\s*(?:Ind[iì]co\b|Dichiaro\b|Passiamo\b|Ha chiesto di parlare|Invito\b|Ordine del giorno\s|Parere (?:contrario|favorevole)|votazione n\.|La Camera (?:approva|respinge)|Risultano ancora accantonat|\(Esame dell.articolo)',paragraph):continue
        paragraphs.append(paragraph)
    text='\n\n'.join(paragraphs)
    text=SPEAKER.sub('\n',text)
    text=ASIDE.sub(' ',text)
    text=re.sub(r'(?:[Ll]a |[Ii]l )?(?:deputat[oa]|onorevole|collega|emendamento)\s+(?:[A-ZÀ-Ü][a-zà-ü]+\s*){1,4}', ' ',text)
    text=re.sub(r'https?://\S+|www\.\S+',' ',text)
    return text

def substantive_heading(value):
    value=re.sub(r'\s+',' ',value).strip()
    if value.startswith('(') and value.endswith(')'):value=value[1:-1]
    if re.match(r'(?i)^(?:(?:ripresa|seguito) )?(?:discussione sulle linee generali|dichiarazioni di voto|votazione finale|esame (?:degli? |dell.|delle? |e votazione))',value):return ''
    if not value or re.match(r'(?i)(?:modifica nella composizione|annuncio|assegnazione|comunicazioni|interventi di fine|svolgimento di|si riprende la discussione|test[oi] de[gl]{1,2}i interventi|missioni|sui lavori|calendario dei lavori|ordine del giorno|sull.ordine dei lavori)',value):return ''
    value=re.sub(r'(?i);?\s*e (?:delle?\s+|dell.)abbinat.*$','',value)
    value=re.sub(r'(?i)^.*?concernent[ei]\s+','',value)
    value=re.sub(r'(?i)^(?:seguito della )?discussione[^:]*:\s*','',value)
    value=re.sub(r'(?i)^conversione in legge.*?recante\s+','',value)
    if ':' in value:value=value.rsplit(':',1)[-1].strip()
    value=re.sub(r'(?i)^S\.\s*\d+\s*[–—-]\s*','',value)
    value=re.sub(r'(?i)^conversione in legge.*?recante\s+','',value)
    value=re.sub(r'\([^)]*(?:\)|$)','',value)
    value=re.sub(r'\s*[-–]\s*n\.\s*\d.*$','',value)
    value=re.sub(r'(?i)^(?:iniziative(?: di competenza)?(?: volte)?(?: a| per| in merito a)?|elementi ed iniziative|chiarimenti(?: in merito)?)(?:\s+al?l[aeo]?|\s+su[il]?|\s+per|\s+a)?\s+','',value)
    value=re.sub(r'\s+',' ',value).strip(' .;:–-')
    return value if len(content_words(value))>=2 else ''

def passage_contexts(rows,documents,data_path,check=lambda:None):
    """Recover real agenda headings from the audited source, validating paragraphs."""
    grouped=defaultdict(list)
    for index,row in enumerate(rows):grouped[row['document']].append((index,row))
    result=['']*len(rows)
    for doc in documents:
        if doc['id'] not in grouped:continue
        check();source=Path(doc['source']);audit=source.parent.parent/'temi'/'audit'/(source.stem+'.jsonl.gz')
        try:
            full=(Path(data_path)/doc['stored']).read_text(encoding='utf-8-sig') if doc['stored'] else source.read_text(encoding='utf-8-sig')
            with gzip.open(audit,'rt') as stream:records={r['output_paragraph']:r for line in stream if (r:=json.loads(line)).get('kept')}
        except (OSError,ValueError,KeyError,TypeError):continue
        starts=[];headings=[];position=0;subheading='';section=''
        for index,paragraph in enumerate(full.rstrip('\n').split('\n\n')):
            record=records.get(index,{})
            valid=record.get('sha256')==hashlib.sha256(paragraph.encode()).hexdigest()
            current=record.get('section','') if valid else ''
            if current!=section:section=current;subheading=''
            if re.fullmatch(r'\([^\n]{20,1200}\)',paragraph.strip()) and re.search(r'[-–]\s*n\.',paragraph):
                subheading=substantive_heading(paragraph)
            starts.append(position);headings.append(subheading or substantive_heading(section) if valid else '')
            position+=len(paragraph)+2
        cursor=0
        for index,row in grouped[doc['id']]:
            start=full.find(row['text'],cursor)
            if start<0:continue
            cursor=start+1;end=start+len(row['text']);counts=Counter()
            first=max(0,bisect.bisect_right(starts,start)-1)
            for part in range(first,len(starts)):
                if starts[part]>=end:break
                last=starts[part+1] if part+1<len(starts) else len(full)
                if headings[part]:counts[headings[part]]+=min(last,end)-max(starts[part],start)
            if counts:result[index]=counts.most_common(1)[0][0]
    return result

def speaker_words(texts):
    """Recognize speakers from explicit uppercase turn headers, never guess names."""
    return {word_value(token.group()) for text in texts for match in SPEAKER.finditer(text)
            for token in WORDS.finditer(match.group('name')) if len(token.group())>=3}

def content_words(text,excluded=frozenset()):
    return [value for match in WORDS.finditer(content_text(text))
            if 3<=len(value:=word_value(match.group()))<=32 and value not in STOPWORDS and value not in excluded and stem(value) not in VERB_STEMS and "'" not in value]

def phrases(text,excluded=frozenset()):
    """Extract actual contiguous phrases, keeping Italian connectors."""
    for sentence in re.split(r'[\n.!?;:(),\[\]]',content_text(text).lower()):
        tokens=list(WORDS.finditer(sentence))
        for start,token in enumerate(tokens):
            value=word_value(token.group())
            if len(value)<3 or value in STOPWORDS or value in excluded or stem(value) in VERB_STEMS or "'" in value:continue
            roots=[];previous=token.start()
            for end in range(start,min(start+8,len(tokens))):
                current=tokens[end]
                if sentence[previous:current.start()].strip() not in {'','-','–','‑'}:break
                previous=current.end();word=word_value(current.group())
                if word in {'e','ed'}:break
                if word in CONNECTORS:continue
                if len(word)<3 or word in STOPWORDS or word in excluded or stem(word) in VERB_STEMS or "'" in word:break
                roots.append(stem(word))
                phrase=sentence[token.start():current.end()].strip()
                phrase=re.sub(r"^(?:l|dell|all|dall|nell|sull|un|quest)['’]",'',phrase)
                if len(roots)<=4:yield tuple(roots),phrase
                if len(roots)==4:break

def content_features(texts,embeddings,check=lambda:None,notify=lambda message:None,semantic_weight=.25,contexts=None):
    """Combine centered semantic vectors with normalized substantive TF-IDF/SVD."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.decomposition import TruncatedSVD
    check();notify('Mappa: contenuti del dibattito, esclusione delle formule procedurali…')
    documents=[];excluded=speaker_words(texts)
    for text in texts:
        check();documents.append(' '.join(stem(word) for word in content_words(text,excluded)))
    centered=embeddings-embeddings.mean(axis=0)
    norms=np.linalg.norm(centered,axis=1,keepdims=True)
    semantic=np.where(norms>1e-7,centered/np.maximum(norms,1e-7),embeddings)
    try:
        vectorizer=TfidfVectorizer(min_df=3 if len(texts)>=100 else 1,max_df=.8,
            max_features=20000,ngram_range=(1,2),sublinear_tf=True,dtype=np.float32)
        lexical=vectorizer.fit_transform(documents)
    except ValueError:return semantic
    check();notify('Mappa: rappresentazione dei temi sostanziali…')
    dimensions=min(96,lexical.shape[0]-1,lexical.shape[1]-1)
    if dimensions<2:return semantic
    reduced=TruncatedSVD(n_components=dimensions,n_iter=7,random_state=42).fit_transform(lexical)
    reduced/=np.maximum(np.linalg.norm(reduced,axis=1,keepdims=True),1e-9)
    components=[math.sqrt(semantic_weight)*semantic,math.sqrt(1-semantic_weight)*reduced]
    if contexts and any(contexts):
        heading_text=[' '.join(stem(word) for word in content_words(value)) for value in contexts]
        try:
            heading_matrix=TfidfVectorizer(min_df=1,ngram_range=(1,2),sublinear_tf=True,dtype=np.float32).fit_transform(heading_text)
            dim=min(64,heading_matrix.shape[0]-1,heading_matrix.shape[1]-1)
            if dim>=2:
                heading_vectors=TruncatedSVD(n_components=dim,random_state=42).fit_transform(heading_matrix)
                heading_vectors/=np.maximum(np.linalg.norm(heading_vectors,axis=1,keepdims=True),1e-9)
                components=[math.sqrt(.1)*semantic,math.sqrt(.3)*reduced,math.sqrt(.6)*heading_vectors]
        except ValueError:pass
    features=np.hstack(components)
    features/=np.maximum(np.linalg.norm(features,axis=1,keepdims=True),1e-9)
    check();return features.astype('<f4')

def procedural_mask(texts):
    excluded=speaker_words(texts)
    return np.asarray([len(content_words(text,excluded))<16 for text in texts],dtype=bool)

def topic_names(texts,labels,check=lambda:None,contexts=None):
    """Names supported by recurring content in passages, not procedural frequency."""
    counts=defaultdict(Counter);coverage=defaultdict(Counter);sizes=Counter();forms=defaultdict(Counter)
    global_counts=Counter();global_coverage=Counter();total=0;excluded=speaker_words(texts)
    for index,(text,group) in enumerate(zip(texts,labels)):
        if int(group)<0:continue
        if contexts and contexts[index]:text=contexts[index]+'.\n'+text
        check();terms=Counter();variants=defaultdict(Counter)
        for key,phrase in phrases(text,excluded|LABEL_FILLER):terms[key]+=1;variants[key][phrase]+=1
        if not terms:continue
        size=sum(math.sqrt(v) for v in terms.values());sizes[int(group)]+=1;total+=1
        for key,count in terms.items():
            value=math.sqrt(count)/size;counts[int(group)][key]+=value;global_counts[key]+=value
            coverage[int(group)][key]+=1;global_coverage[key]+=1;forms[key].update(variants[key])
    result={}
    for group in set(map(int,labels)):
        if group<0:result[group]=['Passaggi brevi o procedurali'];continue
        check();scores={}
        for key,count in counts[group].items():
            if len(key)==1 and key[0] in GENERIC_ROOTS:continue
            cover=coverage[group][key]/sizes[group]
            if cover<(.30 if len(key)==1 else .15):continue
            frequency=count/sizes[group];background=global_counts[key]/max(1,total)
            idf=.2+math.log((1+total)/(1+global_coverage[key]))
            scores[key]=frequency*math.sqrt(cover)*math.sqrt(frequency/background)*idf*(1+.8*(len(key)-1))
        selected=[];seen=set()
        for key in sorted(scores,key=lambda k:(-scores[k],k)):
            if seen.intersection(key):continue
            if len(key)==1:
                if selected:continue
                choices=[k for k in scores if len(k)>1 and key[0] in k and not seen.intersection(k)
                    and scores[k]>=.22*scores[key] and coverage[group][k]>=.18*coverage[group][key]]
                if choices:key=max(choices,key=lambda k:scores[k])
            phrase=min(forms[key],key=lambda p:(-forms[key][p],len(p),p))
            selected.append(phrase[0].upper()+phrase[1:]);seen.update(key)
            if len(selected)==2:break
        result[group]=sorted(selected,key=lambda value:(' ' not in value,selected.index(value))) or ['Temi diversi — nessun argomento prevalente']
        if sizes[group]>=50 and all(' ' not in value for value in selected):
            result[group]=['Temi diversi — nessun argomento prevalente']
        # A prevalent real agenda title is more informative than a fragment of
        # legal prose. Keep it whole; use extracted phrases for long/mixed titles.
        if contexts:
            headings=Counter(context for context,label in zip(contexts,labels) if int(label)==group and context)
            if headings:
                heading,count=headings.most_common(1)[0]
                heading=re.sub(r'(?i)^(?:(?:norme|disposizioni)(?: urgenti)? in materia di|nuovi interventi urgenti in favore delle)\s+','',heading)
                heading=re.sub(r'(?i)^modifiche alla legge .*?in materia di\s+','',heading)
                if count/max(1,sum(int(label)==group for label in labels))>=.65 and len(heading)<=125:
                    result[group]=[heading[0].upper()+heading[1:]]
    return result
