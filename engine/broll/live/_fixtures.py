"""Synthetic snapshots shared by offline tests only."""
from copy import deepcopy


def item(name, start, end, track=2, path=None, source_start=50, kind='video', **kwargs):
    return dict(id=f'{name}:{track}:{start}:{kind}', name=name, start=start, end=end,
                duration=end-start, type=kind, track=track, track_name=f'{kind}{track}',
                media_path=path or '/media/'+name+'.mov', media_type='Video',
                source_start=source_start, source_end=source_start+end-start,
                enabled=True, props={}, broll_extra={'file_size': 1000+source_start}, **kwargs)


def snapshot(name='A', length=600, step=100):
    rows = []
    for start in range(0, length, step):
        end = min(length, start+step)
        for kind in ('video', 'audio'):
            host=item('host', start, end, 1, '/media/host.mov', start+1000, kind)
            host['broll_extra']['file_size']=50000
            rows.append(host)
        line = item(f'句子{start//step+1}', start, end, 1, kind='subtitle')
        line.update(media_path=None, source_start=None, source_end=None)
        rows.append(line)
    return {'name': name, 'id': name, 'start': 0, 'end': length, 'fps': 29.97,
            'tracks': [{'type': 'video', 'index': i, 'name': f'video{i}', 'enabled': True, 'locked': False} for i in range(1, 5)]
                      + [{'type': 'audio', 'index': 1, 'name': 'audio1', 'enabled': True},
                         {'type': 'subtitle', 'index': 1, 'name': 'subtitle1', 'enabled': True}],
            'items': rows, 'markers': {}}


def with_ai(count=3, length=600):
    from .timeline import build_model, record_to_source
    from .ledger import new_ledger
    snap = snapshot(length=length)
    for n in range(count):
        snap['items'].append(item(f'[B-r-{n}] 示例', n*100, (n+1)*100,
                                  path=f'/media/ai{n}.mov', source_start=50+n*100))
    model = build_model(snap)
    ledger = new_ledger(model, 'Creator', 'run-A')
    for n, row in enumerate(i for i in model['items'] if i['owner'] == 'ai_legacy'):
        ledger['spots'][str(n)] = {'origin': 'ai_v3', 'S0': deepcopy(row), 'placement': deepcopy(row),
                                 'semantic': {'text': f'句子{n+1}', 'source_ranges': record_to_source(model, row['start'], row['end'])},
                                 'segment': f's{n}', 'candidates': [], 'removed': []}
    return snap, ledger
