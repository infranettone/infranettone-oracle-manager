# Guía para agentes (y humanos con prisa)

App de escritorio (Python + GTK 4 + libadwaita, con modo terminal) que lista por SSH las instancias Oracle de una máquina y sus
usuarios/esquemas. Uso: [README.md](README.md).

| Módulo | Qué hace |
|---|---|
| `oracle_manager/discover.py` | Script remoto (descubre `ora_pmon_*`, `nsenter` si es un contenedor, `sqlplus / as sysdba`), `ssh` y análisis de la salida en `Instance`/`Container`/`User` |
| `oracle_manager/machines.py` | Modelo `Machine` (validado: nada que ssh tome por opción) y `config.json` |
| `oracle_manager/dump.py` | Volcado de un esquema con `expdp` en la máquina remota, descarga por ssh y limpieza |
| `oracle_manager/usage.py` | Último uso y nivel (Sin uso / Bajo / Medio / Alto) a partir de la actividad |
| `oracle_manager/ui.py` | Máquinas (alta/edición/borrado) con sus instancias debajo a la izquierda; tabla de usuarios (`Gtk.ColumnView`, ordenable, buscador) a la derecha |
| `oracle_manager/__main__.py` | Sin host abre la interfaz; con host, tabla en terminal o `--json` |
| `oracle_manager/version.py` | Versión desde `_version.py` (paquete) o `git describe` |
| `oracle_manager/remote/*.sh` | **Común**: los scripts que se ejecutan en la máquina remota (Python los lee; el `.exe` los lleva incrustados) |
| `tests/fixtures/` | **Común**: salida de ejemplo y resultado esperado; lo comprueban `tests/test_core.py` y `windows/Tests.cs` |
| `windows/Core.cs` | Lo mismo que `discover.py`, `usage.py`, `machines.py` y `dump.py`, en C#: un cambio en uno va también en el otro |
| `windows/MainForm.cs`, `Dialogs.cs`, `Program.cs` | Interfaz WinForms (controles Win32) y arranque |
| `windows/Installer.cs` | El `.exe` se instala a sí mismo por usuario: `%LOCALAPPDATA%\Programs`, menú Inicio y clave Uninstall de HKCU (`--install`, `--uninstall`, `--purge`, `--silent`) |

## Por qué dos núcleos

Windows no trae Python: compartirlo obligaría a empaquetar un intérprete y el `.exe` pasaría de 80 KB a decenas de MB.
Lo que se comparte son los scripts remotos y el fixture. La lógica local (analizar, clasificar, armar el ssh) está en
los dos lenguajes y el fixture avisa si divergen.

## Reglas del proyecto

- **Todo en español** (salida, comentarios, commits, docs), con tildes correctas. Identificadores en inglés.
- Comentarios breves que explican el *porqué*. Sin linter: `python3 -m compileall -q oracle_manager`.
- Lo lento (ssh + sqlplus) va en un hilo y vuelve a la UI con `GLib.idle_add`.
- Commits: título en español, sin líneas de atribución (nada de `Co-Authored-By`). Releases: tag `v*` (SemVer); la CI construye el `.deb` (`packaging/build-deb.sh`) y el `.exe` (`windows/build.ps1`) y los adjunta a la release.

## Trampas conocidas

- **No dejar IP ni nombres de máquinas en el código ni en la documentación**: son datos sensibles.
- **Detección**: cualquier proceso `<prefijo>_pmon_<SID>` cuyo ejecutable sea `…/bin/oracle`. El prefijo cambia
  según la edición (`ora_`, `xe_` en XE —el nombre del proceso sigue siendo `ora_pmon_xe`—, `db_` en 23ai Free,
  `mdb_`…); `asm_`/`apx_` son ASM y se saltan. No usar `pgrep` sin `-f`: compara con `comm` (15 caracteres).
