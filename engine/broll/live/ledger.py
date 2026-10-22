"""Portable ownership, adoption and atomic bookkeeping (design §8)."""
from copy import deepcopy
from pathlib import Path, PurePath
import os
import re
import tempfile
from ._util import encode, digest


def _identity(item):
    extra = item.get('broll_extra', {})
    name = item.get('name', '')
    prefix = re.match(r'^\[[^\]]+\]', name)
    # Legacy tags are shared by main/alternate/audio; keep the main/alternate
    # label in their name prefix. Future v3 tags are already run+spot unique.
    prefix_value = (name.split(' · ',1)[0] if re.match(r'^\[(?:B|D|R)\d{2}\]',name)
                    else prefix.group() if prefix else name or None)
    file_name = item.get('file_name') or PurePath(item.get('media_path') or '').name or None
    if prefix_value == file_name:
        prefix_value = None  # default clip name repeats the source signal
    return {'file_name': file_name,
            'size': item.get('size', extra.get('file_size')),
            'source_start': extra.get('left_offset') if extra.get('left_offset') is not None else item.get('source_start'),
            'prefix': item.get('prefix') or prefix_value}


def match_identity(placement, items):
    """Match at least two independent fields; IDs/path/mtime never decide identity.

    A source offset alone (commonly zero) does not identify a candidate. One
    filename/size/prefix signal is uncertain; multiple strong matches are too.
    """
    old = _identity(placement)
    strong, weak = [], []
    for index, item in enumerate(items):
        if placement.get('type') and placement['type'] != item.get('type'):
            continue
        cur = _identity(item)
        fields = [k for k in old if old[k] is not None and cur[k] is not None and old[k] == cur[k]]
        source = 'file_name' in fields and (old['size'] is None or cur['size'] is None or 'size' in fields)
        independent = 'source_start' in fields or 'prefix' in fields
        if source and independent or 'prefix' in fields and 'source_start' in fields:
            strong.append((index, fields))
        elif source and not independent and ((old['size'] is not None and cur['size'] is not None)
             or (old['prefix'] and cur['prefix'] is None and old['source_start'] is not None and cur['source_start'] is not None)):
            continue  # same file, explicitly another cut/name; not an AI placement
        elif fields and fields != ['source_start']:
            weak.append((index, fields))
    # A direct source may appear many times. Shared filename+size must not
    # outweigh a unique matching name prefix plus source evidence. Never use
    # SDK IDs or record position to break ties.
    if len(strong) > 1:
        anchored = [(i, fields) for i, fields in strong
                    if 'prefix' in fields and 'file_name' in fields]
        if anchored:
            best = max(len(fields) for _, fields in anchored)
            winners = [(i, fields) for i, fields in anchored if len(fields) == best]
            if len(winners) == 1:
                return {'status': 'matched', 'index': winners[0][0], 'fields': winners[0][1]}
    if len(strong) == 1:
        return {'status': 'matched', 'index': strong[0][0], 'fields': strong[0][1]}
    if strong or weak:
        return {'status': 'uncertain', 'candidates': [i for i, _ in strong+weak]}
    return {'status': 'missing', 'candidates': []}


def main_picture(entry):
    """Legacy muted audio/alternates are bookkeeping, not visible B-roll spots."""
    old = entry.get('S0', entry.get('placement', {}))
    return (old.get('type') == 'video' and old.get('role') != 'overlay'
            and not ('备选' in old.get('track_name', '') and not old.get('enabled')))


def exact_state(item):
    """Deletion guard: exact recorded fields; no one-frame/numeric tolerance."""
    return ({k: deepcopy(item.get(k)) for k in
            ('name', 'type', 'track', 'track_name', 'start', 'end', 'duration',
             'source_start', 'source_end', 'enabled', 'props')}
           | {'identity': _identity(item), 'broll_extra': {k: deepcopy(v) for k, v in item.get('broll_extra', {}).items()
                                                       if k not in ('media_id', 'raw_source_end')}})


def learning_allowed(timeline_name):
    """Rejected sentence version, archived/history/review copies default to read-only."""
    return not bool(re.search(r'逐句校对|_archived_v\d+|审核|审片|历史|(?:^|[_ /])history(?:[_ /]|$)',
                              timeline_name, flags=re.I))


def new_ledger(model, creator, first_run, project=None, database=None):
    """Create one lineage from first run and the stable V1 source fingerprint."""
    if not creator or not first_run:
        raise ValueError('创作者和首次 run id 不能为空')
    return {'schema_version': 3, 'creator': creator,
            'project': project or model['snapshot'].get('project'), 'database': database,
            'timeline': model['snapshot']['name'], 'first_run': first_run,
            'v1_source_fingerprint': model['v1_source_fingerprint'],
            'lineage': first_run+'-'+model['v1_source_fingerprint'][:16],
            'runs': [], 'spots': {}, 'reserved': [], 'S0': deepcopy(model['snapshot'])}


def adopt(model, creator, first_run, project=None, database=None):
    """Register every legacy video/audio clip; snapshot S0 is current read-back."""
    from .timeline import record_to_source
    ledger = new_ledger(model, creator, first_run, project, database)
    counts = {}
    for item in model['items']:
        if item['owner'] != 'ai_legacy':
            continue
        prefix = _identity(item)['prefix'] or 'legacy'
        base = f'{prefix}:{item["type"]}:{item["track"]}'
        counts[base] = counts.get(base, 0)+1
        spot = base+':'+str(counts[base])
        lines = [l for l in model['lines'] if l['start'] < item['end'] and item['start'] < l['end']]
        ledger['spots'][spot] = {'origin': 'ai_legacy', 'semantic': {
            'text': ''.join(l['text'] for l in lines),
            'source_ranges': record_to_source(model, item['start'], item['end'])},
            'placement': deepcopy(item), 'S0': deepcopy(item), 'status': 'unreviewed',
            'candidates': [], 'removed': []}
    ledger['runs'].append({'id': first_run, 'kind': 'adopt', 'spots': sorted(ledger['spots'])})
    return ledger


def resolve_lineage(ledger, models, selected=None):
    """Choose a copy by source lineage; two modified copies require a selection."""
    candidates = [m for m in models if learning_allowed(m['snapshot']['name'])
                  and m['v1_source_fingerprint'] == ledger['v1_source_fingerprint']]
    if selected:
        candidates = [m for m in candidates if m['snapshot']['name'] == selected]
    if not candidates:
        raise ValueError('没有同谱系时间线，V1口播源已变化或指定时间线不可学习')
    if len(candidates)>1 and not selected:
        from .learn import diff_snapshots
        changed=[]
        for model in candidates:
            delta=diff_snapshots(ledger['S0'],model['snapshot'],ledger)
            if delta['actions'] or delta['raw_changes'] or delta['aroll_reedit'] or delta['uncertain']:changed.append(model)
        if len(changed)==1:return changed[0]
        if not changed:
            return next((m for m in candidates if m['snapshot']['name']==ledger.get('timeline')),candidates[0])
    if len(candidates) != 1:
        raise ValueError('同谱系时间线不唯一，请指定学习哪条')
    return candidates[0]


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(encode(value)); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_ledger(path, ledger):
    """Atomically replace one B-roll台账.json; caller owns work-directory choice."""
    _atomic_json(path, ledger)


def load_ledger(path):
    import json
    return json.loads(Path(path).read_text(encoding='utf-8'))
