"""T4 minor review items N01..N20 (order in the supplied review list)."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from broll import index
from broll.live import learn, placement
from broll.live.arrange import arrange
from broll.live.timeline import build_model
from broll.live.ledger import new_ledger, resolve_lineage
from broll.live._fixtures import snapshot, with_ai, item
from broll.live.test_learn import action,event,ripple
from broll.live.test_t4_review import catalog,request


class T4MinorTests(unittest.TestCase):
    def test_n01_current_ripple_positions_control_same_source_gap(self):
        s,l=with_ai(3,length=700); a=ripple(s,0,100)
        c=catalog('s2');c['s2']['file']='/media/ai2.mov'
        c.update(catalog('adjacent'));c['adjacent'].update(file='/media/ai2.mov',usable_start=350)
        p=arrange(request(lines=(3,3),end=300,key='adjacent'),c,build_model(a,l),ledger=l)
        self.assertEqual(p['placements'][0]['status'],'skipped')
        self.assertIn('同源',p['placements'][0]['reason'])

    def test_n02_target_cannot_reselect_old_segment(self):
        s,l=with_ai(); c=catalog('s0')
        p=arrange(request(spot='0',lines=(1,1),end=100,key='s0'),c,build_model(s,l),ledger=l,targeted=['0'])
        self.assertEqual(p['placements'][0]['status'],'skipped')

    def test_n03_moved_or_renamed_spot_is_not_weak_positive(self):
        s,l=with_ai(3,length=800)
        for field in ('start','track','name'):
            a=deepcopy(s);clip=a['items'][-1]
            if field=='start':clip.update(start=600,end=700)
            elif field=='track':clip['track']=3
            else:clip['name']='renamed'
            d=learn.diff_snapshots(s,a,l,approve=True)
            r=next(r for r in d['results'] if r['spot']=='2')
            self.assertFalse(r['approved']);self.assertNotEqual(r['status'],'kept')

    def test_n04_n13_replaced_own_is_not_counted_as_ai_gap_addition(self):
        s,l=with_ai(); a=deepcopy(s); a['items'].pop();a['items'].append(item('own',200,300,3))
        d=learn.diff_snapshots(s,a,l)
        self.assertEqual([r['kind'] for r in d['actions']],['replaced_own'])
        self.assertEqual(learn.rebuild_profile([event(d['actions'],d['results'])])['trend'][0]['user_added'],0)

    def test_n05_n15_lineage_selects_only_modified_copy(self):
        s,l=with_ai(); a=deepcopy(s);a['name']='copy';a['items'][-1]['props']['Pan']=5
        original=build_model(s,l); changed=build_model(a,l)
        self.assertEqual(resolve_lineage(l,[original,changed]),changed)
        self.assertEqual(resolve_lineage(l,[original,build_model(deepcopy(s),l)]),original)
        s['items'][-2]['props']['Pan']=6
        with self.assertRaisesRegex(ValueError,'哪条'):resolve_lineage(l,[build_model(s,l),changed])
        other=snapshot();other['items'][0]['media_path']='/src/new.mov'
        with self.assertRaisesRegex(ValueError,'V1|口播源'):resolve_lineage(l,[build_model(other)])

    def test_n06_recent_examples_follow_last_occurrence(self):
        old=event([action('disabled',spot='X')],stamp='1')
        middle=event([action('disabled',spot=str(n)) for n in range(6)],stamp='2')
        last=event([action('disabled',spot='X')],stamp='3')
        p=learn.rebuild_profile([old,middle,last])
        self.assertEqual(p['negative'][-1]['spot'],'X')

    def test_n07_inputs_hash_covers_event_id(self):
        r={'spot':'0','status':'kept','approved':True,'text':'sentence'}
        a=learn.make_event({'results':[r]},'t','A','mac','Creator','L')
        b=learn.make_event({'results':[r]},'t','A','mbp','Creator','L')
        self.assertNotEqual(learn.rebuild_profile([a])['inputs_hash'],learn.rebuild_profile([b])['inputs_hash'])

    def test_n08_direct_requires_confirmed_matching_frame_rate(self):
        c={'kind':'video','in_media_pool':True,'fps':29.97,'vfr':False}
        self.assertEqual(placement.choose_placement(c,0,60,timeline_fps=25)['method'],'render')
        c.pop('fps')
        self.assertEqual(placement.choose_placement(c,0,60)['method'],'render')
        c['probe']={'fps_num':30000,'fps_den':1001}
        self.assertEqual(placement.choose_placement(c,0,60)['method'],'direct')

    def test_n09_source_end_protects_speed_changed_user_footage(self):
        from broll.live.candidates import collect_candidates
        s=snapshot();s['items'].append(item('own',0,100,path='/src/own.mov',source_start=0))
        s['items'][-1]['source_end']=200
        rows=collect_candidates(build_model(s),[{'file_path':'/src/own.mov','kind':'video','fps':29.97,'duration':20}])
        self.assertGreater(rows[0]['available_ranges'][0][0],200/29.97)

    def test_n10_lut_relative_path_resolves_before_basename(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            for folder in ('Sony','Sony Venice'):
                (root/folder).mkdir();(root/folder/'look.cube').write_text('fixture')
            result=placement.select_lut({'file':'/src/log.mov','in_media_pool':True,'input_lut':'Sony/look.cube'},lut_dirs=[root])
            self.assertEqual(result['status'],'ok');self.assertEqual(result['lut_path'],str((root/'Sony/look.cube').resolve()))

    def test_n11_legacy_index_instructions_and_schema_unchanged(self):
        self.assertNotIn('sample_of',index.INSTRUCTIONS)
        self.assertIn('sample_of',index.INSTRUCTIONS_V3)
        from broll.test_index import VALUE
        with self.assertRaisesRegex(ValueError,'字段'):
            index.merge_descriptions(Path('/'),{'files':{}},[dict(segment='s',**VALUE,focus='sharp')],{})

    def test_n12_generated_offset_not_compared_to_original_source(self):
        s,l=with_ai(1); l['spots']['0'].update(source_start=300,source_file='/src/product.mov')
        c=catalog('s0');c['s0']['file']='/src/product.mov'
        c.update(catalog('next'));c['next'].update(file='/src/product.mov',usable_start=400)
        p=arrange(request(lines=(2,2),end=200,key='next'),c,build_model(s,l),ledger=l)
        self.assertEqual(p['placements'][0]['status'],'skipped');self.assertIn('同源',p['placements'][0]['reason'])

    def test_n17_historic_split_blacklist_applies_to_unsplit_parent(self):
        c=catalog('s');p=arrange(request(key='s'),c,build_model(snapshot()),{'blacklist':['s:2']})
        self.assertEqual(p['placements'][0]['status'],'skipped')

    def test_n14_unmapped_legacy_quality_is_reported(self):
        from broll.live.test_t4_review import dense_ledger
        s,l=dense_ledger();a=deepcopy(s);a['tracks']['video2']['items'].pop()
        d=learn.diff_snapshots(s,a,l);p=learn.rebuild_profile([event(d['actions'],d['results'])])
        self.assertTrue(p['quality_unavailable'])
        self.assertIn('无法统计质量',learn.learning_summary(learn.rebuild_profile([]),p))
        self.assertEqual(p['blacklist'],[])

    def test_n18_existing_flash_is_reported_without_blocking_unrelated_plan(self):
        s=snapshot();s['items'] += [item('left',0,98),item('right',100,200)]
        p=arrange(request(),catalog(),build_model(s))
        self.assertEqual(p['placements'][0]['status'],'placed');self.assertEqual(p['flashes'],[])
        self.assertEqual(p['existing_flashes'],[{'start':98,'end':100,'label':'aroll'}])

    def test_n19_subtitle_edge_preference_can_change_arrangement(self):
        rows=[action('user_sample',spot=str(n),duration=100,edges=[{'kinds':['subtitle'],'offset':0}]) for n in range(3)]
        p=learn.rebuild_profile([event(rows,film='A'),event(rows[:1],film='B')])
        self.assertEqual(p['rules']['edge_anchor'],'subtitle')
        s=snapshot();next(i for i in s['items'] if i['type']=='subtitle' and i['start']==100)['start']=102
        m=build_model(s);v=request(lines=(2,2),end=200)
        self.assertEqual(arrange(v,catalog(),m)['placements'][0]['start'],100)
        self.assertEqual(arrange(v,catalog(),m,p)['placements'][0]['start'],102)

    def test_n20_roles_markers_priority_black_exposure_and_replaced_reservation(self):
        s=snapshot()
        for n,kind in enumerate(('Generator','Adjustment Clip','Timeline')):
            clip=item('overlay'+str(n),0,100,4);clip.update(media_path=None,media_type=kind);s['items'].append(clip)
        for n,name in enumerate(('章节','不铺','露脸')):s['markers'][str(300+n*10)]={'name':name,'duration':1}
        m=build_model(s)
        self.assertTrue(all(i['role']=='overlay' for i in m['items'][-3:]))
        for n in range(3):self.assertIn([300+n*10,301+n*10],m['reserved'])
        own=deepcopy(s['items'][0]);own.update(id='other-host',track=3,start=200,end=300);s['items'].append(own)
        self.assertEqual(build_model(s)['items'][-1]['owner'],'aroll')
        for path in ('/project/B-roll片段/a.mov','/project/逐句修订Broll_1/a.mov'):
            a=snapshot();a['items'].append(item('legacy',0,100,path=path));self.assertEqual(build_model(a)['items'][-1]['owner'],'ai_legacy')
        c=catalog();c.update(catalog('priority'));c['priority']['file']='/priority/clip.mov'
        v=request(lines=(1,1),end=100);v['segments'][0]['shots'][0]['candidates'].append({'segment':'priority','reason':'素材优先'})
        self.assertEqual(arrange(v,c,build_model(snapshot()),{'material_priority':['/priority']})['placements'][0]['segment'],'priority')
        a,l=with_ai(1);b=deepcopy(a);b['items'].pop();b['items'][0]['enabled']=False
        d=learn.diff_snapshots(a,b,l);self.assertEqual(next(x for x in d['actions'] if x['kind']=='deleted')['exposed'][0]['label'],'black')
        a,l=with_ai(1);b=deepcopy(a);b['items'].pop();b['items'].append(item('own',0,100,3))
        d=learn.diff_snapshots(a,b,l);b['items'].pop();p=arrange(request(lines=(1,1),end=100),catalog(),build_model(b,d['ledger']),ledger=d['ledger'])
        self.assertEqual(p['placements'][0]['status'],'skipped')


if __name__=='__main__':unittest.main()
