"""Separate durable video index with atomic checkpoints and interval retrieval."""
from contextlib import contextmanager
from bisect import bisect_right
from pathlib import Path
import json
import sqlite3
import tempfile
import time
import numpy as np
from .common import ROOT, Cancelled, check, unit
from .media import (SUPPORTED, fingerprint, probe, blocks, extract_frames, prepare_audio,
                    read_audio, make_waveform)

PIPELINE = 'eg2-q8-av-768-audio20-overlap4-rgb672-v1'


def merge_hits(hits, gap=1.):
    """Merge temporally overlapping evidence; preserve disjoint occurrences."""
    intervals = []
    for hit in sorted(hits, key=lambda h: (h['start'], h['end'])):
        if intervals and hit['start'] <= intervals[-1]['end'] + gap:
            interval = intervals[-1]
            interval['end'] = max(interval['end'], hit['end'])
            interval['score'] = max(interval['score'], hit['score'])
            interval['modalities'] |= {hit['modality']}
        else:
            intervals.append(dict(start=hit['start'], end=hit['end'], score=hit['score'],
                                  modalities={hit['modality']}))
    return intervals


class Library:
    def __init__(self, data):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.cache = self.data / 'cache'
        self.cache.mkdir(exist_ok=True)
        self.db = self.data / 'video.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, duration REAL NOT NULL, has_audio INTEGER NOT NULL,
                    width INTEGER NOT NULL, height INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'in coda',
                    checkpoint REAL NOT NULL DEFAULT 0, frame_step REAL NOT NULL DEFAULT 4,
                    error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS segments (
                    id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                    modality TEXT NOT NULL CHECK(modality IN ('audio','frame')),
                    start REAL NOT NULL, end REAL NOT NULL, timestamp REAL NOT NULL,
                    vector BLOB NOT NULL, UNIQUE(video_id,modality,start));
                CREATE INDEX IF NOT EXISTS segments_video ON segments(video_id);
            ''')
            old = db.execute("SELECT value FROM metadata WHERE key='pipeline'").fetchone()
            if old and old[0] != PIPELINE:
                raise RuntimeError('Indice creato con una pipeline diversa. Usa una cartella dati nuova.')
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('pipeline',?)", (PIPELINE,))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def videos(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('''SELECT v.*, COUNT(s.id) AS vectors FROM videos v
                LEFT JOIN segments s ON s.video_id=v.id GROUP BY v.id ORDER BY v.name COLLATE NOCASE''')]

    def last_source(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='last_source'").fetchone()
        return row[0] if row else None

    def scan(self, source, stop, notify=lambda value: None):
        return self.add_paths([source], stop, notify)

    def add_paths(self, sources, stop, notify=lambda value: None):
        """Import explicit files/folders; duplicates at the same path are idempotent.

        An exact fingerprint and name can reconnect one missing source without
        discarding its completed work. Existing copies remain explicit library entries.
        """
        import os
        files = set()
        last_folder = None
        errors = []
        for source in sources:
            check(stop)
            if not str(source).strip():
                errors.append('Indica una cartella o un file video.')
                continue
            path = Path(str(source).strip()).expanduser()
            if not path.is_absolute():
                path = ROOT / path
            path = path.resolve()
            if path.is_dir():
                last_folder = path
                for folder, _, names in os.walk(path, onerror=lambda e: errors.append(str(e))):
                    check(stop)
                    for name in names:
                        file = Path(folder) / name
                        if file.suffix.lower() in SUPPORTED and file.is_file():
                            files.add(file.resolve())
            elif path.is_file():
                if path.suffix.lower() in SUPPORTED:
                    files.add(path)
                    last_folder = path.parent
                else:
                    errors.append(f'{path.name}: formato non supportato. Usa un file video.')
            else:
                errors.append(f'Percorso non disponibile: {path}')
        added = changed = unchanged = reconnected = 0
        ids = []
        files = sorted(files)
        for i, path in enumerate(files):
            check(stop)
            notify(dict(message=f'Lettura {i + 1}/{len(files)} · {path.name}'))
            try:
                sig = fingerprint(path)
                with self.connect() as db:
                    old = db.execute('SELECT * FROM videos WHERE path=?', (str(path),)).fetchone()
                if old and old['fingerprint'] == sig:
                    unchanged += 1
                    ids.append(old['id'])
                    continue
                info = probe(path, stop)
                with self.connect() as db:
                    if old:
                        db.execute('DELETE FROM segments WHERE video_id=?', (old['id'],))
                        db.execute('''UPDATE videos SET fingerprint=?,duration=?,has_audio=?,width=?,height=?,
                            status='in coda',checkpoint=0,error='' WHERE id=?''',
                            (sig, info['duration'], info['has_audio'], info['width'], info['height'], old['id']))
                        changed += 1
                        ident = old['id']
                    else:
                        candidates = [r for r in db.execute('SELECT * FROM videos WHERE name=? AND fingerprint=?',
                                      (path.name, sig)) if not Path(r['path']).is_file()]
                        if len(candidates) == 1:
                            previous = candidates[0]
                            ident = previous['id']
                            status = 'pronto' if previous['checkpoint'] >= previous['duration'] - 1e-6 else 'in coda'
                            db.execute("UPDATE videos SET path=?,status=?,error='' WHERE id=?", (str(path), status, ident))
                            reconnected += 1
                        else:
                            cursor = db.execute('''INSERT INTO videos(path,name,fingerprint,duration,has_audio,width,height)
                                VALUES (?,?,?,?,?,?,?)''', (str(path), path.name, sig, info['duration'],
                                info['has_audio'], info['width'], info['height']))
                            ident = cursor.lastrowid
                            # INTEGER PRIMARY KEY may reuse an ID after a deletion.
                            self.waveform_path(ident).unlink(missing_ok=True)
                            added += 1
                ids.append(ident)
                if old:
                    self.waveform_path(old['id']).unlink(missing_ok=True)
            except Cancelled:
                raise
            except Exception as e:
                errors.append(f'{path.name}: {e}')
        if last_folder is not None and files:
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('last_source',?)", (str(last_folder),))
        if not files:
            message = 'Nessun video trovato. Scegli i file oppure una cartella che contiene video.'
        else:
            message = (f'{added} video aggiunti · {unchanged} già presenti · {reconnected} ricollegati · '
                       f'{changed} modificati. Seleziona «Analizza selezionati» per elaborarli.')
        if errors:
            message += f' {len(errors)} errori: apri «Dettagli».'
        return dict(message=message, errors=errors, video_ids=ids, added=added, unchanged=unchanged,
                    reconnected=reconnected, changed=changed, source=str(last_folder) if last_folder else None)

    def remove(self, video_ids):
        """Remove selected library entries and derived data; never touch source videos."""
        ids = sorted({int(ident) for ident in video_ids})
        if not ids:
            return dict(message='Nessun video selezionato.', removed=0, errors=[])
        placeholders = ','.join('?' for _ in ids)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute(f'SELECT id FROM videos WHERE id IN ({placeholders})', ids).fetchall()
            for row in rows:
                self.waveform_path(row['id']).unlink(missing_ok=True)
            db.execute(f'DELETE FROM videos WHERE id IN ({placeholders})', ids)
        return dict(message=f'{len(rows)} video rimossi dall’archivio. I file originali sono conservati.',
                    removed=len(rows), errors=[])

    def waveform_path(self, video_id):
        return self.cache / f'{video_id}-wave.npz'

    def reset(self):
        with self.connect() as db:
            count = db.execute('SELECT COUNT(*) FROM segments').fetchone()[0]
            db.execute('DELETE FROM segments')
            db.execute("UPDATE videos SET checkpoint=0,status='in coda',error=''")
        return count

    def index(self, engine, step, stop, notify=lambda value: None, video_ids=None):
        if step not in (1., 2., 4., 8.):
            raise ValueError('Passo dei fotogrammi non valido.')
        videos = [v for v in self.videos() if v['checkpoint'] < v['duration'] - 1e-6
                  and (video_ids is None or v['id'] in video_ids)]
        if not videos:
            return dict(message='Tutti i video sono già indicizzati.', errors=[])
        engine.start(stop, lambda message: notify(dict(message=message)))
        total = sum(v['duration'] - v['checkpoint'] for v in videos)
        completed = 0.
        started = time.monotonic()
        errors = []
        for video in videos:
            check(stop)
            ident, path = video['id'], Path(video['path'])
            original_checkpoint = video['checkpoint']
            frame_step = video['frame_step'] if original_checkpoint else step
            try:
                if fingerprint(path) != video['fingerprint']:
                    raise RuntimeError('Il file è cambiato. Usa «Aggiungi percorso» per aggiornarlo prima di indicizzarlo.')
                visual_end = probe(path, stop)['video_duration']
                with self.connect() as db:
                    db.execute("UPDATE videos SET status='analisi',frame_step=?,error='' WHERE id=?", (frame_step, ident))
                notify(dict(message=f'{video["name"]} · preparazione audio e forma d’onda…'))
                with tempfile.TemporaryDirectory(prefix='gemmavideo-audio-') as folder:
                    wav = Path(folder) / 'audio.wav'
                    if video['has_audio']:
                        prepare_audio(path, wav, video['duration'], stop)
                        if not self.waveform_path(ident).exists():
                            make_waveform(wav, self.waveform_path(ident), video['duration'], stop)
                    for start, end, audio_end in blocks(video['duration'], original_checkpoint):
                        check(stop)
                        notify(dict(message=f'{video["name"]} · analisi da {start:.0f} s', video_id=ident))
                        rows = []
                        if video['has_audio']:
                            audio = read_audio(wav, start, audio_end)
                            if len(audio):
                                # Bound the stored interval by the samples actually present.
                                actual_end = min(audio_end, start + len(audio) / 16000)
                                rows.append((ident, 'audio', start, actual_end, (start + actual_end) / 2,
                                             engine.audio(audio, stop).tobytes()))
                        for left, right, timestamp, image in extract_frames(path, start, min(end, visual_end), frame_step, stop):
                            rows.append((ident, 'frame', left, right, timestamp, engine.image(image, stop).tobytes()))
                        check(stop)
                        if fingerprint(path) != video['fingerprint']:
                            raise RuntimeError('Il video è stato modificato durante l’analisi. Rileggi la cartella.')
                        with self.connect() as db:
                            db.executemany('''INSERT INTO segments(video_id,modality,start,end,timestamp,vector)
                                VALUES (?,?,?,?,?,?)''', rows)
                            db.execute('UPDATE videos SET checkpoint=?,status=? WHERE id=?',
                                       (end, 'pronto' if end >= video['duration'] - 1e-6 else 'analisi', ident))
                        completed += end - start
                        elapsed = time.monotonic() - started
                        notify(dict(message=f'{video["name"]} · {end:.0f}/{video["duration"]:.0f} s analizzati',
                                    fraction=completed / total, eta=elapsed * (total - completed) / completed,
                                    video_id=ident, checkpoint=end))
            except Cancelled:
                with self.connect() as db:
                    db.execute("UPDATE videos SET status='sospeso' WHERE id=? AND status!='pronto'", (ident,))
                raise
            except Exception as e:
                errors.append(f'{video["name"]}: {e}')
                with self.connect() as db:
                    db.execute("UPDATE videos SET status='errore',error=? WHERE id=?", (str(e), ident))
                notify(dict(message=errors[-1]))
                # A dead model cannot process later videos; preserve their queue state.
                if not engine.process or engine.process.poll() is not None:
                    break
        return dict(message='Analisi terminata.' if not errors else 'Analisi terminata con errori. Vedi i dettagli.', errors=errors)

    def search(self, query, mode='both', limit=30, minimum=.65, stop=None):
        if stop is not None:
            check(stop)
        query = unit(query)
        if mode not in {'both', 'audio', 'frame'}:
            raise ValueError('Modalità di ricerca non valida.')
        if not np.isfinite(minimum):
            raise ValueError('Soglia di ricerca non valida.')
        if limit <= 0:
            return []
        available = {}
        candidates, keys = [], []
        per_video = {}
        best = -float('inf')
        with self.connect() as db:
            cursor = db.execute('''SELECT s.*,v.path,v.name,v.duration,v.checkpoint,v.fingerprint,v.has_audio
                FROM segments s JOIN videos v ON v.id=s.video_id
                WHERE (?='both' OR s.modality=?) ORDER BY s.id''', (mode, mode))
            while True:
                if stop is not None:
                    check(stop)
                rows = cursor.fetchmany(1024)
                if not rows:
                    break
                for row in rows:
                    if row['path'] not in available:
                        if stop is not None:
                            check(stop)
                        available[row['path']] = Path(row['path']).is_file()
                rows = [row for row in rows if available[row['path']]]
                if not rows:
                    continue
                if any(len(row['vector']) != 768 * 4 for row in rows):
                    raise ValueError('Embedding archiviato non valido. Archivio conservato.')
                vectors = np.frombuffer(b''.join(row['vector'] for row in rows), dtype='<f4').reshape(-1, 768)
                if not np.isfinite(vectors).all():
                    raise ValueError('Embedding archiviato non valido. Archivio conservato.')
                scores = vectors @ query
                best = max(best, float(scores.max()))
                cutoff = max(minimum, best - .065)
                for index in np.flatnonzero(scores >= cutoff):
                    if stop is not None:
                        check(stop)
                    row, score = rows[index], float(scores[index])
                    ident = row['video_id']
                    key = (-score, row['id'])
                    # Keep the exact best 300, at most 80 per video. IDs break
                    # ties as in the original stable sort, across batch boundaries.
                    if len(keys) == 300 and key >= keys[-1]:
                        continue
                    if per_video.get(ident, 0) == 80:
                        last = next(i for i in range(len(candidates) - 1, -1, -1)
                                    if candidates[i]['video_id'] == ident)
                        if key >= keys[last]:
                            continue
                        keys.pop(last)
                        candidates.pop(last)
                        per_video[ident] -= 1
                    position = bisect_right(keys, key)
                    keys.insert(position, key)
                    # Retain result metadata only; release each batch of vectors.
                    hit = {name: row[name] for name in row.keys() if name != 'vector'}
                    candidates.insert(position, dict(hit, score=score))
                    per_video[ident] = per_video.get(ident, 0) + 1
                    if len(candidates) > 300:
                        keys.pop()
                        per_video[candidates.pop()['video_id']] -= 1
        if stop is not None:
            check(stop)
        # A later batch can raise the global cutoff.
        candidates = [hit for hit in candidates if hit['score'] >= max(minimum, best - .065)]
        groups = {}
        for row in candidates:
            ident = row['video_id']
            groups.setdefault(ident, dict(video_id=ident, path=row['path'], name=row['name'],
                duration=row['duration'], checkpoint=row['checkpoint'], fingerprint=row['fingerprint'],
                has_audio=row['has_audio'], hits=[]))['hits'].append(row)
        occurrences = []
        for group in groups.values():
            for interval in merge_hits(group.pop('hits')):
                occurrences.append((interval['score'], group['video_id'], interval))
            group['intervals'] = []
        for score, ident, interval in sorted(occurrences, key=lambda value: -value[0])[:limit]:
            groups[ident]['intervals'].append(interval)
        results = [group for group in groups.values() if group['intervals']]
        for group in results:
            group['score'] = max(x['score'] for x in group['intervals'])
            group['intervals'].sort(key=lambda interval: interval['start'])
            group['available'] = Path(group['path']).is_file()
        return sorted(results, key=lambda group: -group['score'])
