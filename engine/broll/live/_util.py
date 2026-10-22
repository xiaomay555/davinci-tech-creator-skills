"""Small deterministic data helpers shared by the offline core."""
import hashlib
import json


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False)+'\n').encode('utf-8')


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def union(ranges):
    result = []
    for start, end in sorted([list(r) for r in ranges]):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def intersects(a, b):
    return a[0] < b[1] and b[0] < a[1]


def subtract(interval, blocked):
    parts = [list(interval)]
    for a, b in union(blocked):
        next_parts = []
        for s, e in parts:
            if a >= e or b <= s:
                next_parts.append([s, e])
            else:
                if s < a:
                    next_parts.append([s, a])
                if b < e:
                    next_parts.append([b, e])
        parts = next_parts
    return parts
