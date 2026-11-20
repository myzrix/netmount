#!/bin/bash
# Installe le helper root de netmount et la règle sudoers qui permet à
# l'utilisateur de l'appeler sans mot de passe (et rien d'autre).
#   sudo ./install.sh [utilisateur]        (ou via le bouton de l'appli)
#   sudo ./install.sh --uninstall [utilisateur]
set -euo pipefail

HELPER=/usr/local/sbin/netmount-helper
SRC="$(cd "$(dirname "$0")" && pwd)/netmount-helper"

if [ "$(id -u)" -ne 0 ]; then
    echo "À lancer en root : sudo $0" >&2
    exit 1
fi

UNINSTALL=0
if [ "${1:-}" = "--uninstall" ]; then UNINSTALL=1; shift; fi
USR="${1:-${SUDO_USER:-}}"
if [ -z "$USR" ] && [ -n "${PKEXEC_UID:-}" ]; then
    USR="$(id -nu "$PKEXEC_UID")"
fi
if [ -z "$USR" ] || ! id "$USR" >/dev/null 2>&1; then
    echo "Utilisateur inconnu : passer le nom en argument" >&2
    exit 1
fi
SUDOERS="/etc/sudoers.d/netmount-$USR"

if [ "$UNINSTALL" = 1 ]; then
    rm -f "$SUDOERS"
    [ -n "$(ls /etc/sudoers.d/netmount-* 2>/dev/null)" ] || rm -f "$HELPER"
    echo "Désinstallé pour $USR."
    exit 0
fi

install -o root -g root -m 0755 "$SRC" "$HELPER"

TMP="$(mktemp)"
echo "$USR ALL=(root) NOPASSWD: $HELPER" > "$TMP"
visudo -cf "$TMP" >/dev/null
install -o root -g root -m 0440 "$TMP" "$SUDOERS"
rm -f "$TMP"

echo "OK : $HELPER installé, $USR peut l'appeler sans mot de passe."
