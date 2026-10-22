#!/usr/bin/env python3
"""Per-video editing steps. The Codex agent supplies literal semantic selections."""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import time
import subprocess
import sys
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
ENGINE=ROOT/'engine'
sys.path.insert(0,str(ENGINE/'src'))
from probe_media import probe
from editing_preflight import alignment_runtime
from source_guard import verify_source,single_audio

@contextmanager
def delivery_lock(run):
    """Hold the project's delivery lock; retry briefly because the page and clean probe it for an instant."""
    from prepare_mvp_delivery import exclusive_lock
    for attempt in range(10):
        lock=exclusive_lock(run/'.delivering.lock','这个项目正在保存到达芬奇，请等它完成')
        try:lock.__enter__()
        except RuntimeError:
            if attempt==9:raise
            time.sleep(0.3);continue
        try:yield
        finally:lock.__exit__(None,None,None)
        return


def deliver_preflight(run):
    from run_mvp import resolve_mcp_runtime,node_runtime
    root,python,_=resolve_mcp_runtime()
    node_runtime(root)
    verify_source(json.loads((run/'project_runtime.json').read_text()),run,sampled=True,audio=True)
    return python

class StepFailed(Exception):
    def __init__(self,details):super().__init__(details);self.details=details


def execute(script,env,python=sys.executable,*extra):
    # Progress stays on screen; error output is kept so it can be explained in Chinese.
    process=subprocess.run([str(python),str(ENGINE/'src'/script),*map(str,extra)],env=env,stderr=subprocess.PIPE,text=True)
    if process.stderr and not process.returncode:sys.stderr.write(process.stderr)
    if process.returncode:raise StepFailed(process.stderr or f'{script} 退出码 {process.returncode}')


def run_self(command):
    # The child already printed its own Chinese explanation.
    code=subprocess.run(command).returncode
    if code:raise SystemExit(code)

def service_info(run):
    """The running review service of this project on this Mac, or None."""
    try:info=json.loads((run/'审核网址.txt').read_text())
    except (OSError,ValueError):return None
    if info.get('host')!=socket.gethostname():return {**info,'other_host':True}
    try:os.kill(info['pid'],0)
    except (OSError,KeyError,TypeError):return None
    command=subprocess.run(['ps','-p',str(info['pid']),'-o','command='],capture_output=True,text=True).stdout
    return info if 'review_v2_server.py' in command and str(run) in command else None


def free_port(start=18443):
    for port in range(start,start+200):
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1',port)):return port
    raise SystemExit('找不到空闲端口（18443–18642 都被占用）')


def serve(run,env,args):
    info=service_info(run)
    if info and info.get('other_host'):
        raise SystemExit(f"这个项目的审核网页正在另一台 Mac（{info['host']}）上运行；请在那台 Mac 上使用，或先在那台停止。")
    if not info:
        env['AI_EDIT_PORT']=str(args.port or free_port())
        (run/'审核网址.txt').unlink(missing_ok=True)
        log=(run/'review_service.log').open('a')
        # The project path on the command line lets stop/status recognise this project's page process.
        process=subprocess.Popen([sys.executable,str(ENGINE/'src'/'review_v2_server.py'),str(run)],env=env,stdout=log,stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL,start_new_session=True)
        deadline=time.time()+120
        while not (info:=service_info(run)) or info.get('pid')!=process.pid:
            if process.poll() is not None or time.time()>deadline:
                tail=(run/'review_service.log').read_text(errors='replace').strip().splitlines()[-3:]
                raise SystemExit('审核网页没有启动成功：'+(tail[-1] if tail else '原因未知')+f"\n完整日志：{run/'review_service.log'}")
            time.sleep(0.5)
    if not args.no_open:subprocess.run(['open',info['url']],check=False)
    print(f"审核网页：{info['url']}（共享程序版本 {info.get('version')}）")


