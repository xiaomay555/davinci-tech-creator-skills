# 达芬奇数码博主剪辑 Skills

第 1 版（v1.0.0）。面向中文数码博主的 DaVinci Resolve 剪辑工作流，供 Codex / Claude Code 等 AI 助手使用，包含配套本地引擎。

## 四个入口

| Skill | 用途 |
|---|---|
| [ai-edit](skills/ai-edit/SKILL.md) | 判断任务并分派 |
| [a-roll](skills/a-roll/SKILL.md) | 选重录、连贯初剪、气口修整、字幕和达芬奇交付 |
| [b-roll](skills/b-roll/SKILL.md) | 按语义选镜头、配画面、保护手改与偏好学习 |
| [fx](skills/fx/SKILL.md) | 花字、音效、字卡和局部返修 |

## 使用

1. 克隆或下载仓库，安装 DaVinci Resolve、git、Python、Node.js 和 ffmpeg。环境准备脚本针对 macOS；依赖较多，先读脚本再运行。
2. 运行 `bash bin/setup_mac.sh --check` 查看缺项；运行 `bash bin/setup_mac.sh` 可补装依赖、对齐模型、MCP 和 Skill 链接。Homebrew、Xcode 命令行工具和达芬奇本体需自行安装。也可只运行 `bash bin/install_skill_mac.sh` 连接四个 Skill。
3. 需要豆包语音识别时，用 `python3 bin/doubao_credentials.py` 在本机页面配置自己的 Key。该功能把音频发送给豆包，可能产生费用；公开版不提供任何 Key、配额或共享账号。Key 保存在本地 `secrets/`，不要提交。
4. 运行 `python3 bin/doctor.py --summary` 自检，再让 AI 从 `skills/ai-edit/SKILL.md` 开始。

字体、音效、LUT、素材、模型权重和达芬奇软件均不随本项目发布，需自行取得并确认使用许可。用 `AI_EDIT_FONT_DIR` 和 `AI_EDIT_SFX_DIR` 指向自己的素材目录。

学习数据默认位于当前用户数据目录下的 `ai-talking-head-edit` 文件夹；可用 `AI_EDIT_DATA_DIR` 指定位置。首次使用 B-roll 前自行创建该数据目录，并用 `--creator` 指定自己的档案名。高级配置见 [配置说明](docs/configuration.md)。

## 制作边界

- 保护用户手工定稿，只修改明确授权的对象；默认不使用 Fusion，不自动渲染。
- 新建时间线默认 4K UHD，其他画幅先确认。引擎 v1 的完整自动剪辑仍仅支持单音轨、恒定 29.97 fps；其他帧率需停止自动流程并说明限制。
- [达芬奇接口实测](docs/达芬奇接口实测.md)记录了特定版本的观察，不保证所有版本行为一致。
- 离线测试使用虚构路径、合成或匿名化结构数据；不包含原始拍摄素材、个人工程、真实逐字稿或私人反馈记录。
- 验证命令：`bash bin/run_tests.sh`；B-roll 实时层另运行 `PYTHONPATH=engine python3 -m unittest discover -s engine/broll/live -p 'test_*.py'`。测试不调用付费识别，不连接实际达芬奇工程。

## v1 已知验证限制

本次发布已完成凭据、隐私与文件清单检查，以及 Python / JSON 语法检查。当前本机 FFmpeg 7.1 环境下，部分 B-roll 衍生片段的帧率或 BT.709 标签校验回归失败；相同用例在未修改的原版上也失败。相关渲染功能使用前需在自己的环境复测，不能视为已经通过端到端制作验收。没有连接用户工程或调用付费识别做发布测试。

本仓库以 MIT 许可证发布，第三方依赖遵守各自许可。
