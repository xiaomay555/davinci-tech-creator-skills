"""Small SDK boundary. All untested B-roll SDK semantics are collected here.

No render, database switching, or opening a project on initial connection.
"""
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from engine.fx.connect import connect as fx_connect, guard, find_timeline
from engine.fx.snapshot import snapshot as fx_snapshot
from engine.fx.resolve_io import ResolveIO, objects, folder_clips, SETTINGS
from engine.fx.learn import parse_drt_subtitles
from .timeline import normalize_snapshot
from engine.fx.review import IMAGE_SETTINGS, COLOR_SETTINGS
from .candidates import frame_rate

TRANSFORMS = ('ZoomGang', 'ZoomX', 'ZoomY', 'Pan', 'Tilt', 'RotationAngle',
              'Pitch', 'Yaw', 'FlipX', 'FlipY', 'AnchorPointX', 'AnchorPointY', 'AudioVolume')


class PhotoDurationMismatch(RuntimeError):
    """Only a correctly located still with a wrong duration can be retried."""
    def __init__(self, item, requested, actual):
        self.item_id = item.GetUniqueId()
        self.requested, self.actual = requested, actual
        super().__init__(f'接口放置静态图片长度不符：请求 {requested} 帧，实际 {actual} 帧')


def connect(name=None):
    if name:
        return fx_connect(name)
    # Same fx connection contract, with no timeline-name requirement.
    import locale
    import os
    import sys
    api = os.environ.get('RESOLVE_SCRIPT_API', '/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting')
    os.environ.setdefault('RESOLVE_SCRIPT_LIB', '/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so')
    sys.path.insert(0, str(Path(api)/'Modules'))
    previous = locale.setlocale(locale.LC_ALL)
    try:
        import DaVinciResolveScript
        resolve = DaVinciResolveScript.scriptapp('Resolve')
    finally:
        locale.setlocale(locale.LC_ALL, previous)
    if not resolve:
        raise RuntimeError('无法连接已打开的达芬奇，请先打开目标工程')
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project or not project.GetCurrentTimeline():
        raise ValueError('请打开目标工程和时间线；本工具不会打开其他工程')
    return resolve, project, project.GetCurrentTimeline()


def database_type(resolve):
    db = resolve.GetProjectManager().GetCurrentDatabase() or {}
    # 待实测：各版本协同库 DbType 的取值；未知类型也禁止写入。
    value = str(db.get('DbType', db.get('Type', ''))).casefold()
    if 'postgres' in value:
        return 'PostgreSQL'
    if value in ('disk', 'local', 'sqlite'):
        return 'Disk'
    return '未知'


@contextmanager
def scene(resolve, project, target):
    guard(resolve, project)
    mp = project.GetMediaPool()
    original, folder = project.GetCurrentTimeline(), mp.GetCurrentFolder()
    heads = [(t, t.GetCurrentTimecode()) for t in (original, target) if t]
    try:
        if not project.SetCurrentTimeline(target):
            raise RuntimeError('无法激活目标时间线')
        guard(resolve, project, target)
        yield
        guard(resolve,project,target)
    finally:
        guard(resolve, project)
        # 待实测：SetCurrentTimeline 后播放头恢复是否异步；每次设置均读回。
        for t, tc in reversed(heads):
            if not project.SetCurrentTimeline(t) or not t.SetCurrentTimecode(tc) or t.GetCurrentTimecode() != tc:
                raise RuntimeError('恢复播放头失败，请检查现场')
        if original and not project.SetCurrentTimeline(original):
            raise RuntimeError('恢复原当前时间线失败')
        if not mp.SetCurrentFolder(folder) or mp.GetCurrentFolder().GetUniqueId() != folder.GetUniqueId():
            raise RuntimeError('恢复媒体池文件夹失败')


