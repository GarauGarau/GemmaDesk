"""Whole procedural utterances, including short turns missed by HTML cleanup.

Every sentence must match: an announcement plus substantive speech is kept.
No speaker, topic word, short length, or missing category is an exclusion rule.
"""
import re

COMMON=[
 r'Chiedo di (?:parlare|intervenire)(?: (?:a favore|contro|per (?:un )?richiamo al Regolamento|sull.ordine dei lavori|per dichiarazione di voto))?',
 r'(?:Il )?(?:parere(?: del Governo)? (?:è )?|Governo (?:esprime |ha espresso ))?(?:parere )?conforme(?: (?:a quello (?:espresso )?|al |alla |del |della |dal |dalla ))?(?:(?:relatore|relatrice)(?: per la maggioranza)?|Governo)?',
 r'(?:Invito al ritiro(?:,? (?:altrimenti|o) parere contrario)?|Parere (?:contrario|favorevole)|Accolto(?: come raccomandazione)?)',
 r'(?:(?:Il Governo|Ci|Mi) (?:si )?rimett(?:e|iamo|o)|Rimesso) all.(?:Aula|Assemblea)',
 r'(?:Lo ritiro|Ritiro l.emendamento|Accantoniamo|Accetta la riformulazione|Accetto la riformulazione)',
 r'(?:Sull.ordine dei lavori|Per un richiamo al Regolamento)',
 r'(?:Sì,? |Si,? )?(?:anche )?(?:questo|l.|il) emendamento (?:è |viene )?ritirato',
 r'(?:Il parere(?: del Governo)? è |Parere )favorevole (?:su tutti gli impegni e sulle premesse|su tutti gli ordini del giorno)',
 r'(?:Sì,? |Si,? )?(?:accetto|accettiamo|accolgo|accogliamo) (?:la|questa|tale) riformulazione',
]
CHAIR=[
 r'(?:Il )?(?:Governo|relatore(?: di minoranza)?|relatrice|Commissione)',
 r'(?:Ne ha facoltà|Prego,? ne ha facoltà)(?:,? per (?:un|due|tre|cinque|dieci|[0-9]+) minut[oi])?',
 r'(?:Deve|Dovrebbe|Dovete|La prego di|La invito a|Invito a) (?:concludere|chiudere)',
 r'(?:Colleghi|Collega|Onorevole|Ministro|Presidente|Prego|Grazie|La ringrazio|Grazie a lei|Perfetto|Esatto|Esattamente|Bene|Va bene|Sì|Si|No)(?:,? (?:colleghi|per cortesia|per favore|onorevole|ministro|presidente|grazie))*',
 r'(?:Chiedo scusa|Mi scusi|Scusi)(?:,? (?:colleghi|per favore|onorevole))*',
 r'(?:Per favore,? )?(?:Per favore,? )?(?:un po. di |facciamo |chiedo )?silenzio(?: in Aula)?',
 r'Si rivolga alla Presidenza',
 r'La seduta è (?:ripresa|sospesa|tolta)',
 r'(?:È così|Sono così|Sono|È) (?:esaurit[oaie]|conclus[oaie]) (?:lo svolgimento delle (?:interpellanze urgenti|interrogazioni a risposta immediata)|gli interventi sulle questioni pregiudiziali|la discussione|l.esame degli ordini del giorno presentati)(?: all.ordine del giorno)?',
 r'Passiamo(?:,? (?:ora|dunque|quindi),?)? (?:agli interventi di fine seduta|alle dichiarazioni di voto(?: finale)?|agli interventi)(?: (?:sulla questione di fiducia )?(?:dei rappresentanti dei gruppi(?: e delle componenti politiche del gruppo Misto)?))?',
 r'Non vi sono altri iscritti a parlare e,? pertanto,? dichiaro chiusa la discussione(?: sulle linee generali)?',
 r'(?:Se nessun(?:o| altro) chiede di (?:parlare|intervenire)|Nessuno chiedendo di parlare),? (?:passiamo ai voti|lo pongo in votazione|invito (?:il relatore|la relatrice) ad esprimere il parere della Commissione)',
 r'Richiamo (?:gli oratori|tutti gli oratori) (?:al|ad un) rigoroso rispetto dei tempi(?:,? considerata la diretta televisiva in corso)?',
 r'Considerata la diretta televisiva in corso,? richiamo (?:gli oratori|tutti gli oratori) (?:al|ad un) rigoroso rispetto dei tempi',
 r'Ha chiesto di (?:parlare|intervenire|rispondere)(?:[^.!?]{0,180})',
 r'(?:Il deputato|La deputata|Il Ministro|La Ministra|Il Presidente del Consiglio dei ministri|Il sottosegretario|La sottosegretaria)[^.!?]{0,140} ha facoltà di (?:parlare|rispondere|intervenire)',
 r'(?:Prego|Grazie|La ringrazio),? (?:onorevole|deputato|deputata|ministro|ministra|presidente|collega|senatore)(?: (?-i:[A-ZÀ-Ü][\w’\x27-]*)){0,4}(?:,? (?:può procedere|a lei la parola|continui|prosegua|ne ha facoltà))?',
 r'Passiamo(?:,? (?:ora|dunque|quindi),?)? (?:all.emendamento|agli (?:identici |)emendamenti|all.ordine del giorno|agli ordini del giorno) [^.!?]{0,180}',
 r'(?:L.emendamento|L.ordine del giorno|Gli emendamenti|Gli ordini del giorno) [^.!?]{0,120} (?:è ritirato|sono ritirati|è stato ritirato|sono stati ritirati|è accantonato|sono accantonati|è assorbito|sono assorbiti|è precluso|sono preclusi)',
 r'(?:Viene|È stata|È) (?:accettata|accolta) la riformulazione',
 r'(?:Accetta|Accettate|Viene accettata|Accoglie) la riformulazione',
 r'(?:Invito|Chiedo a) (?:il relatore|la relatrice|il rappresentante del Governo|la rappresentante del Governo|il Governo|la Commissione)[^.!?]{0,80} (?:ad esprimere|di esprimere) (?:il|i) parer[ei]',
 r'(?:Prendo atto che|Prendo atto del fatto che) [^.!?]{0,130}(?:ha ritirato l.emendamento [^.!?]{0,60}|accetta la riformulazione|non insiste per la votazione)',
 r'(?:Il tempo|Il suo tempo|Il tempo a sua disposizione) è (?:scaduto|terminato|finito|esaurito)',
 r'(?:Ha|Avete|Le restano|Le rimangono|Mancano) (?:ancora )?(?:un|due|tre|cinque|dieci|venti|trenta|quaranta|cinquanta|[0-9]+) (?:secondi|minuti|minuto)(?: a disposizione)?',
]
COMMON=[re.compile(p,re.I) for p in COMMON];CHAIR=[re.compile(p,re.I) for p in CHAIR]


