#!/bin/bash
set -e

# VM VPN Installer / Updater
# Usage: curl -fsSL https://raw.githubusercontent.com/carrerfe/vm-vpn/main/install.sh | bash
# Or from a local checkout: VMVPN_SOURCE_DIR=/path/to/checkout ./install.sh
#
# Environment overrides:
#   VMVPN_INSTALL_DIR   Install target (default: ~/.local/bin)
#   VMVPN_SOURCE_DIR    Install from a local checkout instead of GitHub
#   VMVPN_LIMA_VERSION  Lima release to install when missing (default: 2.0.3)
#   VMVPN_SKIP_DEPS=1   Skip the system-package check entirely
#   VMVPN_ASSUME_YES=1  Answer yes to all prompts (also usable without a tty)
#   VMVPN_NO_LAUNCH=1   Never launch the setup window at the end

REPO="carrerfe/vm-vpn"
INSTALL_DIR="${VMVPN_INSTALL_DIR:-$HOME/.local/bin}"
SOURCE_DIR="${VMVPN_SOURCE_DIR:-}"
REPO_URL="https://github.com/$REPO"
RAW_URL="https://raw.githubusercontent.com/$REPO/main"
GUI_DIR="$INSTALL_DIR/vmvpn-gui"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$APPS_DIR/io.github.carrerfe.VmVpn.desktop"
CONFIG_FILE="$INSTALL_DIR/vpn-config.json"
LIMA_VERSION="${VMVPN_LIMA_VERSION:-2.0.3}"

# Detect install vs update
if [[ -f "$INSTALL_DIR/vmvpn" ]]; then
    MODE="update"
    echo "Updating vm-vpn..."
else
    MODE="install"
    echo "Installing vm-vpn..."
fi

# -- prompting ---------------------------------------------------------------
# The installer may run as `curl … | bash`, so stdin is the script itself.
# Interactive answers are read from /dev/tty when one is available.

tty_available() {
    # stderr must be redirected before the input redirect is attempted, or a
    # missing /dev/tty leaks an error line.
    : 2>/dev/null < /dev/tty
}

# ask_yes "prompt" → 0 yes, 1 no, 2 no tty (caller prints the command).
ask_yes() {
    local reply=""
    if [[ "${VMVPN_ASSUME_YES:-}" == "1" ]]; then
        echo "$1 y"
        return 0
    fi
    if ! tty_available; then
        return 2
    fi
    if ! read -r -p "$1 " reply < /dev/tty; then
        return 2
    fi
    [[ -z "$reply" || "$reply" =~ ^[Yy] ]]
}

# -- system dependencies -------------------------------------------------------

DISTRO_ID=""
DISTRO_LIKE=""
detect_distro() {
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        DISTRO_ID="${ID:-}"
        DISTRO_LIKE="${ID_LIKE:-}"
    fi
}

pkg_installed() {
    if command -v dpkg-query >/dev/null 2>&1; then
        dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null \
            | grep -q installed
    elif command -v rpm >/dev/null 2>&1; then
        rpm -q "$1" >/dev/null 2>&1
    else
        return 1
    fi
}

# The command/functionality a package provides (empty: package check only).
probe_for_pkg() {
    case "$1" in
        qemu-system-x86)                 echo qemu-system-x86_64 ;;
        qemu-system-arm|qemu-system-aarch64) echo qemu-system-aarch64 ;;
        qemu-utils|qemu-img)             echo qemu-img ;;
        jq)                              echo jq ;;
        openssh-client|openssh-clients)  echo ssh ;;
        curl)                            echo curl ;;
        python3-gi|python3-gobject)      echo __py_gi ;;
        gir1.2-gtk-3.0|gtk3)             echo __gi_gtk3 ;;
        gir1.2-gtk-4.0|gtk4)             echo __gi_gtk4 ;;
        gir1.2-adw-1|libadwaita)         echo __gi_adw ;;
        gir1.2-ayatanaappindicator3-0.1|libayatana-appindicator-gtk3)
                                         echo __gi_ayatana ;;
        gir1.2-notify-0.7|libnotify)     echo __gi_notify ;;
        libsecret-tools|libsecret)       echo secret-tool ;;
        *)                               echo "" ;;
    esac
}

