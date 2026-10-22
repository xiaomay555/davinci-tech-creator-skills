import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).absolute().parents[3]))
import unittest
from tempfile import TemporaryDirectory
from fractions import Fraction
import wave
from engine.broll import media
from engine.broll.live.audio_clip import render_audio

class AudioTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.source=self.root/'source.mov'
        media.command(['ffmpeg','-v','error','-nostdin','-y','-f','lavfi','-i','color=size=32x32:rate=30000/1001:duration=4','-f','lavfi','-i','sine=frequency=500:duration=4','-c:v','ffv1','-c:a','pcm_s16le',str(self.source)])
        self.c={'file':str(self.source)}
    def test_exact_48000_stereo_samples(self):
        out=render_audio(self.c,self.root/'clips','S01',30,60)
        with wave.open(out['path']) as w:self.assertEqual((w.getnframes(),w.getframerate(),w.getnchannels()),(96096,48000,2))
    def test_cached_same_identity_reuses_file(self):
        a=render_audio(self.c,self.root/'clips','S01',0,60);mtime=Path(a['path']).stat().st_mtime_ns
        b=render_audio(self.c,self.root/'clips','S01',0,60);self.assertEqual(a,b);self.assertEqual(Path(a['path']).stat().st_mtime_ns,mtime)
    def test_insufficient_range_refuses_no_padding(self):
        with self.assertRaisesRegex(ValueError,'超出'):render_audio(self.c,self.root/'clips','S01',110,60)
    def test_changed_identity_refuses(self):
        self.c['size']=0
        with self.assertRaisesRegex(ValueError,'身份'):render_audio(self.c,self.root/'clips','S01',0,60)
    def test_no_audio_refuses(self):
        silent=self.root/'silent.mov';media.command(['ffmpeg','-v','error','-nostdin','-y','-i',str(self.source),'-an','-c:v','copy',str(silent)])
        with self.assertRaisesRegex(ValueError,'无原声'):render_audio({'file':str(silent)},self.root/'clips','S01',0,60)
    def test_real_index_description_plan_and_direct_sdk_write_pipeline(self):
        from unittest.mock import patch
        from engine.broll.live._fake_resolve import Resolve, Media
        from engine.broll.live.resolve_adapter import Adapter
        from engine.broll.live.service import Session
        resolve=Resolve();io=Adapter(resolve,resolve.project,resolve.project.current)
        io.p.pool.root.clips.append(Media(str(self.source),Frames='120'))
        cfg={'creator':'Creator','ledger_index':str(self.root/'registry.json'),'learning_root':str(self.root/'learning')}
        with patch('engine.fx.resolve_io.time.sleep'):
            s=Session(io,cfg,self.root);draft=s.plan();self.assertGreater(draft['待描述'],0)
            value=media.read_json(s.work/'索引/index.json');segments=next(iter(value['files'].values()))['segments']
            desc=[dict(segment=seg['id'],description='离线合成颜色测试片',tags=['颜色','合成','测试'],visible_devices=[],scene='其他',quality={'blur':False,'shake':False,'exposure_bad':False},usable=[True,True,True],focus='sharp',subject='centered',people='none',use='texture',sample_of=None) for seg in segments]
            s.plan(descriptions=desc)
            ident=next(iter(s.catalog));matches={'version':3,'segments':[{'spot':'S01','lines':[2,2],'shots':[{'end':200,'candidates':[{'segment':ident,'reason':'仅为离线串联测试'}]}]}],'face':[]}
            out=s.apply(matches,True);self.assertTrue(out['applied']);self.assertEqual(s.ledger['spots']['S01']['placement']['media_path'],str(self.source))
