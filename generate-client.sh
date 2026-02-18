#!/bin/bash
# Generate OpenVPN Client Configuration Script
# This script generates client certificates and configuration files

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

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    print_error "Please run this script as root or with sudo"
    exit 1
fi

# Check if client name is provided
if [ -z "$1" ]; then
    print_error "Usage: $0 <client-name>"
    exit 1
fi

CLIENT_NAME=$1

# Navigate to Easy-RSA directory
cd /etc/openvpn/easy-rsa

# Source variables
source ./vars

# Generate client certificate and key
print_info "Generating certificate and key for client: $CLIENT_NAME"
./build-key --batch $CLIENT_NAME

# Create client configuration directory
CLIENT_DIR="/etc/openvpn/clients/$CLIENT_NAME"
mkdir -p $CLIENT_DIR

# Get server public IP
SERVER_IP=$(curl -s ifconfig.me || echo "YOUR_SERVER_IP")

# Create client configuration file
print_info "Creating client configuration file..."
cat > $CLIENT_DIR/$CLIENT_NAME.ovpn <<EOF
# OpenVPN Client Configuration
client
dev tun
proto udp

# Server address and port
remote $SERVER_IP 1194

# Client will try to resolve the server address infinitely
resolv-retry infinite

# Don't bind to local port and address
nobind

# Downgrade privileges after initialization
user nobody
group nogroup

# Persist certain state across restarts
persist-key
persist-tun

# Wireless networks often produce a lot of duplicate packets
mute-replay-warnings

# SSL/TLS parameters
remote-cert-tls server

# Enable compression
comp-lzo

# Set log file verbosity
verb 3

# Silence repeating messages
mute 20

# Embedded certificates and keys
<ca>
$(cat /etc/openvpn/keys/ca.crt)
</ca>

<cert>
$(cat /etc/openvpn/easy-rsa/keys/$CLIENT_NAME.crt)
</cert>

<key>
$(cat /etc/openvpn/easy-rsa/keys/$CLIENT_NAME.key)
</key>
EOF

# Set proper permissions
chmod 600 $CLIENT_DIR/$CLIENT_NAME.ovpn

print_info "Client configuration generated successfully!"
print_info "Configuration file: $CLIENT_DIR/$CLIENT_NAME.ovpn"
print_info ""
print_info "You can now distribute this file to the client."
print_info "The client can import this file into their OpenVPN client application."