# True when the package is installed OR its functionality is already there.
dep_present() {
    pkg_installed "$1" && return 0
    local probe
    probe=$(probe_for_pkg "$1")
    case "$probe" in
        "")           return 1 ;;
        __py_gi)      python3 -c "import gi" >/dev/null 2>&1 ;;
        __gi_gtk3)    python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" >/dev/null 2>&1 ;;
        __gi_gtk4)    python3 -c "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk" >/dev/null 2>&1 ;;
        __gi_adw)     python3 -c "import gi; gi.require_version('Adw','1'); from gi.repository import Adw" >/dev/null 2>&1 ;;
        __gi_ayatana) python3 -c "import gi; gi.require_version('AyatanaAppIndicator3','0.1'); from gi.repository import AyatanaAppIndicator3" >/dev/null 2>&1 ;;
        __gi_notify)  python3 -c "import gi; gi.require_version('Notify','0.7'); from gi.repository import Notify" >/dev/null 2>&1 ;;
        *)            command -v "$probe" >/dev/null 2>&1 ;;
    esac
}

ensure_system_deps() {
    if [[ "${VMVPN_SKIP_DEPS:-}" == "1" ]]; then
        echo "Skipping the dependency check (VMVPN_SKIP_DEPS=1)."
        return 0
    fi
    detect_distro
    local machine
    machine=$(uname -m)

    local -a sys_pkgs gui_pkgs install_cmd
    if [[ " $DISTRO_ID $DISTRO_LIKE " == *" fedora "* ]]; then
        if [[ "$machine" == "aarch64" ]]; then
            sys_pkgs=(qemu-system-aarch64 qemu-img edk2-ovmf jq openssh-clients curl)
        else
            sys_pkgs=(qemu-system-x86 qemu-img edk2-ovmf jq openssh-clients curl)
        fi
        gui_pkgs=(python3-gobject gtk3 gtk4 libadwaita \
                  libayatana-appindicator-gtk3 libnotify libsecret)
        install_cmd=(sudo dnf install)
    elif [[ " $DISTRO_ID $DISTRO_LIKE " == *" debian "* \
         || " $DISTRO_ID $DISTRO_LIKE " == *" ubuntu "* ]]; then
        if [[ "$machine" == "aarch64" ]]; then
            sys_pkgs=(qemu-system-arm qemu-utils qemu-efi-aarch64 jq openssh-client curl)
        else
            sys_pkgs=(qemu-system-x86 qemu-utils ovmf jq openssh-client curl)
        fi
        gui_pkgs=(python3-gi gir1.2-gtk-3.0 gir1.2-gtk-4.0 gir1.2-adw-1 \
                  gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7 libsecret-tools)
        install_cmd=(sudo apt install)
    else
        echo ""
        echo "Unknown distribution — make sure these are installed:"
        echo "  QEMU (qemu-system + qemu-img + UEFI firmware), jq, ssh, curl"
        echo "  For the GUI: python3-gi (PyGObject), GTK 3, GTK 4, libadwaita,"
        echo "  Ayatana AppIndicator, libnotify, libsecret-tools, firefox"
        return 0
    fi

    local -a missing=()
    local p
    for p in "${sys_pkgs[@]}" "${gui_pkgs[@]}"; do
        dep_present "$p" || missing+=("$p")
    done

    if [[ ${#missing[@]} -eq 0 ]]; then
        echo "System dependencies: all found."
        return 0
    fi

    local cmd="${install_cmd[*]} ${missing[*]}"
    echo ""
    echo "Missing packages: ${missing[*]}"
    echo "Install command:  $cmd"
    local ans=1
    if ask_yes "Install them now with sudo? [Y/n]"; then
        ans=0
    else
        ans=$?
    fi
    case "$ans" in
        0)
            if ! "${install_cmd[@]}" "${missing[@]}"; then
                echo "Warning: package installation failed; run manually: $cmd" >&2
            fi
            ;;
        2)  echo "No terminal — install them yourself with:"; echo "  $cmd" ;;
        *)  echo "Skipped. To install them later: $cmd" ;;
    esac
}

