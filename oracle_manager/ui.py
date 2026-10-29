"""Interfaz GTK4/libadwaita: a la izquierda las máquinas y sus instancias; a la derecha los usuarios/esquemas."""

import shlex
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from .discover import Instance, Ssh, User, discover, key_needs_passphrase  # noqa: E402
from .dump import default_folder, export_schema  # noqa: E402
from .machines import AUTH_MODES, Config, Machine, new_id  # noqa: E402
from .usage import LEVELS, UNUSED_DAYS, idle_sort, idle_text  # noqa: E402
from .version import version as app_version  # noqa: E402

APP_ID = "com.infranettone.OracleManager"
LEVEL_CSS = {"Sin uso": "error", "Bajo": "warning", "Medio": "accent", "Alto": "success"}
USAGE_HELP = (
    "Sin auditoría ni licencias extra (no se usa AWR/ASH), Oracle solo guarda:\n"
    "• Sesiones: las abiertas ahora, con máquina y programa.\n"
    "• Lecturas y cambios: en los objetos del esquema desde el arranque de la instancia, los haga quien los haga "
    "(también trabajos internos, como las estadísticas).\n"
    "• SQL: ejecuciones lanzadas por el usuario que siguen en memoria.\n"
    "• Última conexión, último SQL y último DML (desde las últimas estadísticas de la tabla).\n\n"
    f"Último uso = la más reciente de esas fechas. Sin uso = nada desde el arranque y último uso hace más de "
    f"{UNUSED_DAYS} días (o nunca). Bajo/Medio/Alto = tercios de los que tienen actividad en esa instancia "
    "(lecturas, cambios, SQL y sesiones). No hay histórico de conexiones: para eso habría que activar la "
    "auditoría de LOGON."
)


class UserItem(GObject.Object):
    """Fila de la tabla: Gtk.ColumnView necesita GObjects con propiedades para ordenar."""

    name = GObject.Property(type=str)
    status = GObject.Property(type=str)
    container = GObject.Property(type=str)
    level = GObject.Property(type=str)
    level_sort = GObject.Property(type=int)
    idle = GObject.Property(type=str)
    idle_sort = GObject.Property(type=int)
    sessions = GObject.Property(type=int)
    sql_execs = GObject.Property(type=GObject.TYPE_INT64)
    logical_reads = GObject.Property(type=GObject.TYPE_INT64)
    block_changes = GObject.Property(type=GObject.TYPE_INT64)
    dml = GObject.Property(type=GObject.TYPE_INT64)
    objects = GObject.Property(type=int)
    size_mb = GObject.Property(type=int)

    def __init__(self, u: User, container: str):
        super().__init__(name=u.name + (" *" if u.oracle_maintained else ""), status=u.status, container=container,
                         level=u.level, level_sort=LEVELS.index(u.level), idle=idle_text(u), idle_sort=idle_sort(u),
                         sessions=u.sessions, sql_execs=u.sql_execs, logical_reads=u.logical_reads,
                         block_changes=u.block_changes, dml=u.dml, objects=u.objects, size_mb=u.size_mb)
        self.user = u
        self.oracle_maintained = u.oracle_maintained


COLUMNS = [  # (título, propiedad que se ve, propiedad por la que se ordena, numérica, expandir)
    ("Usuario", "name", "name", False, True),
    ("Uso", "level", "level_sort", False, False),
    ("Último uso", "idle", "idle_sort", False, False),
    ("Sesiones", "sessions", "sessions", True, False),
    ("SQL", "sql_execs", "sql_execs", True, False),
    ("Lecturas", "logical_reads", "logical_reads", True, False),
    ("Cambios", "block_changes", "block_changes", True, False),
    ("DML", "dml", "dml", True, False),
    ("Objetos", "objects", "objects", True, False),
    ("MB", "size_mb", "size_mb", True, False),
    ("Estado", "status", "status", False, False),
]


