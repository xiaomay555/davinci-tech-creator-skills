"""Store the LAN team's Doubao API key outside Skill instructions and project output."""

import argparse
import html
import hashlib
import json
import os
import secrets
import stat
import tempfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs


ROOT = Path(__file__).resolve().parents[1]
KEY_FILE = ROOT / "secrets" / "doubao_api_key"
VERIFIED_FILE = ROOT / "secrets" / "doubao_verified.json"


def validate_key(key):
    key = key.strip()
    if not 20 <= len(key) <= 1024 or not key.isascii() or any(ord(ch) < 33 or ord(ch) == 127 for ch in key):
        raise ValueError("API Key 格式无效")
    return key


def load_key():
    if KEY_FILE.parent.is_symlink() or KEY_FILE.is_symlink():
        raise RuntimeError("密钥路径不能是符号链接")
    if not KEY_FILE.is_file():
        return None
    try:
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        validate_key(key)
    except (ValueError, UnicodeError):
        raise RuntimeError("NAS 豆包密钥文件为空或格式无效") from None
    return key


def save_key(key):
    key = validate_key(key)
    directory = KEY_FILE.parent
    if directory.is_symlink() or KEY_FILE.is_symlink():
        raise RuntimeError("密钥路径不能是符号链接")
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".doubao-key-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(key + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, KEY_FILE)
        os.chmod(KEY_FILE, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if stat.S_IMODE(KEY_FILE.stat().st_mode) & 0o077:
        raise RuntimeError("NAS 未保持密钥文件的受限权限，请检查共享权限")


def mark_verified(key, resource_id):
    payload = {
        "key_sha256": hashlib.sha256(key.encode("utf-8")).hexdigest(),
        "resource_id": resource_id,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    fd, temporary = tempfile.mkstemp(prefix=".doubao-verified-", dir=KEY_FILE.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, VERIFIED_FILE)
        os.chmod(VERIFIED_FILE, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verified_for_current_key(resource_id, key=None):
    # Check the exact key that will be used, even if the NAS key rotates now.
    key = load_key() if key is None else key
    if not key or not VERIFIED_FILE.is_file():
        return False
    try:
        payload = json.loads(VERIFIED_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    return isinstance(payload, dict) and payload.get("resource_id") == resource_id and payload.get("key_sha256") == hashlib.sha256(key.encode("utf-8")).hexdigest()


def serve():
    csrf = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, body, status=200):
            raw = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def allowed(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.allowed() or self.path != "/":
                return self.reply("拒绝访问", 403)
            page = f'''<!doctype html><html lang="zh"><meta charset="utf-8"><title>配置豆包密钥</title>
<body style="font:17px system-ui;max-width:640px;margin:48px auto;line-height:1.6">
<h1>配置 AI 口播剪辑共用豆包 Key</h1>
<p>请使用<strong>豆包语音新版控制台</strong>的 API Key；火山方舟 API Key 调此语音接口会返回 401。</p>
<p>只保存到 NAS 的受限文件 <code>{html.escape(str(KEY_FILE))}</code>。局域网内拥有该目录读取权限的电脑可调用豆包；不要把此目录公开分享。</p>
<form method="post" action="/save"><input type="hidden" name="csrf" value="{csrf}">
<input type="password" name="key" required autocomplete="off" style="width:95%;padding:12px" placeholder="粘贴现有 API Key"><br>
<button style="padding:12px;margin-top:16px">保存到 NAS</button></form></body></html>'''
            return self.reply(page)

        def do_POST(self):
            if not self.allowed() or self.path != "/save":
                return self.reply("拒绝访问", 403)
            if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{self.server.server_port}"):
                return self.reply("拒绝访问", 403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.reply("请求无效", 400)
            if not 0 < length <= 4096:
                return self.reply("请求过大", 413)
            try:
                form = parse_qs(self.rfile.read(length).decode("utf-8"), max_num_fields=8)
            except (UnicodeDecodeError, ValueError):
                return self.reply("请求无效", 400)
            if not secrets.compare_digest(form.get("csrf", [""])[0].encode("utf-8"), csrf.encode("ascii")):
                return self.reply("请求无效", 403)
            try:
                save_key(form.get("key", [""])[0])
            except (ValueError, RuntimeError) as exc:
                return self.reply(f"<p>{html.escape(str(exc))}</p>", 400)
            except OSError:
                return self.reply("密钥保存失败，请检查 NAS 连接和共享目录写入权限", 500)
            self.reply("<meta charset='utf-8'><p>密钥已保存到 NAS。可以关闭此页。</p>")
            self.server.shutdown()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(f"本机密钥配置页：http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="在 NAS 受限文件中配置豆包 API Key；不会打印密钥")
    parser.add_argument("--from-stdin", action="store_true", help="从标准输入读取并保存；不会回显")
    args = parser.parse_args()
    if args.from_stdin:
        import sys
        save_key(sys.stdin.read())
        print("豆包密钥已保存到 NAS。")
    else:
        serve()
