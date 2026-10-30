// Comprueba el núcleo con el mismo fixture que tests/test_core.py. Uso: Tests.exe <carpeta tests/fixtures>

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Threading.Tasks;
using System.Text;

namespace OracleManager
{
    static class Tests
    {
        static int failures;

        static void Check(string what, string expected, string actual)
        {
            if (expected == actual) return;
            failures++;
            Console.WriteLine("FALLA " + what + "\n  esperado: " + expected + "\n  obtenido: " + actual);
        }

        static int Keygen(params string[] args)
        {
            using (var p = Proc.Start(new[] { "ssh-keygen" }.Concat(args).ToList(), null))
            {
                p.StandardInput.Close();
                var err = p.StandardError.ReadToEndAsync();
                var o = p.StandardOutput.ReadToEndAsync();
                p.WaitForExit();
                Task.WaitAll(err, o);
                return p.ExitCode;
            }
        }

        static int Main(string[] args)
        {
            var dir = args[0];
            var i = Parser.Parse(File.ReadAllText(Path.Combine(dir, "sample.txt"), Encoding.UTF8))[0];
            Check("errores", "ORA-00942: table or view does not exist", string.Join("|", i.Errors));
            Check("cdb", "True", i.Cdb.ToString());
            var rows = i.Containers.SelectMany(c => c.Users.Select(u => string.Join("\t", c.ConId.ToString(), u.Name,
                u.OracleMaintained ? "Y" : "N", u.Level, Usage.IdleText(u), Usage.IdleSort(u).ToString()))).ToList();
            var expected = File.ReadAllText(Path.Combine(dir, "sample.expected.tsv"), Encoding.UTF8).Replace("\r", "").TrimEnd('\n').Split('\n');
            Check("fixture", string.Join("\n", expected), string.Join("\n", rows));
            Check("sesiones", "cliente/2", string.Join("", i.Containers[1].Users.First(u => u.Name == "APP").Clients.Select(c => c.Machine + "/" + c.Count)));

            Check("comillas", "\"a b\"", Proc.Quote("a b"));
            Check("comillas vacías", "\"\"", Proc.Quote(""));
            Check("comillas internas", "\"a\\\"b\"", Proc.Quote("a\"b"));
            Check("barra final", "\"c:\\a b\\\\\"", Proc.Quote("c:\\a b\\"));
            Check("sh", "'a'\"'\"'b'", Proc.ShQuote("a'b"));

            var agent = string.Join(" ", new Ssh { Host = "h" }.Command());
            Check("agente", "ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o BatchMode=yes root@h bash -s", agent);
            var sudo = new Ssh { Host = "h", User = "admin", Port = 2222, Password = "x" }.Command("cat");
            Check("sudo", "sudo -n cat", sudo[sudo.Count - 1]);
            Check("contraseña sin BatchMode", "False", sudo.Contains("BatchMode=yes").ToString());

            foreach (var bad in new[] { "-oProxyCommand=x", "a b", "" })
            {
                try
                {
                    new Machine { Host = bad }.Validate();
                    Check("host rechazado: " + bad, "excepción", "aceptado");
                }
                catch (ArgumentException) { }
            }
            // Con OpenSSH de verdad: rechaza la clave abierta a «Todos» y la acepta tras restringirla.
            var keyDir = Path.Combine(Path.GetTempPath(), "oracle-manager-test-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(keyDir);
            try
            {
                var key = Path.Combine(keyDir, "key");
                Keygen("-q", "-t", "ed25519", "-N", "", "-f", key);
                var open = File.GetAccessControl(key);
                open.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.WorldSid, null), FileSystemRights.Read, AccessControlType.Allow));
                File.SetAccessControl(key, open);
                Check("clave abierta rechazada", "True", (Keygen("-y", "-P", "", "-f", key) != 0).ToString());
                Discovery.RestrictKey(key);
                Check("clave restringida aceptada", "0", Keygen("-y", "-P", "", "-f", key).ToString());
            }
            finally
            {
                Directory.Delete(keyDir, true);
            }
            Console.WriteLine(failures == 0 ? "Todo correcto" : failures + " fallos");
            return failures == 0 ? 0 : 1;
        }
    }
}
