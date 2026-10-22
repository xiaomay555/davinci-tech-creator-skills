import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).absolute().parents[3]))
import unittest
from copy import deepcopy
from tempfile import TemporaryDirectory
from unittest.mock import patch
from engine.broll.live._fake_resolve import Resolve, Timeline, Item, Media
from engine.broll.live.resolve_adapter import Adapter, read_snapshot, media_pool, timecode_frame
from engine.broll.live.timeline import build_model
from engine.broll.live.ledger import adopt, exact_state
from engine.broll.live.writer import execute, undo, snapshot_hash
from engine.broll.live.workspace import write_lock, infer_work_dir, register
from engine.broll.live.review_live import identify, review_ranges

class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.r=Resolve();self.p=self.r.project;self.t=self.p.current
        self.io=Adapter(self.r,self.p,self.t)
        self.sleep=patch('engine.fx.resolve_io.time.sleep');self.sleep.start();self.addCleanup(self.sleep.stop)
        self.p.pool.find('/media/product.mov')
        self.snap=self.io.snapshot();self.book=adopt(build_model(self.snap),'Creator','adopt',self.p.GetName(),self.io.db)
        self.path=self.root/'ledger.json';self.lock=self.root/'write.lock'
    def plan(self,starts=(100,),old=None):
        return {'run':'1012a','basis_hash':snapshot_hash(self.io.snapshot()),'operations':[dict(spot='B'+str(n),old=old or [],text='句子',new=dict(type='video',path='/media/product.mov',start=s,end=s+80,offset=50+n*100,track=2,name=f'[B-1012a-{n:02}] 句子 · product.mov',props={'ZoomGang':False,'ZoomX':1.2,'ZoomY':1.2})) for n,s in enumerate(starts)]}
    def write(self,plan=None,**kw):return execute(self.io,plan or self.plan(),self.book,self.path,apply=True,lock_path=self.lock,**kw)
    def test_snapshot_restores_line_folder_and_both_heads(self):
        other=Timeline(self.p,'other');self.p.timelines.append(other);self.p.current=other;other.tc='00:00:03:03';self.t.tc='00:00:02:02'
        f=self.p.pool.AddSubFolder(self.p.pool.root,'chosen');self.p.pool.current=f
        read_snapshot(self.r,self.p,self.t)
        self.assertIs(self.p.current,other);self.assertIs(self.p.pool.current,f);self.assertEqual(other.tc,'00:00:03:03');self.assertEqual(self.t.tc,'00:00:02:02')
    def test_snapshot_restores_on_drt_failure(self):
        other=Timeline(self.p,'other');self.p.timelines.append(other);self.p.current=other
        next(i for i in self.t.items if i.row['type']=='subtitle').SetName('Subtitle');self.t.export_fail=True
        with self.assertRaises(RuntimeError):read_snapshot(self.r,self.p,self.t)
        self.assertIs(self.p.current,other)
    def test_generic_subtitles_use_exact_drt(self):
        for i in self.t.items:
            if i.row['type']=='subtitle':i.SetName('Subtitle')
        self.assertEqual(build_model(self.io.snapshot())['lines'][0]['text'],'真实字幕')
    def test_photo_offsets_null(self):
        row=deepcopy(self.t.items[0].row);row.update(id='photo',name='照片',track=2)
        self.t.items.append(Item(row,Media('/media/photo.jpg','Still')))
        photo=next(r for r in self.io.snapshot()['timeline']['items'] if r['id']=='photo')
        self.assertIsNone(photo['broll_extra']['left_offset']);self.assertIsNone(photo['broll_extra']['right_offset'])
    def test_pool_properties_bin_and_lut(self):
        f=self.p.pool.AddSubFolder(self.p.pool.root,'产品');m=Media('/product.mov',**{'Input LUT':'Sony.cube','Frames':'300','FPS':'30000/1001'});f.clips.append(m)
        row=next(r for r in media_pool(self.p) if r['file_path']=='/product.mov')
        self.assertAlmostEqual(row['duration'],10.01);self.assertEqual(row['bin'],'Root/产品');self.assertEqual(row['input_lut'],'Sony.cube')
    def test_success_full_flow_preserves_user_and_saves(self):
        out=self.write();self.assertTrue(out['applied']);self.assertEqual(self.r.manager.loaded,1);self.assertTrue(self.path.exists())
        row=out['ledger']['spots']['B0']['placement'];self.assertEqual(row['broll_extra']['color'],'Teal');self.assertFalse(row['props']['ZoomGang'])
        self.assertEqual(len(self.io.snapshot()['timeline']['items']),len(self.snap['timeline']['items'])+1)
        self.assertEqual(self.t.GetMarkers(),{})
    def test_dry_run_does_not_append_save_or_create_tracks(self):
        out=execute(self.io,self.plan(),self.book,self.path,lock_path=self.lock)
        self.assertFalse(out['applied']);self.assertEqual(self.p.pool.append_count,0);self.assertEqual(self.r.manager.saved,0)
    def test_second_place_failure_rolls_back_first(self):
        self.p.pool.fail_at=2
        with self.assertRaisesRegex(RuntimeError,'时间线已恢复原样'):self.write(self.plan((100,300)))
        self.assertEqual(snapshot_hash(self.io.snapshot()),snapshot_hash(self.snap))
    def test_bad_append_return_after_insertion_is_compensated(self):
        self.p.pool.bad_at=1
        with self.assertRaisesRegex(RuntimeError,'时间线已恢复原样'):self.write()
        self.assertEqual(snapshot_hash(self.io.snapshot()),snapshot_hash(self.snap))
    def test_hash_mismatch_stops_before_mutation(self):
        p=self.plan();self.t.items[0].SetClipColor('Red')
        with self.assertRaisesRegex(ValueError,'哈希'):self.write(p)
        self.assertEqual(self.p.pool.append_count,0)
    def test_per_spot_concurrent_change_stops_and_does_not_erase_user(self):
        p=self.plan((100,300));orig=self.io.snapshot;calls=0
        def snap():
            nonlocal calls
            calls+=1
            if calls==7:self.t.items[0].SetClipColor('Red')
            return orig()
        with patch.object(self.io,'snapshot',side_effect=snap),self.assertRaises(RuntimeError):self.write(p)
        self.assertEqual(self.t.items[0].GetClipColor(),'Red')
    def test_postgres_default_reject_and_explicit_optin(self):
        self.r.manager.db['DbType']='PostgreSQL';self.io=Adapter(self.r,self.p,self.t)
        with self.assertRaisesRegex(ValueError,'默认拒绝'):self.write()
        self.assertEqual(self.p.pool.append_count,0)
        self.assertTrue(self.write(config={'allow_postgres_apply':True})['applied'])
    def test_save_reopen_rejects_wrong_project_id(self):
        self.r.manager.wrong_id=True
        with self.assertRaisesRegex(RuntimeError,'工程'):self.io.save_reopen()
    def test_save_reopen_stops_on_folder_change(self):
        self.r.manager.change_folder=True
        with self.assertRaisesRegex(RuntimeError,'文件夹'):self.io.save_reopen()
        self.assertEqual(self.r.manager.loaded,0)
    def test_delete_only_exact_equal_name_color_zoom_offset_enabled_volume(self):
        out=self.write();book=out['ledger'];state=book['spots']['B0']['placement'];item=next(i for i in self.t.items if i.GetUniqueId()==state['id'])
        original=deepcopy(item.row)
        for change in (lambda i:i.SetName('changed'),lambda i:i.SetClipColor('Blue'),lambda i:i.SetProperty('ZoomX',1.200001),lambda i:i.SetClipEnabled(False),lambda i:i.SetProperty('AudioVolume',-5),lambda i:i.row.update(source_start=51)):
            item.row=deepcopy(original);change(item)
            with self.assertRaisesRegex(ValueError,'手动修改'):undo(self.io,book,self.path,apply=True,lock_path=self.lock)
        item.row=original
        self.assertTrue(undo(self.io,book,self.path,apply=True,lock_path=self.lock)['applied'])
    def test_replace_failure_restores_old_exact_parameters(self):
        out=self.write();self.book=out['ledger'];old=self.book['spots']['B0']['placement'];self.p.pool.fail_at=2
        with self.assertRaisesRegex(RuntimeError,'恢复原样'):self.write(self.plan(old=old))
        self.assertEqual([exact_state(r) for r in self.io.snapshot()['timeline']['items']],[exact_state(r) for r in out['ledger']['runs'][-1]['after']['timeline']['items']])
        self.assertEqual(self.p.GetTimelineCount(),1)

    def test_rollback_compensation_failure_still_cleans_copies(self):
        from engine.broll.live import writer
        first=self.write();self.book=first['ledger'];old=self.book['spots']['B0']['placement']
        self.p.pool.fail_at=2
        with patch.object(writer,'_restore',side_effect=RuntimeError('补偿失败')):
            with self.assertRaisesRegex(RuntimeError,'留下的新建时间线：无'):
                self.write(self.plan(old=old))
        self.assertEqual(self.p.GetTimelineCount(),1)

    def test_rollback_copies_are_gone_before_verification(self):
        from engine.broll.live import writer
        first=self.write();self.book=first['ledger'];old=self.book['spots']['B0']['placement']
        check=writer._check
        def verify(*args,**kwargs):
            if len(args)==2 and not kwargs:
                self.assertEqual(self.p.GetTimelineCount(),1)
            return check(*args,**kwargs)
        self.p.pool.fail_at=2
        with patch.object(writer,'_check',side_effect=verify),self.assertRaisesRegex(RuntimeError,'恢复原样'):
            self.write(self.plan(old=old))

    def test_rollback_cleanup_failure_reports_copy_name(self):
        first=self.write();self.book=first['ledger'];old=self.book['spots']['B0']['placement']
        self.p.pool.fail_at=2
        with patch.object(self.io,'delete_timelines',side_effect=RuntimeError('删除副本失败')):
            with self.assertRaisesRegex(RuntimeError,'留下的新建时间线：B-roll历史_main_1012a'):
                self.write(self.plan(old=old))
    def test_undo_restores_replaced_clip(self):
        first=self.write();self.book=first['ledger'];old=self.book['spots']['B0']['placement']
        plan=self.plan(old=old);plan['run']='1012b';plan['operations'][0]['new']['name']='[B-1012b-00] 新画面';plan['operations'][0]['new']['offset']=150
        second=self.write(plan);undone=undo(self.io,second['ledger'],self.path,apply=True,lock_path=self.lock)
        self.assertEqual(exact_state(undone['ledger']['spots']['B0']['placement']),exact_state(old))
    def test_new_audio_track_gain_link_and_rollback(self):
        self.book=self.write()['ledger'];v=self.book['spots']['B0']['placement']
        p={'run':'audio','basis_hash':snapshot_hash(self.io.snapshot()),'operations':[{'spot':'B0','new':dict(type='audio',path='/media/product.mov',track=0,start=v['start'],end=v['end'],offset=50,name='[B-audio-01]',props={'AudioVolume':-5})}]}
        out=self.write(p);a=out['ledger']['spots']['B0']['audio'];self.assertEqual(a['props']['AudioVolume'],-5);self.assertEqual(a['track_name'],'B-roll 原声')
        video=next(i for i in self.t.items if i.GetUniqueId()==v['id']);self.assertEqual(len(video.GetLinkedItems()),1)
    def test_zero_flash_readback_blocks_two_frame_gap(self):
        with self.assertRaisesRegex(RuntimeError,'闪缝'):self.write(self.plan((100,182)))
        self.assertEqual(len(self.t.items),len(self.snap['timeline']['items']))
    def test_local_review_maps_to_main_and_main_markers_unchanged(self):
        out=self.write(local=True);book=out['ledger'];name=next(iter(book['reviews']))
        mapping=book['reviews'][name]['ranges'][0];tc='00:00:01:10' # frame40 + main range start70 =>110
        rows=identify(book,name,tc,29.97);self.assertEqual(rows[0]['spot'],'B0');self.assertEqual(rows[0]['frame'],110)
        self.assertEqual(self.t.GetMarkers(),{})
    def test_first_full_run_has_no_review(self):
        self.write();self.assertEqual(self.p.GetTimelineCount(),1)
    def test_at_limits_ambiguous_candidates(self):
        b=deepcopy(self.book);b['spots']={str(i):{'placement':{'start':100,'end':200}} for i in range(5)}
        self.assertEqual(len(identify(b,'main','00:00:04:00',29.97)),3)
    def test_dropframe_timecodes(self):
        self.assertEqual(timecode_frame('00:01:00;02',29.97),1800)
        with self.assertRaises(ValueError):timecode_frame('00:01:00;00',29.97)
    def test_range_merge_and_mapping_gap(self):
        rows=review_ranges({'a':{'start':100,'end':180},'b':{'start':200,'end':280}},0,600)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['spots'],['a','b'])
    def test_local_lock_refuses_parallel_and_keeps_inode(self):
        with write_lock(self.lock):
            with self.assertRaisesRegex(RuntimeError,'另一处'):
                with write_lock(self.lock):pass
        self.assertTrue(self.lock.exists())
    def test_directory_inference_and_registry(self):
        model=build_model(self.snap);self.assertEqual(infer_work_dir(model),Path('/media/B-roll_AI'))
        with self.assertRaises(ValueError):infer_work_dir({'items':[]})
        self.assertTrue(register(self.root/'index.json',self.book,self.root/'work'))
    def test_disabled_picture_track_is_rejected_before_append(self):
        self.t.value['tracks'][1]['enabled']=False
        with self.assertRaisesRegex(RuntimeError,'停用'):self.write()
        self.assertEqual(self.p.pool.append_count,0)
    def test_local_review_failure_removes_partial_copy_and_compensates_main(self):
        # First append is main replacement, second is review nest video.
        self.p.pool.fail_at=2
        with self.assertRaisesRegex(RuntimeError,'恢复原样'):self.write(local=True)
        self.assertEqual(self.p.GetTimelineCount(),1);self.assertEqual(snapshot_hash(self.io.snapshot()),snapshot_hash(self.snap))
    def test_undo_removes_created_audio_track(self):
        self.book=self.write()['ledger'];v=self.book['spots']['B0']['placement']
        p={'run':'audio','basis_hash':snapshot_hash(self.io.snapshot()),'operations':[{'spot':'B0','new':dict(type='audio',path='/media/product.mov',track=0,start=v['start'],end=v['end'],offset=50,name='[B-audio-01]',props={'AudioVolume':-5})}]}
        out=self.write(p);undo(self.io,out['ledger'],self.path,apply=True,lock_path=self.lock)
        self.assertEqual(self.t.GetTrackCount('audio'),1)
    def test_same_file_multiple_clips_identity_uses_unique_prefix(self):
        from engine.broll.live.ledger import match_identity
        a=deepcopy(self.snap['timeline']['items'][0]);a.update(name='[B-run-01]',track=2)
        b=deepcopy(a);b.update(name='[B-run-02]',source_start=500)
        self.assertEqual(match_identity(a,[a,b])['status'],'matched')
        self.assertEqual(match_identity(a,[a,deepcopy(a)])['status'],'uncertain')
    def test_undo_explicit_independent_run_keeps_later_spot(self):
        first=self.write();self.book=first['ledger'];p=self.plan((300,));p['run']='later';p['operations'][0]['spot']='other';p['operations'][0]['new']['name']='[B-later-01]'
        second=self.write(p);out=undo(self.io,second['ledger'],self.path,run='1012a',apply=True,lock_path=self.lock)
        self.assertNotIn('B0',out['ledger']['spots']);self.assertIn('other',out['ledger']['spots'])
