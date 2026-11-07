"""UTF-8, atomic and flush-safe file helpers (adapted from the sibling 分类 project)."""
import json
import os
from pathlib import Path
import tempfile


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
            name = f.name
            f.write(content.encode('utf-8') if isinstance(content, str) else content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o644)   # NamedTemporaryFile creates 0600; outputs are not secrets (the key is never written)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def strict_json(text):
    """json.loads that rejects duplicate keys and NaN/Infinity literals."""
    def pairs(values):
        obj = {}
        for key, value in values:
            if key in obj:
                raise ValueError(f'duplicate JSON key {key!r}')
            obj[key] = value
        return obj

    def constant(name):
        raise ValueError('nonfinite JSON number')

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def dumps(obj):
    """Compact UTF-8 JSON, key order preserved."""
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def append_jsonl(path, obj):
    """Append one JSON line with a single write + fsync so a crash leaves at most one partial last line."""
    data = (dumps(obj) + '\n').encode('utf-8')
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def repair_jsonl(path):
    """Drop a trailing partial line (crash mid-write) so later appends start on a fresh line."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open('rb+') as f:
        data = f.read()
        if data.endswith(b'\n'):
            return False
        cut = data.rfind(b'\n') + 1
        f.truncate(cut)
        f.flush()
        os.fsync(f.fileno())
    return True


def read_jsonl(path, tolerant=True):
    """Read a JSONL file. With tolerant=True an unparsable *last* line is ignored (interrupted write)."""
    path = Path(path)
    rows = []
    if not path.exists():
        return rows
    lines = path.read_text(encoding='utf-8').split('\n')
    while lines and not lines[-1].strip():
        lines.pop()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rows.append(strict_json(line))
        except ValueError:
            if tolerant and i == len(lines) - 1:
                continue
            raise ValueError(f'{path}: line {i + 1} is not valid JSON') from None
    return rows