- **Conexión**: `nsenter -u -i -n -p` + `chroot --userspec --groups` (del anfitrión) a `/proc/<pid>/root`, con el
  UID, GID y grupos del PMON (`/proc/<pid>/status`). `nsenter -S/-G` quita los grupos secundarios (sin `dba`:
  ORA-01012) y dentro de los contenedores de Oracle no suele haber `su`.
- La XE 18c en k3s rechaza esa conexión (ORA-12547; el proceso servidor muere en `setsid()`): si no sale la línea
  `DB`, se repite con `crictl exec` (o `docker`/`podman exec`) usando el id de contenedor de `/proc/<pid>/cgroup`.
- La salida de sqlplus se etiqueta (`INST`, `DB`, `CON`, `USR`) y se separa con tabuladores: las líneas sin etiqueta
  se ignoran salvo los errores `ORA-`/`SP2-`.
- `cdb_*` y `v$containers` también existen en una no CDB (con `con_id` 0). Ni esas vistas ni `oracle_maintained`
  existen antes de 12c: con 11g saldrían errores `ORA-` y no se listan usuarios.
- Clave / certificado: `-i` + `CertificateFile=` con `IdentitiesOnly=yes` y `PreferredAuthentications=publickey`
  (que no caiga en pedir contraseña). Si la clave está cifrada (`ssh-keygen -y -P ""` falla), la frase de paso va
  por `SSH_ASKPASS` como la contraseña, sin `BatchMode`.
- La interfaz no tiene terminal: ssh va con `BatchMode=yes` (agente) o `SSH_ASKPASS_REQUIRE=force` (contraseña),
  sin `SSH_AUTH_SOCK` y con `PubkeyAuthentication=no` para que no pruebe claves.
- **Nada de AWR/ASH** (`dba_hist_*`, `v$active_session_history`): requieren la licencia Diagnostics Pack aunque
  `control_management_pack_access` lo permita. La actividad sale de `v$session`, `v$sql`, `v$segment_statistics`,
  `dba_tab_modifications` y `dba_users.last_login`, y no se escribe nada (ni `flush_database_monitoring_info`).
- `oracle_maintained = 'Y'` miente con usuarios creados con `"_ORACLE_SCRIPT"=true`: solo se da por bueno si el
  usuario se creó el mismo día que SYS en ese contenedor.
- **Volcados** (`dump.py`): `expdp "/ as sysdba"` por el mismo camino que sqlplus (chroot o exec del contenedor). Para
  una PDB, `ORACLE_PDB_SID` (disparador `DBMS_SET_PDB`, 18c+). Nada de `compression=` (licencia Advanced
  Compression). Si la carpeta de `DATA_PUMP_DIR` no existe (ORA-39070/ORA-29283), se crea con el dueño del PMON. El .dmp se lee por `/proc/<pid>/root/<DATA_PUMP_DIR>` y se borra siempre al acabar. En XE, expdp
  da ORA-31642 en `DBMS_CUBE_EXP` (no trae OLAP): es un aviso, el volcado vale.
- **Windows**: solo C# 5 (lo que compila el `csc.exe` de `%windir%\Microsoft.NET\Framework64\v4.0.30319`, sin SDK ni
  NuGet), sobre .NET Framework 4.8, que ya viene en Windows. `.NET` no deja elegir la codificación de stdin: el script
  se escribe en bytes UTF-8 sin BOM y sin CR (`.gitattributes` fuerza LF en los `.sh`). `SSH_ASKPASS` apunta al propio
  `.exe`, que escribe la contraseña si ve `ORACLE_MANAGER_SSH_PASSWORD`. Sin modo terminal, por ahora.
- OpenSSH avisa por stderr del intercambio de claves no post-cuántico (líneas con `**`): no es un error.

## Probar

```bash
python3 -m oracle_manager             # interfaz
python3 -m oracle_manager HOST        # terminal
python3 -m unittest discover -s tests # pruebas del núcleo
```

En Windows, `windows\build.ps1` compila, pasa las pruebas y deja el `.exe` en `dist\`.
