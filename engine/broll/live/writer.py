"""Transactional SDK writes with exact ownership guards and compensating undo."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from engine.fx.ledger import digest, verify_delta
from engine.fx.resolve_io import objects
from .ledger import exact_state, match_identity, save_ledger
from .timeline import build_model, record_to_source, select_main_track
from .arrange import zero_flash
from .workspace import write_lock
import sys
import re
from .resolve_adapter import database_type, TRANSFORMS, PhotoDurationMismatch


def snapshot_hash(snapshot):
    return digest({k:v for k,v in snapshot.items() if k not in ('timecode','resolve_version')})


def _check(before, after, removed=(), added=(), new_timelines=(), new_tracks=()):
    errors = verify_delta(before,after,removed,added,new_timelines,new_tracks)
    if errors:
        raise RuntimeError('；'.join(errors))


def _matched(state, snapshot):
    match = match_identity(state,snapshot['timeline']['items'])
    if match['status']!='matched':
        raise ValueError('片段无法唯一识别，停止删除')
    row = snapshot['timeline']['items'][match['index']]
    if exact_state(row)!=exact_state(state):
        raise ValueError('片段已被手动修改，绝不删除或覆盖：'+state['name'])
    return row


def _handle(io, row):
    found = [i for i in objects(io.main) if i.GetUniqueId()==row['id']]
    if len(found)!=1:
        raise RuntimeError('待删除片段句柄不唯一')
    return found[0]


def _capture_links(io, row, snapshot):
    saved=deepcopy(row)
    ids={i.GetUniqueId() for i in _handle(io,row).GetLinkedItems() or []}
    saved['_saved_links']=[deepcopy(r) for r in snapshot['timeline']['items'] if r['id'] in ids]
    return saved


def _restore_links(io, deleted, restored):
    live=io.snapshot()
    for old,new in zip(deleted,restored):
        for linked in old.get('_saved_links',[]):
            other=_matched(linked,live)
            if new['type']==other['type']:continue
            video,audio=(new,other) if new['type']=='video' else (other,new)
            io.link(_handle(io,video),_handle(io,audio))


def _restore(io, row):
    media = io.media(row['media_path'],media_id=row.get('broll_extra',{}).get('media_id'))
    offset = row.get('broll_extra',{}).get('left_offset')
    offset = row.get('source_start') if offset is None else offset
    item = io.append_clip(media,row['type'],row['track'],row['start'],offset,row['duration'],allow_disabled=True)
    props = dict(row.get('props',{}))
    # Only writable SDK clip controls, never diagnostics.
    props.update({k:v for k,v in row.get('broll_extra',{}).items() if k in TRANSFORMS and v is not None})
    io.set_state(item,row['name'],row.get('broll_extra',{}).get('color',''),row['enabled'],props)
    actual = next(r for r in io.snapshot()['timeline']['items'] if r['id']==item.GetUniqueId())
    if exact_state(actual)!=exact_state(row):
        raise RuntimeError('段 '+row['id']+'：旧片段补偿参数读回不符')
    return actual


def _rollback(io, before, deleted, added, new_tracks, new_timelines):
    try:
        now = io.snapshot()
        live_ids = {r['id'] for r in now['timeline']['items']}
        for row in reversed(added):
            if row['id'] not in live_ids:
                continue
            current = _matched(row,io.snapshot())
            io.delete([_handle(io,current)])
        restored = []
        for row in deleted:
            restored.append(_restore(io,row))
        _restore_links(io,deleted,restored)
        for kind,track in reversed(new_tracks):
            io.remove_track(kind,track)
    finally:
        # Clean our copies even when clip compensation fails, before verification.
        io.delete_timelines(new_timelines)
    after = io.snapshot()
    # Reinserted SDK items have new IDs. Compare every original state exactly.
    for old,new in zip(deleted,restored):
        for row in after['timeline']['items']:
            if row['id']==new['id']:
                row['id']=old['id']
    _check(before,after)
    io.save_reopen()
    reopened = io.snapshot()
    for old,new in zip(deleted,restored):
        for row in reopened['timeline']['items']:
            if row['id']==new['id']:
                row['id']=old['id']
    _check(before,reopened)
    return restored


def _failed_copies(io, before, run_id, tracked):
    """Include partial copies whose creator failed before returning its handle."""
    names={'B-roll历史_'+io.main_name+'_'+run_id, 'B-roll审核_'+io.main_name+'_'+run_id}
    known={t.GetUniqueId() for t in tracked}
    original={t['id'] for t in before.get('timelines',[])}
    for n in range(1,io.p.GetTimelineCount()+1):
        t=io.p.GetTimelineByIndex(n)
        if t.GetName() in names and t.GetUniqueId() not in known | original:
            tracked.append(t);known.add(t.GetUniqueId())


def _remaining_copies(io, tracked):
    ids={t.GetUniqueId() for t in tracked}
    try:
        remaining=[io.p.GetTimelineByIndex(n) for n in range(1,io.p.GetTimelineCount()+1)]
        return '、'.join(t.GetName()+'（'+t.GetUniqueId()+'）' for t in remaining if t.GetUniqueId() in ids) or '无'
    except Exception as exc:
        return '无法读取：'+str(exc)+'；待查：'+'、'.join(t.GetName() for t in tracked)


def _flashes(snapshot, ledger, baseline=None):
    # Actual track enable is trusted only after active-line readback; disabled
    # picture tracks contribute no visible pixels to the frame-by-frame check.
    model = build_model(snapshot,ledger)
    tracks = {(t['type'],t['index']):t for t in model['tracks']}
    for item in model['items']:
        if not tracks[item['type'],item['track']]['enabled']:
            item['enabled']=False
    flashes=zero_flash(model)
    if baseline is None:return flashes
    existing=_flashes(baseline,ledger)
    old_ids={i['id'] for i in baseline['timeline']['items']}
    added=[i for i in model['items'] if i['id'] not in old_ids and i['type']=='video' and i['enabled'] and i['role']!='overlay']
    return [g for g in flashes if g not in existing or any(i['end']==g['start'] or i['start']==g['end'] for i in added)]


def execute(io, plan, ledger, ledger_path, *, apply=False, config=None, lock_path=None, local=False):
    config = config or {}
    before = io.snapshot()
    if snapshot_hash(before)!=plan['basis_hash']:
        raise ValueError('时间线已变化（哈希不符），请重新生成计划')
    operations = plan.get('operations',[])
    if not operations:
        return {'applied':False,'message':'没有需要写入的改动'}
    if not apply:
        return {'applied':False,'message':'演练完成；加 --apply 才会写入','operations':operations}
    typ = database_type(io.r)
    if typ=='未知' or (typ=='PostgreSQL' and config.get('allow_postgres_apply') is not True):
        raise ValueError('当前数据库'+typ+'默认拒绝写入；PostgreSQL须测试工程实测后配置 allow_postgres_apply=true')
    with write_lock(lock_path):
        print('请暂停操作达芬奇约30秒（持写入锁期间）',file=sys.stderr)
        return _execute_locked(io,plan,ledger,ledger_path,before,local)


def _discard_short_photo(io, before, error, media, placement, track):
    """Delete exactly the proven new item, with zero other timeline changes."""
    after=io.snapshot()
    _check(before,after,added=[error.item_id])
    original={r['id']:r for r in before['timeline']['items']}
    if any(r!=original[r['id']] for r in after['timeline']['items'] if r['id'] in original):
        raise RuntimeError('照片回退前既有片段变化，停止删除')
    row=next(r for r in after['timeline']['items'] if r['id']==error.item_id)
    if (row['type'],row['track'],row['start'],row['duration'],row['media_path'],
        row.get('broll_extra',{}).get('media_id'))!=(
        'video',track,placement['start'],error.actual,placement['path'],media.GetUniqueId()):
        raise RuntimeError('照片回退身份核对失败，停止删除')
    handle=_handle(io,row)
    if handle.GetMediaPoolItem().GetUniqueId()!=media.GetUniqueId():
        raise RuntimeError('照片回退媒体池引用不符，停止删除')
    io.delete([handle])
    if snapshot_hash(before)!=snapshot_hash(io.snapshot()):
        raise RuntimeError('照片回退删除后时间线未恢复，停止')


def _photo_derivative(placement, spot, snapshot, reason):
    from ..media import render_live_clip
    from .placement import select_lut
    fallback=placement['photo_fallback']
    candidate=fallback['candidate']
    lut=select_lut(candidate,folder_rules=fallback['folder_rules'],lut_dirs=fallback['lut_dirs'])
    if lut['status']!='ok':raise ValueError(lut['reason'])
    filename_spot=spot if re.fullmatch(r'[\w-]{1,64}',spot) else 'legacy-'+digest(spot)[:16]
    clip=render_live_clip(candidate,fallback['out_dir'],filename_spot,
        placement['source_start'],placement['end']-placement['start'],
        timeline_fps=snapshot['timeline']['fps'],
        resolution=(int(snapshot['timeline']['width']),int(snapshot['timeline']['height'])),lut_path=lut['lut_path'])
    placement.update(path=clip['path'],offset=clip['startFrame'],generated=True,
                     media_id=None,derivative_reason=reason)


def _execute_locked(io, plan, ledger, ledger_path, before, local):
    run_id = plan['run']
    result = deepcopy(ledger)
    deleted, added, new_tracks, new_timelines = [],[],[],[]
    current_expected = before
    photo_failure = None
    photo_fallbacks = []
    touched = {}
    for op in plan['operations']:
        old=op.get('old',[])
        for state in ([old] if isinstance(old,dict) else old):_matched(state,before)
    try:
        if any(op.get('old') for op in plan['operations']):
            duplicate = io.history('B-roll历史_'+io.main_name+'_'+run_id)
            new_timelines.append(duplicate)
            current_expected = io.snapshot()
            _check(before,current_expected,new_timelines=[duplicate.GetUniqueId()])
        for op in plan['operations']:
            live = io.snapshot()
            if snapshot_hash(live)!=snapshot_hash(current_expected):
                raise ValueError('时间线在写入间发生变化（哈希不符），停止')
            old = op.get('old',[])
            if isinstance(old,dict):old=[old]
            rows = [_matched(s,live) for s in old]
            model = build_model(live,result)
            placement = deepcopy(op.get('new'))
            if placement and placement['type']=='video':
                selected = select_main_track(model,placement['start'],placement['end'],excluded_ids=[r['id'] for r in rows])
                if not io.main.GetIsTrackEnabled('video',placement['track']):raise ValueError('目标主选轨已停用')
                if selected!=placement['track']:
                    raise ValueError('主选轨不再可用，停止')
                if any(a<placement['end'] and placement['start']<b for a,b in model['reserved']+model['picture_occupied']):
                    raise ValueError('写入区间属于用户画面或保留位')
            # Delete one at a time, recording each actual deletion immediately.
            for row in rows:
                saved=_capture_links(io,row,live)
                io.delete([_handle(io,row)])
                deleted.append(saved)
            op_added = []
            if placement:
                fallback_reason = None
                if placement.get('photo_fallback') and photo_failure:
                    fallback_reason='同次运行已出现'+photo_failure+'；本照片直接生成衍生片段'
                    _photo_derivative(placement,op['spot'],live,fallback_reason)
                media = io.media(placement['path'],placement.get('generated',False),media_id=placement.get('media_id'))
                track = placement['track']
                if placement['type']=='audio':
                    track,created = io.ensure_track('audio','B-roll 原声')
                    if created:new_tracks.append(('audio',track))
                ids_before = {r['id'] for r in io.snapshot()['timeline']['items']}
                try:
                    photo_before=io.snapshot() if placement.get('photo_fallback') and not placement.get('generated') else None
                    try:
                        item = io.append_clip(media,placement['type'],track,placement['start'],placement['offset'],placement['end']-placement['start'])
                    except PhotoDurationMismatch as error:
                        if photo_before is None:raise
                        _discard_short_photo(io,photo_before,error,media,placement,track)
                        photo_failure=fallback_reason=str(error)
                        _photo_derivative(placement,op['spot'],photo_before,fallback_reason)
                        media=io.media(placement['path'],True)
                        item=io.append_clip(media,placement['type'],track,placement['start'],placement['offset'],placement['end']-placement['start'])
                    if fallback_reason:
                        photo_fallbacks.append(dict(spot=op['spot'],source_file=placement['source_file'],
                            path=placement['path'],derivative_reason=fallback_reason,
                            attempted_direct=photo_before is not None))
                    io.set_state(item,placement['name'],props=placement.get('props'))
                    if placement.get('name') is None:
                        expected_name=media.GetClipProperty('Clip Name')
                        if (item.GetMediaPoolItem().GetUniqueId()!=media.GetUniqueId()
                            or item.GetMediaPoolItem().GetClipProperty('File Path')!=placement['path']
                            or expected_name and item.GetName()!=expected_name):
                            raise RuntimeError('原素材名称或媒体池引用读回不符')
                    if placement['type']=='audio':
                        video = _matched(result['spots'][op['spot']]['placement'],io.snapshot())
                        io.link(_handle(io,video),item)
                finally:
                    # Append may have inserted even when returning bad data. Record
                    # actual new items so compensation removes them as well.
                    for row in io.snapshot()['timeline']['items']:
                        if row['id'] not in ids_before:
                            added.append(deepcopy(row));op_added.append(deepcopy(row))
                if len(op_added)!=1:
                    raise RuntimeError('spot '+op['spot']+'：单处写入新增数量不符，预期=1，实际='+str(len(op_added)))
                state = op_added[0]
                touched[op['spot']] = state
                record = result['spots'].setdefault(op['spot'],{})
                if placement['type']=='audio':
                    record['audio']=state;record['audio_guard']=deepcopy(state)
                else:
                    record.update(origin='ai_v3',placement=state,S0=state,status='unreviewed',
                        segment=placement.get('parent_segment',placement.get('segment')),parent_segment=placement.get('parent_segment',placement.get('segment')),candidates=op.get('candidates',[]),
                        semantic={'text':op.get('text',''),'source_ranges':record_to_source(model,state['start'],state['end'])},
                        source_start=placement.get('source_start'),source_file=placement.get('source_file'),derivative_reason=placement.get('derivative_reason'),parent_spot=op.get('parent_spot',op['spot']),guard_state=deepcopy(state))
                    if local and rows:record.update(selection_origin='ai',pending_replacement=True)
            else:
                record = result['spots'].get(op['spot'],{})
                if rows and all(r['type']=='audio' for r in rows):
                    record.pop('audio',None)
                else:
                    record['status']='deleted'
                    for row in rows:
                        touched[op['spot']]=row
                if op.get('reserve'):
                    spans=op.get('reserve_ranges') or [[r['start'],r['end']] for r in rows if r['type']=='video']
                    result.setdefault('reserved',[]).extend({'source_ranges':record_to_source(model,a,b),'reason':'用户点名保留','lineage':result['lineage'],'spot':op['spot']} for a,b in spans)
            current_expected = io.snapshot()
            _check(before,current_expected,[r['id'] for r in deleted],[r['id'] for r in added],[t.GetUniqueId() for t in new_timelines],new_tracks)
        if _flashes(current_expected,result,before):
            raise RuntimeError('逐帧读回存在1–3帧闪缝，停止交付')
        if local and touched:
            from .review_live import review_ranges
            ranges = review_ranges(touched,before['timeline']['start'],before['timeline']['end'])
            review = io.create_review('B-roll审核_'+io.main_name+'_'+run_id,ranges)
            new_timelines.append(review)
            result.setdefault('reviews',{})[review.GetName()] = {'id':review.GetUniqueId(),'ranges':ranges,'main':io.main_name}
            current_expected = io.snapshot()
            _check(before,current_expected,[r['id'] for r in deleted],[r['id'] for r in added],[t.GetUniqueId() for t in new_timelines],new_tracks)
        io.save_reopen()
        after = io.snapshot()
        _check(current_expected,after)
        from engine.fx.connect import find_timeline
        for name,review in result.get('reviews',{}).items():
            if review['id'] in {t.GetUniqueId() for t in new_timelines}:
                io.verify_review(find_timeline(io.p,name),review['ranges'])
        for op in plan['operations']:
            if (op.get('new') or {}).get('type')=='audio':
                row=result['spots'][op['spot']]
                video=_handle(io,_matched(row['placement'],after))
                audio=_matched(row['audio'],after)
                if audio['id'] not in {i.GetUniqueId() for i in video.GetLinkedItems() or []}:
                    raise RuntimeError('保存重开后原声链接丢失')
        if _flashes(after,result,before):
            raise RuntimeError('保存重开后逐帧核验存在闪缝')
        run = {'id':run_id,'kind':'write','deleted':deleted,'added':added,'new_tracks':new_tracks,
               'timelines':[t.GetUniqueId() for t in new_timelines],'spots':sorted({op['spot'] for op in plan['operations']}),'before_ledger':{k:v for k,v in ledger.items() if k!='runs'},'before':before,'after':after,'photo_fallbacks':photo_fallbacks}
        result['runs'].append(run)
        result['S0']=after['timeline'];result['last_learned']=None
        save_ledger(ledger_path,result)
        existing=_flashes(after,result)
        return {'applied':True,'message':'已保存、重开、读回核验；本次改动零闪帧'+('；原有闪缝'+str(len(existing))+'处' if existing else ''),
                'existing_flashes':existing,'run':run_id,'ledger':result,'count':len(plan['operations']),'photo_fallbacks':photo_fallbacks}
    except Exception as exc:
        _failed_copies(io,before,run_id,new_timelines)
        try:
            restored = _rollback(io,before,deleted,added,new_tracks,new_timelines)
            # Compensation changes IDs; update ledger identity hints/S0 while
            # preserving the original semantic records and immutable run history.
            repaired = deepcopy(ledger)
            snap = io.snapshot()
            for spot,row in repaired.get('spots',{}).items():
                for field in ('placement','audio'):
                    if row.get(field):
                        match=match_identity(row[field],snap['timeline']['items'])
                        if match['status']=='matched':row[field]=snap['timeline']['items'][match['index']]
            repaired['S0']=snap['timeline'];save_ledger(ledger_path,repaired)
        except Exception as rollback:
            raise RuntimeError('写入失败（run '+run_id+' / spots '+','.join(op['spot'] for op in plan['operations'])+'）：'+str(exc)+'；自动回滚未能核验：'+str(rollback)+'；留下的新建时间线：'+_remaining_copies(io,new_timelines)+'。下一步：暂停操作，检查现场及历史副本') from exc
        raise RuntimeError('写入失败（run '+run_id+' / spots '+','.join(op['spot'] for op in plan['operations'])+'）：'+str(exc)+'；时间线已恢复原样（保存重开核验）') from exc


def undo(io, ledger, ledger_path, *, run=None, apply=False, config=None, lock_path=None):
    runs = [r for r in ledger.get('runs',[]) if r.get('kind')=='write' and not r.get('undone') and (not run or r['id']==run)]
    if not runs:
        raise ValueError('没有可撤销的运行')
    target = runs[-1]
    target_spots=set(target.get('spots',[])) or {s for s,r in ledger['spots'].items() if any(r.get(field,{}).get('id') in {a['id'] for a in target['added']} for field in ('placement','audio'))}
    later=ledger['runs'][ledger['runs'].index(target)+1:]
    if any(not r.get('undone') and target_spots & set(r.get('spots',[])) for r in later):
        raise ValueError('后续运行改过同一位置，请先撤销后续调整')
    current = io.snapshot()
    # Validate ALL targets before any deletion; manually changed clips block undo.
    rows = [_matched(r,current) for r in target['added']]
    if not apply:
        return {'applied':False,'message':'撤销演练完成；加 --apply 才写入','run':target['id']}
    typ = database_type(io.r)
    if typ=='未知' or typ=='PostgreSQL' and (config or {}).get('allow_postgres_apply') is not True:
        raise ValueError('当前数据库默认拒绝撤销写入')
    with write_lock(lock_path):
        if snapshot_hash(io.snapshot())!=snapshot_hash(current):raise ValueError('撤销前时间线已变化')
        deleted,added=[],[];new_timelines=[]
        try:
            history=io.history('B-roll历史_'+io.main_name+'_undo_'+target['id'])
            new_timelines.append(history)
            history_snap=io.snapshot();_check(current,history_snap,new_timelines=[history.GetUniqueId()])
            for row in rows:
                saved=_capture_links(io,row,current)
                io.delete([_handle(io,row)]);deleted.append(saved)
            for old in target['deleted']:
                added.append(_restore(io,old))
            _restore_links(io,target['deleted'],added)
            for kind,track in reversed(target.get('new_tracks',[])):
                io.remove_track(kind,track)
            after = io.snapshot()
            expected_current=deepcopy(current)
            expected_current['timeline']['tracks']=[t for t in expected_current['timeline']['tracks'] if (t['type'],t['index']) not in set(map(tuple,target.get('new_tracks',[])))]
            _check(expected_current,after,[r['id'] for r in deleted],[r['id'] for r in added],new_timelines=[t.GetUniqueId() for t in new_timelines])
            restored = deepcopy(ledger)
            old_book=target['before_ledger']
            for spot in target_spots:
                if spot in old_book['spots']:restored['spots'][spot]=deepcopy(old_book['spots'][spot])
                else:restored['spots'].pop(spot,None)
            restored['reserved']=[r for r in restored.get('reserved',[]) if r.get('spot') not in target_spots]+[deepcopy(r) for r in old_book.get('reserved',[]) if r.get('spot') in target_spots]
            for spot,row in restored['spots'].items():
                for field in ('placement','audio'):
                    if row.get(field):
                        match = match_identity(row[field],after['timeline']['items'])
                        if match['status']=='matched':row[field]=after['timeline']['items'][match['index']]
            io.save_reopen();_check(after,io.snapshot())
            restored['runs']=deepcopy(ledger['runs'])
            next(r for r in restored['runs'] if r['id']==target['id'])['undone']=True
            restored['S0']=io.snapshot()['timeline']
            save_ledger(ledger_path,restored)
            return {'applied':True,'message':'撤销完成，已保存重开读回','ledger':restored,'run':target['id']}
        except Exception as exc:
            for kind,track in target.get('new_tracks',[]):
                if io.main.GetTrackCount(kind)<track:io.ensure_track(kind,'B-roll 原声')
            try:_rollback(io,current,deleted,added,[],new_timelines)
            except Exception as failure:raise RuntimeError('撤销失败且回滚未核验：'+str(failure)) from exc
            raise RuntimeError('撤销失败；已恢复撤销前状态：'+str(exc)) from exc
