"""Synthetic example seeds for offline regression. Never load as creator preferences."""
from .timeline import build_model
from .learn import make_event, _edge
from ._util import digest

# These are synthetic examples, not personal conversational evidence.
_QUOTES = [
    ('coverage_style','broll_first','示例偏好：优先展示配画面。','synthetic/example'),
    ('subject_priority','centered','示例偏好：主体居中。','synthetic/example'),
    ('people_filter',['person','host'],'示例偏好：过滤人物。','synthetic/example'),
    ('focus_filter',['missed'],'示例偏好：过滤失焦。','synthetic/example'),
    ('chapter_reserved',True,'示例偏好：预留章节。','synthetic/example'),
    ('sentence_edges',True,'示例偏好：对齐句界。','synthetic/example'),
    ('no_stretch',True,'示例偏好：不拉长素材。','synthetic/example'),
    ('semantic_match',True,'示例偏好：镜头支持语义。','synthetic/example'),
    ('confirmed_chapters',True,'示例偏好：保留章节。','synthetic/example'),
]


def seed_events(snapshot, timestamp, machine, creator='Creator', lineage=None):
    """Generate quote/paraphrase/verdict and 11 own-picture statistical events.

    Unknown transform/volume/aspect data are omitted, never filled from prose.
    The supplied evidence document's verbatim quotations are user_quote;
    explicitly identified AI summaries remain ai_paraphrase.
    """
    model=build_model(snapshot)
    lineage=lineage or 'seed-1011-'+model['v1_source_fingerprint'][:16]
    actions=[{'kind':'quote','source':'user_quote','rule_key':key,'value':value,'text':text,
              'provenance':{'evidence':'synthetic/example'+reference,'verification':'合成测试数据'}}
             for key,value,text,reference in _QUOTES]
    actions += [
        {'kind':'quote','source':'ai_paraphrase','rule_key':'material_priority',
         'value':['当前项目','补充素材','示例照片','示例视频'],
         'text':'示例素材优先级，仅供离线测试',
         'provenance':{'evidence':'synthetic/example','verification':'合成测试数据'}},
        {'kind':'quote','source':'ai_paraphrase','rule_key':'face_by_meaning','value':True,
         'text':'示例：根据语义决定是否露脸',
         'provenance':{'evidence':'synthetic/example','verification':'合成测试数据'}},
        {'kind':'version_verdict','source':'user_quote','rule_key':'locality','value':True,
         'preferred':'dense-1011','rejected':'sentence-1011','scope':'global',
         'text':'示例：保留已确认版本',
         'provenance':{'evidence':'synthetic/example',
                       'verification':'合成测试数据'}}]
    samples=[]
    for i in model['items']:
        if i['owner']!='user' or i['role']!='picture' or i['type']!='video' or not 2<=i['track']<=4:
            continue
        a={'kind':'user_sample','source':'user_edit','spot':'seed-own:'+digest({
            'name':i['name'],'start':i['start'],'end':i['end'],'track':i['track']})[:16],
           'duration':i['duration'],'start':i['start'],'end':i['end'],'path':i['media_path'],
           'text':''.join(l['text'] for l in model['lines'] if l['start']<i['end'] and i['start']<l['end']),
           'edges':[_edge(model,i['start']),_edge(model,i['end'])],
           'provenance':{'evidence':'dense_synthetic.json','name':i['name'],'track':i['track']}}
        aspect=i['broll_extra'].get('aspect')
        if aspect is not None:a['aspect']=aspect
        props=i.get('props',{})
        if props.get('ZoomX') is not None and props.get('ZoomX')==props.get('ZoomY'):
            a['zoom']=props['ZoomX']
        samples.append(a)
    # Only observed volume values; no -5 dB fabrication from the report.
    volumes=[{'kind':'audio_sample','source':'user_edit','spot':'seed-audio:'+digest({'name':i['name'],'start':i['start']})[:16],
              'volume':i['props']['AudioVolume'],'path':i['media_path'],
              'provenance':{'evidence':'snapshot','track':i['track']}}
             for i in model['items'] if i['type']=='audio' and i['owner']=='user' and 'AudioVolume' in i['props']]
    return [make_event({'actions':actions},timestamp,model['snapshot']['name'],machine,creator,lineage,model['v1_source_fingerprint']),
            make_event({'actions':samples+volumes},timestamp,model['snapshot']['name'],machine+'-samples',creator,lineage,model['v1_source_fingerprint'])]
