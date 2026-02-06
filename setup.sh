#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VM_NAME="ubuntu-vpn"
VM_CONFIG="$SCRIPT_DIR/ubuntu-vpn.yaml"

usage() {
    echo "Usage: $0 {start|stop|restart|shell|ssh|status|delete}"
    echo ""
    echo "Commands:"
    echo "  start    Create and start the VM"
    echo "  stop     Stop the VM"
    echo "  restart  Restart the VM"
    echo "  shell    Open a shell in the VM"
    echo "  ssh      Connect via SSH"
    echo "  status   Show VM status"
    echo "  delete   Delete the VM completely"
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
    *)
        usage
        ;;
esac
