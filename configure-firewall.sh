#!/bin/bash
# FreePBX Firewall Rules Configuration for OpenVPN
# This script configures firewall rules to allow OpenVPN traffic

set -e

# Color codes for output
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color

print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    print_error "Please run this script as root or with sudo"
    exit 1
fi

print_info "Configuring firewall rules for OpenVPN..."

# Install UFW if not installed
if ! command -v ufw &> /dev/null; then
    print_info "Installing UFW firewall..."
    apt-get update -qq
    apt-get install -y ufw
fi

# Allow OpenVPN port
print_info "Allowing OpenVPN port 1194/udp..."
ufw allow 1194/udp comment 'OpenVPN'

# Allow SSH (to prevent lockout)
print_info "Allowing SSH port 22/tcp..."
ufw allow 22/tcp comment 'SSH'

# Allow SIP ports for FreePBX
print_info "Allowing SIP ports for FreePBX..."
ufw allow 5060/udp comment 'SIP UDP'
ufw allow 5060/tcp comment 'SIP TCP'
ufw allow 5061/tcp comment 'SIP TLS'

# Allow RTP ports for FreePBX (media/audio)
print_info "Allowing RTP ports for FreePBX..."
ufw allow 10000:20000/udp comment 'RTP Media'

# Allow HTTP/HTTPS for FreePBX web interface
print_info "Allowing HTTP/HTTPS for FreePBX web interface..."
ufw allow 80/tcp comment 'HTTP'
ufw allow 443/tcp comment 'HTTPS'

# Configure NAT/Masquerading for VPN clients
print_info "Configuring NAT for VPN clients..."

# Determine the main network interface
MAIN_INTERFACE=$(ip route | grep default | awk '{print $5}' | head -n 1)

if [ -z "$MAIN_INTERFACE" ]; then
    print_error "Could not determine main network interface"
    exit 1
fi

print_info "Main interface detected: $MAIN_INTERFACE"

# Add NAT rules for VPN clients to access local network
iptables -t nat -A POSTROUTING -s 10.8.0.0/24 -o $MAIN_INTERFACE -j MASQUERADE
iptables -A FORWARD -i tun0 -o $MAIN_INTERFACE -j ACCEPT
iptables -A FORWARD -i $MAIN_INTERFACE -o tun0 -m state --state RELATED,ESTABLISHED -j ACCEPT

# Install iptables-persistent to save rules
if ! dpkg -l | grep -q iptables-persistent; then
    print_info "Installing iptables-persistent..."
    echo iptables-persistent iptables-persistent/autosave_v4 boolean true | debconf-set-selections
    echo iptables-persistent iptables-persistent/autosave_v6 boolean true | debconf-set-selections
    apt-get install -y iptables-persistent
fi

# Save iptables rules
print_info "Saving iptables rules..."
netfilter-persistent save

# Enable UFW
print_info "Enabling UFW firewall..."
ufw --force enable

# Display status
print_info "Firewall configuration complete!"
print_info ""
ufw status verbose

print_info ""
print_info "Firewall rules configured successfully!"
print_info "OpenVPN is allowed on port 1194/udp"
print_info "FreePBX SIP is allowed on ports 5060-5061"
print_info "FreePBX RTP is allowed on ports 10000-20000/udp"
print_info "Web interface is accessible on ports 80 and 443"
