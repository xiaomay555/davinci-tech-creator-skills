"""Normalize one requested source-audio range. Never generate default audio tracks."""
from fractions import Fraction
from pathlib import Path
import hashlib
import json
import os
import tempfile
import wave
from engine.broll import media, index
from .candidates import frame_rate


def render_audio(candidate, directory, spot, source_start, frames, timeline_fps=29.97):
    fps=frame_rate(timeline_fps)
    path=Path(candidate['file'])
    info=media.probe(path,'video')
    if not info.get('has_audio'):
        raise ValueError('这处素材无原声')
    if source_start<0 or frames<=0 or float(Fraction(source_start+frames)/fps)>info['duration']+1e-7:
        raise ValueError('原声范围超出源片，不补静音拉长')
    st=path.stat()
    for key,actual in (('size',st.st_size),('mtime_ns',st.st_mtime_ns)):
        if candidate.get(key) not in (None,actual):raise ValueError('原声源片身份已改变')
    samples=round(Fraction(frames)*48000/fps)
    meta={'source_path':str(path),'size':st.st_size,'mtime_ns':st.st_mtime_ns,'source_start':source_start,'frames':frames,'samples':samples,'rate':48000,'channels':2}
    directory=media.safe_directory(directory);directory.mkdir(parents=True,exist_ok=True)
    name=hashlib.sha256(json.dumps(meta,sort_keys=True).encode()).hexdigest()[:8]
    output=directory/f'{spot}_audio_{name}.wav'
    def valid(file):
        try:
            with wave.open(str(file),'rb') as w:return (w.getnframes(),w.getframerate(),w.getnchannels(),w.getsampwidth())==(samples,48000,2,2)
        except (OSError,wave.Error):return False
    with index.exclusive_lock(directory/'.audio.lock'):
        if output.exists() and valid(output) and media.read_json(output.with_suffix('.json'))==meta:
            return {'path':str(output),'startFrame':0,'frames':frames,'metadata':meta}
        fd,tmp=tempfile.mkstemp(prefix='.audio-',suffix='.wav',dir=directory);os.close(fd)
        try:
            media.command(['ffmpeg','-v','error','-nostdin','-y','-ss',str(float(Fraction(source_start)/fps)),'-i',str(path),'-map','0:a:0','-vn','-af',f'aresample=48000,atrim=end_sample={samples},asetpts=PTS-STARTPTS','-ac','2','-c:a','pcm_s16le',tmp])
            if not valid(tmp):raise RuntimeError('原声样本数读回不符，禁止补长')
            if (path.stat().st_size,path.stat().st_mtime_ns)!=(st.st_size,st.st_mtime_ns):raise RuntimeError('原声源片在生成中改变')
            os.replace(tmp,output);index.atomic_json(output.with_suffix('.json'),meta)
        finally:Path(tmp).unlink(missing_ok=True)
    return {'path':str(output),'startFrame':0,'frames':frames,'metadata':meta}
