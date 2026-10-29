"""Conservative, auditable thematic derivative of the Camera HTML originals."""
from __future__ import annotations
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parents[1] / 'data/camera_deputati'
VERSION = 'camera-thematic-v2'
WORD = re.compile(r"[^\W_]+(?:[’'-][^\W_]+)*", re.UNICODE)


def words(text):
    return len(WORD.findall(text))


def plain(node):
    return ' '.join(node.get_text().replace('\u200b', '').replace('\ufeff', '').split())


def section_reason(text, classes):
    text = text.casefold()
    if 'titolo_votazioni' in classes or text.startswith('votazioni qualificate effettuate'):
        return 'tabulati delle votazioni'
    if 'titolo_segnalazioni' in classes or text.startswith('segnalazioni relative alle votazioni'):
        return 'rettifiche tecniche delle votazioni'
    if 'titolo_calendario' in classes:
        return 'calendario dei lavori futuri'
    if re.fullmatch(r'missioni(?:\s*\([^)]*\))?\s*\.?', text):
        return 'missioni e presenze'
    if text.startswith('preavviso di votazioni elettroniche'):
        return 'preavviso delle votazioni'
    if re.match(r'ordine del giorno (?:della (?:prossima seduta|seduta di domani)|dei lavori della prossima)', text):
        return 'ordine del giorno futuro'
    return ''


def formula_reason(text):
    # Full paragraph matches only: substantive speech containing these words stays.
    value = re.sub(r'^PRESIDENTE\.\s*', '', text).strip()
    patterns = [
        r'(?:La seduta (?:comincia|termina|è sospesa|riprende)|Inizio) alle [\d,.: ]+\.?',
        r'La seduta è (?:aperta|tolta)\.?',
        r'(?:Dichiaro|Dichiaro quindi) (?:aperta|chiusa) la votazione\.?',
        r'\(?(?:Segue la votazione|La Camera approva|La Camera respinge|È approvato|È approvata)\)?\.?',
        r'\((?:Applausi|Commenti|Si ride|Interruzioni|Proteste)[^)]*\)\.?',
        r'Invito (?:il deputato|la deputata) [Ss]egretari[oa] a dare lettura del processo verbale della seduta (?:precedente|di ieri)\.?',
        r'Se non vi sono osservazioni, il processo verbale si intende approvato\.?',
        r'.+,\s*[Ss]egretari[oa],\s*legge il processo verbale[^.]*\.',
        r'La Camera approva il processo verbale[^.]*\.',
    ]
    return 'formula procedurale isolata' if any(re.fullmatch(p, value) for p in patterns) else ''


# Only explicit turn headers identify a speaker; role never implies irrelevance.
SPEAKER_HEADER = re.compile(r"^(PRESIDENTE|(?:[A-ZÀ-Ü][A-ZÀ-Ü’'–-]*)(?:[ \t]+[A-ZÀ-Ü][A-ZÀ-Ü’'–-]*){0,8})(?:\s*\([^)]{1,100}\))?\s*(?:,\s*[^.]{1,180})?\.\s*")
ASIDE = re.compile(r"\((?:Applausi|Commenti|Si ride|Interruzioni|Proteste|Segue la votazione|Vedi (?:votazione|l.allegato)|È approvat[oa])[^)]*\)", re.I)
PROCEDURES = [
    r"(?:Ha chiesto|È iscritt[oa]|E' iscritt[oa]) (?:di|a) parlare [^.!?]+[.!]?",
    r"(?:Ne ha facoltà|Prego(?:, (?:onorevole|signor Ministro))?|Grazie(?:, [^.!?]+)?|Concluda(?:, [^.!?]+)?|Onorevole, concluda)[.!]?",
    r"(?:Il deputato|La deputata|L'onorevole) [^.!?]+ ha facoltà di (?:parlare|dichiarare se sia soddisfatt[oa])[^.!?]*[.!]?",
    r"Ha facoltà di (?:parlare|replicare) [^.!?]+[.!]?",
    r"Dichiaro (?:aperta|chiusa|aperto|chiuso) (?:la votazione|la discussione(?: sulle linee generali)?|il dibattito|l'esame)[.!]?",
    r"(?:Passiamo|Passeremo)(?: dunque| quindi| ora)? (?:alla votazione|ai voti|all'esame (?:degli|dei) ordini del giorno)[.!]?",
    r"Ind[iì]co la votazione (?:nominale|per appello nominale)[^.!?]*[.!]?",
    r"(?:La Camera|L'Assemblea) (?:approva|respinge)[.!]?",
    r"(?:La invito a concludere|Ha (?:due|tre|cinque|un) minut[oi]|È autorizzat[oa]|È autorizzat[oa] a (?:depositare|consegnare) [^.!?]+)[.!]?",
    r"(?:Il rappresentante del Governo|La relatrice[^.!?]*|Il relatore[^.!?]*) (?:si riserva di intervenire nel prosieguo del dibattito|rinunzia alla replica)[.!]?",
    r"(?:Il seguito (?:del )?dibattito|Il seguito dell'esame|Il seguito della discussione) è rinviato ad altra seduta[.!]?",
    r"Non vi sono altri iscritti a parlare e pertanto dichiaro chiusa la discussione sulle linee generali(?: delle mozioni)?[.!]?",
    r"La seduta, sospesa alle [\d,.: ]+, è ripresa alle [\d,.: ]+[.!]?",
    r"(?:Favorevole|Contrario|Conforme|Mi rimetto all'Aula)[.!]?",

    r"(?:Per cortesia|Sì|No|Va bene|Bene|La ringrazio|Grazie Presidente|Grazie, Presidente)[.!]?",
    r"(?:Parere|Il parere è) (?:favorevole|contrario)[.!]?",
    r"(?:Invito al ritiro|Insisto per la votazione|Ritiro l'emendamento|Accetto la riformulazione)[.!]?",
    r"(?:Presenti|Votanti|Astenuti|Maggioranza|Hanno votato sì|Hanno votato no)\s*[:… .]*[\d .]+[.!]?",
    r"(?:La seduta (?:comincia|termina|è sospesa|riprende)|Inizio) alle [\d,.: ]+[.!]?",
]
PROCEDURES = [re.compile(pattern, re.I) for pattern in PROCEDURES]


