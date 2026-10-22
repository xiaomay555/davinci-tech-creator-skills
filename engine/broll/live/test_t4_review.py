"""T4: independently reproduce the numbered T1/T2 review findings offline."""
from copy import deepcopy
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from broll import index, media
from broll.live import learn, matching
from broll.live.arrange import arrange, zero_flash, DEFAULTS
from broll.live.candidates import collect_candidates
from broll.live.ledger import adopt
from broll.live.timeline import build_model
from broll.live.seeds import seed_events
from broll.live._fixtures import snapshot, item, with_ai
from broll.live.test_timeline import dense
from broll.live.test_learn import event, action, ripple
from broll.test_index import VALUE


def catalog(key='new', **values):
    return {key: dict(segment=key, file='/media/new.mov', usable_start=0, usable_end=1000, **values)}


def request(spot='new', lines=(4, 4), end=400, key='new'):
    return {'version': 3, 'segments': [{'spot': spot, 'lines': list(lines),
        'shots': [{'end': end, 'candidates': [{'segment': key, 'reason': '本句产品近景'}]}]}], 'face': []}


def dense_ledger():
    s=dense(); m=build_model(s)
    return s, adopt(m, 'Creator', '1011a')


class T4ReviewTests(unittest.TestCase):
    def test_01_disabled_dense_keeps_reservation_after_learning(self):
        s,l=dense_ledger(); a=deepcopy(s)
        a['tracks']['video2']['items'][0]['enabled']=False
        d=learn.diff_snapshots(s,a,l)
        m=build_model(a,d['ledger'])
        p=arrange(request(lines=(2,2),end=m['lines'][1]['end']),catalog(),m,
                  {'parameters':{'min_frames':50}},d['ledger'])
        self.assertTrue(d['ledger']['reserved'])
        self.assertFalse(any(r['status']=='placed' for r in p['placements']))

    def test_02_trim_dense_preserved_before_and_after_learning(self):
        s,l=dense_ledger(); a=deepcopy(s)
        clip=next(i for i in a['tracks']['video2']['items'] if i['name'].startswith('[D21]'))
        clip['end']=111470
        d=learn.diff_snapshots(s,a,l)
        for book in (l,d['ledger']):
            m=build_model(a,book)
            p=arrange(request(lines=(65,65),end=m['lines'][64]['end']),catalog(),m,
                      {'parameters':{'min_frames':50}},ledger=book)
            self.assertFalse(any(r['status']=='placed' for r in p['placements']))
        self.assertTrue(d['ledger']['reserved'])

    def test_03_dense_alternate_cleanup_does_not_reserve_main(self):
        s,l=dense_ledger(); a=deepcopy(s); a['tracks']['video7']['items']=[]
        p=arrange({'segments':[]},catalog(),build_model(a,l),ledger=l)
        self.assertEqual(p['reserved'],build_model(s,l)['reserved'])

    def test_04_media_with_text_is_protected_picture(self):
        s=snapshot(); clip=item('ScreenRecording.mov',300,400,3,path='/x/ScreenRecording.mov')
        clip['texts']=['字幕']; s['items'].append(clip)
        m=build_model(s); found=next(i for i in m['items'] if i['id']==clip['id'])
        self.assertEqual((found['role'],found['owner']),('picture','user'))
        self.assertEqual(m['picture_occupied'],[[300,400]])
        self.assertEqual(arrange(request(),catalog(),m)['placements'][0]['status'],'skipped')

    def test_05_host_black_transition_flash_is_detected(self):
        for black_first in (False,True):
            s=snapshot(length=400)
            host=next(i for i in s['items'] if i['type']=='video' and i['start']==200)
            if black_first:
                host['end']=202; host['source_end']=host['source_start']+2
                s['items'].append(item('host',202,300,1,'/media/host.mov',1202))
                s['items'][-1]['broll_extra']['file_size']=50000
            host['enabled']=False
            s['items'].append(item('user',100,200 if black_first else 198))
            self.assertEqual(len(zero_flash(build_model(s))),1)
        # The planner must resolve the host flash even at tolerance one.
        s=snapshot(length=400)
        next(i for i in s['items'] if i['type']=='video' and i['start']==200)['enabled']=False
        next(i for i in s['items'] if i['type']=='subtitle' and i['start']==100)['end']=198
        p=arrange(request(lines=(2,2),end=198),catalog(),build_model(s),{'parameters':{'snap_tolerance':1}})
        self.assertEqual(p['flashes'],[])
        self.assertNotEqual(p['placements'][0]['end'],198)

    def test_06_same_filename_different_size_is_user(self):
        s=snapshot(); clip=item('host.mov',100,200,path='/other/host.mov')
        clip['broll_extra']['file_size']=777; s['items'].append(clip)
        m=build_model(s)
        self.assertEqual(next(i for i in m['items'] if i['id']==clip['id'])['owner'],'user')
        self.assertEqual(m['picture_occupied'],[[100,200]])

    def test_07_same_file_user_cuts_are_matched_one_to_one(self):
        s,l=with_ai(); own=item('C0012.MP4',400,450,3,path='/src/C0012.MP4',source_start=10)
        own['broll_extra']['file_size']=None; s['items'].append(own); l['S0']=deepcopy(s)
        a=deepcopy(s); extra=deepcopy(own); extra.update(id='added',start=500,end=560,source_start=900,source_end=960)
        a['items'].append(extra)
        d=learn.diff_snapshots(s,a,l)
        self.assertEqual([r['kind'] for r in d['actions']],['user_added'])
        reused=deepcopy(own); reused.update(id='reused',start=500,end=550)
        s['items'].append(reused)
        self.assertTrue(learn.diff_snapshots(s,s,l)['update_allowed'])
        slipped=deepcopy(s); slipped['items'][-1]['source_start']+=5
        self.assertTrue(learn.diff_snapshots(s,slipped,l)['update_allowed'])

    def test_08_deleted_spot_baseline_rebases_on_ripple(self):
        s,l=with_ai(5,length=800); a=deepcopy(s); a['items'].pop(); a=ripple(a,0,100)
        d=learn.diff_snapshots(s,a,l)
        second=learn.diff_snapshots(a,a,d['ledger'])
        self.assertEqual(second['actions'],[])
        self.assertEqual(second['ledger']['reserved'],d['ledger']['reserved'])
        self.assertNotEqual(d['ledger']['spots']['4']['S0']['start'],400)

    def test_09_upper_user_replacement_is_not_approved_kept(self):
        s,l=with_ai(); a=deepcopy(s); a['items'].append(item('user',100,200,3))
        d=learn.diff_snapshots(s,a,l,approve=True)
        self.assertIn('replaced_own',[r['kind'] for r in d['actions']])
        self.assertFalse(next(r for r in d['results'] if r['spot']=='1')['approved'])
        self.assertTrue(d['ledger']['reserved'])

    def test_10_seed_and_live_share_film_id(self):
        s,l=dense_ledger(); seeds=seed_events(s,'t','mac'); d=learn.diff_snapshots(s,s,l)
        live=learn.make_event(d,'u','dense','mac','Creator',l['lineage'])
        self.assertEqual({e['film_id'] for e in seeds+[live]}, {l['v1_source_fingerprint']})

    def test_11_uncertain_results_do_not_replace_approval(self):
        s,l=with_ai(5); first=learn.diff_snapshots(s,s,l,approve=True)
        a=deepcopy(s); a['items'].append(deepcopy(a['items'][-1])); a['items'][-1]['id']='duplicate'
        second=learn.diff_snapshots(s,a,first['ledger'])
        self.assertTrue(second['update_allowed'])
        self.assertNotIn('uncertain',[r['status'] for r in second['results']])
        before=event(results=first['results']); after=event(results=second['results'],stamp='z')
        p=learn.rebuild_profile([before,after]); self.assertEqual(len(p['positive']),5)
        self.assertEqual(p['trend'][0]['adoption_rate'],1)

    def test_12_ignore_one_blacklist_entry(self):
        e=event([action(segment=k,spot=k,quality={'focus':'missed'}) for k in ('a','b')])
        ignored=learn.ignore_learning([e],rule='blacklist:a')
        p=learn.rebuild_profile([e],ignored)
        self.assertEqual(p['blacklist'],['b']); self.assertNotIn('blacklist:a',p['evidence'])

    def test_13_split_segment_parent_controls_blacklist_and_reuse(self):
        c=catalog('s0:1',parent_segment='s0')
        p=matching.plan_matches(request(key='s0:1'),build_model(snapshot()),c,{'blacklist':['s0']})
        self.assertEqual(p['placements'][0]['status'],'skipped')
        s,l=with_ai(); p=arrange(request(key='s0:1'),c,build_model(s,l),ledger=l)
        self.assertEqual(p['placements'][0]['status'],'skipped')
        # Historic child keys are canonicalized as well.
        e=event([action(segment='s0:2',quality={'focus':'missed'})])
        self.assertEqual(learn.rebuild_profile([e])['blacklist'],['s0'])

    def mock_index(self, paths, cache, **kw):
        info={'duration':3,'hdr':False}
        def sheet(frames,path,kind): Path(path).parent.mkdir(parents=True,exist_ok=True); Path(path).touch()
        with patch.object(media,'probe',return_value=info), patch.object(media,'scene_cuts',return_value=[]), \
             patch.object(media,'extract_frames',return_value=['frames/f.jpg']), patch.object(media,'contact_sheet',side_effect=sheet):
            return index.build_index_files(paths,cache,**kw)

    def test_14_alias_index_reuses_description_and_segment_id(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); a=root/'a.mov'; a.write_bytes(b'fixture'); alias=root/'alias'; alias.mkdir()
            b=alias/'a.mov'; b.symlink_to(a); cache=root/'cache'
            self.mock_index([a],cache); first=media.read_json(cache/'index.json'); seg=next(iter(first['files'].values()))['segments'][0]['id']
            self.mock_index([a],cache,descriptions=[dict(segment=seg,**VALUE)])
            with patch.object(media,'probe',side_effect=AssertionError('must reuse')):
                r=index.build_index_files([b],cache)
            self.assertEqual(r['pending'],0)
            entry=media.read_json(cache/'index.json')['files'][str(b)]
            self.assertEqual(entry['segments'][0]['id'],seg); self.assertEqual(entry['file_path'],str(b))

    def test_15_subset_keeps_cached_descriptions(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); files=[root/'a.mov',root/'b.mov']; cache=root/'cache'
            for f in files:f.write_bytes(f.name.encode())
            self.mock_index(files,cache); value=media.read_json(cache/'index.json')
            rows=[dict(segment=s['id'],**VALUE) for e in value['files'].values() for s in e['segments']]
            self.mock_index(files,cache,descriptions=rows)
            self.mock_index(files[:1],cache); result=self.mock_index(files,cache)
            self.assertEqual(result['pending'],0)
            self.mock_index(files[:1],cache,descriptions=[])

    def test_16_existing_spot_collision_rejected_and_draft_lists_ai(self):
        s,l=with_ai(); m=build_model(s,l)
        with self.assertRaisesRegex(ValueError,'spot|点名'):
            matching.plan_matches(request(spot='0'),m,catalog(),ledger=l)
        with self.assertRaisesRegex(ValueError,'spot|点名'):
            arrange(request(spot='0'),catalog(),m,ledger=l)
        draft=matching.build_match_draft(m,catalog(),ledger=l)
        self.assertEqual(set(draft['existing_spots']),set(l['spots']))
        self.assertTrue(draft['ai_occupied']); self.assertIn('targeted',draft)

    def test_17_dense_chapter_touching_subtitles_do_not_abort(self):
        s=dense(); m=build_model(s)
        for lines in ((19,21),(19,19),(22,23),(29,30),(52,53)):
            end=m['lines'][lines[1]-1]['end']
            p=matching.plan_matches(request(lines=lines,end=end),m,catalog(),{'parameters':{'min_frames':30,'max_frames':300}})
            for r in p['placements']:
                if r['status']=='placed':
                    self.assertFalse(any(r['start']<b and a<r['end'] for a,b in m['reserved']))

    def test_18_generated_media_is_not_a_candidate(self):
        s=dense(); m=build_model(s); paths=[i['media_path'] for i in m['items'] if i['owner']=='ai_legacy'][:2]
        paths.append('/proj/B-roll_AI/B-roll片段/B01_abcd1234.mov')
        rows=[dict(file_path=p,kind='video',duration=10,fps=29.97) for p in paths]
        self.assertEqual(collect_candidates(m,rows),[])
        # Direct-reference AI source stays available outside the used slice.
        s,l=with_ai(); m=build_model(s,l); path='/media/ai0.mov'
        rows=collect_candidates(m,[dict(file_path=path,kind='video',duration=20,fps=29.97)])
        self.assertFalse(any(a<=2<=b for a,b in rows[0]['available_ranges']))

    def test_19_dense_audio_cleanup_does_not_reserve_main(self):
        s,l=dense_ledger(); a=deepcopy(s); a['tracks']['audio4']['items']=[]
        p=arrange({'segments':[]},catalog(),build_model(a,l),ledger=l)
        self.assertEqual(p['reserved'],build_model(s,l)['reserved'])

    def test_20_seed_zero_offsets_do_not_disable_cut_snapping(self):
        s=dense(); p=learn.rebuild_profile(seed_events(s,'t','mac'))
        self.assertGreaterEqual(p['parameters']['snap_tolerance'],DEFAULTS['snap_tolerance'])
        m=build_model(s); c=catalog(); c['new']['usable_end']=2000
        # Seed-own picture is removed to expose the same request for replay.
        m['items']=[i for i in m['items'] if not (i['owner']=='user' and i['role']=='picture')]
        m['picture_occupied']=[]
        value=request(lines=(6,8),end=m['lines'][7]['end'])
        r=arrange(value,c,m,p)['placements'][0]
        self.assertEqual(r['start'],108391)

    def test_21_explicit_null_is_completed_supplement(self):
        c=catalog(); c['new'].update({k:None for k in index.OPTIONAL_FIELDS})
        self.assertEqual(matching.supplement_ids(request(),c),[])
        value={'files':{'/missing.mov':{'segments':[dict(id='new',start=0,end=3,**{k:None for k in index.OPTIONAL_FIELDS})]}}}
        with tempfile.TemporaryDirectory() as t, patch.object(media,'extract_middle_frame',side_effect=AssertionError('reviewed')):
            self.assertEqual(index.supplement_descriptions(value,['new'],t)['segments'],[])

    def test_22_same_source_user_clip_cannot_claim_ai(self):
        for unknown in (False,True):
            s,l=with_ai(); old=s['items'][-1]
            if unknown:
                old['broll_extra']['file_size']=None
                for state in ('S0','placement'):l['spots']['2'][state]['broll_extra']['file_size']=None
            a=deepcopy(s); a['items'].pop()
            own=item(Path(old['media_path']).name,200,300,path=old['media_path'],source_start=900)
            own['broll_extra']['file_size']=old['broll_extra']['file_size']; a['items'].append(own)
            m=build_model(a,l); self.assertEqual(m['items'][-1]['owner'],'user')
            d=learn.diff_snapshots(s,a,l)
            self.assertIn('replaced_own',[r['kind'] for r in d['actions']]); self.assertTrue(d['ledger']['reserved'])

    def test_23_reserved_flash_pulls_back_without_filling_marker(self):
        s=snapshot(); s['markers']={'100':{'name':'这里留空','duration':1}}
        v=request(lines=(1,1),end=100); v['segments']+=request(spot='next',lines=(2,2),end=200,key='next')['segments']
        c=catalog(); c.update(catalog('next'))
        p=arrange(v,c,build_model(s))
        self.assertEqual(p['flashes'],[])
        self.assertFalse(any(r['status']=='placed' and r['start']<=100<r['end'] for r in p['placements']))


if __name__=='__main__': unittest.main()
