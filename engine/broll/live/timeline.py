"""Snapshot contract and conservative timeline interpretation (design §3).

Input follows fx.snapshot.read_timeline: tracks[], items[] and broll_extra.
Reference captures use normalize_snapshot; raw source_out is retained only as
evidence because its inclusive/exclusive semantics vary. Never stat media here.
"""
from copy import deepcopy
from pathlib import PurePath
import re
from ._util import digest, union, intersects

_OLD = re.compile(r'^\[(?:B|D|R)\d{2}\]')
_AI = re.compile(r'^\[B-[^\]]+\]')
_AI_PATH = re.compile(r'(?:^|/)(?:密铺B-roll片段|B-roll片段|逐句修订Broll_[^/]+)(?:/|$)|Master/B-roll素材')
_OVERLAY = re.compile(r'花字\d*|人物抠像|音效\d*')
_STILLS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.heic', '.heif'}


def composite(item):
    typ=str(item.get('media_type') or '').casefold()
    if typ in ('fusion title','fusion标题','generator','生成器','adjustment clip'):
        return False
    return bool(item.get('nested') or item.get('broll_extra',{}).get('composite')
                or any(word in typ for word in ('compound','fusion clip','复合','融合片段'))
                or re.match(r'^(Compound Clip|Fusion Clip|复合片段|Fusion片段)(?:\s|$)', item.get('name','')))


def composite_sources(item):
    """Read provenance without opening or decomposing the user's compound."""
    sources=[]
    def walk(row):
        if row.get('media_path'):
            sources.append({'file':row['media_path'],'source_start':row.get('source_start'),
                            'source_end':row.get('source_end')})
        for child in row.get('nested',{}).get('items',[]):
            if child.get('type')=='video' and child.get('enabled',True):walk(child)
        sources.extend(row.get('broll_extra',{}).get('internal_sources',[]))
    walk(item)
    return [dict(row) for row in {digest(r):r for r in sources}.values()]


def normalize_snapshot(snapshot):
    """Return a deep-owned fx-compatible timeline dict with broll_extra."""
    raw = snapshot.get('timeline') if isinstance(snapshot.get('timeline'), dict) else snapshot
    data = deepcopy(raw)
    if isinstance(data.get('tracks'), dict):
        tracks, items = [], []
        for key, track in data['tracks'].items():
            match = re.fullmatch(r'(video|audio|subtitle)(\d+)', key)
            if not match:
                raise ValueError('未知轨道：'+key)
            kind, number = match.group(1), int(match.group(2))
            tracks.append({'type': kind, 'index': number, 'name': track['name'],
                           'enabled': track.get('enabled'), 'locked': track.get('locked', False)})
            for row in track['items']:
                item = {k: deepcopy(v) for k, v in row.items()
                        if k not in ('dur', 'src_in', 'src_out', 'file', 'color', 'media_id')}
                item.update(type=kind, track=number, track_name=track['name'],
                            duration=row['end']-row['start'], source_start=row.get('src_in'),
                            source_end=row.get('src_out'), media_path=row.get('file'),
                            media_type=row.get('media_type'), props=row.get('props', {}))
                item['broll_extra'] = dict(row.get('broll_extra', {}), color=row.get('color'),
                                           media_id=row.get('media_id'), raw_source_end=row.get('src_out'))
                items.append(item)
        data.update(tracks=tracks, items=items, name=data.get('timeline'), id=data.get('timeline_id'))
        data.setdefault('broll_extra', {})['track_enabled_trusted'] = False
    data.setdefault('name', data.get('timeline', ''))
    data.setdefault('broll_extra', {})
    data['broll_extra'].setdefault('track_enabled_trusted', False)
    for item in data['items']:
        for key in ('start', 'end', 'track'):
            if type(item[key]) is not int:
                raise ValueError('段 '+str(item.get('id','未提供ID'))+'：帧数/轨号必须是整数：'+key)
        if item['end'] <= item['start']:
            raise ValueError('段 '+str(item.get('id','未提供ID'))+'：无效片段范围：'+item['name']+' '+str((item['start'],item['end'])))
        item['duration'] = item['end']-item['start']
        item.setdefault('props', {})
        extra = item.setdefault('broll_extra', {})
        item.setdefault('track_name', next((t['name'] for t in data['tracks']
                        if (t['type'], t['index']) == (item['type'], item['track'])), ''))
        if PurePath(item.get('media_path') or '').suffix.lower() in _STILLS:
            item['media_type'] = 'Still'
            extra.update(left_offset=None, right_offset=None)
    data['markers'] = {str(int(float(k))): v for k, v in data.get('markers', {}).items()}
    return data


