import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from broll.live.candidates import collect_candidates, build_catalog, frame_rate
from broll.live._fixtures import snapshot, item


def candidate(path='/media/a.mov', **kw):
    return dict(file_path=path,kind='video',fps='60000/1001',duration=10,input_lut=None,bin_path='素材',**kw)


class CandidateTests(unittest.TestCase):
    def test_exclude_host_audio_include_photo(self):
        rows=[candidate('/media/host.mov'),candidate(),dict(candidate('/media/s.wav'),kind='audio'),dict(candidate('/media/p.jpg'),kind='photo')]
        self.assertEqual([r['file_path'] for r in collect_candidates(snapshot(),rows)],['/media/a.mov','/media/p.jpg'])

    def test_used_range_native_rate_guards(self):
        snap=snapshot();used=item('自己',0,60,path='/media/a.mov',source_start=120)
        snap['items'].append(used); original=copy.deepcopy(snap)
        ranges=collect_candidates(snap,[candidate()])[0]['available_ranges']
        self.assertAlmostEqual(ranges[0][1],1.502);self.assertAlmostEqual(ranges[1][0],4.504,places=8)
        self.assertEqual(snap,original)

    def test_all_used_and_used_photo(self):
        snap=snapshot();snap['items'] += [item('自己',0,400,path='/media/a.mov',source_start=0),item('照片',0,100,path='/media/p.jpg')]
        self.assertEqual(collect_candidates(snap,[candidate(),dict(candidate('/media/p.jpg'),kind='photo')]),[])

    def test_named_folders_priority_pool_dedup(self):
        rows=collect_candidates(snapshot(),[candidate()],{'new_folders':['/extra'],'material_priority':['/extra']},folder_files=[candidate('/extra/x.mov'),candidate('/ignored/x.mov'),candidate()])
        self.assertEqual([r['file_path'] for r in rows],['/extra/x.mov','/media/a.mov'])
        self.assertFalse(rows[0]['in_media_pool']);self.assertTrue(rows[1]['in_media_pool'])

    def test_split_catalog_not_whole_exclusion(self):
        c=candidate();c['available_ranges']=[[0,2],[4,10]]
        value={'files':{'/media/a.mov':{'size':1,'mtime_ns':1,'segments':[{'id':'s','start':0,'end':10}]}}}
        result=build_catalog(value,[c]);self.assertEqual(len(result),2)
        self.assertEqual(result['s:1']['usable_end'],59);self.assertEqual(result['s:2']['usable_start'],120)

    def test_rates_and_missing_used_source(self):
        self.assertEqual(frame_rate(29.97),frame_rate([30000,1001]))
        with self.assertRaises(ValueError):frame_rate(0)
        snap=snapshot();row=item('自己',0,100,path='/media/a.mov');row['source_start']=None;snap['items'].append(row)
        with self.assertRaisesRegex(ValueError,'源入点'):collect_candidates(snap,[candidate()])
