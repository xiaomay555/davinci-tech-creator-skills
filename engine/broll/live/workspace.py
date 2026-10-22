"""Work-directory inference, shared ledger registry, and local SDK write lock."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import json
import os
from .ledger import save_ledger
from ._util import digest


def project_work_dir(project_dir):
    """Prefer existing work, then the project's AI-material folder; never move it."""
    root = Path(project_dir).expanduser().absolute()
    old = root/'B-roll_AI'
    if old.exists():
        return old
    for name in ('3.7AI素材', '3.7 AI素材'):
        ai = root/'3.视频素材'/name
        if (ai/'B-roll_AI'/root.name).exists():
            return ai/'B-roll_AI'/root.name
    for name in ('3.7AI素材', '3.7 AI素材'):
        ai = root/'3.视频素材'/name
        if (ai/'B-roll_AI').exists():
            return ai/'B-roll_AI'
    for name in ('3.7AI素材', '3.7 AI素材'):
        ai = root/'3.视频素材'/name
        if ai.is_dir():
            return ai/'B-roll_AI'/root.name
    return old


def work_project_dir(work):
    work = Path(work)
    if work.parent.name=='B-roll_AI' and work.parent.parent.name in ('3.7AI素材','3.7 AI素材') and work.parent.parent.parent.name=='3.视频素材':
        return work.parent.parent.parent.parent
    if work.parent.name in ('3.7AI素材', '3.7 AI素材') and work.parent.parent.name == '3.视频素材':
        return work.parent.parent.parent
    return work.parent


def infer_work_dir(model, project_dir=None):
    if project_dir:
        return project_work_dir(project_dir)
    paths = {Path(m['media_path']) for m in model['items'] if m['type']=='video' and m['track']==1 and m.get('media_path')}
    if not paths or any(not p.is_absolute() for p in paths):
        raise ValueError('无法从V1推出项目目录；下一步：请确认一次 --project-dir')
    # Recognized production folders; otherwise all V1 sources must share a parent.
    markers = ('视频素材','原片','a-roll','aroll','口播','拍摄')
    roots = set()
    for p in paths:
        # Prefer production folders to nested categories such as 3.2音乐素材.
        marker = next((a for a in p.parents if any(x in a.name.casefold() for x in markers)),None)
        if marker is None:marker=next((a for a in p.parents if '素材' in a.name),None)
        parent=marker.parent if marker is not None else p.parent
        roots.add(parent)
    if len(roots)!=1:
        raise ValueError('V1源片分属不同目录，无法唯一推出项目；下一步：请确认一次 --project-dir')
    return project_work_dir(roots.pop())


@contextmanager
def write_lock(path=None):
    path = Path(path or Path.home()/'.cache/ai-talking-head-edit/resolve-write.lock')
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+') as handle:
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('另一处正在写达芬奇，请等待后重试') from exc
        try:
            handle.seek(0);handle.truncate();handle.write(str(os.getpid()));handle.flush()
            yield
        finally:
            fcntl.flock(handle,fcntl.LOCK_UN)
    # Keep the inode: deleting a flock file would allow two separate locks.


def register(index_path, ledger, work_dir):
    if not index_path:
        raise ValueError('请配置 NAS 台账索引路径 ledger_index')
    path = Path(index_path)
    with write_lock(path.with_suffix('.lock')):
        data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'version':3,'entries':{}}
        work = Path(work_dir).absolute()
        parts = work.parts
        volume = parts[2] if len(parts)>2 and parts[1]=='Volumes' else ''
        relative = str(Path(*parts[3:])) if volume else str(work)
        key = digest({k:ledger.get(k) for k in ('project','database','timeline','lineage')})
        data['entries'][key] = {k:ledger.get(k) for k in ('project','database','timeline','lineage')} | {'volume':volume,'work_dir':relative,'learned':ledger.get('last_learned')}
        save_ledger(path,data)
    return key
