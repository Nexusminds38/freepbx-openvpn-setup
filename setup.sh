#!/bin/bash
# FreePBX OpenVPN Setup Script
# This script automates the installation and configuration of OpenVPN for FreePBX

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    print_error "Please run this script as root or with sudo"
    exit 1
fi

print_info "Starting FreePBX OpenVPN Setup..."

# Update package list
print_info "Updating package list..."
apt-get update -qq

# Install OpenVPN and Easy-RSA
print_info "Installing OpenVPN and Easy-RSA..."
apt-get install -y openvpn easy-rsa

# Create OpenVPN directory structure
print_info "Creating OpenVPN directory structure..."
mkdir -p /etc/openvpn/keys
mkdir -p /var/log/openvpn

# Copy server configuration
print_info "Copying OpenVPN server configuration..."
cp openvpn-server.conf /etc/openvpn/server.conf

# Setup Easy-RSA for certificate management
print_info "Setting up Easy-RSA..."
make-cadir /etc/openvpn/easy-rsa
cd /etc/openvpn/easy-rsa

# Configure Easy-RSA variables
cat > vars <<EOF
export KEY_COUNTRY="US"
export KEY_PROVINCE="CA"
export KEY_CITY="SanFrancisco"
export KEY_ORG="FreePBX-OpenVPN"
export KEY_EMAIL="admin@freepbx.local"
export KEY_OU="IT"
export KEY_NAME="FreePBXServer"
EOF

# Source the variables
source ./vars

# Clean all existing keys
./clean-all

# Build the Certificate Authority
print_info "Building Certificate Authority..."
./build-ca --batch

# Build server certificate and key
print_info "Building server certificate and key..."
./build-key-server --batch server

# Build Diffie-Hellman parameters
print_info "Building Diffie-Hellman parameters (this may take a while)..."
./build-dh

# Copy keys to OpenVPN directory
print_info "Copying keys to OpenVPN directory..."
cp keys/ca.crt /etc/openvpn/keys/
cp keys/server.crt /etc/openvpn/keys/
cp keys/server.key /etc/openvpn/keys/
cp keys/dh2048.pem /etc/openvpn/keys/

# Set proper permissions
chmod 600 /etc/openvpn/keys/server.key

# Enable IP forwarding
print_info "Enabling IP forwarding..."
echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
sysctl -p

# Configure firewall rules
print_info "Configuring firewall rules..."
# Allow OpenVPN through firewall
ufw allow 1194/udp
ufw allow OpenSSH

# Setup NAT for VPN clients
iptables -t nat -A POSTROUTING -s 10.8.0.0/24 -o eth0 -j MASQUERADE
iptables -A FORWARD -s 10.8.0.0/24 -j ACCEPT
iptables -A FORWARD -d 10.8.0.0/24 -j ACCEPT

# Save iptables rules
iptables-save > /etc/iptables/rules.v4

# Enable and start OpenVPN service
print_info "Enabling and starting OpenVPN service..."
systemctl enable openvpn@server
systemctl start openvpn@server

# Configure FreePBX extension admin0
print_info "Configuring FreePBX extension admin0..."
if [ -d "/etc/asterisk" ]; then
    # Backup existing configuration
    cp /etc/asterisk/pjsip.conf /etc/asterisk/pjsip.conf.backup.$(date +%Y%m%d_%H%M%S) 2>/dev/null || true
    
    # Append admin0 extension to pjsip.conf
    cat freepbx-extension-admin0.conf >> /etc/asterisk/pjsip.conf
    
    # Reload Asterisk configuration
    asterisk -rx "pjsip reload"
    
    print_info "FreePBX extension admin0 configured successfully"
else
    print_warning "Asterisk directory not found. Please manually configure the extension."
fi

# Check OpenVPN status
print_info "Checking OpenVPN status..."
systemctl status openvpn@server --no-pager || true

print_info "FreePBX OpenVPN Setup completed successfully!"
print_info ""
print_info "Next steps:"
print_info "1. Generate client certificates using: ./generate-client.sh <client-name>"
print_info "2. Configure your FreePBX firewall to allow VPN traffic"
print_info "3. Test the VPN connection from a client device"
print_info ""
print_info "OpenVPN is running on UDP port 1194"
print_info "VPN subnet: 10.8.0.0/24"
