# Jarvis — Windows 桌面与手机语音助手

本项目的源码备份，包含中文离线唤醒、透明面具动画、手机控制页面、Skills 白名单、微信登录桥接、本地/云端聊天路由及需确认的项目任务中心。

## 安全与上传范围

- Git 采用默认拒绝的文件清单；只纳入经过检查的源码、测试、文档与配置。
- 不上传任何真实环境变量文件、API 密钥、桌面桥凭据、日志、录音、聊天记忆、项目任务运行记录、虚拟环境、模型、备份或生成的可执行程序。
- 面具图片的授权仅限本机个人使用，图片本身不放入仓库。来源及限制见 [assets/SOURCE.md](assets/SOURCE.md)。现有本机图片不被删除；缺少素材时动画使用内置矢量回退，视觉效果并不等同于本机完整版。
- 不将语音原文传给 shell；电脑操作只经过 Skills。开发任务必须由用户确认，取消或重启后不会自动重放中断操作。
- 窗口分析需主动触发、先预览后确认、仅本地处理，图片不落盘。

## 环境与启动

目前针对 Windows、Python 3.12、耳机麦克风及 Windows 离线中文 SAPI 语音开发。下面命令用于新安装环境；现有运行环境无需重新安装。

```powershell
py -3.12 -m venv .voice-venv
& .\.voice-venv\Scripts\python.exe -m pip install -r requirements.txt
```

手机认证使用独立的 `JARVIS_API_KEY`，云端聊天使用独立的 `OPENAI_API_KEY`；在目标电脑自行安全配置，切勿通过 Git、聊天或日志传递密钥。应用可在运行时加载本机已批准的 `openai.env`，该文件永不纳入版本控制。不要覆盖已有认证文件。

本地模型固定连接 `http://127.0.0.1:11434`，当前配置名称为 `qwen3.8:27b`；需自行安装、启动 Ollama 并准备相应模型。聊天按复杂度路由，本地/云端故障会如实显示；云端联网需要实际 API 权限、额度与可达网络。

本机 ASR 使用 faster-whisper `small` 模型；首次安装需准备模型缓存，运行时音频只在内存中处理。按需在本机运行 `generate_speech_cache.ps1` 生成离线语音缓存，不把生成的音频提交到 Git。

- 手机服务：运行 `start_jarvis.ps1`，只监听 `127.0.0.1:8000`，页面为 `/control`。远程 HTTPS 接入由用户另行配置，仓库不含私有访问地址或隧道凭据。
- 桌面监听：在自己的交互桌面运行 `start_jarvis_desktop.ps1`；现有本机也可双击 `JarvisDesktop.exe`。仓库提供启动器源码 `jarvis_interactive_launcher.cs`，不包含生成的 EXE。
- 克隆到另一台电脑后，先检查 `skills.json` 中的应用路径、`desktop_config.json` 白名单及项目目录配置；不要直接沿用本机路径执行操作。
- 用户项目根目录当前为 `D:\LYY_work`。项目任务还依赖独立配置的本机 Codex SDK/应用环境，Python 依赖安装并不等于项目任务线路已经就绪。

## 验证

```powershell
& .\.voice-venv\Scripts\python.exe -m unittest discover -s . -p 'test_*.py' -q
& .\.voice-venv\Scripts\python.exe desktop_controller.py --check
& .\.voice-venv\Scripts\python.exe check_wake_sensitivity.py
```

唤醒冒烟检查只使用缓存模型和内存合成语音，不打开麦克风、不播放声音、不保存音频。模拟及合成检查不替代真人耳机、桌面动画、微信和实际项目任务验收。

最近本机结果：223 项 Python 回归、8 种合成语音/噪声检查通过。真人耳机灵敏度仍待确认；云端最后一次实际生成返回 HTTP 429，不能据此宣称云端问答或联网已完全可用。最新运行状态见本机健康页面，不以历史测试代替实时状态。

## 详细说明

- [桌面唤醒、动画与语音控制](DESKTOP_VOICE_README.md)
- [聊天路由及项目任务](CODEX_INTEGRATION_README.md)
- [Skills 白名单](SKILLS_README.md)
- [升级进度与已知限制](UPGRADE_PROGRESS.md)

本仓库不对第三方面具素材或依赖另行授予许可。