def stop(run):
    info=service_info(run)
    if not info:print('这个项目没有正在运行的审核网页。');return
    if info.get('other_host'):raise SystemExit(f"审核网页在另一台 Mac（{info['host']}）上运行，需在那台停止。")
    os.kill(info['pid'],signal.SIGTERM)
    for _ in range(40):
        if not service_info(run):break
        time.sleep(0.25)
    (run/'审核网址.txt').unlink(missing_ok=True)
    print('审核网页已停止。')


def self_command(action,folder,args,*extra):
    command=[sys.executable,str(Path(__file__).resolve()),action,'--project-dir',str(folder),*map(str,extra)]
    if args.align_python and action in ('init','align'):command+=['--align-python',str(args.align_python)]
    return command


def prepare(folder,run,args):
    """Transcribe, then align in the background while the agent chooses retakes."""
    if not (run/'project_runtime.json').exists():
        run_self(self_command('init',folder,args,'--video',args.video.expanduser().resolve(strict=True)))
    if alignment_running(run):
        print('声学对齐已在后台进行；继续写 review_decisions.json，写好后运行 finish。');return
    if (run/'character_alignment.json').exists():
        print('识别和对齐都已完成；请通读 审核/sentence_units.json 写 review_decisions.json，然后运行 finish。');return
    with (run/'align.log').open('a') as log:
        process=subprocess.Popen(self_command('align',folder,args),stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True)
    (run/'对齐进程.txt').write_text(json.dumps({'pid':process.pid,'host':socket.gethostname()}))
    print('识别完成，声学对齐已在后台进行（日志 审核/align.log）。现在通读 审核/sentence_units.json 写 review_decisions.json，写好后运行 finish。')


def alignment_running(run):
    """True while this project's background `align` (started by prepare on this Mac) is still running."""
    try:info=json.loads((run/'对齐进程.txt').read_text())
    except (OSError,ValueError):return False
    if not isinstance(info,dict):return False
    if info.get('host')!=socket.gethostname():
        raise SystemExit(f"这个项目的声学对齐是在另一台 Mac（{info.get('host')}）上启动的；请在那台 Mac 上继续，或等它完成。")
    status=subprocess.run(['ps','-p',str(info.get('pid')),'-o','stat=,command='],capture_output=True,text=True).stdout.strip()
    return bool(status) and not status.startswith('Z') and 'edit_project.py' in status and ' align ' in status+' ' and str(run.parent) in status


def wait_for_alignment(run):
    while alignment_running(run):
        print('等待后台对齐完成…',flush=True);time.sleep(10)
    (run/'对齐进程.txt').unlink(missing_ok=True)
    if not (run/'character_alignment.json').exists():
        tail=(run/'align.log').read_text(errors='replace').strip().splitlines()[-3:] if (run/'align.log').exists() else []
        raise SystemExit('声学对齐没有完成：'+(tail[-1] if tail else '没有日志')+f"\n日志：{run/'align.log'}；可重新运行 align。")


def finish(folder,run,args):
    """Review + script comparison + open the review page, then print a Chinese summary."""
    if not (run/'review_decisions.json').exists():raise SystemExit('还没有 审核/review_decisions.json；先完成选择再运行 finish。')
    wait_for_alignment(run)
    if not (run/'review_v2_state.json').exists():run_self(self_command('review',folder,args))
    script=args.script or ((run/'文案.txt') if (run/'文案.txt').exists() else None)
    if script:run_self(self_command('script',folder,args,*(['--script',script] if args.script else [])))
    run_self(self_command('proofread',folder,args))
    state=json.loads((run/'review_v2_state.json').read_text())
    pending='subtitle_proofread' in state and not state['subtitle_proofread']
    # Open the page for the user only after proofreading, so they never review stale subtitles.
    if not args.no_serve and not pending:run_self(self_command('serve',folder,args,*(['--no-open'] if args.no_open else [])))
    full=json.loads((run/'review_v2_full.json').read_text())
    lines=[f"初剪完成：保留 {full['seconds']:.1f} 秒。"]
    report=run/'文案对照.json'
    if script and report.exists():
        r=json.loads(report.read_text())
        lines.append(f"文案对照：{len(r['candidates'])} 处与文案不同（校对时一并参考）。")
        if r['possibly_missing']:lines.append('文案里可能没念到：'+'；'.join(r['possibly_missing']))
    uncertain=[r for r in state.get('repeat_edits',{}).get('candidates',[]) if r['status']=='review']
    if uncertain:
        lines.append('重复词已保留，需 AI 语义复核：'+'；'.join(r['before_text']+'（'+r['reason']+'）' for r in uncertain))
    if pending:
        lines.append('下一步：按 审核/字幕校对.json 逐句校对字幕，用 corrections --file … --done 写入并标记完成，再运行 serve 打开审核网页给用户（未标记前不能导入达芬奇）。')
    print('\n'.join(lines))


