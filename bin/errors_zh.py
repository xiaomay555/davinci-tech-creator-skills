"""Turn a failure into a Chinese reason and next step for non-technical users."""
import re

# (pattern in the raw error text, reason, next step); first match wins.
KNOWN = [
    (r'Timed out while waiting|timed out|TimeoutError', '达芬奇接口长时间没有响应', '先在达芬奇里保存，退出并重新打开达芬奇后再试'),
    (r'Connection refused|Could not connect|scriptapp|Resolve is not running', '连不上达芬奇', '确认达芬奇已经打开，再试一次'),
    (r'Resolve MCP installation not found', '这台 Mac 没有安装达芬奇 MCP', '让 Codex 运行 bash bin/setup_mac.sh'),
    (r'Node\.js is required', '这台 Mac 没有安装 Node.js', '让 Codex 运行 bash bin/setup_mac.sh'),
    (r'subtitle dependencies unavailable', '字幕组件缺失', '让 Codex 运行 bash bin/setup_mac.sh'),
    (r'Open project .* first; found', '达芬奇当前打开的不是这个片子的工程', '在达芬奇里保存并关闭当前工程后再试'),
    (r'Resolve is rendering', '达芬奇正在渲染', '等渲染完成后再保存'),
    (r'Another delivery operation is running', '另一个“保存到达芬奇”还在进行', '等它完成后再试'),
    (r'Bound existing project is missing', '找不到绑定的达芬奇工程', '确认达芬奇里的工程名没被改过；改过就把新名字告诉 Codex'),
    (r'Existing source binding differs|Media-pool source does not match', '达芬奇里的素材和原片路径对不上', '原片可能被移动或改名，把现在的原片位置告诉 Codex'),
    (r'Cut partially overlaps a spoken word|Cannot separate two spoken words', '有一处删除切到了半个字', '在网页里把整个词一起选中再删'),
    (r'Retained words lack reliable alignment', '有保留的字没有可靠的时间点', '让 Codex 检查对齐结果（审核/align.log）'),
    (r'Cannot repair alignment|Alignment repair did not converge', '有字的时间点无法修复', '让 Codex 查看对齐结果，必要时重新运行对齐'),
    (r'Review already exists', '这个片子已经生成过审核，不会覆盖你的修改', '直接打开审核网页继续修改；真要从头来，用一个新的项目文件夹重新开始'),
    (r'No reviewed speech remains|Cannot delete every spoken word', '所有口播都被删掉了', '至少保留一些文字'),
    (r'Package predates|prepare again|changed after preparation', '交付包已过期', '再点一次“保存到达芬奇”'),
    (r'No space left|Permission denied|Read-only file system', '磁盘空间不足或没有写入权限', '检查 NAS 剩余空间和这个文件夹的权限'),
    (r'No such file or directory', '找不到需要的文件', '确认 NAS 已连接、原片和项目文件夹没有被移动'),
    (r'differs|inconsistent|mismatch|Frozen delivery file changed', '交付后的读回核对没通过，本次没有标记成功', '再保存一次；还不行就把下面的信息发给 Codex'),
]


def last_error_line(text):
    """The most specific error line: nested errors (inside "|" exception groups) count, wrapper lines do not."""
    lines = [re.sub(r'^[\s|+]*', '', line).strip() for line in text.strip().splitlines()]
    errors = [line for line in lines if re.match(r'^[\w.]*(Error|Exception|SourceChanged|StateConflict)\b', line)
              and not re.match(r'^(subprocess\.CalledProcessError|ExceptionGroup|BaseExceptionGroup)\b', line)]
    chinese = [line for line in errors if re.search(r'[\u4e00-\u9fff]', line)]
    if chinese:
        return chinese[-1]
    if errors:
        return errors[-1]
    return next((line for line in reversed(lines) if line), '程序意外退出')


def explain(text):
    """Return (reason, next step, raw line)."""
    raw = last_error_line(text)
    message = raw.split(':', 1)[1].strip() if re.match(r'^[\w.]+:', raw) else raw
    if re.search(r'[一-鿿]', message):
        return message, '按上面的提示操作；不清楚就把这段发给 Codex', raw
    for pattern, reason, step in KNOWN:
        if re.search(pattern, text):
            return reason, step, raw
    return '程序遇到了没有预料到的问题', '把下面的原始信息和日志位置发给 Codex', raw


def report(text, log=None):
    reason, step, raw = explain(text)
    lines = [f'出错了：{reason}', f'下一步：{step}', f'原始信息：{raw[:300]}']
    if log:
        lines.append(f'完整日志：{log}')
    return '\n'.join(lines)
