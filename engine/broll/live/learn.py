"""Action-only learning and deterministic profile reconstruction (design §9)."""
from copy import deepcopy
from pathlib import Path, PurePath
import json
import os
import re
from statistics import median
from ._util import encode, digest, union, intersects, subtract
from .timeline import build_model, record_to_source, source_to_record
from .ledger import match_identity, learning_allowed, _atomic_json, main_picture, _identity
from .arrange import visible_frames, DEFAULTS

ALGO_VERSION = 'broll-offline-1'
SOURCE_RANK = {'ai_paraphrase':1, 'user_quote':2, 'user_edit':3}
_TRANSFORMS = ('ZoomX','ZoomY','Pan','Tilt','RotationAngle','Rotation','Rotate')


def _meta(catalog, spot):
    key = spot.get('segment')
    rows = spot.get('candidates',[])
    if not key and rows:
        key = rows[0].get('segment') if isinstance(rows[0],dict) else rows[0]
    parent=spot.get('parent_segment') or re.sub(r':\d+$','',key or '')
    meta = deepcopy(catalog.get(key) or next((v for k,v in sorted(catalog.items()) if v.get('parent_segment',k)==parent),{}))
    meta['segment'] = meta.get('parent_segment') or spot.get('parent_segment') or re.sub(r':\d+$','',key or '') or None
    return meta


def _edge(model, frame):
    kinds = []
    if frame in model['cuts']:
        kinds.append('cut')
    if any(frame in (l['start'],l['end']) for l in model['lines']):
        kinds.append('subtitle')
    if any(frame in r for r in model['picture_occupied']):
        kinds.append('user_picture')
    if any(frame in r for r in model['reserved']):
        kinds.append('reserved')
    anchors = sorted(set(model['cuts']) | {l[k] for l in model['lines'] for k in ('start','end')})
    nearest = min(anchors,key=lambda p:(abs(p-frame),p))
    return {'frame':frame,'kinds':kinds,'offset':frame-nearest}


def _relocate(item, before, after):
    sources = record_to_source(before,item['start'],item['end'])
    ranges = source_to_record(after,sources)
    if not ranges:
        return None
    if len(ranges) != 1:
        raise ValueError('AI位置映射成不连续区间，需要先确认口播重剪')
    expected = deepcopy(item)
    expected.update(start=ranges[0][0],end=ranges[0][1],duration=ranges[0][1]-ranges[0][0])
    # Leading host content removed by ripple also removes the corresponding
    # front of the B-roll source. Internal removals have no preference signal.
    current_sources = record_to_source(after,*ranges[0])
    front = current_sources[0]
    lost = 0
    for old in sources:
        if old['source']==front['source'] and old['start']<=front['start']<old['end']:
            lost += front['start']-old['start'];break
        lost += old['end']-old['start']
    if expected.get('source_start') is not None:
        expected['source_start'] += lost
    return expected


def _check_subtitles(before,after):
    if after['snapshot'].get('broll_extra',{}).get('subtitles_synced') is False:
        raise ValueError('字幕与V1不同步，停止学习')
    if before['structure_fingerprint']==after['structure_fingerprint']:
        return
    for line in before['lines']:
        expected = source_to_record(after,record_to_source(before,line['start'],line['end']))
        if not expected:
            continue
        matching = [l for l in after['lines'] if l['text']==line['text'] and l['track']==line['track']]
        if not any([l['start'],l['end']] in expected for l in matching):
            raise ValueError('字幕与V1源帧不同步，停止学习：'+line['text'])


