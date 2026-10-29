"""Download the complete official Essays index, with provenance and resumable files."""
from __future__ import annotations
import argparse,concurrent.futures,datetime,hashlib,html,json,re,subprocess,time
from pathlib import Path
from urllib.parse import urljoin,urlparse
from bs4 import BeautifulSoup

INDEX='https://paulgraham.com/articles.html'
ROOT=Path(__file__).resolve().parents[1]/'data'/'paul_graham'

def fetch(url,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    part=target.with_suffix(target.suffix+'.part')
    result=subprocess.run(['curl','-fL','--retry','3','--retry-delay','2','--connect-timeout','15','--max-time','60',
                           '--silent','--show-error','--user-agent','GemmaDesk-personal-archive/1.0',url,'-o',str(part)],capture_output=True,text=True)
    if result.returncode:part.unlink(missing_ok=True);raise RuntimeError(result.stderr.strip())
    if not part.stat().st_size:raise ValueError('Empty download: '+url)
    part.replace(target)

def extract(raw,title):
    # HTML5 repairs the site's old unclosed <br>/<font> markup like a browser.
    # html.parser can nest the whole essay inside a <br>, losing it on replace.
    soup=BeautifulSoup(raw,'html5lib')
    for node in soup(['script','style','noscript','nav','form']):node.decompose()
    # Some essays close/reopen the outer font halfway through (e.g. growth,
    # langdes). Select the containing column, retaining all notes and code.
    candidates=soup.find_all('article') or soup.find_all('td') or [soup]
    body=max(candidates,key=lambda n:len(n.get_text()))
    # Preserve code indentation while joining physical HTML source line wraps.
    code=[]
    for pre in body.find_all('pre'):
        code.append(pre.get_text());pre.replace_with(f'\n\nGEMMADESKCODE{len(code)-1}\n\n')
    for br in body.find_all('br'):br.replace_with('\n')
    for block in body.find_all(['p','div','h1','h2','h3','h4','li','blockquote']):
        block.insert_before('\n\n');block.insert_after('\n\n')
    text=body.get_text().replace('\r','').replace('\xa0',' ')
    paragraphs=[' '.join(part.split()) for part in re.split(r'\n\s*\n',text)]
    text='\n\n'.join(p for p in paragraphs if p)
    for i,value in enumerate(code):text=text.replace(f'GEMMADESKCODE{i}',value.strip('\n'))
    text=re.sub(r'\n{3,}','\n\n',text).strip()
    # Keep notes, acknowledgments, quotations and code. No bibliography filtering.
    if len(text)<40:raise ValueError('No substantial text found: '+title)
    date=re.search(r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December|Spring|Summer|Fall|Winter)\s+\d{4}\b',text[:1200])
    links=[dict(url=urljoin(INDEX,a['href']),title=a.get_text(' ',strip=True)) for a in soup.select('a[href]')]
    return text,date.group() if date else '',links

def download(root=ROOT,workers=2,offline=False,prepare=False):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    if not offline and not prepare:
        fetch(INDEX,root/'articles.html');fetch('https://paulgraham.com/robots.txt',root/'robots.txt')
    soup=BeautifulSoup((root/'articles.html').read_bytes(),'html.parser');entries={}
    for a in soup.select('a[href]'):
        title=a.get_text(' ',strip=True);url=urljoin(INDEX,a['href']);parsed=urlparse(url)
        if not title or parsed.path.endswith('/rss.html'):continue
        if parsed.hostname not in {'paulgraham.com','www.paulgraham.com','sep.turbifycdn.com'}:continue
        entries.setdefault(url,title)
    if len(entries)<200:raise ValueError('Official index unexpectedly small; refusing an incomplete collection.')
    if prepare:
        config=[];(root/'originali').mkdir(exist_ok=True)
        for url in entries:
            source=Path(urlparse(url).path);target=(root/'originali'/source.name).resolve()
            if target.exists() and target.stat().st_size:continue
            config.extend(['url = '+json.dumps(url),'output = '+json.dumps(str(target))])
        (root/'download.curl').write_text('\n'.join(config)+'\n')
        print(f'{len(entries)} testi; {len(config)//2} download da completare',flush=True)
        return
    def one(pair):
        url,title=pair;slug=Path(urlparse(url).path).stem
        suffix=Path(urlparse(url).path).suffix.lower();raw=root/'originali'/(slug+suffix)
        if not raw.exists():
            if offline:raise FileNotFoundError(raw)
            fetch(url,raw);time.sleep(.25)
        payload=raw.read_bytes()
        if suffix=='.txt':
            text=payload.decode('utf-8');date='';links=[]
        else:text,date,links=extract(payload,title)
        fulltext={'rootsoflisp':'jmc.ps','lwba':'bbnexcerpts.txt'}.get(slug)
        full_source=None
        if fulltext:
            full_source=next(link['url'] for link in links if '/'+fulltext in link['url'])
            asset=root/'originali'/fulltext
            if not asset.exists():
                if offline:raise FileNotFoundError(asset)
                fetch(full_source,asset)
            if asset.suffix=='.ps':
                pdf=asset.with_suffix('.pdf');plain=asset.with_suffix('.txt')
                subprocess.run(['ps2pdf','-dSAFER',str(asset),str(pdf)],check=True,capture_output=True)
                subprocess.run(['pdftotext','-enc','UTF-8','-nopgbrk',str(pdf),str(plain)],check=True,capture_output=True)
                text=plain.read_text(encoding='utf-8')
            else:text=asset.read_text(encoding='utf-8')
        target=root/'testi'/(slug+'.txt');target.parent.mkdir(exist_ok=True)
        target.write_text(title+'\n\n'+text+'\n',encoding='utf-8')
        item=dict(slug=slug,title=title,url=url,date=date,original=str(raw.relative_to(root)),
                  text=str(target.relative_to(root)),characters=len(text),words=len(text.split()),
                  sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                  downloaded_at=datetime.datetime.fromtimestamp(raw.stat().st_mtime,datetime.timezone.utc).isoformat(),links=links)
        if full_source:item.update(full_text_url=full_source,full_text_original='originali/'+fulltext)
        meta=root/'metadati'/(slug+'.json');meta.parent.mkdir(exist_ok=True);meta.write_text(json.dumps(item,ensure_ascii=False,indent=2))
        return item
    results=[];errors=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(one,pair):pair for pair in entries.items()}
        for future in concurrent.futures.as_completed(futures):
            url,title=futures[future]
            try:results.append(future.result())
            except Exception as e:errors.append(dict(url=url,title=title,error=str(e)))
            if (len(results)+len(errors))%20==0:print(f'{len(results)} scaricati; {len(errors)} errori; {len(entries)} totali',flush=True)
    rank={url:i for i,url in enumerate(entries)};results.sort(key=lambda r:rank[r['url']])
    index=dict(source=INDEX,fetched_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
               indexed_links=len(entries),downloaded=len(results),errors=errors,essays=results)
    (root/'indice.json').write_text(json.dumps(index,ensure_ascii=False,indent=2))
    rows=''.join(f'<tr><td>{html.escape(r["title"])}</td><td>{html.escape(r["date"])}</td><td>{r["words"]:,}</td><td><a href="{r["text"]}">Testo</a> · <a href="{r["original"]}">Originale locale</a> · <a href="{html.escape(r["url"],quote=True)}">Sito</a></td></tr>' for r in results)
    (root/'index.html').write_text('<!doctype html><html lang="it"><meta charset="utf-8"><title>Paul Graham · raccolta locale</title><style>body{background:#e9e5da;color:#25333c;font:16px sans-serif;margin:36px}table{border-collapse:collapse;width:100%;background:#f6f3e9}td,th{padding:12px;border-bottom:1px solid #c1c2b7;text-align:left}a{color:#3e7079}</style><h1>Paul Graham · raccolta locale</h1><p>'+str(len(results))+' testi dall’indice ufficiale Essays. Note e ringraziamenti conservati.</p><table><tr><th>Titolo</th><th>Data nel testo</th><th>Parole</th><th>Apri</th></tr>'+rows+'</table></html>')
    print(json.dumps({k:v for k,v in index.items() if k!='essays'},ensure_ascii=False,indent=2),flush=True)
    if errors:raise RuntimeError(f'{len(errors)} downloads incomplete; rerun to resume.')
    return index

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--directory',type=Path,default=ROOT)
    parser.add_argument('--offline',action='store_true');parser.add_argument('--prepare',action='store_true')
    args=parser.parse_args();download(args.directory,offline=args.offline,prepare=args.prepare)
