# Ubuntu VPN Lima VM

A Lima-based Ubuntu 24.04 LTS virtual machine for running FortiClient VPN with a proxy server accessible from the host machine.

## Overview

This project provides:
- **FortiClient VPN**: Runs inside a real VM for full isolation
- **Proxy Server**: Running in the VM, allows the host to access web resources through the VPN tunnel
- **Full VM**: Real Linux kernel, systemd, complete Ubuntu environment

## Requirements

- [Lima](https://lima-vm.io/) - Linux virtual machines on Linux/macOS
- QEMU (installed automatically by Lima on most systems)

### Install Lima

**Linux:**
```bash
curl -fsSL https://lima-vm.io/install.sh | bash
```

**macOS:**
```bash
brew install lima
```

## Architecture

```
                         Host Machine
                              │
                              │ HTTP/HTTPS requests
                              ▼
┌─────────────────────────────┼───────────┐
│              Lima VM        │           │
│           (Ubuntu 24.04 LTS)│           │
│                     ┌───────┴────────┐  │
│                     │  Proxy Server  │  │
│                     │  (e.g. Squid)  │  │
│                     └───────┬────────┘  │
│  ┌─────────────┐            │           │
│  │ FortiClient │◀───────────┘           │
│  │    VPN      │────────▶ VPN Network   │
│  └─────────────┘                        │
└─────────────────────────────────────────┘
```

## Quick Start

```bash
# Create and start the VM
./setup.sh start

# Access the VM shell
./setup.sh shell

# Or connect via SSH
./setup.sh ssh

# Check VM status
./setup.sh status

# Stop the VM
./setup.sh stop

# Delete the VM completely
./setup.sh delete
```

## Setup Script Commands

| Command   | Description                    |
|-----------|--------------------------------|
| `start`   | Create and start the VM        |
| `stop`    | Stop the VM                    |
| `restart` | Restart the VM                 |
| `shell`   | Open a shell in the VM         |
| `ssh`     | Connect via SSH                |
| `status`  | Show VM status                 |
| `delete`  | Delete the VM and all its data |

## Project Structure

```
.
├── ubuntu-vpn.yaml   # Lima VM configuration
├── setup.sh          # Management script
└── README.md
```

## VM Specifications

- **OS**: Ubuntu 24.04 LTS (cloud image)
- **CPUs**: 2
- **Memory**: 4 GiB
- **Disk**: 20 GiB
- **FortiClient**: 7.4.x (installed from official Fortinet repo)
- Pre-installed tools: `curl`, `wget`, `vim`, `htop`, `net-tools`, `iproute2`

## FortiClient VPN Usage

Once inside the VM, use the FortiClient CLI:

```bash
# Connect to VPN
sudo /opt/forticlient/fortivpn connect <vpn-name> --server=<server> --user=<username>

# Disconnect
sudo /opt/forticlient/fortivpn disconnect

# Check status
sudo /opt/forticlient/fortivpn status
```

Or use the GUI (requires X11 forwarding or desktop environment):
```bash
/opt/forticlient/forticlient
```

## Proxy Server

The VM runs a Squid proxy on port **3128** that allows the host to access resources through the VPN tunnel.

### Using the Proxy

Once the VPN is connected inside the VM, configure your host applications to use the proxy:

```bash
# Test the proxy
curl -x http://127.0.0.1:3128 http://internal-vpn-resource.example.com

# Set environment variables for CLI tools
export http_proxy=http://127.0.0.1:3128
export https_proxy=http://127.0.0.1:3128

# Or use per-command proxy
curl --proxy http://127.0.0.1:3128 https://internal-site.example.com
```

### Browser Configuration

Configure your browser to use proxy `127.0.0.1:3128` for HTTP/HTTPS traffic to access VPN resources.

## Customization

Edit `ubuntu-vpn.yaml` to customize:

```yaml
cpus: 2          # Number of CPUs
memory: "4GiB"   # RAM allocation
disk: "20GiB"    # Disk size
```

## License

MIT
