"""Design §12 learning acceptance ①–④, with explicit control groups."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from broll.live.timeline import build_model
from broll.live.arrange import arrange, visible_frames
from broll.live.learn import diff_snapshots, make_event, rebuild_profile, ignore_learning
from broll.live.seeds import seed_events
from broll.live._fixtures import with_ai, snapshot, item
from broll.live.test_timeline import dense
from broll.live.test_learn import ripple
from broll.live._util import encode, union, intersects, digest


def replay_metrics():
    """11 real own clips are labels; proposals contain line ranges, not their edges.

    This tests arrangement, not semantic vision. Candidate selection is held
    constant between control and profile; usable frames are synthetic metadata,
    with no claim that the referenced media was probed or opened.
    A sentence hit means >=50% of answer duration is visibly B-roll. An edge hit
    means an overlapping plan has BOTH edges within ±2 frames of her edges.
    Every learned score uses seeds with that answer omitted (leave-one-out).
    Combined hit requires both; denominator is all eleven answer clips.
    """
    raw=dense();full=build_model(raw)
    answer=[i for i in full['items'] if i['owner']=='user' and i['role']=='picture' and 2<=i['track']<=4]
    baseline=deepcopy(full['snapshot']);ids={i['id'] for i in answer}
    baseline['items']=[i for i in baseline['items'] if i['id'] not in ids and not i['name'].startswith(('[D','[B','[R'))]
    model=build_model(baseline)
    matches={'segments':[]};catalog={}
    for number,i in enumerate(answer):
        lines=[l['line'] for l in model['lines'] if l['start']<i['end'] and i['start']<l['end']]
        segment='own'+str(number)
        matches['segments'].append({'spot':segment,'lines':[min(lines),max(lines)],'candidates':[segment]})
        catalog[segment]={'segment':segment,'file':i['media_path'],'usable_start':0,'usable_end':1000}
    seeds=seed_events(raw,'2026-10-11T22:00:00','test')
    scores={}
    for name in ('empty','learned'):
        sentence_hits=edge_hits=combined_hits=0
        flashes=[]
        for i in answer:
            training=deepcopy(seeds)
            for e in training:
                e['actions']=[a for a in e['actions'] if not (a['kind']=='user_sample' and a.get('start')==i['start'] and a.get('end')==i['end'] and a.get('provenance',{}).get('name')==i['name'])]
                e['id']=digest({k:v for k,v in e.items() if k!='id'})
            plan=arrange(matches,catalog,model,{} if name=='empty' else rebuild_profile(training))
            flashes+=plan['flashes']
            visible=[r for r in visible_frames(model,plan['placements']) if r['label']=='picture']
            ranges=union((max(i['start'],r['start']),min(i['end'],r['end'])) for r in visible)
            sentence=sum(e-s for s,e in ranges)>=0.5*i['duration']
            edge=any(o['status']=='placed' and intersects((i['start'],i['end']),(o['start'],o['end']))
                     and all(abs(o[k]-i[k])<=2 for k in ('start','end'))
                     for o in plan['placements'])
            sentence_hits+=sentence;edge_hits+=edge;combined_hits+=sentence and edge
        scores[name]={'answers':len(answer),'sentence_hits':sentence_hits,'edge_hits':edge_hits,
                      'combined_hits':combined_hits,'sentence_rate':sentence_hits/len(answer),
                      'edge_rate':edge_hits/len(answer),'combined_rate':combined_hits/len(answer),
                      'flashes':flashes}
    return scores


class AcceptanceTests(unittest.TestCase):
    def test_1_dense_own_picture_replay_profile_not_worse(self):
        scores=replay_metrics()
        self.assertEqual(scores['empty']['answers'],11)
        for metric in ('sentence_rate','edge_rate','combined_rate'):
            self.assertGreaterEqual(scores['learned'][metric],scores['empty'][metric])
        self.assertGreater(scores['learned']['sentence_hits'],0)
        self.assertEqual(scores['learned']['flashes'],[])

    def test_2_train_A_plan_B_filtered_local_and_zoom(self):
        s,ledger=with_ai(6,length=800);after=deepcopy(s)
        cat={f's{n}':{'segment':f's{n}','file':f'/media/ai{n}.mov','usable_start':0,'usable_end':1000,
                      'aspect':'2.39:1','people':None if n==0 else 'none'} for n in range(6)}
        # Delete the person, trim five tails to exact V1 cuts, reframe three.
        # Extend baselines first, so trim is a deliberate source-aligned action.
        for n in range(1,6):
            s['items'][-6+n]['end']+=10;s['items'][-6+n]['duration']+=10
            ledger['spots'][str(n)]['S0']['end']+=10;ledger['spots'][str(n)]['S0']['duration']+=10
            ledger['spots'][str(n)]['placement']=deepcopy(ledger['spots'][str(n)]['S0'])
        after['items']=[i for i in after['items'] if i['id']!=ledger['spots']['0']['S0']['id']]
        for i in after['items']:
            if i['name'].startswith(('[B-r-1]','[B-r-2]','[B-r-3]')):
                i['props'].update(ZoomX=1.2,ZoomY=1.2)
        delta=diff_snapshots(s,after,ledger,cat)
        self.assertEqual(sum(a['kind']=='trimmed' for a in delta['actions']),5)
        self.assertEqual(sum(a['kind']=='reframed' for a in delta['actions']),3)
        # Unknown quality is admitted by default. Two actual deletions of this
        # segment, at different spots, must be what blocks it on film B.
        extra=deepcopy(next(a for a in delta['actions'] if a['kind']=='deleted'))
        extra['spot']='second-deletion';delta['actions'].append(extra)
        event=make_event(delta,'2026-10-11T23:00:00','A','test','Creator',ledger['lineage'],'A')
        profile=rebuild_profile([event]);self.assertIn('s0',profile['blacklist'])
        self.assertEqual(profile['zoom']['2.39:1'],1.2)
        b=build_model(snapshot('B',length=800))
        cat['ordinary']={'segment':'ordinary','file':'/x/ordinary.mov','usable_start':0,'usable_end':1000,'aspect':'16:9'}
        mm={'segments':[{'spot':str(n),'lines':[n+1,n+1],'candidates':[f's{n}']} for n in range(6)]
            +[{'spot':'unrelated','lines':[7,7],'candidates':['ordinary']}]}
        control=arrange(mm,cat,b);planned=arrange(mm,cat,b,profile)
        self.assertEqual(control['placements'][0]['status'],'placed')
        self.assertEqual(planned['placements'][0]['status'],'skipped')
        self.assertTrue(all(cat[o['segment']].get('people')!='person' for o in planned['placements'] if o['status']=='placed'))
        self.assertEqual(encode(control['placements'][-1]),encode(planned['placements'][-1]))
        self.assertNotEqual(encode(control['placements'][1]),encode(planned['placements'][1]))
        self.assertEqual(planned['placements'][1]['props']['ZoomX'],1.2)
        self.assertTrue(all(o['start'] in b['cuts'] and o['end'] in b['cuts'] for o in planned['placements'] if o['status']=='placed'))
        self.assertEqual(planned['flashes'],[])

    def test_3_aroll_fx_copy_drp_negative_controls(self):
        s,l=with_ai(3)
        controls=[ripple(s,0,100),deepcopy(s),deepcopy(s),deepcopy(s)]
        title=item('Text+',300,400,4);title['media_path']=None;controls[1]['items'].append(title)
        controls[2]['name']='复制的主时间线'
        for i in controls[3]['items']:
            i['id']='drp-'+i['id']
            if i['media_path']:i['media_path']='/relink/'+Path(i['media_path']).name
        for a in controls:
            with self.subTest(name=a['name'],items=len(a['items'])):
                d=diff_snapshots(s,a,l)
                self.assertEqual(d['actions'],[])

    def test_4_ignore_learning_byte_restoration(self):
        raw=dense();seeds=seed_events(raw,'2026-10-11T22:00:00','test')
        original=rebuild_profile(seeds)
        change=make_event({'actions':[{'kind':'deleted','source':'user_edit','spot':'bad',
                                     'segment':'bad','text':'人物','quality':{'people':'person'}}]},
                          '2026-10-12T00:00:00','B','test','Creator','B-lineage')
        self.assertNotEqual(encode(original),encode(rebuild_profile(seeds+[change])))
        ignored=ignore_learning(seeds+[change],last=True)
        self.assertEqual(encode(original),encode(rebuild_profile(seeds+[change],ignored)))


if __name__=='__main__':unittest.main()
