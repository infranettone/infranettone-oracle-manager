using System;
using System.Text;
using System.Windows.Forms;

namespace OracleManager
{
    static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            // ssh nos ejecuta como SSH_ASKPASS: la contraseña llega por el entorno y sale por stdout.
            var secret = Environment.GetEnvironmentVariable(Ssh.SecretVar);
            if (secret != null)
            {
                var bytes = new UTF8Encoding(false).GetBytes(secret + "\n");
                using (var o = Console.OpenStandardOutput()) o.Write(bytes, 0, bytes.Length);
                return 0;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            var silent = Array.IndexOf(args, "--silent") >= 0;
            if (Array.IndexOf(args, "--install") >= 0) return Installer.Install(silent) ? 0 : 1;
            if (Array.IndexOf(args, "--uninstall") >= 0)
            {
                Installer.Uninstall(silent, Array.IndexOf(args, "--purge") >= 0);
                return 0;
            }
            if (Installer.OfferInstall()) return 0;
            Application.Run(new MainForm());
            return 0;
        }
    }
}
