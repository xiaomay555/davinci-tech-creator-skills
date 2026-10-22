"""Stateful fake SDK, used only by offline T3 tests."""
from copy import deepcopy
from itertools import count
from pathlib import Path
import zipfile
from ._fixtures import snapshot

_ids=count(1)
def uid():return 'fake-'+str(next(_ids))

class Media:
    def __init__(self,path,kind='Video',**props):
        self.id=uid();self.props={'File Path':path,'Clip Name':Path(path).name,'Type':kind,'FPS':'29.97','Frames':'1200','Input LUT':'None',**props};self.timeline=None
    def GetUniqueId(self):return self.id
    def GetClipProperty(self,key=None):return self.props.get(key) if key else deepcopy(self.props)
    def GetTimeline(self):return self.timeline

class Item:
    def __init__(self,row,media):self.row=deepcopy(row);self.media=media;self.links=[]
    def GetUniqueId(self):return self.row['id']
    def GetName(self):return self.row['name']
    def GetStart(self):return self.row['start']
    def GetEnd(self):return self.row['end']
    def GetDuration(self):return self.GetEnd()-self.GetStart()
    def GetClipEnabled(self):return self.row['enabled']
    def GetSourceStartFrame(self):return self.row['source_start']
    def GetSourceEndFrame(self):return self.row['source_end']
    def GetLeftOffset(self):return self.row['source_start']
    def GetRightOffset(self):return 1200-(self.row['source_start'] or 0)-self.GetDuration()
    def GetMediaPoolItem(self):return self.media
    def GetProperty(self,key=None):return self.row.get('props',{}).get(key) if key else deepcopy(self.row.get('props',{}))
    def GetClipColor(self):return self.row.get('color','')
    def GetFusionCompCount(self):return 0
    def GetTrackTypeAndIndex(self):return [self.row['type'],self.row['track']]
    def SetName(self,v):self.row['name']=v;return True
    # 21.1.1 实测：AnchorPointX/Y 写入后值生效但 SetProperty 返回 False。
    def SetProperty(self,k,v):self.row.setdefault('props',{})[k]=v;return k not in ('AnchorPointX','AnchorPointY')
    def SetClipColor(self,v):self.row['color']=v;return True
    def SetClipEnabled(self,v):self.row['enabled']=v;return True
    def GetLinkedItems(self):return self.links

class Folder:
    def __init__(self,name):self.name=name;self.id=uid();self.clips=[];self.sub=[]
    def GetName(self):return self.name
    def GetUniqueId(self):return self.id
    def GetClipList(self):return self.clips
    def GetSubFolderList(self):return self.sub

class Timeline:
    def __init__(self,project,name='main',value=None):
        self.project=project;self.value=deepcopy(value or snapshot(name));self.id=uid();self.name=name;self.tc='00:00:00:12'
        self.settings={'timelineFrameRate':'29.97','timelineResolutionWidth':'1920','timelineResolutionHeight':'1080','timelineDropFrameTimecode':'0'}
        self.items=[]
        for row in self.value['items']:
            media=project.pool.find(row.get('media_path')) if row.get('media_path') else None
            self.items.append(Item(row,media))
        self.media=Media('',kind='Timeline');self.media.timeline=self
        self.generic_drt=False;self.export_fail=False
    def GetUniqueId(self):return self.id
    def GetName(self):return self.name
    def GetMediaPoolItem(self):return self.media
    def GetStartFrame(self):return self.value['start']
    def GetEndFrame(self):return self.value['end']
    def GetStartTimecode(self):return '00:00:00:00'
    def SetStartTimecode(self,v):return True
    def GetCurrentTimecode(self):return self.tc
    def SetCurrentTimecode(self,v):self.tc=v;return True
    def GetSetting(self,key=None):return self.settings.get(key) if key else deepcopy(self.settings)
    def SetSetting(self,k,v):self.settings[k]=v;return True
    def GetMarkers(self):return deepcopy(self.value['markers'])
    def GetMarkInOut(self):return {}
    def GetTrackCount(self,kind):return sum(t['type']==kind for t in self.value['tracks'])
    def _track(self,k,i):return next(t for t in self.value['tracks'] if t['type']==k and t['index']==i)
    def GetTrackName(self,k,i):return self._track(k,i)['name']
    def GetIsTrackEnabled(self,k,i):return self._track(k,i)['enabled']
    def GetIsTrackLocked(self,k,i):return self._track(k,i).get('locked',False)
    def GetItemListInTrack(self,k,i):return [v for v in self.items if v.row['type']==k and v.row['track']==i]
    def DeleteClips(self,items,ripple):
        assert ripple is False
        self.items=[i for i in self.items if i not in items];return True
    def AddTrack(self,k,*args):self.value['tracks'].append(dict(type=k,index=self.GetTrackCount(k)+1,name='',enabled=True,locked=False));return True
    def SetTrackName(self,k,i,n):self._track(k,i)['name']=n;return True
    def DeleteTrack(self,k,i):self.value['tracks'].remove(self._track(k,i));return True
    def SetClipsLinked(self,items,value):
        for item in items:item.links=[i for i in items if i is not item] if value else []
        return True
    # 时间线标记只接受标记颜色（Cyan 等）；片段颜色 Teal 会被拒绝。
    MARKER_COLORS={'Blue','Cyan','Green','Yellow','Red','Pink','Purple','Fuchsia','Rose','Lavender','Sky','Mint','Lemon','Sand','Cocoa','Cream'}
    def AddMarker(self,frame,color,name,note,duration,data):
        if color not in self.MARKER_COLORS:return False
        self.value['markers'][str(frame)]=dict(color=color,name=name,note=note,duration=duration,customData=data);return True
    def DuplicateTimeline(self,name):
        val=deepcopy(self.value);val['items']=[deepcopy(i.row) for i in self.items]
        for r,i in zip(val['items'],self.items):
            r['id']=uid()
            if i.media:r['media_path']=i.media.GetClipProperty('File Path')
        t=Timeline(self.project,name,val);self.project.timelines.append(t);self.project.pool.current.clips.append(t.media);return t
    def Export(self,path,kind):
        if self.export_fail:return False
        cues=''.join(f'<Sm2TiGenerator><Name>真实字幕</Name><PrettyType>Subtitle</PrettyType><Start>{i.GetStart()}</Start><Duration>{i.GetDuration()}</Duration></Sm2TiGenerator>' for i in self.items if i.row['type']=='subtitle')
        with zipfile.ZipFile(path,'w') as z:z.writestr('SeqContainer/a.xml','<SubtitleTrackVec><Element>'+cues+'</Element></SubtitleTrackVec>')
        return True

