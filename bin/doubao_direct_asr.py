#!/usr/bin/env python3
"""Local-only entry for Doubao recording ASR turbo's documented audio.data API."""

import argparse
import base64
import hashlib
import html
import json
import secrets
import threading
import uuid
import os
import tempfile
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from urllib.request import Request, urlopen
from doubao_credentials import load_key, mark_verified, validate_key, verified_for_current_key


API = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
RESOURCE_ID = "volc.bigasr.auc_turbo"
MAX_AUDIO_BYTES = 100 * 1024 * 1024
FORMATS = {".wav": "wav", ".mp3": "mp3", ".ogg": "ogg", ".opus": "ogg"}
OPTIONS = {
    "model_name": "bigmodel",
    "enable_punc": True,
    "enable_ddc": False,
    "enable_itn": False,
    "show_utterances": True,
}


def count_timed_tokens(result):
    if not isinstance(result, dict) or not isinstance(result.get("audio_info"), dict) or not isinstance(result.get("result"), dict):
        raise RuntimeError("豆包响应结构异常；不能用于逐字剪辑")
    duration = result["audio_info"].get("duration")
    transcript = result["result"]
    utterances = transcript.get("utterances", [])
    if type(duration) is not int or duration <= 0:
        raise RuntimeError("豆包未返回有效音频时长；不能用于逐字剪辑")
    if not isinstance(utterances, list) or not utterances:
        raise RuntimeError("豆包未返回逐句结果；不能用于逐字剪辑")
    count = 0
    previous_end = -1
    sentence_text = []
    for utterance in utterances:
        if not isinstance(utterance, dict) or not isinstance(utterance.get("text"), str):
            raise RuntimeError("豆包逐句正文结构异常；不能用于逐字剪辑")
        words = utterance.get("words", [])
        if not isinstance(words, list):
            raise RuntimeError("豆包逐句字词结构异常；不能用于逐字剪辑")
        spoken = "".join(char.casefold() for char in utterance.get("text", "") if char.isalnum())
        sentence_text.append(spoken)
        if any(not isinstance(word, dict) or not isinstance(word.get("text"), str) for word in words):
            raise RuntimeError("豆包字词正文结构异常；不能用于逐字剪辑")
        timed_text = "".join(char.casefold() for word in words for char in word["text"] if char.isalnum())
        if spoken != timed_text:
            raise RuntimeError("豆包逐句文字与逐字时间项不完整对应；不能用于逐字剪辑")
        for word in words:
            if not any(char.isalnum() for char in word.get("text", "")):
                continue  # 空格等格式项可能以 -1/-1 返回，不能用于剪辑。
            start, end = word.get("start_time"), word.get("end_time")
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= duration:
                raise RuntimeError("豆包返回无效字词时间戳；不能用于逐字剪辑")
            if start < previous_end:
                raise RuntimeError("豆包字词时间戳重叠或倒序；不能用于逐字剪辑")
            previous_end = end
            count += 1
        # Some historical responses omit utterance bounds. If present, reject
        # malformed bounds before the preview formatter or a timeline uses them.
        for name in ("start_time", "end_time"):
            if name in utterance and (type(utterance[name]) is not int or not 0 <= utterance[name] <= duration):
                raise RuntimeError("豆包返回无效逐句时间戳；不能用于逐字剪辑")
        if "start_time" in utterance and "end_time" in utterance and utterance["start_time"] > utterance["end_time"]:
            raise RuntimeError("豆包逐句时间戳倒序；不能用于逐字剪辑")
    if "text" in transcript:
        if not isinstance(transcript["text"], str) or "".join(char.casefold() for char in transcript["text"] if char.isalnum()) != "".join(sentence_text):
            raise RuntimeError("豆包全文与逐句结果不完整对应；不能用于逐字剪辑")
    if count == 0:
        raise RuntimeError("豆包未返回可用的字词时间戳；不能用于逐字剪辑")
    return count


