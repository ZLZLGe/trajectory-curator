"""Rubric loader. configs/rubric.json is the single source of enums, bucket ids, ranges and weights."""
import hashlib
import json
import math
from pathlib import Path

TIME_DIM = 'expert_time'


def _is_int(x):
    return type(x) is int


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


class Rubric:
    def __init__(self, data, path=None, sha256=None):
        if not isinstance(data, dict):
            raise ValueError('rubric must be a JSON object')
        self.data = data
        self.path = str(path) if path is not None else None
        self.sha256 = sha256
        self.version = str(data.get('version', ''))
        self.max_demands = data.get('max_demands', 20)
        self.deliverables = data.get('deliverables')
        self.time_buckets = data.get('time_buckets')
        self.count_bins = data.get('count_bins', {})
        self.dimensions = data.get('dimensions')
        self.levels = data.get('levels')
        self._check()
        self.time_ids = [b['id'] for b in self.time_buckets]
        self.time_by_id = {b['id']: b for b in self.time_buckets}
        self.count_dims = list(self.count_bins)
        # dimensions the model scores directly on a 0..max integer scale
        self.scale_dims = [d for d in self.dimensions if d != TIME_DIM and d not in self.count_bins]
        self.difficulty_keys = list(self.dimensions) + ['rationale']

    def _check(self):
        if not _is_int(self.max_demands) or self.max_demands < 1:
            raise ValueError('rubric.max_demands must be an integer >= 1')
        d = self.deliverables
        if not isinstance(d, list) or not d or not all(isinstance(x, str) and x for x in d) or len(set(d)) != len(d):
            raise ValueError('rubric.deliverables must be a non-empty list of distinct strings')
        dims = self.dimensions
        if not isinstance(dims, dict) or TIME_DIM not in dims:
            raise ValueError(f'rubric.dimensions must be an object containing {TIME_DIM}')
        for name, spec in dims.items():
            if (not isinstance(spec, dict) or not _is_int(spec.get('max')) or spec['max'] < 1
                    or not _is_num(spec.get('weight')) or spec['weight'] <= 0):
                raise ValueError(f'rubric.dimensions.{name} needs integer max >= 1 and weight > 0')
        tb = self.time_buckets
        if not isinstance(tb, list) or not tb:
            raise ValueError('rubric.time_buckets must be a non-empty list')
        ids = []
        for b in tb:
            if (not isinstance(b, dict) or not isinstance(b.get('id'), str) or not b['id']
                    or not _is_num(b.get('minutes')) or b['minutes'] < 0
                    or not _is_int(b.get('score')) or not 0 <= b['score'] <= dims[TIME_DIM]['max']):
                raise ValueError('each time bucket needs id, minutes >= 0 and integer score in 0..dimensions.expert_time.max')
            ids.append(b['id'])
        if len(set(ids)) != len(ids):
            raise ValueError('rubric.time_buckets ids must be distinct')
        if not isinstance(self.count_bins, dict):
            raise ValueError('rubric.count_bins must be an object')
        for name, rows in self.count_bins.items():
            if name not in dims or name == TIME_DIM:
                raise ValueError(f'rubric.count_bins.{name} must name a non-time dimension')
            self._check_bins(name, rows, dims[name]['max'])
        lv = self.levels
        if not isinstance(lv, list) or not lv:
            raise ValueError('rubric.levels must be a non-empty list')
        prev_hi = 0
        for i, row in enumerate(lv):
            if (not isinstance(row, list) or len(row) != 3 or not _is_num(row[0]) or not _is_num(row[1])
                    or not _is_int(row[2])):
                raise ValueError('each level row must be [lo, hi, level]')
            if row[0] != prev_hi or row[1] <= row[0]:
                raise ValueError('rubric.levels must be contiguous [lo, hi) ranges starting at 0')
            prev_hi = row[1]
        if prev_hi < 100:
            raise ValueError('rubric.levels must cover up to 100')

    @staticmethod
    def _check_bins(name, rows, dim_max):
        if not isinstance(rows, list) or not rows:
            raise ValueError(f'rubric.count_bins.{name} must be a non-empty list')
        expected_lo = 0
        for i, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != 3:
                raise ValueError(f'rubric.count_bins.{name} rows must be [lo, hi_or_null, score]')
            lo, hi, score = row
            if not _is_int(lo) or lo != expected_lo:
                raise ValueError(f'rubric.count_bins.{name} must be contiguous starting at 0 (row {i})')
            if hi is None:
                if i != len(rows) - 1:
                    raise ValueError(f'rubric.count_bins.{name}: only the last row may have null hi')
            else:
                if not _is_int(hi) or hi < lo:
                    raise ValueError(f'rubric.count_bins.{name} row {i}: hi must be an integer >= lo')
                expected_lo = hi + 1
            if not _is_int(score) or not 0 <= score <= dim_max:
                raise ValueError(f'rubric.count_bins.{name} row {i}: score must be an integer in 0..{dim_max}')
        if rows[-1][1] is not None:
            raise ValueError(f'rubric.count_bins.{name}: last row must have null hi (open-ended)')


def load_rubric(path):
    path = Path(path)
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode('utf-8'))
    except ValueError as exc:
        raise ValueError(f'{path}: invalid JSON ({exc})') from None
    return Rubric(data, path=path, sha256=hashlib.sha256(raw).hexdigest())
