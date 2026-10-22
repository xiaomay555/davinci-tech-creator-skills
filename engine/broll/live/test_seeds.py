from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from broll.live.seeds import seed_events
from broll.live.learn import rebuild_profile
from broll.live.test_timeline import dense
from broll.live._util import encode


class SeedTests(unittest.TestCase):
    def test_only_eleven_own_media_not_ai_or_fx(self):
        e=seed_events(dense(),'2026-10-11T22:00:00','mac')
        samples=[a for x in e for a in x['actions'] if a['kind']=='user_sample']
        self.assertEqual(len(samples),11);self.assertTrue(all(a['source']=='user_edit' for a in samples))
        self.assertTrue(all('zoom' not in a and 'aspect' not in a for a in samples))
        self.assertFalse(any(a['kind']=='audio_sample' for x in e for a in x['actions']))

    def test_verbatim_quotes_vs_ai_paraphrases_verdict_provenance(self):
        e=seed_events(dense(),'t','mac');quotes=e[0]['actions']
        self.assertEqual(sum(a['source']=='ai_paraphrase' for a in quotes),2)
        self.assertTrue(all(a.get('provenance') for a in quotes))
        p=rebuild_profile(e);self.assertTrue(p['rules']['locality']);self.assertEqual(p['blacklist'],[])
        self.assertEqual(p['version_verdict'][0]['rejected'],'sentence-1011')

    def test_observed_fields_only_and_deterministic(self):
        d=dense();own=next(i for i in d['tracks']['video2']['items'] if not i['name'].startswith('['))
        own.update(props={'ZoomX':1.2,'ZoomY':1.2},broll_extra={'aspect':'2.39:1'})
        d['tracks']['audio2']['items'][0]['props']={'AudioVolume':-5}
        a=seed_events(d,'t','mac');b=seed_events(deepcopy(d),'t','mac')
        self.assertEqual(encode(a),encode(b))
        self.assertEqual(sum(x.get('zoom')==1.2 for x in a[1]['actions']),1)
        self.assertEqual(next(x for x in a[1]['actions'] if x['kind']=='audio_sample')['volume'],-5)

    def test_numeric_duration_seed_but_no_inferred_global_single_film(self):
        p=rebuild_profile(seed_events(dense(),'t','mac'))
        self.assertEqual((p['parameters']['min_frames'],p['parameters']['max_frames']),(50,186))
        self.assertNotIn('edge_anchor',p['rules']);self.assertEqual(p['zoom'],{})

    def test_rejected_snapshot_never_enters_seed_learning(self):
        d=dense();d['timeline']='示例主时间线_逐句校对_20261011'
        self.assertEqual(encode(rebuild_profile(seed_events(d,'t','mac'))),encode(rebuild_profile([])))


if __name__=='__main__':unittest.main()
