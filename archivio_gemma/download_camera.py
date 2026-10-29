"""Archivio riprendibile dei resoconti ufficiali dell'Assemblea della Camera.

Gli stadi producono configurazioni curl: nessun download implicito e nessuna
inferenza. Originali, testi derivati e database restano nella cartella dedicata.
"""
from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1] / 'data/camera_deputati'
START = '2016-10-28'
END = '2026-10-27'
MONTHS = 'gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre'.split()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config(root, name, jobs):
    lines = []
    for url, path in jobs:
        if urlparse(url).hostname not in {'www.camera.it', 'documenti.camera.it'}:
            raise ValueError('Fonte non Camera: ' + url)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines.extend(['url = ' + json.dumps(url), 'output = ' + json.dumps(str(path.resolve()))])
    (root / name).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'{name}: {len(jobs)} richieste', flush=True)


def indices(root, start, end):
    months = []
    for leg, first, last in [(17, (2016, 10), (2018, 3)), (18, (2018, 3), (2022, 10)),
                             (19, (2022, 10), (int(end[:4]), int(end[5:7])))]:
        y, m = first
        while (y, m) <= last:
            if start[:7] <= f'{y}-{m:02}' <= end[:7]:
                months.append(dict(legislatura=leg, year=y, month=m,
                    url=f'https://www.camera.it/leg{leg}/207?annomese={y},{m}',
                    file=f'indici/leg{leg}_{y}-{m:02}.html'))
            y, m = (y+1, 1) if m == 12 else (y, m+1)
    save(root / 'mesi.json', months)
    config(root, 'indici.curl', [(i['url'], root / i['file']) for i in months])


def catalog(root, start, end):
    entries, deferred, monthly = {}, [], []
    resolutions_path = root / 'risoluzioni_indice.json'
    resolutions = json.loads(resolutions_path.read_text()) if resolutions_path.exists() else {}
    for month in json.loads((root / 'mesi.json').read_text()):
        path = root / month['file']
        soup = BeautifulSoup(path.read_bytes(), 'html.parser')
        items = soup.select('li.titoloSeduta')
        if not items and 'Le informazioni saranno pubblicate appena disponibili' not in soup.get_text():
            raise ValueError(f'Indice vuoto o non riconosciuto: {path}')
        monthly.append(dict(**month, sedute=len(items), sha256=sha(path)))
        for node in items:
            heading = node.select_one('.numero-resoconto-assemblea')
            match = re.search(r'Seduta n\.\s*(\d+)', heading.get_text(' ', strip=True))
            if not match:
                raise ValueError(f'Numero seduta non riconosciuto: {path}')
            number = int(match[1])
            odg = node.select_one('a.eleres_odg[href]')
            query = parse_qs(urlparse(odg['href']).query) if odg else {}
            if 'slGiorno' not in query or 'slAnnoMese' not in query:
                raise ValueError(f'Data non riconosciuta: {path}, seduta {number}')
            ymonth = query['slAnnoMese'][0]
            date = f'{ymonth[:4]}-{ymonth[4:6]}-{int(query["slGiorno"][0]):02}'
            dt.date.fromisoformat(date)
            if date[:7] != f'{month["year"]}-{month["month"]:02}':
                raise ValueError(f'Indice reindirizzato a un altro mese: {path}, {date}')
            if not start <= date <= end:
                continue
            leg = month['legislatura']
            slug = f'{date}_leg{leg}_sed{number:04}'
            title = f'Camera · {date} · Legislatura {leg} · Seduta {number}'
            pdfs = {urljoin(month['url'], a['href']).replace('http:', 'https:', 1)
                    for a in node.select('a.eleres_pdf[href]')}
            info = dict(slug=slug, title=title, date=date, legislatura=leg, seduta=number,
                        index_url=month['url'])
            resolution = resolutions.get(f'{leg}/{number}')
            if not pdfs and resolution:
                pdfs.add(resolution['pdf_url'])
                info['index_resolution'] = resolution['evidence']
            if not pdfs:
                deferred.append(dict(**info, reason='PDF definitivo non presente nell’indice ufficiale'))
                continue
            if len(pdfs) != 1:
                raise ValueError(f'Più PDF stenografici: {slug}: {pdfs}')
            pdf_url = pdfs.pop()
            if not pdf_url.endswith('/stenografico.pdf'):
                raise ValueError('PDF inatteso: ' + pdf_url)
            info.update(pdf_url=pdf_url, html_url=pdf_url[:-3] + 'htm',
                        pdf=f'pdf/{date[:4]}/{slug}.pdf', original=f'originali_html/{date[:4]}/{slug}.html',
                        text=f'testi/{slug}.txt', metadata=f'metadati/{slug}.json')
            key = (leg, number)
            if key in entries and entries[key] != info:
                raise ValueError('Seduta duplicata discordante: ' + slug)
            entries[key] = info
    records = sorted(entries.values(), key=lambda r: (r['date'], r['legislatura'], r['seduta']))
    gaps = {}
    for leg in sorted({r['legislatura'] for r in records}):
        numbers = {r['seduta'] for r in records + deferred if r['legislatura'] == leg}
        gaps[str(leg)] = sorted(set(range(min(numbers), max(numbers)+1)) - numbers)
    result = dict(start=start, end=end, scope='Assemblea; solo resoconto stenografico, senza allegati A/B separati',
        cataloged_at=dt.datetime.now(dt.timezone.utc).isoformat(), months=monthly, documents=records,
        deferred=deferred, sequence_gaps=gaps, by_year=dict(sorted(Counter(r['date'][:4] for r in records).items())))
    save(root / 'catalogo.json', result)
    print(json.dumps(dict(documents=len(records), deferred=deferred, sequence_gaps=gaps,
                          by_year=result['by_year']), ensure_ascii=False, indent=2), flush=True)