def reason(text,role):
    value=' '.join(text.split()).strip()
    value=re.sub(r'(?<=\d)\.(?=\d)','·',value)
    value=re.sub(r'\bn\.\s*(?=\d)','n ',value)
    if len(value)>3500:return ''
    sentences=re.split(r'(?<=[.!?…])\s+(?=[A-ZÀ-Ü])',value)
    for sentence in sentences:
        sentence=sentence.strip(' .!?…')
        if re.search(r'\b(?:ma|perché|ritengo|condanno|denuncio)\b',sentence,re.I):return ''
        # Vocatives only; never remove a clause containing an assertion.
        sentence=re.sub(r'^(?:Signor Presidente|Presidente|Collega|Colleghi|Onorevole|Ministro),\s*','',sentence,flags=re.I)
        if role=='chair':
            sentence=re.sub(r'^(?:Onorevole|Deputato|Deputata|Collega|Ministro) (?:[A-ZÀ-Ü][a-zà-ü]+(?:[- ][A-ZÀ-Ü][a-zà-ü]+){0,3}),\s*','',sentence)
        sentence=re.sub(r',\s*(?:Presidente|onorevole|Ministro)$','',sentence,flags=re.I)
        if any(p.fullmatch(sentence) for p in COMMON):continue
        if role=='chair' and any(p.fullmatch(sentence) for p in CHAIR):continue
        return ''
    return 'sole richieste di parola, pareri o conduzione senza contenuto tematico' if sentences else ''
