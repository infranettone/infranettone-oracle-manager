// El .exe es su propio instalador, por usuario y sin administrador: un MSI o Inno Setup añadirían herramientas y peso
// para hacer solo esto. El menú Inicio es lo que encuentra el buscador de Windows; la clave Uninstall, lo que lo lista
// en Aplicaciones instaladas.

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using Microsoft.Win32;

namespace OracleManager
{
    static class Installer
    {
        const string AppName = "Oracle Manager";
        const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\InfranettoneOracleManager";

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        static extern bool DeleteFile(string path);

        static string Folder { get { return Path.Combine(Environment.GetEnvironmentVariable("LOCALAPPDATA"), "Programs", AppName); } }
        static string InstalledExe { get { return Path.Combine(Folder, "OracleManager.exe"); } }
        static string Shortcut
        {
            get { return Path.Combine(Environment.GetEnvironmentVariable("APPDATA"), @"Microsoft\Windows\Start Menu\Programs", AppName + ".lnk"); }
        }
        static string Current { get { return Assembly.GetEntryAssembly().Location; } }

        // Al abrirlo sin argumentos. true si ya se ha lanzado la copia instalada y este proceso debe terminar.
        public static bool OfferInstall()
        {
            if (string.Equals(Path.GetFullPath(Current), Path.GetFullPath(InstalledExe), StringComparison.OrdinalIgnoreCase)) return false;
            var version = Application.ProductVersion;
            if (!File.Exists(InstalledExe))
            {
                if (MessageBox.Show("¿Instalar " + AppName + " " + version + "?\n\nAparecerá en el menú Inicio y en el buscador de Windows. No necesita permisos de administrador.\n\nSi eliges No, se abre sin instalar.",
                    AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return false;
                return InstallAndOpen();
            }
            var installed = FileVersionInfo.GetVersionInfo(InstalledExe).ProductVersion;
            var same = installed == version;
            var text = same ? AppName + " " + version + " ya está instalado." : "Tienes instalada la versión " + installed + " y esta es la " + version + ".";
            switch (Ask(text, same ? null : "Sustituir la instalada", "Abrir", "Desinstalar"))
            {
                case 0: return InstallAndOpen();
                case 1:
                    if (same) { Process.Start(InstalledExe); return true; }
                    return false; // esta versión, sin instalar
                case 2: Uninstall(false); return true;
                default: return true;
            }
        }

        static bool InstallAndOpen()
        {
            if (!Install(false)) return false;
            Process.Start(InstalledExe);
            return true;
        }

        // Devuelve el índice del botón pulsado, o -1 si se cierra.
        static int Ask(string text, params string[] choices)
        {
            using (var f = Ui.Dialog(AppName))
            {
                var t = Ui.Grid(1);
                t.Controls.Add(Ui.Text(text, 420));
                var p = new FlowLayoutPanel { AutoSize = true, Margin = new Padding(0, Ui.S(10), 0, 0) };
                var result = -1;
                for (var n = 0; n < choices.Length; n++)
                {
                    if (choices[n] == null) continue;
                    var index = n;
                    var b = new Button { Text = choices[n], AutoSize = true, MinimumSize = new Size(Ui.S(90), 0) };
                    b.Click += delegate { result = index; f.Close(); };
                    p.Controls.Add(b);
                }
                t.Controls.Add(p);
                f.Controls.Add(t);
                f.ShowInTaskbar = true;
                f.StartPosition = FormStartPosition.CenterScreen;
                f.ShowDialog();
                return result;
            }
        }

        // Conservar es lo marcado por defecto: borrar las máquinas guardadas hay que pedirlo.
        static bool? AskUninstall()
        {
            using (var f = Ui.Dialog("Desinstalar " + AppName))
            {
                var t = Ui.Grid(1);
                var keep = new RadioButton { Text = "Conservar mis datos (máquinas guardadas y preferencias)", AutoSize = true, Checked = true };
                var erase = new RadioButton { Text = "Borrar también mis datos", AutoSize = true };
                t.Controls.Add(Ui.Text("¿Desinstalar " + AppName + "?"));
                t.Controls.Add(keep);
                t.Controls.Add(erase);
                var buttons = Ui.Buttons(f, "Desinstalar");
                t.Controls.Add(buttons);
                f.Controls.Add(t);
                f.ShowInTaskbar = true;
                f.StartPosition = FormStartPosition.CenterScreen;
                if (f.ShowDialog() != DialogResult.OK) return null;
                return erase.Checked;
            }
        }

        public static bool Install(bool silent)
        {
            try
            {
                Directory.CreateDirectory(Folder);
                File.Copy(Current, InstalledExe, true);
                // La marca «descargado de Internet» se copia con el archivo: SmartScreen ya se pasó al abrir el descargado.
                DeleteFile(InstalledExe + ":Zone.Identifier");
                CreateShortcut();
                Register();
                return true;
            }
            catch (Exception e)
            {
                if (!(e is IOException || e is UnauthorizedAccessException)) throw;
                if (!silent)
                    MessageBox.Show("No se pudo instalar. Si " + AppName + " está abierto, ciérralo y vuelve a intentarlo.\n\n" + e.Message,
                        AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
                return false;
            }
        }

        // Por defecto no toca %APPDATA%\infranettone-oracle, como apt remove; con `purge`, sí (solo esa carpeta).
        public static void Uninstall(bool silent, bool purge = false)
        {
            if (!silent)
            {
                var answer = AskUninstall();
                if (!answer.HasValue) return;
                purge = answer.Value;
            }
            if (purge)
            {
                var data = Path.Combine(Environment.GetEnvironmentVariable("APPDATA"), "infranettone-oracle");
                if (Directory.Exists(data)) Directory.Delete(data, true);
            }
            File.Delete(Shortcut);
            Registry.CurrentUser.DeleteSubKeyTree(UninstallKey, false);
            // Un .exe en marcha no puede borrarse: lo hace cmd cuando este proceso ya ha salido.
            var inFolder = Path.GetDirectoryName(Path.GetFullPath(Current)).Equals(Path.GetFullPath(Folder), StringComparison.OrdinalIgnoreCase);
            Process.Start(new ProcessStartInfo("cmd.exe", "/c ping -n 3 127.0.0.1 >nul & rmdir /s /q \"" + Folder + "\"")
            {
                UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = inFolder ? Path.GetTempPath() : Environment.CurrentDirectory
            });
        }

        static void CreateShortcut()
        {
            Directory.CreateDirectory(Path.GetDirectoryName(Shortcut));
            // WScript.Shell por reflexión: viene en Windows y evita generar interops.
            var type = Type.GetTypeFromProgID("WScript.Shell");
            var shell = Activator.CreateInstance(type);
            var link = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { Shortcut });
            var linkType = link.GetType();
            linkType.InvokeMember("TargetPath", BindingFlags.SetProperty, null, link, new object[] { InstalledExe });
            linkType.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, link, new object[] { Folder });
            linkType.InvokeMember("Description", BindingFlags.SetProperty, null, link, new object[] { "Bases de datos Oracle y sus usuarios/esquemas, por SSH" });
            linkType.InvokeMember("Save", BindingFlags.InvokeMethod, null, link, null);
        }

        static void Register()
        {
            using (var key = Registry.CurrentUser.CreateSubKey(UninstallKey))
            {
                key.SetValue("DisplayName", AppName);
                key.SetValue("DisplayVersion", Application.ProductVersion);
                key.SetValue("Publisher", "Raul Adamuz");
                key.SetValue("DisplayIcon", InstalledExe);
                key.SetValue("InstallLocation", Folder);
                key.SetValue("UninstallString", "\"" + InstalledExe + "\" --uninstall");
                key.SetValue("URLInfoAbout", "https://github.com/infranettone/infranettone-oracle-manager");
                key.SetValue("EstimatedSize", (int)(new FileInfo(InstalledExe).Length / 1024), RegistryValueKind.DWord);
                key.SetValue("NoModify", 1, RegistryValueKind.DWord);
                key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
            }
        }
    }
}
