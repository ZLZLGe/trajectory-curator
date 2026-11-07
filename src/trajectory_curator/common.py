import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(2**20), b''):
            h.update(block)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    temp.replace(path)


def read_jsonl(path):
    with Path(path).open() as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def hf_token():
    if os.environ.get('HF_TOKEN'):
        return os.environ['HF_TOKEN'].strip()
    home = Path(os.environ.get('HF_HOME', Path.home() / '.cache/huggingface'))
    path = Path(os.environ.get('HF_TOKEN_PATH', home / 'token'))
    return path.read_text().strip() if path.is_file() else None


def safe_relative(path):
    if not isinstance(path, str) or not path or path.startswith('/') or '\\' in path:
        raise ValueError('unsafe_relative_path')
    if any(x in ('', '.', '..') for x in path.split('/')):
        raise ValueError('unsafe_relative_path')
    return path
