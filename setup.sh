#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VM_NAME="ubuntu-vpn"
VM_CONFIG="$SCRIPT_DIR/ubuntu-vpn.yaml"
VPN_CONFIG="${VPN_CONFIG:-$SCRIPT_DIR/vpn-config.json}"

usage() {
    echo "Usage: $0 {start|stop|restart|shell|ssh|status|delete|vpn-connect|vpn-disconnect|vpn-status}"
    echo ""
    echo "Commands:"
    echo "  start          Create and start the VM"
    echo "  stop           Stop the VM"
    echo "  restart        Restart the VM"
    echo "  shell          Open a shell in the VM"
    echo "  ssh            Connect via SSH"
    echo "  status         Show VM status"
    echo "  delete         Delete the VM completely"
    echo "  vpn-connect    Connect to VPN using config from vpn-config.json"
    echo "  vpn-disconnect Disconnect from VPN"
    echo "  vpn-status     Show VPN connection status"
    echo ""
    echo "Environment variables:"
    echo "  VPN_CONFIG     Path to VPN config JSON file (default: ./vpn-config.json)"
    exit 1
}

check_lima() {
    if ! command -v limactl &> /dev/null; then
        echo "Error: Lima is not installed."
        echo ""
        echo "Install Lima:"
        echo "  Linux:  curl -fsSL https://lima-vm.io/install.sh | bash"
        echo "  macOS:  brew install lima"
        echo ""
        echo "See: https://lima-vm.io/"
        exit 1
    fi
}

check_jq() {
    if ! command -v jq &> /dev/null; then
        echo "Error: jq is not installed (required to parse VPN config)."
        echo ""
        echo "Install jq:"
        echo "  Ubuntu/Debian: sudo apt install jq"
        echo "  macOS:         brew install jq"
        exit 1
    fi
}

start() {
    check_lima
    if limactl list -q | grep -q "^${VM_NAME}$"; then
        echo "Starting existing VM..."
        limactl start "$VM_NAME"
    else
        echo "Creating and starting VM..."
        limactl start --name="$VM_NAME" "$VM_CONFIG"
    fi
    echo ""
    echo "VM is ready. Use '$0 shell' to access it."
}

stop() {
    check_lima
    echo "Stopping VM..."
    limactl stop "$VM_NAME" 2>/dev/null || echo "VM is not running."
}

restart() {
    stop
    start
}

shell() {
    check_lima
    limactl shell "$VM_NAME"
}

ssh_connect() {
    check_lima
    ssh -F ~/.lima/"$VM_NAME"/ssh.config lima-"$VM_NAME"
}

status() {
    check_lima
    echo "VM status:"
    limactl list | grep -E "^NAME|^${VM_NAME}"
    echo ""
    if limactl list -q | grep -q "^${VM_NAME}$"; then
        echo "VM info:"
        limactl info "$VM_NAME" 2>/dev/null || true
    fi
}

delete() {
    check_lima
    echo "This will delete the VM and all its data."
    read -p "Are you sure? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        limactl delete -f "$VM_NAME" 2>/dev/null || echo "VM does not exist."
        echo "VM deleted."
    else
        echo "Cancelled."
    fi
}

check_vpn_config() {
    if [[ ! -f "$VPN_CONFIG" ]]; then
        echo "Error: VPN config file not found: $VPN_CONFIG"
        echo ""
        echo "Create a vpn-config.json file with:"
        echo '{'
        echo '  "gateway": "vpn.example.com",'
        echo '  "port": 443,'
        echo '  "username": "your-username",'
        echo '  "password": "your-password"'
        echo '}'
        echo ""
        echo "Or copy the example: cp vpn-config.json.example vpn-config.json"
        exit 1
    fi
}

read_vpn_config() {
    check_jq
    check_vpn_config
    VPN_GATEWAY=$(jq -r '.gateway' "$VPN_CONFIG")
    VPN_PORT=$(jq -r '.port // 443' "$VPN_CONFIG")
    VPN_USERNAME=$(jq -r '.username' "$VPN_CONFIG")
    VPN_PASSWORD=$(jq -r '.password' "$VPN_CONFIG")

    if [[ -z "$VPN_GATEWAY" || "$VPN_GATEWAY" == "null" ]]; then
        echo "Error: 'gateway' is required in $VPN_CONFIG"
        exit 1
    fi
    if [[ -z "$VPN_USERNAME" || "$VPN_USERNAME" == "null" ]]; then
        echo "Error: 'username' is required in $VPN_CONFIG"
        exit 1
    fi
    if [[ -z "$VPN_PASSWORD" || "$VPN_PASSWORD" == "null" ]]; then
        echo "Error: 'password' is required in $VPN_CONFIG"
        exit 1
    fi
}

vpn_connect() {
    check_lima
    read_vpn_config

    echo "Connecting to VPN: $VPN_GATEWAY:$VPN_PORT as $VPN_USERNAME..."

    # Use expect-like approach via stdin to avoid password prompts
    # The password is passed via stdin to fortivpn
    limactl shell "$VM_NAME" -- sudo /opt/forticlient/fortivpn connect vpn-tunnel \
        --server="$VPN_GATEWAY:$VPN_PORT" \
        --user="$VPN_USERNAME" \
        --password-stdin <<< "$VPN_PASSWORD"

    echo ""
    echo "VPN connected. Use proxy at http://127.0.0.1:3128 to access VPN resources."
}

vpn_disconnect() {
    check_lima
    echo "Disconnecting from VPN..."
    limactl shell "$VM_NAME" -- sudo /opt/forticlient/fortivpn disconnect 2>/dev/null || true
    echo "VPN disconnected."
}

vpn_status() {
    check_lima
    echo "VPN status:"
    limactl shell "$VM_NAME" -- sudo /opt/forticlient/fortivpn status 2>/dev/null || echo "VPN is not connected."
}

case "${1:-}" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    shell)
        shell
        ;;
    ssh)
        ssh_connect
        ;;
    status)
        status
        ;;
    delete)
        delete
        ;;
    vpn-connect)
        vpn_connect
        ;;
    vpn-disconnect)
        vpn_disconnect
        ;;
    vpn-status)
        vpn_status
        ;;
    *)
        usage
        ;;
esac
