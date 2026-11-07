# 从 Git 拉取后的安装与验证

这是面向 Windows 的源码安装版，不是免 Python、免模型的独立整机安装包。可从公开的 [GitHub 仓库](https://github.com/BlackMamba2615/Jarvis)克隆或下载源码；不要复制他人的环境文件、聊天记忆或桌面桥凭据。

## 1. 准备 Python

使用 [Python 官方 Windows 下载页](https://www.python.org/downloads/windows/)提供的 **64 位 Python 3.12**，安装时保留 Python Launcher 和 Tcl/Tk。安装器不会替你安装系统 Python、修改执行策略或要求管理员权限。

Python 虚拟环境必须在目标电脑重新创建，不能直接复制另一台电脑的 `.voice-venv`。参见 [Python venv 官方说明](https://docs.python.org/3.12/library/venv.html)。

## 2. 双击安装入口

双击 `SetupJarvis.cmd`。如果已经自行生成了启动器，也可以双击 `JarvisSetup.exe`。中文界面中依次进行：

1. **初始化环境**：创建本目录的 `.voice-venv` 并从 PyPI 安装依赖。已有环境不会自动覆盖或升级；如果检测到本安装器留下的未完成标记，用户再次明确初始化时可续装。
2. **保存本机设置**：选择自己的项目根目录、已安装的 Ollama 模型名称、微信程序位置。模型名称可与作者的 `qwen3.8:27b` 不同，不会强制下载大模型。目录应为本机普通目录，不可使用网络共享、符号链接或目录联接。
3. **安装检测**：只检查依赖、图形组件、中文语音、输入设备数量、模型缓存、路径和配置是否存在。不会打开麦克风、截图、执行技能、运行模型生成或调用付费 API。凭据只检查存在性，不读取/验证内容。
4. **下载语音模型**：由用户明确点击后从 Hugging Face 下载 `Systran/faster-whisper-small` 缓存，约数百 MB；不会开始录音或推理。
5. **准备固定语音回复**：生成 Skills 的固定中文 WAV 缓存，供手机技能回复使用；仅生成文件，不播放声音。普通聊天使用动态的本机 SAPI 语音。

没有中文离线语音时，请自行在 Windows 中安装中文语音；没有可用麦克风或未授予权限时，需在系统中手动处理。检测到设备不等于真人唤醒已成功。

可选的 NVIDIA 语音识别加速：默认基础安装支持 CPU，不要求独立显卡。若希望使用已安装的 NVIDIA 显卡，可在项目目录运行以下固定命令，安装约 1.3 GB 的 CUDA 12/cuDNN 9 Python 运行库；这不安装显卡驱动，不修改系统 PATH。已在 RTX 4060 Ti、Windows x64、CTranslate2 4.8.2 上验证。依赖要求参考 [faster-whisper 官方 GPU 说明](https://github.com/SYSTRAN/faster-whisper#gpu)。

```powershell
& .\.voice-venv\Scripts\python.exe -m pip --isolated --disable-pip-version-check install --no-input --only-binary=:all: -r requirements-asr-cuda.txt
```

桌面监听的 `wake_listener.asr_device` 默认为 `auto`：显卡可用时使用 FP16，运行库缺失或显卡推理出错时回退 CPU int8；没有缓存的语音模型时要求先下载，不在后台自动下载。若与本地聊天模型争用显存，可改为 `cpu`。`check_wake_sensitivity.py` 可做无麦克风/无播放的离线合成检查，`--cpu` 单独验证 CPU 路径。

## 3. 密钥与手机连接

- 安装向导不会读取、显示或写入 `.env`、`openai.env`，不会创建 OpenAI 密钥，也不会覆盖已有认证。
- 已配置手机认证的电脑可留空“手机连接密码”。新安装者可以输入 **16–256 个非空格 ASCII 字符**作为本次连接密码，然后点击“启动手机服务”。该值只进入新服务进程的环境，不进入命令行、磁盘文件、Git 或安装日志；输入框随后清空。服务退出后需重新输入。
- 手机控制页面需要输入同一个连接密码。启动已在线的服务不会替换其密码，也不会重启或中断它。
- 云端聊天仍需用户自己已有、已获授权的 OpenAI 凭据；应用只在运行时沿用环境变量或本机 `openai.env`。不要向其他使用者分发作者的密钥。配置存在并不表示 API 权限、网络和额度可用。

## 4. 启动与检测

- `StartJarvis.cmd` / `JarvisStart.exe` 打开启动中心，不会自行开始录音。
- “启动桌面（麦克风）”会再次提示确认；点击同意后才启动常驻唤醒。以桌面“语音监听已就绪”和实时心跳为准，不以进程存在为准。
- “启动手机服务”只监听 `127.0.0.1:8000`。“检查本地服务”核对状态后，可打开 `http://127.0.0.1:8000/control`。
- `CheckJarvis.cmd` / `JarvisCheck.exe` 打开只读安装检测；运行期间不会录音、截图或发送付费 API 请求。
- Ollama 必须由用户自己启动并安装所选模型；安装器只检查回环服务及模型列表，不会自动启动 Ollama 或占用 GPU 加载模型。窗口分析还需要模型支持视觉输入。
- 从手机远程访问，需用户另行配置受认证的 HTTPS/私有网络入口。安装器不开放防火墙、不监听公网、不自动配置 Tailscale 或其他账号。
- Codex 项目开发是可选扩展，需要独立安装与登录相应本机 SDK/应用环境。向导只检测，不自动获取账号、授权或恢复中断任务；项目确认卡与目录限制保持不变。

## 5. 生成可分发的启动入口

向导中点击“生成 EXE 入口”，使用系统已有的 .NET Framework C# 编译器生成：

- `JarvisSetup.exe`：安装/配置向导。
- `JarvisStart.exe`：启动中心。
- `JarvisCheck.exe`：安装检测。

这些是源码目录的启动包装器，必须与源码一起分发，不能单独复制；不包含 Python、模型、凭据或面具图片。它们是未签名的本地构建，**不要绕过系统安全警告**；可审查源码后自行编译，或使用文本 CMD 入口。不会更改原有 `JarvisDesktop.exe` 等本机启动器，不会自动覆盖同名新入口。

也可在源码目录执行：

```powershell
py -3.12 jarvis_setup.py --build-launchers
py -3.12 jarvis_setup.py --check --json
```

这条构建路径不执行 PowerShell 脚本，也不需要 `ExecutionPolicy Bypass`。

## 本机配置与更新

机器相关设置只保存在 Git 忽略的 `jarvis.local.json`：项目根目录、本地模型名、已有白名单应用的路径。不能在其中保存密钥、改变服务地址、添加技能或扩大桌面动作白名单。保存时会先备份已有本机配置；运行中的服务不会被向导自动重启。

新代码采用本机配置；未使用该配置的老安装仍保留既有默认行为。新启动中心要求先明确保存本机设置，避免克隆后直接执行作者电脑上的路径。

Git 不包含虚拟环境、模型、生成 EXE、日志、语音缓存、任务数据、记忆或受限图片。缺少面具图片时使用内置矢量面具，视觉效果与作者本机不同。

## 安装后验收

```powershell
& .\.voice-venv\Scripts\python.exe -m unittest discover -s . -p 'test_*.py' -q
& .\.voice-venv\Scripts\python.exe desktop_controller.py --check
```

程序检测与回归通过后，仍需用户验证耳机真实唤醒、动画、语音中断、手机播报、微信窗口及项目确认流程。安装检查不会替代这些验收，也不会声称云端 HTTP 429、网络限制或账号权限已解决。

2026-10-25 验证记录：原环境 254 项测试全部通过；独立源码副本重新安装依赖后，全套 254 项无失败，其中 6 项个人使用图片的像素检查因未分发素材而明确跳过；无图片的矢量回退照常测试。Codex 桥接逻辑使用内存模拟 SDK，不需要安装或登录真实 SDK，因此这些测试不代表真实项目开发功能已就绪。新环境只读检测如实报告未下载 ASR、未配置认证、未安装 SDK 等警告；三个 EXE 包装器均编译成功。未在这次检查中录音、播放、截图或运行模型推理。
