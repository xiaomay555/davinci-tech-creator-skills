import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).absolute().parents[3]))
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch
from copy import deepcopy
import json
from engine.broll.live._fake_resolve import Resolve, Media
from engine.broll.live.resolve_adapter import Adapter
from engine.broll.live.service import Session, check_adjust_scope, preflight, main_for_review
from engine.broll.live.writer import snapshot_hash
from engine.broll.live.ledger import exact_state

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.r=Resolve();self.io=Adapter(self.r,self.r.project,self.r.project.current)
        self.sleep=patch('engine.fx.resolve_io.time.sleep');self.sleep.start();self.addCleanup(self.sleep.stop)
        self.config={'creator':'Creator','ledger_index':str(self.root/'registry.json'),'learning_root':str(self.root/'learning')}
        self.s=Session(self.io,self.config,self.root/'film')
        self.source=self.root/'product.mov';self.source.write_bytes(b'fake test media')
        self.io.p.pool.root.clips.append(Media(str(self.source)))
        self.catalog={name:dict(segment=name,parent_segment=name,file=str(self.source),file_path=str(self.source),kind='video',in_media_pool=True,fps='29.97',vfr=False,duration=40,usable_start=start,usable_end=start+600,focus='sharp',subject='centered',people='none',use='operation',sample_of=None,probe={'has_audio':True},size=self.source.stat().st_size,mtime_ns=self.source.stat().st_mtime_ns) for name,start in [('one',0),('two',600),('three',1200)]}
        self.s.catalog=self.catalog
        self.matches={'version':3,'segments':[{'spot':'S01','lines':[2,2],'shots':[{'end':200,'candidates':[{'segment':'one','reason':'产品画面对上口播'},{'segment':'two','reason':'产品操作近景'}]}]}],'face':[]}
    def test_auto_adopt_then_noop_does_not_write_learning_event(self):
        self.assertTrue(self.s.path.exists());self.assertEqual(self.s.events,[])
        self.s.auto_learn();self.assertEqual(self.s.events,[])
    def test_unknown_creator_asks_once(self):
        cfg=deepcopy(self.config);cfg.pop('creator')
        with self.assertRaisesRegex(ValueError,'创作者'):Session(self.io,cfg,self.root/'other')
    def test_prepare_direct_preserves_candidates_null_quality_and_no_write(self):
        p=self.s.prepare(self.matches);self.assertEqual(len(p['operations']),1);op=p['operations'][0]
        self.assertFalse(op['new']['generated']);self.assertEqual(op['new']['offset'],0);self.assertEqual(len(op['candidates']),2)
        self.assertEqual(self.io.p.pool.append_count,0)

    def test_dry_apply_outputs_sheet_without_resolve_writes(self):
        path=str(self.s.work/'演练核图/test.jpg')
        with patch('engine.broll.live.service.media.rehearsal_sheet',return_value=path) as sheet:
            out=self.s.apply(self.matches)
        self.assertEqual(out['演练核图'],path)
        shot=sheet.call_args[0][0][0]
        self.assertEqual((shot['file'],shot['source_start'],shot['end']-shot['start']),(str(self.source),0,100))
        self.assertEqual(self.io.p.pool.append_count,0)

    def test_written_apply_and_dry_adjust_keep_full_report(self):
        out=self.s.apply(self.matches,True)
        self.assertEqual(out['placements'][0]['file'],self.source.name)
        self.assertEqual(out['placements'][0]['source_start'],0)
        self.assertGreater(out['coverage'],out['previous_coverage'])
        self.s.snapshot=self.io.snapshot()
        dry=self.s.adjust(['S01'],remove=True)
        self.assertEqual(dry['coverage'],0)
        self.assertGreater(dry['previous_coverage'],0)
        self.assertIn('product.mov',dry['report']);self.assertIn('匹配遵守率：100.0%',dry['report'])
        wet=self.s.adjust(['S01'],remove=True,apply=True)
        self.assertEqual((wet['coverage'],wet['previous_coverage']),(dry['coverage'],dry['previous_coverage']))

    def test_noop_apply_still_reports_skips_filters_and_coverage(self):
        self.catalog['one']['people']='host';self.catalog['two']['people']='host'
        for apply in (False,True):
            out=self.s.apply(self.matches,apply)
            self.assertFalse(out['applied']);self.assertEqual(len(out['skipped']),1)
            self.assertEqual(out['compliance']['considered'],2)
            self.assertEqual(out['compliance']['filtered_candidates'],2)
            self.assertIn('people=host',out['report'])

    def test_lut_filter_and_quality_filter_retain_raw_candidate_count(self):
        self.catalog['one'].update(is_log=True,input_lut=None)
        self.catalog['two']['people']='host'
        out=self.s.apply(self.matches)
        self.assertEqual(out['compliance']['considered'],2)
        self.assertEqual(out['compliance']['filtered_candidates'],2)
        self.assertEqual(out['compliance']['rate'],0)
        self.assertIn('Log',out['report']);self.assertIn('people=host',out['report'])
    def test_t4_legacy_target_render_uses_safe_filename(self):
        import re
        self.s.apply(self.matches,True);self.s.snapshot=self.io.snapshot()
        legacy='[D01] 主选:video:2:1';self.s.ledger['spots'][legacy]=self.s.ledger['spots'].pop('S01')
        v=deepcopy(self.matches);v['segments'][0]['spot']=legacy;self.catalog['two']['fps']=30
        output=self.root/'generated.mov';output.write_bytes(b'fixture')
        with patch('engine.broll.live.service.media.render_live_clip',return_value={'path':str(output),'startFrame':0}) as render:
            plan=self.s.prepare(v,[legacy])
        self.assertEqual(plan['operations'][0]['spot'],legacy)
        self.assertTrue(re.fullmatch(r'[\w-]{1,64}',render.call_args[0][2]))
    def test_apply_then_adjust_only_named_one(self):
        out=self.s.apply(self.matches,True);self.assertTrue(out['applied'])
        self.s.snapshot=self.io.snapshot()
        out=self.s.adjust(['S01'],use=2,apply=True);self.assertTrue(out['applied']);self.assertEqual(self.s.ledger['spots']['S01']['segment'],'two')
    def test_n20_adjustment_surviving_next_learning_is_weak_positive(self):
        self.s.apply(self.matches,True);self.s.snapshot=self.io.snapshot()
        self.s.adjust(['S01'],use=2,apply=True)
        self.assertTrue(self.s.ledger['spots']['S01']['pending_replacement'])
        self.s.auto_learn()
        self.assertTrue(self.s.ledger['spots']['S01']['approved'])
        self.assertEqual(self.s.profile['positive'][0]['weight'],0.3)
        self.assertNotIn('pending_replacement',self.s.ledger['spots']['S01'])
    def test_n18_unrelated_existing_flash_does_not_block_writer_readback(self):
        from engine.broll.live._fake_resolve import Item
        from engine.broll.live._fixtures import item
        for name,a,b in [('left',0,98),('right',100,200)]:
            path=str(self.source);self.io.main.items.append(Item(item(name,a,b,path=path),self.io.p.pool.find(path)))
        self.s.auto_learn();v=deepcopy(self.matches);v['segments'][0].update(lines=[4,4],shots=[{'end':400,'candidates':[{'segment':'three','reason':'其他句产品画面'}]}])
        self.assertTrue(self.s.apply(v,True)['applied'])
    def test_automatic_learning_does_not_reauthorize_manual_clip_overwrite(self):
        self.s.apply(self.matches,True)
        old=self.s.ledger['spots']['S01']['placement'];handle=next(i for i in self.io.main.items if i.GetUniqueId()==old['id']);handle.SetProperty('Pan',5)
        self.s.auto_learn();self.assertEqual(self.s.ledger['spots']['S01']['placement']['props']['Pan'],5)
        out=self.s.adjust(['S01'],use=2,apply=True)
        self.assertFalse(out['applied']);self.assertEqual(handle.GetProperty('Pan'),5)
    def test_remove_reserved_then_next_plan_and_explicit_face_off(self):
        self.s.apply(self.matches,True);self.s.snapshot=self.io.snapshot()
        self.s.adjust(['S01'],remove=True,apply=True)
        self.s.snapshot=self.io.snapshot();fresh=deepcopy(self.matches);fresh['segments'][0]['spot']='new_S01'
        self.assertEqual(self.s.prepare(fresh)['operations'],[])
        self.assertTrue(self.s.adjust(['S01'],face='off',apply=True)['applied'])
    def test_audio_on_and_off_only_named(self):
        self.s.apply(self.matches,True);self.s.snapshot=self.io.snapshot()
        self.s.adjust(['S01'],audio='on',apply=True);self.assertEqual(self.s.ledger['spots']['S01']['audio']['props']['AudioVolume'],-5)
        self.s.snapshot=self.io.snapshot();self.s.adjust(['S01'],audio='off',apply=True);self.assertNotIn('audio',self.s.ledger['spots']['S01'])
    def test_segmented_spot_stores_two_shots_without_overwrite(self):
        self.matches['segments'][0].update(lines=[2,3],shots=[{'end':200,'candidates':[{'segment':'one','reason':'第一句产品操作'}]},{'end':300,'candidates':[{'segment':'two','reason':'第二句产品操作'}]}])
        self.s.apply(self.matches,True)
        self.assertIn('S01',self.s.ledger['spots']);self.assertIn('S01-shot2',self.s.ledger['spots'])
        self.s.snapshot=self.io.snapshot();self.s.adjust(['S01'],remove=True,apply=True)
        self.assertTrue(all(r['status']=='deleted' for r in self.s.ledger['spots'].values()))
    def test_lut_skip_tries_secondary_and_retains_original_candidates(self):
        self.catalog['one'].update(is_log=True,input_lut=None)
        p=self.s.prepare(self.matches)
        self.assertEqual(p['operations'][0]['new']['segment'],'two');self.assertEqual(len(p['operations'][0]['candidates']),2)
        self.assertIn('Log',p['report'])
    def test_adjust_scope_checks_set_not_only_count(self):
        with self.assertRaisesRegex(ValueError,'范围'):check_adjust_scope([{'spot':'S02'}],['S01'])
        with self.assertRaises(ValueError):check_adjust_scope([{'spot':'S01'},{'spot':'S02'}],['S01'])
        check_adjust_scope([{'spot':'S01'},{'spot':'S01'}],['S01'])
    def test_review_playhead_resolves_main_via_registry(self):
        self.s.apply(self.matches,True);self.s.snapshot=self.io.snapshot();self.s.adjust(['S01'],use=2,apply=True)
        name=next(iter(self.s.ledger['reviews']));t=next(t for t in self.io.p.timelines if t.name==name);self.io.p.current=t
        io,project_dir=main_for_review(Adapter(self.r,self.io.p,t),self.config)
        self.assertEqual(io.main_name,'main');self.assertEqual(Path(project_dir),self.root/'film')
    def test_approval_verdict_note_and_forget(self):
        self.s.apply(self.matches,True);self.s.snapshot=self.io.snapshot()
        self.s.learning_command(approve=True);self.assertTrue(self.s.events)
        self.s.learning_command(verdict='旧版更好',note='这一处留空')
        self.assertEqual(self.s.events[-1]['actions'][0]['source'],'user_quote')
        self.s.learning_command(forget='last');self.assertTrue(self.s.ignored['events'])
    def test_clean_keeps_media_pool_references_and_moves_unreferenced(self):
        folder=self.s.work/'B-roll片段';folder.mkdir(parents=True)
        used=folder/'used.mov';used.write_bytes(b'a');free=folder/'free.mov';free.write_bytes(b'b');free.with_suffix('.json').write_text('{}')
        self.io.p.pool.root.clips.append(Media(str(used)))
        rows=self.s.clean()['清单'];self.assertEqual(len(rows),2);self.assertTrue(used.exists());self.assertTrue((folder/'待清理/free.mov').exists())
    def test_preflight_all_checks_and_failure_paths(self):
        from types import SimpleNamespace
        cfg={**self.config,'nas_mount':str(self.root)}
        def run(args,**kw):return SimpleNamespace(returncode=0,stdout='Mach-O arm64 x86_64 zscale',stderr='')
        with patch('engine.broll.live.service.shutil.which',return_value='/fake/ffmpeg'),patch('engine.broll.live.service.sys.flags',SimpleNamespace(utf8_mode=1)):
            io,env=preflight(cfg,connector=lambda _: (self.r,self.io.p,self.io.main),runner=run);self.assertEqual(env['数据库'],'Disk')
            def broken(args,**kw):return SimpleNamespace(returncode=0,stdout='arm64 x86_64',stderr='')
            with self.assertRaisesRegex(ValueError,'zscale'):preflight(cfg,connector=lambda _:None,runner=broken)
    def test_stale_draft_basis_refuses_before_placement(self):
        (self.s.work/'匹配依据.json').write_text(json.dumps({'basis_hash':'old'}))
        with self.assertRaisesRegex(ValueError,'依据已变化'):self.s.prepare(self.matches)
        self.assertEqual(self.io.p.pool.append_count,0)
    def test_full_sentence_face_reservation(self):
        self.s.apply(self.matches,True);self.s.snapshot=self.io.snapshot();self.s.adjust(['S01'],face='on',apply=True)
        self.assertEqual(self.s.ledger['reserved'][0]['source_ranges'][0]['start'],1100)
        self.assertEqual(self.s.ledger['reserved'][0]['source_ranges'][0]['end'],1200)
    def test_clean_yes_deletes_previously_staged_only_after_ref_check(self):
        folder=self.s.work/'B-roll片段';folder.mkdir(parents=True);(folder/'free.mov').write_bytes(b'b')
        self.s.clean();self.assertTrue((folder/'待清理/free.mov').exists())
        self.s.clean(True);self.assertFalse((folder/'待清理/free.mov').exists())
    def test_two_modified_copies_require_explicit_selection(self):
        self.s.apply(self.matches,True);state=self.s.ledger['spots']['S01']['placement']
        original=next(i for i in self.io.main.items if i.GetUniqueId()==state['id']);original.SetProperty('Pan',5)
        copy=self.io.main.DuplicateTimeline('copy');next(i for i in copy.items if i.GetName()==state['name']).SetProperty('Pan',6)
        with self.assertRaisesRegex(ValueError,'哪条'):Session(self.io,self.config,self.root/'film')
    def test_generated_audio_only_on_request(self):
        self.s.apply(self.matches,True);row=self.s.ledger['spots']['S01'];row['placement']['media_path']=str(self.root/'generated.mov');row['source_start']=30
        h=next(i for i in self.io.main.items if i.GetUniqueId()==row['placement']['id']);h.media=Media(row['placement']['media_path'])
        self.s.snapshot=self.io.snapshot();row['placement']=next(r for r in self.s.snapshot['timeline']['items'] if r['id']==h.GetUniqueId());row['guard_state']=deepcopy(row['placement'])
        output=self.root/'audio.wav';output.write_bytes(b'audio test')
        with patch('engine.broll.live.audio_clip.render_audio',return_value={'path':str(output),'startFrame':0,'frames':100}) as render:
            out=self.s.adjust(['S01'],audio='on',apply=True)
        self.assertTrue(out['applied']);render.assert_called_once();self.assertEqual(self.s.ledger['spots']['S01']['audio']['media_path'],str(output))
    def test_approval_of_unchanged_frame_does_not_duplicate_events(self):
        self.s.apply(self.matches,True);self.s.auto_learn(True);count=len(self.s.events);self.s.auto_learn(True);self.assertEqual(count,len(self.s.events))

    def test_feedback19_original_names_and_items_survive_apply_and_adjust(self):
        media=next(m for m in self.io.p.pool.root.clips if m.GetClipProperty('File Path')==str(self.source))
        media.props['Clip Name']='用户媒体池原名称'
        with patch('engine.broll.live.service.media.render_live_clip',side_effect=AssertionError('普通素材不可渲染')):
            self.s.apply(self.matches,True)
            state=self.s.ledger['spots']['S01']['placement']
            self.assertEqual(state['name'],'用户媒体池原名称')
            self.assertEqual(state['broll_extra']['media_id'],media.GetUniqueId())
            self.assertEqual(state['media_path'],str(self.source))
            self.s.snapshot=self.io.snapshot()
            self.s.adjust(['S01'],use=2,apply=True)
            self.assertEqual(self.s.ledger['spots']['S01']['placement']['name'],'用户媒体池原名称')
        self.assertEqual(media.GetClipProperty('Clip Name'),'用户媒体池原名称')
        self.assertFalse((self.s.work/'B-roll片段').exists())

    def test_feedback19_photo_imports_original_without_render_or_rename(self):
        photo=self.root/'原截图.png';photo.write_bytes(b'fake image')
        for row in self.catalog.values():
            row.update(kind='photo',file=str(photo),file_path=str(photo),in_media_pool=False,
                       size=photo.stat().st_size,mtime_ns=photo.stat().st_mtime_ns)
        with patch('engine.broll.live.service.media.render_live_clip',side_effect=AssertionError('照片不可渲染')):
            out=self.s.apply(self.matches,True)
        self.assertTrue(out['applied'])
        state=self.s.ledger['spots']['S01']['placement']
        self.assertEqual((state['name'],state['media_path']),(photo.name,str(photo)))
        self.assertEqual(sum(m.GetClipProperty('File Path')==str(photo) for m in self.io.p.pool.root.clips),1)

    def test_feedback19_derivative_reason_and_original_source_are_in_ledger(self):
        for row in self.catalog.values():row['hdr']=True
        output=self.root/(self.source.name+'__converted.mov');output.write_bytes(b'fixture')
        with patch('engine.broll.live.service.media.render_live_clip',return_value={'path':str(output),'startFrame':0}):
            self.s.apply(self.matches,True)
        state=self.s.ledger['spots']['S01']
        self.assertEqual(state['source_file'],str(self.source))
        self.assertIn('HDR',state['derivative_reason'])

    def test_feedback19_compound_unresolved_skips_only_it_and_learns_other_edit(self):
        from engine.broll.live._fake_resolve import Item
        from engine.broll.live._fixtures import item
        self.s.apply(self.matches,True)
        state=self.s.ledger['spots']['S01']['placement']
        next(i for i in self.io.main.items if i.GetUniqueId()==state['id']).SetProperty('Pan',5)
        clip=Item(item('Compound Clip 2',300,400,path=None),Media('',kind='Compound'))
        self.io.main.items.append(clip)
        self.s.auto_learn()
        self.assertTrue(any(a['kind']=='reframed' for a in self.s.events[-1]['actions']))
        self.assertEqual(self.s.learning_skipped[0]['name'],'Compound Clip 2')
        self.assertTrue((self.s.work/'学习跳过.json').exists())
        self.assertIn(clip,self.io.main.items)
        self.assertEqual(clip.GetName(),'Compound Clip 2')
        self.io.main.items.remove(clip)
        self.s.auto_learn()
        self.assertEqual(json.loads((self.s.work/'学习跳过.json').read_text())['items'],[])

    def test_feedback19_compound_and_fusion_trace_nested_original(self):
        from engine.broll.live._fake_resolve import Item, Timeline
        from engine.broll.live._fixtures import item, snapshot
        from engine.broll.live.timeline import build_model
        for typ in ('Compound','Fusion Clip'):
            nested_value=snapshot('inside')
            nested_value['items']=[item('inside',0,500,path=str(self.source),track=1,source_start=50)]
            nested=Timeline(self.io.p,'inside',nested_value)
            media=Media('',kind=typ);media.timeline=nested
            row=item(typ+' 2',300,400,path=None,source_start=10)
            clip=Item(row,media)
            self.io.main.items.append(clip)
            model=build_model(self.io.snapshot())
            traced=next(i for i in model['items'] if i['id']==clip.GetUniqueId())
            self.assertEqual(traced['owner'],'user')
            self.assertEqual(traced['media_path'],str(self.source))
            self.assertEqual(traced['source_start'],60)
            self.assertNotIn('learning_skip_reason',traced)
            self.io.main.items.remove(clip)

    def test_feedback19_duplicate_pool_path_uses_catalog_media_id(self):
        chosen=Media(str(self.source),**{'Clip Name':'另一文件夹的原名'})
        self.io.p.pool.root.clips.append(chosen)
        for row in self.catalog.values():row['media_id']=chosen.GetUniqueId()
        self.s.apply(self.matches,True)
        state=self.s.ledger['spots']['S01']['placement']
        self.assertEqual((state['broll_extra']['media_id'],state['name']),
                         (chosen.GetUniqueId(),'另一文件夹的原名'))

    def test_feedback19_new_ai_directory_review_finds_same_project(self):
        root=self.root/'new-film';ai=root/'3.视频素材/3.7 AI素材';ai.mkdir(parents=True)
        self.s=Session(self.io,self.config,root)
        self.s.catalog=self.catalog
        self.assertEqual(self.s.work,ai/'B-roll_AI'/root.name)
        self.s.apply(self.matches,True);self.s.snapshot=self.io.snapshot()
        self.s.adjust(['S01'],use=2,apply=True)
        name=next(iter(self.s.ledger['reviews']))
        t=next(t for t in self.io.p.timelines if t.name==name);self.io.p.current=t
        io,project_dir=main_for_review(Adapter(self.r,self.io.p,t),self.config)
        self.assertEqual(Path(project_dir),root)
        self.assertEqual(Session(io,self.config,project_dir,readonly=True).work,self.s.work)
