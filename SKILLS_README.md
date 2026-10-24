# Jarvis 技能配置说明

Jarvis V4 将应用、别名、自然说法和场景放在 `skills.json` 中。手机录音、Tailscale HTTPS、FastAPI、faster-whisper、OpenCC 和 `X-Jarvis-Key` 认证接口保持不变。

## 安装与启动

1. 先在正在运行 Jarvis 的窗口按 `Ctrl+C`。
2. 在本升级包目录运行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install_jarvis_skills.ps1
   ```

3. 启动 Jarvis：

   ```powershell
   Set-Location D:\Jarvis
   .\.voice-venv\Scripts\Activate.ps1
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```

如果你原来使用了不同端口或启动参数，请继续使用原来的启动命令。Tailscale 的 HTTPS 转发目标不需要更改。

安装程序会在 `D:\Jarvis\backups\技能系统安装时间` 下备份原有的 `main.py`、`.env` 和相关技能文件。它不会显示、修改或覆盖 `.env`。

## 增加一个应用技能

打开 `D:\Jarvis\skills.json`，在 `actions` 中增加一项：

```json
"open_calculator": {
  "type": "open_path",
  "target": "C:\\Windows\\System32\\calc.exe",
  "aliases": ["计算器", "calculator", "calc"],
  "intent": "open",
  "message": "计算器已打开"
}
```

注意上一项结尾需要逗号。保存后重启 Jarvis，即可说“帮我打开计算器”。`target` 必须是绝对路径，并且文件或文件夹必须存在。

## 修改别名和自然说法

- 应用名或别名：编辑对应动作的 `aliases`。
- “打开”的自然说法：编辑 `settings.open_intents`。
- 唤醒词：编辑 `settings.wake_words`。
- 繁体中文会先由 OpenCC 转为简体，再匹配这些词。

例如给微信增加“聊天软件”别名，只需把微信的别名改为：

```json
"aliases": ["微信", "聊天软件", "weixin", "wechat"]
```

## 增加或修改场景

在 `scenes` 中增加一项，并引用已经存在于 `actions` 的动作 ID：

```json
"writing": {
  "keywords": ["开始写作", "进入写作模式"],
  "actions": ["open_notepad", "open_jarvis"],
  "message": "写作模式已启动"
}
```

一个场景可以依次执行多个白名单动作。

## 安全边界

- 语音文本只用于匹配配置中的动作 ID，从不传给 PowerShell、cmd 或其他 shell。
- 技能类型只允许 `open_path`、`status`、`lock_pc` 和专用的 `wechat_login_qr`；后者只能由带 API Key 的手机接口执行，电脑端语音监听不会执行它。
- 默认没有关机、重启、删除、下载或任意命令执行动作。
- 配置格式或动作类型不合法时，Jarvis 会拒绝启动并指出配置位置，避免静默执行错误内容。

## 手机端微信登录二维码

在手机 Jarvis 页面说“帮我在电脑上登录微信”。Jarvis 只会启动白名单中的电脑版微信，并在识别到微信**登录**二维码后，将裁切后的二维码显示在手机页面。二维码只在内存中保留最多 60 秒，图片接口需要 `X-Jarvis-Key`，不会发送整个桌面或聊天窗口截图。若电脑微信已登录、窗口不可见或二维码不符合登录码特征，会提示失败而不发送图片。

若 Jarvis 页面与微信在同一部手机上，先保存二维码图片，再在微信“扫一扫”里从相册选择；电脑端登录仍需你在手机微信里自行确认。过期后重新说一次指令。