def strip_procedures(text, chair=False):
    """Remove matched sentences, never a whole turn just because of its speaker."""
    removed=[]
    def aside(match):
        removed.append(dict(reason='didascalia', text=match.group()));return ' '
    text=' '.join(ASIDE.sub(aside,text).split())
    text=re.sub(r'\s+([.!?;,])',r'\1',text)
    text=re.sub(r'([.!?])\s*\.',r'\1',text)
    kept=[]
    # Protect the abbreviation used in parliamentary act numbers while splitting.
    text=re.sub(r'\bn\.\s*(?=\d)', 'n§ ', text)
    for sentence in re.split(r'(?<=[.!?])\s+(?=[A-ZÀ-Ü(])|\n+',text):
        sentence=sentence.replace('n§','n.').strip()
        if not sentence or not WORD.search(sentence):continue
        reason=formula_reason(sentence)
        if not reason and any(pattern.fullmatch(sentence) for pattern in PROCEDURES):
            reason='conduzione e formule di voto'
        # Technical agenda announcements carry a heading, not additional debate.
        if chair and not reason:
            technical=[
                r"(?:È iscritt[oa]|Ha chiesto) (?:a|di) parlare .+",
                r"(?:Il deputato|La deputata|L'onorevole|Il Ministro|La Ministra|Il Sottosegretario|La Sottosegretaria) .+ ha facoltà di (?:illustrare|replicare|rispondere|intervenire).+",
                r"Ha facoltà di (?:intervenire|replicare|rispondere)(?:,| ).+",
                r"(?:Adesso )?[Dd]o la parola .+",
                r"(?:Ind[iì]co la votazione|Dichiaro aperta la discussione sulle linee generali|Passiamo all'esame (?:delle questioni pregiudiziali|degli emendamenti|dell'articolo)).+",
                r"(?:Prendo atto che|Il relatore|La relatrice|Il rappresentante del Governo).*(?:si riserva di intervenire|rinunciano? (?:alla replica|alle repliche)|rinunzia alla replica|non voler replicare).*",
                r"Comunico che (?:il collega|la collega|il deputato|la deputata).*(?:grave lutto|perdita del padre|perdita della madre).*",
                r"La Presidenza della Camera ha già formulato ai familiari le espressioni della più sentita partecipazione.*",
                r"(?:I relativi testi sono in distribuzione|La ringrazio|Ne ha facoltà, prego|Ha esaurito il suo tempo|Sospendo la seduta che riprenderà alle ore [\d,. ]+)[.!]?",
                r"(?:Intanto,? )?[Ss]alut(?:o|iamo) .*(?:assistono ai nostri lavori|tribun[ae]).*",
                r"(?:Ricordo che i tempi sono previsti|Avverto che (?:i presidenti|il presidente) dei? .+ ne ha(?:nno)? chiesto l'ampliamento).*",
                r"(?:Avverto che,? )?.*Commissione.*si intende autorizzata a riferire oralmente[.!]?",
                r"Avverto che sono state (?:altresì )?presentate (?:le mozioni|le questioni pregiudiziali).*",
            ]
            if not re.search(r'\b(?:ma|perché|ritengo|condanno)\b',sentence,re.I) and any(re.fullmatch(p,sentence) for p in technical):
                reason='conduzione della seduta'
        if chair and not reason and re.match(r"^(?:L'ordine del giorno reca|Avverto che (?:lo schema|sono stati presentati|è stata chiesta)|Passiamo (?:all'interrogazione|alla (?:prima|seconda|successiva) interrogazione))\b",sentence):
            if not re.search(r'\b(?:ma|perché|ritengo|crisi|condanno)\b',sentence,re.I):reason='annuncio tecnico dell’argomento'
        if reason:removed.append(dict(reason=reason,text=sentence))
        else:kept.append(sentence)
    return ' '.join(kept),removed


