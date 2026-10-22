"""Local B-roll discovery and frame preparation."""
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path

VIDEO = {'.mp4', '.mov', '.m4v', '.mxf'}
PHOTO = {'.jpg', '.jpeg', '.png', '.heic', '.heif'}


@contextmanager
def photo_source(path, kind):
    """Let ffmpeg assemble HEIF grids before probing/scaling the full image."""
    if kind != 'photo' or Path(path).suffix.lower() not in {'.heic', '.heif'}:
        yield path
        return
    with tempfile.TemporaryDirectory(prefix='broll-photo-') as directory:
        decoded = Path(directory)/'full.png'
        command(['ffmpeg','-v','error','-nostdin','-y','-protocol_whitelist','file',
                 '-i',str(path),'-frames:v','1',str(decoded)])
        if not decoded.is_file():raise RuntimeError('HEIC/HEIF完整图解码失败：'+str(path))
        yield decoded


def in_shared_directory(path):
    """Recognise aliases and mounted paths by filesystem identity."""
    root = Path(__file__).resolve().parents[2].stat()
    path = Path(path).resolve()
    for parent in (path, *path.parents):
        try: stat = parent.stat()
        except OSError: continue
        if (stat.st_dev, stat.st_ino) == (root.st_dev, root.st_ino):return True
    return False


def safe_directory(path):
    path = Path(path)
    if path.is_symlink():raise ValueError('目录不能是符号链接：'+str(path))
    if in_shared_directory(path):raise ValueError('B-roll 素材或片段不能放入共享能力目录')
    return path


def read_json(path):
    try:return json.loads(Path(path).read_text())
    except OSError:raise ValueError('无法读取 JSON 文件：'+str(path)) from None
    except (json.JSONDecodeError, UnicodeError):raise ValueError('JSON 语法错误：'+str(path)) from None


def field_error(row, fields):
    actual = set(row) if isinstance(row,dict) else set()
    return '字段不符；缺少：'+('、'.join(sorted(fields-actual)) or '无')+'；多出：'+('、'.join(sorted(actual-fields)) or '无')


def annotations(relative):
    shot_by, tags = None, []
    for name in Path(relative).parts[:-1]:
        if name.endswith('拍摄') and name[:-2]:shot_by = name[:-2]
        else:tags.append(name)
    return shot_by, tags


def scan(directory):
    files = {}
    for base, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(base)/d).is_symlink())
        for name in sorted(names):
            path = Path(base)/name
            if name.startswith('.') or not path.is_file():continue
            suffix = path.suffix.lower()
            if suffix not in VIDEO | PHOTO:continue
            rel = path.relative_to(directory).as_posix()
            stat = path.stat()
            shot_by, tags = annotations(rel)
            files[rel] = {'size':stat.st_size, 'mtime_ns':stat.st_mtime_ns,
                          'kind':'video' if suffix in VIDEO else 'photo', 'shot_by':shot_by, 'folder_tags':tags}
    return dict(sorted(files.items()))


def command(args):
    try:result = subprocess.run(args, capture_output=True, text=True, check=True)
    except FileNotFoundError:raise RuntimeError('找不到 ffmpeg/ffprobe，请先安装') from None
    except subprocess.CalledProcessError:raise RuntimeError('素材处理失败，请检查文件是否完整：'+str(args[args.index('-i')+1]) if '-i' in args else '素材探测失败') from None
    return result


