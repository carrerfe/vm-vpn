#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VM_NAME="ubuntu-vpn"
VM_CONFIG="$SCRIPT_DIR/ubuntu-vpn.yaml"
VPN_CONFIG="${VPN_CONFIG:-$SCRIPT_DIR/vpn-config.json}"
CERT_FINGERPRINT_FILE="$SCRIPT_DIR/.vpn-cert-fingerprint"
SOCKS_PORT="${SOCKS_PORT:-1080}"

usage() {
    echo "Usage: $0 {start|stop|restart|shell|ssh|status|delete|vpn-connect|vpn-disconnect|vpn-status|proxy}"
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
    echo "  proxy          Start SOCKS5 proxy on localhost:1080 (for browsers)"
    echo "  proxy-stop     Stop SOCKS5 proxy"
    echo ""
    echo "Environment variables:"
    echo "  VPN_CONFIG     Path to VPN config JSON file (default: ./vpn-config.json)"
    echo "  SOCKS_PORT     SOCKS5 proxy port (default: 1080)"
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
    VPN_PASSWORD=$(jq -r '.password // empty' "$VPN_CONFIG")

    if [[ -z "$VPN_GATEWAY" || "$VPN_GATEWAY" == "null" ]]; then
        echo "Error: 'gateway' is required in $VPN_CONFIG"
        exit 1
    fi
    if [[ -z "$VPN_USERNAME" || "$VPN_USERNAME" == "null" ]]; then
        echo "Error: 'username' is required in $VPN_CONFIG"
        exit 1
    fi
    # If password is not in config, prompt for it interactively
    if [[ -z "$VPN_PASSWORD" ]]; then
        read -s -p "VPN Password for $VPN_USERNAME: " VPN_PASSWORD
        echo ""
        if [[ -z "$VPN_PASSWORD" ]]; then
            echo "Error: Password cannot be empty"
            exit 1
        fi
    fi
}

check_certificate_fingerprint() {
    local new_fingerprint="$1"
    local saved_fingerprint=""

    if [[ -f "$CERT_FINGERPRINT_FILE" ]]; then
        saved_fingerprint=$(cat "$CERT_FINGERPRINT_FILE")
    fi

    if [[ -z "$saved_fingerprint" ]]; then
        echo ""
        echo "New VPN server certificate detected:"
        echo "  Fingerprint (SHA1): $new_fingerprint"
        echo ""
        read -p "Trust this certificate and save fingerprint? (y/n) [n]: " response
        if [[ "$response" =~ ^[Yy]$ ]]; then
            echo "$new_fingerprint" > "$CERT_FINGERPRINT_FILE"
            echo "Certificate fingerprint saved."
            return 0
        else
            echo "Certificate rejected. Aborting connection."
            return 1
        fi
    elif [[ "$saved_fingerprint" != "$new_fingerprint" ]]; then
        echo ""
        echo "WARNING: VPN server certificate has CHANGED!"
        echo "  Saved fingerprint:  $saved_fingerprint"
        echo "  New fingerprint:    $new_fingerprint"
        echo ""
        echo "This could indicate a man-in-the-middle attack or a legitimate certificate update."
        read -p "Accept the new certificate? (y/n) [n]: " response
        if [[ "$response" =~ ^[Yy]$ ]]; then
            echo "$new_fingerprint" > "$CERT_FINGERPRINT_FILE"
            echo "New certificate fingerprint saved."
            return 0
        else
            echo "Certificate rejected. Aborting connection."
            return 1
        fi
    else
        echo "Certificate fingerprint verified."
        return 0
    fi
}

vpn_connect() {
    check_lima
    read_vpn_config

    echo "Connecting to VPN: $VPN_GATEWAY:$VPN_PORT as $VPN_USERNAME..."

    # Create/update VPN profile using expect (VPN-only version doesn't have import command)
    local edit_script="
set timeout 30
spawn /opt/forticlient/forticlient-cli vpn edit vpn-tunnel
expect {
    \"Remote Gateway:\" {
        send \"${VPN_GATEWAY}:${VPN_PORT}\r\"
        exp_continue
    }
    \"Authentication\" {
        send \"1\r\"
        exp_continue
    }
    \"Certificate Type\" {
        send \"3\r\"
        exp_continue
    }
    eof
}
"
    echo "$edit_script" | limactl shell "$VM_NAME" -- expect -f - >/dev/null 2>&1

    # First attempt to connect and capture certificate info
    local connect_output
    local connect_script="
