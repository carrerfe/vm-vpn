#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VM_NAME="ubuntu-vpn"
VM_CONFIG="$SCRIPT_DIR/ubuntu-vpn.yaml"
VPN_CONFIG="${VPN_CONFIG:-$SCRIPT_DIR/vpn-config.json}"
CERT_FINGERPRINT_FILE="$SCRIPT_DIR/.vpn-cert-fingerprint"

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
    echo "  vpn-connect    Connect to VPN (auto-starts VM and proxies per config)"
    echo "  vpn-disconnect Disconnect from VPN (stops proxies)"
    echo "  vpn-status     Show VPN and proxy status"
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

read_proxy_config() {
    check_jq
    check_vpn_config
    
    # Proxy settings from config
    SOCKS_PROXY_ENABLED=$(jq -r '.socks_proxy.enabled // true' "$VPN_CONFIG")
    SOCKS_PROXY_PORT=$(jq -r '.socks_proxy.port // 1080' "$VPN_CONFIG")
    SOCKS_PROXY_AUTO_START=$(jq -r '.socks_proxy.auto_start // true' "$VPN_CONFIG")
    SOCKS_PROXY_AUTO_STOP=$(jq -r '.socks_proxy.auto_stop // true' "$VPN_CONFIG")
    HTTP_PROXY_ENABLED=$(jq -r '.http_proxy.enabled // false' "$VPN_CONFIG")
    HTTP_PROXY_PORT=$(jq -r '.http_proxy.port // 3128' "$VPN_CONFIG")
    HTTP_PROXY_AUTO_START=$(jq -r '.http_proxy.auto_start // false' "$VPN_CONFIG")
    HTTP_PROXY_AUTO_STOP=$(jq -r '.http_proxy.auto_stop // false' "$VPN_CONFIG")
}

read_vpn_config() {
    read_proxy_config
    
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

ensure_vm_running() {
    check_lima
    
    # Check if VM exists and is running
    if ! limactl list -q 2>/dev/null | grep -q "^${VM_NAME}$"; then
        echo "VM not found. Creating and starting..."
        limactl start --name="$VM_NAME" "$VM_CONFIG"
    elif ! limactl list 2>/dev/null | grep "^${VM_NAME}" | grep -q "Running"; then
        echo "Starting VM..."
        limactl start "$VM_NAME"
    fi
}

vpn_connect() {
    read_vpn_config
    ensure_vm_running

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

    # Start proxies if configured
    start_proxies

    echo ""
    echo "VPN connected."
    if [[ "$SOCKS_PROXY_ENABLED" == "true" ]]; then
        echo "  SOCKS5 proxy: localhost:$SOCKS_PROXY_PORT"
    fi
    if [[ "$HTTP_PROXY_ENABLED" == "true" ]]; then
        echo "  HTTP proxy:   localhost:$HTTP_PROXY_PORT"
    fi
}

vpn_disconnect() {
    read_proxy_config
    check_lima
    
    # Stop proxies
    echo "Stopping proxies..."
    stop_proxies
    
    echo "Disconnecting from VPN..."
    limactl shell "$VM_NAME" -- /opt/forticlient/forticlient-cli vpn disconnect 2>/dev/null || true
    echo "VPN disconnected."
}

vpn_status() {
    read_proxy_config
    check_lima
    
    echo "VPN status:"
    limactl shell "$VM_NAME" -- /opt/forticlient/forticlient-cli vpn status 2>/dev/null || echo "VPN is not connected."
    
    show_proxy_status
}

start_socks_proxy() {
    local port="$1"
    
    # Check if SOCKS proxy is already running
    if pgrep -f "ssh.*-D.*127.0.0.1:$port.*lima-$VM_NAME" > /dev/null 2>&1; then
        return 0
    fi
    
    local ssh_config="$HOME/.lima/$VM_NAME/ssh.config"
    ssh -F "$ssh_config" -D "127.0.0.1:$port" -N -f lima-$VM_NAME 2>/dev/null
}

stop_socks_proxy() {
    local port="$1"
    pkill -f "ssh.*-D.*127.0.0.1:$port.*lima-$VM_NAME" 2>/dev/null || true
}

start_proxies() {
    if [[ "$SOCKS_PROXY_ENABLED" == "true" && "$SOCKS_PROXY_AUTO_START" == "true" ]]; then
        echo "Starting SOCKS5 proxy on localhost:$SOCKS_PROXY_PORT..."
        start_socks_proxy "$SOCKS_PROXY_PORT"
    fi
    
    # HTTP proxy (Squid) is already running in the VM, just report if enabled and auto_start
    if [[ "$HTTP_PROXY_ENABLED" == "true" && "$HTTP_PROXY_AUTO_START" == "true" ]]; then
        echo "HTTP proxy available on localhost:$HTTP_PROXY_PORT"
    fi
}

stop_proxies() {
    if [[ "$SOCKS_PROXY_ENABLED" == "true" && "$SOCKS_PROXY_AUTO_STOP" == "true" ]]; then
        stop_socks_proxy "$SOCKS_PROXY_PORT"
    fi
}

show_proxy_status() {
    echo ""
    echo "Proxy status:"
    
    if [[ "$SOCKS_PROXY_ENABLED" == "true" ]]; then
        if pgrep -f "ssh.*-D.*127.0.0.1:$SOCKS_PROXY_PORT.*lima-$VM_NAME" > /dev/null 2>&1; then
            echo "  SOCKS5 proxy: running on localhost:$SOCKS_PROXY_PORT"
        else
            echo "  SOCKS5 proxy: not running (configured on port $SOCKS_PROXY_PORT)"
        fi
    else
        echo "  SOCKS5 proxy: disabled"
    fi
    
    if [[ "$HTTP_PROXY_ENABLED" == "true" ]]; then
        echo "  HTTP proxy:   available on localhost:$HTTP_PROXY_PORT"
    else
        echo "  HTTP proxy:   disabled"
    fi
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
