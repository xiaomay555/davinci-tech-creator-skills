"""T6 regressions: local synthesized/mocked media; no SDK connection or NAS."""
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).absolute().parents[3]))
from engine.broll import index, media

INFO={'duration':4,'width':64,'height':48,'fps_num':30,'fps_den':1,'hdr':False}


class MediaFixtures:

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.cache=self.root/'cache'
        self.good=self.root/'a.mov';self.bad=self.root/'b.mov';self.later=self.root/'c.mov'
        for p in (self.good,self.bad,self.later):p.write_bytes(b'local fixture')

    def frames(self,path,segment,directory,kind):
        p=Path(directory)/f"{segment['id']}_0.jpg";p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'frame')
        return ['frames/'+p.name]

    def sheet(self,frames,path,kind):
        Path(path).parent.mkdir(parents=True,exist_ok=True);Path(path).write_bytes(b'sheet')

    def build(self,paths,stage=None):
        def probe(p,kind):
            if stage=='probe' and str(p)==str(self.bad):raise RuntimeError('probe failed')
            return INFO
        def cuts(p,threshold):
            if stage=='cuts' and str(p)==str(self.bad):raise RuntimeError('cuts failed')
            return []
        def frames(p,*args):
            if stage=='frames' and str(p)==str(self.bad):raise RuntimeError('frames failed')
            return self.frames(p,*args)
        with patch.object(media,'probe',side_effect=probe),patch.object(media,'scene_cuts',side_effect=cuts),patch.object(media,'extract_frames',side_effect=frames),patch.object(media,'contact_sheet',side_effect=self.sheet):
            return index.build_index_files(paths,self.cache)


class FileFailureTests(MediaFixtures, unittest.TestCase):
    def test_each_file_failure_skips_and_later_files_finish_and_cache_reuses(self):
        for stage in ('probe','cuts','frames'):
            with self.subTest(stage=stage):
                self.cache=self.root/stage
                out=self.build([self.good,self.bad,self.later],stage)
                self.assertEqual(out['files'],2);self.assertEqual(out['skipped'][0]['file'],str(self.bad))
                self.assertIn(stage,out['skipped'][0]['reason'])
                self.assertEqual(set(media.read_json(self.cache/'index.json')['files']),{str(self.good),str(self.later)})
                with patch.object(media,'probe',side_effect=AssertionError('redecode')):
                    self.assertEqual(index.build_index_files([self.good,self.later],self.cache)['files'],2)

    def test_offline_and_unsupported_are_reported(self):
        out=self.build([self.good,self.root/'missing.mov',self.root/'a.arw'])
        self.assertEqual(out['files'],1);self.assertEqual(len(out['skipped']),2)

    def test_describe_successes_without_failed_file_blocking_submission(self):
        from engine.broll.test_index import VALUE
        self.build([self.good,self.bad],'probe')
        value=media.read_json(self.cache/'index.json')
        segment=value['files'][str(self.good)]['segments'][0]['id']
        with patch.object(media,'probe',side_effect=AssertionError('no redecode')):
            stderr=StringIO()
            with redirect_stderr(stderr):
                out=index.build_index_files([self.good,self.bad],self.cache,descriptions=[dict(segment=segment,**VALUE)])
        self.assertEqual(out['pending'],0);self.assertEqual(out['skipped'][0]['file'],str(self.bad))
        self.assertEqual(out['estimate']['description_minutes'],0)
        self.assertIn('描述约 0.0 分钟',stderr.getvalue())

    def test_estimate_counts_only_remaining_descriptions(self):
        from engine.broll.test_index import VALUE
        out=self.build([self.good,self.later])
        value=media.read_json(self.cache/'index.json')
        segment=value['files'][str(self.good)]['segments'][0]['id']
        out=index.build_index_files([self.good,self.later],self.cache,descriptions=[dict(segment=segment,**VALUE)])
        self.assertEqual(out['pending'],1)
        self.assertEqual(out['estimate']['description_minutes'],.2)

    def test_sheet_failure_and_timeout_are_isolated(self):
        for failure in (RuntimeError('sheet failed'),subprocess.TimeoutExpired('ffmpeg',20)):
            self.cache=self.root/type(failure).__name__
            calls=0
            def sheet(frames,path,kind):
                nonlocal calls
                calls+=1
                if calls==2:raise failure
                self.sheet(frames,path,kind)
            with patch.object(media,'probe',return_value=INFO),patch.object(media,'scene_cuts',return_value=[]),patch.object(media,'extract_frames',side_effect=self.frames),patch.object(media,'contact_sheet',side_effect=sheet):
                out=index.build_index_files([self.good,self.bad,self.later],self.cache)
            self.assertEqual(out['files'],2);self.assertEqual(out['skipped'][0]['file'],str(self.bad))

    def test_real_ffmpeg_corrupt_file_does_not_block_healthy_video(self):
        from engine.broll.test_index import generate
        generate(self.good)
        out=index.build_index_files([self.good,self.bad],self.cache)
        self.assertEqual(out['files'],1);self.assertEqual(out['skipped'][0]['file'],str(self.bad))
        probe=media.probe
        def bad_only(path,kind):
            self.assertEqual(str(path),str(self.bad));return probe(path,kind)
        with patch.object(media,'probe',side_effect=bad_only):
            self.assertEqual(index.build_index_files([self.good,self.bad],self.cache)['files'],1)

    def test_heic_grid_is_decoded_before_probe_and_frame_scaling(self):
        heic=self.root/'grid.HEIC';heic.write_bytes(b'grid fixture')
        def decode(args):
            # Full grid decoding must not select tile stream 0 or scale tile metadata.
            self.assertNotIn('-map',args);self.assertNotIn('-vf',args)
            Path(args[-1]).write_bytes(b'assembled image')
            return subprocess.CompletedProcess(args,0,'','')
        def probe(p,kind):
            self.assertEqual(Path(p).suffix,'.png');return dict(INFO,width=4032,height=3024)
        def frames(p,*args):
            self.assertEqual(Path(p).suffix,'.png');return self.frames(p,*args)
        with patch.object(media,'command',side_effect=decode) as call,patch.object(media,'probe',side_effect=probe),patch.object(media,'extract_frames',side_effect=frames),patch.object(media,'contact_sheet',side_effect=self.sheet):
            self.assertEqual(index.build_index_files([heic],self.cache)['files'],1)
        call.assert_called_once()
        self.assertEqual(media.read_json(self.cache/'index.json')['files'][str(heic)]['probe']['width'],4032)
        heic.write_bytes(b'changed grid')
        with patch.object(media,'command',side_effect=RuntimeError('decode failed')):
            out=index.build_index_files([heic],self.cache)
        self.assertEqual(out['files'],0);self.assertIn('decode failed',out['skipped'][0]['reason'])