def source_identity(item):
    """Cross-host source reference; missing size remains unknown, no filesystem IO."""
    return {'file_name': PurePath(item.get('media_path') or '').name,
            'size': item.get('broll_extra', {}).get('file_size')}


def _overlay(item):
    return bool(_OVERLAY.search(item.get('track_name', '')) or (item.get('texts') and not item.get('media_path'))
                or item.get('nested') or item.get('name') in ('Text+', 'Text', '文本', 'Adjustment Clip', '调整片段')
                or item.get('media_type') in ('Timeline', '时间线', 'Generator', '生成器',
                                              'Fusion Title', 'Fusion标题', 'Adjustment Clip'))


def _fx_ids(record):
    result = set()
    for key in ('effects', 'nested', 'sounds', 'mattes'):
        for row in (record or {}).get(key, []):
            if row.get('item_id'):
                result.add(row['item_id'])
    return result | set((record or {}).get('item_ids', []))


def reservation_reason(reason, status=None):
    labels={'deleted':'她删','replaced_own':'她换','disabled':'她停用','trimmed':'她剪掉',
            'new_reserved':'章节停用V1','停用V1':'章节停用V1','新停用V1':'章节停用V1',
            '用户点名保留':'点名留空','用户标记':'点名留空','人物抠像':'露脸'}
    kind=labels.get(reason) or labels.get(status)
    return '保留位（'+(kind or '点名留空')+'）'


def reservation_details(model, rows, lineage=None, spots=None):
    """Expand the shared source/text/record reservation contract once."""
    details=[]
    for r in rows:
        if r.get('lineage') and r['lineage']!=lineage:continue
        if r.get('source_ranges'):ranges=source_to_record(model,r['source_ranges'])
        elif r.get('text'):ranges=[[l['start'],l['end']] for l in model['lines'] if r['text'] in l['text']]
        elif 'start' in r and 'end' in r:ranges=[[r['start'],r['end']]]
        else:raise ValueError('保留规则 '+str(r.get('spot','未提供ID'))+'：缺少source_ranges/text/start/end')
        status=(spots or {}).get(r.get('spot'),{}).get('status')
        details.extend({'start':a,'end':b,'reason':r.get('reason','台账/档案保留'),
                        'skip_reason':reservation_reason(r.get('reason'),status)} for a,b in ranges)
    return details


def protected_ranges(model, profile=None, ledger=None):
    profile,ledger=profile or {},ledger or {}
    details=reservation_details(model,profile.get('reserved',[])+ledger.get('reserved',[]),ledger.get('lineage'))
    return union(model['reserved']+[[r['start'],r['end']] for r in details])


