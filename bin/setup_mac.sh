#!/bin/bash
# 新 Mac 一键准备 AI 口播剪辑环境。每一步先检查，已就绪就跳过；不碰视频、项目和达芬奇工程。
#   bash bin/setup_mac.sh            # 安装缺的部分（默认用国内镜像）
#   bash bin/setup_mac.sh --check    # 只检查，不安装
#   bash bin/setup_mac.sh --no-mirror
set -u
root="$(cd "$(dirname "$0")/.." && pwd -P)"
tools="${CODEX_HOME:-$HOME/.codex}/tools"
align="$tools/ai-edit-align"
mcp="$tools/davinci-resolve-mcp"
mcp_commit=6337d8c
check=0; mirror=1
for arg in "$@"; do
  case "$arg" in --check) check=1;; --no-mirror) mirror=0;; *) echo "未知参数：$arg"; exit 2;; esac
done
pip_index=(); npm_registry=(); hf_endpoint=()
if [ $mirror -eq 1 ]; then
  pip_index=(-i https://pypi.tuna.tsinghua.edu.cn/simple)
  npm_registry=(--registry https://registry.npmmirror.com)
  hf_endpoint=(env HF_ENDPOINT=https://hf-mirror.com)
fi
missing=()
ok() { echo "  ✓ $1"; }
need() { echo "  ✗ $1"; missing+=("$1"); }
run() { if [ $check -eq 1 ]; then need "$1"; return 1; fi; echo "  → 安装：$1"; shift; "$@" || { echo "  ✗ 安装失败：$1"; exit 1; }; }

echo "== 1. 基础工具"
command -v git >/dev/null && command -v python3 >/dev/null && ok "git / python3" || { need "Xcode 命令行工具（终端运行 xcode-select --install，需要输入密码，请本人操作）"; }
if ! command -v brew >/dev/null; then
  need "Homebrew（需要输入密码，请本人在终端按 https://brew.sh 的一行命令安装，然后重新运行本脚本）"
  echo; echo "请先装好上面缺的部分。"; exit 1
fi
ok "Homebrew"
ffmpeg_has_zscale() { ffmpeg -hide_banner -filters 2>/dev/null | grep -Eq '[[:space:]]zscale[[:space:]]'; }
if command -v ffmpeg >/dev/null && ffmpeg_has_zscale; then ok 'ffmpeg（含 zscale，支持 HDR 转换）'
elif [ "$check" -eq 1 ]; then need 'ffmpeg 缺失或缺 zscale：运行 bash bin/setup_mac.sh 安装/切换 ffmpeg-full'
else
  run 'ffmpeg-full（含 zscale）' brew install ffmpeg-full
  # 普通 ffmpeg 新版不含 zimg；先解除链接，再链接完整版本。
  if brew list --formula ffmpeg >/dev/null 2>&1; then brew unlink ffmpeg || exit 1; fi
  brew link --force ffmpeg-full || exit 1
  ffmpeg_has_zscale && ok 'ffmpeg zscale' || { need 'ffmpeg-full 已安装但当前 ffmpeg 仍缺 zscale，请检查 PATH'; exit 1; }
fi
command -v node >/dev/null && ok 'node' || run 'node' brew install node
command -v python3.12 >/dev/null && ok "python3.12" || run "python3.12" brew install python@3.12

echo "== 2. 声学对齐环境（${align}）"
if "$align/bin/python" -c "import qwen_asr, torch, soundfile" 2>/dev/null; then ok "对齐依赖"
else
  run "对齐依赖" bash -c "python3.12 -m venv '$align' && '$align/bin/pip' install ${pip_index[*]} \
    torch==2.10.0 torchaudio==2.10.0 transformers==4.57.6 accelerate==1.12.0 huggingface_hub==0.36.2 \
    soundfile==0.14.0 numpy==2.3.5 qwen-asr==0.0.6"
fi
if [ -x "$align/bin/python" ] && "$align/bin/python" -c "from huggingface_hub import snapshot_download as s; s('Qwen/Qwen3-ForcedAligner-0.6B', local_files_only=True)" 2>/dev/null; then ok "对齐模型已下载"
else
  run "对齐模型（约 1.2 GB）" ${hf_endpoint[@]+"${hf_endpoint[@]}"} "$align/bin/python" -c "from huggingface_hub import snapshot_download as s; s('Qwen/Qwen3-ForcedAligner-0.6B')"
fi

echo "== 3. 达芬奇（需本人从 Blackmagic 官网安装并打开过一次）"
[ -d "/Applications/DaVinci Resolve/DaVinci Resolve.app" ] && ok "DaVinci Resolve" || need "DaVinci Resolve（本人安装）"

echo "== 4. 达芬奇 MCP（${mcp}）"
if [ -f "$mcp/src/server.py" ]; then ok "MCP 程序"
else
  run "MCP 程序" bash -c "git clone https://github.com/samuelgursky/davinci-resolve-mcp.git '$mcp' && git -C '$mcp' checkout $mcp_commit && cd '$mcp' && python3.12 install.py --clients codex --update-policy never"
fi
if [ -d "$mcp/resolve-advanced" ] && (cd "$mcp/resolve-advanced" && node -e "require('./vendor/drp-format/place-subtitles')" 2>/dev/null); then ok "字幕组件（Node 依赖）"
elif [ -d "$mcp/resolve-advanced" ]; then
  run "字幕组件（Node 依赖）" bash -c "cd '$mcp/resolve-advanced' && npm install --omit=dev ${npm_registry[*]}"
else
  need "字幕组件（MCP 里没有 resolve-advanced 目录，请让 Codex 检查 MCP 版本）"
fi

echo "== 5. 共享 Skill"
skill_links_ready=1
skill_home="${CODEX_HOME:-$HOME/.codex}/skills"
for name in ai-edit a-roll b-roll fx; do
  if [ ! -f "$root/skills/$name/SKILL.md" ] || [ ! -L "$skill_home/$name" ] || [ "$(readlink "$skill_home/$name")" != "$root/skills/$name" ]; then
    skill_links_ready=0
  fi
done
if [ -e "$skill_home/ai-talking-head-edit" ] || [ -L "$skill_home/ai-talking-head-edit" ]; then
  skill_links_ready=0
fi
if [ "$skill_links_ready" -eq 1 ]; then ok "ai-edit / a-roll / b-roll / fx Skill 已连接"
else run "Skill 连接" bash "$root/bin/install_skill_mac.sh"; fi

echo "== 6. 花字字体与音效库"
fx_font_source="${AI_EDIT_FONT_DIR:-$root/assets/fonts}"
fx_font_target="$HOME/Library/Fonts"
for font in 'ChillDuanSans_Black.otf' '极影毁片圆.ttf' '爱点风雅黑长体(商用免费).ttf' 'AlimamaShuHeiTi-Bold.ttf'; do
  if [ -f "$fx_font_target/$font" ]; then ok "字体 $font"
  elif [ ! -f "$fx_font_source/$font" ]; then need "字体 $font（请设置 AI_EDIT_FONT_DIR）"
  elif [ "$check" -eq 1 ]; then need "字体 $font（运行 bash bin/setup_mac.sh 安装）"
  else
    mkdir -p "$fx_font_target" || exit 1
    cp -n "$fx_font_source/$font" "$fx_font_target/$font" || exit 1
    [ -s "$fx_font_target/$font" ] && ok "字体 $font 已复制（不覆盖已有）" || exit 1
  fi
done
if python3 -c 'import fontTools' 2>/dev/null; then ok 'fontTools'
else run 'fontTools' python3 -m pip install --user fonttools; fi
fx_sfx_dir="${AI_EDIT_SFX_DIR:-$root/assets/sfx}"
if [ -d "$fx_sfx_dir" ] && [ -r "$fx_sfx_dir" ] && [ -x "$fx_sfx_dir" ]; then ok '音效库可访问'
else need '音效库不可访问（设置 AI_EDIT_SFX_DIR）'; fi

echo "== 7. 自动抠人独立环境与模型"
matting="$tools/ai-edit-matting"
if PYTHONPATH="$root" python3 -c "from engine.fx.matte import environment_check; import sys; sys.exit(not environment_check()[0])" 2>/dev/null; then ok "RVM 环境与模型校验"
else
  run "RVM 环境与模型" python3 "$root/bin/setup_matting.py" ${pip_index[@]+"${pip_index[@]}"}
fi

echo "== 8. 自检"
python3 "$root/bin/doctor.py" --summary
echo
if [ ${#missing[@]} -gt 0 ]; then echo "还缺：${missing[*]}"; exit 1; fi
echo "环境准备完成。"
