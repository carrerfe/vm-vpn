# VM VPN

A Lima-based Ubuntu 24.04 LTS virtual machine for running FortiClient VPN with proxy servers accessible from the host machine.

## Overview

This project provides:
- **FortiClient VPN**: Runs inside a real VM for full isolation
- **SOCKS5 Proxy**: SSH-based proxy for browsers and applications
- **HTTP Proxy**: Squid proxy for HTTP/HTTPS traffic
- **Firefox Integration**: Dedicated browser profile with auto-configured proxy
- **Full VM**: Real Linux kernel, systemd, complete Ubuntu environment

## Installation

```bash
curl -fsSL https://raw.githubusercontent.com/carrerfe/vm-vpn/main/install.sh | bash
```

This installs to `~/.local/bin`. Set `VMVPN_INSTALL_DIR` to customize.

## Requirements

- [Lima](https://lima-vm.io/) - Linux virtual machines on Linux/macOS
- QEMU (installed automatically by Lima on most systems)
- jq (for JSON config parsing)

### Install Dependencies

**Linux:**
```bash
curl -fsSL https://lima-vm.io/install.sh | bash
sudo apt install jq  # Debian/Ubuntu
```

**macOS:**
```bash
brew install lima jq
```

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                         Host Machine                             │
│                                                                  │
│  ┌──────────┐                                                    │
│  │ Firefox  │──┐                                                 │
│  │ (vmvpn)  │  │                                                 │
│  └──────────┘  │                                                 │
│                │                                                 │
│  ┌──────────┐  │  SOCKS5 (localhost:1080)                        │
│  │  curl    │──┼─────────────────────────────┐                   │
│  │  apps    │  │                             │                   │
│  └──────────┘  │  HTTP (localhost:3128)      │                   │
│                └─────────────────────────┐   │                   │
└──────────────────────────────────────────┼───┼───────────────────┘
                                           │   │
┌──────────────────────────────────────────┼───┼───────────────────┐
│              Lima VM (Ubuntu 24.04 LTS)  │   │                   │
│                                          ▼   ▼                   │
│  ┌───────────────────────┐    ┌─────────────────────────────┐    │
│  │     Squid Proxy       │    │   SSH Dynamic Port Forward  │    │
│  │   (HTTP/HTTPS proxy)  │    │      (SOCKS5 proxy)         │    │
│  │     port 3128         │    │       port 1080             │    │
│  └───────────┬───────────┘    └──────────────┬──────────────┘    │
│              │                               │                   │
│              └───────────┬───────────────────┘                   │
│                          ▼                                       │
│                 ┌─────────────────┐                              │
│                 │  FortiClient    │                              │
│                 │     VPN         │────────▶ Corporate Network   │
│                 └─────────────────┘                              │
└──────────────────────────────────────────────────────────────────┘
```

**SOCKS5 Proxy** (recommended): SSH dynamic port forwarding (`ssh -D`). Runs on host, tunnels through VM. Best for browsers.

**HTTP Proxy**: Squid running inside the VM. Useful for apps that only support HTTP proxies or environment variables.

## Quick Start

```bash
# 1. Create your VPN config
cp vpn-config.json.example vpn-config.json
# Edit vpn-config.json with your credentials

# 2. Connect to VPN (auto-starts VM and proxies)
./vmvpn vpn-connect

# 3. Launch Firefox with VPN proxy
./vmvpn firefox

# 4. Check status
./vmvpn vpn-status

# 5. Disconnect when done
./vmvpn vpn-disconnect
```

## Shell Completion

```bash
# Bash - add to ~/.bashrc
eval "$(./vmvpn completion bash)"

# Zsh - add to ~/.zshrc
eval "$(./vmvpn completion zsh)"
```

## Commands

### VM Commands
| Command    | Description                    |
|------------|--------------------------------|
| `start`    | Create and start the VM        |
| `stop`     | Stop the VM                    |
| `restart`  | Restart the VM                 |
| `shell`    | Open a shell in the VM         |
| `ssh`      | Connect via SSH                |
| `status`   | Show VM status                 |
| `delete`   | Delete the VM and all its data |

### VPN Commands
| Command          | Description                              |
|------------------|------------------------------------------|
| `vpn-connect`    | Connect to VPN (auto-starts VM/proxies)  |
| `vpn-disconnect` | Disconnect from VPN (stops proxies)      |
| `vpn-status`     | Show VPN and proxy status                |

### Browser Commands
| Command          | Description                              |
|------------------|------------------------------------------|
| `firefox`        | Launch Firefox with VPN proxy profile    |
| `firefox-profile`| Show Firefox profile info and deletion   |

### Shell Completion
| Command           | Description                    |
|-------------------|--------------------------------|
| `completion bash` | Output bash completion script  |
| `completion zsh`  | Output zsh completion script   |

## Project Structure

```
.
├── vmvpn                   # CLI script
├── vmvpn.yaml              # Lima VM configuration
├── vpn-config.json.example # VPN config template
├── vpn-config.json         # Your VPN credentials (gitignored)
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

### VPN Config File

Create `vpn-config.json` with your VPN and proxy settings:

```json
{
  "gateway": "vpn.example.com",
  "port": 443,
  "username": "your-username",
  "socks_proxy": {
    "enabled": true,
    "port": 1080,
    "auto_start": true,
    "auto_stop": true
  },
  "http_proxy": {
    "enabled": false,
    "port": 3128,
    "auto_start": false,
    "auto_stop": false
  }
}
```

- **password**: Optional. If omitted, you'll be prompted interactively.
- **socks_proxy**: SOCKS5 proxy via SSH (recommended for browsers)
- **http_proxy**: Squid HTTP proxy
- **auto_start/auto_stop**: Control proxy lifecycle with VPN connect/disconnect

> **Note:** `vpn-config.json` is gitignored to protect your credentials.

### Manual Connection (Inside VM)

If you prefer to connect manually inside the VM:

```bash
./vmvpn shell

# Then inside the VM (run as non-root user):
/opt/forticlient/forticlient-cli vpn connect <profile-name> --username=<username>

# List VPN profiles
/opt/forticlient/forticlient-cli vpn list

# Check status
/opt/forticlient/forticlient-cli vpn status
```

## Proxy Servers

Two proxy options are available:

### SOCKS5 Proxy (Recommended)

SSH-based SOCKS5 proxy on port **1080** (default). Best for browsers.

```bash
# Test with curl
curl --socks5-hostname 127.0.0.1:1080 https://internal-site.example.com
```

### HTTP Proxy

Squid HTTP proxy on port **3128**. Enable in config if needed.

```bash
# Test with curl
curl -x http://127.0.0.1:3128 https://internal-site.example.com

# Set environment variables
export http_proxy=http://127.0.0.1:3128
export https_proxy=http://127.0.0.1:3128
```

### Firefox Integration

The easiest way to browse through the VPN:

```bash
# Launch Firefox with pre-configured proxy profile
./vmvpn firefox

# View profile info and deletion instructions
./vmvpn firefox-profile
```

This creates a dedicated `vmvpn` Firefox profile with proxy settings matching your config.

## Customization

Edit `vmvpn.yaml` to customize:

```yaml
cpus: 2          # Number of CPUs
memory: "4GiB"   # RAM allocation
disk: "20GiB"    # Disk size
```

## License

MIT
