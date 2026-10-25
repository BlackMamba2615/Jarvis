using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class JarvisDistributionLauncher
{
    [STAThread]
    private static void Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string script = Path.Combine(root, "jarvis_setup.py");
        if (!File.Exists(script)) {
            MessageBox.Show("请将启动器放在 Jarvis 源码目录内，不能只复制 EXE。", "Jarvis");
            return;
        }
        string python = Path.Combine(root, ".voice-venv", "Scripts", "pythonw.exe");
        string args = "\"" + script + "\"";
#if CHECK
        args += " --check-gui";
#elif LAUNCHER
        args += " --launcher";
#endif
        if (!File.Exists(python)) {
            python = "pyw.exe";
            args = "-3.12 " + args;
        }
        try {
            Process.Start(new ProcessStartInfo(python, args) {
                WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true
            });
        } catch {
            MessageBox.Show("启动未完成。请安装含 Tcl/Tk 和启动器的 64 位 Python 3.12，再使用 SetupJarvis.cmd。", "Jarvis");
        }
    }
}
