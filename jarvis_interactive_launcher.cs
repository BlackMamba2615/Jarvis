using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Drawing;
using System.Windows.Forms;

internal static class JarvisInteractiveLauncher
{
    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    private static extern IntPtr GetThreadDesktop(uint threadId);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool GetUserObjectInformation(
        IntPtr handle, int index, StringBuilder value, uint length, out uint needed);

    private static string DesktopName()
    {
        var value = new StringBuilder(128);
        uint needed;
        return GetUserObjectInformation(GetThreadDesktop(GetCurrentThreadId()), 2,
            value, (uint)(value.Capacity * 2), out needed)
            ? value.ToString()
            : "unknown";
    }

    [STAThread]
    private static void Main()
    {
        var directory = AppDomain.CurrentDomain.BaseDirectory;
        var runtime = Path.Combine(directory, "runtime");
        Directory.CreateDirectory(runtime);
        var statusPath = Path.Combine(runtime, "interactive_launcher_status.txt");

        try
        {
            var python = Path.Combine(directory, ".voice-venv", "Scripts", "pythonw.exe");
#if PREVIEW
            var script = Path.Combine(directory, "jarvis_overlay.py");
            var arguments = "\"" + script + "\" --status listening --message \"JARVIS 现在可见吗\" --duration-ms 45000";
#else
            var script = Path.Combine(directory, "desktop_controller.py");
#if LIVE
            var arguments = "\"" + script + "\"";
#else
            var arguments = "\"" + script + "\" --dry-run";
#endif
#endif
            var process = Process.Start(new ProcessStartInfo(python, arguments)
            {
                WorkingDirectory = directory,
                UseShellExecute = false,
                CreateNoWindow = false,
            });
            File.WriteAllText(statusPath,
                DateTime.Now.ToString("O") + " desktop=" + DesktopName() +
                " child_pid=" + process.Id + Environment.NewLine,
                Encoding.UTF8);
            using (var splash = new Form())
            {
                splash.Text = "Jarvis 启动状态";
                splash.StartPosition = FormStartPosition.Manual;
                splash.Location = new Point(20, 20);
                splash.ClientSize = new Size(290, 80);
                splash.FormBorderStyle = FormBorderStyle.FixedDialog;
                splash.MaximizeBox = false;
                splash.MinimizeBox = false;
                splash.Controls.Add(new Label
                {
#if PREVIEW
                    Text = "Jarvis 动画已启动，请看屏幕中央",
#elif LIVE
                    Text = "Jarvis 正在启动（计算器控制已启用）",
#else
                    Text = "Jarvis 正在启动（安全测试模式）",
#endif
                    AutoSize = false,
                    Dock = DockStyle.Fill,
                    TextAlign = ContentAlignment.MiddleCenter,
                    Font = new Font("Microsoft YaHei UI", 10),
                });
                var timer = new System.Windows.Forms.Timer { Interval = 8000 };
                timer.Tick += (sender, eventArgs) => { timer.Stop(); splash.Close(); };
                splash.Shown += (sender, eventArgs) => timer.Start();
                Application.Run(splash);
            }
        }
        catch (Exception error)
        {
            File.WriteAllText(statusPath,
                DateTime.Now.ToString("O") + " error=" + error.GetType().Name +
                Environment.NewLine, Encoding.UTF8);
        }
    }
}
