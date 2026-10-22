"""Pure sentence/cut-boundary arrangement with local ownership protection (§5)."""
from copy import deepcopy
from math import ceil
from ._util import digest, union, intersects, subtract
from .timeline import select_main_track, source_to_record, protected_ranges, reservation_details
from .ledger import match_identity, exact_state, main_picture
from ..coverage import coverage_limit

DEFAULTS = {'min_frames': 75, 'max_frames': 150, 'snap_tolerance': 5}


def segment_identity(key, meta=None):
    """Catalog children are available slices of one indexed material segment."""
    import re
    return (meta or {}).get('parent_segment') or re.sub(r':\d+$', '', key or '')


def validate_spots(matches, ledger, targeted=()):
    known=ledger.get('spots',{}); targets=set(targeted)
    if targets-set(known):raise ValueError('点名位置不在台账中：'+','.join(sorted(targets-set(known))))
    for row in matches.get('segments',matches.get('spots',[])):
        if not isinstance(row,dict) or not isinstance(row.get('spot'),str):raise ValueError('spot 无效：'+str(row.get('spot','未提供ID') if isinstance(row,dict) else row))
        shots=row.get('shots',[])
        if not isinstance(shots,list):raise ValueError('spot '+row['spot']+'：shots须为列表')
        names=[row['spot']]+[row['spot']+'-shot'+str(n) for n in range(2,len(shots)+1)]
        for name in names:
            if name in known and name not in targets:
                raise ValueError('spot 已在台账中，须明确点名：'+name)
        if targets and row['spot'] not in targets:raise ValueError('匹配 spot 未被点名：'+row['spot'])


def _line_range(row, model):
    first, last = row['lines']
    if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(model['lines']):
        raise ValueError('spot '+str(row.get('spot','未提供ID'))+'：语义段须为有效连续行范围')
    lines = model['lines'][first-1:last]
    return lines[0]['start'], lines[-1]['end'], ''.join(l['text'] for l in lines)


def _quality(candidate, profile):
    q = dict(candidate.get('quality', {}))
    q.update({k: candidate[k] for k in ('focus', 'subject', 'people') if k in candidate})
    filters = dict(focus=['missed'], subject=['cut_off'], people=['person', 'host'])
    filters.update(profile.get('hard_filters', {}))
    people = q.get('people')
    use = candidate.get('use', candidate.get('quality', {}).get('use'))
    if people == 'host':
        return '质量过滤：people=host'
    if people == 'person' and use in ('operation','scene'):
        return '质量过滤：people=person / use='+use
    for key, bad in filters.items():
        if key == 'people' and people == 'person' and use == 'sample':
            continue
        if q.get(key) in bad:
            return '质量过滤：'+key+'='+q[key]
    if {segment_identity(candidate['segment']),segment_identity(candidate['segment'],candidate)} & {segment_identity(k) for k in profile.get('blacklist', [])}:
        return '素材黑名单'
    return None


def _snap(frame, model, params, inward=None):
    points = [p for p in model['snap_points'] if abs(p-frame) <= params['snap_tolerance']]
    if inward == 'start':
        points = [p for p in points if p >= frame]
    elif inward == 'end':
        points = [p for p in points if p <= frame]
    if params.get('edge_anchor', 'cut') == 'cut':
        cuts = [p for p in points if p in model['cuts']]
        if cuts:
            points = cuts
    elif params.get('edge_anchor')=='subtitle':
        subtitles={l[k] for l in model['lines'] for k in ('start','end')}
        preferred=[p for p in points if p in subtitles]
        if preferred:points=preferred
    return min(points, key=lambda p: (abs(p-frame), p not in model['cuts'], p)) if points else None


