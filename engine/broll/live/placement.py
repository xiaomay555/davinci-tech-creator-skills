"""Placement decisions and LUT lookup; no Resolve connection."""
from fractions import Fraction
from pathlib import Path
from .candidates import frame_rate

DEFAULT_DIRECT_FPS = (Fraction(30000,1001),)
DEFAULT_LUT_DIRS = (Path('/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT'),
    Path.home()/'Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT')


def choose_placement(candidate, source_start, frames, *, timeline_fps=29.97, direct_fps=DEFAULT_DIRECT_FPS):
    """Return direct/render plus native source frame; source_start is T1 frames."""
    if any(type(n) is not int or n<0 for n in (source_start,frames)) or frames==0:
        raise ValueError('段 '+str(candidate.get('segment','未提供ID'))+'：源入点/帧数无效：'+str((source_start,frames)))
    probe=candidate.get('probe',{})
    fps=candidate.get('fps') or ([probe['fps_num'],probe['fps_den']] if probe.get('fps_num') and probe.get('fps_den') else None)
    target=frame_rate(timeline_fps);rate=frame_rate(fps) if fps else target
    allowed={frame_rate(r) for r in direct_fps}
    hdr=bool(candidate.get('hdr') or probe.get('hdr'))
    vfr=candidate.get('vfr',probe.get('vfr'))
    kind=candidate['kind']
    direct=not hdr and (kind=='photo' or kind=='video' and fps is not None and target==Fraction(30000,1001) and rate in allowed and vfr is False)
    reason=('原照片直接引用' if kind=='photo' else '恒定帧率原片直接引用') if direct else 'HDR需转709' if hdr else '可变帧率需转换' if vfr else '未知恒定帧率，需转换' if vfr is None else '该源帧率暂不支持直接放置，需转换'
    seconds=Fraction(source_start)/target
    return {'method':'direct' if direct else 'render','reason':reason,
            'source_start_frame':0 if kind=='photo' else round(seconds*rate),'source_start_seconds':float(seconds),
            'source_frames':round(Fraction(frames)*rate/target), 'frames':frames}


def select_lut(candidate, *, folder_rules=(), lut_dirs=None):
    """Return ok/skip and .cube path; missing/ambiguous LUTs skip one candidate.

    folder_rules=[{folder, lut}]; longest matching folder wins outside pool.
    Relative SDK names are matched recursively by filename (case insensitive).
    """
    path=Path(candidate.get('file_path',candidate.get('file','')))
    name=candidate.get('input_lut')
    if isinstance(name,str) and name.strip().casefold() in ('','none','no lut','无','无lut'):
        name=None
    if not candidate.get('in_media_pool'):
        matches=[r for r in folder_rules if path.is_relative_to(Path(r['folder']))]
        name=max(matches,key=lambda r:len(Path(r['folder']).parts))['lut'] if matches else None
    if not name:
        if candidate.get('is_log') or candidate.get('log') or candidate.get('probe',{}).get('log'):
            return {'status':'skip','lut_path':None,'reason':'源片判为Log，但没有Input LUT或文件夹LUT规则，跳过该候选'}
        return {'status':'ok','lut_path':None,'reason':'无需烘焙LUT'}
    if not isinstance(name,str):raise ValueError('Input LUT名称须为字符串')
    filename=Path(name).name
    if not filename.lower().endswith('.cube'):filename += '.cube'
    roots=DEFAULT_LUT_DIRS if lut_dirs is None else [Path(p) for p in lut_dirs]
    relative=Path(name if name.lower().endswith('.cube') else name+'.cube')
    found=sorted({str(p.resolve()) for root in roots if root.is_dir() and relative.parent!=Path('.')
                  for p in root.rglob('*') if p.is_file() and p.relative_to(root).as_posix().casefold()==relative.as_posix().casefold()})
    if not found:
        found=sorted({str(p.resolve()) for root in roots if root.is_dir() for p in root.rglob('*') if p.is_file() and p.suffix.lower()=='.cube' and p.name.casefold()==filename.casefold()})
    if len(found)!=1:
        return {'status':'skip','lut_path':None,'reason':('找不到Input LUT：' if not found else '同名Input LUT不唯一：')+str(name)+'，跳过该候选'}
    return {'status':'ok','lut_path':found[0],'reason':'烘焙源片Input LUT'}


def clean_candidates(files, referenced_paths):
    """Pure manifest computation; referenced MOV/sidecar pairs stay untouched."""
    # Lexical paths only: no directory scan, stat, move or deletion.
    available={str(Path(p)) for p in files}
    referenced={str(Path(p)) for p in referenced_paths}
    result=[]
    for name in sorted(available):
        path=Path(name)
        if path.suffix.lower()!='.mov' or path.name.startswith('.') or '待清理' in path.parts:
            continue
        sidecar=str(path.with_suffix('.json'))
        if name in referenced or sidecar in referenced:continue
        result.append({'path':name,'destination':str(path.parent/'待清理'/path.name)})
        if sidecar in available:
            result.append({'path':sidecar,'destination':str(path.parent/'待清理'/Path(sidecar).name)})
    return result


def prepare_placement(candidate, source_start, frames, *, timeline_fps=29.97,
                      direct_fps=DEFAULT_DIRECT_FPS, folder_rules=(), lut_dirs=None):
    """Combine placement/LUT checks; callers can try fallback when method=skip."""
    placement=choose_placement(candidate,source_start,frames,timeline_fps=timeline_fps,direct_fps=direct_fps)
    if placement['method']=='direct' and not candidate.get('in_media_pool'):
        lut=select_lut(candidate,folder_rules=folder_rules,lut_dirs=lut_dirs)
        if lut['status']=='skip':return dict(placement,method='skip',lut_path=None,reason=lut['reason'])
        if lut['lut_path']:return dict(placement,method='render',lut_path=lut['lut_path'],reason='池外素材需烘焙文件夹LUT')
    if placement['method']=='direct':
        if (candidate.get('is_log') or candidate.get('log') or candidate.get('probe',{}).get('log')) and (not candidate.get('input_lut') or str(candidate.get('input_lut')).strip().casefold() in ('none','no lut','无','无lut')):
            return dict(placement,method='skip',reason='源片判为Log，但没有Input LUT，跳过该候选')
        return dict(placement,input_lut=candidate.get('input_lut'),lut_path=None)
    lut=select_lut(candidate,folder_rules=folder_rules,lut_dirs=lut_dirs)
    return dict(placement,method='skip' if lut['status']=='skip' else 'render',
                lut_path=lut['lut_path'],reason=lut['reason'] if lut['status']=='skip' else placement['reason'])
