using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

internal static class JarvisInteractiveLauncher
{
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] private static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] private static extern IntPtr GetThreadDesktop(uint threadId);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    private static extern bool GetUserObjectInformation(IntPtr handle,int index,StringBuilder value,uint length,out uint needed);

    private static string DesktopName()
    {
        StringBuilder value=new StringBuilder(128); uint needed;
        return GetUserObjectInformation(GetThreadDesktop(GetCurrentThreadId()),2,value,(uint)(value.Capacity*2),out needed)?value.ToString():"unknown";
    }

    private static bool ListenerReady(string directory)
    {
        string path=Path.Combine(directory,"runtime","desktop_health.json");
        FileInfo info=new FileInfo(path);
        if(!info.Exists || info.Length>16384 || DateTime.UtcNow-info.LastWriteTimeUtc>TimeSpan.FromSeconds(6)) return false;
        string health=File.ReadAllText(path,Encoding.UTF8);
        if(!Regex.IsMatch(health,"\"model_ready\"\\s*:\\s*true") ||
            !Regex.IsMatch(health,"\"microphone\"\\s*:\\s*\"(ready|recovered)\"")) return false;
        Match match=Regex.Match(health,"\"pid\"\\s*:\\s*([0-9]+)");
        if(!match.Success) return false;
        using(Process listener=Process.GetProcessById(int.Parse(match.Groups[1].Value)))
            return !listener.HasExited && (listener.ProcessName=="pythonw" || listener.ProcessName=="python");
    }

    private static bool IsStoppedHealth(string health,DateTime writtenAt,DateTime launchedAt)
    {
        // A stopped record from a previous session must not cancel a new
        // launch while the new model is still loading.
        return writtenAt>=launchedAt && health.Length<=16384 &&
            Regex.IsMatch(health,"\"microphone\"\\s*:\\s*\"stopped\"");
    }

    private static bool ListenerStopped(string directory,DateTime launchedAt)
    {
        string path=Path.Combine(directory,"runtime","desktop_health.json");
        FileInfo info=new FileInfo(path);
        if(!info.Exists || info.Length>16384 || info.LastWriteTimeUtc<launchedAt) return false;
        return IsStoppedHealth(File.ReadAllText(path,Encoding.UTF8),info.LastWriteTimeUtc,launchedAt);
    }

    private static void CheckLifecycle()
    {
        DateTime start=new DateTime(2026,1,1,0,0,0,DateTimeKind.Utc);
        string stopped="{\"microphone\":\"stopped\",\"model_ready\":false}";
        if(!IsStoppedHealth(stopped,start.AddSeconds(1),start) ||
            IsStoppedHealth(stopped,start.AddSeconds(-1),start) ||
            IsStoppedHealth("{\"microphone\":\"ready\"}",start.AddSeconds(1),start) ||
            IsStoppedHealth("{\"microphone\":\"unavailable\"}",start.AddSeconds(1),start))
            throw new InvalidOperationException("Startup exit lifecycle check failed");
    }

    [STAThread]
    private static int Main(string[] options)
    {
        SetProcessDPIAware(); Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        // These authored visual-only flags cannot start the listener or a skill.
        if(options.Length==1 && options[0]=="--check") { HologramRenderer.Check(); CheckLifecycle(); return 0; }
        if(options.Length==2 && options[0]=="--render-frames") { HologramRenderer.RenderFrames(options[1]); return 0; }
        if(options.Length==1 && (options[0]=="--preview" || options[0]=="--preview-hold"))
        {
            using(HologramStartupForm preview=new HologramStartupForm(null,options[0]=="--preview-hold")) Application.Run(preview);
            return 0;
        }
        if(options.Length!=0) return 2;
        string directory=AppDomain.CurrentDomain.BaseDirectory;
        string runtime=Path.Combine(directory,"runtime"); Directory.CreateDirectory(runtime);
        string statusPath=Path.Combine(runtime,"interactive_launcher_status.txt");
        bool first;
        using(Mutex instance=new Mutex(true,"Local\\JarvisHolographicStartup",out first))
        {
            if(!first) return 0;
            try
            {
                string python=Path.Combine(directory,".voice-venv","Scripts","pythonw.exe");
#if PREVIEW
                string script=Path.Combine(directory,"jarvis_overlay.py");
                string arguments="\""+script+"\" --status listening --message \"\" --duration-ms 45000";
#else
                string script=Path.Combine(directory,"desktop_controller.py");
                string arguments="\""+script+"\" --hologram-startup";
#if !LIVE
                arguments+=" --dry-run";
#endif
#endif
                DateTime launchedAt=DateTime.UtcNow;
                Process process=Process.Start(new ProcessStartInfo(python,arguments)
                { WorkingDirectory=directory, UseShellExecute=false, CreateNoWindow=true });
                File.WriteAllText(statusPath,DateTime.Now.ToString("O")+" desktop="+DesktopName()+" child_pid="+process.Id+Environment.NewLine,Encoding.UTF8);
                using(HologramStartupForm splash=new HologramStartupForm(
                    delegate { return ListenerReady(directory); },false,
                    delegate { return ListenerStopped(directory,launchedAt); })) Application.Run(splash);
                return 0;
            }
            catch(Exception error)
            {
                File.WriteAllText(statusPath,DateTime.Now.ToString("O")+" error="+error.GetType().Name+Environment.NewLine,Encoding.UTF8);
                return 1;
            }
        }
    }
}
