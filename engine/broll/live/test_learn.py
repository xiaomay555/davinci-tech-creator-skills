from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from broll.live import learn
from broll.live.timeline import build_model
from broll.live._fixtures import with_ai, item
from broll.live._util import encode


def event(actions=(),results=(),stamp='2026-10-11T12:00:00',film='A',lineage=None,allowed=True):
    return learn.make_event({'actions':list(actions),'results':list(results),'update_allowed':allowed},
                            stamp,film,'mac','Creator',lineage or film+'-lineage',film)


def action(kind='deleted',spot='s',segment='seg',**kwargs):
    return dict(kind=kind,spot=spot,segment=segment,text='测试句子',source='user_edit',**kwargs)


def ripple(snap, start, end):
    """Delete one complete host sentence and ripple every downstream item."""
    result=deepcopy(snap);rows=[]
    for i in result['items']:
        if i['start']>=start and i['end']<=end:
            continue
        if i['start']>=end:
            i['start']-=end-start;i['end']-=end-start
        rows.append(i)
    result['items']=rows;result['end']-=end-start
    return result


class LearnTests(unittest.TestCase):
    def setUp(self):
        self.s,self.l=with_ai(5,length=800)
        self.cat={f's{n}':{'segment':f's{n}','description':f'画面{n}','aspect':'2.39:1'} for n in range(5)}

    def diff(self,after,**kwargs):
        return learn.diff_snapshots(self.s,after,self.l,self.cat,**kwargs)

    def test_no_kept_action_no_positive_without_approval(self):
        d=self.diff(self.s);self.assertEqual(d['actions'],[])
        p=learn.rebuild_profile([event(results=d['results'])]);self.assertEqual(p['positive'],[])
        self.assertEqual(p['parameters']['min_frames'],75)

    def test_approval_and_fx_build_only_weak_positive(self):
        for key in ('approve','fx_built'):
            d=self.diff(self.s,**{key:True});self.assertEqual(d['actions'],[])
            p=learn.rebuild_profile([event(results=d['results'])])
            self.assertEqual(len(p['positive']),5);self.assertTrue(all(a['weight']==0.3 for a in p['positive']))
            self.assertEqual(p['parameters']['max_frames'],150)

    def test_deleted_exposed_and_reserve_not_reason_inference(self):
        a=deepcopy(self.s);a['items'].pop();d=self.diff(a)
        self.assertEqual([x['kind'] for x in d['actions']],['deleted'])
        self.assertEqual(d['actions'][0]['exposed'][0]['label'],'aroll')
        self.assertNotIn('reason_class',d['actions'][0]);self.assertEqual(len(d['ledger']['reserved']),1)

    def test_replaced_own_uses_union_not_double_count(self):
        a=deepcopy(self.s);a['items'].pop()
        a['items'] += [item('她的画面1',400,425,3,source_start=500),item('她的画面2',425,450,4,source_start=700)]
        d=self.diff(a);self.assertEqual(d['actions'][0]['kind'],'replaced_own')
        self.assertEqual(len(d['actions'][0]['materials']),2);self.assertEqual(d['new_folders'],['/media'])
        a['items'][-1]['start']=400;a['items'][-1]['end']=425
        self.assertEqual(self.diff(a)['actions'][0]['kind'],'deleted')

    def test_disabled_trim_extended_slip_reframe_full_actions(self):
        a=deepcopy(self.s);ai=a['items'][-5:]
        ai[0]['enabled']=False
        ai[1]['end']-=20
        ai[2]['end']+=20
        ai[3]['source_start']+=10;ai[3]['source_end']+=10
        ai[4]['props']={'ZoomX':1.2,'ZoomY':1.2,'Pan':5,'Tilt':6,'RotationAngle':2}
        d=self.diff(a)
        self.assertEqual(set(x['kind'] for x in d['actions']),{'disabled','trimmed','extended','slipped','reframed'})
        trimmed=next(x for x in d['actions'] if x['kind']=='trimmed');self.assertEqual(trimmed['delta_frames'],-20)
        self.assertEqual(next(x for x in d['actions'] if x['kind']=='reframed')['zoom'],1.2)

    def test_new_reserved_and_marker_only_visual_feedback(self):
        a=deepcopy(self.s);a['items'][0]['enabled']=False
        a['markers']={'150':{'name':'画面换成产品近景','duration':20},'250':{'name':'这里加花字','duration':20}}
        d=self.diff(a)
        self.assertEqual(set(x['kind'] for x in d['actions']),{'new_reserved','marker_note'})
        self.assertEqual(next(x for x in d['actions'] if x['kind']=='marker_note')['source'],'user_quote')

    def test_user_added_and_fx_all_excluded(self):
        a=deepcopy(self.s);title=item('Text+',600,700,4);title['media_path']=None
        fx=item('fx视频',500,600,4,source_start=700)
        a['items'] += [title,fx]
        self.assertEqual(self.diff(a,fx_record={'item_ids':[fx['id']]})['actions'],[])

    def test_ripple_realigns_no_preference_actions(self):
        a=ripple(self.s,500,600);d=self.diff(a)
        self.assertEqual(d['actions'],[]);self.assertEqual(len(d['aroll_reedit']),1)
        a=ripple(self.s,0,100);d=self.diff(a)
        self.assertEqual(d['actions'],[]);self.assertEqual(len(d['aroll_reedit']),1)

    def test_ripple_inside_or_at_front_of_multi_sentence_ai_is_not_trim_or_slip(self):
        s,l=with_ai(1,length=400)
        clip=s['items'][-1];clip.update(end=200,duration=200,source_end=250)
        l['spots']['0']['S0']=deepcopy(clip);l['spots']['0']['placement']=deepcopy(clip)
        for cut_start in (0,100):
            a=ripple(s,cut_start,cut_start+100)
            if cut_start==0:
                # ripple() retained a crossing item; model actual ripple trim.
                current=a['items'][-1];current.update(start=0,end=100,source_start=150,source_end=250)
            else:
                current=a['items'][-1];current.update(end=100,source_end=150)
            d=learn.diff_snapshots(s,a,l)
            self.assertEqual(d['actions'],[]);self.assertEqual(len(d['aroll_reedit']),1)

    def test_unsynced_subtitles_fail_before_events(self):
        a=ripple(self.s,0,100)
        for i in a['items']:
            if i['type']=='subtitle':i['start']+=100;i['end']+=100
        with self.assertRaisesRegex(ValueError,'字幕'):self.diff(a)

    def test_drp_copy_relink_are_zero_preference_events(self):
        a=deepcopy(self.s);a['name']='copy'
        for i in a['items']:
            i['id']='new'+i['id']
            if i['media_path']:i['media_path']='/new/'+Path(i['media_path']).name
        d=self.diff(a);self.assertEqual(d['actions'],[]);self.assertEqual(d['aroll_reedit'],[])

    def test_uncertainty_over_twenty_percent_blocks_entire_profile(self):
        a=deepcopy(self.s)
        a['items'] += [deepcopy(a['items'][-1]),deepcopy(a['items'][-2])]
        a['items'][-1]['id']='dup1';a['items'][-2]['id']='dup2'
        d=self.diff(a);self.assertEqual(d['uncertain_rate'],0.4);self.assertFalse(d['update_allowed'])
        e=learn.make_event(d,'t','A','mac','Creator',self.l['lineage'])
        self.assertEqual(encode(learn.rebuild_profile([e])),encode(learn.rebuild_profile([])))
        self.assertEqual(d['ledger'],self.l)

    def test_exact_twenty_percent_can_learn_known_actions(self):
        a=deepcopy(self.s);a['items'].append(deepcopy(a['items'][-1]));a['items'][-1]['id']='dup'
        a['items'][-3]['props']['ZoomX']=1.2
        d=self.diff(a);self.assertEqual(d['uncertain_rate'],0.2);self.assertTrue(d['update_allowed'])

    def test_repeated_same_baseline_no_new_actions(self):
        a=deepcopy(self.s);a['items'].pop();first=self.diff(a)
        second=learn.diff_snapshots(a,a,first['ledger'],self.cat)
        self.assertEqual(second['actions'],[]);self.assertEqual(second['results'][-1]['status'],'deleted')
        a['items'][-1]['enabled']=False
        first=learn.diff_snapshots(first['ledger']['S0'],a,first['ledger'],self.cat)
        second=learn.diff_snapshots(a,a,first['ledger'],self.cat)
        self.assertEqual(second['actions'],[]);self.assertEqual(second['results'][-2]['status'],'disabled')

    def test_profile_deterministic_byte_order_and_no_wall_clock(self):
        a=event([action(quality={'people':'person'})]);b=event([action('rule',rule_key='edge_anchor',value='cut')],stamp='z',film='B')
        self.assertEqual(encode(learn.rebuild_profile([a,b])),encode(learn.rebuild_profile([b,a])))
        p=learn.rebuild_profile([a,b]);self.assertIn('algo_version',p);self.assertIn('inputs_hash',p)

    def test_blacklist_quality_repeat_and_semantic_exception(self):
        rows=[action(spot='1',segment='quality',quality={'focus':'missed'}),
              action(spot='2',segment='repeat'),action(spot='3',segment='repeat'),
              action(spot='4',segment='semantic',quality={'people':'person'},reason_class='semantic'),
              action(spot='5',segment='semantic',reason_class='semantic')]
        p=learn.rebuild_profile([event(rows)])
        self.assertEqual(p['blacklist'],['quality','repeat']);self.assertEqual(len(p['negative']),5)
        repeated=learn.rebuild_profile([event(rows),event(rows,stamp='z')])
        self.assertEqual(repeated['quality_deletions'],p['quality_deletions'])

    def test_ai_paraphrase_never_blacklists_or_learns_numeric(self):
        a=action(quality={'people':'person'});a['source']='ai_paraphrase'
        p=learn.rebuild_profile([event([a])]);self.assertEqual(p['blacklist'],[]);self.assertEqual(p['negative'],[])

    def test_source_ranking_latest_quote_overrides_old_quote_not_stronger_edit(self):
        a=action('quote',rule_key='material_priority',value=['昨天']);a['source']='user_quote'
        b=dict(a,value=['今天']);c=dict(b,value=['AI'],source='ai_paraphrase')
        p=learn.rebuild_profile([event([a]),event([b],stamp='y'),event([c],stamp='z')])
        self.assertEqual(p['material_priority'],['今天']);self.assertEqual(p['evidence']['material_priority']['source'],'user_quote')

    def test_global_rule_two_films_three_spots(self):
        a=event([action('rule',spot=str(i),rule_key='edge_anchor',value='cut') for i in range(3)])
        self.assertNotIn('edge_anchor',learn.rebuild_profile([a])['rules'])
        b=event([action('rule',spot='1',rule_key='edge_anchor',value='cut')],film='B')
        p=learn.rebuild_profile([a,b]);self.assertEqual(p['rules']['edge_anchor'],'cut')
        self.assertEqual(p['evidence']['edge_anchor']['films'],2)

    def test_duration_five_zoom_three_aspect_grouped_and_no_kept_numeric(self):
        actions=[action('user_sample',spot=str(i),duration=50+20*i,zoom=1.2,aspect='2.39:1',edges=[{'offset':2}]) for i in range(5)]
        p=learn.rebuild_profile([event(actions[:4])]);self.assertEqual(p['parameters']['min_frames'],75)
        p=learn.rebuild_profile([event(actions)]);self.assertEqual(p['parameters']['min_frames'],50);self.assertEqual(p['parameters']['max_frames'],130)
        self.assertEqual(p['zoom'],{'2.39:1':1.2});self.assertEqual(p['parameters']['snap_tolerance'],2)
        self.assertEqual(learn.rebuild_profile([event(actions[:2])])['zoom'],{})

    def test_latest_results_one_per_lineage_spot_and_adoption_rate(self):
        r={'spot':'s','text':'句子','status':'kept','approved':True,'weight':0.3}
        a=event(results=[r]);b=event(results=[dict(r,status='deleted',approved=False)],stamp='z')
        p=learn.rebuild_profile([a,b]);self.assertEqual(p['trend'][0]['total'],1);self.assertEqual(p['trend'][0]['adoption_rate'],0)
        # A historical approval must not remain a positive after deletion.
        self.assertEqual(p['positive'],[]);self.assertEqual(len(learn.latest_results([a,b])),1)

    def test_recent_six_examples_and_version_verdict(self):
        rows=[action(spot=str(i),segment=str(i)) for i in range(9)]
        rows.append(dict(kind='version_verdict',source='user_quote',text='旧版更好',preferred='dense',rejected='sentence'))
        p=learn.rebuild_profile([event(rows)]);self.assertEqual(len(p['negative']),6)
        self.assertEqual(p['negative'][0]['spot'],'3');self.assertTrue(p['rules']['locality'])

    def test_ignore_latest_restores_exact_bytes_and_ignore_rule(self):
        base=event([dict(kind='quote',source='user_quote',rule_key='edge_anchor',value='cut',text='贴跳切')])
        added=event([action(quality={'people':'person'})],stamp='z')
        before=learn.rebuild_profile([base]);ignored=learn.ignore_learning([base,added],last=True)
        self.assertEqual(encode(before),encode(learn.rebuild_profile([base,added],ignored)))
        ignored=learn.ignore_learning([base],rule='edge_anchor')
        self.assertNotIn('edge_anchor',learn.rebuild_profile([base],ignored)['rules'])

    def test_event_file_exclusive_timestamp_timeline_machine(self):
        with tempfile.TemporaryDirectory() as d:
            e=event([action()]);p=learn.write_event(d,e)
            self.assertIn('2026-10-11T12_00_00_A_mac',p.name);self.assertEqual(p.read_bytes(),encode(e))
            with self.assertRaises(FileExistsError):learn.write_event(d,e)
            e2=dict(e,machine='mbp');learn.write_event(d,e2);self.assertEqual(len(list(Path(d).glob('*.json'))),2)

    def test_cached_hash_mismatch_recompute_and_markdown_three_line_report(self):
        events=[event([action(quality={'focus':'missed'})])]
        with tempfile.TemporaryDirectory() as d:
            p=learn.save_profile(d,events);(Path(d)/'档案.json').write_text('{}')
            self.assertEqual(learn.load_profile(d,events),p)
            self.assertIn('依据', (Path(d)/'学到的偏好.md').read_text())
            self.assertEqual(len(learn.learning_summary(learn.rebuild_profile([]),p).splitlines()),3)

    def test_creator_isolation_and_aroll_raw_does_not_change_hash(self):
        a=event([action()]);b=deepcopy(a);b['creator']='Other';b['id']='other'
        with self.assertRaisesRegex(ValueError,'创作者'):learn.rebuild_profile([a,b])
        self.assertEqual(learn.rebuild_profile([a,b],creator='Creator'),learn.rebuild_profile([a]))
        raw=event();raw['aroll_reedit']=[{'something':'changed'}]
        self.assertEqual(encode(learn.rebuild_profile([raw])),encode(learn.rebuild_profile([])))

    def test_rejected_sentence_version_not_learning(self):
        a=deepcopy(self.s);a['name']='示例主时间线_逐句校对_20261011';a['items'].pop()
        d=self.diff(a);self.assertFalse(d['update_allowed']);self.assertEqual(d['actions'],[])

    def test_one_special_edge_does_not_expand_snap_tolerance(self):
        rows=[action('user_sample',spot=str(i),duration=80,edges=[{'offset':2},{'offset':0}]) for i in range(5)]
        rows[-1]['edges'].append({'offset':24})
        self.assertEqual(learn.rebuild_profile([event(rows)])['parameters']['snap_tolerance'],2)

    def test_disabled_not_quality_blacklist_and_numeric_rule_can_be_ignored(self):
        p=learn.rebuild_profile([event([action('disabled',quality={'people':'person'})])])
        self.assertEqual(p['blacklist'],[]);self.assertEqual(p['negative'][0]['weight'],0.3)
        rows=[action('user_sample',spot=str(i),duration=80,zoom=1.2,aspect='2.39:1') for i in range(5)]
        e=event(rows);ignored=learn.ignore_learning([e],rule='zoom:2.39:1')
        self.assertEqual(learn.rebuild_profile([e],ignored)['zoom'],{})

    def test_each_film_trend_and_removed_rule_summary(self):
        r={'spot':'s','text':'句子','status':'kept','approved':False}
        a=event(results=[r]);b=event(results=[dict(r,status='deleted')],stamp='z')
        p=learn.rebuild_profile([a,b]);self.assertEqual([x['rate'] for x in p['trend'][0]['history']],[1,0])
        self.assertIn('100.0% → 0.0%',learn.preferences_markdown(p))
        summary=learn.learning_summary({'rules':{'locality':True}},{'rules':{}})
        self.assertIn('仅修改点名位置',summary)


if __name__=='__main__':unittest.main()
