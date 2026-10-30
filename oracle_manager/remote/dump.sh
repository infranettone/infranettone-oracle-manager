set -u
fail() { printf 'ERR\t%s\n' "$*"; exit 1; }
pid=""
for d in /proc/[0-9]*; do
    args=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
    [[ $args =~ ^([a-z0-9]+)_pmon_([^ ]+) ]] || continue
    [[ ${BASH_REMATCH[1]} == asm || ${BASH_REMATCH[1]} == apx ]] && continue
    [[ $(readlink "$d/exe" 2>/dev/null) == */bin/oracle* ]] || continue
    envof() { tr '\0' '\n' < "$d/environ" 2>/dev/null | sed -n "s/^$1=//p" | head -n1; }
    sid=$(envof ORACLE_SID); [ -n "$sid" ] || sid=${BASH_REMATCH[2]}
    [ "$sid" = "$SID" ] || continue
    home=$(envof ORACLE_HOME); [ -n "$home" ] || home=$(readlink "$d/exe" | sed 's|/bin/oracle.*||')
    pid=${d#/proc/}
    break
done
[ -n "$pid" ] || fail "La instancia $SID ya no está en marcha"
ids=$(awk '/^Uid:/ {u=$2} /^Gid:/ {g=$2} /^Groups:/ {$1=""; gs=$0} END {gsub(/^ +| +$/, "", gs); gsub(/ +/, ",", gs); print u":"g" "gs}' "$d/status")
groups=${ids#* }
cid=$(grep -oE '[0-9a-f]{64}' "$d/cgroup" | tail -n1)
envs=(ORACLE_SID="$sid" ORACLE_HOME="$home" NLS_LANG=AMERICAN_AMERICA.AL32UTF8)
# Con una PDB, el disparador DBMS_SET_PDB (18c en adelante) cambia a ella al entrar como sysdba.
[ -n "$PDB" ] && envs+=(ORACLE_PDB_SID="$PDB")
# Igual que al consultar: chroot con los grupos del PMON y, si la base lo rechaza, el exec del motor de contenedores.
ora() {
    local prog=$home/bin/$1; shift
    case $mode in
        chroot) env "${envs[@]}" nsenter -t "$pid" -u -i -n -p -- \
                    chroot --userspec="${ids%% *}" ${groups:+--groups="$groups"} "$d/root" "$prog" "$@" ;;
        crictl) crictl exec -i "$cid" env "${envs[@]}" "$prog" "$@" ;;
        *) $mode exec -i -u "${ids%% *}" "${envs[@]/#/--env=}" "$cid" "$prog" "$@" ;;
    esac
}
Q="set pagesize 0 linesize 32767 trimspool on feedback off heading off verify off
select 'DIR'||chr(9)||directory_path from dba_directories where directory_name = 'DATA_PUMP_DIR';
select 'OWN'||chr(9)||count(*) from dba_users where username = '$SCHEMA';
select 'CON'||chr(9)||sys_context('USERENV', 'CON_NAME') from dual;
exit"
mode=chroot
out=$(ora sqlplus -S -L / as sysdba <<<"$Q" 2>&1)
if ! grep -q $'^DIR\t' <<<"$out" && [ -n "$cid" ]; then
    if command -v crictl >/dev/null && crictl inspect "$cid" >/dev/null 2>&1; then
        mode=crictl
    else
        for rt in docker podman; do
            command -v $rt >/dev/null && $rt inspect "$cid" >/dev/null 2>&1 && { mode=$rt; break; }
        done
    fi
    [ "$mode" = chroot ] || out=$(ora sqlplus -S -L / as sysdba <<<"$Q" 2>&1)
fi
dir=$(sed -n 's/^DIR\t//p' <<<"$out")
[ -n "$dir" ] || fail "No se puede usar el directorio DATA_PUMP_DIR: $(grep -m1 -E '^(ORA|SP2)-' <<<"$out")"
[ -z "$PDB" ] || grep -qx $'CON\t'"$PDB" <<<"$out" || fail "No se pudo entrar en la PDB $PDB"
grep -qx $'OWN\t1' <<<"$out" || fail "No existe el usuario $SCHEMA"
host_dir=$d/root${dir%/}
# DATA_PUMP_DIR apunta a una carpeta que muchas imágenes no crean (ORA-29283): se crea con el dueño del PMON.
if [ ! -d "$host_dir" ]; then
    printf 'STEP\tCreando %s…\n' "$dir"
    uidgid=${ids%% *}
    mkdir -p "$host_dir" && chown "$uidgid" "$host_dir" && chmod 750 "$host_dir" \
        || fail "No se pudo crear $dir en el contenedor"
fi
printf 'STEP\tExportando %s con expdp…\n' "$SCHEMA"
# flashback_time: copia coherente de todas las tablas en el mismo instante. Sin compresión (exige licencia).
ora expdp '"/ as sysdba"' schemas="$SCHEMA" directory=DATA_PUMP_DIR dumpfile="$NAME.dmp" logfile="$NAME.log" \
    reuse_dumpfiles=y flashback_time=systimestamp </dev/null 2>&1
rc=$?
printf 'RC\t%s\n' "$rc"
[ -f "$host_dir/$NAME.dmp" ] || fail "expdp no ha generado el volcado (código $rc)"
printf 'FILE\t%s\t%s\n' "$host_dir/$NAME.dmp" "$(stat -c %s "$host_dir/$NAME.dmp")"
printf 'LOG\t%s\n' "$host_dir/$NAME.log"