def visible_frames(model, placements=(), excluded_ids=()):
    """Topmost visible media at every frame; transparent overlays never occlude.

    Track-enabled is deliberately ignored (capture/DRP bug); clip-enabled is
    required. Returned labels are picture/aroll/black, with half-open runs.
    """
    pictures = [i for i in model['items'] if i['type']=='video' and i['role'] != 'overlay'
                and i['enabled'] and i['id'] not in excluded_ids]
    pictures += [dict(p, role='picture', enabled=True, type='video') for p in placements if p['status']=='placed']
    rows = []
    for frame in range(model['start'], model['end']):
        live = [i for i in pictures if i['start'] <= frame < i['end']]
        top = max(live, key=lambda i: i['track']) if live else None
        label = 'black' if top is None else 'aroll' if top['role']=='aroll' else 'picture'
        if rows and rows[-1]['label']==label:
            rows[-1]['end'] = frame+1
        else:
            rows.append({'start': frame, 'end': frame+1, 'label': label})
    return rows


def zero_flash(model, placements=(), excluded_ids=()):
    """List short host/black exposures adjoining pictures, including V1 black."""
    runs = visible_frames(model, placements, excluded_ids)
    return [deepcopy(r) for n,r in enumerate(runs) if 0 < n < len(runs)-1
            and r['label'] != 'picture' and r['end']-r['start'] <= 3
            and 'picture' in (runs[n-1]['label'],runs[n+1]['label'])
            and runs[n-1]['label']!=r['label']!=runs[n+1]['label']]


def _coverage(model, placements=(), excluded_ids=()):
    return sum(r['end']-r['start'] for r in visible_frames(model, placements, excluded_ids)
               if r['label']=='picture')/(model['end']-model['start'])