def parse_document(path, entry):
    soup = BeautifulSoup(path.read_bytes(), 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    expected = re.search(r'Sed\. n\.\s*(\d+).*?(\d{1,2})[°º]?\s+(' + '|'.join(MONTHS) + r')\s+(\d{4})', title, re.I)
    if not expected or int(expected[1]) != entry['seduta']:
        raise ValueError('Identità HTML non valida: ' + str(path))
    date = f'{expected[4]}-{MONTHS.index(expected[3].lower())+1:02}-{int(expected[2]):02}'
    if date != entry['date'] or not re.search(rf'\b{entry["legislatura"]}\^?\s*Legislatura', title, re.I):
        raise ValueError('Data/legislatura HTML discordante: ' + title)
    body = soup.select_one('#wrapper')
    if body is None or not re.search(r'La seduta\s+(comincia|è aperta)|Inizio alle', body.get_text(' ', strip=True)):
        raise ValueError('Corpo stenografico non riconosciuto: ' + str(path))
    for node in body.select('#navigator, script, style, noscript'):
        node.decompose()
    page_markers = len(body.select('.numeroPagina'))
    for node in body.find_all('br'):
        node.replace_with('\n')
    for node in body.select('.numeroPagina'):
        node.insert_before('\n\n')
        node.insert_after('\n\n')
    for node in body.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'tr']):
        node.insert_before('\n\n')
        node.insert_after('\n\n')
    for node in body.find_all(['td', 'th']):
        node.insert_after(' | ')
    raw = body.get_text().replace('\ufeff', '').replace('\u200b', '').replace('\xa0', ' ')
    paragraphs = [' '.join(p.split()) for p in re.split(r'\n\s*\n', raw)]
    text = '\n\n'.join(p for p in paragraphs if p)
    if len(text) < 100:
        raise ValueError('Testo troppo breve: ' + str(path))
    return text, page_markers


def valid_pdf(path):
    if not path.is_file() or path.stat().st_size < 100:
        return False
    with path.open('rb') as stream:
        if stream.read(5) != b'%PDF-':
            return False
        stream.seek(max(0, path.stat().st_size - 2048))
        return b'%%EOF' in stream.read()


def download_plan(root):
    catalog = json.loads((root / 'catalogo.json').read_text())
    jobs = []
    for entry in catalog['documents']:
        for key, urlkey in [('original', 'html_url'), ('pdf', 'pdf_url')]:
            final = root / entry[key]
            part = final.with_suffix(final.suffix + '.part')
            for candidate in [final, part]:
                try:
                    if key == 'pdf':
                        if not valid_pdf(candidate):
                            continue
                    else:
                        parse_document(candidate, entry)
                    if candidate == part:
                        part.replace(final)
                    break
                except (OSError, ValueError):
                    continue
            else:
                jobs.append((entry[urlkey], part))
    config(root, 'documenti.curl', jobs)