def probe(path, kind='video'):
    data = json.loads(command(['ffprobe','-v','error','-show_streams','-show_format','-of','json','-protocol_whitelist','file','-i',str(path)]).stdout)
    if not data.get('streams'):raise RuntimeError('素材没有可读取的画面：'+str(path))
    stream = next((s for s in data['streams'] if s.get('codec_type','video')=='video'),None)
    if stream is None:raise RuntimeError('素材没有可读取的画面：'+str(path))
    fps = Fraction(stream.get('avg_frame_rate','0/1')) if stream.get('avg_frame_rate') != '0/0' else Fraction(0)
    duration = float(stream.get('duration') or data.get('format',{}).get('duration') or 0) if kind == 'video' else 0.0
    vfr = kind == 'video' and Fraction(stream.get('r_frame_rate') or '0/1') != fps
    if kind == 'video' and duration <= 0:raise RuntimeError('素材时长无效：'+str(path))
    return {'width':stream['width'],'height':stream['height'],'fps_num':fps.numerator,
            'fps_den':fps.denominator,'duration':duration,'vfr':vfr,'color_transfer':stream.get('color_transfer'),
            'has_audio':kind=='video' and any(s.get('codec_type')=='audio' for s in data['streams']),
            'hdr':stream.get('color_transfer') in ('arib-std-b67','smpte2084')}


TONEMAP = 'zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=mobius:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p'


def hdr_filter(info):
    if not info['hdr']:return ''
    if not re.search(r'\bzscale\b',command(['ffmpeg','-hide_banner','-filters']).stdout):
        raise RuntimeError('这台 Mac 的 ffmpeg 不支持把 HDR 素材转成普通画面（缺 zscale），请运行 bin/setup_mac.sh 或换带 libzimg 的 ffmpeg')
    return TONEMAP+','


def scene_cuts(path, threshold=0.35):
    args = ['ffmpeg','-hide_banner','-nostdin','-hwaccel','videotoolbox','-protocol_whitelist','file','-i',str(path),'-an',
            '-vf',f"scale=320:-2,select='gt(scene,{threshold})',showinfo",'-f','null','-']
    try:result = command(args)
    except RuntimeError:result = command(args[:3]+args[5:])
    return sorted(set(float(x) for x in re.findall(r'pts_time:([\d.]+)',result.stderr)))


def windows(duration, cuts, rules):
    bounds = [0.0]+sorted(set(t for t in cuts if 0<t<duration))+[duration]
    shots = []
    for a,b in zip(bounds,bounds[1:]):
        if b-a < rules['min_shot_seconds'] and shots:shots[-1][1] = b
        else:shots.append([a,b])
    # A short first shot has no predecessor; merge it into the next shot.
    if len(shots)>1 and shots[0][1]-shots[0][0]<rules['min_shot_seconds']:
        first = shots.pop(0)
        shots[0][0] = first[0]
    result = []
    for a,b in shots:
        count = math.ceil((b-a)/rules['window_max_seconds'])
        for i in range(count):result.append((a+(b-a)*i/count,a+(b-a)*(i+1)/count))
    return result


def segments(relative, info, kind, rules, cuts=(), size=None, mtime_ns=None, *, max_segments=None):
    spans = [(0.0,rules['photo_max_seconds'])] if kind == 'photo' else windows(info['duration'],cuts,rules)
    if max_segments is not None and len(spans)>max_segments:
        # Spread samples by source time, even when scene lengths differ.
        remaining=list(spans);chosen=[]
        for n in range(max_segments):
            target=info['duration']*(n/(max_segments-1) if max_segments>1 else .5)
            span=min(remaining,key=lambda s:abs((s[0]+s[1])/2-target))
            chosen.append(span);remaining.remove(span)
        spans=sorted(chosen)
    return [{'id':hashlib.sha1(json.dumps([relative,a,size,mtime_ns]).encode()).hexdigest()[:12],'start':a,'end':b} for a,b in spans]


def extract_frames(path, segment, directory, kind='video'):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    frames = [];prefix = hdr_filter(probe(path,kind))
    for i, fraction in ([(1,0.5)] if kind == 'photo' else enumerate((0.15,0.5,0.85))):
        name = f"{segment['id']}_{i}.jpg"
        args = ['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y']
        if kind == 'video':args += ['-ss',str(segment['start']+(segment['end']-segment['start'])*fraction)]
        command(args+['-protocol_whitelist','file','-i',str(path),'-vf',prefix+'scale=640:-2','-frames:v','1',str(directory/name)])
        if not (directory/name).is_file():raise RuntimeError('素材抽帧失败：'+str(path))
        frames.append('frames/'+name)
    return frames