def cleanable(run):
    """Regenerable files a finished project no longer needs: (path, bytes, why)."""
    def size(path):return sum(f.stat().st_size for f in path.rglob('*') if f.is_file()) if path.is_dir() else path.stat().st_size
    items=[]
    if (run/'audio_asr.wav').exists() and (run/'character_alignment.json').exists():
        items.append((run/'audio_asr.wav','对齐用的音频，需要时会从 转写/source_audio.mp3 重新生成'))
    if (run/'preview_parts').is_dir():items.append((run/'preview_parts','网页预览缓存，打开网页时按需重新生成'))
    # Delivery packages are kept: they hold the pre-import project backup and let later saves recognise
    # hand-edited main lines. They are small (about 1–2 MB each).
    newest=lambda paths,keep:sorted(paths,key=lambda p:p.stat().st_mtime)[:-keep] if len(paths)>keep else []
    items+=[(p,'旧的保存日志') for p in newest(list(run.glob('resolve_sync_*.log')),3)]
    items+=[(p,'较早的审核状态备份（保留最近 20 份）') for p in newest(list((run/'review_state_backups').glob('*.json')) if (run/'review_state_backups').is_dir() else [],20)]
    clips=run.parent/'B-roll片段'
    if clips.is_dir() and not clips.is_symlink():
        referenced=set()
        for timeline in (run/'mvp_deliveries').glob('*/broll_timeline.json'):
            try:
                for item in json.loads(timeline.read_text())['items']:
                    referenced.add(Path(item['media']['path']).resolve())
                    referenced.update(Path(a['media']['path']).resolve() for a in item.get('alternates',[]))
            except (OSError,ValueError,KeyError,TypeError):
                raise ValueError('无法读取交付包的 B-roll 引用，停止清理：'+str(timeline)) from None
        items += [(p,'没有被交付包引用的 B-roll 片段') for p in clips.glob('*.mov') if p.resolve() not in referenced and not p.is_symlink()]
        items += [(p,'B-roll 生成中断留下的临时片段') for p in clips.glob('.clip-*.partial') if not p.is_symlink()]
    return [(path,size(path),why) for path,why in items]


def clean(run,yes):
    from prepare_mvp_delivery import delivering
    if service_info(run) or delivering(run):raise SystemExit('审核网页还开着或正在保存到达芬奇；先停止网页、等保存完成再清理。')
    items=cleanable(run)
    if not items:print('没有可以清理的内容。');return
    for path,size,why in items:print(f'{size/1e6:8.1f} MB  {path.relative_to(run.parent)}  —— {why}')
    print(f'合计 {sum(s for _,s,_ in items)/1e6:.1f} MB。')
    if not yes:print('以上内容都能重新生成，不影响剪辑结果。确认删除请加 --yes 重新运行。');return
    for path,_,_ in items:shutil.rmtree(path) if path.is_dir() else path.unlink()
    print('已清理。')


