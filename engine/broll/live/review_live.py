"""Viewing ranges and portable playhead-to-main-line identification."""
from copy import deepcopy
from .resolve_adapter import timecode_frame


def review_ranges(spots, start, end, pad=30, merge_gap=30):
    rows = []
    for spot, state in spots.items():
        a,b = max(start,state['start']-pad),min(end,state['end']+pad)
        if a<b:
            rows.append({'start':a,'end':b,'spots':[spot]})
    merged = []
    for row in sorted(rows,key=lambda r:r['start']):
        if merged and row['start']-merged[-1]['end']<merge_gap:
            merged[-1]['end'] = max(merged[-1]['end'],row['end'])
            merged[-1]['spots'] = sorted(set(merged[-1]['spots']+row['spots']))
        else:
            merged.append(deepcopy(row))
    cursor = start
    for row in merged:
        row.update(review_start=cursor,review_end=cursor+row['end']-row['start'])
        cursor = row['review_end']+20
    return merged


def identify(ledger, timeline_name, timecode, fps):
    frame = timecode_frame(timecode,fps)
    reviews = ledger.get('reviews',{})
    if timeline_name in reviews:
        mapping = [r for r in reviews[timeline_name]['ranges'] if r['review_start']<=frame<r['review_end']]
        if not mapping:
            return []
        frame = mapping[0]['start']+frame-mapping[0]['review_start']
    elif timeline_name!=ledger['timeline']:
        raise ValueError('当前线既不是主线，也不是登记的审核线')
    rows = [{'spot':spot,'frame':frame,'main':ledger['timeline'],'text':row.get('semantic',{}).get('text','')}
            for spot,row in ledger.get('spots',{}).items() if row.get('placement') and row['placement']['start']<=frame<row['placement']['end']]
    if not rows:
        ordered = sorted(((min(abs(frame-r['placement']['start']),abs(frame-r['placement']['end'])),spot,r)
                          for spot,r in ledger.get('spots',{}).items() if r.get('placement')),key=lambda x:(x[0],x[1]))
        rows = [{'spot':s,'frame':frame,'main':ledger['timeline'],'text':r.get('semantic',{}).get('text',''),'nearby':True} for _,s,r in ordered[:3]]
    return rows[:3]