class LayerTests(MediaFixtures, unittest.TestCase):
    def test_feedback19_ai_workdir_still_indexes_whole_project_first(self):
        from engine.broll.live import service
        from engine.broll.live._fake_resolve import Resolve, Media
        from engine.broll.live.resolve_adapter import Adapter
        film=self.root/'film';ai=film/'3.视频素材/3.7AI素材';ai.mkdir(parents=True)
        source=film/'3.视频素材/产品.mov';source.write_bytes(b'fixture')
        r=Resolve();io=Adapter(r,r.project,r.project.current)
        io.p.pool.root.clips.append(Media(str(source)))
        cfg={'creator':'Codex','ledger_index':str(self.root/'registry.json'),'learning_root':str(self.root/'learning')}
        session=service.Session(io,cfg,film)
        with patch.object(media,'probe',return_value=INFO),patch.object(media,'scene_cuts',return_value=[]),patch.object(media,'extract_frames',side_effect=self.frames),patch.object(media,'contact_sheet',side_effect=self.sheet):
            result=session.plan()
        self.assertEqual(session.work,ai/'B-roll_AI'/film.name)
        self.assertEqual(result['候选分层']['first_files'],1)
        self.assertEqual({row['file'] for row in session.catalog.values()},{str(source)})

    def test_default_layer_more_cache_and_profile_priority(self):
        from engine.broll.live import service
        from engine.broll.live._fake_resolve import Resolve, Media
        from engine.broll.live.resolve_adapter import Adapter
        r=Resolve();io=Adapter(r,r.project,r.project.current)
        film=self.root/'film';film.mkdir();one=film/'older.mov';one.write_bytes(b'one')
        recent=film/'recent.mov';recent.write_bytes(b'recent')
        external=self.root/'external.mov';external.write_bytes(b'external')
        for p in (one,recent,external):io.p.pool.root.clips.append(Media(str(p)))
        cfg={'creator':'Codex','ledger_index':str(self.root/'registry.json'),'learning_root':str(self.root/'learning')}
        s=service.Session(io,cfg,film);s.profile['material_priority']=['recent']
        with patch.object(media,'probe',return_value=INFO),patch.object(media,'scene_cuts',return_value=[]),patch.object(media,'extract_frames',side_effect=self.frames),patch.object(media,'contact_sheet',side_effect=self.sheet):
            out=s.plan()
            self.assertEqual({row['file'] for row in s.catalog.values()},{str(one),str(recent)})
            self.assertEqual(out['候选分层']['message'],'第二层还有 1 个文件可补')
            draft=media.read_json(s.work/'B-roll匹配稿.json')
            self.assertEqual(draft['candidate_layers']['second_files'],1)
            self.assertEqual(draft['catalog'][0]['file'],str(recent))
            self.assertEqual(next(row['priority'] for row in s.catalog.values() if row['file']==str(recent)),0)
            def new_only(path,kind):
                self.assertEqual(str(path),str(external));return INFO
            with patch.object(media,'probe',side_effect=new_only):s.plan(more=True)
        self.assertEqual({row['file'] for row in s.catalog.values()},{str(one),str(recent),str(external)})
        with patch.object(media,'probe',side_effect=AssertionError('cache must be reused')):
            s.plan()
        self.assertEqual({row['file'] for row in s.catalog.values()},{str(one),str(recent)})

    def test_explicit_external_folder_is_included_without_more(self):
        from engine.broll.live import service
        from engine.broll.live._fake_resolve import Resolve
        from engine.broll.live.resolve_adapter import Adapter
        r=Resolve();io=Adapter(r,r.project,r.project.current)
        folder=self.root/'supplement';folder.mkdir();photo=folder/'a.jpg';photo.write_bytes(b'photo')
        cfg={'creator':'Codex','ledger_index':str(self.root/'registry.json'),'learning_root':str(self.root/'learning')}
        s=service.Session(io,cfg,self.root/'film')
        with patch.object(media,'probe',return_value=INFO),patch.object(media,'extract_frames',side_effect=self.frames),patch.object(media,'contact_sheet',side_effect=self.sheet):
            s.plan(folders=[str(folder)])
        self.assertEqual({row['file'] for row in s.catalog.values()},{str(photo)})
        with patch.object(media,'probe',side_effect=AssertionError('cached folder must not decode')):
            s.plan(folders=[str(folder)])

    def test_cached_external_folder_video_avoids_inventory_reprobe(self):
        from engine.broll.live import service
        from engine.broll.live._fake_resolve import Resolve
        from engine.broll.live.resolve_adapter import Adapter
        r=Resolve();io=Adapter(r,r.project,r.project.current)
        folder=self.root/'supplement';folder.mkdir();video=folder/'a.mov';video.write_bytes(b'video')
        cfg={'creator':'Codex','ledger_index':str(self.root/'registry.json'),'learning_root':str(self.root/'learning')}
        s=service.Session(io,cfg,self.root/'film')
        with patch.object(media,'probe',return_value=INFO),patch.object(media,'scene_cuts',return_value=[]),patch.object(media,'extract_frames',side_effect=self.frames),patch.object(media,'contact_sheet',side_effect=self.sheet):
            s.plan(folders=[str(folder)])
        with patch.object(media,'probe',side_effect=AssertionError('cached folder must not decode')):
            s.plan(folders=[str(folder)])


