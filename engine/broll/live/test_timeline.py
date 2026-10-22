import json
from pathlib import Path
import sys
import unittest
from copy import deepcopy
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from broll.live import timeline as t
from broll.live._fixtures import snapshot, item


def dense():
    return json.loads((Path(__file__).parent/'testdata/dense_synthetic.json').read_text(encoding='utf-8'))


class TimelineTests(unittest.TestCase):
    def test_dense_owners(self):
        m = t.build_model(dense())
        self.assertEqual(sum(i['owner']=='ai_legacy' and i['track']==2 and i['type']=='video' for i in m['items']), 38)
        self.assertEqual(sum(i['owner']=='user' and i['role']=='picture' for i in m['items']), 11)
        self.assertTrue(all(i['owner']=='ai_legacy' for i in m['items'] if (i['type'],i['track']) in (('video',7),('audio',4))))

    def test_text_is_overlay_not_occupied(self):
        m = t.build_model(dense())
        self.assertTrue(all(i['role']=='overlay' and i['owner']=='fx' for i in m['items'] if i['type']=='video' and i['track']==6))
        self.assertEqual(len(m['lines']),117)
        self.assertEqual(t.select_main_track(m, 109578,109748),2)

    def test_four_exact_disabled_ranges_include_three_chapters(self):
        m = t.build_model(dense())
        self.assertEqual(m['reserved'], [[108991,109050],[109182,109252],[109543,109579],[110818,110850]])

    def test_track_false_is_not_clip_false(self):
        d = dense(); m = t.build_model(d)
        self.assertEqual(len(m['picture_occupied']),5)
        self.assertFalse(m['snapshot']['broll_extra']['track_enabled_trusted'])

    def test_no_unknown_ownership(self):
        s = snapshot(); s['items'].append(item('未知',0,100)); s['items'][-1]['media_path']=None
        with self.assertRaisesRegex(ValueError,'无法归属'): t.build_model(s)

    def test_ai_path_and_v3_name_conservative(self):
        s = snapshot(); s['items'] += [item('hash',0,100,path='/x/Master/B-roll素材/x.mov'),item('[B-ab-02] 画面',100,200)]
        m=t.build_model(s)
        self.assertTrue(all(i['owner']=='ai_legacy' for i in m['items'][-2:]))

    def test_matte_markers_ledger_and_profile_reserved(self):
        s=snapshot(); matte=item('前景',0,80,4); matte['track_name']='人物抠像';s['items'].append(matte)
        s['markers']={'100':{'name':'留空', 'duration':20},'200':{'name':'章节', 'duration':20,'customData':'broll-old'}}
        m=t.build_model(s, {'reserved':[{'start':300,'end':350}]}, {'reserved':[{'text':'句子5'}]})
        self.assertEqual(m['reserved'],[[0,80],[100,120],[300,350],[400,500]])

    def test_map_roundtrip_and_structure_ignores_ids_paths(self):
        s=snapshot(); m=t.build_model(s); sources=t.record_to_source(m,80,220)
        self.assertEqual(t.source_to_record(m,sources),[[80,220]])
        other=deepcopy(s)
        for i in other['items']: i['id']='new'+i['id']; i['media_path']=('/other/'+Path(i['media_path']).name) if i['media_path'] else None
        self.assertEqual(m['structure_fingerprint'],t.build_model(other)['structure_fingerprint'])

    def test_generic_subtitle_requires_resolved_text(self):
        s=snapshot();sub=next(i for i in s['items'] if i['type']=='subtitle'); sub['name']='Subtitle'
        with self.assertRaisesRegex(ValueError,'DRT'):t.build_model(s)
        sub['subtitle_text']='实际文本';self.assertEqual(t.build_model(s)['lines'][0]['text'],'实际文本')

    def test_unknown_enabled_and_nonlinear_mapping_fail(self):
        s=snapshot(); s['items'][0]['enabled']=None
        with self.assertRaisesRegex(ValueError,'启用'): t.build_model(s)
        s['items'][0]['enabled']=True; s['items'][0]['source_end']=1500
        with self.assertRaisesRegex(ValueError,'线性'):t.build_model(s)

    def test_fx_claimed_media_excluded(self):
        s=snapshot(); fx=item('被认领的媒体',200,300);s['items'].append(fx)
        m=t.build_model(s,fx_record={'item_ids':[fx['id']]})
        self.assertEqual(m['items'][-1]['owner'],'fx');self.assertEqual(m['picture_occupied'],[])

    def test_still_offset_null_and_no_input_mutation(self):
        s=snapshot(); photo=item('照片',0,80,path='/x/p.jpg');photo['broll_extra']['left_offset']=100000;s['items'].append(photo)
        before=deepcopy(s);m=t.build_model(s)
        self.assertIsNone(m['items'][-1]['broll_extra']['left_offset']);self.assertEqual(s,before)

    def test_overlay_ceiling_and_alternate_track(self):
        s=snapshot();s['tracks'][1]['name']='花字';s['tracks'][2]['name']='B-roll备选'
        self.assertIsNone(t.select_main_track(t.build_model(s),0,100))


if __name__=='__main__':unittest.main()
