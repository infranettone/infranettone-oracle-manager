// Núcleo de la app de Windows: lo mismo que discover.py, usage.py, machines.py y dump.py. Los scripts remotos no se
// duplican (oracle_manager/remote/*.sh, incrustados en el .exe) y tests/fixtures comprueba que ambos analizan igual.
// C# 5 a propósito: es lo que compila el csc.exe que trae Windows, sin SDK ni NuGet.

using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace OracleManager
{
    public class Client
    {
        public string Machine, Program, OsUser, Since;
        public int Count;
    }

    public class User
    {
        public string Name, Status, Tablespace, Created;
        public bool OracleMaintained;
        public long Objects, SizeMb;
        public string LastLogin = "", SqlLast = "", DmlLast = "", DdlLast = "";
        public long Sessions, ActiveSessions, SqlExecs, LogicalReads, PhysicalReads, BlockChanges, Dml;
        public List<Client> Clients = new List<Client>();
        // Calculado en Usage.Classify
        public string LastUse = "";
        public int? IdleDays;
        public string Level = "";
    }

    public class Container
    {
        public int ConId;
        public string Name, OpenMode;
        public List<User> Users = new List<User>();
    }

    public class Instance
    {
        public string Sid, OracleHome, ContainerName;
        public string DbName = "", UniqueName = "", OpenMode = "", LogMode = "", Version = "", HostName = "", Started = "",
            Now = "", Command = "";
        public bool Cdb;
        public List<Container> Containers = new List<Container>();
        public List<string> Errors = new List<string>();
    }

    public static class Parser
    {
        public static List<Instance> Parse(string output)
        {
            var instances = new List<Instance>();
            foreach (var raw in output.Split('\n'))
            {
                var line = raw.TrimEnd('\r');
                var tab = line.IndexOf('\t');
                var tag = tab < 0 ? line : line.Substring(0, tab);
                var rest = tab < 0 ? "" : line.Substring(tab + 1);
                var f = rest.Split('\t');
                if (tag == "INST" && f.Length == 3)
                {
                    instances.Add(new Instance { Sid = f[0], OracleHome = f[1], ContainerName = f[2] });
                    continue;
                }
                if (instances.Count == 0) continue;
                var i = instances[instances.Count - 1];
                if (tag == "CMD" && rest != "") i.Command = rest;
                else if (tag == "DB" && f.Length == 9)
                {
                    i.DbName = f[0]; i.UniqueName = f[1]; i.Cdb = f[2] == "YES"; i.OpenMode = f[3]; i.LogMode = f[4];
                    i.Version = f[5]; i.HostName = f[6]; i.Started = f[7]; i.Now = f[8];
                }
                else if (tag == "ACT" && f.Length == 13)
                {
                    var u = FindUser(i, f);
                    if (u == null) continue;
                    u.LastLogin = f[2]; u.Sessions = long.Parse(f[3]); u.ActiveSessions = long.Parse(f[4]);
                    u.SqlExecs = long.Parse(f[5]); u.SqlLast = f[6]; u.LogicalReads = long.Parse(f[7]);
                    u.PhysicalReads = long.Parse(f[8]); u.BlockChanges = long.Parse(f[9]); u.Dml = long.Parse(f[10]);
                    u.DmlLast = f[11]; u.DdlLast = f[12];
                }
                else if (tag == "SES" && f.Length == 7)
                {
                    var u = FindUser(i, f);
                    if (u != null)
                        u.Clients.Add(new Client { Machine = f[2], Program = f[3], OsUser = f[4], Count = int.Parse(f[5]), Since = f[6] });
                }
                else if (tag == "CON" && f.Length == 3)
                    i.Containers.Add(new Container { ConId = int.Parse(f[0]), Name = f[1], OpenMode = f[2] });
                else if (tag == "USR" && f.Length == 8)
                {
                    var id = int.Parse(f[0]);
                    var con = i.Containers.FirstOrDefault(c => c.ConId == id);
                    if (con == null) // por si v$containers falla: todo va a la propia base
                    {
                        con = new Container { ConId = id, Name = i.DbName, OpenMode = i.OpenMode };
                        i.Containers.Add(con);
                    }
                    con.Users.Add(new User
                    {
                        Name = f[1], Status = f[2], Tablespace = f[3], Created = f[4], OracleMaintained = f[5] == "Y",
                        Objects = long.Parse(f[6]), SizeMb = long.Parse(f[7])
                    });
                }
                else if (line.StartsWith("ORA-") || line.StartsWith("SP2-") || line.StartsWith("ERROR"))
                    i.Errors.Add(line.Trim());
            }
            foreach (var i in instances)
            {
                foreach (var c in i.Containers)
                {
                    // oracle_maintained también queda en «Y» con «_ORACLE_SCRIPT»=true. Los de verdad nacen el mismo día que SYS.
                    var sys = c.Users.FirstOrDefault(u => u.Name == "SYS");
                    if (sys == null) continue;
                    foreach (var u in c.Users)
                        if (u.OracleMaintained && u.Created != sys.Created) u.OracleMaintained = false;
                }
                Usage.Classify(i);
            }
            return instances;
        }

        static User FindUser(Instance i, string[] f)
        {
            int id;
            if (!int.TryParse(f[0], out id)) return null;
            var con = i.Containers.FirstOrDefault(c => c.ConId == id);
            return con == null ? null : con.Users.FirstOrDefault(u => u.Name == f[1]);
        }
    }

    public static class Usage
    {
        public const int UnusedDays = 90;
        public static readonly string[] Levels = { "Sin uso", "Bajo", "Medio", "Alto" };
        const string Fmt = "yyyy-MM-dd HH:mm";

        static DateTime? Date(string s)
        {
            DateTime d;
            return DateTime.TryParseExact(s, Fmt, CultureInfo.InvariantCulture, DateTimeStyles.None, out d) ? d : (DateTime?)null;
        }

        static double Score(User u)
        {
            return Math.Log10(1 + u.LogicalReads) + Math.Log10(1 + u.BlockChanges) + Math.Log10(1 + u.SqlExecs)
                + 2 * Math.Log10(1 + u.Sessions);
        }

        public static void Classify(Instance i)
        {
            var now = Date(i.Now) ?? DateTime.Now;
            var active = new List<User>();
            foreach (var u in i.Containers.SelectMany(c => c.Users))
            {
                var dates = new[] { u.LastLogin, u.SqlLast, u.DmlLast }.Select(Date).Where(d => d.HasValue).Select(d => d.Value).ToList();
                DateTime? last = u.Sessions > 0 ? now : dates.Count > 0 ? dates.Max() : (DateTime?)null;
                u.LastUse = last.HasValue ? last.Value.ToString(Fmt, CultureInfo.InvariantCulture) : "";
                u.IdleDays = last.HasValue ? Math.Max(0, (now - last.Value).Days) : (int?)null;
                if (HasActivity(u)) active.Add(u);
                else u.Level = u.IdleDays.HasValue && u.IdleDays.Value <= UnusedDays ? "Bajo" : "Sin uso";
            }
            // Los de Oracle siempre tienen mucha actividad: se clasifican aparte para no desplazar al resto.
            foreach (var maintained in new[] { false, true })
            {
                var ranked = active.Where(u => u.OracleMaintained == maintained).OrderBy(Score).ToList();
                for (var n = 0; n < ranked.Count; n++)
                    ranked[n].Level = Levels[1 + Math.Min(2, 3 * n / ranked.Count)];
            }
        }

        public static bool HasActivity(User u)
        {
            return u.Sessions != 0 || u.SqlExecs != 0 || u.LogicalReads != 0 || u.BlockChanges != 0;
        }

        public static int IdleSort(User u)
        {
            if (u.Sessions != 0) return -1;
            if (u.IdleDays.HasValue) return u.IdleDays.Value;
            return HasActivity(u) ? 1000000 : 10000000;
        }

        public static string IdleText(User u)
        {
            if (u.Sessions != 0) return "ahora";
            if (!u.IdleDays.HasValue) return HasActivity(u) ? "desde el arranque" : "nunca";
            var d = u.IdleDays.Value;
            if (d == 0) return "hoy";
            if (d < 60) return "hace " + d + " d";
            if (d < 730) return "hace " + d / 30 + " meses";
            return "hace " + d / 365 + " años";
        }
    }

    public static class Scripts
    {
        // Sin CR aunque el archivo llegue con CRLF: bash se rompería.
        public static string Load(string name)
        {
            using (var s = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
            using (var r = new StreamReader(s, new UTF8Encoding(false)))
                return r.ReadToEnd().Replace("\r\n", "\n");
        }
    }

    public class Ssh
    {
        public const string SecretVar = "ORACLE_MANAGER_SSH_PASSWORD";
        public string Host, User = "root", Identity, Certificate, Password, Passphrase;
        public int Port;

        // Siempre sin terminal (interfaz): equivale a tty=False en discover.py.
        public List<string> Command(string remote = "bash -s")
        {
            var cmd = new List<string> { "ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new" };
            if (Password != null)
                cmd.AddRange(new[] { "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
                    "-o", "NumberOfPasswordPrompts=1" });
            else if (Passphrase == null)
                cmd.AddRange(new[] { "-o", "BatchMode=yes" });
            if (Port != 0) cmd.AddRange(new[] { "-p", Port.ToString() });
            if (!string.IsNullOrEmpty(Identity))
                cmd.AddRange(new[] { "-o", "IdentitiesOnly=yes", "-o", "PreferredAuthentications=publickey", "-i", Proc.Expand(Identity) });
            if (!string.IsNullOrEmpty(Certificate))
                cmd.AddRange(new[] { "-o", "CertificateFile=" + Proc.Expand(Certificate) });
            cmd.Add(string.IsNullOrEmpty(User) ? Host : User + "@" + Host);
            cmd.Add(User == "root" ? remote : "sudo -n " + remote);
            return cmd;
        }

        public void ApplyEnv(ProcessStartInfo psi)
        {
            var secret = Password ?? Passphrase;
            if (secret == null) return;
            // El propio .exe hace de SSH_ASKPASS (ver Program.Main): no hay que escribir ningún script ni pasar la
            // contraseña por las reglas de comillas de cmd.exe.
            psi.EnvironmentVariables.Remove("SSH_AUTH_SOCK");
            psi.EnvironmentVariables["SSH_ASKPASS"] = Assembly.GetEntryAssembly().Location;
            psi.EnvironmentVariables["SSH_ASKPASS_REQUIRE"] = "force";
            psi.EnvironmentVariables[SecretVar] = secret;
        }

        public string Error(string stderr, int code)
        {
            // Los avisos de OpenSSH (p. ej. el de intercambio de claves post-cuántico) empiezan por «**».
            var text = string.Join("\n", stderr.Replace("\r", "").Split('\n').Where(l => !l.StartsWith("**"))).Trim();
            return text != "" ? text : "ssh terminó con código " + code;
        }
    }

    public static class Proc
    {
        public static string Expand(string path)
        {
            if (path == "~" || path.StartsWith("~/") || path.StartsWith("~\\"))
                return Environment.GetFolderPath(Environment.SpecialFolder.UserProfile) + path.Substring(1);
            return path;
        }

        // Reglas de CommandLineToArgvW: .NET Framework no tiene ArgumentList.
        public static string Quote(string arg)
        {
            if (arg != "" && arg.IndexOfAny(new[] { ' ', '\t', '"' }) < 0) return arg;
            return "\"" + Regex.Replace(Regex.Replace(arg, @"(\\*)""", "$1$1\\\""), @"(\\+)$", "$1$1") + "\"";
        }

        public static Process Start(List<string> cmd, Ssh ssh)
        {
            var psi = new ProcessStartInfo(cmd[0], string.Join(" ", cmd.Skip(1).Select(Quote)))
            {
                UseShellExecute = false, CreateNoWindow = true, RedirectStandardInput = true,
                RedirectStandardOutput = true, RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
            };
            if (ssh != null) ssh.ApplyEnv(psi);
            try
            {
                return Process.Start(psi);
            }
            catch (Win32Exception)
            {
                throw new ApplicationException("No encuentro " + cmd[0] + ": activa «Cliente OpenSSH» en Configuración > Sistema > Características opcionales");
            }
        }

        // .NET Framework no deja elegir la codificación de stdin: se escriben los bytes (UTF-8, sin BOM).
        public static void WriteInput(Process p, string text)
        {
            try
            {
                var bytes = new UTF8Encoding(false).GetBytes(text);
                p.StandardInput.BaseStream.Write(bytes, 0, bytes.Length);
                p.StandardInput.Close();
            }
            catch (IOException) { } // ssh ya ha salido (p. ej. no pudo entrar): el error llega por stderr
        }

        public static void Quiet(List<string> cmd, Ssh ssh, Stream output = null)
        {
            using (var p = Start(cmd, ssh))
            {
                p.StandardInput.Close();
                var err = p.StandardError.ReadToEndAsync();
                var copy = p.StandardOutput.BaseStream.CopyToAsync(output ?? Stream.Null);
                if (!p.WaitForExit(60000)) { try { p.Kill(); } catch (InvalidOperationException) { } return; }
                Task.WaitAll(err, copy);
                if (p.ExitCode != 0 && output != null) output.SetLength(0);
            }
        }

        // Comillas simples para el shell remoto, como shlex.quote.
        public static string ShQuote(string s)
        {
            return Regex.IsMatch(s, @"^[A-Za-z0-9_@%+=:,./-]+\z") ? s : "'" + s.Replace("'", "'\"'\"'") + "'";
        }
    }

    public static class Discovery
    {
        public static List<Instance> Discover(Ssh ssh)
        {
            using (var p = Proc.Start(ssh.Command(), ssh))
            {
                var stdout = p.StandardOutput.ReadToEndAsync();
                var stderr = p.StandardError.ReadToEndAsync();
                Proc.WriteInput(p, Scripts.Load("discover.sh"));
                if (!p.WaitForExit(300000))
                {
                    try { p.Kill(); } catch (InvalidOperationException) { }
                    throw new ApplicationException(ssh.Host + " no ha terminado en 300 s");
                }
                Task.WaitAll(stdout, stderr);
                var instances = Parser.Parse(stdout.Result);
                if (p.ExitCode != 0 && instances.Count == 0)
                    throw new ApplicationException(ssh.Error(stderr.Result, p.ExitCode));
                return instances;
            }
        }

        // OpenSSH ignora una clave que otros puedan leer. Deja el archivo solo para el usuario actual, sin heredar nada.
        public static void RestrictKey(string path)
        {
            var security = new FileSecurity();
            security.SetAccessRuleProtection(true, false);
            security.AddAccessRule(new FileSystemAccessRule(WindowsIdentity.GetCurrent().User, FileSystemRights.FullControl, AccessControlType.Allow));
            File.SetAccessControl(Proc.Expand(path), security);
        }

        public static bool KeyNeedsPassphrase(string path)
        {
            using (var p = Proc.Start(new List<string> { "ssh-keygen", "-y", "-P", "", "-f", Proc.Expand(path) }, null))
            {
                p.StandardInput.Close();
                var err = p.StandardError.ReadToEndAsync();
                var o = p.StandardOutput.ReadToEndAsync();
                if (!p.WaitForExit(10000)) { try { p.Kill(); } catch (InvalidOperationException) { } return false; }
                Task.WaitAll(err, o);
                return p.ExitCode != 0 && err.Result.ToLowerInvariant().Contains("passphrase");
            }
        }
    }

    public static class Dump
    {
        static readonly Regex Ident = new Regex(@"^[A-Z][A-Z0-9_$#]*\z");
        static readonly Regex SidRe = new Regex(@"^[A-Za-z0-9_$#]+\z");

        [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
        static extern int SHGetKnownFolderPath([MarshalAs(UnmanagedType.LPStruct)] Guid id, uint flags, IntPtr token, out IntPtr path);

        public static string DefaultFolder()
        {
            IntPtr p;
            // FOLDERID_Downloads: puede estar redirigida, no se da por hecho que sea %USERPROFILE%\Downloads.
            if (SHGetKnownFolderPath(new Guid("374DE290-123F-4565-9164-39C4925E467B"), 0, IntPtr.Zero, out p) == 0)
            {
                var s = Marshal.PtrToStringUni(p);
                Marshal.FreeCoTaskMem(p);
                return s;
            }
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Downloads");
        }

        // Devuelve la ruta local del .dmp; `warning`, si expdp terminó con avisos. progress: texto y fracción (null = sin medida).
        public static string ExportSchema(Ssh ssh, string sid, string pdb, string schema, string folder, string db,
            Action<string, double?> progress, out string warning)
        {
            if (!Ident.IsMatch(schema))
                throw new ApplicationException("No se puede exportar «" + schema + "»: el nombre lleva caracteres que expdp no acepta sin comillas");
            if (!SidRe.IsMatch(sid) || (pdb != "" && !Ident.IsMatch(pdb)))
                throw new ApplicationException("SID o PDB no válidos");
            var dest = Proc.Expand(folder);
            if (!Directory.Exists(dest)) throw new ApplicationException("No existe la carpeta " + dest);
            var name = string.Join("_", new[] { schema, pdb != "" ? pdb : db, DateTime.Now.ToString("yyyyMMdd-HHmmss") }.Where(x => x != ""));
            var header = "SID='" + sid + "'\nPDB='" + pdb + "'\nSCHEMA='" + schema + "'\nNAME='" + name + "'\n";

            progress("Conectando con " + ssh.Host + "…", null);
            var tags = new Dictionary<string, string[]>();
            var tail = new List<string>();
            int code;
            string stderrText;
            using (var p = Proc.Start(ssh.Command(), ssh))
            {
                var stderr = p.StandardError.ReadToEndAsync();
                Proc.WriteInput(p, header + Scripts.Load("dump.sh"));
                string line;
                while ((line = p.StandardOutput.ReadLine()) != null)
                {
                    var tab = line.IndexOf('\t');
                    var tag = tab < 0 ? line : line.Substring(0, tab);
                    var rest = tab < 0 ? "" : line.Substring(tab + 1);
                    if (tag == "STEP" || tag == "ERR" || tag == "RC" || tag == "FILE" || tag == "LOG")
                    {
                        tags[tag] = rest.Split('\t');
                        if (tag == "STEP") progress(rest, null);
                    }
                    else if (line.Trim() != "")
                    {
                        tail.Add(line.Trim());
                        if (tail.Count > 8) tail.RemoveAt(0);
                        progress(line.Trim(), null);
                    }
                }
                p.WaitForExit();
                stderr.Wait();
                stderrText = stderr.Result;
                code = p.ExitCode;
            }
            if (tags.ContainsKey("ERR"))
                throw new ApplicationException(string.Join("\n", new[] { tags["ERR"][0] }.Concat(tail)));
            if (!tags.ContainsKey("FILE"))
                throw new ApplicationException(code != 0 ? ssh.Error(stderrText, code) : string.Join("\n", tail));
            var remote = tags["FILE"][0];
            var size = long.Parse(tags["FILE"][1]);
            var remoteLog = tags.ContainsKey("LOG") ? tags["LOG"][0] : "";
            // expdp sale con 5 si terminó con avisos: el volcado sirve, pero se avisa.
            warning = tags.ContainsKey("RC") && tags["RC"][0] == "0" ? "" : "expdp terminó con avisos: revisa el .log";

            var local = Path.Combine(dest, name + ".dmp");
            try
            {
                Download(ssh, remote, local, size, progress);
                if (remoteLog != "")
                {
                    using (var ms = new MemoryStream())
                    {
                        Proc.Quiet(ssh.Command("cat -- " + Proc.ShQuote(remoteLog)), ssh, ms);
                        if (ms.Length > 0) File.WriteAllBytes(Path.ChangeExtension(local, ".log"), ms.ToArray());
                    }
                }
            }
            finally
            {
                progress("Borrando el volcado de la máquina…", null);
                var files = new[] { remote, remoteLog }.Where(x => x != "").Select(Proc.ShQuote);
                Proc.Quiet(ssh.Command("rm -f -- " + string.Join(" ", files)), ssh);
            }
            return local;
        }

        static void Download(Ssh ssh, string remote, string local, long size, Action<string, double?> progress)
        {
            var part = local + ".part"; // a medias no se confunde con un volcado bueno
            // Compression=yes: un .dmp sin comprimir se reduce mucho por el camino.
            var cmd = ssh.Command("cat -- " + Proc.ShQuote(remote));
            cmd.InsertRange(1, new[] { "-o", "Compression=yes" });
            long done = 0;
            var shown = Path.GetFileName(local);
            try
            {
                string error;
                int code;
                using (var f = File.Create(part))
                using (var p = Proc.Start(cmd, ssh))
                {
                    p.StandardInput.Close();
                    var stderr = p.StandardError.ReadToEndAsync();
                    var buffer = new byte[1 << 20];
                    int n;
                    while ((n = p.StandardOutput.BaseStream.Read(buffer, 0, buffer.Length)) > 0)
                    {
                        f.Write(buffer, 0, n);
                        done += n;
                        progress("Descargando " + shown + ": " + done / 1048576 + " de " + size / 1048576 + " MB",
                            size > 0 ? (double)done / size : (double?)null);
                    }
                    p.WaitForExit();
                    stderr.Wait();
                    error = stderr.Result;
                    code = p.ExitCode;
                }
                if (code != 0 || done != size)
                    throw new ApplicationException("La descarga falló (" + done + " de " + size + " bytes): " + ssh.Error(error, code));
                File.Move(part, local);
            }
            finally
            {
                if (File.Exists(part)) File.Delete(part);
            }
        }
    }

    public class Machine
    {
        public static readonly KeyValuePair<string, string>[] AuthModes =
        {
            new KeyValuePair<string, string>("agent", "Agente SSH / claves"),
            new KeyValuePair<string, string>("password", "Contraseña"),
            new KeyValuePair<string, string>("key", "Clave / certificado")
        };
        // Sin «-» al principio: ssh lo tomaría por una opción (p. ej. -oProxyCommand=…).
        static readonly Regex NameRe = new Regex(@"^[A-Za-z0-9_.][A-Za-z0-9_.@-]*\z");
        static readonly Regex HostRe = new Regex(@"^[A-Za-z0-9_.:\[\]][A-Za-z0-9_.:\[\]-]*\z");

        public string Name = "", Host = "", User = "root", Auth = "agent", KeyFile = "", CertFile = "";
        public int Port = 22;
        public string Id = Guid.NewGuid().ToString("N").Substring(0, 12);

        public string Label { get { return Name != "" ? Name : Host; } }

        public void Validate()
        {
            if (!HostRe.IsMatch(Host)) throw new ArgumentException("Host no válido (IP o nombre, sin espacios)");
            if (!NameRe.IsMatch(User)) throw new ArgumentException("Usuario SSH no válido");
            if (Port <= 0 || Port >= 65536) throw new ArgumentException("Puerto no válido");
            if (!AuthModes.Any(a => a.Key == Auth)) throw new ArgumentException("Autenticación no válida");
            if (Auth == "key")
            {
                if (KeyFile == "" || !File.Exists(Proc.Expand(KeyFile))) throw new ArgumentException("No encuentro la clave privada");
                if (CertFile != "" && !File.Exists(Proc.Expand(CertFile))) throw new ArgumentException("No encuentro el certificado");
            }
        }
    }

    public class Config
    {
        public List<Machine> Machines = new List<Machine>();
        public bool ShowOracle;
        public string DumpFolder = "";

        static string FilePath
        {
            get
            {
                var appData = Environment.GetEnvironmentVariable("APPDATA") ?? Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
                return Path.Combine(appData, "infranettone-oracle", "config.json");
            }
        }

        static string Str(IDictionary d, string key, string fallback = "")
        {
            return d.Contains(key) && d[key] != null ? d[key].ToString() : fallback;
        }

        public static Config Load()
        {
            var cfg = new Config();
            try
            {
                var data = (IDictionary)new JavaScriptSerializer().DeserializeObject(File.ReadAllText(FilePath, Encoding.UTF8));
                cfg.ShowOracle = data.Contains("show_oracle") && true.Equals(data["show_oracle"]);
                cfg.DumpFolder = Str(data, "dump_folder");
                if (data.Contains("machines"))
                    foreach (IDictionary m in (IEnumerable)data["machines"])
                    {
                        int port;
                        var machine = new Machine
                        {
                            Name = Str(m, "name"), Host = Str(m, "host"), User = Str(m, "user", "root"), Auth = Str(m, "auth", "agent"),
                            KeyFile = Str(m, "key_file"), CertFile = Str(m, "cert_file"),
                            Port = int.TryParse(Str(m, "port", "22"), out port) ? port : 22
                        };
                        if (m.Contains("id") && m["id"] != null) machine.Id = m["id"].ToString();
                        cfg.Machines.Add(machine);
                    }
            }
            catch (Exception e)
            {
                if (!(e is IOException || e is UnauthorizedAccessException || e is ArgumentException || e is InvalidCastException)) throw;
            }
            return cfg;
        }

        public void Save()
        {
            Directory.CreateDirectory(Path.GetDirectoryName(FilePath));
            var data = new Dictionary<string, object>
            {
                { "machines", Machines.Select(m => new Dictionary<string, object> {
                    { "name", m.Name }, { "host", m.Host }, { "user", m.User }, { "port", m.Port }, { "auth", m.Auth },
                    { "id", m.Id }, { "key_file", m.KeyFile }, { "cert_file", m.CertFile } }).ToList() },
                { "show_oracle", ShowOracle },
                { "dump_folder", DumpFolder }
            };
            File.WriteAllText(FilePath, new JavaScriptSerializer().Serialize(data), new UTF8Encoding(false));
        }
    }
}
