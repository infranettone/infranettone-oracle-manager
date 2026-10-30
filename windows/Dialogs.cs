using System;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Windows.Forms;

namespace OracleManager
{
    static class Ui
    {
        // Los controles se colocan a mano: se escala con los DPI del sistema (ver app.manifest).
        public static readonly float Scale;

        static Ui()
        {
            using (var g = Graphics.FromHwnd(IntPtr.Zero)) Scale = g.DpiX / 96f;
        }

        public static int S(int n) { return (int)(n * Scale); }

        public static Form Dialog(string title)
        {
            return new Form
            {
                Text = title, FormBorderStyle = FormBorderStyle.FixedDialog, MaximizeBox = false, MinimizeBox = false,
                ShowInTaskbar = false, StartPosition = FormStartPosition.CenterParent, AutoSize = true,
                AutoSizeMode = AutoSizeMode.GrowAndShrink, Font = SystemFonts.MessageBoxFont, AutoScaleMode = AutoScaleMode.None,
                Padding = new Padding(S(12))
            };
        }

        public static TableLayoutPanel Grid(int columns)
        {
            var t = new TableLayoutPanel { AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = columns, Dock = DockStyle.Fill };
            return t;
        }

        public static Label Text(string text, int maxWidth = 0)
        {
            var l = new Label { Text = text, AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(S(3), S(6), S(3), S(6)) };
            if (maxWidth > 0) l.MaximumSize = new Size(S(maxWidth), 0);
            return l;
        }

        public static FlowLayoutPanel Buttons(Form f, string acceptText)
        {
            var ok = new Button { Text = acceptText, DialogResult = DialogResult.OK, AutoSize = true, MinimumSize = new Size(S(80), 0) };
            var cancel = new Button { Text = "Cancelar", DialogResult = DialogResult.Cancel, AutoSize = true, MinimumSize = new Size(S(80), 0) };
            f.AcceptButton = ok;
            f.CancelButton = cancel;
            var p = new FlowLayoutPanel { AutoSize = true, Anchor = AnchorStyles.Right, FlowDirection = FlowDirection.LeftToRight, Margin = new Padding(0, S(10), 0, 0) };
            p.Controls.Add(ok);
            p.Controls.Add(cancel);
            return p;
        }
    }

    class MachineDialog : Form
    {
        readonly TextBox name = new TextBox(), host = new TextBox(), user = new TextBox(), port = new TextBox(), key = new TextBox(), cert = new TextBox();
        readonly ComboBox auth = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList };
        readonly Control[] keyRows;
        readonly string id;

        public MachineDialog(string title, Machine d)
        {
            Text = title;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = MinimizeBox = ShowInTaskbar = false;
            StartPosition = FormStartPosition.CenterParent;
            AutoSize = true;
            AutoSizeMode = AutoSizeMode.GrowAndShrink;
            Font = SystemFonts.MessageBoxFont;
            AutoScaleMode = AutoScaleMode.None;
            Padding = new Padding(Ui.S(12));
            id = d.Id;

            name.Text = d.Name; host.Text = d.Host; user.Text = d.User; port.Text = d.Port.ToString();
            key.Text = d.KeyFile; cert.Text = d.CertFile;
            foreach (var a in Machine.AuthModes) auth.Items.Add(a.Value);
            auth.SelectedIndex = Array.FindIndex(Machine.AuthModes, a => a.Key == d.Auth);

            var t = Ui.Grid(3);
            Row(t, "Nombre (opcional)", name);
            Row(t, "Host (IP o nombre)", host);
            Row(t, "Usuario SSH", user);
            Row(t, "Puerto", port);
            Row(t, "Autenticación", auth);
            var keyLabel = Ui.Text("Clave privada");
            var certLabel = Ui.Text("Certificado (opcional, *-cert.pub)");
            var keyBtn = Browse(key, "Elegir la clave privada");
            var certBtn = Browse(cert, "Elegir el certificado");
            RowControls(t, keyLabel, key, keyBtn);
            RowControls(t, certLabel, cert, certBtn);
            keyRows = new Control[] { keyLabel, key, keyBtn, certLabel, cert, certBtn };

            var note = Ui.Text("La contraseña SSH y la frase de paso de la clave no se guardan: se piden al consultar. Si no eliges certificado, ssh usa el <clave>-cert.pub que haya al lado de la clave.", 460);
            t.Controls.Add(note);
            t.SetColumnSpan(note, 3);
            var buttons = Ui.Buttons(this, "Guardar");
            t.Controls.Add(buttons);
            t.SetColumnSpan(buttons, 3);
            Controls.Add(t);

            auth.SelectedIndexChanged += delegate { SyncAuth(); };
            SyncAuth();
        }

        void SyncAuth()
        {
            foreach (var c in keyRows) c.Visible = auth.SelectedIndex == 2;
        }

        static void Row(TableLayoutPanel t, string label, Control c)
        {
            RowControls(t, Ui.Text(label), c, null);
        }

        static void RowControls(TableLayoutPanel t, Label label, Control c, Control extra)
        {
            c.Width = Ui.S(300);
            c.Anchor = AnchorStyles.Left;
            var row = t.RowCount++;
            t.Controls.Add(label, 0, row);
            t.Controls.Add(c, 1, row);
            if (extra != null) t.Controls.Add(extra, 2, row);
        }

        static Button Browse(TextBox target, string title)
        {
            var b = new Button { Text = "…", Width = Ui.S(32), Anchor = AnchorStyles.Left };
            b.Click += delegate
            {
                using (var d = new OpenFileDialog { Title = title, CheckFileExists = true, Filter = "Todos los archivos|*.*" })
                {
                    var ssh = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".ssh");
                    if (Directory.Exists(ssh)) d.InitialDirectory = ssh;
                    if (d.ShowDialog() == DialogResult.OK) target.Text = d.FileName;
                }
            };
            return b;
        }

        public Machine Result()
        {
            var mode = Machine.AuthModes[auth.SelectedIndex].Key;
            int p;
            return new Machine
            {
                Name = name.Text.Trim(), Host = host.Text.Trim(), User = user.Text.Trim() != "" ? user.Text.Trim() : "root",
                Port = int.TryParse(port.Text.Trim(), out p) ? p : 0, Auth = mode, Id = id,
                KeyFile = mode == "key" ? key.Text.Trim() : "", CertFile = mode == "key" ? cert.Text.Trim() : ""
            };
        }
    }

    static class SecretDialog
    {
        // Devuelve null si se cancela. Lo escrito solo vive en memoria mientras dura la operación.
        public static string Ask(IWin32Window owner, string title, string label)
        {
            using (var f = Ui.Dialog(title))
            {
                var t = Ui.Grid(2);
                var box = new TextBox { UseSystemPasswordChar = true, Width = Ui.S(280) };
                t.Controls.Add(Ui.Text(label), 0, 0);
                t.Controls.Add(box, 1, 0);
                var note = Ui.Text("Se usa solo esta vez y no se guarda.");
                t.Controls.Add(note, 0, 1);
                t.SetColumnSpan(note, 2);
                var buttons = Ui.Buttons(f, "Conectar");
                t.Controls.Add(buttons, 0, 2);
                t.SetColumnSpan(buttons, 2);
                f.Controls.Add(t);
                return f.ShowDialog(owner) == DialogResult.OK && box.Text != "" ? box.Text : null;
            }
        }
    }
}
