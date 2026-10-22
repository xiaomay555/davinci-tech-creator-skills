"""19B feedback regressions; all files and SDK state are disposable fakes."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tempfile import TemporaryDirectory
import unittest
from engine.broll.live.workspace import infer_work_dir, work_project_dir


class WorkspaceFeedbackTests(unittest.TestCase):
    def test_new_ai_folder_both_spellings_and_old_work_wins(self):
        for name in ('3.7AI素材', '3.7 AI素材'):
            with self.subTest(name=name), TemporaryDirectory() as tmp:
                root = Path(tmp)
                ai = root/'3.视频素材'/name
                ai.mkdir(parents=True)
                model = {'items': [{'type':'video', 'track':1,
                                  'media_path':str(root/'3.视频素材/口播.mov')}]}
                for explicit in (None, root):
                    self.assertEqual(infer_work_dir(model, explicit), ai/'B-roll_AI'/root.name)
                self.assertEqual(work_project_dir(ai/'B-roll_AI'/root.name), root)
                self.assertFalse((ai/'B-roll_AI').exists())
                old = root/'B-roll_AI'
                old.mkdir()
                self.assertEqual(infer_work_dir(model), old)
                self.assertEqual(work_project_dir(old), root)

    def test_missing_ai_folder_keeps_old_default(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(infer_work_dir({}, tmp), Path(tmp)/'B-roll_AI')

    def test_existing_spaced_work_preferred_over_empty_unspaced_folder(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'3.视频素材/3.7AI素材').mkdir(parents=True)
            existing=root/'3.视频素材/3.7 AI素材/B-roll_AI'
            existing.mkdir(parents=True)
            self.assertEqual(infer_work_dir({}, root), existing)


class CoverageFeedbackTests(unittest.TestCase):
    def test_live_profile_overrides_default_without_thirty_percent_stop(self):
        from engine.broll.live.test_arrange import catalog, matches
        from engine.broll.live._fixtures import snapshot
        from engine.broll.live.timeline import build_model
        from engine.broll.live.arrange import arrange
        model=build_model(snapshot())
        value=matches(*[(n,n,[str(n-1)]) for n in range(1,7)])
        cat=catalog()
        self.assertEqual(arrange(value,cat,model)['coverage'],1)
        for group in ('rules','parameters'):
            profile={group:{'max_coverage':.95}}
            result=arrange(value,cat,model,profile)
            self.assertGreater(result['coverage'],.3)
            self.assertLessEqual(result['coverage'],.95)
        result=arrange(value,cat,model,{'rules':{'max_coverage':.3}})
        self.assertLessEqual(result['coverage'],.3)
        self.assertTrue(any('覆盖上限' in p['reason'] for p in result['placements']))

    def test_existing_learning_profile_is_reused_and_creators_stay_separate(self):
        from engine.broll.live.learn import rebuild_profile, make_event
        from engine.broll.coverage import coverage_limit
        event=make_event({'actions':[{'kind':'rule','source':'user_quote',
            'rule_key':'max_coverage','value':.95,'text':'本次允许高覆盖'}]},
            '2026-10-16T20:00:00','main','fake','Creator','test')
        self.assertEqual(coverage_limit(rebuild_profile([event],creator='Creator')),.95)
        self.assertEqual(coverage_limit(rebuild_profile([event],creator='Other')),1)
        self.assertEqual(coverage_limit({}, {'max_coverage':.4}),.4)
        for bad in (-1,1.1,True,float('nan')):
            with self.assertRaises(ValueError):coverage_limit({'rules':{'max_coverage':bad}})

    def test_legacy_arranger_profile_can_override_global_budget(self):
        import json
        from engine.broll import plan, index
        from engine.broll.test_plan import sample_rows, fake_index, match
        rules=json.loads(index.RULES_FILE.read_text())
        rules['max_coverage']=.01
        spots=plan.validate_matches({'spots':[match()], 'face':[]},sample_rows(),fake_index(),rules)
        plain=plan.arrange(spots,sample_rows(),rules,1200)
        creator=plan.arrange(spots,sample_rows(),rules,1200,profile={'rules':{'max_coverage':.95}})
        self.assertEqual(plain[0]['frames'],0)
        self.assertGreater(creator[0]['frames'],0)


class SkillFeedbackTests(unittest.TestCase):
    def test_guidance_keeps_story_evidence_and_tool_limits_explicit(self):
        root=Path(__file__).resolve().parents[3]
        guide=(root/'skills/b-roll/references/看图与匹配.md').read_text()
        for phrase in ('这段要让观众看到/相信什么事','账号主页→相机→实际拍摄',
                       '截图、录屏','画面里的文字、操作和结果','工作或使用场景里的本人',
                       '旁边叠真实证据','约0.7秒','九成以上','约七成','2–3个实际候选',
                       '先试剪一小段','扩大范围时先确认','超出自动排布能力'):
            self.assertIn(phrase,guide)
        skill=(root/'skills/b-roll/SKILL.md').read_text()
        self.assertNotIn('现有 `apply/adjust` 若仍生成替代片段',skill)
        self.assertIn('转换原因',skill)


class CompositeFeedbackTests(unittest.TestCase):
    def test_unresolved_fusion_does_not_create_deletion_preference(self):
        from copy import deepcopy
        from engine.broll.live._fixtures import with_ai, item
        from engine.broll.live.learn import diff_snapshots
        before,book=with_ai(1)
        after=deepcopy(before)
        after['items']=[r for r in after['items'] if r['id']!=book['spots']['0']['placement']['id']]
        clip=item('Fusion Clip 2',0,100)
        clip.update(media_type='Fusion Clip',media_path=None)
        after['items'].append(clip)
        delta=diff_snapshots(before,after,book)
        self.assertTrue(delta['update_allowed'])
        self.assertEqual(delta['actions'],[])
        self.assertEqual(delta['skipped'][0]['name'],'Fusion Clip 2')
        self.assertEqual(delta['ledger']['reserved'],book['reserved'])

    def test_nonlinear_nested_source_is_skipped_instead_of_inventing_frames(self):
        from engine.broll.live._fixtures import snapshot, item
        from engine.broll.live.timeline import build_model
        snap=snapshot()
        child=item('inner',0,300,track=1,source_start=0)
        child['source_end']=600
        clip=item('Compound Clip 2',300,400,source_start=0)
        clip.update(media_type='Compound',media_path=None,nested={
            'start':0,'fps':29.97,'items':[child]})
        snap['items'].append(clip)
        model=build_model(snap)
        self.assertIn('不能可靠映射',model['learning_skipped'][0]['reason'])
        self.assertEqual(model['learning_skipped'][0]['sources'][0]['file'],child['media_path'])