# -- Lima ---------------------------------------------------------------------

ensure_lima() {
    if command -v limactl >/dev/null 2>&1; then
        local ver major=""
        ver=$(limactl --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
        [[ "$ver" =~ ^[0-9]+ ]] && major="${ver%%.*}"
        echo "Lima found: limactl ${ver:-unknown}"
        if [[ -n "$major" ]] && (( major < 1 )); then
            echo "Warning: Lima $ver is older than 1.x; please upgrade Lima." >&2
        fi
        return 0
    fi

    local machine
    machine=$(uname -m)
    case "$machine" in
        x86_64|aarch64) ;;
        *)
            echo "Warning: no prebuilt Lima release for $machine; install Lima manually." >&2
            echo "  https://lima-vm.io/" >&2
            return 0
            ;;
    esac

    local url="https://github.com/lima-vm/lima/releases/download/v${LIMA_VERSION}/lima-${LIMA_VERSION}-Linux-${machine}.tar.gz"
    echo "Installing Lima $LIMA_VERSION into $HOME/.local (no sudo)…"
    local tmp
    tmp=$(mktemp -d)
    if ! curl -fsSL "$url" -o "$tmp/lima.tar.gz"; then
        echo "Warning: Lima download failed:" >&2
        echo "  $url" >&2
        echo "Install Lima manually (see https://lima-vm.io/), then re-run." >&2
        rm -rf "$tmp"
        return 0
    fi
    if ! tar -xzf "$tmp/lima.tar.gz" -C "$tmp"; then
        echo "Warning: could not extract the Lima archive; install Lima manually." >&2
        rm -rf "$tmp"
        return 0
    fi
    mkdir -p "$HOME/.local/bin" "$HOME/.local/share"
    # The tarball ships bin/ and share/ at its root.
    cp -R "$tmp/bin/"* "$HOME/.local/bin/" 2>/dev/null || true
    cp -R "$tmp/share/"* "$HOME/.local/share/" 2>/dev/null || true
    rm -rf "$tmp"
    export PATH="$HOME/.local/bin:$PATH"
    if command -v limactl >/dev/null 2>&1; then
        echo "Lima installed: $(limactl --version 2>/dev/null | head -1)"
    else
        echo "Warning: limactl is still not on PATH after the install." >&2
    fi
}

# -- system checks (warnings only, never fatal) ---------------------------------

check_system() {
    # KVM access — without it the VM still works but is much slower.
    if [[ -e /dev/kvm && ! ( -r /dev/kvm && -w /dev/kvm ) ]]; then
        echo ""
        echo "Note: /dev/kvm exists but is not accessible to you."
        echo "Fix:  sudo usermod -aG kvm \"\$USER\"   then log out and back in."
        local ans=1
        if ask_yes "Run 'sudo usermod -aG kvm $USER' now? [Y/n]"; then
            ans=0
        else
            ans=$?
        fi
        if [[ $ans -eq 0 ]]; then
            if sudo usermod -aG kvm "$USER"; then
                echo "Done — log out and back in for the group to take effect."
            else
                echo "Warning: usermod failed; run it yourself." >&2
            fi
        fi
    elif [[ ! -e /dev/kvm ]]; then
        echo ""
        echo "Note: /dev/kvm is missing — the VM will run, but slowly."
    fi

    # Free disk space in ~ (VM disk grows to ~3 GiB; images are cached too).
    local avail
    avail=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2{print $4}')
    if [[ "$avail" =~ ^[0-9]+$ ]] && (( avail < 5242880 )); then
        echo ""
        echo "Warning: less than 5 GiB free in $HOME — the VM needs ~3 GiB." >&2
    fi

    # GNOME without an AppIndicator extension: the tray icon can't show.
    if [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]] \
       && command -v gnome-extensions >/dev/null 2>&1 \
       && ! gnome-extensions list --enabled 2>/dev/null \
            | grep -qiE "appindicator|appindicatorsupport"; then
        echo ""
        echo "Note: GNOME has no AppIndicator extension enabled, so the tray"
        echo "      icon will not appear. The VM VPN window works without it."
        echo "      Ubuntu enables it by default; Fedora:"
        echo "      sudo dnf install gnome-shell-extension-appindicator"
    fi
}