def diff_snapshots(before_snapshot, after_snapshot, ledger, catalog=None, approve=False, fx_built=False, fx_record=None):
    """Return preference actions, raw/A-roll changes, latest spot results and ledger.

    Never writes. `ledger` returned with the new baseline prevents replaying the
    same manual actions. Uncertainty >20% blocks every profile contribution.
    """
    catalog = catalog or {}
    before = build_model(before_snapshot,ledger,fx_record=fx_record)
    after = build_model(after_snapshot,ledger,fx_record=fx_record)
    result = {'actions':[],'results':[],'aroll_reedit':[],'raw_changes':[],
              'uncertain':[],'update_allowed':True,'new_folders':[], 'ledger':deepcopy(ledger),
              'skipped':after['learning_skipped']}
    if not learning_allowed(after['snapshot']['name']):
        result.update(update_allowed=False,reason='审核/历史/逐句否决版默认不学');return result
    if before['v1_source_fingerprint'] != after['v1_source_fingerprint']:
        raise ValueError('V1口播源身份改变，不是同谱系；请确认新项目')
    _check_subtitles(before,after)
    if before['structure_fingerprint'] != after['structure_fingerprint']:
        result['aroll_reedit'].append({'before':before['structure'],'after':after['structure']})
    own_before = [i for i in before['items'] if i['owner']=='user' and i['role']=='picture' and not i.get('learning_skip_reason')]
    added, uncertain_own, own_used = [], [], set()
    for i in sorted(after['items'], key=lambda i: (i['start'],i['end'])):
        if i['owner'] != 'user' or i['role'] != 'picture' or not i['enabled'] or i.get('learning_skip_reason'):
            continue
        cur = _identity(i)
        candidates = [n for n,old in enumerate(own_before) if n not in own_used
                      and _identity(old)['file_name']==cur['file_name']
                      and (_identity(old)['size'] is None or cur['size'] is None or _identity(old)['size']==cur['size'])
                      and (_identity(old)['source_start']==cur['source_start']
                           or (old['start'],old['end'],old['track'])==(i['start'],i['end'],i['track']))]
        exact = [n for n in candidates if (own_before[n]['start'],own_before[n]['end'],own_before[n]['track'])==(i['start'],i['end'],i['track'])]
        winners = exact or candidates
        if not winners:
            added.append(i)
        elif len(winners)==1:
            own_used.add(winners[0])
        else:
            uncertain_own.append(i)
    eligible = 0
    replacement_ids = set()
    for spot,entry in ledger['spots'].items():
        old = entry['S0']
        if not main_picture(entry) or entry.get('status')=='aroll_removed':
            continue
        eligible += 1
        expected = _relocate(old,before,after)
        if expected is None:
            result['ledger']['spots'][spot]['status']='aroll_removed'
            result['raw_changes'].append({'spot':spot,'reason':'所在口播源区间已删除，排除偏好'});continue
        if any(i.get('learning_skip_reason') and i['enabled'] and intersects((i['start'],i['end']),(expected['start'],expected['end'])) for i in after['items']):
            eligible-=1
            result['raw_changes'].append({'spot':spot,'reason':'与未能追溯的复合/Fusion片段重叠，本处不推导删换偏好'})
            continue
        # Every surviving baseline follows the same coordinate system, including
        # missing/uncertain/fx-owned spots when the timeline baseline advances.
        result['ledger']['spots'][spot]['S0']=deepcopy(expected)
        result['ledger']['spots'][spot]['placement']=deepcopy(expected)
        match = match_identity(old,after['items'])
        state = {'lineage':ledger['lineage'],'spot':spot,'text':entry['semantic']['text'],
                 'segment':entry.get('segment'),'status':'unreviewed','approved':False,'weight':0,
                 'creator':ledger['creator']}
        meta = _meta(catalog,entry)
        payload = {'spot':spot, 'text':entry['semantic']['text'],'segment':meta.get('segment'),
                   'description':meta.get('description',''), 'quality':deepcopy(meta.get('quality',{})),
                   'aspect':meta.get('aspect','unknown'), 'source':'user_edit',
                   'source_ranges':record_to_source(after,expected['start'],expected['end']),
                   'start':expected['start'],'end':expected['end']}
        payload['quality'].update({k:meta[k] for k in ('people','focus','subject') if k in meta})
        if match['status']=='uncertain':
            result['uncertain'].append(spot);continue
        local_actions = []
        cur = after['items'][match['index']] if match['status']=='matched' else None
        covering = [i for i in added if cur is None or i['track']>cur['track']]
        owned_ranges = union((max(i['start'],expected['start']),min(i['end'],expected['end'])) for i in covering
                             if intersects((i['start'],i['end']),(expected['start'],expected['end'])))
        replaced = sum(e-s for s,e in owned_ranges)/(expected['end']-expected['start'])>=0.5
        if cur is None or replaced:
            kind = 'replaced_own' if replaced else 'deleted'
            action = dict(payload,kind=kind)
            action['exposed'] = [r for r in visible_frames(after) if intersects((r['start'],r['end']),(expected['start'],expected['end']))]
            if kind=='replaced_own':
                replacement_ids.update(i['id'] for i in covering if intersects((i['start'],i['end']),(expected['start'],expected['end'])))
                action['materials']=[{'path':i['media_path'],'folder':str(PurePath(i['media_path']).parent),
                                      'media_type':i.get('media_type')} for i in added
                                     if intersects((i['start'],i['end']),(expected['start'],expected['end']))]
            if match_identity(old,before['items'])['status']!='missing' and entry.get('status')!=kind:
                if entry.get('origin')=='ai_legacy' and not meta.get('segment'):
                    action['quality_unavailable']=True
                local_actions.append(action)
            if cur:
                result['ledger']['spots'][spot]['S0']=deepcopy(cur)
                result['ledger']['spots'][spot]['placement']=deepcopy(cur)
            state['status']=kind
            reserve = {'lineage':ledger['lineage'],'source_ranges':payload['source_ranges'],
                       'reason':'她删除/替换过的AI位置','spot':spot}
            if reserve not in result['ledger']['reserved']:
                result['ledger']['reserved'].append(reserve)
        else:
            cur = after['items'][match['index']]
            if cur['owner']=='fx' or cur['role']=='overlay':
                result['ledger']['spots'][spot]['S0']=deepcopy(cur)
                result['ledger']['spots'][spot]['placement']=deepcopy(cur)
                result['ledger']['spots'][spot]['status']='fx_excluded'
                result['raw_changes'].append({'spot':spot,'reason':'fx认领，排除'});continue
            result['ledger']['spots'][spot]['S0']=deepcopy(cur)
            result['ledger']['spots'][spot]['placement']=deepcopy(cur)
            if old['enabled'] and not cur['enabled']:
                local_actions.append(dict(payload,kind='disabled'))
                reserve={'lineage':ledger['lineage'],'source_ranges':payload['source_ranges'],'reason':'disabled','spot':spot}
                if reserve not in result['ledger']['reserved']:result['ledger']['reserved'].append(reserve)
            if cur['duration'] != expected['duration']:
                kind='trimmed' if cur['duration']<expected['duration'] else 'extended'
                local_actions.append(dict(payload,kind=kind,delta_frames=cur['duration']-expected['duration'],
                                          duration=cur['duration'], edges=[_edge(after,cur['start']),_edge(after,cur['end'])]))
                if kind=='trimmed':
                    removed=subtract((expected['start'],expected['end']),[(cur['start'],cur['end'])])
                    ranges=[r for a,b in removed for r in record_to_source(after,a,b)]
                    if ranges:
                        local_actions[-1]['reserved_source_ranges']=ranges
                        reserve={'lineage':ledger['lineage'],'source_ranges':ranges,'reason':'trimmed','spot':spot}
                        if reserve not in result['ledger']['reserved']:result['ledger']['reserved'].append(reserve)
            elif (cur['start'],cur['end']) != (expected['start'],expected['end']):
                result['raw_changes'].append({'spot':spot,'before':expected,'after':cur,'reason':'未分类位置差异'})
            # The slip baseline is the source offset expected after ripple.
            old_offset=expected.get('broll_extra',{}).get('left_offset')
            if old_offset is not None:
                old_offset += expected.get('source_start',0)-old.get('source_start',0)
            else:
                old_offset=expected.get('source_start')
            new_offset=cur.get('broll_extra',{}).get('left_offset')
            if new_offset is None:
                new_offset=cur.get('source_start')
            if (cur['start'],cur['end'])==(expected['start'],expected['end']) and old_offset!=new_offset:
                local_actions.append(dict(payload,kind='slipped',before=old_offset,after=new_offset))
            changed = {k:{'before':old.get('props',{}).get(k),'after':cur.get('props',{}).get(k)}
                       for k in _TRANSFORMS if old.get('props',{}).get(k)!=cur.get('props',{}).get(k)}
            if changed:
                action=dict(payload,kind='reframed',changes=changed)
                if cur.get('props',{}).get('ZoomX') is not None and cur['props'].get('ZoomX')==cur['props'].get('ZoomY'):
                    action['zoom']=cur['props']['ZoomX']
                local_actions.append(action)
            raw_fields={k:{'before':old.get(k),'after':cur.get(k)} for k in ('name','track','enabled','props','broll_extra')
                        if old.get(k)!=cur.get(k)}
            if raw_fields:
                result['raw_changes'].append({'spot':spot,'fields':raw_fields})
            state['status']=('disabled' if not cur['enabled'] else 'modified' if local_actions or raw_fields or (cur['start'],cur['end'])!=(expected['start'],expected['end'])
                             else entry.get('status') if entry.get('status') in ('modified','replaced_own') else 'kept')
            if not local_actions and (approve or fx_built):
                if state['status']=='kept':state.update(approved=True,weight=0.3)
            elif not local_actions and entry.get('approved') and state['status']=='kept':
                state.update(approved=True,weight=0.3)
            if entry.get('pending_replacement') and state['status']=='kept':
                state.update(approved=True,weight=0.3)
        result['actions'].extend(local_actions)
        state['actions']=[a['kind'] for a in local_actions]
        state.update(description=payload['description'],quality=payload['quality'])
        result['results'].append(state)
        result['ledger']['spots'][spot].update(status=state['status'],approved=state['approved'])
        result['ledger']['spots'][spot].pop('pending_replacement',None)
    result['uncertain_rate']=len(result['uncertain'])/eligible if eligible else 0
    result['update_allowed']=result['uncertain_rate']<=0.2 and not uncertain_own
    if not result['update_allowed']:
        result['reason']='身份不确定超过20%或新增用户画面身份不确定，本次不更新档案'
    for i in added:
        if i['id'] in replacement_ids:
            if not any(m.get('file',m.get('media_path'))==i['media_path'] for m in catalog.values()):result['new_folders'].append(str(PurePath(i['media_path']).parent))
            continue
        result['actions'].append({'kind':'user_added','source':'user_edit','spot':'own:'+digest({
            'file':PurePath(i['media_path']).name,'source_start':i['source_start'],'start':i['start']})[:16],
            'start':i['start'],'end':i['end'],'duration':i['duration'],
            'path':i['media_path'],'folder':str(PurePath(i['media_path']).parent),
            'media_type':i.get('media_type'),'text':''.join(l['text'] for l in after['lines']
                    if intersects((l['start'],l['end']),(i['start'],i['end']))),
            'edges':[_edge(after,i['start']),_edge(after,i['end'])],
            'aspect':i['broll_extra'].get('aspect','unknown'),
            'zoom':i.get('props',{}).get('ZoomX') if i.get('props',{}).get('ZoomX')==i.get('props',{}).get('ZoomY') else None})
        if not any(m.get('file',m.get('media_path'))==i['media_path'] for m in catalog.values()):
            result['new_folders'].append(str(PurePath(i['media_path']).parent))
    for i in after['items']:
        if i['type']!='video' or i['track']!=1 or i['enabled']:
            continue
        sources=record_to_source(after,i['start'],i['end'])
        old_ranges=source_to_record(before,sources)
        if any(v['type']=='video' and v['track']==1 and v['enabled'] and
               any(intersects((v['start'],v['end']),r) for r in old_ranges) for v in before['items']):
            result['actions'].append({'kind':'new_reserved','source':'user_edit','spot':'v1:'+digest(sources)[:16],
                                      'source_ranges':sources,'start':i['start'],'end':i['end'],'reason':'新停用V1'})
    old_markers=before['snapshot']['markers']
    for frame,marker in after['snapshot']['markers'].items():
        text=marker.get('note','')+' '+marker.get('name','')
        start=after['start']+int(frame);end=start+max(1,int(marker.get('duration',1)))
        if old_markers.get(frame)==marker or marker.get('customData','').startswith(('fx','broll')):
            continue
        if not re.search('画面|素材|镜头|B.?roll|留空|不铺|露脸',text,re.I) or ('花字' in text and not re.search('画面|素材|镜头|B.?roll',text,re.I)):
            continue
        overlapping=[r for r in result['results'] if any(intersects((start,end),v) for v in source_to_record(after,
                         ledger['spots'][r['spot']]['semantic']['source_ranges']))]
        for r in overlapping:
            result['actions'].append({'kind':'marker_note','source':'user_quote','spot':r['spot'],
                                      'text':text.strip(),'provenance':{'timeline':after['snapshot']['name'],'marker_frame':frame}})
    result['new_folders']=sorted(set(result['new_folders']))
    # Do not advance ambiguous baselines; caller may persist raw evidence only.
    if result['update_allowed']:
        result['ledger']['S0']=deepcopy(after['snapshot'])
    else:
        result['ledger']=deepcopy(ledger)
    return result


