"""Compare real model throughput without modifying the user's archive."""
import json,statistics,tempfile,threading,time
from pathlib import Path
import numpy as np
from core import EnginePool,ROOT,check

def benchmark(data,backend,stop,notify):
    rows=[];baseline=None;inputs=[]
    with tempfile.TemporaryDirectory(prefix='gemmadesk-benchmark-') as tmp:
        for workers,batch in [(1,1),(1,2),(1,4),(2,2),(2,4),(1,8),(2,8)]:
            check(stop);label=f'{workers} worker · batch {batch}'
            notify('Prova prestazioni: '+label)
            engine=EnginePool(Path(tmp)/f'{workers}-{batch}',backend,workers,batch,text_only=True)
            try:
                engine.start(stop,notify)
                if not inputs:
                    # Fixed benchmark samples; production segmentation may now
                    # keep a whole essay in one input. Do not require 8 splits.
                    for path in sorted((ROOT/'examples/documents').glob('*.txt'))[:4]:
                        slug=path.stem;text=path.read_text(encoding='utf-8')
                        text=(text+'\n')*(12000//len(text)+1)
                        for offset in range(0,12000,3000):
                            inputs.append('title: '+slug+' | text: '+text[offset:offset+3000])
                    if len(inputs)!=16:raise ValueError('Servono sedici passaggi per il confronto.')
                def run():
                    return np.stack([v for i in range(0,len(inputs),engine.batch_size)
                                     for v in engine.encode_many(inputs[i:i+engine.batch_size],stop)])
                warm=run()
                if baseline is None:baseline=warm
                cosine=float(np.min(np.sum(baseline*warm,axis=1)))
                if cosine<.999:raise ValueError(f'Controllo equivalenza batch fallito: coseno {cosine:.6f}')
                elapsed=[]
                for _ in range(3):
                    check(stop);started=time.monotonic();run();elapsed.append(time.monotonic()-started)
                seconds=statistics.median(elapsed)
                rows.append(dict(workers=workers,batch_size=batch,seconds=seconds,
                                 samples_seconds=elapsed,embeddings_per_second=len(inputs)/seconds,
                                 min_cosine_vs_single=cosine,resources=engine.resources(),engine=engine.info))
                notify(f'{label}: {len(inputs)/seconds:.2f} embedding/s')
            finally:engine.close()
    # Prefer the simpler configuration unless the measured gain exceeds 5%.
    best=rows[0]
    for row in rows[1:]:
        if row['seconds']<best['seconds']*.95:best=row
    result=dict(backend=backend,inputs=len(inputs),repeats=3,rows=rows,
                selected=dict(workers=best['workers'],batch_size=best['batch_size']),
                speedup_vs_single=rows[0]['seconds']/best['seconds'],
                note='Mediana di 3 prove a caldo, stessi 16 passaggi; non misura lettura, caricamento o database.')
    path=Path(data)/('benchmark-'+backend+'.json');path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2));return result

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--backend',choices=['cpu','vulkan'],default='cpu')
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    result=benchmark(args.output,args.backend,threading.Event(),lambda s:print(s,flush=True))
    print(json.dumps(result['selected']),f"speedup {result['speedup_vs_single']:.2f}x")