# -- files ---------------------------------------------------------------------

ensure_system_deps
ensure_lima

# Create install directory if needed
mkdir -p "$INSTALL_DIR" "$GUI_DIR/icons"

# Fetch/copy files
if [[ -n "$SOURCE_DIR" ]]; then
    echo "Copying from local checkout: $SOURCE_DIR"
    cp "$SOURCE_DIR/vmvpn" "$INSTALL_DIR/vmvpn"
    cp "$SOURCE_DIR/vmvpn.yaml" "$INSTALL_DIR/vmvpn.yaml"
    cp "$SOURCE_DIR/vpn-config.json.example" "$INSTALL_DIR/vpn-config.json.example"
    cp "$SOURCE_DIR/gui/vmvpn_common.py" "$GUI_DIR/vmvpn_common.py"
    cp "$SOURCE_DIR/gui/vmvpn-tray" "$GUI_DIR/vmvpn-tray"
    cp "$SOURCE_DIR/gui/vmvpn-window" "$GUI_DIR/vmvpn-window"
    cp "$SOURCE_DIR"/gui/icons/*.svg "$GUI_DIR/icons/"
else
    echo "Downloading from $REPO_URL..."
    curl -fsSL "$RAW_URL/vmvpn" -o "$INSTALL_DIR/vmvpn"
    curl -fsSL "$RAW_URL/vmvpn.yaml" -o "$INSTALL_DIR/vmvpn.yaml"
    curl -fsSL "$RAW_URL/vpn-config.json.example" -o "$INSTALL_DIR/vpn-config.json.example"
    curl -fsSL "$RAW_URL/gui/vmvpn_common.py" -o "$GUI_DIR/vmvpn_common.py"
    curl -fsSL "$RAW_URL/gui/vmvpn-tray" -o "$GUI_DIR/vmvpn-tray"
    curl -fsSL "$RAW_URL/gui/vmvpn-window" -o "$GUI_DIR/vmvpn-window"
    for icon in vmvpn-connected vmvpn-connecting vmvpn-disconnected vmvpn-off vmvpn-error; do
        curl -fsSL "$RAW_URL/gui/icons/$icon.svg" -o "$GUI_DIR/icons/$icon.svg"
    done
fi

# Make executable
chmod +x "$INSTALL_DIR/vmvpn" "$GUI_DIR/vmvpn-tray" "$GUI_DIR/vmvpn-window"

# Launcher symlinks next to the vmvpn CLI
ln -sfn "vmvpn-gui/vmvpn-tray" "$INSTALL_DIR/vmvpn-tray"
ln -sfn "vmvpn-gui/vmvpn-window" "$INSTALL_DIR/vmvpn-window"

# Quote an Exec value per the Desktop Entry spec: wrap in double quotes,
# backslash-escape " ` $ \
desktop_exec_quote() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//\`/\\\`}"
    value="${value//\$/\\\$}"
    printf '"%s"' "$value"
}

# Desktop entry (app grid / launcher)
mkdir -p "$APPS_DIR"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=VM VPN
Comment=FortiClient VPN in a lightweight VM
Exec=$(desktop_exec_quote "$INSTALL_DIR/vmvpn-window")
Icon=$GUI_DIR/icons/vmvpn-connected.svg
Terminal=false
Categories=Network;
StartupNotify=true
Actions=tray;

[Desktop Action tray]
Name=Start tray icon
Exec=$(desktop_exec_quote "$INSTALL_DIR/vmvpn-tray")
EOF
if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$DESKTOP_FILE" \
        || echo "Warning: generated desktop file did not validate." >&2
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi

check_system

# GUI dependency check - informational only, never fails the install
check_gui_deps() {
    local missing=()
    if ! command -v python3 >/dev/null 2>&1; then
        missing+=("python3 [required]")
    elif ! python3 -c "import gi" 2>/dev/null; then
        missing+=("PyGObject python3-gi [required]")
    else
        # Each probe runs in its own process: Gtk can only be loaded once
        # per process, so 3.0 (tray) and 4.0 (window) need separate probes.
        local probe label req
        while IFS='|' read -r probe label req; do
            if ! python3 -c "import gi; gi.require_version('${probe%%:*}', '${probe##*:}'); from gi.repository import ${probe%%:*}" \
                2>/dev/null; then
                if [[ "$req" == "required" ]]; then
                    missing+=("$label [required]")
                else
                    missing+=("$label [optional]")
                fi
            fi
        done <<'PROBES'
Gtk:3.0|GTK 3 (tray)|required
AyatanaAppIndicator3:0.1|Ayatana AppIndicator (tray)|required
Gtk:4.0|GTK 4 (window)|required
Adw:1|libadwaita (window)|required
Notify:0.7|libnotify (notifications)|optional
PROBES
    fi
    if ! command -v secret-tool >/dev/null 2>&1; then
        missing+=("secret-tool (GNOME keyring) [optional]")
    fi

    if [[ ${#missing[@]} -eq 0 ]]; then
        echo "GUI dependencies: all found."
        return 0
    fi

    echo ""
    echo "GUI dependencies missing: ${missing[*]}"
    echo "The vmvpn CLI works without them. To install the GUI:"
    echo "  Ubuntu/Debian: sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7 libsecret-tools"
    echo "  Fedora:        sudo dnf install python3-gobject gtk3 gtk4 libadwaita libayatana-appindicator-gtk3 libnotify libsecret"
    echo "Note: GNOME needs the AppIndicator extension for the tray (enabled by"
    echo "default on Ubuntu; Fedora: gnome-shell-extension-appindicator)."
    echo "KDE Plasma shows the tray natively."
    return 1
}
GUI_MISSING=0
if ! check_gui_deps; then
    GUI_MISSING=1
fi

if [[ "$MODE" == "install" ]]; then
    # Check if install dir is in PATH
    if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
        echo ""
        echo "NOTE: $INSTALL_DIR is not in your PATH — open a new terminal"
        echo "      (or log out and back in), or use the full path:"
        echo "      $INSTALL_DIR/vmvpn"
    fi
fi

# First run: launch the window so the setup assistant opens automatically.
launched=false
if [[ -z "${VMVPN_NO_LAUNCH:-}" && ! -f "$CONFIG_FILE" ]] \
   && { [[ -n "${WAYLAND_DISPLAY:-}" || -n "${DISPLAY:-}" ]]; } \
   && [[ "$GUI_MISSING" -eq 0 ]]; then
    setsid "$INSTALL_DIR/vmvpn-window" >/dev/null 2>&1 &
    launched=true
fi

if [[ "$MODE" == "install" ]]; then
    echo ""
    echo "Installed to: $INSTALL_DIR/vmvpn"
    echo ""
    if [[ "$launched" == "true" ]]; then
        echo "Open 'VM VPN' from your apps (it opened for you)."
    else
        echo "Open 'VM VPN' from your apps."
    fi
    echo "Or in a terminal: vmvpn setup"
    echo ""
    echo "(Optional) Shell completion:"
    echo "  eval \"\$(vmvpn completion bash)\"  # or zsh"
else
    echo ""
    echo "Updated vm-vpn in: $INSTALL_DIR"
    if [[ -f "$CONFIG_FILE" ]]; then
        echo "  Your vpn-config.json was preserved."
    fi
    if [[ -f "$INSTALL_DIR/.vpn-cert-fingerprint" ]]; then
        echo "  Your saved certificate fingerprint was preserved."
    fi
    echo ""
    echo "GUI: 'VM VPN' in the app grid, 'vmvpn-window' for the window,"
    echo "'vmvpn-tray' for the tray icon."
    echo ""
    echo "If the VM definition (vmvpn.yaml) changed, you may want to recreate the VM:"
    echo "  vmvpn delete && vmvpn vpn-connect"
fi
echo ""
"$INSTALL_DIR/vmvpn" --version
echo "Done!"
