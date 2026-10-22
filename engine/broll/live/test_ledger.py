from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from broll.live.ledger import (match_identity, exact_state, adopt, new_ledger, resolve_lineage,
                              learning_allowed, save_ledger, load_ledger)
from broll.live.timeline import build_model
from broll.live._fixtures import snapshot, item, with_ai
from broll.live.test_timeline import dense


class LedgerTests(unittest.TestCase):
    def test_drp_relink_ids_not_identity(self):
        a=item('[B-r-01] 画面',0,100);b=deepcopy(a)
        b.update(id='changed',media_path='/another/'+Path(a['media_path']).name)
        b['broll_extra']['media_id']='new'
        self.assertEqual(match_identity(a,[b])['status'],'matched')

    def test_two_signals_one_uncertain_duplicates_uncertain(self):
        a=item('[B-r-01] 画面',0,100);b=deepcopy(a)
        b.update(name='different',source_start=300);b['broll_extra']['file_size']=None
        self.assertEqual(match_identity(a,[b])['status'],'uncertain')
        b['source_start']=a['source_start'];self.assertEqual(match_identity(a,[b])['status'],'matched')
        self.assertEqual(match_identity(a,[a,deepcopy(a)])['status'],'uncertain')

    def test_common_offset_alone_does_not_claim_unrelated_file(self):
        a=item('a',0,100);b=item('b',0,100);b['broll_extra']['file_size']=None
        self.assertEqual(match_identity(a,[b])['status'],'missing')

    def test_exact_guard_preserves_one_frame_and_transform_changes(self):
        a=item('[B-r-01]',0,100);b=deepcopy(a);b['end']-=1
        self.assertNotEqual(exact_state(a),exact_state(b))
        b=deepcopy(a);b['props']['AudioVolume']=-5
        self.assertNotEqual(exact_state(a),exact_state(b))

    def test_adopt_all_legacy_and_current_s0(self):
        m=build_model(dense());l=adopt(m,'Creator','1011a')
        self.assertEqual(len(l['spots']),80)
        self.assertEqual(sum(s['placement']['type']=='video' and s['placement']['track']==2 for s in l['spots'].values()),38)
        self.assertTrue(all(s['origin']=='ai_legacy' and s['placement']==s['S0'] for s in l['spots'].values()))
        self.assertEqual(l['S0'],m['snapshot'])

    def test_dense_adoption_no_false_ambiguity_between_main_and_alternate(self):
        from broll.live.learn import diff_snapshots
        m=build_model(dense());l=adopt(m,'Creator','1011a')
        d=diff_snapshots(m['snapshot'],m['snapshot'],l)
        self.assertEqual(d['actions'],[]);self.assertEqual(d['uncertain_rate'],0)
        self.assertTrue(d['update_allowed'])

    def test_renamed_timeline_same_lineage_ambiguous_copies_stop(self):
        a=build_model(snapshot());l=new_ledger(a,'Creator','first')
        b=build_model(snapshot('copy'))
        self.assertEqual(new_ledger(b,'Creator','first')['lineage'],l['lineage'])
        self.assertEqual(resolve_lineage(l,[b]),b)
        self.assertEqual(resolve_lineage(l,[a,b]),a)
        self.assertEqual(resolve_lineage(l,[a,b],selected='copy'),b)

    def test_defaults_do_not_learn_rejected_or_archive_review_history(self):
        for name in ('示例主时间线_逐句校对_20261011','main_archived_v01','B-roll审核_main','审片_main','B-roll/历史/main','main_history_copy'):
            self.assertFalse(learning_allowed(name),name)
        self.assertTrue(learning_allowed('示例主时间线'))

    def test_atomic_failure_does_not_damage_existing_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'B-roll台账.json';save_ledger(p,{'value':1});before=p.read_bytes()
            with patch('broll.live.ledger.os.replace',side_effect=OSError('模拟失败')):
                with self.assertRaises(OSError):save_ledger(p,{'value':2})
            self.assertEqual(p.read_bytes(),before);self.assertEqual(len(list(Path(d).iterdir())),1)
            save_ledger(p,{'value':3});self.assertEqual(load_ledger(p),{'value':3})


if __name__=='__main__':unittest.main()