def prepare(root):
    database=(root / 'database/archivio.sqlite3').resolve()
    if database.exists():
        with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as db:
            if db.execute("SELECT 1 FROM meta WHERE key='camera_preprocessing'").fetchone():
                raise RuntimeError('Questo database usa la versione tematica. Gli integrali non vengono reimportati. Per rigenerare i testi tematici usare camera_preprocess.py prima di iniziare l’indicizzazione.')
    catalog = json.loads((root / 'catalogo.json').read_text())
    documents, errors = [], []
    for entry in catalog['documents']:
        try:
            parsed = None
            for key in ['original', 'pdf']:
                final = root / entry[key]
                part = final.with_suffix(final.suffix + '.part')
                if part.exists():
                    if key == 'pdf':
                        if not valid_pdf(part):
                            raise ValueError('PDF incompleto: ' + str(part))
                    else:
                        parsed = parse_document(part, entry)
                    part.replace(final)
            if not valid_pdf(root / entry['pdf']):
                raise ValueError('PDF mancante/non valido')
            info = subprocess.run(['pdfinfo', str(root / entry['pdf'])], capture_output=True,
                                  text=True, timeout=30, env={**os.environ, 'LC_ALL': 'C'})
            pdf_pages = re.search(r'^Pages:\s+(\d+)', info.stdout, re.M)
            if info.returncode or not pdf_pages:
                raise ValueError('PDF non leggibile: ' + info.stderr[:300])
            body, pages = parsed if parsed is not None else parse_document(root / entry['original'], entry)
            text = entry['title'] + '\nFonte: ' + entry['html_url'] + '\n\n' + body + '\n'
            target = root / entry['text']
            target.parent.mkdir(exist_ok=True)
            target.write_text(text, encoding='utf-8')
            record = dict(**entry, characters=len(body), words=len(body.split()), page_markers=pages,
                pdf_pages=int(pdf_pages[1]),
                text_sha256=sha(target), html_sha256=sha(root / entry['original']),
                pdf_sha256=sha(root / entry['pdf']), pdf_bytes=(root / entry['pdf']).stat().st_size,
                html_bytes=(root / entry['original']).stat().st_size, text_bytes=target.stat().st_size,
                extraction='HTML ufficiale integrale: rimossi solo navigazione, script e stile; conservati paragrafi, oratori, pagine e tabelle finali.',
                prepared_at=dt.datetime.now(dt.timezone.utc).isoformat())
            save(root / entry['metadata'], record)
            documents.append(record)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            errors.append(dict(slug=entry['slug'], error=str(exc)))
        if (len(documents) + len(errors)) % 100 == 0:
            print(f'{len(documents)} preparati; {len(errors)} errori', flush=True)
    summary = dict(start=catalog['start'], end=catalog['end'], expected=len(catalog['documents']),
                   prepared=len(documents), errors=errors, deferred=catalog['deferred'],
                   by_year=dict(sorted(Counter(r['date'][:4] for r in documents).items())),
                   totals={k: sum(r[k] for r in documents) for k in ['words', 'characters', 'page_markers', 'pdf_pages', 'pdf_bytes', 'html_bytes', 'text_bytes']})
    save(root / 'rapporto.json', summary)
    if errors:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        raise RuntimeError('Preparazione incompleta. Ripetere downloads e curl per recuperare i file mancanti.')
    # Only the explicitly isolated database is ever opened for writing.
    from core import Archive
    archive = Archive(root / 'database')
    result = archive.add([root / r['text'] for r in documents])
    if result['rejected']:
        raise RuntimeError(json.dumps(result['rejected'], ensure_ascii=False))
    with archive.connect() as db:
        for r in documents:
            db.execute('UPDATE documents SET name=?,title=?,url=? WHERE source=?',
                       (r['title'], r['title'], r['html_url'], str((root / r['text']).resolve())))
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('collection', 'Camera dei deputati · Assemblea'))
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('source_catalog', str((root / 'catalogo.json').resolve())))
        summary['database_documents'] = db.execute('SELECT count(*) FROM documents').fetchone()[0]
        summary['database_chunks'] = db.execute('SELECT count(*) FROM chunks').fetchone()[0]
        summary['database_integrity'] = db.execute('PRAGMA integrity_check').fetchone()[0]
    summary['database_added'] = len(result['added'])
    summary['database_already_present'] = len(result['duplicates'])
    save(root / 'rapporto.json', summary)
    rows = ''.join(f'<tr><td>{r["date"]}</td><td>{r["legislatura"]}</td><td>{r["seduta"]}</td>'
                   f'<td>{r["words"]:,}</td><td><a href="{r["text"]}">Testo</a> · '
                   f'<a href="{r["pdf"]}">PDF</a> · <a href="{r["original"]}">HTML</a> · '
                   f'<a href="{html.escape(r["html_url"], quote=True)}">Fonte</a></td></tr>' for r in documents)
    (root / 'index.html').write_text('<!doctype html><html lang="it"><meta charset="utf-8">'
        '<title>Camera dei deputati · Assemblea</title><style>body{font:16px sans-serif;margin:3em;'
        'background:#f6f3e9;color:#25333c}table{border-collapse:collapse;width:100%}td,th{padding:9px;'
        'text-align:left;border-bottom:1px solid #ccc}a{color:#3e7079}</style>'
        f'<h1>Camera dei deputati · Assemblea</h1><p>{len(documents)} resoconti stenografici definitivi. '
        f'Intervallo richiesto: {catalog["start"]} – {catalog["end"]}. '
        f'{len(catalog["deferred"])} sedute in attesa del PDF definitivo al momento del censimento.</p>'
        '<p>Testi pronti nel database separato di GemmaDesk. Gli embedding devono ancora essere calcolati. '
        'Sono esclusi commissioni, sommari e allegati A/B separati.</p>'
        '<table><tr><th>Data</th><th>Legislatura</th><th>Seduta</th><th>Parole</th><th>Documenti</th></tr>'
        + rows + '</table></html>', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['indices', 'catalog', 'downloads', 'prepare'])
    parser.add_argument('--directory', type=Path, default=ROOT)
    parser.add_argument('--start', default=START)
    parser.add_argument('--end', default=END)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    if args.stage == 'indices':
        indices(args.directory, args.start, args.end)
    elif args.stage == 'catalog':
        catalog(args.directory, args.start, args.end)
    elif args.stage == 'downloads':
        download_plan(args.directory)
    else:
        prepare(args.directory)


if __name__ == '__main__':
    main()