set timeout 60
log_user 1
spawn /opt/forticlient/forticlient-cli vpn connect vpn-tunnel --user=${VPN_USERNAME} -p
expect {
    \"Password:\" {
        send \"${VPN_PASSWORD}\r\"
        exp_continue
    }
    \"Please input password\" {
        send \"${VPN_PASSWORD}\r\"
        exp_continue
    }
    -re \"Fingerprint \\\\(SHA1\\\\): (\[0-9A-Fa-f:\]+)\" {
        set fingerprint \$expect_out(1,string)
        puts \"CERT_FINGERPRINT::\$fingerprint\"
        exp_continue
    }
    \"Confirm (y/n)\" {
        send \"n\r\"
    }
    eof
}
"
    connect_output=$(echo "$connect_script" | limactl shell "$VM_NAME" -- expect -f - 2>&1)

    # Extract fingerprint from output
    local cert_fingerprint
    cert_fingerprint=$(echo "$connect_output" | grep -oP 'CERT_FINGERPRINT::\K[0-9A-Fa-f:]+' || true)

    if [[ -n "$cert_fingerprint" ]]; then
        # Certificate confirmation was requested - verify fingerprint
        if ! check_certificate_fingerprint "$cert_fingerprint"; then
            return 1
        fi

        # Disconnect the stuck connection from first attempt before reconnecting
        limactl shell "$VM_NAME" -- /opt/forticlient/forticlient-cli vpn disconnect 2>/dev/null || true
        sleep 1

        # Reconnect and accept the certificate
        local reconnect_script="
set timeout 60
spawn /opt/forticlient/forticlient-cli vpn connect vpn-tunnel --user=${VPN_USERNAME} -p
expect {
    \"Password:\" {
        send \"${VPN_PASSWORD}\r\"
        exp_continue
    }
    \"Please input password\" {
        send \"${VPN_PASSWORD}\r\"
        exp_continue
    }
    \"Confirm (y/n)\" {
        send \"y\r\"
        exp_continue
    }
    eof
}
"
        echo "$reconnect_script" | limactl shell "$VM_NAME" -- expect -f -
    else
        # No certificate prompt - just show the output
        echo "$connect_output"
    fi

    echo ""
    echo "VPN connected. Use proxy at http://127.0.0.1:3128 to access VPN resources."
}

vpn_disconnect() {
    check_lima
    echo "Disconnecting from VPN..."
    limactl shell "$VM_NAME" -- /opt/forticlient/forticlient-cli vpn disconnect 2>/dev/null || true
    echo "VPN disconnected."
}

vpn_status() {
    check_lima
    echo "VPN status:"
    limactl shell "$VM_NAME" -- /opt/forticlient/forticlient-cli vpn status 2>/dev/null || echo "VPN is not connected."
}

start_proxy() {
    check_lima
    
    # Check if SOCKS proxy is already running
    if pgrep -f "ssh.*-D.*$SOCKS_PORT.*lima-$VM_NAME" > /dev/null 2>&1; then
        echo "SOCKS5 proxy already running on localhost:$SOCKS_PORT"
        return 0
    fi
    
    local ssh_config="$HOME/.lima/$VM_NAME/ssh.config"
    if [[ ! -f "$ssh_config" ]]; then
        echo "Error: VM is not running. Start it first with: $0 start"
        exit 1
    fi
    
    echo "Starting SOCKS5 proxy on localhost:$SOCKS_PORT..."
    ssh -F "$ssh_config" -D "127.0.0.1:$SOCKS_PORT" -N -f lima-$VM_NAME
    
    echo ""
    echo "SOCKS5 proxy started on localhost:$SOCKS_PORT"
    echo ""
    echo "Configure Firefox:"
    echo "  1. Settings -> Network Settings -> Manual proxy configuration"
    echo "  2. SOCKS Host: 127.0.0.1, Port: $SOCKS_PORT"
    echo "  3. Select 'SOCKS v5'"
    echo "  4. Check 'Proxy DNS when using SOCKS v5'"
    echo ""
    echo "Or use with curl:"
    echo "  curl --socks5-hostname 127.0.0.1:$SOCKS_PORT https://example.com"
}

stop_proxy() {
    echo "Stopping SOCKS5 proxy..."
    pkill -f "ssh.*-D.*$SOCKS_PORT.*lima-$VM_NAME" 2>/dev/null || true
    echo "SOCKS5 proxy stopped."
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
    proxy)
        start_proxy
        ;;
    proxy-stop)
        stop_proxy
        ;;
    *)
        usage
        ;;
esac