def usable_span(segment, usable):
    a,b = segment['start'],segment['end']
    third = (b-a)/3
    if not usable[1]:return a,a
    return a+(0 if usable[0] else third), b-(0 if usable[2] else third)


def contact_sheet(frames, path, kind):
    path = Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    args = ['ffmpeg','-v','error','-nostdin','-y']
    for frame in frames:args += ['-protocol_whitelist','file','-i',str(frame)]
    if kind=='photo':args += ['-vf','scale=960:-2']
    else:
        filters = ';'.join(f'[{i}:v]scale=480:-2,setsar=1[v{i}]' for i in range(3))
        args += ['-filter_complex',filters+';[v0][v1][v2]hstack=inputs=3[out]','-map','[out]']
    command(args+['-frames:v','1',str(path)])


def render_clip(item, out_dir, *, force=False, legacy_identity=False):
    import tempfile
    from .index import exclusive_lock
    source, render = item['source'],item['render'];path = Path(source['path'])
    def unchanged():
        try:st = path.stat()
        except OSError:raise RuntimeError('B-roll 素材在生成期间被改动') from None
        if (st.st_size,st.st_mtime_ns)!=(source['size'],source['mtime_ns']):raise RuntimeError('B-roll 素材在生成期间被改动')
    n = item['frames'];w,h = render['width'],render['height']
    if type(n) is not int or n<=0 or any(type(x) is not int or x<=0 or x%2 for x in (w,h)):raise ValueError('B-roll 片段帧数或尺寸无效')
    unchanged()
    info = probe(path,source['kind']);prefix = hdr_filter(info)
    if info['hdr']:render['tonemap'] = 'mobius-v1'
    bitrate = render.setdefault('bitrate', '20M' if source['kind']=='photo' else '50M')
    if legacy_identity:render['audio'] = None
    audio = render.setdefault('audio','pcm48k-v1' if info.get('has_audio',False) else None)
    if audio not in (None,'pcm48k-v1') or audio and not info.get('has_audio',False):raise ValueError('B-roll 冻结原声与素材不符')
    item['has_audio'] = audio is not None
    samples = round(n*1001*48000/30000)
    identity = [source, n, w, h, render['version'], render.get('tonemap'), bitrate] + ([] if legacy_identity else [{'audio':audio}])
    directory = safe_directory(out_dir);directory.mkdir(parents=True,exist_ok=True)
    output = directory/(hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:16]+'.mov')
    def valid(file):
        try:
            data = json.loads(command(['ffprobe','-v','error','-show_streams','-of','json','-protocol_whitelist','file','-i',str(file)]).stdout)
            streams = data['streams'];v = [s for s in streams if s['codec_type']=='video']
            a = [s for s in streams if s['codec_type']=='audio']
            audio_valid = not a if not audio else len(a)==1 and a[0].get('codec_name')=='pcm_s16le' and int(a[0].get('sample_rate',0))==48000 and a[0].get('channels')==2 and int(a[0].get('duration_ts',0))*Fraction(a[0]['time_base'])*48000==samples and int(a[0].get('nb_frames',0))==samples
            return len(v)==1 and audio_valid and int(v[0].get('nb_frames',0))==n and v[0]['avg_frame_rate']=='30000/1001' and v[0]['r_frame_rate']=='30000/1001' and (v[0]['width'],v[0]['height'])==(w,h) and v[0].get('pix_fmt')=='yuv420p' and all(v[0].get(k)=='bt709' for k in ('color_space','color_primaries','color_transfer'))
        except (RuntimeError,ValueError,KeyError):return False
    with exclusive_lock(directory/'.render.lock','B-roll 片段正在另一处生成'):
        unchanged()
        if not force and output.exists() and valid(output):unchanged();return output
        fd,name = tempfile.mkstemp(prefix='.clip-',suffix='.partial',dir=directory);os.close(fd);temporary = Path(name)
        try:
            args = ['ffmpeg','-v','error','-nostdin','-y']
            if source['kind']=='photo':args += ['-loop','1']
            else:args += ['-ss',str(source['start_seconds'])]
            args += ['-protocol_whitelist','file','-i',str(path),'-map','0:v:0','-vf',prefix+f'scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:out_range=tv,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709',
                     '-r','30000/1001','-fps_mode','cfr','-frames:v',str(n),'-c:v',render['codec'],'-b:v',bitrate,'-pix_fmt','yuv420p','-color_range','tv','-colorspace','bt709','-color_primaries','bt709','-color_trc','bt709','-f','mov',str(temporary)]
            vf = args.index('-vf')+1
            args[vf] += f',fps=30000/1001,tpad=stop_mode=clone:stop_duration={n*1001/30000},trim=end_frame={n},setpts=N*1001/(30000*TB)'
            # Limit audio independently; -frames:v can stop audio encoding early,
            # so encode both finite filter outputs without an output frame limit.
            if audio:
                index = args.index('-frames:v');del args[index:index+2]
                index = args.index('-r')
                args[index:index] = ['-map','0:a:0','-af',f'aresample=48000,apad,atrim=end_sample={samples},asetpts=N/SR/TB','-c:a','pcm_s16le','-ar','48000','-ac','2']
            else:args[args.index('-c:v'):args.index('-c:v')] = ['-an']
            command(args);unchanged()
            if not valid(temporary):raise RuntimeError('B-roll 片段帧数、帧率、音轨或尺寸校验失败')
            os.replace(temporary,output)
        finally:temporary.unlink(missing_ok=True)
    return output