def arrange(matches, catalog, model, profile=None, ledger=None, targeted=()):
    """Return placements, skips, preserved clips, compliance and coverage comparison.

    matches={segments:[{spot, lines:[first,last], candidates:[segment|{segment,
    source_start}], shots?:[{end,candidates}]}], face:[{lines:[first,last]}]}.
    catalog is a segment-keyed dict (or list); all usable_* values are timeline
    frames. A shot's source_start is in this same frame domain; T2 translates it
    to native source FPS. Coverage is a creator preference, never a fill target.
    """
    profile, ledger = profile or {}, ledger or {}
    catalog = {c['segment']:c for c in catalog} if isinstance(catalog,list) else catalog
    params = dict(DEFAULTS, **profile.get('parameters', {}))
    params.update(profile.get('rules', {}))
    limit=coverage_limit(profile)
    if not 0 < params['min_frames'] <= params['max_frames'] or params['snap_tolerance'] < 0:
        raise ValueError('档案时长或吸附参数无效')
    targets = set(targeted)
    known = ledger.get('spots', {})
    validate_spots(matches,ledger,targets)
    if targets-set(known):
        raise ValueError('点名位置不在台账中：'+','.join(sorted(targets-set(known))))
    reserved = protected_ranges(model,profile,ledger)
    reserve_details=deepcopy(model.get('reserved_details',[]))
    reserve_details+=reservation_details(model,profile.get('reserved',[])+ledger.get('reserved',[]),ledger.get('lineage'),ledger.get('spots'))
    preserved, removed_ids, manual_targets, current = [], set(), set(), {}
    for spot, old in known.items():
        if old.get('status')=='aroll_removed':continue
        match = match_identity(old['placement'],model['items'])
        if match['status'] != 'matched':
            if main_picture(old) and (old['S0']['enabled'] or old.get('status') in ('disabled','modified','replaced_own')):
                ranges=source_to_record(model,old['semantic']['source_ranges'])
                reserved += ranges
                label={'replaced_own':'她换','disabled':'她停用'}.get(old.get('status'),'她删')
                reserve_details += [dict(start=a,end=b,skip_reason='保留位（'+label+'）') for a,b in ranges]
            if spot in targets:
                manual_targets.add(spot)
            continue
        cur = model['items'][match['index']]
        current[spot]=cur
        if spot in targets and exact_state(cur)==exact_state(old['S0']):
            removed_ids.add(cur['id'])
        elif spot in targets:
            manual_targets.add(spot)
        if main_picture(old) and spot not in targets:
            ranges=source_to_record(model,old['semantic']['source_ranges'])
            if old['S0']['enabled'] and not cur['enabled'] or old.get('status')=='disabled':
                reserved += ranges
                reserve_details += [dict(start=a,end=b,skip_reason='保留位（她停用）') for a,b in ranges]
            elif cur['enabled']:
                trims=[r for a,b in ranges for r in subtract((a,b),[(cur['start'],cur['end'])])]
                reserved += trims
                reserve_details += [dict(start=a,end=b,skip_reason='保留位（她剪掉）') for a,b in trims]
    for i in model['items']:
        if i['owner'] in ('ai_v3','ai_legacy') and i['id'] not in removed_ids:
            preserved.append(deepcopy(i))
    ai_occupied = union((i['start'],i['end']) for i in preserved if i['type']=='video' and i['enabled'])
    faces = [_line_range(r,model)[:2] for r in matches.get('face',[])]
    blocked = union(model['picture_occupied']+reserved+faces+ai_occupied)
    blockers=[(r['start'],r['end'],r.get('skip_reason','保留位（点名留空）')) for r in reserve_details]
    blockers += [(a,b,'保留位（点名留空）') for a,b in reserved if not any(r['start']<=a and b<=r['end'] for r in reserve_details)]
    blockers += [(a,b,'用户画面') for a,b in model['picture_occupied']]
    blockers += [(a,b,'露脸') for a,b in faces]
    blockers += [(a,b,'已有AI') for a,b in ai_occupied]
    spots = matches.get('segments',matches.get('spots',[]))
    seen_spots, outputs, filtered, considered = set(), [], [], 0
    used = {segment_identity(r.get('segment'),r) for spot,r in known.items() if r.get('segment') and spot in current and current[spot]['id'] not in removed_ids}
    replaced_segments={spot:segment_identity(r.get('segment'),r) for spot,r in known.items() if spot in targets and r.get('segment')}
    used_sources = []
    for spot,r in known.items():
        cur=current.get(spot)
        if not cur or cur['id'] in removed_ids:
            continue
        c = catalog.get(r.get('segment')) or next((v for k,v in catalog.items() if segment_identity(k,v)==segment_identity(r.get('segment'),r)),None)
        path=r.get('source_file') or (c.get('file',c.get('media_path')) if c else None)
        if path:
            source=r.get('source_start') if r.get('source_start') is not None else cur.get('source_start')
            used_sources.append((path,cur['start'],cur['end'],source,source+cur['duration']))
    previous_sources=deepcopy(used_sources)
    for row in spots:
        spot = row['spot']
        if spot in seen_spots:
            raise ValueError('重复 spot：'+spot)
        seen_spots.add(spot)
        start,end,text = _line_range(row,model)
        shots = row.get('shots') or [{'end':end,'candidates':row.get('candidates',[])}]
        if not 1 <= len(shots) <= 3:
            raise ValueError('spot '+str(row.get('spot','未提供ID'))+'：每段最多三镜')
        if [s['end'] for s in shots] != sorted({s['end'] for s in shots}) or shots[-1]['end'] != end:
            raise ValueError('spot '+str(row.get('spot','未提供ID'))+'：分镜须连续并覆盖语义段')
        if any(s['end'] not in model['cuts'] for s in shots[:-1]):
            raise ValueError('spot '+str(row.get('spot','未提供ID'))+'：语义段内分镜必须在跳切点')
        for number,shot in enumerate(shots):
            stop = shot['end']
            raw_start,raw_end = shot.get('start',start),stop
            candidates = shot.get('candidates',row.get('candidates',[]))
            if len(candidates)>3:
                raise ValueError('spot '+str(row.get('spot','未提供ID'))+'：每镜最多三候选')
            if raw_start < start or raw_end > stop:
                raise ValueError('spot '+str(row.get('spot','未提供ID'))+'：分镜越过语义段')
            for c in candidates:
                key = c if isinstance(c,str) else c['segment']
                if key not in catalog:
                    raise ValueError('候选不存在：'+key)
            base = {'spot':spot, 'shot':number+1, 'text':text, 'requested':[raw_start,raw_end],
                    'start':None,'end':None,'source_start':None,'segment':None,'track':None,
                    'status':'skipped','reason':''}
            if spot in manual_targets:
                outputs.append(dict(base,reason='该处已手改或身份不确定，保留现状'));start=stop;continue
            a,b = _snap(raw_start,model,params),_snap(raw_end,model,params)
            if a is None or b is None or b<=a:
                outputs.append(dict(base,reason='边缘无法吸附'));start=stop;continue
            if b-a > params['max_frames']:
                ends = [l['end'] for l in model['lines'] if a+params['min_frames'] <= l['end'] <= a+params['max_frames'] and l['end']<=b]
                if not ends:
                    outputs.append(dict(base,reason=f'超过时长上限（{b-a}帧>最长{params["max_frames"]}帧）'));start=stop;continue
                b = max(ends)
            new_occupied=[[o['start'],o['end']] for o in outputs if o['status']=='placed']
            safe_parts = subtract((a,b),blocked+new_occupied)
            # Keep only boundary-aligned pieces after protection clipping.
            parts = [[s,e] for s,e in safe_parts if s in model['snap_points'] and e in model['snap_points']
                     and e-s >= params['min_frames']]
            if not parts:
                if safe_parts:
                    length=max(e-s for s,e in safe_parts)
                    reason=f'裁后太短（{length}帧<最短{params["min_frames"]}帧）' if length<params['min_frames'] else '裁后边缘不在允许边界'
                else:
                    reason=next((label for s,e,label in blockers if intersects((a,b),(s,e))), '已有AI')
                outputs.append(dict(base,reason=reason));start=stop;continue
            # One selected segment per shot, choose the longest safe remainder.
            a,b = max(parts,key=lambda r:(r[1]-r[0],-r[0]))
            ranked = []
            for order,c in enumerate(candidates):
                considered += 1
                key = c if isinstance(c,str) else c['segment']
                meta = catalog[key]
                reason = _quality(meta,profile)
                if reason:
                    filtered.append({'spot':spot,'segment':key,'reason':reason});continue
                penalty = int(meta.get('focus',meta.get('quality',{}).get('focus'))=='soft')+int(meta.get('subject',meta.get('quality',{}).get('subject'))=='offset')
                priorities = profile.get('material_priority',[])
                path = meta.get('file',meta.get('media_path',''))
                priority = next((n for n,p in enumerate(priorities) if p in path),len(priorities))
                ranked.append((penalty,priority,order,c,meta))
            chosen = None
            failures = []
            for _,_,_,c,meta in sorted(ranked,key=lambda r:r[:3]):
                key = meta['segment']
                lo = meta.get('usable_start',0)
                hi = meta.get('usable_end',lo+meta.get('available_frames',0))
                source = c.get('source_start',lo) if isinstance(c,dict) else lo
                parent=segment_identity(key,meta)
                if parent in used or parent==replaced_segments.get(spot):
                    failures.append('同一素材段已用');continue
                if source < lo or source+b-a > hi:
                    failures.append('素材不足，不拉长');continue
                path = meta.get('file',meta.get('media_path',''))
                bad = False
                for file,s,e,src,src_end in used_sources:
                    if file == path and (e==a or b==s):
                        gap = max(source-src_end,src-(source+b-a))
                        if gap < ceil(model['fps']):
                            bad = True;break
                if bad:
                    failures.append('同源相邻区间重叠或不足一秒');continue
                track = select_main_track(model,a,b,[o for o in outputs if o['status']=='placed'],removed_ids)
                if track is None:
                    failures.append('没有叠加层下方的空闲主选轨');continue
                chosen = dict(base,start=a,end=b,source_start=source,segment=key,track=track,
                              status='placed',reason='按语义边界/跳切点排布',
                              file=path,usable_start=lo,usable_end=hi,parent_segment=parent)
                if limit<1 and not targets and _coverage(model,outputs+[chosen],removed_ids)>limit+1e-9:
                    failures.append(f'超过创作者/全局覆盖上限（{limit:.0%}）')
                    chosen=None
                    continue
                zoom = profile.get('zoom',{}).get(str(meta.get('aspect','unknown')))
                if zoom is not None:
                    if meta.get('black_bars'):
                        chosen['zoom_suggestion']=zoom
                    else:
                        chosen['props']={'ZoomGang':False,'ZoomX':zoom,'ZoomY':zoom}
                used.add(parent);used_sources.append((path,a,b,source,source+b-a));break
            outputs.append(chosen or dict(base,reason=next(iter(failures),next((r['reason'] for r in filtered if r['spot']==spot),'没有候选'))))
            start=stop
    # Repair only new placements, using their own available source frames.
    def source_gap_ok(out,start,end,source_start):
        others=[(o['file'],o['start'],o['end'],o['source_start'],o['source_start']+o['end']-o['start'])
                for o in outputs if o is not out and o['status']=='placed']
        others += previous_sources
        for file,s,e,src,src_end in others:
            if file==out['file'] and (end==s or e==start):
                if max(source_start-src_end,src-(source_start+end-start))<ceil(model['fps']):
                    return False
        return True
    for _ in range(len(outputs)*2+1):
        flashes = zero_flash(model,outputs,removed_ids)
        repaired = False
        for gap in flashes:
            a,b=gap['start'],gap['end']
            can_extend=not any(intersects((a,b),r) for r in model['picture_occupied']+reserved+faces)
            left=next((o for o in outputs if o['status']=='placed' and o['end']==a),None)
            right=next((o for o in outputs if o['status']=='placed' and o['start']==b),None)
            if can_extend and left and left['source_start']+b-left['start']<=left['usable_end'] and b-left['start']<=params['max_frames'] and source_gap_ok(left,left['start'],b,left['source_start']):
                if select_main_track(model,a,b,excluded_ids=removed_ids)==left['track'] and b in model['snap_points']:
                    left.update(end=b,reason='用本片段尾余量补齐闪帧');repaired=True;break
            if can_extend and right and right['source_start']-(b-a)>=right['usable_start'] and right['end']-a<=params['max_frames'] and source_gap_ok(right,a,right['end'],right['source_start']-(b-a)):
                if select_main_track(model,a,b,excluded_ids=removed_ids)==right['track'] and a in model['snap_points']:
                    right.update(start=a,source_start=right['source_start']-(b-a),reason='用本片段头余量补齐闪帧');repaired=True;break
            # No extension: pull the bordering new shot back to an earlier snap.
            if left:
                subtitles={l[k] for l in model['lines'] for k in ('start','end')}
                points=[p for p in model['snap_points'] if left['start']+params['min_frames']<=p<a and b-p>3
                        and (a-p<=params['snap_tolerance'] or p in subtitles)]
                if points:
                    left.update(end=max(points),reason='余量不足，缩回上一吸附点')
                else:
                    left.update(status='skipped',reason='余量不足，撤下本处以免闪帧')
                repaired=True;break
            if right:
                subtitles={l[k] for l in model['lines'] for k in ('start','end')}
                points=[p for p in model['snap_points'] if b<p<=right['end']-params['min_frames'] and p-a>3
                        and (p-b<=params['snap_tolerance'] or p in subtitles)]
                if points:
                    p=min(points);right['source_start']+=p-right['start'];right.update(start=p,reason='余量不足，缩回下一吸附点')
                else:
                    right.update(status='skipped',reason='余量不足，撤下本处以免闪帧')
                repaired=True;break
        if not repaired:
            break
    before,after = _coverage(model),_coverage(model,outputs,removed_ids)
    existing=zero_flash(model)
    flashes=zero_flash(model,outputs,removed_ids)
    new_flashes=[g for g in flashes if g not in existing or any(o['status']=='placed' and (o['end']==g['start'] or o['start']==g['end']) for o in outputs)]
    return {'placements':outputs,'preserved':preserved,'removed_ids':sorted(removed_ids),
            'reserved':union(reserved),'profile_hash':digest(profile),'filtered':filtered,
            'compliance':{'considered':considered,'filtered_candidates':len(filtered),
                          'filtered_spots':len({r['spot'] for r in filtered})},
            'coverage':after,'coverage_limit':limit,'previous_coverage':before,'coverage_delta':after-before,
            'needs_coverage_confirmation':before-after>0.15,
            'flashes':new_flashes,'existing_flashes':[g for g in flashes if g not in new_flashes]}