class Pool:
    def __init__(self,p):self.project=p;self.root=Folder('Root');self.current=self.root;self.append_count=0;self.fail_at=None;self.bad_at=None;self.photo_frames=None
    def GetRootFolder(self):return self.root
    def GetCurrentFolder(self):return self.current
    def SetCurrentFolder(self,f):self.current=f;return True
    def AddSubFolder(self,p,n):f=Folder(n);p.sub.append(f);return f
    def find(self,path):
        def all(f):
            yield from f.clips
            for s in f.sub:yield from all(s)
        matches=[m for m in all(self.root) if m.GetClipProperty('File Path')==path]
        if matches:return matches[0]
        m=Media(path);self.root.clips.append(m);return m
    def ImportMedia(self,paths):
        result=[Media(p) for p in paths];self.current.clips.extend(result);return result
    def AppendToTimeline(self,rows):
        self.append_count+=1
        if self.fail_at==self.append_count:return []
        result=[]
        for r in rows:
            m=r['mediaPoolItem'];a=r['recordFrame'];n=r['endFrame']-r['startFrame'];kind='video' if r['mediaType']==1 else 'audio'
            if self.photo_frames is not None and (Path(m.GetClipProperty('File Path') or '').suffix.lower() in ('.jpg','.jpeg','.png','.webp','.bmp','.tif','.tiff','.heic','.heif') or m.GetClipProperty('Type') in ('Still','Photo','静帧','照片')):
                n=self.photo_frames
            item=Item(dict(id=uid(),name=m.GetClipProperty('Clip Name'),type=kind,track=r['trackIndex'],start=a,end=a+n,source_start=r['startFrame'],source_end=r['endFrame'],enabled=True,props={}),m)
            self.project.current.items.append(item);result.append(item)
        return [] if self.bad_at==self.append_count else result
    def MoveClips(self,clips,folder):
        def remove(f):
            f.clips=[c for c in f.clips if c not in clips]
            for sub in f.sub:remove(sub)
        remove(self.root);folder.clips.extend(clips);return True
    def CreateEmptyTimeline(self,name):
        val=snapshot(name);val['items']=[];val['tracks']=[t for t in val['tracks'] if t['index']==1 and t['type']!='subtitle']
        t=Timeline(self.project,name,val);self.project.timelines.append(t);self.current.clips.append(t.media);return t
    def DeleteTimelines(self,ts):
        for t in ts:self.project.timelines.remove(t)
        return True

class Project:
    def __init__(self):self.id='project';self.pool=Pool(self);self.timelines=[];self.current=Timeline(self);self.timelines.append(self.current)
    def GetUniqueId(self):return self.id
    def GetName(self):return 'project'
    def GetMediaPool(self):return self.pool
    def GetCurrentTimeline(self):return self.current
    def SetCurrentTimeline(self,t):self.current=t;return True
    def GetTimelineCount(self):return len(self.timelines)
    def GetTimelineByIndex(self,i):return self.timelines[i-1]

class Manager:
    def __init__(self,p):self.p=p;self.current=p;self.db={'DbType':'Disk','DbName':'test'};self.folder='projects/sub';self.saved=0;self.loaded=0;self.wrong_id=False;self.change_folder=False
    def GetCurrentProject(self):return self.current
    def GetCurrentDatabase(self):return deepcopy(self.db)
    def GetCurrentFolder(self):return self.folder
    def SaveProject(self):self.saved+=1;return True
    def CloseProject(self,p):self.current=None;self.folder='changed' if self.change_folder else self.folder;return True
    def LoadProject(self,name):
        assert name==self.p.GetName()
        self.loaded+=1;self.current=self.p
        if self.wrong_id:self.p.id='other-project'
        return self.p

class Resolve:
    EXPORT_DRT=1
    def __init__(self):self.project=Project();self.manager=Manager(self.project)
    def GetProjectManager(self):return self.manager
    def GetVersionString(self):return 'fake offline'