class LimitTests(MediaFixtures, unittest.TestCase):
    def test_default_twelve_uniform_windows_and_report(self):
        with patch.object(media,'probe',return_value=dict(INFO,duration=1440)),patch.object(media,'scene_cuts',side_effect=AssertionError('long source must not scan scenes')),patch.object(media,'extract_frames',side_effect=self.frames),patch.object(media,'contact_sheet',side_effect=self.sheet):
            out=index.build_index_files([self.good],self.cache)
        entry=media.read_json(self.cache/'index.json')['files'][str(self.good)]
        spans=[(s['start'],s['end']) for s in entry['segments']]
        self.assertEqual(len(spans),12);self.assertEqual(spans[0][0],0);self.assertEqual(spans[-1][1],1440)
        gaps=[b[0]-a[0] for a,b in zip(spans,spans[1:])]
        self.assertLessEqual(max(gaps)-min(gaps),12)
        self.assertEqual(out['limited'][0]['message'],'长素材只看了 12 段')

    def test_configurable_cap_and_rule_invalidation(self):
        rules=json.loads(index.RULES_FILE.read_text())
        self.build([self.good]);rules['max_segments_per_file']=3
        with patch.object(media,'probe',return_value=dict(INFO,duration=240)),patch.object(media,'extract_frames',side_effect=self.frames),patch.object(media,'contact_sheet',side_effect=self.sheet):
            out=index.build_index_files([self.good],self.cache,rules=rules)
        self.assertEqual(out['segments'],3);self.assertEqual(out['limited'][0]['message'],'长素材只看了 3 段')
        for bad in (0,-1,2.5,True):
            with self.subTest(bad=bad),self.assertRaisesRegex(ValueError,'正整数'):
                index.build_index_files([self.good],self.cache,rules=dict(rules,max_segments_per_file=bad))

    def test_uneven_scene_lengths_sample_by_time(self):
        rules=json.loads(index.RULES_FILE.read_text())
        segments=media.segments('a',{'duration':1440},'video',rules,cuts=list(range(1,121)),max_segments=12)
        self.assertEqual(len(segments),12)
        self.assertGreater(segments[6]['start'],650);self.assertEqual(segments[-1]['end'],1440)
        single=media.segments('a',{'duration':1440},'video',rules,max_segments=1)
        self.assertLess(abs((single[0]['start']+single[0]['end'])/2-720),12)

    def test_rule_change_regenerates_existing_contact_sheet(self):
        self.build([self.good])
        rules=json.loads(index.RULES_FILE.read_text());rules['max_segments_per_file']=3
        with patch.object(media,'probe',return_value=INFO),patch.object(media,'scene_cuts',return_value=[]),patch.object(media,'extract_frames',side_effect=self.frames),patch.object(media,'contact_sheet',side_effect=self.sheet) as sheet:
            index.build_index_files([self.good],self.cache,rules=rules)
        sheet.assert_called_once()


