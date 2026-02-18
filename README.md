# FreePBX OpenVPN Setup

Complete setup and configuration for OpenVPN server with FreePBX integration, including admin0 extension configuration.

## Overview

This repository provides automated scripts and configuration files to:
- Install and configure OpenVPN server on FreePBX
- Set up secure VPN access for remote SIP clients
- Configure the admin0 extension (extension 1000) in FreePBX
- Manage SSL certificates for VPN clients
- Configure firewall rules for secure access

## Features

- **OpenVPN Server Configuration**: Pre-configured OpenVPN server with secure defaults
- **FreePBX Extension**: Ready-to-use admin0 extension configuration (extension 1000)
- **Automated Setup**: One-command installation script
- **Client Management**: Easy client certificate generation
- **Firewall Configuration**: Automated firewall rules for OpenVPN and FreePBX
- **Security**: SSL/TLS encryption, certificate-based authentication

## Prerequisites

- Ubuntu/Debian-based system with FreePBX installed
- Root or sudo access
- Public IP address or domain name
- Ports 1194/UDP (OpenVPN), 5060/UDP (SIP), 10000-20000/UDP (RTP) available

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/Nexusminds38/freepbx-openvpn-setup.git
cd freepbx-openvpn-setup
```

### 2. Run the Setup Script

```bash
sudo ./setup.sh
```

This script will:
- Install OpenVPN and Easy-RSA
- Generate SSL certificates (CA, server certificate, DH parameters)
- Configure OpenVPN server
- Set up IP forwarding and NAT
- Configure firewall rules
- Add admin0 extension to FreePBX
- Start OpenVPN service

### 3. Configure Firewall (Optional)

If you need to reconfigure firewall rules:

```bash
sudo ./configure-firewall.sh
```

### 4. Generate Client Configuration

Create a VPN client configuration for a user:

```bash
sudo ./generate-client.sh client-name
```

This creates a `.ovpn` file in `/etc/openvpn/clients/client-name/` that can be imported into any OpenVPN client.

## Configuration Files

### OpenVPN Server Configuration

**File**: `openvpn-server.conf`

Key settings:
- **Port**: 1194 (UDP)
- **VPN Subnet**: 10.8.0.0/24
- **Encryption**: SSL/TLS with certificate-based authentication
- **Compression**: LZO compression enabled
- **Max Clients**: 100 concurrent connections

### FreePBX admin0 Extension

**File**: `freepbx-extension-admin0.conf`

Extension details:
- **Extension Number**: 1000
- **Caller ID**: "Admin" <1000>
- **Type**: Friend (can make and receive calls)
- **Codecs**: ulaw, alaw, gsm, g729
- **Security**: Password-protected, SRTP media encryption
- **Features**: Voicemail (mailbox 1000), BLF/presence support

**Default Password**: `admin0Password123!` (Change this after setup!)

## Network Configuration

### VPN Network

- **VPN Subnet**: 10.8.0.0/24
- **Server IP**: 10.8.0.1
- **Client IP Range**: 10.8.0.2 - 10.8.0.254

### Firewall Rules

The following ports are configured:
- **1194/UDP**: OpenVPN
- **5060/UDP**: SIP (unencrypted)
- **5061/TCP**: SIP (TLS encrypted)
- **10000-20000/UDP**: RTP (media/audio)
- **80/TCP**: HTTP (web interface)
- **443/TCP**: HTTPS (secure web interface)
- **22/TCP**: SSH (administrative access)

## Client Setup

### Using Generated Configuration

1. Generate client configuration:
   ```bash
   sudo ./generate-client.sh john-doe
   ```

2. Download the configuration file:
   ```
   /etc/openvpn/clients/john-doe/john-doe.ovpn
   ```

3. Import into OpenVPN client:
   - **Windows**: OpenVPN GUI
   - **macOS**: Tunnelblick or OpenVPN Connect
   - **Linux**: OpenVPN or NetworkManager
   - **Android/iOS**: OpenVPN Connect app

### Manual Configuration

Use `client-template.ovpn` as a template and replace:
- `YOUR_SERVER_IP` with your FreePBX server's public IP
- Certificate placeholders with actual certificates from `/etc/openvpn/keys/`

## Admin0 Extension Usage

### SIP Client Configuration

Configure your SIP client with:
- **Server**: Your FreePBX IP (over VPN: 10.8.0.1)
- **Username**: 1000
- **Password**: admin0Password123!
- **Extension**: 1000
- **Transport**: UDP/TCP
- **Encryption**: SRTP (recommended)

### Changing the Extension Password

1. Edit `/etc/asterisk/pjsip.conf`
2. Find the `[admin0]` section
3. Update the `secret=` line
4. Reload Asterisk: `asterisk -rx "pjsip reload"`

## Security Recommendations

1. **Change Default Password**: Update the admin0 extension password immediately
2. **Use Strong Passwords**: Use complex passwords for all extensions
3. **Enable SRTP**: Use encrypted media for sensitive calls
4. **Regular Updates**: Keep OpenVPN and FreePBX updated
5. **Monitor Logs**: Regularly check `/var/log/openvpn/` for suspicious activity
6. **Client Revocation**: Revoke certificates for users who no longer need access
7. **Firewall**: Keep firewall enabled and restrict access to necessary ports only

## Troubleshooting

### OpenVPN Not Starting

Check the service status:
```bash
systemctl status openvpn@server
```

View logs:
```bash
tail -f /var/log/openvpn/openvpn-status.log
```

### Client Cannot Connect

1. Verify server IP is correct in client config
2. Check firewall allows port 1194/UDP
3. Ensure certificates are valid
4. Check server logs for error messages

### SIP Extension Not Registering

1. Verify extension is in `/etc/asterisk/pjsip.conf`
2. Reload Asterisk: `asterisk -rx "pjsip reload"`
3. Check Asterisk logs: `asterisk -rvvv`
4. Verify VPN connection is active
5. Test connectivity: `ping 10.8.0.1` (from client over VPN)

### IP Forwarding Issues

Enable IP forwarding:
```bash
echo "net.ipv4.ip_forward=1" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

## File Structure

```
freepbx-openvpn-setup/
├── README.md                      # This file
├── openvpn-server.conf           # OpenVPN server configuration
├── freepbx-extension-admin0.conf # FreePBX admin0 extension config
├── setup.sh                      # Main setup script
├── generate-client.sh            # Client certificate generator
├── configure-firewall.sh         # Firewall configuration script
└── client-template.ovpn          # Client configuration template
```

## Advanced Configuration

### Adding More Extensions

1. Copy `freepbx-extension-admin0.conf`
2. Modify extension number and credentials
3. Append to `/etc/asterisk/pjsip.conf`
4. Reload: `asterisk -rx "pjsip reload"`

### Custom VPN Subnet

Edit `openvpn-server.conf`:
```conf
server 10.9.0.0 255.255.255.0
```

Update NAT rules in `setup.sh` accordingly.

### Multiple VPN Servers

Create additional OpenVPN instances:
```bash
cp /etc/openvpn/server.conf /etc/openvpn/server2.conf
# Edit server2.conf (change port, subnet)
systemctl start openvpn@server2
```

## Support

For issues and questions:
- Check the [Troubleshooting](#troubleshooting) section
- Review OpenVPN logs: `/var/log/openvpn/`
- Review Asterisk logs: `/var/log/asterisk/`
- Open an issue on GitHub

## License

This project is provided as-is for educational and production use.

## Contributing

Contributions are welcome! Please submit pull requests or open issues for bugs and feature requests.

## Credits

Created for FreePBX OpenVPN integration with admin0 extension configuration.