def clean_html(raw):
    soup = BeautifulSoup(raw, 'lxml')
    body = soup.select_one('#wrapper')
    if body is None:raise ValueError('Manca il corpo ufficiale del resoconto')
    blocks=[]
    def visit(parent):
        for node in parent.children:
            if not isinstance(node, Tag):continue
            if node.name in {'script','style','noscript','hr','br'}:continue
            if node.get('id') in {'navigator','votazioni_elettroniche'}:blocks.append(node)
            elif node.name in {'p','table'} or 'numeroPagina' in node.get('class',[]):blocks.append(node)
            else:visit(node)
    visit(body)
    output=[];audit=[];section='';subsection='';excluded_section='';before_first_section=True
    speaker='';role='unknown';turn=0;written=False;offset=0
    for index,node in enumerate(blocks):
        original=plain(node)
        if not original:continue
        classes=set(node.get('class',[]));reason='';removed=[]
        # Page markers can be nested inside a speech: remove before text extraction.
        for marker in node.select('.numeroPagina'):
            removed.append(dict(reason='numero di pagina',text=plain(marker)));marker.decompose()
        text=plain(node)
        if 'intervento' in classes:
            speaker='';role='unknown';turn+=1
        header=SPEAKER_HEADER.match(text)
        if header:
            turn+=1;speaker=header[1];role='chair' if speaker=='PRESIDENTE' else 'speech'
            removed.append(dict(reason='etichetta dell’oratore',text=header.group()))
            text=text[header.end():]
        if node.get('id')=='navigator':reason='navigazione'
        elif node.get('id')=='votazioni_elettroniche':reason='tabulati delle votazioni'
        elif 'numeroPagina' in classes:reason='numero di pagina'
        elif 'presidenza' in classes or (before_first_section and classes & {'center','centerBold'}):reason='intestazione o presidenza'
        else:
            if classes & {'titolo','titolo_allegato','titolo_calendario','titolo_segnalazioni','titolo_votazioni'}:
                before_first_section=False;section=original;subsection='';turn+=1;speaker='';role='unknown'
                written='titolo_allegato' in classes and bool(re.search('intervent|dichiarazion',section,re.I))
                excluded_section=section_reason(section,classes)
                if re.match(r'(?i)(?:Modifica(?:zioni)? nella composizione|Annunzio|Annuncio|Assegnazione a Commission)',section):
                    excluded_section='comunicazioni amministrative interne'
                reason=excluded_section or 'titolo conservato nei metadati'
            elif 'sottotitolo' in classes:
                subsection=original;turn+=1
                reason=excluded_section or 'sottotitolo conservato nei metadati'
            else:
                reason=excluded_section
                if not reason:
                    text,parts=strip_procedures(text,role=='chair');removed.extend(parts)
                    if not text:reason='formula procedurale isolata'
        if node.name=='table' and not reason:
            text='\n'.join(' | '.join(plain(cell) for cell in row.find_all(['td','th'],recursive=False)) for row in node.find_all('tr')) or text
        entry=dict(block=index,source_id=node.get('id',''),section=section,subsection=subsection,
                   speaker=speaker,role=role,turn=turn,written=written,input_words=words(original),
                   words=words(text) if not reason else words(original),kept=not bool(reason),reason=reason)
        if reason:entry['text']=original
        else:
            entry.update(output_paragraph=len(output),sha256=hashlib.sha256(text.encode()).hexdigest(),
                         start=offset,end=offset+len(text),removed=removed)
            output.append(text);offset+=len(text)+2
        audit.append(entry)
    return '\n\n'.join(output),audit



def prepare(root=ROOT,workers=12,rebuild=False):
    """Compatibility API; use the staged, versioned builder for every entrypoint."""
    import fcntl
    from PySide6.QtCore import QLockFile
    from camera_rebuild import prepare as build
    root=Path(root).resolve()
    with (root/'.pipeline.lock').open('w') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        lock=QLockFile(str(root/'database/app.lock'));lock.setStaleLockTime(0)
        if not lock.tryLock(0):raise RuntimeError('Chiudere la Camera prima di rigenerare i testi')
        try:return build(root,workers,rebuild)
        finally:lock.unlock()


if __name__=='__main__':
    import sys
    from camera_rebuild import main
    sys.argv.insert(1,'--prepare');main()
