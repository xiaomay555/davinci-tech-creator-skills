"""19E: still-duration fallback, using disposable media and the fake SDK only."""
import json
import sys
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from engine.broll.live._fake_resolve import Resolve, Media, Timeline
from engine.broll.live._fixtures import snapshot
from engine.broll.live.resolve_adapter import Adapter, PhotoDurationMismatch
from engine.broll.live.service import Session
from engine.broll.live.writer import execute, snapshot_hash, _discard_short_photo
from engine.broll.live.ledger import adopt
from engine.broll.live.timeline import build_model


class PhotoFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.r=Resolve();self.p=self.r.project
        self.p.current=Timeline(self.p,value=snapshot(length=1400));self.p.timelines=[self.p.current]
        self.io=Adapter(self.r,self.p,self.p.current)
        sleep=patch('engine.fx.resolve_io.time.sleep');sleep.start();self.addCleanup(sleep.stop)
        self.source=self.root/'原照片.png';self.source.write_bytes(b'fixture')
        self.photo=Media(str(self.source),'Still');self.p.pool.root.clips.append(self.photo)
        self.before=self.io.snapshot();self.book=adopt(build_model(self.before),'test','adopt',self.p.GetName(),self.io.db)
        self.path=self.root/'ledger.json'

    def plan(self, length=144):
        candidate=dict(file=str(self.source),kind='photo',segment='photo',in_media_pool=True)
        def photo(spot,start):
            return dict(spot=spot,new=dict(type='video',path=str(self.source),media_id=self.photo.GetUniqueId(),
                generated=False,start=start,end=start+length,offset=0,track=2,name=None,
                source_file=str(self.source),source_kind='photo',source_start=0,
                photo_fallback=dict(candidate=candidate,out_dir=str(self.root/'B-roll片段'),folder_rules=[],lut_dirs=[])))
        video=self.root/'原视频.mov';video.write_bytes(b'fixture')
        return dict(run='fallback',basis_hash=snapshot_hash(self.before),operations=[photo('P1',0),photo('P2',length),
            dict(spot='V1',new=dict(type='video',path=str(video),start=length*2,end=length*2+100,
                offset=50,track=2,name=None,source_file=str(video),source_start=50))])

    def write(self, plan):
        return execute(self.io,plan,self.book,self.path,apply=True,lock_path=self.root/'lock')

    def render(self,candidate,out_dir,spot,source_start,frames,**kwargs):
        path=Path(out_dir)/(Path(candidate['file']).name+'__'+spot+'.mov')
        path.parent.mkdir(exist_ok=True);path.write_bytes(b'fixture')
        return dict(path=str(path),startFrame=59)

    def test_short_stills_fallback_ledger_and_other_clips_unchanged(self):
        for actual,requested in ((30,144),(150,620)):
            with self.subTest(actual=actual,requested=requested):
                self.p.pool.photo_frames=actual
                plan=self.plan(requested)
                with patch('engine.broll.media.render_live_clip',side_effect=self.render) as render, patch.object(self.io,'delete',wraps=self.io.delete) as delete:
                    out=self.write(plan)
                self.assertTrue(out['applied']);self.assertEqual(render.call_count,2)
                self.assertEqual(delete.call_count,1)
                deleted=delete.call_args[0][0]
                self.assertEqual(len(deleted),1);self.assertEqual(deleted[0].GetMediaPoolItem().GetUniqueId(),self.photo.GetUniqueId())
                self.assertEqual(deleted[0].GetDuration(),actual)
                reason=f'接口放置静态图片长度不符：请求 {requested} 帧，实际 {actual} 帧'
                book=json.loads(self.path.read_text())
                self.assertEqual(book['spots']['P1']['derivative_reason'],reason)
                self.assertIn('同次运行',book['spots']['P2']['derivative_reason'])
                self.assertIn(reason,book['spots']['P2']['derivative_reason'])
                for spot in ('P1','P2'):
                    row=book['spots'][spot]
                    self.assertEqual(row['placement']['duration'],requested)
                    self.assertIn(self.source.name,Path(row['placement']['media_path']).name)
                    self.assertEqual(row['source_file'],str(self.source))
                self.assertEqual([r['attempted_direct'] for r in out['photo_fallbacks']],[True,False])
                self.assertEqual(book['runs'][-1]['photo_fallbacks'],out['photo_fallbacks'])
                v=book['spots']['V1'];self.assertIsNone(v['derivative_reason'])
                self.assertEqual(v['placement']['media_path'],str(self.root/'原视频.mov'))
                self.assertEqual(v['placement']['source_start'],50)
                after={r['id']:r for r in self.io.snapshot()['timeline']['items']}
                for row in self.before['timeline']['items']:self.assertEqual(row,after[row['id']])
                self.assertEqual(self.p.pool.append_count,4)
                # Reset the disposable fake before the other duration scenario.
                self.p.current.items=[i for i in self.p.current.items if i.GetUniqueId() in {r['id'] for r in self.before['timeline']['items']}]
                self.p.pool.append_count=0

    def test_matching_still_length_keeps_original_and_no_render(self):
        self.p.pool.photo_frames=144
        with patch('engine.broll.media.render_live_clip') as render:
            out=self.write(self.plan())
        render.assert_not_called();self.assertEqual(out['photo_fallbacks'],[])
        self.assertEqual(out['ledger']['spots']['P1']['placement']['media_path'],str(self.source))

    def test_cleanup_refuses_old_identity_extra_item_or_changed_other_clip(self):
        for defect in ('old_id','extra','changed'):
            with self.subTest(defect=defect):
                before=self.io.snapshot();self.p.pool.photo_frames=30
                with self.assertRaises(PhotoDurationMismatch) as error:
                    self.io.append_clip(self.photo,'video',2,0,0,144)
                placement=dict(path=str(self.source),start=0)
                if defect=='old_id':error.exception.item_id=before['timeline']['items'][0]['id']
                if defect=='extra':self.p.pool.AppendToTimeline([dict(mediaPoolItem=self.photo,mediaType=1,recordFrame=400,trackIndex=2,startFrame=0,endFrame=144)])
                if defect=='changed':self.p.current.items[0].SetClipColor('Red')
                with patch.object(self.io,'delete',wraps=self.io.delete) as delete:
                    with self.assertRaises(RuntimeError):_discard_short_photo(self.io,before,error.exception,self.photo,placement,2)
                delete.assert_not_called()
                self.p.current.items=[i for i in self.p.current.items if i.GetUniqueId() in {r['id'] for r in self.before['timeline']['items']}]
                self.p.current.items[0].SetClipColor('')

    def test_failed_derivative_restores_timeline_without_leaking_short_still(self):
        self.p.pool.photo_frames=30
        with patch('engine.broll.media.render_live_clip',side_effect=RuntimeError('编码失败')):
            with self.assertRaisesRegex(RuntimeError,'时间线已恢复原样'):self.write(self.plan())
        self.assertEqual(snapshot_hash(self.before),snapshot_hash(self.io.snapshot()))

    def test_session_prepares_fallback_and_includes_it_in_report(self):
        config=dict(creator='test',ledger_index=str(self.root/'index.json'),learning_root=str(self.root/'learn'))
        session=Session(self.io,config,self.root/'film')
        session.catalog={'photo':dict(segment='photo',parent_segment='photo',file=str(self.source),kind='photo',
            in_media_pool=True,media_id=self.photo.GetUniqueId(),usable_start=0,usable_end=1000,
            focus='sharp',subject='centered',people='none',use='scene',probe={})}
        matches=dict(version=3,segments=[dict(spot='P1',lines=[2,2],shots=[dict(end=200,candidates=[dict(segment='photo',reason='图片对应口播')])])],face=[])
        self.p.pool.photo_frames=30
        with patch('engine.broll.media.render_live_clip',side_effect=self.render):out=session.apply(matches,True)
        self.assertIn('接口放置静态图片长度不符：请求 100 帧，实际 30 帧',out['report'])
        self.assertEqual(len(out['photo_fallbacks']),1)
        self.assertTrue(Path(out['photo_fallbacks'][0]['path']).is_relative_to(session.work/'B-roll片段'))

    def test_real_photo_derivative_encoded_and_placed(self):
        from engine.broll import media
        media.command(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=blue:s=64x48',
                       '-frames:v','1',str(self.source)])
        self.io.main.settings.update(timelineResolutionWidth='64',timelineResolutionHeight='48')
        self.before=self.io.snapshot()
        self.p.pool.photo_frames=30
        out=self.write(self.plan())
        for spot in ('P1','P2'):
            state=out['ledger']['spots'][spot]['placement']
            info=media.probe(state['media_path'])
            self.assertEqual((info['width'],info['height']),(64,48))
            sidecar=json.loads(Path(state['media_path']).with_suffix('.json').read_text())
            self.assertEqual(sidecar['frames'],144)
            self.assertEqual(sidecar['source_path'],str(self.source))
            self.assertEqual(state['source_start'],sidecar['before_frames'])
            self.assertEqual(round(info['duration']*30000/1001),sidecar['total_frames'])

    def test_wrong_photo_position_is_not_retried(self):
        append=self.p.pool.AppendToTimeline
        def misplaced(rows):
            items=append(rows)
            items[0].row['start']+=1;items[0].row['end']+=1
            return items
        with patch.object(self.p.pool,'AppendToTimeline',side_effect=misplaced), patch('engine.broll.media.render_live_clip') as render:
            with self.assertRaises(RuntimeError) as error:
                self.io.append_clip(self.photo,'video',2,0,0,144)
        self.assertNotIsInstance(error.exception,PhotoDurationMismatch)
        render.assert_not_called()

    def test_skill_documents_guarded_fallback(self):
        text=(Path(__file__).resolve().parents[3]/'skills/b-roll/references/写入与核验.md').read_text()
        for term in ('长度不符','本次新增','其他片段零变化','derivative_reason','后续照片'):
            self.assertIn(term,text)


if __name__=='__main__':unittest.main()
