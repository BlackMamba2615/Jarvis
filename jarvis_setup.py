"""Local setup/launch wizard, usable before installing project dependencies."""
from __future__ import annotations

import argparse
import json
import queue
from pathlib import Path
import sys
import threading
import webbrowser

from jarvis_install import (
    SetupError, build_launchers, initialize_environment, installation_report,
    prepare_asr, prepare_voice_replies, service_report, start_desktop, start_phone,
)
from jarvis_settings import BASE_DIR, SettingsError, load_local_settings, save_local_settings


def show_wizard(*, launcher: bool = False, check_only: bool = False) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("Jarvis 启动中心" if launcher else "Jarvis 安装与配置向导")
    root.geometry("830x730")
    root.minsize(740, 650)
    panel = ttk.Frame(root, padding=18)
    panel.pack(fill="both", expand=True)
    ttk.Label(panel, text="JARVIS  /  本机安装与启动", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
    ttk.Label(panel, text="不读取或覆盖密钥文件 · 不自动录音/截图 · 不自动登录或执行项目任务").pack(anchor="w", pady=(6, 12))
    try:
        settings = load_local_settings()
    except SettingsError:
        settings = {"version": 1}
        messagebox.showwarning("本机配置", "现有本机配置无效；请检查后明确保存，不会自动覆盖。", parent=root)
    legacy_projects = Path(r"D:\LYY_work")
    project = tk.StringVar(value=settings.get("project_root", str(legacy_projects if legacy_projects.is_dir() else BASE_DIR / "projects")))
    model = tk.StringVar(value=settings.get("local_model", "qwen3.8:27b"))
    wechat = tk.StringVar(value=settings.get("skill_targets", {}).get("open_wechat", ""))
    fields = ttk.LabelFrame(panel, text="本机非敏感配置（保存到忽略的 jarvis.local.json）", padding=10)
    fields.pack(fill="x")
    for row, (label, variable, chooser) in enumerate((
        ("项目根目录", project, lambda: filedialog.askdirectory(parent=root)),
        ("Ollama 模型名称", model, None),
        ("微信程序（可选）", wechat, lambda: filedialog.askopenfilename(parent=root, filetypes=[("微信程序", "Weixin.exe WeChat.exe")]))
    )):
        ttk.Label(fields, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(fields, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8)
        if chooser:
            def choose(var=variable, fn=chooser):
                selected = fn()
                if selected:
                    var.set(selected)
            ttk.Button(fields, text="浏览", command=choose).grid(row=row, column=2)
    fields.columnconfigure(1, weight=1)
    phone_secret = tk.StringVar()
    ttk.Label(panel, text="手机连接密码（已有认证可留空；新安装可输入 ≥16 字符，仅传给本次服务进程，不保存）").pack(anchor="w", pady=(12, 4))
    ttk.Entry(panel, textvariable=phone_secret, show="●").pack(fill="x")
    ttk.Label(panel, text="云端凭据仅沿用运行时已有配置；不创建/显示 OpenAI 密钥。手机远程 HTTPS 接入需另外配置。").pack(anchor="w", pady=5)
    actions = ttk.Frame(panel)
    actions.pack(fill="x", pady=8)
    output = tk.Text(panel, height=17, wrap="word", state="disabled", font=("Microsoft YaHei UI", 10))
    output.pack(fill="both", expand=True)
    footer = tk.StringVar(value="请先初始化环境，再检测；安装不会自动下载/运行 27B 模型。")
    ttk.Label(panel, textvariable=footer, wraplength=770).pack(anchor="w", pady=(8, 0))
    pending: queue.Queue[tuple[bool, object]] = queue.Queue()
    buttons = []
    busy = [False]

    def display(value):
        if isinstance(value, dict) and "checks" in value:
            text = "\n".join(f"[{item['status']}] {item['id']}：{item['detail']}" for item in value["checks"])
            text += "\n\n基础检查通过。" if value.get("base_ready") else "\n\n请先处理 error 项。"
            text += " 检测未验证真人唤醒或实际模型回答。"
        elif isinstance(value, dict):
            text = "\n".join(f"{k}: {v}" for k, v in value.items()) + "\n只检测本地接口与模型安装情况，未运行推理。"
        else:
            text = str(value)
        output.configure(state="normal")
        output.delete("1.0", "end")
        output.insert("end", text)
        output.configure(state="disabled")

    def execute(fn):
        if busy[0]:
            return
        busy[0] = True
        for button in buttons:
            button.configure(state="disabled")
        footer.set("正在处理，请稍候；窗口不会启动麦克风或截图，除非你刚选择了桌面启动。")
        def work():
            try:
                pending.put((True, fn()))
            except (SetupError, SettingsError) as error:
                pending.put((False, str(error)))
            except Exception as error:
                pending.put((False, "操作失败（" + type(error).__name__ + "），未显示内部信息或凭据。"))
        threading.Thread(target=work, daemon=True).start()

    def receive():
        try:
            okay, value = pending.get_nowait()
        except queue.Empty:
            pass
        else:
            busy[0] = False
            for button in buttons:
                button.configure(state="normal")
            footer.set("操作完成；仍需按提示验证。" if okay else "操作未完成，请查看说明。")
            display(value)
        root.after(100, receive)

    def save():
        value = {"version": 1, "project_root": project.get().strip(), "local_model": model.get().strip(),
                 "skill_targets": {**settings.get("skill_targets", {}), "open_jarvis": str(BASE_DIR)}}
        if wechat.get().strip():
            value["skill_targets"]["open_wechat"] = wechat.get().strip()
        else:
            value["skill_targets"].pop("open_wechat", None)
        execute(lambda: (save_local_settings(BASE_DIR, value), "本机配置已保存。已有配置已备份，密钥与 Skills 白名单未修改；运行中的服务需下次启动才采用新配置。影响电脑操作的路径已由你明确选择。")[1])

    def install():
        if messagebox.askyesno("初始化环境", "将创建独立 .voice-venv 并从 PyPI 下载依赖。已有环境不会自动升级。继续？", parent=root):
            execute(initialize_environment)

    def download():
        if messagebox.askyesno("语音模型", "从 Hugging Face 下载离线识别 small 模型，占用数百 MB；不打开麦克风。继续？", parent=root):
            execute(prepare_asr)

    def desktop():
        if messagebox.askyesno("开启桌面监听", "这将打开本机麦克风并启动唤醒与动画。是否现在启动？", parent=root):
            execute(start_desktop)

    def phone():
        value = phone_secret.get()
        phone_secret.set("")
        execute(lambda: start_phone(phone_key=value))

    for number, (label, action) in enumerate((
        ("1 初始化环境", install), ("2 保存本机设置", save), ("3 安装检测", lambda: execute(installation_report)),
        ("4 下载语音模型", download), ("生成 EXE 入口", lambda: execute(build_launchers)),
        ("准备固定语音回复", lambda: execute(prepare_voice_replies)),
        ("检查本地服务", lambda: execute(service_report)), ("启动桌面（麦克风）", desktop),
        ("启动手机服务", phone), ("打开本机控制页", lambda: webbrowser.open("http://127.0.0.1:8000/control")),
    )):
        button = ttk.Button(actions, text=label, command=action)
        button.grid(row=number // 3, column=number % 3, padx=(0, 7), pady=4, sticky="ew")
        buttons.append(button)
    for col in range(3):
        actions.columnconfigure(col, weight=1)

    def close():
        if busy[0]:
            messagebox.showinfo("仍在处理", "请等待当前安装/检测结束，避免留下未完成环境。", parent=root)
        else:
            root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, receive)
    if check_only:
        root.after(200, lambda: execute(installation_report))
    root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis safe setup and launch wizard")
    parser.add_argument("--check", action="store_true", help="Read-only installation check; no capture or paid API")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--install", action="store_true", help="Initialize a NEW isolated environment")
    parser.add_argument("--build-launchers", action="store_true", help="Compile local EXE wrappers, never change execution policy")
    parser.add_argument("--launcher", action="store_true")
    parser.add_argument("--check-gui", action="store_true")
    args = parser.parse_args()
    try:
        if args.build_launchers:
            print(build_launchers())
        elif args.install:
            print(initialize_environment())
        elif args.check:
            report = installation_report()
            if args.json:
                print(json.dumps(report, ensure_ascii=True))
            else:
                for check in report["checks"]:
                    print(f"[{check['status']}] {check['id']}: {check['detail']}")
            return 0 if report["base_ready"] else 1
        else:
            show_wizard(launcher=args.launcher, check_only=args.check_gui)
        return 0
    except (SetupError, SettingsError) as error:
        print(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
