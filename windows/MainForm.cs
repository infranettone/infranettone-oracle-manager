// Interfaz WinForms: controles Win32 del sistema. A la izquierda las máquinas y sus instancias; a la derecha los usuarios.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace OracleManager
{
    class MainForm : Form
    {
        const string UsageHelp =
            "Sin auditoría ni licencias extra (no se usa AWR/ASH), Oracle solo guarda:\n" +
            "• Sesiones: las abiertas ahora, con máquina y programa.\n" +
            "• Lecturas y cambios: en los objetos del esquema desde el arranque de la instancia, los haga quien los haga " +
            "(también trabajos internos, como las estadísticas).\n" +
            "• SQL: ejecuciones lanzadas por el usuario que siguen en memoria.\n" +
            "• Última conexión, último SQL y último DML (desde las últimas estadísticas de la tabla).\n\n" +
            "Último uso = la más reciente de esas fechas. Sin uso = nada desde el arranque y último uso hace más de 90 días " +
            "(o nunca). Bajo/Medio/Alto = tercios de los que tienen actividad en esa instancia (lecturas, cambios, SQL y " +
            "sesiones). No hay histórico de conexiones: para eso habría que activar la auditoría de LOGON.";

        class Row
        {
            public User User;
            public string Container;
        }

        class Col
        {
            public string Title;
            public int Width;
            public bool Numeric;
            public Func<Row, string> Text;
            public Func<Row, IComparable> Key;

            public Col(string title, int width, bool numeric, Func<Row, string> text, Func<Row, IComparable> key)
            {
                Title = title; Width = width; Numeric = numeric; Text = text; Key = key;
            }
        }

        static Col Num(string title, Func<User, long> value)
        {
            return new Col(title, 72, true, r => Human(value(r.User)), r => value(r.User));
        }

        static readonly Col[] Columns =
        {
            new Col("Usuario", 190, false, r => r.User.Name + (r.User.OracleMaintained ? " *" : ""), r => r.User.Name),
            new Col("Contenedor", 110, false, r => r.Container, r => r.Container),
            new Col("Uso", 70, false, r => r.User.Level, r => Array.IndexOf(Usage.Levels, r.User.Level)),
            new Col("Último uso", 110, false, r => Usage.IdleText(r.User), r => Usage.IdleSort(r.User)),
            Num("Sesiones", u => u.Sessions), Num("SQL", u => u.SqlExecs), Num("Lecturas", u => u.LogicalReads),
            Num("Cambios", u => u.BlockChanges), Num("DML", u => u.Dml), Num("Objetos", u => u.Objects), Num("MB", u => u.SizeMb),
            new Col("Estado", 120, false, r => r.User.Status, r => r.User.Status),
        };

        static readonly Dictionary<string, Color> LevelColor = new Dictionary<string, Color>
        {
            { "Sin uso", Color.FromArgb(192, 28, 40) }, { "Bajo", Color.FromArgb(156, 110, 3) },
            { "Medio", Color.FromArgb(28, 113, 216) }, { "Alto", Color.FromArgb(38, 132, 74) }
        };

        readonly Config cfg = Config.Load();
        // Por máquina (id): instancias, último error y consultas en marcha. Solo en memoria.
        readonly Dictionary<string, List<Instance>> results = new Dictionary<string, List<Instance>>();
        readonly Dictionary<string, string> errors = new Dictionary<string, string>();
        readonly HashSet<string> loading = new HashSet<string>();
        string selectedMachine, selectedSid;
        Instance current;
        bool exporting, rebuilding;
        int sortColumn = 0;
        bool sortAscending = true;
        List<Row> rows = new List<Row>(), visible = new List<Row>();

        readonly TreeView tree = new TreeView { HideSelection = false, ShowNodeToolTips = true, Dock = DockStyle.Fill, FullRowSelect = true, ShowLines = false };
        readonly TextBox search = new TextBox();
        readonly ComboBox levelFilter = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList };
        readonly CheckBox showOracle = new CheckBox { Text = "Usuarios de Oracle", AutoSize = true };
        readonly Label title = new Label { AutoSize = true };
        readonly Label details = new Label { AutoSize = true, ForeColor = SystemColors.GrayText };
        readonly Label instErrors = new Label { AutoSize = true, ForeColor = Color.FromArgb(192, 28, 40), Visible = false };
        readonly ListView table = new ListView { View = View.Details, FullRowSelect = true, MultiSelect = false, HideSelection = false, Dock = DockStyle.Fill };
        readonly ListView detail = new ListView { View = View.Details, HeaderStyle = ColumnHeaderStyle.None, FullRowSelect = true, Dock = DockStyle.Fill, ShowItemToolTips = true };
        readonly Label detailTitle = new Label { AutoSize = true };
        readonly Button export = new Button { Text = "Exportar (dump)…", AutoSize = true, Enabled = false };
        readonly ToolStripStatusLabel status = new ToolStripStatusLabel { Spring = true, TextAlign = ContentAlignment.MiddleLeft };
        readonly ToolStripStatusLabel summary = new ToolStripStatusLabel();
        readonly ToolStripProgressBar progress = new ToolStripProgressBar { Visible = false };
        readonly Panel dataPanel = new Panel { Dock = DockStyle.Fill };
        readonly Label empty = new Label { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter };
        readonly ToolStripSplitButton copyCommand = new ToolStripSplitButton("Copiar comando sqlplus");

        [DllImport("uxtheme.dll", CharSet = CharSet.Unicode)]
        static extern int SetWindowTheme(IntPtr hwnd, string app, string idList);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        static extern IntPtr SendMessage(IntPtr hwnd, int msg, IntPtr wParam, string lParam);

        public MainForm()
        {
            Text = "Oracle Manager";
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            Font = SystemFonts.MessageBoxFont;
            AutoScaleMode = AutoScaleMode.None;
            Size = new Size(Ui.S(1200), Ui.S(780));
            MinimumSize = new Size(Ui.S(800), Ui.S(500));
            StartPosition = FormStartPosition.CenterScreen;
            title.Font = new Font(Font.FontFamily, Font.Size * 1.5f, FontStyle.Bold);
            detailTitle.Font = new Font(Font.FontFamily, Font.Size * 1.2f, FontStyle.Bold);

            var split = new SplitContainer { Dock = DockStyle.Fill, FixedPanel = FixedPanel.Panel1 };
            split.Panel1.Controls.Add(tree);
            split.Panel1.Controls.Add(MachineBar());
            split.Panel2.Controls.Add(dataPanel);
            split.Panel2.Controls.Add(empty);
            BuildContent();

            var strip = new StatusStrip();
            strip.Items.AddRange(new ToolStripItem[] { status, progress, summary });
            Controls.Add(split);
            Controls.Add(strip);
            Load += delegate { split.Panel1MinSize = Ui.S(220); split.SplitterDistance = Ui.S(320); };

            tree.ContextMenuStrip = MachineMenu();
            tree.AfterSelect += delegate { OnTreeSelect(); };
            tree.NodeMouseDoubleClick += (s, e) =>
            {
                var m = e.Node.Tag as Machine;
                if (m != null) Refresh(m);
            };
            tree.NodeMouseClick += (s, e) => { if (e.Button == MouseButtons.Right) tree.SelectedNode = e.Node; };

            HandleCreated += delegate
            {
                SetWindowTheme(tree.Handle, "explorer", null);
                SetWindowTheme(table.Handle, "explorer", null);
                SetWindowTheme(detail.Handle, "explorer", null);
                SendMessage(search.Handle, 0x1501 /* EM_SETCUEBANNER */, (IntPtr)1, "Buscar usuario");
            };

            Rebuild();
            ShowEmpty(null, null);
            // Al abrir se consultan solas las que no piden nada; las de contraseña, cuando se pulsen.
            Shown += delegate
            {
                foreach (var m in cfg.Machines.Where(x => x.Auth == "agent").ToList()) Refresh(m);
            };
        }

        // --- barra lateral: máquinas e instancias ---

        ToolStrip MachineBar()
        {
            var bar = new ToolStrip { GripStyle = ToolStripGripStyle.Hidden, Dock = DockStyle.Top };
            bar.Items.Add(new ToolStripButton("Añadir", null, delegate { Edit(null, null); }));
            bar.Items.Add(new ToolStripButton("Consultar", null, delegate { WithMachine(Refresh); }));
            bar.Items.Add(new ToolStripButton("Editar", null, delegate { WithMachine(m => Edit(m, null)); }));
            bar.Items.Add(new ToolStripButton("Eliminar", null, delegate { WithMachine(Delete); }));
            var about = new ToolStripButton("Acerca de", null, delegate
            {
                MessageBox.Show(this, "Oracle Manager " + Application.ProductVersion + "\nBases de datos Oracle y sus usuarios/esquemas, por SSH\n\nRaul Adamuz\nhttps://github.com/infranettone/infranettone-oracle-manager",
                    "Acerca de", MessageBoxButtons.OK, MessageBoxIcon.Information);
            });
            about.Alignment = ToolStripItemAlignment.Right;
            bar.Items.Add(about);
            return bar;
        }

        ContextMenuStrip MachineMenu()
        {
            var menu = new ContextMenuStrip();
            menu.Items.Add("Consultar", null, delegate { WithMachine(Refresh); });
            menu.Items.Add("Editar", null, delegate { WithMachine(m => Edit(m, null)); });
            menu.Items.Add("Eliminar", null, delegate { WithMachine(Delete); });
            return menu;
        }

        void WithMachine(Action<Machine> action)
        {
            var n = tree.SelectedNode;
            if (n == null) { Toast("Elige una máquina"); return; }
            var m = (n.Tag as Machine) ?? (n.Parent != null ? n.Parent.Tag as Machine : null);
            if (m != null) action(m);
        }

        Machine FindMachine(string id)
        {
            return cfg.Machines.FirstOrDefault(m => m.Id == id);
        }

        // Rehace la lista: cada máquina y, debajo, sus instancias (si ya se han consultado).
        void Rebuild()
        {
            rebuilding = true;
            tree.BeginUpdate();
            tree.Nodes.Clear();
            TreeNode selected = null;
            foreach (var m in cfg.Machines)
            {
                var text = m.Label;
                if (loading.Contains(m.Id)) text += " (consultando…)";
                else if (errors.ContainsKey(m.Id)) text += " ⚠";
                var node = new TreeNode(text) { Tag = m, ToolTipText = errors.ContainsKey(m.Id) ? errors[m.Id] :
                    m.User + "@" + m.Host + (m.Port != 22 ? ":" + m.Port : "") + " · " + Machine.AuthModes.First(a => a.Key == m.Auth).Value.ToLowerInvariant() };
                tree.Nodes.Add(node);
                if (selectedMachine == m.Id && selectedSid == null) selected = node;
                List<Instance> found;
                if (results.TryGetValue(m.Id, out found))
                    foreach (var i in found)
                    {
                        var n = i.Containers.SelectMany(c => c.Users).Count(u => !u.OracleMaintained);
                        var inode = new TreeNode(i.Sid + " · " + (i.ContainerName != "" ? i.ContainerName : "en la máquina") + " · " + n + " usuarios" + (i.Errors.Count > 0 ? " ⚠" : ""))
                        {
                            Tag = i, ToolTipText = string.Join("\n", i.Errors)
                        };
                        node.Nodes.Add(inode);
                        if (selectedMachine == m.Id && selectedSid == i.Sid) selected = inode;
                    }
                node.Expand();
            }
            tree.EndUpdate();
            tree.SelectedNode = selected;
            rebuilding = false;
        }

        void OnTreeSelect()
        {
            if (rebuilding || tree.SelectedNode == null) return;
            var i = tree.SelectedNode.Tag as Instance;
            var m = tree.SelectedNode.Tag as Machine;
            if (i != null)
            {
                selectedMachine = ((Machine)tree.SelectedNode.Parent.Tag).Id;
                selectedSid = i.Sid;
                ShowInstance(FindMachine(selectedMachine), i);
            }
            else if (m != null)
            {
                selectedMachine = m.Id;
                selectedSid = null;
                if (errors.ContainsKey(m.Id)) ShowEmpty("No se pudo consultar " + m.Label, errors[m.Id]);
                else ShowEmpty(null, null);
            }
        }

        // --- alta, edición y borrado ---

        void Edit(Machine m, Machine draft)
        {
            using (var d = new MachineDialog(m != null ? "Editar máquina" : "Nueva máquina", draft ?? m ?? new Machine()))
            {
                if (d.ShowDialog(this) != DialogResult.OK) return;
                var updated = d.Result();
                try
                {
                    updated.Validate();
                }
                catch (ArgumentException e)
                {
                    Toast(e.Message);
                    MessageBox.Show(this, e.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    Edit(m, updated); // se vuelve a abrir con lo escrito
                    return;
                }
                if (m != null)
                {
                    cfg.Machines[cfg.Machines.IndexOf(m)] = updated;
                    // Otra máquina u otra forma de entrar: lo consultado ya no vale.
                    if (m.Host != updated.Host || m.User != updated.User || m.Port != updated.Port || m.Auth != updated.Auth
                        || m.KeyFile != updated.KeyFile || m.CertFile != updated.CertFile)
                        Forget(updated.Id);
                }
                else cfg.Machines.Add(updated);
                cfg.Save();
                Rebuild();
                if (m == null || !results.ContainsKey(m.Id)) Refresh(updated);
            }
        }

        void Delete(Machine m)
        {
            if (MessageBox.Show(this, "Solo se quita de la lista; en la máquina no se toca nada.", "¿Eliminar " + m.Label + "?",
                MessageBoxButtons.OKCancel, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2) != DialogResult.OK) return;
            cfg.Machines.Remove(m);
            cfg.Save();
            Forget(m.Id);
            Rebuild();
        }

        void Forget(string id)
        {
            results.Remove(id);
            errors.Remove(id);
            if (selectedMachine == id)
            {
                selectedMachine = selectedSid = null;
                ShowEmpty(null, null);
            }
        }

        // --- consultar ---

        void Refresh(Machine m)
        {
            if (loading.Contains(m.Id)) return;
            Ssh ssh;
            if (WithSecret(m, out ssh)) Run(m, ssh);
        }

        // Pide la contraseña o la frase de paso si hacen falta (no se guardan). false si se cancela.
        bool WithSecret(Machine m, out Ssh ssh)
        {
            ssh = new Ssh { Host = m.Host, User = m.User, Port = m.Port };
            if (m.Auth == "password")
            {
                ssh.Password = SecretDialog.Ask(this, "Contraseña de " + m.User + "@" + m.Host, "Contraseña");
                return ssh.Password != null;
            }
            if (m.Auth != "key") return true;
            ssh.Identity = m.KeyFile;
            ssh.Certificate = m.CertFile;
            try
            {
                if (!Discovery.KeyNeedsPassphrase(m.KeyFile)) return true;
            }
            catch (ApplicationException e)
            {
                Toast(e.Message);
                return false;
            }
            ssh.Passphrase = SecretDialog.Ask(this, "Frase de paso de " + Path.GetFileName(m.KeyFile), "Frase de paso");
            return ssh.Passphrase != null;
        }

        async void Run(Machine m, Ssh ssh)
        {
            loading.Add(m.Id);
            Rebuild();
            List<Instance> found = null;
            string error = null;
            try
            {
                // ssh y sqlplus tardan unos segundos: en otro hilo, y await vuelve al de la interfaz.
                found = await Task.Run(() => Discovery.Discover(ssh));
            }
            catch (Exception e)
            {
                if (!(e is ApplicationException || e is IOException || e is FormatException || e is OverflowException)) throw;
                error = e.Message;
            }
            loading.Remove(m.Id);
            if (FindMachine(m.Id) == null) return; // se borró mientras tanto
            if (error != null && m.Auth == "key" && error.Contains("UNPROTECTED PRIVATE KEY") && OfferRestrictKey(m))
            {
                errors.Remove(m.Id);
                Refresh(m);
                return;
            }
            if (error != null)
            {
                errors[m.Id] = error;
                Toast(m.Label + ": no se pudo consultar");
            }
            else
            {
                errors.Remove(m.Id);
                results[m.Id] = found;
                Toast(found.Count > 0 ? m.Label + ": " + found.Count + " instancias" : m.Label + ": no hay ningún proceso ora_pmon en marcha");
            }
            Rebuild();
            if (selectedMachine != m.Id) return;
            var again = found == null ? null : found.FirstOrDefault(i => i.Sid == selectedSid);
            if (again != null) ShowInstance(m, again);
            else
            {
                selectedSid = null;
                if (error != null) ShowEmpty("No se pudo consultar " + m.Label, error);
                else ShowEmpty(null, null);
            }
        }

        // Cambia permisos de un archivo del usuario: nunca sin preguntar.
        bool OfferRestrictKey(Machine m)
        {
            if (MessageBox.Show(this, "OpenSSH no usa esta clave porque otros usuarios pueden leerla:\n\n" + m.KeyFile
                + "\n\n¿Dejarla accesible solo para tu usuario y volver a intentarlo? El contenido del archivo no cambia.",
                "Permisos de la clave", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return false;
            try
            {
                Discovery.RestrictKey(m.KeyFile);
                return true;
            }
            catch (Exception e)
            {
                if (!(e is UnauthorizedAccessException || e is IOException || e is InvalidOperationException)) throw;
                MessageBox.Show(this, e.Message, "No se pudieron cambiar los permisos", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return false;
            }
        }

        // --- contenido ---

        void BuildContent()
        {
            var bar = new ToolStrip { GripStyle = ToolStripGripStyle.Hidden, Dock = DockStyle.Top };
            var viaSsh = new ToolStripMenuItem("Copiar con ssh desde este equipo", null, delegate { CopyCommand(true); });
            copyCommand.DropDownItems.Add(viaSsh);
            copyCommand.ButtonClick += delegate { CopyCommand(false); };
            bar.Items.Add(copyCommand);
            bar.Items.Add(new ToolStripButton("Copiar lista (TSV)", null, delegate { CopyTsv(); }));
            bar.Items.Add(new ToolStripButton("Cómo se mide el uso", null, delegate
            {
                MessageBox.Show(this, UsageHelp, "Cómo se mide el uso", MessageBoxButtons.OK, MessageBoxIcon.Information);
            }));

            search.Width = Ui.S(220);
            search.TextChanged += delegate { Fill(); };
            levelFilter.Items.Add("Todo el uso");
            levelFilter.Items.AddRange(Usage.Levels.Cast<object>().ToArray());
            levelFilter.SelectedIndex = 0;
            levelFilter.Width = Ui.S(120);
            levelFilter.SelectedIndexChanged += delegate { Fill(); };
            showOracle.Checked = cfg.ShowOracle;
            showOracle.Margin = new Padding(Ui.S(8), Ui.S(4), 0, 0);
            showOracle.CheckedChanged += delegate
            {
                cfg.ShowOracle = showOracle.Checked;
                cfg.Save();
                Fill();
            };
            var filters = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Top, WrapContents = false };
            filters.Controls.AddRange(new Control[] { search, levelFilter, showOracle });

            var head = new TableLayoutPanel { AutoSize = true, Dock = DockStyle.Top, ColumnCount = 1, Padding = new Padding(Ui.S(6)) };
            head.Controls.Add(title);
            head.Controls.Add(details);
            head.Controls.Add(instErrors);
            head.Controls.Add(filters);
            details.MaximumSize = instErrors.MaximumSize = new Size(Ui.S(850), 0);

            foreach (var c in Columns)
                table.Columns.Add(c.Title, Ui.S(c.Width), c.Numeric ? HorizontalAlignment.Right : HorizontalAlignment.Left);
            table.ColumnClick += (s, e) =>
            {
                sortAscending = e.Column == sortColumn ? !sortAscending : true;
                sortColumn = e.Column;
                Fill();
            };
            table.SelectedIndexChanged += delegate { ShowUser(); };
            typeof(Control).GetProperty("DoubleBuffered", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance).SetValue(table, true, null);

            detail.Columns.Add("", Ui.S(165));
            detail.Columns.Add("", Ui.S(140));
            export.Click += delegate { ExportDialog(); };

            var detailHead = new TableLayoutPanel { AutoSize = true, Dock = DockStyle.Top, ColumnCount = 1, Padding = new Padding(Ui.S(6)) };
            detailHead.Controls.Add(detailTitle);
            detailHead.Controls.Add(export);

            var body = new SplitContainer { Dock = DockStyle.Fill, FixedPanel = FixedPanel.Panel2 };
            body.Panel1.Controls.Add(table);
            body.Panel2.Controls.Add(detail);
            body.Panel2.Controls.Add(detailHead);
            dataPanel.Controls.Add(body);
            dataPanel.Controls.Add(head);
            dataPanel.Controls.Add(bar);
            Load += delegate { body.Panel2MinSize = Ui.S(280); body.SplitterDistance = body.Width - Ui.S(330); };
            ShowUser();
        }

        static string Human(long n)
        {
            var units = new[] { new KeyValuePair<long, string>(1000000000000, " B"), new KeyValuePair<long, string>(1000000000, " mil M"),
                new KeyValuePair<long, string>(1000000, " M"), new KeyValuePair<long, string>(1000, " mil") };
            foreach (var u in units)
                if (n >= u.Key)
                {
                    var s = ((double)n / u.Key).ToString("0.0", System.Globalization.CultureInfo.InvariantCulture).Replace(".", ",");
                    return (s.EndsWith(",0") ? s.Substring(0, s.Length - 2) : s) + u.Value;
                }
            return n.ToString();
        }

        void ShowEmpty(string heading, string description)
        {
            if (heading == null)
            {
                heading = cfg.Machines.Count > 0 ? "Elige una instancia" : "No hay máquinas";
                description = cfg.Machines.Count > 0 ? "Pulsa dos veces una máquina para consultarla." : "Añade una con el botón Añadir.";
            }
            current = null;
            rows = new List<Row>();
            empty.Text = heading + "\n\n" + description;
            dataPanel.Visible = false;
            empty.Visible = true;
            summary.Text = "";
        }

        void ShowInstance(Machine m, Instance i)
        {
            current = i;
            title.Text = m.Label + " · " + i.Sid + " — " + (i.DbName != "" ? i.DbName : "?");
            details.Text = string.Join(" · ", new[] { "Oracle " + i.Version, i.OpenMode, i.LogMode, i.Cdb ? "CDB" : "no CDB",
                "arrancada " + i.Started, "consultada " + i.Now, i.ContainerName != "" ? "pod/contenedor " + i.ContainerName : "", i.OracleHome }.Where(x => x != ""));
            instErrors.Text = string.Join("\n", i.Errors);
            instErrors.Visible = i.Errors.Count > 0;
            // La columna de contenedor (PDB) solo tiene sentido en una CDB.
            table.Columns[1].Width = i.Containers.Count > 1 ? Ui.S(Columns[1].Width) : 0;
            rows = i.Containers.SelectMany(c => c.Users.Select(u => new Row { User = u, Container = c.Name })).ToList();
            Fill();
            empty.Visible = false;
            dataPanel.Visible = true;
        }

        // Filtra, ordena y pinta la tabla.
        void Fill()
        {
            var q = search.Text.Trim().ToUpperInvariant();
            var level = levelFilter.SelectedIndex;
            var shown = rows.Where(r => showOracle.Checked || !r.User.OracleMaintained).ToList();
            visible = shown.Where(r => (level <= 0 || r.User.Level == Usage.Levels[level - 1]) && (q == "" || r.User.Name.Contains(q))).ToList();
            var col = Columns[sortColumn];
            visible = (sortAscending ? visible.OrderBy(col.Key) : visible.OrderByDescending(col.Key)).ToList();

            table.BeginUpdate();
            table.Items.Clear();
            for (var n = 0; n < Columns.Length; n++)
                table.Columns[n].Text = Columns[n].Title + (n == sortColumn ? (sortAscending ? " ▲" : " ▼") : "");
            foreach (var r in visible)
            {
                var item = new ListViewItem(Columns[0].Text(r)) { Tag = r, UseItemStyleForSubItems = false };
                foreach (var c in Columns.Skip(1))
                {
                    var sub = item.SubItems.Add(c.Text(r));
                    if (c.Title == "Uso" && LevelColor.ContainsKey(r.User.Level)) sub.ForeColor = LevelColor[r.User.Level];
                }
                table.Items.Add(item);
            }
            table.EndUpdate();
            // Recuento por nivel de lo que se ve sin el filtro de nivel (para saber cuántos hay de cada uno).
            summary.Text = visible.Count + " usuarios   |   " + string.Join(" · ", Usage.Levels.Select(l => shown.Count(r => r.User.Level == l) + " " + l.ToLowerInvariant()));
            ShowUser();
        }

        void AddDetail(ListViewGroup group, string name, string value, string note = "")
        {
            var item = new ListViewItem(name, group);
            item.SubItems.Add(value != "" ? value : "—");
            item.ToolTipText = note;
            detail.Items.Add(item);
        }

        ListViewGroup Group(string name)
        {
            var g = new ListViewGroup(name);
            detail.Groups.Add(g);
            return g;
        }

        void ShowUser()
        {
            var r = table.SelectedItems.Count > 0 ? (Row)table.SelectedItems[0].Tag : null;
            detail.BeginUpdate();
            detail.Items.Clear();
            detail.Groups.Clear();
            export.Enabled = r != null && !exporting;
            if (r == null)
            {
                detailTitle.Text = "Elige un usuario para ver su actividad.";
                detail.EndUpdate();
                return;
            }
            var u = r.User;
            detailTitle.Text = u.Name + "\nUso " + u.Level.ToLowerInvariant() + " · último uso " + Usage.IdleText(u);

            var now = Group("Ahora");
            AddDetail(now, "Sesiones abiertas", u.Sessions.ToString(), u.ActiveSessions + " ejecutando algo");
            AddDetail(now, "Ejecutando algo", u.ActiveSessions.ToString());
            if (u.Clients.Count > 0)
            {
                var clients = Group("Conectados ahora");
                foreach (var c in u.Clients.OrderByDescending(c => c.Count))
                    AddDetail(clients, c.Machine != "" ? c.Machine : "?", c.Count.ToString(), (c.Program != "" ? c.Program : "?") + " · " + (c.OsUser != "" ? c.OsUser : "?") + " · desde " + c.Since);
            }
            var since = Group("Desde el arranque (" + current.Started + ")");
            AddDetail(since, "Lecturas lógicas", Human(u.LogicalReads), "en sus objetos, por cualquier usuario");
            AddDetail(since, "Lecturas físicas", Human(u.PhysicalReads));
            AddDetail(since, "Bloques cambiados", Human(u.BlockChanges));
            AddDetail(since, "Ejecuciones de SQL", Human(u.SqlExecs), "lanzadas por este usuario, en memoria");
            var dates = Group("Fechas");
            AddDetail(dates, "Última conexión", u.LastLogin);
            AddDetail(dates, "Último SQL", u.SqlLast);
            AddDetail(dates, "Último DML", u.DmlLast, u.Dml + " filas desde las últimas estadísticas");
            AddDetail(dates, "Último cambio de estructura", u.DdlLast, "DDL en sus objetos");
            var info = Group("Cuenta");
            AddDetail(info, "Estado", u.Status);
            AddDetail(info, "Tablespace", u.Tablespace);
            AddDetail(info, "Creado", u.Created);
            AddDetail(info, "Objetos", u.Objects.ToString());
            AddDetail(info, "Tamaño", u.SizeMb + " MB");
            detail.EndUpdate();
        }

        // --- volcado (expdp) de un esquema ---

        void ExportDialog()
        {
            var m = FindMachine(selectedMachine);
            var i = current;
            if (m == null || i == null || exporting || table.SelectedItems.Count == 0) return;
            var r = (Row)table.SelectedItems[0].Tag;
            // En una CDB, los esquemas de una PDB se exportan entrando en ella; los de CDB$ROOT, desde la raíz.
            var pdb = i.Cdb && r.Container != "CDB$ROOT" ? r.Container : "";
            using (var d = new FolderBrowserDialog
            {
                Description = "Exportar " + r.User.Name + " de " + m.Label + " (" + i.Sid + (pdb != "" ? " / " + pdb : "") + "). Se hace con expdp, se descarga aquí junto con su .log y se borra de la máquina. Mientras tanto ocupa allí espacio en DATA_PUMP_DIR (el esquema ocupa " + r.User.SizeMb + " MB).",
                SelectedPath = cfg.DumpFolder != "" ? cfg.DumpFolder : Dump.DefaultFolder(),
                ShowNewFolderButton = true
            })
            {
                if (d.ShowDialog(this) != DialogResult.OK) return;
                cfg.DumpFolder = d.SelectedPath;
                cfg.Save();
                Ssh ssh;
                if (WithSecret(m, out ssh)) RunExport(ssh, i, pdb, r.User.Name, d.SelectedPath);
            }
        }

        async void RunExport(Ssh ssh, Instance i, string pdb, string schema, string folder)
        {
            exporting = true;
            ShowUser();
            progress.Visible = true;
            progress.Value = 0;
            var report = new Progress<KeyValuePair<string, double?>>(p =>
            {
                status.Text = p.Key;
                progress.Style = p.Value.HasValue ? ProgressBarStyle.Continuous : ProgressBarStyle.Marquee;
                if (p.Value.HasValue) progress.Value = (int)(100 * Math.Min(1, p.Value.Value));
            });
            string path = null, warning = null, error = null;
            try
            {
                path = await Task.Run(() => Dump.ExportSchema(ssh, i.Sid, pdb, schema, folder, i.DbName,
                    (text, fraction) => ((IProgress<KeyValuePair<string, double?>>)report).Report(new KeyValuePair<string, double?>(text, fraction)), out warning));
            }
            catch (Exception e)
            {
                if (!(e is ApplicationException || e is IOException || e is UnauthorizedAccessException)) throw;
                error = e.Message;
            }
            exporting = false;
            progress.Visible = false;
            ShowUser();
            if (error != null)
            {
                status.Text = "";
                MessageBox.Show(this, error, "No se pudo exportar " + schema, MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            var message = Path.GetFileName(path) + " descargado" + (warning != "" ? " (" + warning + ")" : "");
            Toast(message);
            if (MessageBox.Show(this, message + "\n\n¿Abrir la carpeta?", "Exportación terminada", MessageBoxButtons.YesNo,
                warning != "" ? MessageBoxIcon.Warning : MessageBoxIcon.Information) == DialogResult.Yes)
                Process.Start("explorer.exe", "/select,\"" + path + "\"");
        }

        // --- varios ---

        void Toast(string text)
        {
            status.Text = text;
        }

        void CopyCommand(bool viaSsh)
        {
            var m = FindMachine(selectedMachine);
            if (current == null || m == null || current.Command == "")
            {
                Toast("No hay comando: vuelve a consultar la máquina");
                return;
            }
            var cmd = current.Command;
            if (viaSsh)
            {
                // Comillas dobles: es lo que entienden tanto cmd.exe como PowerShell.
                var remote = m.User == "root" ? cmd : "sudo " + cmd;
                cmd = "ssh -t " + (m.Port != 22 ? "-p " + m.Port + " " : "") + (m.Auth == "key" ? "-i " + Proc.Quote(m.KeyFile) + " " : "")
                    + m.User + "@" + m.Host + " \"" + remote + "\"";
            }
            Clipboard.SetText(cmd);
            Toast("Comando copiado" + (viaSsh ? "" : ": pégalo como root en la máquina"));
        }

        void CopyTsv()
        {
            var text = new StringBuilder();
            text.Append("Contenedor\t" + string.Join("\t", Columns.Where(c => c.Title != "Contenedor").Select(c => c.Title)) + "\tÚltima conexión\tÚltimo SQL\tÚltimo DML\tÚltimo DDL");
            foreach (var r in visible)
            {
                var u = r.User;
                text.Append("\n" + r.Container + "\t" + string.Join("\t", new[] { r.User.Name + (u.OracleMaintained ? " *" : ""), u.Level, Usage.IdleText(u) }
                    .Concat(new[] { u.Sessions, u.SqlExecs, u.LogicalReads, u.BlockChanges, u.Dml, u.Objects, u.SizeMb }.Select(n => n.ToString()))
                    .Concat(new[] { u.Status, u.LastLogin, u.SqlLast, u.DmlLast, u.DdlLast })));
            }
            if (visible.Count == 0) { Toast("No hay usuarios que copiar"); return; }
            Clipboard.SetText(text.ToString());
            Toast(visible.Count + " usuarios copiados");
        }
    }
}
