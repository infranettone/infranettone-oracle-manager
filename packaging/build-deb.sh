#!/usr/bin/env bash
# Construye dist/infranettone-oracle_<versión>_all.deb. Uso: packaging/build-deb.sh [versión]
set -euo pipefail

cd "$(dirname "$0")/.."
VERSION="${1:-0.0.0}"
VERSION="${VERSION#v}"
PKG=infranettone-oracle
ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT

# Código: en dist-packages para que /usr/bin/python3 -m oracle_manager funcione.
LIB="$ROOT/usr/lib/python3/dist-packages/oracle_manager"
install -d "$LIB"
install -m 644 oracle_manager/*.py "$LIB/"
echo "VERSION = \"$VERSION\"" > "$LIB/_version.py"  # la que enseña la app (version.py)

install -D -m 755 /dev/stdin "$ROOT/usr/bin/$PKG" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 -m oracle_manager "$@"
EOF

install -D -m 644 packaging/com.infranettone.OracleManager.desktop \
  "$ROOT/usr/share/applications/com.infranettone.OracleManager.desktop"
install -D -m 644 README.md "$ROOT/usr/share/doc/$PKG/README.md"

install -d "$ROOT/DEBIAN"
cat > "$ROOT/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: database
Priority: optional
Architecture: all
Maintainer: Raul Adamuz <radamuz16@gmail.com>
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.6), openssh-client
Installed-Size: $(du -sk "$ROOT/usr" | cut -f1)
Homepage: https://github.com/infranettone/infranettone-oracle-manager
Description: Bases de datos Oracle y sus usuarios/esquemas, por SSH
 App GTK4/libadwaita (con modo terminal) que descubre por SSH las instancias
 Oracle de varias máquinas, lista sus usuarios/esquemas con su nivel de uso,
 exporta un esquema con expdp a una carpeta local y copia el comando para entrar
 en sqlplus.
EOF

cat > "$ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
command -v py3compile >/dev/null && py3compile -p infranettone-oracle /usr/lib/python3/dist-packages/oracle_manager || true
EOF
cat > "$ROOT/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
command -v py3clean >/dev/null && py3clean -p infranettone-oracle || true
find /usr/lib/python3/dist-packages/oracle_manager -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
EOF
chmod 755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/prerm"

find "$ROOT" -type d -exec chmod 755 {} +

mkdir -p dist
dpkg-deb --root-owner-group --build "$ROOT" "dist/${PKG}_${VERSION}_all.deb"
