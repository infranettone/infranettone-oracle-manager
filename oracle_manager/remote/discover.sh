set -u
mntns=$(readlink /proc/1/ns/mnt)
SQL=$(cat <<'SQL'
set pagesize 0 linesize 32767 trimspool on trimout on feedback off heading off verify off echo off tab off
select 'DB'||chr(9)||d.name||chr(9)||d.db_unique_name||chr(9)||d.cdb||chr(9)||d.open_mode||chr(9)||d.log_mode
       ||chr(9)||i.version||chr(9)||i.host_name||chr(9)||to_char(i.startup_time, 'YYYY-MM-DD HH24:MI')
       ||chr(9)||to_char(sysdate, 'YYYY-MM-DD HH24:MI')
  from v$database d, v$instance i;
select 'CON'||chr(9)||con_id||chr(9)||name||chr(9)||open_mode from v$containers order by con_id;
select 'USR'||chr(9)||u.con_id||chr(9)||u.username||chr(9)||u.account_status||chr(9)||u.default_tablespace
       ||chr(9)||to_char(u.created, 'YYYY-MM-DD')||chr(9)||u.oracle_maintained||chr(9)||nvl(o.n, 0)||chr(9)||nvl(s.mb, 0)
  from cdb_users u
  left join (select con_id, owner, count(*) n from cdb_objects group by con_id, owner) o
         on o.con_id = u.con_id and o.owner = u.username
  left join (select con_id, owner, round(sum(bytes) / 1048576) mb from cdb_segments group by con_id, owner) s
         on s.con_id = u.con_id and s.owner = u.username
 order by u.con_id, u.username;
-- Actividad, solo con vistas sin licencia extra (nada de AWR/ASH, que exigen el Diagnostics Pack) y sin escribir
-- nada. v$sql y v$segment_statistics cuentan desde el arranque (v$sql, solo lo que siga en la shared pool);
-- dba_tab_modifications, desde las últimas estadísticas de cada tabla.
select 'ACT'||chr(9)||u.con_id||chr(9)||u.username||chr(9)||to_char(u.last_login, 'YYYY-MM-DD HH24:MI')
       ||chr(9)||nvl(s.n, 0)||chr(9)||nvl(s.act, 0)||chr(9)||nvl(q.ex, 0)||chr(9)||to_char(q.last, 'YYYY-MM-DD HH24:MI')
       ||chr(9)||nvl(g.lr, 0)||chr(9)||nvl(g.pr, 0)||chr(9)||nvl(g.bc, 0)
       ||chr(9)||nvl(m.dml, 0)||chr(9)||to_char(m.last, 'YYYY-MM-DD HH24:MI')||chr(9)||to_char(o.ddl, 'YYYY-MM-DD HH24:MI')
  from cdb_users u
  left join (select con_id, username, count(*) n, sum(decode(status, 'ACTIVE', 1, 0)) act
               from v$session where username is not null and type = 'USER' group by con_id, username) s
         on s.con_id = u.con_id and s.username = u.username
  left join (select con_id, parsing_schema_name owner, sum(executions) ex, max(last_active_time) last
               from v$sql group by con_id, parsing_schema_name) q
         on q.con_id = u.con_id and q.owner = u.username
  left join (select con_id, owner, sum(decode(statistic_name, 'logical reads', value)) lr,
                    sum(decode(statistic_name, 'physical reads', value)) pr,
                    sum(decode(statistic_name, 'db block changes', value)) bc
               from v$segment_statistics
              where statistic_name in ('logical reads', 'physical reads', 'db block changes')
              group by con_id, owner) g
         on g.con_id = u.con_id and g.owner = u.username
  left join (select con_id, table_owner owner, sum(inserts + updates + deletes) dml, max(timestamp) last
               from cdb_tab_modifications group by con_id, table_owner) m
         on m.con_id = u.con_id and m.owner = u.username
  left join (select con_id, owner, max(last_ddl_time) ddl from cdb_objects group by con_id, owner) o
         on o.con_id = u.con_id and o.owner = u.username;
-- Quién está conectado ahora (no hay histórico de conexiones sin auditoría).
select 'SES'||chr(9)||con_id||chr(9)||username||chr(9)||replace(machine, chr(9), ' ')||chr(9)||replace(program, chr(9), ' ')
       ||chr(9)||replace(osuser, chr(9), ' ')||chr(9)||count(*)||chr(9)||to_char(min(logon_time), 'YYYY-MM-DD HH24:MI')
  from v$session where username is not null and type = 'USER'
 group by con_id, username, machine, program, osuser;
exit
SQL
)
# El PMON de cada instancia se llama <prefijo>_pmon_<SID> y el prefijo depende de la edición: ora_ (normal),
# xe_ (XE), db_ (23ai Free), mdb_ (base de gestión de Grid)… No se fía del prefijo: vale cualquiera, siempre que el
# ejecutable sea el binario «oracle». Las ASM (asm_, apx_) no son bases de datos y se saltan.
for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    args=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
    [[ $args =~ ^([a-z0-9]+)_pmon_([^ ]+) ]] || continue
    [[ ${BASH_REMATCH[1]} == asm || ${BASH_REMATCH[1]} == apx ]] && continue
    [[ $(readlink "$d/exe" 2>/dev/null) == */bin/oracle* ]] || continue
    envof() { tr '\0' '\n' < "$d/environ" 2>/dev/null | sed -n "s/^$1=//p" | head -n1; }
    sid=$(envof ORACLE_SID); home=$(envof ORACLE_HOME)
    [ -n "$sid" ] || sid=${BASH_REMATCH[2]}
    [ -n "$home" ] || home=$(readlink "$d/exe" | sed 's|/bin/oracle.*||')
    where=""
    if [ "$(readlink "$d/ns/mnt")" != "$mntns" ]; then
        where=$(envof HOSTNAME); [ -n "$where" ] || where=contenedor
    fi
    printf 'INST\t%s\t%s\t%s\n' "$sid" "$home" "$where"
    # «/ as sysdba» exige los mismos grupos que el PMON (dba, oinstall…). nsenter -S/-G los quitaría, así que se
    # entra en los espacios de nombres sin el de montaje y chroot (del anfitrión) pone UID, GID y grupos y salta a
    # la raíz del proceso (/proc/<pid>/root): vale igual en la máquina o en un contenedor, sin su ni nada dentro.
    ids=$(awk '/^Uid:/ {u=$2} /^Gid:/ {g=$2} /^Groups:/ {$1=""; gs=$0} END {gsub(/^ +| +$/, "", gs); gsub(/ +/, ",", gs); print u":"g" "gs}' "$d/status")
    groups=${ids#* }
    cid=$(grep -oE '[0-9a-f]{64}' "$d/cgroup" | tail -n1)
    # Comando para entrar a mano (como root en la máquina) en sqlplus de esta instancia.
    run="env ORACLE_SID=$sid ORACLE_HOME=$home $home/bin/sqlplus / as sysdba"
    if [ -z "$where" ]; then
        cmd="runuser -u $(stat -c %U "$d") -- $run"
    elif [ -n "$cid" ] && command -v crictl >/dev/null && crictl inspect "$cid" >/dev/null 2>&1; then
        cmd="crictl exec -it $cid $run"
    else
        cmd="nsenter -t $pid -u -i -n -p -- chroot --userspec=${ids%% *} ${groups:+--groups=$groups} $d/root $run"
        for rt in docker podman; do
            command -v $rt >/dev/null && $rt inspect "$cid" >/dev/null 2>&1 && { cmd="$rt exec -it -u ${ids%% *} $cid $run"; break; }
        done
    fi
    printf 'CMD\t%s\n' "$cmd"
    out=$(ORACLE_SID=$sid ORACLE_HOME=$home NLS_LANG=AMERICAN_AMERICA.AL32UTF8 \
        nsenter -t "$pid" -u -i -n -p -- \
        chroot --userspec="${ids%% *}" ${groups:+--groups="$groups"} "$d/root" \
        "$home/bin/sqlplus" -S -L / as sysdba <<<"$SQL" 2>&1)
    # Algunas (p. ej. XE 18c en k3s) rechazan con ORA-12547 una conexión que no nace dentro del contenedor: entonces
    # se usa el exec del propio motor de contenedores, como hace `kubectl exec`.
    if ! grep -q $'^DB\t' <<<"$out" && [ -n "$where" ] && [ -n "$cid" ]; then
        envs=(ORACLE_SID="$sid" ORACLE_HOME="$home" NLS_LANG=AMERICAN_AMERICA.AL32UTF8)
        if command -v crictl >/dev/null && crictl inspect "$cid" >/dev/null 2>&1; then
            # crictl exec no deja elegir usuario: entra con el de la imagen (en las de Oracle, «oracle»).
            out=$(crictl exec -i "$cid" env "${envs[@]}" "$home/bin/sqlplus" -S -L / as sysdba <<<"$SQL" 2>&1)
        else
            for rt in docker podman; do
                command -v $rt >/dev/null && $rt inspect "$cid" >/dev/null 2>&1 || continue
                out=$($rt exec -i -u "${ids%% *}" "${envs[@]/#/--env=}" "$cid" \
                      "$home/bin/sqlplus" -S -L / as sysdba <<<"$SQL" 2>&1)
                break
            done
        fi
    fi
    printf '%s\n' "$out"
done
