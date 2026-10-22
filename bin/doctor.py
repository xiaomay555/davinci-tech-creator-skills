"""Read-only client check for the NAS-shared editing workspace."""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from doubao_credentials import verified_for_current_key
from editing_preflight import alignment_runtime


def working_ffmpeg():
    executable = shutil.which('ffmpeg')
    if not executable:
        return None, False, '本机没有 ffmpeg'
    try:
        result = subprocess.run([executable, '-hide_banner', '-encoders'], capture_output=True,
                                text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return executable, False, 'ffmpeg 无法运行或检查超时'
    if result.returncode:
        return executable, False, 'ffmpeg 运行失败'
    if 'libmp3lame' not in result.stdout:
        return executable, False, 'ffmpeg 缺少提音所需的 libmp3lame 编码器'
    return executable, True, None


def collect_skill_checks(root, codex_home):
    skills = {}
    for name in ('ai-edit', 'a-roll', 'b-roll'):
        source = root / 'skills' / name
        installed = codex_home / 'skills' / name
        present = (source / 'SKILL.md').is_file()
        linked = installed.is_symlink() and installed.resolve() == source.resolve()
        skills[name] = {'skill_source_present': present, 'skill_installed_as_nas_link': linked,
                        'shared_skill_ready': present and linked}
    return {
        'skills': skills,
        'skill_source_present': all(s['skill_source_present'] for s in skills.values()),
        'skill_installed_as_nas_link': all(s['skill_installed_as_nas_link'] for s in skills.values()),
        'shared_skill_ready': all(s['shared_skill_ready'] for s in skills.values()),
    }


def ffmpeg_has_zscale(executable):
    if not executable:
        return False
    try:
        result = subprocess.run([executable, '-hide_banner', '-filters'], capture_output=True,
                                text=True, timeout=10)
        return result.returncode == 0 and any(
            len(row.split()) > 1 and row.split()[1] == 'zscale'
            for row in result.stdout.splitlines())
    except (OSError, subprocess.TimeoutExpired):
        return False


def collect_checks(align_python=None):
    root = Path(__file__).resolve().parents[1]
    codex_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    mcp_root = Path(os.environ.get('DAVINCI_RESOLVE_MCP_ROOT', str(codex_home / 'tools' / 'davinci-resolve-mcp')))
    key_file = root / 'secrets' / 'doubao_api_key'
    key_present = False
    key_verified = False
    key_restricted = False
    key_access_error = None
    try:
        if not os.access(key_file.parent, os.R_OK | os.X_OK):
            key_access_error = 'NAS secrets 目录不可读；请检查 SMB 账户权限'
        elif key_file.is_file():
            if not os.access(key_file, os.R_OK):
                key_access_error = 'NAS 豆包 Key 不可读；请检查 SMB 账户权限'
            else:
                key_present = key_file.stat().st_size > 0
                key_restricted = bool(key_present and not stat.S_IMODE(key_file.stat().st_mode) & 0o077)
                key_verified = verified_for_current_key('volc.bigasr.auc_turbo') if key_present else False
    except (OSError, RuntimeError):
        key_access_error = 'NAS 豆包 Key 检查失败；请检查文件与 SMB 权限'
    ffmpeg, ffmpeg_ready, ffmpeg_error = working_ffmpeg()
    subtitle_ready=False;subtitle_error=None
    try:
        sys.path.insert(0,str(root/'engine/src'))
        from run_mvp import node_runtime
        node_runtime(mcp_root)
        subtitle_ready=True
    except (OSError,RuntimeError) as error:
        subtitle_error=str(error)
    transcription_entry = root / 'bin' / 'transcribe_video.py'
    transcription_ready = bool(transcription_entry.is_file() and ffmpeg_ready and key_present and key_verified and not key_access_error)
    resolve_apps = [Path('/Applications/DaVinci Resolve/DaVinci Resolve.app'), Path('/Applications/DaVinci Resolve.app')]
    resolve_app = next((path for path in resolve_apps if path.is_dir()), None)
    checks = {
        **collect_fx_checks(root, codex_home),
        **alignment_runtime(align_python),
        'nas_workspace': str(root),
        # Someone is changing the shared program right now; paid or Resolve runs may hit half-edited code.
        'shared_code_being_modified': (root / '正在修改中.txt').read_text(encoding='utf-8').strip() if (root / '正在修改中.txt').is_file() else None,
        'shared_version': (root / 'VERSION').read_text().strip() if (root / 'VERSION').is_file() else None,
        **collect_skill_checks(root, codex_home),
        'python3': sys.executable,
        'node': shutil.which('node'),
        'subtitle_dependencies_ready': subtitle_ready,
        'subtitle_dependencies_error': subtitle_error,
        'ffmpeg': ffmpeg,
        'ffmpeg_can_extract_mp3': ffmpeg_ready,
        'ffmpeg_has_zscale': ffmpeg_has_zscale(ffmpeg),
        'ffmpeg_error': ffmpeg_error,
        'ffprobe': shutil.which('ffprobe'),
        'codex': shutil.which('codex'),
        'resolve_app': str(resolve_app) if resolve_app else None,
        'resolve_mcp_server': str(mcp_root / 'src' / 'server.py') if (mcp_root / 'src' / 'server.py').is_file() else None,
        'resolve_mcp_python': str(mcp_root / 'venv' / 'bin' / 'python') if (mcp_root / 'venv' / 'bin' / 'python').is_file() else None,
        'doubao_direct_asr_entry': str(root / 'bin' / 'doubao_direct_asr.py') if (root / 'bin' / 'doubao_direct_asr.py').is_file() else None,
        'video_transcription_entry': str(root / 'bin' / 'transcribe_video.py') if (root / 'bin' / 'transcribe_video.py').is_file() else None,
        'shared_doubao_key_present': key_present,
        'shared_doubao_key_permissions_restricted': key_restricted,
        'shared_doubao_key_access_error': key_access_error,
        'shared_doubao_key_api_verified': key_verified,
        'video_transcription_ready': transcription_ready,
        'resolve_mcp_connection_verified': False,
        'parameterized_editing_entry': str(root/'bin/edit_project.py') if (root/'bin/edit_project.py').is_file() else None,
        'verified_editing_fps': [[30000,1001]],
    }
    checks['missing'] = missing_items(checks)
    checks['end_to_end_ready'] = not checks['missing']
    return checks


def collect_fx_checks(root, codex_home):
    """Separate FX fields preserve the existing three editing-skill checks."""
    config = json.loads((root / 'engine/config/fx_styles.json').read_text())
    font_dir = Path.home() / 'Library/Fonts'
    fonts = {name: {'filename': filename, 'installed': (font_dir / filename).is_file()}
             for name, filename in config['font_files'].items()}
    sfx = json.loads((root / 'engine/config/sfx_favorites.json').read_text())
    sfx_dir = Path(os.environ.get('AI_EDIT_SFX_DIR', sfx['root']))
    try:
        import fontTools
        fonttools = True
    except ImportError:
        fonttools = False
    source = root / 'skills/fx'
    link = codex_home / 'skills/fx'
    sys.path.insert(0,str(root))
    from engine.fx.matte import environment_check
    matting_ready,matting_error=environment_check(codex_home/'tools/ai-edit-matting')
    return {'fx_matting_ready':matting_ready,'fx_matting_error':matting_error,'fx_fonts': fonts, 'fx_fonttools_importable': fonttools,
            'fx_sfx_directory': str(sfx_dir),
            'fx_sfx_directory_accessible': sfx_dir.is_dir() and os.access(sfx_dir, os.R_OK | os.X_OK),
            'fx_skill_ready': (source / 'SKILL.md').is_file() and link.is_symlink() and link.resolve() == source.resolve()}


def missing_items(c):
    """Chinese list of what this Mac still lacks, each with the next step."""
    rows = [
        (s['shared_skill_ready'], f'Skill {name} ' + ('源文件缺失' if not s['skill_source_present'] else '未正确连接')
         + '：运行 bash bin/setup_mac.sh') for name, s in c['skills'].items()
    ] + [
        (c['ffmpeg_can_extract_mp3'], 'ffmpeg 不可用：运行 bash bin/setup_mac.sh'),
        (c['shared_doubao_key_present'] and not c['shared_doubao_key_access_error'], '读不到共用豆包 Key：确认 NAS 已连接且有权限'),
        (c['shared_doubao_key_api_verified'], '豆包 Key 未验证：管理员按 README 用短音频测试一次'),
        (c.get('alignment_dependencies_ready'), '声学对齐环境缺失：运行 bash bin/setup_mac.sh'),
        (c['subtitle_dependencies_ready'], '字幕组件缺失：运行 bash bin/setup_mac.sh'),
        (c['resolve_app'], '没有安装 DaVinci Resolve：本人从官网安装'),
        (c['resolve_mcp_server'] and c['resolve_mcp_python'], '达芬奇 MCP 缺失：运行 bash bin/setup_mac.sh'),
    ]
    if 'ffmpeg_has_zscale' in c:
        rows.append((c['ffmpeg_has_zscale'], 'ffmpeg 缺 zscale（B-roll HDR 转换需要）：运行 bash bin/setup_mac.sh 安装/切换 ffmpeg-full'))
    rows += [(entry['installed'], f'花字字体「{name}」缺失：连接 NAS 后运行 bash bin/setup_mac.sh')
             for name, entry in c.get('fx_fonts', {}).items()]
    for key, message in (
        ('fx_matting_ready', '自动抠人环境或模型缺失：运行 bash bin/setup_mac.sh'),
        ('fx_fonttools_importable', 'fontTools 缺失：运行 python3 -m pip install --user fonttools'),
        ('fx_sfx_directory_accessible', '音效库不可访问：挂载素材 NAS，或设置 AI_EDIT_SFX_DIR 指向音效目录'),
        ('fx_skill_ready', 'Skill fx 未正确连接：运行 bash bin/setup_mac.sh'),
    ):
        if key in c:
            rows.append((c[key], message))
    return [message for ready, message in rows if not ready]


def summary(c):
    lines = [f"共享程序版本 {c['shared_version']}"]
    if c.get('shared_code_being_modified'):
        lines.append('⚠ 有人正在修改共享程序，先别开始识别或交付：' + c['shared_code_being_modified'].replace('\n', '；'))
    lines += [f"Skill {name}：" + ('已就绪' if s['shared_skill_ready'] else '未就绪')
              for name, s in c['skills'].items()]
    if 'ffmpeg_has_zscale' in c:
        lines.append('ffmpeg HDR 转换（zscale）：' + ('已就绪' if c['ffmpeg_has_zscale'] else '未就绪'))
    if 'fx_skill_ready' in c:
        lines.append('Skill fx：' + ('已就绪' if c['fx_skill_ready'] else '未就绪'))
        lines += [f'花字字体「{name}」：' + ('已安装' if entry['installed'] else '缺失')
                  for name, entry in c['fx_fonts'].items()]
        lines.append('花字字形检查（fontTools）：' + ('已就绪' if c['fx_fonttools_importable'] else '未就绪'))
        lines.append('音效素材库：' + ('可访问' if c['fx_sfx_directory_accessible'] else '不可访问'))
        lines.append('自动抠人环境与模型（RVM）：' + ('已就绪' if c['fx_matting_ready'] else '未就绪'))
    lines.append('可以转文字：' + ('是' if c['video_transcription_ready'] else '否'))
    lines.append('可以完整剪辑并放进达芬奇：' + ('是（首次连接达芬奇时以实际交付为准）' if c['end_to_end_ready'] else '否'))
    lines += ['还缺：' + item for item in c['missing']]
    return '\n'.join(lines)


if __name__ == "__main__":
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--align-python',type=Path)
    parser.add_argument('--summary',action='store_true',help='只输出中文清单')
    args=parser.parse_args()
    checks=collect_checks(args.align_python)
    print(summary(checks) if args.summary else json.dumps(checks, ensure_ascii=False, indent=2))
