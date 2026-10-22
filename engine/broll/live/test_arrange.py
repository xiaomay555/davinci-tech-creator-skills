from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from broll.live.arrange import arrange, zero_flash, visible_frames
from broll.live.timeline import build_model
from broll.live._fixtures import snapshot, item, with_ai
from broll.live._util import encode


def catalog(n=8):
    return {str(i):{'segment':str(i),'file':f'/x/{i}.mov','usable_start':0,'usable_end':1000} for i in range(n)}


def matches(*rows):
    return {'segments':[{'spot':str(i), 'lines':[a,b], 'candidates':cs} for i,(a,b,cs) in enumerate(rows)]}


class ArrangeTests(unittest.TestCase):
    def setUp(self):
        self.snap=snapshot();self.m=build_model(self.snap);self.cat=catalog()

    def test_t7_b04_end_does_not_retreat_55_frames_to_cut(self):
        m=deepcopy(self.m)
        m['lines']=[{'line':54,'start':110937,'end':111000,'text':'行54'},
                    {'line':55,'start':111000,'end':111068,'text':'行55'}]
        m['cuts']=[110937,111013,111200]
        m['snap_points']=sorted(set(m['cuts']+[111000,111068]))
        # arrange line ranges index the supplied subtitle list.
        out=arrange(matches((1,2,['0'])),self.cat,m)['placements'][0]
        self.assertEqual((out['start'],out['end']),(110937,111068))

    def test_people_usage_and_host_cannot_be_overridden(self):
        for people,use,placed in [('person','sample',True),('person','operation',False),
                                  ('person','scene',False),('person',None,False),
                                  ('host','sample',False),('none','operation',True)]:
            with self.subTest(people=people,use=use):
                self.cat['0'].update(people=people,use=use)
                out=arrange(matches((1,1,['0'])),self.cat,self.m)
                self.assertEqual(out['placements'][0]['status']=='placed',placed)
        self.assertIn('host',arrange(matches((1,1,['0'])),dict(self.cat,**{'0':dict(self.cat['0'],people='host')}),self.m,{'hard_filters':{'people':[]}})['filtered'][0]['reason'])

    def test_no_coverage_cap_or_head_tail_protection(self):
        p=arrange(matches(*[(i,i,[str(i-1)]) for i in range(1,7)]),self.cat,self.m)
        self.assertEqual(p['coverage'],1);self.assertFalse(p['flashes'])

    def test_short_candidate_not_stretched_and_fallback(self):
        self.cat['0']['usable_end']=99
        p=arrange(matches((1,1,['0','1'])),self.cat,self.m)
        self.assertEqual(p['placements'][0]['segment'],'1')
        p=arrange(matches((1,1,['0'])),self.cat,self.m)
        self.assertEqual(p['placements'][0]['status'],'skipped');self.assertIn('不拉长',p['placements'][0]['reason'])

    def test_hard_quality_blacklist_unknown_soft_offset(self):
        for field,value in [('focus','missed'),('subject','cut_off'),('people','person'),('people','host')]:
            with self.subTest(field=field,value=value):
                self.cat['0'][field]=value
                p=arrange(matches((1,1,['0','1'])),self.cat,self.m)
                self.assertEqual(p['placements'][0]['segment'],'1');self.cat['0'].pop(field)
        self.assertEqual(arrange(matches((1,1,['0','1'])),self.cat,self.m,{'blacklist':['0']})['placements'][0]['segment'],'1')
        self.cat['0']['focus']='soft';self.cat['1']['subject']='centered'
        self.assertEqual(arrange(matches((1,1,['0','1'])),self.cat,self.m)['placements'][0]['segment'],'1')

    def test_trim_around_occupied_and_reserved_edges(self):
        self.snap['items'].append(item('用户画面',0,20))
        m=build_model(self.snap,{'reserved':[{'start':180,'end':200}]})
        p=arrange(matches((1,1,['0']),(2,2,['1'])),self.cat,m)
        self.assertEqual([(o['start'],o['end']) for o in p['placements']],[(20,100),(100,180)])

    def test_face_and_profile_reservation(self):
        mm=matches((1,1,['0']),(2,2,['1']));mm['face']=[{'lines':[1,1]}]
        p=arrange(mm,self.cat,self.m,{'reserved':[{'text':'句子2'}]})
        self.assertTrue(all(o['status']=='skipped' for o in p['placements']))

    def test_multishot_continuity_only_at_cut(self):
        mm={'segments':[{'spot':'s','lines':[1,3],'shots':[{'end':100,'candidates':['0']},{'end':200,'candidates':['1']},{'end':300,'candidates':['2']}]}]}
        p=arrange(mm,self.cat,self.m);self.assertEqual(p['coverage'],0.5);self.assertEqual(p['flashes'],[])
        mm['segments'][0]['shots'][0]['end']=97
        with self.assertRaisesRegex(ValueError,'跳切点'):arrange(mm,self.cat,self.m)

    def test_invalid_candidates_lines_and_shots_rejected(self):
        for mm in [matches((0,1,['0'])),matches((1,1,['missing'])),matches((1,1,['0','1','2','3']))]:
            with self.subTest(mm=mm):
                with self.assertRaises(ValueError):arrange(mm,self.cat,self.m)

    def test_no_repeat_segment_and_one_second_same_source_gap(self):
        mm=matches((1,1,['0']),(2,2,['0','1']))
        self.cat['1']['file']=self.cat['0']['file'];self.cat['1']['usable_start']=120
        p=arrange(mm,self.cat,self.m);self.assertEqual(p['placements'][1]['status'],'skipped')
        self.cat['1']['usable_start']=130
        self.assertEqual(arrange(mm,self.cat,self.m)['placements'][1]['status'],'placed')

    def test_main_track_no_new_track(self):
        self.snap['tracks'][1]['name']='花字';m=build_model(self.snap)
        p=arrange(matches((1,1,['0'])),self.cat,m)
        self.assertEqual(p['placements'][0]['status'],'skipped');self.assertIn('主选轨',p['placements'][0]['reason'])

    def test_locality_preserved_and_deleted_never_refilled(self):
        snap,ledger=with_ai();m=build_model(snap,ledger)
        request=matches((1,1,['0']),(4,4,['3']));request['segments'][0]['spot']='new0'
        request['segments'][1]['spot']='new1'
        p=arrange(request,self.cat,m,ledger=ledger)
        self.assertEqual(len(p['preserved']),3);self.assertEqual(p['placements'][0]['status'],'skipped')
        snap['items']=[i for i in snap['items'] if i['id']!=ledger['spots']['0']['S0']['id']]
        request=matches((1,1,['0']));request['segments'][0]['spot']='new0'
        p=arrange(request,self.cat,build_model(snap,ledger),ledger=ledger)
        self.assertEqual(p['placements'][0]['status'],'skipped');self.assertIn([0,100],p['reserved'])

    def test_target_guard_exact_and_coverage_drop(self):
        snap,ledger=with_ai();m=build_model(snap,ledger)
        p=arrange({'segments':[]},self.cat,m,ledger=ledger,targeted=['0','1'])
        self.assertTrue(p['needs_coverage_confirmation']);self.assertEqual(len(p['removed_ids']),2)
        snap['items'][-3]['props']['Pan']=1
        p=arrange(matches((1,1,['0'])),self.cat,build_model(snap,ledger),ledger=ledger,targeted=['0'])
        self.assertEqual(p['removed_ids'],[]);self.assertEqual(p['placements'][0]['status'],'skipped')

    def test_flash_visible_overlay_disabled_v1_black(self):
        self.snap['items'] += [item('左',0,99),item('右',101,200)]
        title=item('Text+',99,101,4);title['media_path']=None;self.snap['items'].append(title)
        for i in self.snap['items']:
            if i['type']=='video' and i['track']==1:i['enabled']=False
        m=build_model(self.snap)
        self.assertEqual(zero_flash(m),[{'start':99,'end':101,'label':'black'}])
        self.snap['items'][-2]['enabled']=False
        self.assertEqual(zero_flash(build_model(self.snap)),[])

    def test_flash_fill_only_own_spare_or_shrink(self):
        # Subtitle ends at 99; following user's picture starts at 101.
        next(i for i in self.snap['items'] if i['type']=='subtitle')['end']=99
        self.snap['items'].append(item('用户',101,200))
        m=build_model(self.snap);self.cat['0']['usable_end']=101
        p=arrange(matches((1,1,['0'])),self.cat,m)
        self.assertEqual(p['placements'][0]['end'],101);self.assertEqual(p['flashes'],[])
        self.cat['0']['usable_end']=99
        p=arrange(matches((1,1,['0'])),self.cat,m)
        self.assertEqual(p['placements'][0]['status'],'skipped');self.assertEqual(p['flashes'],[])

    def test_zoom_group_and_black_bars(self):
        profile={'zoom':{'2.39:1':1.2}};self.cat['0']['aspect']='2.39:1'
        p=arrange(matches((1,1,['0'])),self.cat,self.m,profile)
        self.assertEqual(p['placements'][0]['props'],{'ZoomGang':False,'ZoomX':1.2,'ZoomY':1.2})
        self.cat['0']['black_bars']=True
        p=arrange(matches((1,1,['0'])),self.cat,self.m,profile)
        self.assertNotIn('props',p['placements'][0]);self.assertEqual(p['placements'][0]['zoom_suggestion'],1.2)

    def test_pure_and_unrelated_placement_identical(self):
        mm=matches((1,1,['0']),(2,2,['1']));before=deepcopy((mm,self.cat,self.m))
        plain=arrange(mm,self.cat,self.m);learned=arrange(mm,self.cat,self.m,{'blacklist':['0']})
        self.assertEqual(encode(plain['placements'][1]),encode(learned['placements'][1]))
        self.assertEqual(before,(mm,self.cat,self.m))

    def test_overlapping_semantic_proposals_do_not_create_layered_ai(self):
        p=arrange(matches((1,1,['0']),(1,1,['1'])),self.cat,self.m)
        self.assertEqual([o['status'] for o in p['placements']],['placed','skipped'])

    def test_flash_repair_cannot_consume_same_source_one_second_gap(self):
        # End 100 / next start 103: extension would reduce source gap 32 to 29.
        for i in self.snap['items']:
            if i['type']=='subtitle' and i['start']==100:i['start']=103
        for i in self.snap['items']:
            if i['type']=='video' and i['track']==1 and i['start']==100:i['start']=103;i['source_start']+=3
        self.cat['1'].update(file=self.cat['0']['file'],usable_start=132)
        p=arrange(matches((1,1,['0']),(2,2,['1'])),self.cat,build_model(self.snap))
        self.assertEqual(p['flashes'],[])
        self.assertFalse(any(o['status']=='placed' and o['start']==0 and o['end']==103 for o in p['placements']))


if __name__=='__main__':unittest.main()
