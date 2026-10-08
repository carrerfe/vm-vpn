#!/bin/bash
set -e

# VM VPN Installer / Updater
# Usage: curl -fsSL https://raw.githubusercontent.com/carrerfe/vm-vpn/main/install.sh | bash
# Or from a local checkout: VMVPN_SOURCE_DIR=/path/to/checkout ./install.sh

REPO="carrerfe/vm-vpn"
INSTALL_DIR="${VMVPN_INSTALL_DIR:-$HOME/.local/bin}"
SOURCE_DIR="${VMVPN_SOURCE_DIR:-}"
REPO_URL="https://github.com/$REPO"
RAW_URL="https://raw.githubusercontent.com/$REPO/main"
GUI_DIR="$INSTALL_DIR/vmvpn-gui"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$APPS_DIR/io.github.carrerfe.VmVpn.desktop"

# Detect install vs update
if [[ -f "$INSTALL_DIR/vmvpn" ]]; then
    MODE="update"
    echo "Updating vm-vpn..."
else
    MODE="install"
    echo "Installing vm-vpn..."
fi

# Create install directory if needed
mkdir -p "$INSTALL_DIR" "$GUI_DIR"

# Fetch/copy files
if [[ -n "$SOURCE_DIR" ]]; then
    echo "Copying from local checkout: $SOURCE_DIR"
    cp "$SOURCE_DIR/vmvpn" "$INSTALL_DIR/vmvpn"
    cp "$SOURCE_DIR/vmvpn.yaml" "$INSTALL_DIR/vmvpn.yaml"
    cp "$SOURCE_DIR/vpn-config.json.example" "$INSTALL_DIR/vpn-config.json.example"
    cp "$SOURCE_DIR/gui/vmvpn_common.py" "$GUI_DIR/vmvpn_common.py"
    cp "$SOURCE_DIR/gui/vmvpn-tray" "$GUI_DIR/vmvpn-tray"
    cp "$SOURCE_DIR/gui/vmvpn-window" "$GUI_DIR/vmvpn-window"
else
    echo "Downloading from $REPO_URL..."
    curl -fsSL "$RAW_URL/vmvpn" -o "$INSTALL_DIR/vmvpn"
    curl -fsSL "$RAW_URL/vmvpn.yaml" -o "$INSTALL_DIR/vmvpn.yaml"
    curl -fsSL "$RAW_URL/vpn-config.json.example" -o "$INSTALL_DIR/vpn-config.json.example"
    curl -fsSL "$RAW_URL/gui/vmvpn_common.py" -o "$GUI_DIR/vmvpn_common.py"
    curl -fsSL "$RAW_URL/gui/vmvpn-tray" -o "$GUI_DIR/vmvpn-tray"
    curl -fsSL "$RAW_URL/gui/vmvpn-window" -o "$GUI_DIR/vmvpn-window"
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
Icon=network-vpn
Terminal=false
Categories=Network;
StartupNotify=true
Actions=tray;

[Desktop Action tray]
Name=Start tray icon
Exec=$(desktop_exec_quote "$INSTALL_DIR/vmvpn-tray")
EOF
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi

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
}
check_gui_deps

if [[ "$MODE" == "install" ]]; then
    # Check if install dir is in PATH
    if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
        echo ""
        echo "NOTE: $INSTALL_DIR is not in your PATH."
        echo "Add it to your shell profile:"
        echo ""
        echo "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
        echo "  # or for zsh:"
        echo "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
        echo ""
    fi

    echo ""
    echo "Installed to: $INSTALL_DIR/vmvpn"
    echo ""
    echo "Next steps:"
    echo "  1. Copy and edit the config:"
    echo "     cp $INSTALL_DIR/vpn-config.json.example $INSTALL_DIR/vpn-config.json"
    echo ""
    echo "  2. Connect to VPN:"
    echo "     vmvpn vpn-connect"
    echo ""
    echo "  3. (Optional) GUI: open 'VM VPN' from the app grid, or run"
    echo "     'vmvpn-window'. For the tray icon run 'vmvpn-tray' (or enable"
    echo "     'Start tray icon at login' in the window's Settings page)."
    echo ""
    echo "  4. (Optional) Enable shell completion:"
    echo "     eval \"\$(vmvpn completion bash)\"  # or zsh"
else
    echo ""
    echo "Updated vm-vpn in: $INSTALL_DIR"
    if [[ -f "$INSTALL_DIR/vpn-config.json" ]]; then
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