class EstimateTests(MediaFixtures, unittest.TestCase):
    def test_estimate_precedes_decode_and_progress_every_twenty_on_stderr(self):
        paths=[];err=StringIO()
        for n in range(41):
            p=self.root/f'{n:02}.mov';p.write_bytes(b'fixture');paths.append({'file_path':str(p),'duration':1440})
        def probe(p,kind):
            self.assertIn('要处理 41 个文件，预计切段约',err.getvalue())
            self.assertIn('描述约',err.getvalue());return dict(INFO,duration=1440)
        with redirect_stderr(err),patch.object(media,'probe',side_effect=probe),patch.object(media,'extract_frames',side_effect=self.frames),patch.object(media,'contact_sheet',side_effect=self.sheet):
            out=index.build_index_files(paths,self.cache)
        self.assertIn('20/41',err.getvalue());self.assertIn('40/41',err.getvalue())
        self.assertEqual(out['estimate']['description_minutes'],98.4)
        err=StringIO()
        with redirect_stderr(err),patch.object(media,'probe',side_effect=AssertionError('cached')):
            out=index.build_index_files(paths,self.cache)
        self.assertEqual(out['estimate']['split_minutes'],0);self.assertEqual(out['estimate']['cached_files'],41)


class WorkDirTests(unittest.TestCase):
    def model(self,path):
        return {'items':[{'type':'video','track':1,'media_path':path}]}

    def test_live_music_subfolder_uses_production_project_root(self):
        from engine.broll.live.workspace import infer_work_dir
        self.assertEqual(infer_work_dir(self.model('/project/示例产品体验/3.视频素材/3.2音乐素材/C3745.MP4')),Path('/project/示例产品体验/B-roll_AI'))

    def test_containing_markers_case_and_generic_material_fallback(self):
        from engine.broll.live.workspace import infer_work_dir
        for folder in ('本片视频素材归档','原片备份','拍摄当天','我的口播','素材包','A-roll源','aroll原始'):
            with self.subTest(folder=folder):
                self.assertEqual(infer_work_dir(self.model('/project/film/'+folder+'/nested/a.mp4')),Path('/project/film/B-roll_AI'))
        self.assertEqual(infer_work_dir(self.model('/project/film/a.mp4')),Path('/project/film/B-roll_AI'))

    def test_nearest_specific_marker_and_explicit_override(self):
        from engine.broll.live.workspace import infer_work_dir
        model=self.model('/project/视频素材/film/原片/a.mp4')
        self.assertEqual(infer_work_dir(model),Path('/project/视频素材/film/B-roll_AI'))
        self.assertEqual(infer_work_dir(model,'/explicit'),Path('/explicit/B-roll_AI'))
        model['items'].append({'type':'video','track':1,'media_path':'/other/原片/b.mp4'})
        with self.assertRaisesRegex(ValueError,'无法唯一'):infer_work_dir(model)


class ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        from engine.broll.live._fake_resolve import Resolve
        from engine.broll.live.resolve_adapter import Adapter
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.r=Resolve();self.io=Adapter(self.r,self.r.project,self.r.project.current)
        self.cfg={'creator':'Codex','ledger_index':str(self.root/'registry.json'),'learning_root':str(self.root/'learning')}

    def state(self):
        return {str(p.relative_to(self.root)):(p.read_bytes(),p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}

    def test_new_status_session_creates_no_files_or_directories(self):
        from engine.broll.live.service import Session
        with patch('engine.broll.live.service.learn.load_profile',side_effect=AssertionError('writeful loader')),patch('engine.broll.live.service.Session.auto_learn',side_effect=AssertionError('auto learn')):
            s=Session(self.io,self.cfg,self.root/'film',readonly=True)
        self.assertEqual(list(self.root.iterdir()),[]);self.assertFalse(s.work.exists())
        with self.assertRaisesRegex(ValueError,'只读'):s.save()

    def test_existing_status_with_manual_change_and_stale_profile_is_byte_readonly(self):
        from engine.broll.live.service import Session
        s=Session(self.io,self.cfg,self.root/'film')
        profile=s.learning/'档案.json';profile.write_text('{"stale":true}')
        self.io.main.items[0].SetProperty('Pan',17)
        before=self.state()
        readonly=Session(self.io,self.cfg,self.root/'film',readonly=True)
        self.assertEqual(self.state(),before)
        self.assertEqual(readonly.ledger['last_learned'],s.ledger['last_learned'])
        self.assertEqual(readonly.events,[])

    def test_cli_status_is_readonly_but_plan_apply_adjust_learn_allow_learning(self):
        import importlib.util
        from contextlib import redirect_stdout
        from unittest.mock import Mock
        spec=importlib.util.spec_from_file_location('t6_cli',Path(__file__).absolute().parents[3]/'bin/broll.py')
        cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        config=self.root/'config.json';config.write_text(json.dumps(self.cfg))
        preflight=lambda *_:(self.io,{'数据库':'Disk'})
        before=self.state()
        with redirect_stdout(StringIO()):
            code=cli.main(['status','--config',str(config),'--project-dir',str(self.root/'film')],preflight=preflight)
        self.assertEqual(code,0);self.assertEqual(self.state(),before);self.assertFalse((self.root/'film').exists())
        for command in ('plan','apply','adjust','learn','profile','at','undo','clean'):
            args=[command,'--config',str(config),'--project-dir',str(self.root/'film')]
            if command=='apply':
                matches=self.root/'matches.json';matches.write_text('{}');args+=['--matches',str(matches)]
            if command=='adjust':args+=['--spot','S01','--remove']
            if command=='at':args+=['--playhead']
            factory=Mock(side_effect=RuntimeError('stop after constructor'))
            with redirect_stderr(StringIO()):cli.main(args,preflight=preflight,session_type=factory)
            self.assertEqual(factory.call_args.kwargs['readonly'],command in ('status','profile','at'))
            self.assertEqual(factory.call_args.kwargs['learn_on_open'],command in ('plan','apply','adjust','learn'))

    def test_cli_undo_can_save_result_without_learning_on_open(self):
        import importlib.util
        from contextlib import redirect_stdout
        from copy import deepcopy
        from engine.broll.live.service import Session
        s=Session(self.io,self.cfg,self.root/'film')
        restored=deepcopy(s.ledger);restored['runs'].append({'id':'undo-test','kind':'undo'})
        spec=importlib.util.spec_from_file_location('t6_undo_cli',Path(__file__).absolute().parents[3]/'bin/broll.py')
        cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        config=self.root/'config.json';config.write_text(json.dumps(self.cfg))
        with redirect_stdout(StringIO()),patch.object(cli,'undo',return_value={'applied':True,'ledger':restored}),patch.object(Session,'auto_learn',side_effect=AssertionError('undo must not learn')):
            code=cli.main(['undo','--apply','--config',str(config),'--project-dir',str(self.root/'film')],preflight=lambda *_:(self.io,{'数据库':'Disk'}))
        self.assertEqual(code,0);self.assertEqual(media.read_json(s.path),restored)