def read_snapshot(resolve, project, timeline):
    with scene(resolve, project, timeline):
        data = fx_snapshot(resolve, project, timeline)
        by_id = {i.GetUniqueId(): i for i in objects(timeline)}
        for row in data['timeline']['items']:
            item = by_id[row['id']]
            path = Path(row.get('media_path') or '')
            photo = path.suffix.casefold() in ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff') or row.get('media_type') in ('Still', 'Photo', '照片')
            media = item.GetMediaPoolItem()
            # 待实测：LeftOffset/RightOffset 和变换/音量字段；照片 offset 不可信。
            row['broll_extra'] = {'left_offset': None if photo or row['type']=='subtitle' else item.GetLeftOffset(),
                'right_offset': None if photo or row['type']=='subtitle' else item.GetRightOffset(),
                'color': item.GetClipColor(), 'media_id': media.GetUniqueId() if media else None,
                'file_size': path.stat().st_size if path.is_file() else None,
                **{k: item.GetProperty(k) for k in TRANSFORMS}}
            if row['type']=='video' and not row.get('media_path') and not row.get('texts'):
                row['broll_extra']['composite']=bool(item.GetFusionCompCount() or
                    any(s in str(row.get('media_type') or '').casefold() for s in ('compound','fusion','复合')))
                if not row.get('nested') and media:
                    from engine.fx.snapshot import read_timeline
                    get_timeline=getattr(media,'GetTimeline',None)
                    try:
                        nested=get_timeline() if callable(get_timeline) else None
                        if nested:row['nested']=read_timeline(nested)
                    except (RuntimeError,ValueError,AttributeError) as exc:
                        row['broll_extra']['composite']=True
                        row['broll_extra']['source_trace_error']=str(exc)
        subtitles = [r for r in data['timeline']['items'] if r['type']=='subtitle']
        if any((r.get('subtitle_text') or '').strip().casefold() in ('', 'subtitle', 'subtitle clip', '字幕') for r in subtitles):
            with TemporaryDirectory(prefix='broll-subtitles-') as temp:
                path = Path(temp)/'subtitles.drt'
                # 待实测：DRT 中字幕绝对帧是否与 SDK 完全一致；解析器严格匹配。
                if not timeline.Export(str(path), resolve.EXPORT_DRT) or not path.is_file():
                    raise RuntimeError('通用字幕名，临时DRT导出失败')
                cues = parse_drt_subtitles(path, subtitles)
                mapping = {(c['track'], c['start'], c['end']): c['text'] for c in cues}
                for r in subtitles:
                    r['subtitle_text'] = mapping[r['track'], r['start'], r['end']]
        data['timeline'] = normalize_snapshot(data)
        data['timeline']['broll_extra']['track_enabled_trusted'] = True
        return data


def media_pool(project):
    rows = []
    def walk(folder, parent=''):
        bin_name = '/'.join(filter(None, (parent, folder.GetName())))
        for media in folder.GetClipList() or []:
            props = media.GetClipProperty() or {}
            path = props.get('File Path')
            if not path:
                continue
            # 中文界面读回 '视频 + 音频'/'静帧'/'音频'（21.1.1 实测），英文为 'Video + Audio'/'Still'/'Audio'。
            typ = ''.join(str(props.get('Type', '')).casefold().split())
            kind = 'photo' if Path(path).suffix.casefold() in ('.jpg','.jpeg','.png','.webp','.bmp','.tif','.tiff','.heic') or typ in ('still','photo','静帧') else 'audio' if typ in ('audio','音频') else 'video' if 'video' in typ or '视频' in typ else 'other'
            fps = props.get('FPS') or '29.97'
            # 待实测：媒体池 Frames/Duration 的单位。优先 Frames，时间码按素材帧率。
            frames = props.get('Frames')
            duration = None
            if frames not in (None, ''):
                duration = float(frames)/float(frame_rate(fps))
            elif props.get('Duration'):
                raw = str(props['Duration'])
                if ':' in raw or ';' in raw:
                    duration = timecode_frame(raw, fps)/float(frame_rate(fps))
                else:
                    raise ValueError('媒体池时长单位不明确：'+path)
            rows.append({'file_path': path, 'kind':kind, 'fps':fps, 'duration':duration or 0,
                         'input_lut':props.get('Input LUT'), 'bin':bin_name, 'media_id':media.GetUniqueId()})
        for sub in folder.GetSubFolderList() or []:
            walk(sub, bin_name)
    walk(project.GetMediaPool().GetRootFolder())
    return rows


