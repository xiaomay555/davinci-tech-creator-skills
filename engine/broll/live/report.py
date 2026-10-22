"""Shared, JSON-readable rehearsal/write/adjust reporting."""
from pathlib import Path
from engine.fx.snapshot import frame_timecode


def placement_report(plan, model, catalog, matches=None, drop=False):
    rows=[]
    for placement in plan['placements']:
        p=dict(placement)
        request=next((r for r in (matches or {}).get('segments',[]) if r['spot']==p['spot']),None)
        options=request['shots'][p.get('shot',1)-1]['candidates'] if request else []
        candidate=next(iter(options),{})
        meta=catalog.get(p.get('segment') or candidate.get('segment'),{})
        frame=p.get('start') if p.get('start') is not None else p['requested'][0]
        source=p.get('source_start')
        if source is None:source=candidate.get('source_start',meta.get('usable_start'))
        source_tc=frame_timecode(source,model['fps']) if source is not None else '无'
        row={'spot':p['spot'],'shot':p.get('shot',1),'status':p['status'],
             'timecode':frame_timecode(frame,model['fps'],drop),'text':p['text'],
             'file':Path(p.get('file') or meta.get('file','')).name or '无',
             'source_start':source,'source_timecode':source_tc,'reason':p['reason']}
        row['summary']='｜'.join((row['timecode'],row['text'],row['file'],
                                f'{source}帧（{source_tc}）' if source is not None else '无源入点'))
        rows.append(row)
    compliance=plan.get('compliance',{'considered':0,'filtered_candidates':0,'filtered_spots':0,'rate':1.0})
    lines=[r['summary']+('｜跳过：'+r['reason'] if r['status']=='skipped' else '') for r in rows]
    before,after=plan['previous_coverage'],plan['coverage']
    lines.append(f'覆盖率：{before:.1%} → {after:.1%}')
    lines.append(f"匹配遵守率：{compliance['rate']:.1%}（原始候选{compliance['considered']}个，强制过滤{compliance['filtered_candidates']}个）")
    for r in plan.get('filtered',[]):
        reasons=r.get('reasons') or [r.get('reason','未知规则')]
        lines.append(f"{r['spot']} 第{r.get('shot',1)}镜 / {r['segment']}：{'；'.join(reasons)}")
    return {'placements':rows,'skipped':[r for r in rows if r['status']=='skipped'],
            'previous_coverage':before,'coverage':after,'coverage_delta':after-before,
            'compliance':compliance,'filtered':plan.get('filtered',[]),'report':'\n'.join(lines)}