def _human(n: int) -> str:
    """1234567 → «1,2 M»: las lecturas llegan a billones y en la tabla no se leerían."""
    for div, suffix in ((10**12, " B"), (10**9, " mil M"), (10**6, " M"), (10**3, " mil")):
        if n >= div:
            return f"{n / div:.1f}".replace(".", ",").removesuffix(",0") + suffix
    return str(n)


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Oracle", default_width=1150, default_height=780)
        self.cfg = Config.load()
        # Por máquina (id): instancias encontradas, último error y si hay una consulta en marcha. Solo en memoria.
        self.results: dict[str, list[Instance]] = {}
        self.errors: dict[str, str] = {}
        self.loading: set[str] = set()
        self.selected: tuple[str, str] | None = None  # (máquina, SID)
        self.current_instance: Instance | None = None
        self.exporting = False  # un volcado cada vez: ocupan disco en la máquina hasta que se descargan

        for name, fn in (("edit", lambda m: self._edit(m)), ("delete", self._delete), ("refresh", self.refresh)):
            action = Gio.SimpleAction(name=name, parameter_type=GLib.VariantType("s"))
            action.connect("activate", lambda _a, v, fn=fn: (m := self._machine(v.get_string())) and fn(m))
            self.add_action(action)

        self.toasts = Adw.ToastOverlay()
        split = Adw.NavigationSplitView(min_sidebar_width=300, max_sidebar_width=380)
        split.set_sidebar(Adw.NavigationPage(title="Máquinas", child=self._sidebar()))
        split.set_content(Adw.NavigationPage(title="Usuarios", child=self._content()))
        self.split = split
        self.toasts.set_child(split)
        self.set_content(self.toasts)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 700sp"))
        bp.add_setter(split, "collapsed", True)
        self.add_breakpoint(bp)

        self._rebuild()
        self._show_empty()
        # Al abrir se consultan solas las que no piden nada; las de contraseña, cuando se pulsen.
        for m in self.cfg.machines:
            if m.auth == "agent":
                self.refresh(m)

    # --- barra lateral: máquinas e instancias ---

    def _sidebar(self) -> Gtk.Widget:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Añadir máquina")
        add.connect("clicked", lambda *_: self._edit(None))
        header.pack_start(add)
        about = Gtk.Button(icon_name="help-about-symbolic", tooltip_text="Acerca de")
        about.connect("clicked", lambda *_: Adw.AboutDialog(
            application_name="Oracle Manager", application_icon="network-server", version=app_version(),
            developer_name="Raul Adamuz", comments="Bases de datos Oracle y sus usuarios/esquemas, por SSH",
            website="https://github.com/infranettone/infranettone-oracle-manager").present(self))
        header.pack_end(about)
        view.add_top_bar(header)

        self.machine_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.machine_list.connect("row-selected", self._on_row_selected)
        self.machine_list.connect("row-activated", self._on_row_activated)
        view.set_content(Gtk.ScrolledWindow(child=self.machine_list, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                            vexpand=True))
        return view

    def _machine(self, mid: str) -> Machine | None:
        return next((m for m in self.cfg.machines if m.id == mid), None)

    def _rebuild(self) -> None:
        """Rehace la lista: cada máquina y, debajo, sus instancias (si ya se han consultado)."""
        self.machine_list.remove_all()
        selected_row = None
        for m in self.cfg.machines:
            row = Adw.ActionRow(title=GLib.markup_escape_text(m.label), selectable=False, activatable=True,
                                subtitle=GLib.markup_escape_text(
                                    f"{m.user}@{m.host}{f':{m.port}' if m.port != 22 else ''} · "
                                    f"{dict(AUTH_MODES)[m.auth].lower()}"))
            row.machine_id, row.sid = m.id, None
            row.add_prefix(Gtk.Image(icon_name="network-server-symbolic"))
            if m.id in self.loading:
                row.add_suffix(Adw.Spinner())
            elif m.id in self.errors:
                row.add_suffix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["error"],
                                         tooltip_text=self.errors[m.id]))
            menu = Gio.Menu()
            menu.append("Consultar", f"win.refresh('{m.id}')")
            menu.append("Editar", f"win.edit('{m.id}')")
            menu.append("Eliminar", f"win.delete('{m.id}')")
            row.add_suffix(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER,
                                          css_classes=["flat"], tooltip_text="Acciones"))
            self.machine_list.append(row)

            for i in self.results.get(m.id, []):
                n = sum(1 for c in i.containers for u in c.users if not u.oracle_maintained)
                irow = Adw.ActionRow(title=i.sid, subtitle=f"{i.container or 'en la máquina'} · {n} usuarios",
                                     margin_start=24)
                irow.machine_id, irow.sid = m.id, i.sid
                irow.add_prefix(Gtk.Image(icon_name="drive-harddisk-symbolic"))
                if i.errors:
                    irow.add_suffix(Gtk.Image(icon_name="dialog-warning-symbolic", tooltip_text="\n".join(i.errors)))
                self.machine_list.append(irow)
                if self.selected == (m.id, i.sid):
                    selected_row = irow
        if selected_row:
            self.machine_list.select_row(selected_row)

    def _on_row_activated(self, _list, row) -> None:
        if row.sid is None:  # fila de máquina: consultar (o ver el error)
            m = self._machine(row.machine_id)
            if m.id in self.errors:
                self._show_empty(f"No se pudo consultar {m.label}", self.errors[m.id])
            self.refresh(m)

    def _on_row_selected(self, _list, row) -> None:
        if row is None or row.sid is None:
            return
        self.selected = (row.machine_id, row.sid)
        i = next((x for x in self.results.get(row.machine_id, []) if x.sid == row.sid), None)
        if i:
            self._show_instance(self._machine(row.machine_id), i)

    # --- alta, edición y borrado ---

    def _edit(self, m: Machine | None, draft: Machine | None = None) -> None:
        d = draft or m or Machine("", "")
        rows = {
            "name": Adw.EntryRow(title="Nombre (opcional)", text=d.name),
            "host": Adw.EntryRow(title="Host (IP o nombre)", text=d.host),
            "user": Adw.EntryRow(title="Usuario SSH", text=d.user),
            "port": Adw.EntryRow(title="Puerto", text=str(d.port), input_purpose=Gtk.InputPurpose.DIGITS),
        }
        auth = Adw.ComboRow(title="Autenticación", model=Gtk.StringList.new([label for _, label in AUTH_MODES]),
                            selected=[k for k, _ in AUTH_MODES].index(d.auth))
        files = {
            "key_file": self._file_row("Clave privada", d.key_file, "Elegir la clave privada"),
            "cert_file": self._file_row("Certificado (opcional, *-cert.pub)", d.cert_file, "Elegir el certificado"),
        }
        box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        for r in (*rows.values(), auth, *files.values()):
            box.append(r)

        def on_auth(*_):
            for r in files.values():
                r.set_visible(AUTH_MODES[auth.get_selected()][0] == "key")

        auth.connect("notify::selected", on_auth)
        on_auth()

        dialog = Adw.AlertDialog(heading="Editar máquina" if m else "Nueva máquina", extra_child=box,
                                 body="La contraseña SSH y la frase de paso de la clave no se guardan: se piden al "
                                      "consultar. Si no eliges certificado, ssh usa el <clave>-cert.pub que haya al "
                                      "lado de la clave.")
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("save", "Guardar")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def on_response(_d, response):
            if response != "save":
                return
            text = {k: r.get_text().strip() for k, r in (rows | files).items()}
            mode = AUTH_MODES[auth.get_selected()][0]
            new = Machine(text["name"], text["host"], text["user"] or "root",
                          int(text["port"]) if text["port"].isdigit() else 0, mode, m.id if m else new_id(),
                          text["key_file"] if mode == "key" else "", text["cert_file"] if mode == "key" else "")
            try:
                new.validate()
            except ValueError as e:
                self.toast(str(e))
                return self._edit(m, new)  # se vuelve a abrir con lo escrito
            if m:
                self.cfg.machines[self.cfg.machines.index(m)] = new
                # Otra máquina u otra forma de entrar: lo consultado ya no vale.
                if (m.host, m.user, m.port, m.auth, m.key_file, m.cert_file) != \
                        (new.host, new.user, new.port, new.auth, new.key_file, new.cert_file):
                    self._forget(new.id)
            else:
                self.cfg.machines.append(new)
            self.cfg.save()
            self._rebuild()
            if not m or m.id not in self.results:
                self.refresh(new)

        dialog.connect("response", on_response)
        dialog.present(self)

    def _file_row(self, title: str, path: str, chooser_title: str) -> Adw.EntryRow:
        row = Adw.EntryRow(title=title, text=path)
        btn = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                         tooltip_text=chooser_title)

        def pick(*_):
            dialog = Gtk.FileDialog(title=chooser_title, modal=True,
                                    initial_folder=Gio.File.new_for_path(str(Path.home() / ".ssh")))

            def done(d, result):
                try:
                    f = d.open_finish(result)
                except GLib.Error:  # cancelado
                    return
                if f and f.get_path():
                    row.set_text(f.get_path())

            dialog.open(self, None, done)

        btn.connect("clicked", pick)
        row.add_suffix(btn)
        return row

    def _delete(self, m: Machine) -> None:
        dialog = Adw.AlertDialog(heading=f"¿Eliminar {m.label}?",
                                 body="Solo se quita de la lista; en la máquina no se toca nada.")
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("delete", "Eliminar")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, response):
            if response == "delete":
                self.cfg.machines.remove(m)
                self.cfg.save()
                self._forget(m.id)
                self._rebuild()

        dialog.connect("response", on_response)
        dialog.present(self)

    def _forget(self, mid: str) -> None:
        self.results.pop(mid, None)
        self.errors.pop(mid, None)
        if self.selected and self.selected[0] == mid:
            self.selected = None
            self._show_empty()

    # --- consultar ---

    def refresh(self, m: Machine) -> None:
        if m.id not in self.loading:
            self._with_secret(m, lambda secret: self._run(m, secret))

    def _with_secret(self, m: Machine, then) -> None:
        """Pide la contraseña o la frase de paso si hacen falta (no se guardan) y sigue con `then(secret)`."""
        if m.auth == "password":
            self._ask_password(m, then)
        elif m.auth == "key" and key_needs_passphrase(m.key_file):
            self._ask_password(m, then, passphrase=True)
        else:
            then(None)

    def _ssh(self, m: Machine, secret: str | None) -> Ssh:
        key = m.auth == "key"
        return Ssh(m.host, m.user, m.port, identity=m.key_file if key else None,
                   certificate=(m.cert_file or None) if key else None,
                   password=secret if m.auth == "password" else None,
                   passphrase=secret if key else None, tty=False)

    def _ask_password(self, m: Machine, then, passphrase: bool = False) -> None:
        entry = Adw.PasswordEntryRow(title="Frase de paso" if passphrase else "Contraseña", activates_default=True)
        box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        box.append(entry)
        heading = (f"Frase de paso de {Path(m.key_file).name}" if passphrase else f"Contraseña de {m.user}@{m.host}")
        dialog = Adw.AlertDialog(heading=heading, extra_child=box, body="Se usa solo esta vez y no se guarda.")
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("connect", "Conectar")
        dialog.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("connect")

        def on_response(_d, response):
            password = entry.get_text()
            if response == "connect" and password:
                then(password)

        dialog.connect("response", on_response)
        dialog.present(self)
        entry.grab_focus()

    def _run(self, m: Machine, secret: str | None) -> None:
        self.loading.add(m.id)
        self._rebuild()

        # ssh y sqlplus tardan unos segundos: en un hilo, y el resultado vuelve a la UI con GLib.idle_add.
        def work():
            try:
                s = self._ssh(m, secret)
                result = discover(s.host, s.user, s.port, s.identity, s.password, s.tty, s.certificate, s.passphrase)
            except (RuntimeError, OSError) as e:
                GLib.idle_add(self._done, m, None, str(e))
            else:
                GLib.idle_add(self._done, m, result, None)

        threading.Thread(target=work, daemon=True).start()

    def _done(self, m: Machine, instances: list[Instance] | None, error: str | None) -> None:
        self.loading.discard(m.id)
        if not self._machine(m.id):  # se borró mientras tanto
            return
        if error:
            self.errors[m.id] = error
            self.toast(f"{m.label}: no se pudo consultar")
        else:
            self.errors.pop(m.id, None)
            self.results[m.id] = instances
            self.toast(f"{m.label}: {len(instances)} instancias" if instances else
                       f"{m.label}: no hay ningún proceso ora_pmon en marcha")
        self._rebuild()
        if self.selected and self.selected[0] == m.id and not self.machine_list.get_selected_row():
            self.selected = None
            self._show_empty()

    # --- contenido: datos de la instancia, tabla de usuarios y detalle del seleccionado ---

    def _content(self) -> Gtk.Widget:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.search = Gtk.SearchEntry(placeholder_text="Buscar usuario", width_chars=22)
        self.search.connect("search-changed", lambda *_: self.filter.changed(Gtk.FilterChange.DIFFERENT))
        header.set_title_widget(self.search)
        self.level_filter = Gtk.DropDown.new_from_strings(["Todo el uso", *LEVELS])
        self.level_filter.set_tooltip_text("Filtrar por nivel de uso")
        self.level_filter.connect("notify::selected", lambda *_: self.filter.changed(Gtk.FilterChange.DIFFERENT))
        header.pack_start(self.level_filter)
        help_btn = Gtk.MenuButton(icon_name="dialog-information-symbolic", tooltip_text="Cómo se mide el uso",
                                  popover=Gtk.Popover(child=Gtk.Label(
                                      label=USAGE_HELP, wrap=True, max_width_chars=60, xalign=0, margin_top=12,
                                      margin_bottom=12, margin_start=12, margin_end=12)))
        header.pack_start(help_btn)
        self.oracle_btn = Gtk.ToggleButton(icon_name="emblem-system-symbolic", active=self.cfg.show_oracle,
                                           tooltip_text="Mostrar los usuarios que crea Oracle (SYS, XDB…)")
        self.oracle_btn.connect("toggled", self._on_oracle_toggled)
        header.pack_end(self.oracle_btn)
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copiar la lista visible (TSV)")
        copy.connect("clicked", self._copy)
        header.pack_end(copy)
        view.add_top_bar(header)

        self.stack = Gtk.Stack()
        self.empty = Adw.StatusPage(icon_name="network-server-symbolic")
        self.stack.add_named(self.empty, "empty")

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12, margin_bottom=12,
                       margin_start=12, margin_end=12)
        self.title = Gtk.Label(xalign=0, css_classes=["title-2"], selectable=True)
        self.details = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"], selectable=True)
        self.inst_errors = Gtk.Label(xalign=0, wrap=True, css_classes=["error"], selectable=True, visible=False)
        self.summary = Gtk.Box(spacing=8)
        self.count = Gtk.Label(xalign=0, css_classes=["caption"])
        # Copiar el comando para entrar en sqlplus: tal cual (para pegarlo como root en la máquina) o envuelto en ssh.
        via_ssh = Gtk.Button(label="Copiar con ssh desde este equipo", css_classes=["flat"])
        sqlplus = Adw.SplitButton(label="Copiar comando sqlplus", valign=Gtk.Align.CENTER,
                                  tooltip_text="Entrar como sysdba en esta instancia, como root en la máquina",
                                  popover=Gtk.Popover(child=via_ssh))
        sqlplus.connect("clicked", lambda *_: self._copy_command(False))
        via_ssh.connect("clicked", lambda *_: (sqlplus.get_popover().popdown(), self._copy_command(True)))
        self.title.set_hexpand(True)
        title_row = Gtk.Box(spacing=12)
        title_row.append(self.title)
        title_row.append(sqlplus)
        for w in (title_row, self.details, self.inst_errors, self.summary):
            page.append(w)

        self.store = Gio.ListStore(item_type=UserItem)
        self.filter = Gtk.CustomFilter.new(self._visible)
        filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.table = Gtk.ColumnView(css_classes=["data-table"], show_row_separators=True, reorderable=False)
        sorted_model = Gtk.SortListModel(model=filtered, sorter=self.table.get_sorter())
        self.selection = Gtk.SingleSelection(model=sorted_model, autoselect=False, can_unselect=True)
        self.selection.connect("notify::selected-item", lambda *_: self._show_user())
        self.table.set_model(self.selection)
        filtered.connect("items-changed", lambda m, *_: self._update_summary(m))
        for title, prop, sort_prop, numeric, expand in COLUMNS:
            self.table.append_column(self._column(title, prop, sort_prop, numeric, expand))
        self.container_col = self._column("Contenedor", "container", "container", False, False)
        self.table.insert_column(1, self.container_col)

        # Detalle del usuario elegido: a la derecha de la tabla.
        self.detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=12, margin_end=12,
                              margin_top=6, margin_bottom=12)
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True, vexpand=True,
                          resize_start_child=True, shrink_start_child=False, shrink_end_child=False)
        paned.set_start_child(Gtk.Frame(child=Gtk.ScrolledWindow(child=self.table)))
        paned.set_end_child(Gtk.ScrolledWindow(child=self.detail, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                               width_request=330))
        page.append(paned)
        page.append(self.count)
        # Progreso del volcado en curso (expdp y luego la descarga).
        self.export_bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, visible=False)
        self.export_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["caption"])
        self.export_progress = Gtk.ProgressBar()
        self.export_bar.append(self.export_label)
        self.export_bar.append(self.export_progress)
        page.append(self.export_bar)
        self.stack.add_named(page, "data")

        view.set_content(self.stack)
        return view

    def _column(self, title: str, prop: str, sort_prop: str, numeric: bool, expand: bool) -> Gtk.ColumnViewColumn:
        def bind(_f, li):
            label, item = li.get_child(), li.get_item()
            value = item.get_property(prop)
            label.set_label(_human(value) if numeric else value)
            if prop == "level":
                label.set_css_classes([LEVEL_CSS.get(value, ""), "heading"])
            if numeric:
                label.set_tooltip_text(f"{value:n}")

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, li: li.set_child(
            Gtk.Label(xalign=1 if numeric else 0, css_classes=["numeric"] if numeric else [])))
        factory.connect("bind", bind)
        expr = Gtk.PropertyExpression.new(UserItem, None, sort_prop)
        sorter = (Gtk.NumericSorter.new(expr) if numeric or sort_prop != prop else Gtk.StringSorter.new(expr))
        return Gtk.ColumnViewColumn(title=title, factory=factory, sorter=sorter, expand=expand, resizable=True)

    def _visible(self, item: UserItem) -> bool:
        if item.oracle_maintained and not self.oracle_btn.get_active():
            return False
        level = self.level_filter.get_selected()
        if level and item.level != LEVELS[level - 1]:
            return False
        q = self.search.get_text().strip().upper()
        return not q or q in item.name

    def _update_summary(self, model) -> None:
        self.count.set_label(f"{model.get_n_items()} usuarios")
        child = self.summary.get_first_child()
        while child:
            self.summary.remove(child)
            child = self.summary.get_first_child()
        # Recuento por nivel de lo que se ve sin el filtro de nivel (para saber cuántos hay de cada uno).
        show_oracle = self.oracle_btn.get_active()
        users = [self.store.get_item(n) for n in range(self.store.get_n_items())]
        users = [u for u in users if show_oracle or not u.oracle_maintained]
        for level in LEVELS:
            n = sum(1 for u in users if u.level == level)
            self.summary.append(Gtk.Label(label=f"{n} {level.lower()}", css_classes=[LEVEL_CSS[level], "heading"]))

    def _row(self, group: Adw.PreferencesGroup, title: str, value: str, subtitle: str = "") -> None:
        row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(subtitle), subtitle_selectable=True)
        row.add_suffix(Gtk.Label(label=value or "—", selectable=True, css_classes=["numeric"], wrap=True,
                                 xalign=1, max_width_chars=22))
        group.add(row)

    def _show_user(self) -> None:
        child = self.detail.get_first_child()
        while child:
            self.detail.remove(child)
            child = self.detail.get_first_child()
        item = self.selection.get_selected_item()
        if not item:
            self.detail.append(Gtk.Label(label="Elige un usuario para ver su actividad.", wrap=True,
                                         css_classes=["dim-label"], margin_top=24))
            return
        u = item.user
        self.detail.append(Gtk.Label(label=u.name, xalign=0, css_classes=["title-3"], selectable=True))
        self.detail.append(Gtk.Label(label=f"Uso {u.level.lower()} · último uso {idle_text(u)}", xalign=0,
                                     css_classes=[LEVEL_CSS[u.level], "heading"]))
        export = Gtk.Button(label="Exportar (dump)…", halign=Gtk.Align.START, sensitive=not self.exporting,
                            tooltip_text="Volcado del esquema con expdp, descargado a tu equipo")
        export.connect("clicked", lambda *_: self._export_dialog(u, item.container))
        self.detail.append(export)

        now = Adw.PreferencesGroup(title="Ahora")
        self._row(now, "Sesiones abiertas", f"{u.sessions:n}", f"{u.active_sessions:n} ejecutando algo")
        self.detail.append(now)
        if u.clients:
            clients = Adw.PreferencesGroup(title="Conectados ahora")
            for c in sorted(u.clients, key=lambda c: -c.count):
                self._row(clients, GLib.markup_escape_text(c.machine or "?"), f"{c.count:n}",
                          f"{c.program or '?'} · {c.osuser or '?'} · desde {c.since}")
            self.detail.append(clients)

        since = Adw.PreferencesGroup(title="Desde el arranque",
                                     description=f"La instancia arrancó el {self.current_instance.started}")
        self._row(since, "Lecturas lógicas", _human(u.logical_reads), "en sus objetos, por cualquier usuario")
        self._row(since, "Lecturas físicas", _human(u.physical_reads))
        self._row(since, "Bloques cambiados", _human(u.block_changes))
        self._row(since, "Ejecuciones de SQL", _human(u.sql_execs), "lanzadas por este usuario, en memoria")
        self.detail.append(since)

        dates = Adw.PreferencesGroup(title="Fechas")
        self._row(dates, "Última conexión", u.last_login)
        self._row(dates, "Último SQL", u.sql_last)
        self._row(dates, "Último DML", u.dml_last, f"{u.dml:n} filas desde las últimas estadísticas")
        self._row(dates, "Último cambio de estructura", u.ddl_last, "DDL en sus objetos")
        self.detail.append(dates)

        info = Adw.PreferencesGroup(title="Cuenta")
        self._row(info, "Estado", u.status)
        self._row(info, "Tablespace", u.tablespace)
        self._row(info, "Creado", u.created)
        self._row(info, "Objetos", f"{u.objects:n}", f"{u.size_mb:n} MB")
        self.detail.append(info)

    def _show_empty(self, title: str | None = None, description: str | None = None) -> None:
        if title is None:
            title, description = (("Elige una instancia", "Pulsa una máquina para consultarla.")
                                  if self.cfg.machines else
                                  ("No hay máquinas", "Añade una con el botón + de la barra lateral."))
        self.empty.set_title(title)
        self.empty.set_description(GLib.markup_escape_text(description or ""))
        self.stack.set_visible_child_name("empty")

    def _show_instance(self, m: Machine, i: Instance) -> None:
        self.current_instance = i
        self.title.set_label(f"{m.label} · {i.sid} — {i.db_name or '?'}")
        self.details.set_label(" · ".join(x for x in (
            f"Oracle {i.version}", i.open_mode, i.log_mode, "CDB" if i.cdb else "no CDB",
            f"arrancada {i.started}", f"consultada {i.now}", f"pod/contenedor {i.container}" if i.container else "",
            i.oracle_home) if x))
        self.inst_errors.set_label("\n".join(i.errors))
        self.inst_errors.set_visible(bool(i.errors))
        # La columna de contenedor (PDB) solo tiene sentido en una CDB.
        self.container_col.set_visible(len(i.containers) > 1)
        self.store.remove_all()
        for c in i.containers:
            for u in c.users:
                self.store.append(UserItem(u, c.name))
        self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        self._show_user()
        self.stack.set_visible_child_name("data")
        self.split.set_show_content(True)

    # --- volcado (expdp) de un esquema ---

    def _export_dialog(self, u: User, container: str) -> None:
        m, i = self._machine(self.selected[0]), self.current_instance
        if not m or not i or self.exporting:
            return
        # En una CDB, los esquemas de una PDB se exportan entrando en ella; los de CDB$ROOT, desde la raíz.
        pdb = container if i.cdb and container != "CDB$ROOT" else ""
        folder = self.cfg.dump_folder or default_folder()
        folder_row = Adw.ActionRow(title="Carpeta de destino", subtitle=GLib.markup_escape_text(folder),
                                   subtitle_selectable=True)
        pick = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                          tooltip_text="Elegir carpeta")
        folder_row.add_suffix(pick)
        box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        box.append(folder_row)

        def on_pick(*_):
            def done(d, result):
                try:
                    f = d.select_folder_finish(result)
                except GLib.Error:  # cancelado
                    return
                if f and f.get_path():
                    folder_row.set_subtitle(GLib.markup_escape_text(f.get_path()))
                    folder_row.path = f.get_path()

            Gtk.FileDialog(title="Carpeta para el volcado", modal=True,
                           initial_folder=Gio.File.new_for_path(folder_row.path)).select_folder(self, None, done)

        folder_row.path = folder
        pick.connect("clicked", on_pick)
        where = f"{i.sid}{f' / {pdb}' if pdb else ''}"
        dialog = Adw.AlertDialog(
            heading=f"Exportar {u.name}", extra_child=box,
            body=f"Se hace un volcado con expdp en {m.label} ({where}), se descarga a la carpeta elegida junto con "
                 f"su .log y se borra de la máquina. Mientras tanto ocupa allí espacio en DATA_PUMP_DIR (el esquema "
                 f"ocupa {u.size_mb:n} MB).")
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("export", "Exportar")
        dialog.set_response_appearance("export", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            if response != "export":
                return
            self.cfg.dump_folder = folder_row.path
            self.cfg.save()
            self._with_secret(m, lambda secret: self._export(m, i, pdb, u.name, folder_row.path, secret))

        dialog.connect("response", on_response)
        dialog.present(self)

    def _export(self, m: Machine, i: Instance, pdb: str, schema: str, folder: str, secret: str | None) -> None:
        self.exporting = True
        self._show_user()  # desactiva el botón
        self.export_bar.set_visible(True)
        self.export_progress.set_fraction(0)

        def progress(text: str, fraction: float | None) -> None:
            GLib.idle_add(self._export_progress, text, fraction)

        def work():
            try:
                path, warning = export_schema(self._ssh(m, secret), i.sid, pdb, schema, folder, i.db_name, progress)
            except (RuntimeError, OSError) as e:
                GLib.idle_add(self._export_done, schema, None, str(e))
            else:
                GLib.idle_add(self._export_done, schema, path, warning)

        threading.Thread(target=work, daemon=True).start()

    def _export_progress(self, text: str, fraction: float | None) -> None:
        self.export_label.set_label(text)
        if fraction is None:
            self.export_progress.pulse()
        else:
            self.export_progress.set_fraction(fraction)

    def _export_done(self, schema: str, path: Path | None, message: str) -> None:
        self.exporting = False
        self.export_bar.set_visible(False)
        self._show_user()
        if path is None:
            dialog = Adw.AlertDialog(heading=f"No se pudo exportar {schema}", body=message)
            dialog.add_response("ok", "Cerrar")
            dialog.present(self)
            return
        toast = Adw.Toast(title=GLib.markup_escape_text(f"{path.name} descargado" + (f" ({message})" if message
                                                                                      else "")),
                          button_label="Abrir carpeta", timeout=0 if message else 10)
        toast.connect("button-clicked", lambda *_: Gtk.FileLauncher.new(
            Gio.File.new_for_path(str(path))).open_containing_folder(self, None, None))
        self.toasts.add_toast(toast)

    # --- varios ---

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=6))

    def _copy_command(self, via_ssh: bool) -> None:
        i, m = self.current_instance, self.selected and self._machine(self.selected[0])
        if not i or not m or not i.command:
            return self.toast("No hay comando: vuelve a consultar la máquina")
        cmd = i.command
        if via_ssh:
            remote = cmd if m.user == "root" else f"sudo {cmd}"
            port = f"-p {m.port} " if m.port != 22 else ""
            key = f"-i {shlex.quote(m.key_file)} " if m.auth == "key" else ""
            cmd = f"ssh -t {port}{key}{m.user}@{m.host} {shlex.quote(remote)}"
        self.get_clipboard().set(cmd)
        self.toast("Comando copiado" + ("" if via_ssh else ": pégalo como root en la máquina"))

    def _on_oracle_toggled(self, btn: Gtk.ToggleButton) -> None:
        self.cfg.show_oracle = btn.get_active()
        self.cfg.save()
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def _copy(self, *_) -> None:
        model = self.table.get_model()
        cols = [("Contenedor", "container"), *((t, p) for t, p, *_ in COLUMNS),
                ("Última conexión", None), ("Último SQL", None), ("Último DML", None), ("Último DDL", None)]
        lines = ["\t".join(t for t, _ in cols)]
        for n in range(model.get_n_items()):
            it = model.get_item(n)
            u = it.user
            lines.append("\t".join([*(str(it.get_property(p)) for _, p in cols if p),
                                    u.last_login, u.sql_last, u.dml_last, u.ddl_last]))
        self.get_clipboard().set("\n".join(lines))
        self.toast(f"{len(lines) - 1} usuarios copiados")

class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        (self.get_active_window() or Window(self)).present()