def timecode_frame(tc, fps):
    import re
    match = re.fullmatch(r'(\d{2,}):(\d{2}):(\d{2})([:;])(\d{2})', tc)
    if not match:
        raise ValueError('时间码格式应为 HH:MM:SS:FF')
    h, m, s, sep, f = match.groups(); h,m,s,f = map(int,(h,m,s,f))
    nominal = round(float(frame_rate(fps)))
    if m>=60 or s>=60 or f>=nominal:
        raise ValueError('时间码数值超出范围')
    value = ((h*60+m)*60+s)*nominal+f
    if sep==';':
        if nominal not in (30,60):
            raise ValueError('丢帧时间码仅支持29.97/59.94')
        drop = 2 if nominal==30 else 4
        if m%10 and s==0 and f<drop:
            raise ValueError('该时间码是被跳过的丢帧编号')
        value -= drop*((h*60+m)-(h*60+m)//10)
    return value


class Adapter(ResolveIO):
    def __init__(self, resolve, project, timeline):
        super().__init__(resolve, project, timeline, project.GetName())

    def snapshot(self):
        self.guard()
        return read_snapshot(self.r, self.p, self.main)

    def media(self, path, generated=False, media_id=None):
        mp = self.p.GetMediaPool()
        found = [m for m in folder_clips(mp.GetRootFolder()) if m.GetClipProperty('File Path')==str(path)]
        if media_id:
            found=[m for m in found if m.GetUniqueId()==media_id]
            if not found:raise ValueError('原媒体池素材ID已变化，请重新plan：'+str(path))
        if len(found)>1:
            raise ValueError('同路径有多个媒体池素材，请重新plan明确原素材ID：'+str(path))
        if found:
            return found[0]
        if generated:
            folder = self.folder('片段', self.folder('B-roll'))
            self.write(mp.SetCurrentFolder, folder)
        imported = self.write(mp.ImportMedia, [str(path)])
        if not imported or len(imported)!=1 or imported[0].GetClipProperty('File Path')!=str(path):
            raise RuntimeError('B-roll片段导入读回不符：'+str(path))
        return imported[0]

    def append_clip(self, media, kind, track, start, offset, length, allow_disabled=False):
        self.activate(self.main)
        if (not allow_disabled and not self.main.GetIsTrackEnabled(kind,track)) or self.main.GetIsTrackLocked(kind,track):
            raise ValueError('目标轨道已停用或锁定')
        if any(i.GetStart()<start+length and start<i.GetEnd() for i in self.main.GetItemListInTrack(kind,track) or []):
            raise ValueError('目标区间已有片段，不覆盖')
        # 待实测：endFrame 半开、源帧域、mediaType=1/2、LeftOffset 读回。
        placed = self.write(self.p.GetMediaPool().AppendToTimeline, [dict(mediaPoolItem=media,
            startFrame=offset, endFrame=offset+length, recordFrame=start, trackIndex=track,
            mediaType=1 if kind=='video' else 2)])
        if not placed or len(placed)!=1:
            raise RuntimeError('精确放置未返回一个片段')
        item = placed[0]
        photo = Path(media.GetClipProperty('File Path') or '').suffix.casefold() in ('.jpg','.jpeg','.png','.webp','.bmp','.tif','.tiff','.heic','.heif') or media.GetClipProperty('Type') in ('Still','Photo','静帧','照片')
        if item.GetUniqueId() not in {i.GetUniqueId() for i in self.main.GetItemListInTrack(kind,track) or []}:
            raise RuntimeError('段 '+item.GetUniqueId()+'：放置轨道读回不符，预期='+kind+str(track))
        if (photo and kind=='video' and item.GetStart()==start
            and item.GetDuration()>0 and item.GetEnd()==start+item.GetDuration()
            and item.GetDuration()!=length):
            raise PhotoDurationMismatch(item,length,item.GetDuration())
        if (item.GetStart(),item.GetEnd(),item.GetDuration())!=(start,start+length,length) or (not photo and item.GetLeftOffset()!=offset):
            raise RuntimeError('段 '+item.GetUniqueId()+'：放置范围或源入点读回不符；预期='+str((start,start+length,length,offset))+'，实际='+str((item.GetStart(),item.GetEnd(),item.GetDuration(),item.GetLeftOffset())))
        return item

    def set_state(self, item, name, color='Teal', enabled=True, props=None):
        # 待实测：TimelineItem.SetName/颜色/ZoomGang 解锁后 ZoomX/Y 和 −5dB 音量持久化。
        for fn, value, read in ((item.SetName,name,item.GetName),(item.SetClipColor,color,item.GetClipColor),(item.SetClipEnabled,enabled,item.GetClipEnabled)):
            if value is None:continue
            if not self.write(fn,value) or read()!=value:
                raise RuntimeError('段 '+item.GetUniqueId()+'：片段名称/颜色/启用读回不符；预期='+str(value)+'，实际='+str(read()))
        props = props or {}
        from engine.fx.ledger import equal
        for key in sorted(props, key=lambda k: (k!='ZoomGang', k)):
            # 21.1.1 实测：AnchorPointX/Y 即使写入相同值 SetProperty 也返回 False；
            # 已相同就不写，写后只以读回为准。
            if equal(item.GetProperty(key),props[key]):continue
            self.write(item.SetProperty,key,props[key])
            if not equal(item.GetProperty(key),props[key]):
                raise RuntimeError('段 '+item.GetUniqueId()+'：片段属性读回不符：'+key+'；预期='+str(props[key])+'，实际='+str(item.GetProperty(key)))

    def delete(self, items):
        # 待实测：DeleteClips(False) 非波纹删除，外部负责精确相等的归属校验。
        if items and not self.write(self.main.DeleteClips,items,False):
            raise RuntimeError('非波纹删除失败')

    def link(self, video, audio):
        # 待实测：SetClipsLinked/GetLinkedItems 的支持范围及重开持久化。
        if not self.write(self.main.SetClipsLinked,[video,audio],True):
            raise RuntimeError('原声链接失败')
        if audio.GetUniqueId() not in {i.GetUniqueId() for i in video.GetLinkedItems() or []}:
            raise RuntimeError('段 '+video.GetUniqueId()+' / '+audio.GetUniqueId()+'：原声链接读回不符')

    def history(self, name):
        # 待实测：DuplicateTimeline + MoveClips 到历史 bin；主线身份必须保持。
        self.activate(self.main)
        duplicate = self.write(self.main.DuplicateTimeline,name)
        if not duplicate:
            raise RuntimeError('历史时间线复制失败')
        if not self.write(self.p.GetMediaPool().MoveClips,[duplicate.GetMediaPoolItem()],self.folder('历史',self.folder('B-roll'))):
            self.activate(self.main)
            self.delete_timelines([duplicate])
            raise RuntimeError('历史时间线归档失败，已移除本次副本')
        self.activate(self.main)
        return duplicate

    def save_reopen(self):
        self.activate(self.main)
        # 待实测：项目管理器文件夹名称/路径语义与协同库 Close/LoadProject、ID 持久化。
        folder = self.pm.GetCurrentFolder()
        if not self.write(self.pm.SaveProject):
            raise RuntimeError('保存工程失败')
        if not self.write(self.pm.CloseProject,self.p):
            raise RuntimeError('关闭工程失败')
        if self.pm.GetCurrentDatabase()!=self.db or self.pm.GetCurrentFolder()!=folder:
            raise RuntimeError('数据库或项目管理器文件夹已改变，停止重开')
        current = self.pm.GetCurrentProject()
        if current and current.GetName() not in ('Untitled Project','无标题项目',self.name):
            raise RuntimeError('用户已打开其他工程，停止重开')
        self.p = self.pm.LoadProject(self.name)
        self.guard()
        self.main = find_timeline(self.p,self.main_name)
        if self.main.GetUniqueId()!=self.main_id:
            raise RuntimeError('重开后主时间线ID变化')
        self.activate(self.main)

    def create_review(self, name, ranges):
        # 待实测：嵌套主线的视频/音频，源帧以主线起点为0，审核线 marker 为相对帧。
        mp = self.p.GetMediaPool()
        self.write(mp.SetCurrentFolder,self.folder('审核',self.folder('B-roll')))
        if any(self.p.GetTimelineByIndex(i).GetName()==name for i in range(1,self.p.GetTimelineCount()+1)):
            raise ValueError('审核线名称已存在，不覆盖')
        t = self.write(mp.CreateEmptyTimeline,name)
        if not t:
            raise RuntimeError('创建审核线失败')
        try:
            self.activate(t)
            self.write(t.SetSetting,'useCustomSettings','1')
            for key in (*SETTINGS,*IMAGE_SETTINGS,*COLOR_SETTINGS):
                value = self.main.GetSetting(key)
                if value is None:continue
                self.write(t.SetSetting,key,value)
                if str(t.GetSetting(key))!=str(value):
                    raise RuntimeError('审核线 '+name+'：设置读回不符：'+key)
            if not self.write(t.SetStartTimecode,self.main.GetStartTimecode()):
                raise RuntimeError('审核线起点设置失败')
            original = self.main
            try:
                for row in ranges:
                    self.main = t
                    for kind in ('video','audio'):
                        self.append_clip(original.GetMediaPoolItem(),kind,1,row['review_start'],row['start']-original.GetStartFrame(),row['end']-row['start'])
                    marker = dict(color='Cyan',name='、'.join(row['spots']),note='请回原时间线修改',duration=row['review_end']-row['review_start'],customData='broll-review')
                    if not self.write(t.AddMarker,row['review_start']-t.GetStartFrame(),*marker.values()):
                        raise RuntimeError('审核线标记失败')
                self.verify_review(t,ranges,original)
            finally:
                self.main = original
                self.activate(original)
            return t
        except Exception:
            self.activate(self.main)
            self.delete_timelines([t])
            raise

    def verify_review(self, target, ranges, main=None):
        main=main or self.main
        # 待实测：审核线嵌套媒体身份、左右offset及marker在保存重开后持久化。
        with scene(self.r,self.p,target):
            if target.GetStartFrame()!=main.GetStartFrame():raise RuntimeError('审核线 '+target.GetName()+'：起点不符')
            for kind in ('video','audio'):
                items=sorted([i for i in objects(target,kind)],key=lambda i:i.GetStart())
                if len(items)!=len(ranges):raise RuntimeError('审核线 '+target.GetName()+'：'+kind+'片段数量不符，预期='+str(len(ranges))+'，实际='+str(len(items)))
                for item,row in zip(items,ranges):
                    if (item.GetStart(),item.GetEnd(),item.GetLeftOffset())!=(row['review_start'],row['review_end'],row['start']-main.GetStartFrame()) or item.GetMediaPoolItem().GetUniqueId()!=main.GetMediaPoolItem().GetUniqueId() or not item.GetClipEnabled():
                        raise RuntimeError('审核线 '+target.GetName()+' / 段 '+item.GetUniqueId()+' / spot '+','.join(row['spots'])+'：范围或来源读回不符')
            expected={str(row['review_start']-target.GetStartFrame()):dict(color='Cyan',name='、'.join(row['spots']),note='请回原时间线修改',duration=row['review_end']-row['review_start'],customData='broll-review') for row in ranges}
            if {str(k):v for k,v in (target.GetMarkers() or {}).items()}!=expected:raise RuntimeError('审核线 '+target.GetName()+' / spots '+','.join(s for r in ranges for s in r['spots'])+'：标记读回不符')

    def remove_track(self, kind, index):
        # 待实测：DeleteTrack 仅用于本次创建且已清空的原声轨回滚。
        if self.main.GetItemListInTrack(kind,index) or not self.write(self.main.DeleteTrack,kind,index):
            raise RuntimeError('回滚新建原声轨失败')

    def delete_timelines(self, timelines):
        # 待实测：删除本次失败创建的历史/审核副本，不触碰既有时间线。
        if timelines and not self.write(self.p.GetMediaPool().DeleteTimelines,timelines):
            raise RuntimeError('回滚本次副本失败')