def make_event(delta, timestamp, timeline, machine, creator, lineage, film_id=None):
    """Build one immutable event with caller-supplied timestamp, never wall clock."""
    event={'timestamp':timestamp,'timeline':timeline,'machine':machine,'creator':creator,'lineage':lineage,
           'film_id':film_id or delta.get('ledger',{}).get('v1_source_fingerprint') or lineage,**{k:deepcopy(delta.get(k,[])) for k in
                                        ('actions','results','aroll_reedit','raw_changes','uncertain','new_folders')},
           'update_allowed':delta.get('update_allowed',True)}
    event['results']=[r for r in event['results'] if r.get('status')!='uncertain']
    event['id']=digest(event)
    return event


def write_event(directory, event):
    """Create a new JSON file atomically, never replace another machine's event."""
    def clean(value):
        return re.sub(r'[^\w.\-]+','_',str(value)) or '_'
    path=Path(directory)/f'{clean(event["timestamp"])}_{clean(event["timeline"])}_{clean(event["machine"])}.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    # 完整写入临时文件后再改名，读者只会看到完整文件。NAS(SMB) 不支持硬链接
    # （2026-10-14 实测 Errno 45），所以用“目标不存在才改名”；文件名含时间戳与机器名，
    # 两台 Mac 不会同名，同名即报错而不覆盖。
    import tempfile
    if path.exists():raise FileExistsError('学习事件已存在，不覆盖：'+path.name)
    fd,tmp=tempfile.mkstemp(prefix='.event-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(encode(event));f.flush();os.fsync(f.fileno())
        if path.exists():raise FileExistsError('学习事件已存在，不覆盖：'+path.name)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return path


def _active(events, ignored, creator):
    ignored=ignored or {}
    skip=set(ignored.get('events',[]));rules=set(ignored.get('rules',[]))
    active=[]
    for event in sorted(events,key=lambda e:(e['timestamp'],e['id'])):
        if event['id'] in skip or not event.get('update_allowed',True) or not learning_allowed(event['timeline']):
            continue
        if event['creator']!=creator:
            continue
        e=deepcopy(event)
        e['actions']=[a for a in e['actions'] if a.get('rule_key') not in rules
                      and a.get('source') in SOURCE_RANK]
        active.append(e)
    return active


def latest_results(events, ignored=None, creator=None):
    """Result table keyed by lineage+spot, latest timestamp/id wins (no kept events)."""
    if creator is None:
        creators={e['creator'] for e in events}
        if len(creators)>1:
            raise ValueError('需要指定创作者，不合并不同人的档案')
        creator=next(iter(creators),'Creator')
    results={}
    for e in _active(events,ignored,creator):
        for r in e.get('results',[]):
            if r.get('status')=='uncertain':continue
            key=e['lineage']+'|'+r['spot']
            results[key]=dict(deepcopy(r),film_id=e['film_id'],timestamp=e['timestamp'],event_id=e['id'])
    return results


def rebuild_profile(events, ignored=None, creator=None):
    """Deterministically recompute §9.4 data; no current time or decay.

    Numerical rules use explicit >=5 duration / >=3 aspect-zoom sample gates.
    Inferred global rules additionally require >=2 films and >=3 distinct spots;
    explicit user quotes and individual-material blacklists are not inference.
    """
    if creator is None:
        creators={e['creator'] for e in events}
        if len(creators)>1:
            raise ValueError('需要指定创作者，不合并不同人的档案')
        creator=next(iter(creators),'Creator')
    active=_active(events,ignored,creator)
    results=latest_results(events,ignored,creator)
    profile={'algo_version':ALGO_VERSION,'creator':creator,'rules':{},'hard_filters':{
        'focus':['missed'],'subject':['cut_off'],'people':['person','host']},
        'blacklist':[],'positive':[],'negative':[],'parameters':deepcopy(DEFAULTS),
        'zoom':{},'reserved':[],'material_priority':[],'evidence':{},'version_verdict':[],
        'semantic_guidance':[],'trend':[],'new_folders':[],'quality_deletions':{},'quality_unavailable':[]}
    signals={};numeric={};deletions={};examples=[];reservations={};rule_samples={};folder_set=set()
    hash_inputs=[]
    for e in active:
        meaningful=[]
        for index,a in enumerate(e['actions']):
            kind=a['kind'];source=a['source'];strong=source=='user_edit'
            sample=(e['lineage'],a.get('spot',f'{e["id"]}:{index}'))
            if kind in ('aroll_reedit','kept'):
                continue
            meaningful.append(a)
            context=dict(a,film_id=e['film_id'],lineage=e['lineage'],timestamp=e['timestamp'])
            if a.get('quality_unavailable'):profile['quality_unavailable'].append({'lineage':e['lineage'],'spot':a['spot']})
            if context.get('segment'):context['segment']=context.get('parent_segment') or re.sub(r':\d+$','',context['segment'])
            if kind in ('quote','rule','version_verdict'):
                key=a.get('rule_key','locality' if kind=='version_verdict' else None)
                if key:
                    if source=='user_edit' and kind=='rule' and not a.get('explicit'):
                        rule_samples.setdefault(key,{})[sample]=context
                    else:
                        previous=signals.get(key)
                        if previous is None or SOURCE_RANK[source]>=SOURCE_RANK[previous['source']]:
                            signals[key]=context
                if kind=='version_verdict':
                    profile['version_verdict'].append(context)
            if kind in ('deleted','disabled') and strong:
                segment=a.get('segment')
                if segment and kind=='deleted':
                    deletions[sample]=context
                if kind=='disabled':context['weight']=0.3
                examples.append((e['timestamp'],e['id'],index,'negative',context))
            if strong and kind in ('trimmed','extended','user_added','user_sample'):
                numeric[sample]=context
                if kind in ('user_added','user_sample'):
                    examples.append((e['timestamp'],e['id'],index,'positive',context))
            if strong and kind=='reframed':
                numeric[sample]=dict(numeric.get(sample,{}),**context)
                examples.append((e['timestamp'],e['id'],index,'positive',context))
            if strong and kind in ('deleted','disabled','replaced_own','new_reserved','trimmed'):
                ranges=a.get('reserved_source_ranges') if kind=='trimmed' else a.get('source_ranges')
                if ranges:
                    reservations[sample]={'lineage':e['lineage'],'spot':a['spot'],
                                          'source_ranges':ranges,'reason':kind}
            kinds={k for edge in a.get('edges',[]) for k in edge.get('kinds',[])}
            if strong and kinds & {'cut','subtitle'}:
                rule_samples.setdefault('edge_anchor',{})[sample]=dict(context,value='cut' if 'cut' in kinds else 'subtitle')
            folder_set.update(e.get('new_folders',[]))
        relevant_results=[r for r in e.get('results',[]) if r.get('status')!='uncertain']
        # Raw/A-roll evidence never alters the preference input hash.
        if meaningful or relevant_results:
            hash_inputs.append({'id':e['id'],'creator':creator,'lineage':e['lineage'],'film_id':e['film_id'],
                                'timestamp':e['timestamp'],'actions':meaningful,'results':relevant_results,
                                'new_folders':e.get('new_folders',[])})
    grouped={}
    for a in deletions.values():
        grouped.setdefault(a['segment'],[]).append(a)
        for k,v in a.get('quality',{}).items():
            profile['quality_deletions'][f'{k}={v}']=profile['quality_deletions'].get(f'{k}={v}',0)+1
    for segment,rows in grouped.items():
        nonsemantic=[r for r in rows if r.get('reason_class')!='semantic']
        quality=any(r.get('quality',{}).get('people') in ('person','host') or
                    r.get('quality',{}).get('focus')=='missed' or r.get('quality',{}).get('subject') in ('offset','cut_off')
                    for r in nonsemantic)
        if quality or len(nonsemantic)>=2:
            profile['blacklist'].append(segment)
            profile['evidence']['blacklist:'+segment]={'films':len({r['film_id'] for r in nonsemantic}),
                                                       'spots':len(nonsemantic),'source':'user_edit'}
    profile['blacklist'].sort()
    for key,row in sorted(signals.items()):
        value=row.get('value',True if key=='locality' else row.get('text'))
        profile['rules'][key]=value
        profile['evidence'][key]={'films':1,'spots':1,'source':row['source'],'text':row.get('text',''),
                                  'provenance':row.get('provenance')}
        if key=='material_priority':
            profile['material_priority']=value
        if key=='reserved':
            profile['reserved'].extend(value)
        if key=='hard_filters':
            profile['hard_filters'].update(value)
        if key=='semantic_guidance':
            profile['semantic_guidance']=(value if isinstance(value,list) else [value])[-6:]
        if key in DEFAULTS:
            profile['parameters'][key]=value
    for key,rows in sorted(rule_samples.items()):
        films={r['film_id'] for r in rows.values()}
        if len(films)>=2 and len(rows)>=3 and key not in signals:
            profile['rules'][key]=list(rows.values())[-1].get('value','cut')
            profile['evidence'][key]={'films':len(films),'spots':len(rows),'source':'user_edit'}
    durations=[a['duration'] for a in numeric.values() if a.get('duration') is not None]
    if len(durations)>=5:
        if 'min_frames' not in signals:profile['parameters']['min_frames']=min(durations)
        if 'max_frames' not in signals:profile['parameters']['max_frames']=max(durations)
        profile['evidence']['duration']={'films':len({a['film_id'] for a in numeric.values() if a.get('duration') is not None}),
                                         'spots':len(durations),'source':'user_edit'}
    offsets=[abs(edge['offset']) for a in numeric.values() for edge in a.get('edges',[]) if 'offset' in edge]
    recurring=[offset for offset in set(offsets) if offset>0 and offsets.count(offset)>=2]
    if len(numeric)>=5 and recurring and 'snap_tolerance' not in signals:
        profile['parameters']['snap_tolerance']=max(recurring)
        profile['evidence']['snap_tolerance']={'films':len({a['film_id'] for a in numeric.values()}),
                                               'spots':len(numeric),'source':'user_edit'}
    zooms={}
    for a in numeric.values():
        if a.get('zoom') is not None and a.get('aspect','unknown')!='unknown':
            zooms.setdefault(str(a['aspect']),[]).append(a)
    for aspect,rows in sorted(zooms.items()):
        if len(rows)>=3:
            profile['zoom'][aspect]=median(r['zoom'] for r in rows)
            profile['evidence']['zoom:'+aspect]={'films':len({r['film_id'] for r in rows}),'spots':len(rows),'source':'user_edit'}
    profile['reserved'].extend(reservations.values())
    profile['reserved']=sorted(profile['reserved'],key=lambda r:encode(r))
    # Approved, unchanged spots are weak examples; one per lineage+spot.
    for key,r in sorted(results.items()):
        if r.get('approved') and r.get('status')=='kept':
            examples.append((r['timestamp'],r['event_id'],0,'positive',dict(r,weight=0.3)))
    for sign in ('positive','negative'):
        dedup={}
        for stamp,event_id,index,s,a in sorted(examples,key=lambda r:r[:3]):
            if s==sign:
                key=(a.get('lineage'),a.get('spot'),a.get('segment'),a.get('text'))
                dedup.pop(key,None);dedup[key]=a
        profile[sign]=list(dedup.values())[-6:]
    history={};running={}
    for e in active:
        for r in e.get('results',[]):
            if r.get('status')!='uncertain':
                running[e['lineage']+'|'+r['spot']]=dict(r,film_id=e['film_id'])
        rows=[r for r in running.values() if r['film_id']==e['film_id']]
        if rows:
            rate=sum(r['status'] in ('kept','modified','trimmed','extended','reframed','slipped') for r in rows)/len(rows)
            points=history.setdefault(e['film_id'],[])
            if not points or points[-1]['rate']!=rate:
                points.append({'timestamp':e['timestamp'],'rate':rate})
    per_film={}
    for r in results.values():
        per_film.setdefault(r['film_id'],[]).append(r)
    for film,rows in sorted(per_film.items()):
        kept=sum(r['status'] in ('kept','modified','trimmed','extended','reframed','slipped') for r in rows)
        # All latest results are represented; repeated learn does not inflate N.
        profile['trend'].append({'film_id':film,'total':len(rows),'adopted':kept,
                                  'adoption_rate':kept/len(rows),
                                  'history':history.get(film,[]),
                                  'user_added':len({(e['lineage'],a.get('spot')) for e in active if e['film_id']==film
                                                    for a in e['actions'] if a['kind']=='user_added'})})
    profile['new_folders']=sorted(folder_set)
    profile['quality_unavailable']=[{'lineage':lineage,'spot':spot} for lineage,spot in sorted({(r['lineage'],r['spot']) for r in profile['quality_unavailable']})]
    ignored_rules=set((ignored or {}).get('rules',[]))
    for key in ignored_rules:
        profile['rules'].pop(key,None);profile['evidence'].pop(key,None)
        if key in DEFAULTS:profile['parameters'][key]=DEFAULTS[key]
        if key=='duration':
            profile['parameters'].update(min_frames=DEFAULTS['min_frames'],max_frames=DEFAULTS['max_frames'])
        if key.startswith('zoom:'):profile['zoom'].pop(key[5:],None)
        if key=='blacklist':profile['blacklist']=[]
        if key.startswith('blacklist:'):profile['blacklist']=[s for s in profile['blacklist'] if s!=key[10:]]
        if key=='reserved':profile['reserved']=[]
    profile['inputs_hash']=digest({'algo_version':ALGO_VERSION,'creator':creator,'inputs':hash_inputs,
                                  'ignored_rules':sorted(ignored_rules)})
    return profile


def ignore_learning(events, ignored=None, last=False, rule=None, creator=None):
    """Return an ignore table by latest learning or rule name, no opaque ID needed."""
    result=deepcopy(ignored or {'events':[],'rules':[]})
    result.setdefault('events',[]);result.setdefault('rules',[])
    if last==bool(rule):
        raise ValueError('请选择最近一次学习或一条规则')
    if last:
        candidates=[e for e in events if e['id'] not in result['events'] and e.get('update_allowed',True)
                    and (e.get('actions') or any(r.get('approved') for r in e.get('results',[])))
                    and (creator is None or e['creator']==creator)]
        if not candidates:
            raise ValueError('没有可忽略的学习')
        result['events'].append(max(candidates,key=lambda e:(e['timestamp'],e['id']))['id'])
    else:
        result['rules'].append(rule)
    result['events']=sorted(set(result['events']));result['rules']=sorted(set(result['rules']))
    return result


def learning_summary(before, after):
    """Exactly three Chinese lines from profile differences (deterministic template)."""
    added=sorted(set(after.get('blacklist',[]))-set(before.get('blacklist',[])))
    changed=sorted(k for k in set(before.get('rules',{}))|set(after.get('rules',{}))
                   if before.get('rules',{}).get(k)!=after.get('rules',{}).get(k))
    labels={'min_frames':'最短时长','max_frames':'最长时长','snap_tolerance':'吸附容差',
            'edge_anchor':'边缘吸附优先级','locality':'仅修改点名位置','material_priority':'素材优先级'}
    numerical=sorted(k for k in set(before.get('parameters',{}))|set(after.get('parameters',{}))
                     if before.get('parameters',{}).get(k)!=after.get('parameters',{}).get(k))
    removed=set(before.get('blacklist',[]))-set(after.get('blacklist',[]))
    zoom=before.get('zoom',{})!=after.get('zoom',{})
    return '\n'.join(['这次学到的规则：'+('、'.join(labels.get(k,k) for k in changed) if changed else '没有变化'),
                      '素材与保留位：新增黑名单'+str(len(added))+'段，撤销'+str(len(removed))+'段，保留位'+str(len(after.get('reserved',[])))+'处'+('；旧片段'+str(len(after['quality_unavailable']))+'处未关联源索引，无法统计质量' if after.get('quality_unavailable') else ''),
                      '数值偏好：'+('、'.join([labels.get(k,k) for k in numerical]+(['按画幅分组的放大倍数'] if zoom else []))+'已更新'
                                  if numerical or zoom else '没有变化')])


def preferences_markdown(profile):
    """Render 学到的偏好.md using data templates, evidence counts and adoption trend."""
    lines=['# 学到的偏好','', '学习功能：试用。','']
    labels={'edge_anchor':'边缘吸附优先','locality':'口头反馈只改点名位置','duration':'单段时长（帧）',
            'snap_tolerance':'吸附容差（帧）','material_priority':'素材优先级'}
    values=dict(profile['rules'],**profile['parameters'])
    values.update({'zoom:'+k:v for k,v in profile['zoom'].items()})
    values['hard_filters']=profile['hard_filters']
    for key,value in sorted(values.items()):
        evidence=profile['evidence'].get(key,profile['evidence'].get('duration',{}) if key in ('min_frames','max_frames') else {})
        lines.append(f'- {labels.get(key,key)}：{json.dumps(value,ensure_ascii=False)}；依据 {evidence.get("films",0)} 条片子 / {evidence.get("spots",0)} 处；来源 {evidence.get("source","默认规则")}')
    for segment in profile['blacklist']:
        evidence=profile['evidence'].get('blacklist:'+segment,{})
        lines.append(f'- 素材黑名单 {segment}：依据 {evidence.get("films",0)} 条片子 / {evidence.get("spots",0)} 处')
    lines += ['', '- 素材黑名单：'+('、'.join(profile['blacklist']) or '无'),
              '- 保留位：'+str(len(profile['reserved']))+'处','', '## 采纳率趋势','',
              '| 片子 | 原样或小改 / AI总数 | 采纳率趋势 | AI空档自加画面 |','| --- | --- | --- | --- |']
    for row in profile['trend']:
        film=str(row['film_id']).replace('|','\\|').replace('\n',' ')
        rates=' → '.join(f'{r["rate"]:.1%}' for r in row.get('history',[])) or f'{row["adoption_rate"]:.1%}'
        lines.append(f'| {film} | {row["adopted"]}/{row["total"]} | {rates} | {row["user_added"]} |')
    return '\n'.join(lines)+'\n'


def save_profile(directory, events, ignored=None, creator=None):
    """Atomically save profile plus human-readable preference data in caller dir."""
    directory=Path(directory);profile=rebuild_profile(events,ignored,creator)
    _atomic_json(directory/'档案.json',profile)
    # Markdown is derived; JSON remains the authoritative state.
    import tempfile
    fd,tmp=tempfile.mkstemp(prefix='.preferences-',dir=directory)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(preferences_markdown(profile));f.flush();os.fsync(f.fileno())
        os.replace(tmp,directory/'学到的偏好.md')
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return profile


def load_profile(directory, events, ignored=None, creator=None):
    """Verify algorithm/input hash and bytes; rebuild stale or corrupt cached data."""
    path=Path(directory)/'档案.json';expected=rebuild_profile(events,ignored,creator)
    try:
        current=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):
        current=None
    if current!=expected:
        return save_profile(directory,events,ignored,creator)
    return current
