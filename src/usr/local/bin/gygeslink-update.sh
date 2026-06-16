#!/bin/bash
# GygesLink — OTA Update
# Télécharge, vérifie et installe une mise à jour depuis GitHub Releases.
# Le téléchargement passe par Tor (SOCKS5 127.0.0.1:9050).
# La signature GPG et les checksums SHA256 sont vérifiés avant extraction.
# L'archive ne peut contenir que des chemins whitelistés.
# Si overlayfs est actif, le rootfs est remonté en écriture puis en lecture seule.

set -euo pipefail

VERSION_FILE="/data/gygeslink/version.txt"
PUBKEY_FILE="/etc/gygeslink/update-pubkey.gpg"
DOWNLOAD_DIR="/data/gygeslink/updates"
OVERLAYFS_FLAG="/data/gygeslink/overlayfs-enabled"
STATUS_FILE="/data/gygeslink/update-status.json"
GITHUB_REPO="gregspk/gygeslink"

ALLOWED_PREFIXES=(
    "./usr/local/bin/"
    "./etc/gygeslink/"
    "./etc/tor/"
    "./etc/systemd/system/gygeslink-"
    "./data/gygeslink/version.txt"
)

OVERLAYFS_WAS_ACTIVE=0

LOG() { echo "[gygeslink-update] $*"; }
ERR() { echo "[gygeslink-update] ERREUR: $*" >&2; }

write_status() {
    local status="$1"
    local progress="${2:-0}"
    local message="${3:-}"
    cat > "$STATUS_FILE" << EOF
{"status": "$status", "progress": $progress, "version": "", "message": "$message"}
EOF
}

write_status_version() {
    local status="$1"
    local progress="${2:-0}"
    local version="${3:-}"
    local message="${4:-}"
    cat > "$STATUS_FILE" << EOF
{"status": "$status", "progress": $progress, "version": "$version", "message": "$message"}
EOF
}

# ── 1. Vérifier la whitelist de l'archive ─────────────────────────
verify_whitelist() {
    local archive="$1"

    local invalid
    invalid=$(tar tzf "$archive" 2>/dev/null | while read -r entry; do
        case "$entry" in
            ./VERSION) continue ;;
            ./SHA256SUMS|./SHA256SUMS.sig) continue ;;
        esac

        local allowed=false
        for prefix in "${ALLOWED_PREFIXES[@]}"; do
            if [[ "$entry" == "$prefix"* ]]; then
                allowed=true
                break
            fi
        done

        if [[ "$allowed" == "false" ]]; then
            echo "$entry"
        fi
    done)

    if [[ -n "$invalid" ]]; then
        ERR "Fichiers non autorisés dans l'archive :"
        echo "$invalid" >&2
        return 1
    fi

    LOG "Whitelist vérifiée."
    return 0
}

# ── 2. Vérifier la signature GPG et les checksums ────────────────
verify_signature() {
    local archive="$1"
    local sums="$2"
    local sig="$3"

    if [[ ! -f "$PUBKEY_FILE" ]]; then
        ERR "Clé publique GPG introuvable : $PUBKEY_FILE"
        return 1
    fi

    if ! gpg --batch --no-default-keyring --keyring "$PUBKEY_FILE" \
         --verify "$sig" "$sums" 2>/dev/null; then
        ERR "Signature GPG invalide. L'archive est corrompue ou falsifiée."
        return 1
    fi

    LOG "Signature GPG vérifiée."

    local archive_name
    archive_name=$(basename "$archive")

    if ! (cd "$(dirname "$archive")" && sha256sum -c "$sums" 2>/dev/null); then
        ERR "Checksum SHA256 invalide."
        return 1
    fi

    LOG "Checksum SHA256 vérifié."
    return 0
}

# ── 3. Gérer overlayfs ────────────────────────────────────────────
disable_overlayfs() {
    if [[ -f "$OVERLAYFS_FLAG" ]] && [[ "$(cat "$OVERLAYFS_FLAG" 2>/dev/null)" == "1" ]]; then
        LOG "Overlayfs actif — remontage du rootfs en écriture..."
        mount -o remount,rw /
        OVERLAYFS_WAS_ACTIVE=1
    else
        LOG "Overlayfs non actif — rootfs déjà en écriture."
        OVERLAYFS_WAS_ACTIVE=0
    fi
}

enable_overlayfs() {
    if [[ "$OVERLAYFS_WAS_ACTIVE" == "1" ]]; then
        LOG "Réactivation overlayfs — remontage du rootfs en lecture seule..."
        mount -o remount,ro /
    fi
}

# ── 4. Extraire l'archive ─────────────────────────────────────────
extract_update() {
    local archive="$1"
    local version="$2"

    LOG "Extraction de la mise à jour v${version}..."

    mkdir -p /data/gygeslink/rollback

    tar xzf "$archive" -C /

    LOG "Fichiers extraits avec succès."
}

# ── Main ──────────────────────────────────────────────────────────
main() {
    local archive="${1:?Usage: gygeslink-update.sh <archive.tar.gz> <version>}"
    local version="${2:?Version requise}"

    if [[ ! -f "$archive" ]]; then
        ERR "Archive introuvable : $archive"
        write_status "error" 0 "Archive introuvable"
        exit 1
    fi

    write_status "verifying" 40 "Vérification de la signature..."

    # Vérifications de sécurité
    if ! verify_whitelist "$archive"; then
        write_status "error" 0 "Fichiers non autorisés dans l'archive"
        exit 1
    fi

    local sums="${DOWNLOAD_DIR}/SHA256SUMS"
    local sig="${DOWNLOAD_DIR}/SHA256SUMS.sig"

    if ! verify_signature "$archive" "$sums" "$sig"; then
        write_status "error" 0 "Signature ou checksum invalide"
        exit 1
    fi

    write_status "installing" 60 "Installation des fichiers..."

    # Gestion overlayfs
    disable_overlayfs

    # Extraction
    if ! extract_update "$archive" "$version"; then
        enable_overlayfs
        write_status "error" 0 "Échec de l'extraction"
        exit 1
    fi

    # Mise à jour du fichier version
    echo "$version" > "$VERSION_FILE"
    LOG "Version mise à jour : $version"

    # Réactivation overlayfs si nécessaire
    enable_overlayfs

    write_status_version "done" 100 "$version" "Mise à jour terminée. Redémarrage..."

    LOG "Mise à jour terminée. Redémarrage dans 3 secondes..."
    sleep 3
    systemctl reboot
}

main "$@"