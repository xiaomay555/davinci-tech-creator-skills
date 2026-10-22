"""Read-only candidate selection; callers supply pool and folder inventories."""
from copy import deepcopy
from fractions import Fraction
from pathlib import PurePath
from math import ceil, floor
from ._util import subtract
from .timeline import build_model, _AI_PATH


def frame_rate(value):
    """Accept SDK decimals, fractions or [numerator, denominator]."""
    if isinstance(value, (list, tuple)):
        rate = Fraction(*value)
    else:
        rate = Fraction(str(value))
    # SDK rounds NTSC rates to two decimals.
    for nominal, exact in ((29.97, Fraction(30000, 1001)), (59.94, Fraction(60000, 1001))):
        if abs(float(rate)-nominal) < 0.0001:
            return exact
    if rate <= 0:
        raise ValueError('素材帧率无效')
    return rate


def collect_candidates(snapshot, media_pool, profile=None, *, folder_files=(), folders=()):
    """Return candidate metadata with available_ranges in seconds, never stat/scan.

    folder_files is a supplied inventory; only explicitly named/profile folders
    enter the list. Used user-picture slices are removed with 0.5 s guards.
    """
    profile = profile or {}
    model = snapshot if 'v1_map' in snapshot else build_model(snapshot)
    sources = {i['media_path'] for i in model['items'] if i['type']=='video' and i['track']==1}
    named = list(folders)+profile.get('new_folders', [])
    inventory = {}
    for row in media_pool:
        inventory[row['file_path']] = dict(deepcopy(row), in_media_pool=True)
    for row in folder_files:
        path = PurePath(row['file_path'])
        if any(path.is_relative_to(PurePath(f)) for f in named):
            inventory.setdefault(row['file_path'], dict(deepcopy(row), in_media_pool=False))
    result = []
    for path, row in inventory.items():
        if path in sources or _AI_PATH.search(path) or row['kind'] not in ('video', 'photo'):
            continue
        duration = row.get('duration', 0)
        if row['kind']=='video' and duration <= 0:
            raise ValueError('候选素材时长无效：'+path)
        blocked, used_photo = [], False
        for item in model['items']:
            if item.get('media_path')!=path or item.get('owner') not in ('user','ai_v3','ai_legacy') or item.get('role')!='picture':
                continue
            if row['kind']=='photo':
                used_photo=True; break
            extra = item.get('broll_extra', {})
            source_fps = frame_rate(row['fps'])
            start = extra.get('source_start_seconds')
            if start is None:
                source_start = item.get('source_start')
                if source_start is None:
                    raise ValueError('用户已用素材缺源入点：'+path)
                start = float(Fraction(source_start)/source_fps)
            end = extra.get('source_end_seconds')
            if end is None:
                end = start+float(Fraction(item['duration'])/frame_rate(model['fps']))*extra.get('source_rate', 1)
                if item.get('source_end') is not None:end=max(end,float(Fraction(item['source_end'])/source_fps))
            blocked.append([max(0, start-.5), min(duration, end+.5)])
        if used_photo:
            continue
        row['available_ranges'] = subtract((0, duration), blocked) if row['kind']=='video' else None
        if row['kind']=='video' and not row['available_ranges']:
            continue
        priorities = profile.get('material_priority', [])
        row['priority'] = next((n for n,p in enumerate(priorities) if p in path), len(priorities))
        result.append(row)
    return sorted(result, key=lambda r:(r['priority'], r['file_path']))


def build_catalog(value, candidates, timeline_fps=29.97):
    """Intersect index seconds with candidate ranges; return T1 frame-domain dict.

    Split usable intervals become separate IDs; parent_segment retains the index
    ID for supplemental descriptions. Empty intersections never enter arrange.
    """
    inventory = {r['file_path']:r for r in candidates}
    fps = frame_rate(timeline_fps)
    catalog = {}
    for path, entry in value['files'].items():
        path = entry.get('file_path', path)
        if path not in inventory:
            continue
        row = inventory[path]
        for segment in entry['segments']:
            lo, hi = segment.get('usable_start',segment['start']), segment.get('usable_end',segment['end'])
            spans = [[lo,hi]] if row['kind']=='photo' else [[max(lo,a), min(hi,b)] for a,b in row['available_ranges'] if max(lo,a)<min(hi,b)]
            for n,(a,b) in enumerate(spans):
                start, end = ceil(a*fps-1e-7), floor(b*fps+1e-7)
                if start>=end:
                    continue
                ident = segment['id'] if len(spans)==1 and a==lo and b==hi else f"{segment['id']}:{n+1}"
                catalog[ident] = {**deepcopy(row), **deepcopy(segment), 'segment':ident,
                    'parent_segment':segment['id'], 'file':path, 'usable_start':start, 'usable_end':end,
                    'size':entry['size'], 'mtime_ns':entry['mtime_ns'], 'probe':deepcopy(entry.get('probe',{}))}
    return catalog