def extract_middle_frame(path, segment, output, kind='video'):
    """One 1280-wide reference frame; no source mutation."""
    output = Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    args = ['ffmpeg','-v','error','-nostdin','-y']
    if kind=='video':args += ['-ss',str((segment['start']+segment['end'])/2)]
    command(args+['-protocol_whitelist','file','-i',str(path),'-vf',hdr_filter(probe(path,kind))+'scale=1280:-2','-frames:v','1',str(output)])
    if not output.is_file():raise RuntimeError('中间帧生成失败：'+str(path))
    return output


def rehearsal_sheet(shots, output, timeline_fps=29.97):
    """Rows follow the placement report; sample the actual source in/mid/last frame."""
    from .live.candidates import frame_rate
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    fps=frame_rate(timeline_fps)
    with tempfile.TemporaryDirectory(prefix='broll-rehearsal-') as tmp:
        rows=[]
        for number,shot in enumerate(shots,1):
            kind=shot['kind'];count=shot['end']-shot['start']
            positions=[shot['source_start']+n for n in (0,(count-1)//2,count-1)] if kind=='video' else [0]
            frames=[]
            with photo_source(shot['file'],kind) as source:
                prefix=hdr_filter(probe(source,kind))
                for n,frame in enumerate(positions):
                    target=Path(tmp)/f'{number}-{n}.jpg'
                    args=['ffmpeg','-v','error','-nostdin','-y']
                    if kind=='video':args+=['-ss',str(float(Fraction(frame)/fps))]
                    vf=prefix+'scale=480:270:force_original_aspect_ratio=decrease,pad=480:270:(ow-iw)/2:(oh-ih)/2,setsar=1'
                    command(args+['-protocol_whitelist','file','-i',str(source),'-vf',vf,'-frames:v','1',str(target)])
                    if not target.is_file():raise RuntimeError('演练抽帧失败：'+shot['spot']+' / '+shot['file'])
                    frames.append(target)
            row=Path(tmp)/f'row-{number}.jpg'
            args=['ffmpeg','-v','error','-nostdin','-y']
            for frame in frames:args+=['-i',str(frame)]
            # Numbered rows match the ordered report, including one-frame photos.
            label=f'drawtext=text={number}:x=8:y=8:fontsize=24:fontcolor=white:box=1:boxcolor=black'
            filters=('hstack=inputs=3,' if len(frames)==3 else 'pad=1440:270:0:0,')+label
            command(args+['-filter_complex',filters,'-frames:v','1',str(row)])
            rows.append(row)
        args=['ffmpeg','-v','error','-nostdin','-y']
        for row in rows:args+=['-i',str(row)]
        if len(rows)>1:args+=['-filter_complex',f'vstack=inputs={len(rows)}']
        command(args+['-frames:v','1',str(output)])
    return str(output)


def render_live_clip(candidate, out_dir, spot, source_start, frames, *, timeline_fps=29.97,
                     resolution=(1920,1080), lut_path=None, codec='prores_ks', force=False):
    """v3 render with bounded independent handles; old render_clip is unchanged.

    source_start/frames/usable_* are T1 timeline frames. Returns path and
    startFrame (= actual pre-handle) plus metadata; insufficient video raises.
    LUT replaces HDR conversion. Audio is omitted (v3 main picture only).
    """
    import tempfile
    from math import floor
    from .index import exclusive_lock, atomic_json
    from .live.candidates import frame_rate
    if not isinstance(spot,str) or not re.fullmatch(r'[\w-]{1,64}',spot):raise ValueError('spot文件名无效：'+str(spot))
    if type(source_start) is not int or source_start<0 or type(frames) is not int or frames<=0:raise ValueError('spot '+spot+' / 段 '+str(candidate.get('segment','未提供ID'))+'：源入点/帧数无效：'+str((source_start,frames)))
    w,h=resolution
    if any(type(x) is not int or x<=0 or x%2 for x in (w,h)):raise ValueError('片段尺寸须为正偶数')
    fps=frame_rate(timeline_fps)
    if fps!=Fraction(30000,1001):raise ValueError('发布版片段仅支持29.97时间线')
    path=Path(candidate.get('file_path',candidate.get('file','')))
    kind=candidate['kind'];stat=path.stat()
    identity={'path':str(path),'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
    for key in ('size','mtime_ns'):
        if candidate.get(key) is not None and candidate[key]!=identity[key]:raise RuntimeError('B-roll素材身份已改变')
    info=probe(path,kind)
    lo=candidate.get('usable_start',0)
    hi=candidate.get('usable_end',floor(info['duration']*fps+1e-7))
    if kind=='video':
        hi=min(hi,floor(info['duration']*fps+1e-7))
        if candidate.get('available_ranges') is not None:
            spans=[(math.ceil(a*fps-1e-7),math.floor(b*fps+1e-7)) for a,b in candidate['available_ranges']]
            available=next(((a,b) for a,b in spans if a<=source_start and source_start+frames<=b),None)
            if available is None:raise ValueError('素材不足或越过已用源区间，不能复制末帧补长')
            lo=max(lo,available[0]);hi=min(hi,available[1])
        if source_start<lo or source_start+frames>hi:raise ValueError('素材不足或越过可用范围，不能复制末帧补长')
        before=min(floor(2*fps),source_start-lo)
        after=min(floor(2*fps),hi-source_start-frames)
    elif kind=='photo':
        before=after=floor(2*fps)
    else:raise ValueError('仅支持视频和照片')
    if any(type(n) is not int or n<0 for n in (before,after)):raise ValueError('可用范围必须为整数帧')
    begin=source_start-before if kind=='video' else 0
    total=before+frames+after
    if kind=='video':
        assert lo<=begin and begin+total<=hi
        assert 0<=float(Fraction(begin)/fps) and float(Fraction(begin+total)/fps)<=info['duration']+1e-7
    lut=Path(lut_path) if lut_path else None
    lut_sha=hashlib.sha256(lut.read_bytes()).hexdigest() if lut else None
    if lut and lut.suffix.lower()!='.cube':raise ValueError('LUT须为.cube文件')
    if (candidate.get('is_log') or candidate.get('log') or candidate.get('probe',{}).get('log') or info.get('log')) and not lut:
        raise ValueError('Log素材缺少LUT，应跳过该候选')
    input_lut=candidate.get('input_lut')
    if input_lut and str(input_lut).strip().casefold() not in ('none','no lut','无','无lut') and not lut:
        raise ValueError('Input LUT尚未解析，应跳过该候选')
    prefix='' if lut else hdr_filter(info)
    metadata={'version':3,'source':identity,'source_path':str(path),'source_start':source_start,
        'source_start_seconds':float(Fraction(source_start)/fps),'render_start_seconds':float(Fraction(begin)/fps),
        'frames':frames,'total_frames':total,'before_frames':before,'after_frames':after,
        'before_seconds':float(Fraction(before)/fps),'after_seconds':float(Fraction(after)/fps),
        'timeline_fps':[fps.numerator,fps.denominator],'resolution':[w,h],'codec':codec,
        'lut':{'path':str(lut),'sha256':lut_sha} if lut else None,
        'hdr_to_709':bool(info['hdr'] and not lut),'kind':kind}
    hash8=hashlib.sha256(json.dumps(metadata,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:8]
    directory=safe_directory(out_dir);directory.mkdir(parents=True,exist_ok=True)
    output=directory/f'{path.name}__{spot}_{hash8}.mov';sidecar=output.with_suffix('.json')
    def unchanged():
        current=path.stat()
        if (current.st_size,current.st_mtime_ns)!=(stat.st_size,stat.st_mtime_ns):raise RuntimeError('B-roll素材在生成期间被改动')
        if lut and hashlib.sha256(lut.read_bytes()).hexdigest()!=lut_sha:raise RuntimeError('LUT在生成期间被改动')
    def valid(file):
        try:
            streams=json.loads(command(['ffprobe','-v','error','-show_streams','-of','json',str(file)]).stdout)['streams']
            videos=[s for s in streams if s.get('codec_type')=='video']
            return (len(streams)==len(videos)==1 and int(videos[0].get('nb_frames',0))==total
                and videos[0]['avg_frame_rate']=='30000/1001' and videos[0]['r_frame_rate']=='30000/1001'
                and (videos[0]['width'],videos[0]['height'])==(w,h)
                and all(videos[0].get(k)=='bt709' for k in ('color_space','color_transfer','color_primaries')))
        except (RuntimeError,ValueError,KeyError):return False
    with exclusive_lock(directory/'.render.lock','B-roll片段正在另一处生成'):
        unchanged()
        if not force and output.exists() and sidecar.exists() and read_json(sidecar)==metadata and valid(output):
            return {'path':str(output),'startFrame':before,'frames':frames,'metadata':metadata}
        fd,name=tempfile.mkstemp(prefix='.live-',suffix='.mov',dir=directory);os.close(fd);temporary=Path(name)
        staged_lut=None
        try:
            if lut:
                # Avoid ffmpeg's two-level filename escaping for SDK LUT names.
                fd,lut_name=tempfile.mkstemp(prefix='broll-lut-',suffix='.cube');os.close(fd)
                staged_lut=Path(lut_name);staged_lut.write_bytes(lut.read_bytes())
                prefix='lut3d=file='+str(staged_lut)+','
            args=['ffmpeg','-v','error','-nostdin','-y']
            if kind=='photo':args += ['-loop','1']
            else:args += ['-ss',str(float(Fraction(begin)/fps))]
            filters=prefix+f'fps=30000/1001,scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,trim=end_frame={total},setpts=N*1001/(30000*TB),setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709'
            args += ['-protocol_whitelist','file','-i',str(path),'-map','0:v:0','-an','-vf',filters,
                     '-frames:v',str(total),'-c:v',codec,'-pix_fmt','yuv422p10le',
                     '-colorspace','bt709','-color_primaries','bt709','-color_trc','bt709',
                     '-color_range','tv','-f','mov',str(temporary)]
            command(args);unchanged()
            if not valid(temporary):raise RuntimeError('片段帧数/帧率/尺寸/709校验失败；素材不足时禁止补末帧')
            os.replace(temporary,output);atomic_json(sidecar,metadata)
        finally:
            temporary.unlink(missing_ok=True)
            if staged_lut:staged_lut.unlink(missing_ok=True)
    return {'path':str(output),'startFrame':before,'frames':frames,'metadata':metadata}
