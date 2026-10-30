# Oracle Manager

App de escritorio (GTK4 + libadwaita) que entra por SSH en una máquina con Oracle y lista sus **bases de datos** (instancias) y los
**usuarios/esquemas** de cada una, con estado, tablespace por defecto, fecha de creación, número de objetos y espacio
ocupado. No hay que saber los SID ni ninguna contraseña de Oracle.

Documentación para desarrollar: [AGENTS.md](AGENTS.md).

## Instalar

Descarga el `.deb` de la [última release](https://github.com/infranettone/infranettone-oracle-manager/releases/latest)
e instálalo con `sudo apt install ./infranettone-oracle_*_all.deb`. Se abre desde el menú de aplicaciones o con
`infranettone-oracle`.

En Windows 10/11, descarga el `.exe` de la misma release y ábrelo: se ofrece a instalarse (sin administrador) y queda
en el menú Inicio y en el buscador de Windows. Para actualizar, abre el `.exe` de una versión nueva. Se desinstala
abriendo de nuevo ese `.exe` o desde **Configuración > Aplicaciones instaladas**, y deja elegir entre conservar las
máquinas guardadas (por defecto) o borrarlas. Solo necesita el Cliente OpenSSH
y .NET Framework 4.8, que ya vienen con el sistema. Las máquinas se guardan en
`%APPDATA%\infranettone-oracle\config.json`. No tiene modo terminal. No está firmado, así que SmartScreen puede avisar
la primera vez.

## Uso

`python3 -m oracle_manager` abre la interfaz. Con **+** se añaden máquinas (nombre, host, usuario SSH, puerto y
autenticación: **Agente SSH / claves**, **Contraseña** o **Clave / certificado**); el menú ⋮ de cada una permite consultarla, editarla o
eliminarla. Al abrir se consultan solas las que usan el agente; las de contraseña la piden al pulsarlas y no se
guarda. Debajo de cada máquina salen sus instancias; al elegir una, la tabla de usuarios se ordena por cualquier
columna, tiene buscador, un botón para mostrar los usuarios que crea Oracle y otro para copiar la lista (TSV).
Las máquinas se guardan en `~/.config/infranettone-oracle/config.json`.

También por terminal:

```bash
python3 -m oracle_manager HOST                  # usuarios de aplicación
python3 -m oracle_manager HOST --all            # también SYS, SYSTEM, XDB… (marcados con *)
python3 -m oracle_manager HOST --json           # para otros programas
python3 -m oracle_manager HOST --password       # contraseña SSH en vez del agente
python3 -m oracle_manager HOST -u admin -p 2222 -i ~/.ssh/id_ed25519
python3 -m oracle_manager HOST -i ~/.ssh/id_ed25519 -c ~/.ssh/id_ed25519-cert.pub   # certificado de una CA
```

Necesita **root** en la máquina remota (por defecto entra como `root`) o un usuario con `sudo` sin contraseña.
Con el agente usa las claves del ssh-agent o de `~/.ssh`. Con contraseña, ssh no prueba ninguna clave y la recibe
por `SSH_ASKPASS` (el programa auxiliar no la contiene; solo va en el entorno del proceso ssh). Con **Clave / certificado**
se elige la clave privada y, opcionalmente, el certificado firmado por la CA (`*-cert.pub`; si no se elige, ssh
usa el que haya junto a la clave); solo se prueba esa clave, y si está cifrada la frase de paso se pide al
consultar y no se guarda. Desde la interfaz,
la huella de una máquina nueva se acepta sola (`StrictHostKeyChecking=accept-new`) y si cambia, falla.

## Uso de los esquemas

Cada usuario tiene un nivel de **uso** (Sin uso, Bajo, Medio, Alto) y su **último uso**; la tabla se filtra por nivel
y arriba se ve cuántos hay de cada uno. Al elegir un usuario, el panel de la derecha muestra el detalle: sesiones
abiertas ahora (máquina, programa y usuario del sistema), lecturas y cambios en sus objetos y ejecuciones de SQL
desde el arranque, y las fechas de última conexión, último SQL, último DML y último cambio de estructura. El botón ⓘ
explica cómo se calcula.

Todo sale de vistas que Oracle mantiene siempre, en solo lectura y **sin AWR/ASH** (exigen la licencia Diagnostics
Pack). Límites: los contadores empiezan en el arranque de la instancia y no hay histórico de conexiones; para eso
habría que activar la auditoría de `LOGON`.

## Exportar un esquema (dump)

En el panel de un usuario, **Exportar (dump)…** pide la carpeta de destino (por defecto, Descargas; la última
elegida se recuerda) y hace un `expdp` del esquema con `flashback_time=systimestamp` (copia coherente). El volcado
se escribe en el `DATA_PUMP_DIR` de la base (o de la PDB), se descarga por ssh con compresión junto con su `.log`
como `<ESQUEMA>_<BASE o PDB>_<fecha>.dmp` y se borra de la máquina. Mientras dura ocupa allí espacio en disco. Sin
la compresión de expdp, que exige la licencia Advanced Compression.

## Cómo funciona

1. Manda un script a `bash -s` por SSH (por stdin: nada pasa por el shell local).
2. Busca los procesos `ora_pmon_<SID>` y lee `ORACLE_SID` y `ORACLE_HOME` de su entorno.
3. Si la instancia corre en un contenedor (docker, k3s…), entra en sus espacios
   de nombres con `nsenter`; si no, en la propia máquina.
4. Ejecuta `sqlplus / as sysdba` como el dueño del proceso y consulta `v$database`, `v$containers`, `cdb_users`,
   `cdb_objects` y `cdb_segments` (en una CDB salen todas las PDB abiertas).
