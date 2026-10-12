// Tiny native launcher for the OpenReview crawler GUI.
//
// Why a C# launcher instead of PyInstaller: PyInstaller has to be installed and
// produces a 10+ MB bundle that antivirus software likes to quarantine. The C#
// compiler (csc.exe) ships with every Windows installation, so this builds in
// under a second, is about 8 KB, and needs no downloads. It does require the
// user's existing Python 3 -- which the crawler needs anyway.
//
// Compiled twice from this one file:
//   csc /target:winexe /out:OpenReview爬虫.exe launcher.cs
//   csc /target:winexe /define:LOGIN /out:OpenReview登录.exe launcher.cs
//
// Behaviour notes:
//  * pythonw.exe is used, so no console window flashes up;
//  * --self-test / --wait make the launcher wait for the child and return its
//    exit code, which is what makes the frozen exe testable from a script.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

internal static class OpenReviewLauncher
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    private const uint MB_ICONERROR = 0x10;

    [STAThread]
    private static int Main(string[] args)
    {
#if LOGIN
        // The login-only build opens straight into the credential prompt.
        string[] builtIn = { "--login-only" };
        string scriptName = "app_gui.py";
#elif CLEANUP
        // The cleanup build is its own small window.
        string[] builtIn = { };
        string scriptName = "cleanup_gui.py";
#else
        string[] builtIn = { };
        string scriptName = "app_gui.py";
#endif

        string baseDir;
        try
        {
            baseDir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        }
        catch (Exception)
        {
            baseDir = Directory.GetCurrentDirectory();
        }
        if (string.IsNullOrEmpty(baseDir))
        {
            baseDir = Directory.GetCurrentDirectory();
        }

        string script = Path.Combine(baseDir, Path.Combine("tools", scriptName));
        if (!File.Exists(script))
        {
            Fail("找不到界面程序：\n" + script +
                 "\n\n请把本 exe 放在项目根目录（与 crawl.py 同级）。");
            return 2;
        }

        string interpreter = FindInterpreter(baseDir);
        if (interpreter == null)
        {
            Fail("没有找到 Python 3。\n\n" +
                 "请先安装 Python 3 并在安装时勾选 Add python.exe to PATH；\n" +
                 "或者在本目录下新建 python-path.txt，把 pythonw.exe 的完整路径写进去。");
            return 3;
        }

        List<string> finalArgs = new List<string>();
        finalArgs.Add(Quote(script));
        foreach (string a in builtIn) finalArgs.Add(a);
        foreach (string a in args) finalArgs.Add(Quote(a));

        bool waitForExit = false;
        foreach (string a in args)
        {
            if (a == "--self-test" || a == "--wait") { waitForExit = true; }
        }

        ProcessStartInfo psi = new ProcessStartInfo(interpreter);
        psi.Arguments = string.Join(" ", finalArgs.ToArray());
        psi.WorkingDirectory = baseDir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;

        try
        {
            using (Process child = Process.Start(psi))
            {
                if (child == null)
                {
                    Fail("无法启动：" + interpreter);
                    return 4;
                }
                if (waitForExit)
                {
                    child.WaitForExit();
                    return child.ExitCode;
                }
            }
        }
        catch (Exception ex)
        {
            Fail("启动失败：\n" + ex.Message);
            return 5;
        }
        return 0;
    }

    /// <summary>pythonw.exe, so nothing flashes a console window.</summary>
    private static string FindInterpreter(string baseDir)
    {
        // 1. An explicit override beside the exe. Also lets a portable Python
        //    live anywhere, including outside PATH.
        string overrideFile = Path.Combine(baseDir, "python-path.txt");
        try
        {
            if (File.Exists(overrideFile))
            {
                string pinned = File.ReadAllText(overrideFile, Encoding.UTF8).Trim();
                if (pinned.Length > 0 && File.Exists(pinned))
                {
                    return pinned;
                }
            }
        }
        catch (Exception) { /* fall through to discovery */ }

        // 2. Next to python.exe on PATH.
        string pathVar = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
        foreach (string raw in pathVar.Split(';'))
        {
            string folder = raw.Trim().Trim('"');
            if (folder.Length == 0) continue;
            string found = Probe(folder);
            if (found != null) return found;
        }

        // 3. Per-user installs, newest first.
        try
        {
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string programs = Path.Combine(Path.Combine(local, "Programs"), "Python");
            if (Directory.Exists(programs))
            {
                List<string> dirs = new List<string>(Directory.GetDirectories(programs, "Python3*"));
                dirs.Sort();
                dirs.Reverse();
                foreach (string dir in dirs)
                {
                    string candidate = Path.Combine(dir, "pythonw.exe");
                    if (File.Exists(candidate)) return candidate;
                }
            }
        }
        catch (Exception) { /* ignore and keep looking */ }

        // 4. Classic all-users install locations.
        string[] roots = { @"C:\", @"C:\Program Files", @"C:\Program Files (x86)" };
        foreach (string root in roots)
        {
            string found = Probe(root);
            if (found != null) return found;
        }

        // 5. The py launcher, which knows where Python lives.
        foreach (string raw in pathVar.Split(';'))
        {
            string folder = raw.Trim().Trim('"');
            if (folder.Length == 0) continue;
            string pyw = Path.Combine(folder, "pyw.exe");
            if (File.Exists(pyw)) return pyw;
        }
        return null;
    }

    private static string Probe(string folder)
    {
        try
        {
            string pythonw = Path.Combine(folder, "pythonw.exe");
            if (File.Exists(pythonw)) return pythonw;
            // A folder holding python.exe but no pythonw.exe: fall back to
            // python.exe, which only costs a console window.
            string python = Path.Combine(folder, "python.exe");
            if (File.Exists(python)) return python;
        }
        catch (Exception) { /* unreadable folder */ }
        return null;
    }

    private static string Quote(string value)
    {
        if (value.Length > 0 && value.IndexOf(' ') < 0 && value.IndexOf('"') < 0)
        {
            return value;
        }
        return "\"" + value.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
    }

    private static void Fail(string message)
    {
        try
        {
            MessageBoxW(IntPtr.Zero, message, "OpenReview 审稿爬虫", MB_ICONERROR);
        }
        catch (Exception)
        {
            Console.Error.WriteLine(message);
        }
    }
}
