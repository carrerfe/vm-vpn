#!/bin/bash
set -e

# VM VPN Installer / Updater
# Usage: curl -fsSL https://raw.githubusercontent.com/carrerfe/vm-vpn/main/install.sh | bash

REPO="carrerfe/vm-vpn"
INSTALL_DIR="${VMVPN_INSTALL_DIR:-$HOME/.local/bin}"
REPO_URL="https://github.com/$REPO"

# Detect install vs update
if [[ -f "$INSTALL_DIR/vmvpn" ]]; then
    MODE="update"
    echo "Updating vm-vpn..."
else
    MODE="install"
    echo "Installing vm-vpn..."
fi

# Create install directory if needed
mkdir -p "$INSTALL_DIR"

# Download files
echo "Downloading from $REPO_URL..."
curl -fsSL "https://raw.githubusercontent.com/$REPO/main/vmvpn" -o "$INSTALL_DIR/vmvpn"
curl -fsSL "https://raw.githubusercontent.com/$REPO/main/vmvpn.yaml" -o "$INSTALL_DIR/vmvpn.yaml"
curl -fsSL "https://raw.githubusercontent.com/$REPO/main/vpn-config.json.example" -o "$INSTALL_DIR/vpn-config.json.example"

# Make executable
chmod +x "$INSTALL_DIR/vmvpn"

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
    echo "  3. (Optional) Enable shell completion:"
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
    echo "If the VM definition (vmvpn.yaml) changed, you may want to recreate the VM:"
    echo "  vmvpn delete && vmvpn vpn-connect"
fi
echo ""
echo "Done!"