def build_model(snapshot, ledger=None, profile=None, fx_record=None):
    """Interpret roles, owners, subtitles, protected ranges and V1/A1 fingerprints."""
    from .ledger import match_identity
    tl = normalize_snapshot(snapshot)
    ledger, profile = ledger or {}, profile or {}
    claimed = {}
    for spot, row in ledger.get('spots', {}).items():
        match = match_identity(row.get('placement', row.get('S0', {})), tl['items'])
        if match['status'] == 'matched':
            claimed[match['index']] = (spot, row.get('origin', 'ai_v3'))
    fx_ids = _fx_ids(fx_record)
    v1 = sorted([i for i in tl['items'] if i['type'] == 'video' and i['track'] == 1], key=lambda i: i['start'])
    source_ids = [source_identity(i) for i in v1]
    if not v1:
        raise ValueError('没有 V1 口播，无法建立时间线模型')
    items = []
    for number, original in enumerate(tl['items']):
        item = deepcopy(original)
        is_composite=composite(item) and not item.get('texts') and item['id'] not in fx_ids
        if is_composite:
            sources=composite_sources(item)
            item['broll_extra']['internal_sources']=sources
            # A sole linear nested source can contribute source-trim preferences.
            offset=item.get('source_start')
            nested_start=item.get('nested',{}).get('start',0)
            children=[r for r in item.get('nested',{}).get('items',[]) if r.get('type')=='video' and r.get('enabled',True)
                      and offset is not None and intersects((r['start'],r['end']),
                          (nested_start+offset,nested_start+offset+item['duration']))]
            child=children[0] if len(children)==1 else None
            linear=child and child.get('source_start') is not None and child.get('source_end') is not None and abs(child['source_end']-child['source_start']-(child['end']-child['start']))<=1
            same_rate=float(item.get('nested',{}).get('fps') or tl['fps'])==float(tl['fps'])
            if child and linear and same_rate and child.get('media_path') and child['start']<=nested_start+offset and nested_start+offset+item['duration']<=child['end']:
                item['media_path']=child['media_path']
                item['source_start']=child['source_start']+nested_start+offset-child['start']
                item['source_end']=item['source_start']+item['duration']
            elif item.get('media_path') and not item.get('nested'):
                pass  # A Fusion effect on an ordinary source keeps its source identity.
            else:
                item['learning_skip_reason']='复合/Fusion片段内部源区间不能可靠映射' if sources else '复合/Fusion片段无法追到内部真实源素材'
        path = item.get('media_path') or ''
        if item['type'] == 'subtitle':
            role, owner = 'subtitle', 'user'
        else:
            role = 'picture' if is_composite and item['type']=='video' else 'overlay' if _overlay(item) or item['id'] in fx_ids else 'picture' if item['type'] == 'video' else 'audio'
            if number in claimed:
                item['spot'], owner = claimed[number]
            elif _OLD.match(item['name']) or _AI.match(item['name']) or _AI_PATH.search(path):
                owner = 'ai_legacy'
            elif role == 'overlay' or item['id'] in fx_ids:
                owner = 'fx'
            elif source_identity(item) in source_ids and path:
                owner = 'aroll'
                if item['type'] == 'video':
                    role = 'aroll'
            elif path:
                owner = 'user'
            elif is_composite:
                owner = 'user'
            else:
                raise ValueError('无法归属片段：'+item['name'])
            if item.get('enabled') not in (True, False):
                raise ValueError('无法确认片段启用：'+item['name'])
        item.update(role=role, owner=owner)
        items.append(item)
    tracks = deepcopy(tl['tracks'])
    for track in tracks:
        content = [i for i in items if (i['type'], i['track']) == (track['type'], track['index'])]
        track['role'] = ('overlay' if _OVERLAY.search(track['name']) or any(i['role'] == 'overlay' for i in content)
                         else 'aroll' if track['type'] == 'video' and track['index'] == 1
                         else 'picture' if track['type'] == 'video' else track['type'])
    lines = []
    for i in sorted((i for i in items if i['type'] == 'subtitle'), key=lambda i: (i['start'], i['track'])):
        text = i.get('subtitle_text') or i.get('broll_extra', {}).get('subtitle_text') or i['name']
        if text.lower() in ('subtitle', '字幕', 'subtitle clip'):
            raise ValueError('字幕是通用名，需要调用方用 DRT 解析后补入 subtitle_text')
        lines.append({'line': len(lines)+1, 'text': text, 'start': i['start'], 'end': i['end'], 'track': i['track']})
    occupied = union((i['start'], i['end']) for i in items
                     if i['role'] == 'picture' and i['owner'] == 'user' and i['enabled'])
    reservations = [{'start': i['start'], 'end': i['end'], 'reason': '停用V1'} for i in v1 if not i['enabled']]
    reservations += [{'start': i['start'], 'end': i['end'], 'reason': '人物抠像'} for i in items
                     if i['type'] == 'video' and '人物抠像' in i['track_name'] and i['enabled']]
    for frame, marker in tl['markers'].items():
        text = marker.get('name', '')+' '+marker.get('note', '')
        if not marker.get('customData', '').startswith(('broll-', 'fx_')) and re.search('留空|章节|不铺|露脸', text):
            start = tl['start']+int(frame)
            reservations.append({'start': start, 'end': start+max(1, int(marker.get('duration', 1))), 'reason': '用户标记'})
    mapping = []
    for i in v1:
        s = i.get('source_start')
        if s is None:
            raise ValueError('V1 源入点缺失，无法对齐')
        rate = i['broll_extra'].get('source_rate', 1)
        raw_end = i.get('source_end')
        if rate != 1 or (raw_end is not None and abs(raw_end-s-i['duration']) > 1):
            raise ValueError('V1 变速或源区间不线性，无法可靠对齐')
        mapping.append({'start': i['start'], 'end': i['end'], 'source': source_identity(i),
                        'source_start': s, 'source_end': s+i['duration']})
    if any(a['end'] > b['start'] for a, b in zip(mapping, mapping[1:])):
        raise ValueError('V1 重叠，源帧映射不唯一')
    model = {'snapshot': tl, 'items': items, 'tracks': tracks, 'lines': lines,
             'learning_skipped':[{'id':i['id'],'name':i['name'],'reason':i['learning_skip_reason'],
                                  'sources':i['broll_extra'].get('internal_sources',[])} for i in items if i.get('learning_skip_reason')],
             'cuts': sorted({i[k] for i in v1 for k in ('start', 'end')}),
             'picture_occupied': occupied, 'v1_map': mapping, 'reserved_details': reservations,
             'start': tl['start'], 'end': tl['end'], 'fps': float(tl['fps'])}
    for r in reservations:r['skip_reason']=reservation_reason(r['reason'])
    reservations.extend(reservation_details(model,ledger.get('reserved',[])+profile.get('reserved',[]),ledger.get('lineage'),ledger.get('spots')))
    model['reserved'] = union((r['start'], r['end']) for r in reservations)
    structure = [{k: i.get(k) for k in ('type', 'track', 'start', 'end', 'source_start', 'enabled')}
                 | {'source': source_identity(i)} for i in items
                 if i['type'] in ('video', 'audio') and i['track'] == 1]
    model['structure'] = {'fps': model['fps'], 'items': sorted(structure, key=lambda i: (i['type'], i['start']))}
    model['structure_fingerprint'] = digest(model['structure'])
    model['v1_source_fingerprint'] = digest(sorted({digest(m['source']) for m in mapping}))
    model['snap_points'] = sorted(set(model['cuts']) | {l[k] for l in lines for k in ('start', 'end')}
                                 | {x for r in occupied+model['reserved'] for x in r})
    return model