def default_project_dir(video):
    """Only choose a location; never move an existing editing project."""
    video = Path(video).expanduser().resolve()
    legacy = video.parent / (video.stem + '_AI剪辑')
    if legacy.is_dir():
        return legacy
    for parent in video.parents:
        media = parent / '3.视频素材'
        if media.is_dir():
            for name in ('3.7AI素材', '3.7 AI素材'):
                ai = media / name
                if ai.is_dir():
                    return ai / 'A-roll_AI' / (video.stem + '_AI剪辑')
            break
    return legacy


def main():
    parser=argparse.ArgumentParser(description='新片：初始化、声学对齐、生成审核、文案对照、启动网页、MCP 交付；不渲染')
    parser.add_argument('action',choices=['prepare','finish','init','align','review','script','proofread','corrections','serve','stop','status','deliver','clean','broll-index','broll-plan'])
    parser.add_argument('--project-dir',type=Path,help='项目目录；新项目不填时按素材项目结构选择，其他动作必填')
    parser.add_argument('--broll-dir',type=Path,help='broll-index：素材目录；默认原片旁 <片名>_B-roll')
    parser.add_argument('--descriptions',type=Path,help='broll-index：描述 JSON 列表')
    broll_mode=parser.add_mutually_exclusive_group()
    broll_mode.add_argument('--matches',type=Path,help='broll-plan：匹配 JSON')
    broll_mode.add_argument('--clear',action='store_true',help='broll-plan：清除计划')
    broll_mode.add_argument('--list',dest='broll_list',action='store_true',help='broll-plan：只读列出实际放置')
    broll_mode.add_argument('--use',type=int,choices=range(1,4),help='broll-plan：将候选 1–3 与候选1对调，选中项放 V2，须配 --spot')
    for flag,label in (('skip','跳过'),('restore','恢复'),('confirm','确认'),('audio-on','打开原声'),('audio-off','关闭原声')):
        broll_mode.add_argument('--'+flag,dest=flag.replace('-','_'),action='store_true',help='broll-plan：'+label+'，须配 --spot')
    broll_mode.add_argument('--at',help='broll-plan：只读查看时刻对应的句子和画面')
    broll_mode.add_argument('--face-on',type=int,help='broll-plan：此句露脸')
    broll_mode.add_argument('--face-off',type=int,help='broll-plan：取消此句露脸')
    parser.add_argument('--reason',help='broll-plan：--face-on 的中文理由（≤60字）')
    parser.add_argument('--spot',help='broll-plan：调整的 B-roll ID')
    parser.add_argument('--video',type=Path)
    parser.add_argument('--audio-stream',type=int)
    parser.add_argument('--align-python',type=Path)
    parser.add_argument('--port',type=int,help='serve：指定端口；不填则自动找空闲端口')
    parser.add_argument('--no-open',action='store_true',help='serve：不自动打开浏览器')
    parser.add_argument('--no-serve',action='store_true',help='finish：不启动审核网页')
    parser.add_argument('--yes',action='store_true',help='clean：确认删除')
    parser.add_argument('--done',action='store_true',help='corrections：标记字幕校对完成')
    parser.add_argument('--script',type=Path,help='script：纯文本文案文件')
    parser.add_argument('--file',type=Path,help='corrections：字幕纠错 JSON 列表')
    args=parser.parse_args()
    if args.project_dir is None:
        if args.action not in ('prepare','init') or not args.video:
            parser.error('此动作需要 --project-dir；新项目 prepare/init 可用 --video 自动选择目录')
        args.project_dir=default_project_dir(args.video)
    folder=args.project_dir.expanduser().resolve();run=folder/'审核'
    if folder==ROOT or ROOT in folder.parents:parser.error('项目不能放入共享能力目录')
    for field in ('descriptions','matches'):
        value=getattr(args,field)
        if value is not None:setattr(args,field,value if value.is_absolute() else folder/value)
    env={**os.environ,'AI_EDIT_RUN_DIR':str(run)}
    if args.action=='prepare':
        if not args.video and not (run/'project_runtime.json').exists():parser.error('prepare 需要 --video')
        prepare(folder,run,args)
    elif args.action=='finish':finish(folder,run,args)
    elif args.action=='init':
        if not args.video:parser.error('初始化需要 --video')
        video=args.video.expanduser().resolve(strict=True)
        info=probe(video);track=next(t for t in info['tracks'] if t['handler']=='vide')
        if track.get('fps')!={'num':30000,'den':1001} or not track.get('constant_presentation_duration'):
            parser.error('本版剪切引擎只支持恒定 30000/1001 fps；不强行套用其他帧率')
        if (run/'project_runtime.json').exists():parser.error('项目已初始化；不要覆盖审核状态')
        single_audio(info,args.audio_stream or 0)
        # Video edit lists normally skip one frame of B-frame delay; an audio start offset (phone AAC priming) is rarer.
        audio=next(t for t in info['tracks'] if t['handler']=='soun')
        edits=audio.get('edits') or []
        offset_ms=round(sum(e['media_time'] for e in edits if e['media_time']>0)/audio.get('timescale',1)*1000,1) if edits else 0
        if any(e['media_time']==-1 for e in edits) or offset_ms>1001/30:
            print(f'提示：原片音频带起始偏移（约 {offset_ms} 毫秒或空白开头）。剪完请在达芬奇里听一下口型和声音是否对齐。')
        alignment=alignment_runtime(args.align_python)
        if not alignment['alignment_dependencies_ready']:parser.error(alignment['alignment_error'])
        run.mkdir(parents=True,exist_ok=True,mode=0o700)
        command=[sys.executable,str(ROOT/'bin/transcribe_video.py'),'--video',str(video),'--work-dir',str(folder/'转写')]
        if args.audio_stream is not None:command+=['--audio-stream',str(args.audio_stream)]
        run_self(command)  # transcription prints its own Chinese errors
        manifest=json.loads((folder/'转写/transcription_manifest.json').read_text())
        response=json.loads((folder/'转写/doubao_turbo.json').read_text())
        name=re.sub('[^A-Za-z0-9_-]','_',video.stem).strip('_') or 'Video'
        config={'name':name,'source':str(video),'source_frames':track['sample_count'],'fps':[30000,1001],'resolution':track['display_size'],'audio_edit_offset_ms':offset_ms,
                'audio_stream':manifest['audio_stream'],'media_pool_item_id':None,
                'project':'_mcp_AI口播_'+name+'_'+datetime.now().strftime('%Y%m%d')+'_'+hashlib.sha256(str(folder).encode()).hexdigest()[:6],
                'sections':[{'start':0,'title':'口播全文'}]}
        units=[{'id':i,'text':x['text'],'context_text':x['text'],'start_ms':x['start_time'],'end_ms':x['end_time'],'project_generic':True} for i,x in enumerate(response['result']['utterances'])]
        for filename,data in [('project_runtime.json',config),('sentence_units.json',units),('sentence_boundaries.json',{str(u['id']):{'ms':u['start_ms'],'safe':True} for u in units})]:
            (run/filename).write_text(json.dumps(data,ensure_ascii=False,indent=2))
        (run/'glossary.txt').write_text('保留原始识别；产品名和专业词须对照原声。\n')
    elif args.action=='broll-index':
        if not (run/'project_runtime.json').is_file():parser.error('项目尚未初始化；先运行 init（缺少 审核/project_runtime.json）')
        config=json.loads((run/'project_runtime.json').read_text())
        source=Path(config['source'])
        directory=args.broll_dir or source.with_name(source.stem+'_B-roll')
        sys.path.insert(0,str(ENGINE))
        from broll.index import build_index
        try:build_index(directory,descriptions=args.descriptions)
        except (RuntimeError,OSError,ValueError) as error:raise SystemExit(str(error)) from None
    elif args.action=='broll-plan':
        sys.path.insert(0,str(ENGINE))
        from broll.plan import build_plan
        action='choose' if args.use is not None else next((a for a in ('skip','restore','confirm','audio-on','audio-off') if getattr(args,a.replace('-','_'))),None)
        if bool(args.spot)!=(action is not None):parser.error('调整须同时提供 --spot 和 --use/--skip/--restore/--confirm/--audio-on/--audio-off 中的一项')
        if args.reason is not None and args.face_on is None:parser.error('--reason 须配 --face-on')
        try:build_plan(folder,broll_dir=args.broll_dir,matches=args.matches,clear=args.clear,action=action,spot_id=args.spot,candidate=args.use-1 if args.use is not None else None,list_only=args.broll_list,at=args.at,face_on=args.face_on,face_off=args.face_off,reason=args.reason)
        except (RuntimeError,OSError,ValueError) as error:raise SystemExit(str(error)) from None
    elif args.action=='align':
        alignment=alignment_runtime(args.align_python)
        if not alignment['alignment_dependencies_ready']:parser.error(alignment['alignment_error'])
        python=Path(alignment['alignment_python'])
        verify_source(json.loads((run/'project_runtime.json').read_text()),run)
        wav=run/'audio_asr.wav'
        if not wav.exists():subprocess.run([shutil.which('ffmpeg'),'-hide_banner','-loglevel','error','-nostdin','-n','-i',str(folder/'转写/source_audio.mp3'),'-ac','1','-ar','16000','-c:a','pcm_s16le',str(wav)],check=True)
        execute('align_local.py',env,python)
    elif args.action=='review':execute('prepare_new_review.py',env)
    elif args.action=='script':execute('script_check.py',env,sys.executable,*(['--script',args.script.expanduser().resolve(strict=True)] if args.script else []))
    elif args.action=='corrections':
        if not args.file and not args.done:parser.error('corrections 需要 --file，或用 --done 标记校对完成')
        extra=(['--corrections',args.file.expanduser().resolve(strict=True)] if args.file else [])+(['--done'] if args.done else [])
        execute('script_check.py',env,sys.executable,*extra)
    elif args.action=='proofread':execute('script_check.py',env,sys.executable,'--sheet')
    elif args.action=='serve':serve(run,env,args)
    elif args.action=='stop':stop(run)
    elif args.action=='clean':clean(run,args.yes)
    elif args.action=='status':
        info=service_info(run)
        print('没有运行' if not info else (f"在另一台 Mac（{info['host']}）上运行" if info.get('other_host') else f"运行中：{info['url']}，版本 {info.get('version')}，启动于 {info.get('started')}"))
    else:
        from prepare_mvp_delivery import exclusive_lock
        # Resolve has one active project per local app: serialize all projects.
        # Refuse unproofread subtitles before anything touches Resolve.
        execute('script_check.py',env,sys.executable,'--check')
        with exclusive_lock(Path.home()/'.cache/ai-talking-head-edit/resolve-delivery.lock'),\
             delivery_lock(run):
            python=deliver_preflight(run)
            execute('setup_new_resolve.py',env,python)
            execute('run_mvp.py',env)

def explain_failure(details):
    import errors_zh
    try:
        project=Path(sys.argv[sys.argv.index('--project-dir')+1]).expanduser().resolve()/'审核'
        log=project/'错误日志.txt' if project.is_dir() else None
    except (ValueError,IndexError):log=None
    if log:
        with log.open('a',encoding='utf-8') as stream:stream.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} {' '.join(sys.argv[1:2])}\n{details}\n")
    print(errors_zh.report(details,log),file=sys.stderr)
    raise SystemExit(1)


if __name__=='__main__':
    try:main()
    except StepFailed as failure:explain_failure(failure.details)
    except (KeyboardInterrupt,SystemExit):raise
    except Exception:
        import traceback;explain_failure(traceback.format_exc())