def recognize(audio_path, key, timeout=900, expected_audio_sha256=None):
    key = validate_key(key)
    audio_path = Path(audio_path).expanduser().resolve(strict=True)
    audio_format = FORMATS.get(audio_path.suffix.lower())
    if not audio_format:
        raise ValueError("请先把音频转成 WAV、MP3 或 OGG/OPUS")
    before = audio_path.stat()
    size = before.st_size
    if not 0 < size <= MAX_AUDIO_BYTES:
        raise ValueError("音频必须大于 0，且不超过极速版 100 MB 限制")
    audio_bytes = audio_path.read_bytes()
    after = audio_path.stat()
    if len(audio_bytes) != size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("读取期间音频已变化，请确认源文件后重试")
    if expected_audio_sha256 is not None and hashlib.sha256(audio_bytes).hexdigest() != expected_audio_sha256:
        raise ValueError("待上传音频与来源校验不一致；未提交识别，请核对原片及音频")
    payload = {
        "user": {"uid": "local-talking-head-edit"},
        "audio": {"format": audio_format, "data": base64.b64encode(audio_bytes).decode("ascii")},
        "request": OPTIONS,
    }
    request = Request(
        API,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": key,
            "X-Api-Resource-Id": RESOURCE_ID,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            code = response.headers.get("X-Api-Status-Code", "")
            message = response.headers.get("X-Api-Message", "")
            body = response.read()
    except HTTPError as exc:
        raise RuntimeError(f"豆包 HTTP {exc.code}；请检查极速版权限和密钥") from None
    except URLError as exc:
        raise RuntimeError(f"连接豆包失败：{exc.reason}") from None
    if code != "20000000":
        raise RuntimeError(f"豆包拒绝请求：{code or '无状态码'} {message.replace(key, '[已隐藏密钥]')[:160]}")
    result = json.loads(body)
    return result


def save_result(output, result):
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".doubao-result-", dir=output.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(result, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description="本地音频直传豆包录音文件识别极速版；密钥仅用于本次请求")
    parser.add_argument("--audio", required=True, type=Path, help="这台 Mac 可读取的 WAV/MP3/OGG 音频路径")
    parser.add_argument("--output", required=True, type=Path, help="本机输出 JSON 路径，不得在共享能力目录内")
    args = parser.parse_args()
    audio = args.audio.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve()
    shared_root = Path(__file__).resolve().parents[1]
    if output == shared_root or shared_root in output.parents:
        parser.error("转写结果必须保存在个人项目目录，不能写入 NAS 共享能力目录")
    if output.exists():
        parser.error("输出文件已存在；请指定新文件，避免覆盖审核数据")
    if FORMATS.get(audio.suffix.lower()) is None:
        parser.error("音频格式只支持 WAV、MP3、OGG/OPUS；视频请先用 ffmpeg 提取音频")
    if not 0 < audio.stat().st_size <= MAX_AUDIO_BYTES:
        parser.error("音频必须大于 0，且不超过 100 MB")

    shared_key = load_key()
    if shared_key:
        result = recognize(audio, shared_key)
        save_result(output, result)
        word_count = count_timed_tokens(result)
        if not verified_for_current_key(RESOURCE_ID, shared_key):
            try:
                mark_verified(shared_key, RESOURCE_ID)
            except OSError:
                print("识别结果已保存在本机；NAS 验证标记写入失败，请由管理员检查共享权限", file=sys.stderr)
        print(json.dumps({"status": "complete", "output": str(output), "timed_tokens": word_count}, ensure_ascii=False))
        return

    token = secrets.token_urlsafe(32)
    state = {"stage": "waiting_for_key", "audio": audio.name, "output": str(output)}
    guard = threading.Lock()

    def worker(key):
        try:
            result = recognize(audio, key)
            save_result(output, result)
            words = count_timed_tokens(result)
            with guard:
                state.update(stage="complete", words=words)
        except Exception as exc:
            with guard:
                state.update(stage="failed", error=str(exc)[:200])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, content, content_type="application/json", status=200):
            raw = content.encode("utf-8") if isinstance(content, str) else json.dumps(content, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def allowed(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.allowed():
                return self.reply({}, status=403)
            if self.path == "/status":
                with guard:
                    return self.reply(state.copy())
            if self.path != "/":
                return self.reply({}, status=404)
            page = f'''<!doctype html><html lang="zh"><meta charset="utf-8"><title>豆包音频直传</title>
<style>body{{font:17px system-ui;max-width:680px;margin:50px auto;padding:20px;line-height:1.6}}input{{width:95%;padding:12px}}button{{padding:12px;margin-top:16px}}pre{{white-space:pre-wrap}}</style>
<h1>豆包录音识别 · 本机直传</h1><p>音频：{html.escape(audio.name)}</p>
<p>此次调用使用<strong>极速版</strong> <code>{RESOURCE_ID}</code>，需另行开通；标准版剩余额度不保证适用。如果没有极速版赠送额度，提交可能产生按量费用。点击提交后，音频会从这台 Mac 直接传到豆包，不经过 TOS。结果保存在这台 Mac；密钥不写入文件。</p>
<form method="post" action="/start"><input type="hidden" name="csrf" value="{token}">
<input type="password" name="key" required autocomplete="off" placeholder="粘贴豆包语音新版控制台 API Key"><br>
<button>确认使用极速版并上传音频</button></form><pre id="status"></pre>
<script>setInterval(async()=>{{document.querySelector('#status').textContent=JSON.stringify(await(await fetch('/status')).json(),null,2)}},2000)</script></html>'''
            return self.reply(page, "text/html")

        def do_POST(self):
            if not self.allowed() or self.path != "/start":
                return self.reply({}, status=403)
            if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{self.server.server_port}"):
                return self.reply({}, status=403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.reply({}, status=400)
            if not 0 < length <= 8192:
                return self.reply({}, status=413)
            try:
                form = parse_qs(self.rfile.read(length).decode("utf-8"), max_num_fields=8)
            except (UnicodeDecodeError, ValueError):
                return self.reply({}, status=400)
            key = form.get("key", [""])[0].strip()
            if not secrets.compare_digest(form.get("csrf", [""])[0].encode("utf-8"), token.encode("ascii")) or not key:
                return self.reply({}, status=400)
            with guard:
                if state["stage"] != "waiting_for_key":
                    return self.reply({"error": "本次任务已启动"}, status=409)
                state["stage"] = "uploading_and_recognizing"
            threading.Thread(target=worker, args=(key,), daemon=True).start()
            return self.reply('<meta charset="utf-8"><p>已提交。请返回页面查看状态。</p><a href="/">查看状态</a>', "text/html")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(f"本机密钥输入页：http://127.0.0.1:{server.server_port}/", flush=True)
    print("按 Ctrl+C 关闭；音频只在点击提交后上传。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