def record_to_source(model, start, end):
    """Map a record range into ordered source slices; refuse unmapped frames."""
    rows = []
    for m in model['v1_map']:
        s, e = max(start, m['start']), min(end, m['end'])
        if s < e:
            rows.append({'source': m['source'], 'start': m['source_start']+s-m['start'],
                         'end': m['source_start']+e-m['start']})
    if sum(r['end']-r['start'] for r in rows) != end-start:
        raise ValueError('录制区间有未映射的口播帧')
    return rows


def source_to_record(model, source_ranges):
    """Map surviving source slices to record ranges; duplicated source frames fail."""
    result = []
    for r in source_ranges:
        covered = []
        for m in model['v1_map']:
            if m['source'] != r['source']:
                continue
            s, e = max(r['start'], m['source_start']), min(r['end'], m['source_end'])
            if s < e:
                if any(intersects((s, e), x) for x in covered):
                    raise ValueError('相同口播源帧出现多次，无法唯一对齐')
                covered.append([s, e])
                result.append([m['start']+s-m['source_start'], m['start']+e-m['source_start']])
    return union(result)


def select_main_track(model, start, end, additional=(), excluded_ids=()):
    """Lowest free existing picture track below every overlay; never creates tracks."""
    overlays = [t['index'] for t in model['tracks'] if t['type'] == 'video' and t['role'] == 'overlay']
    ceiling = min(overlays, default=10**9)
    for t in sorted(model['tracks'], key=lambda t: t['index']):
        if t['type'] != 'video' or t['role'] != 'picture' or not 1 < t['index'] < ceiling:
            continue
        if t.get('locked') or re.search('花字|人物抠像|备选|音效', t['name']):
            continue
        if any(i['type'] == 'video' and i['track'] == t['index'] and i['id'] not in excluded_ids
               and intersects((start, end), (i['start'], i['end'])) for i in model['items']):
            continue
        if any(i['track'] == t['index'] and intersects((start, end), (i['start'], i['end'])) for i in additional):
            continue
        return t['index']
    return None
