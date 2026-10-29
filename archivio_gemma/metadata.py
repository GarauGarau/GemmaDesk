"""Optional <document>.json sidecars for titles, dates and source URLs."""
import json
from pathlib import Path


def read_metadata(source, sha=None):
    source = Path(source)
    candidates = [source.with_suffix(source.suffix + '.json'),
                  source.parent.parent / 'metadati' / (source.stem + '.json')]
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if value.get('sha256') and sha and value['sha256'] != sha:
            continue
        result = {key: value[key] for key in ('title', 'date', 'url')
                  if isinstance(value.get(key), str)}
        if isinstance(value.get('words'), int) and value['words'] > 0:
            result['words'] = value['words']
        return result
    return {}
