#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' "install.sh must run as root" >&2
  exit 1
fi

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)

if ! getent group subnet-tracker >/dev/null; then
  groupadd --system subnet-tracker
fi
if ! getent passwd subnet-tracker >/dev/null; then
  useradd --system --gid subnet-tracker --home-dir /var/lib/subnet-tracker --no-create-home subnet-tracker
fi

mkdir -p /etc/subnet-tracker
chown root:subnet-tracker /etc/subnet-tracker
chmod 0750 /etc/subnet-tracker

mkdir -p /var/lib/subnet-tracker/pki
chown subnet-tracker:subnet-tracker /var/lib/subnet-tracker
chmod 2775 /var/lib/subnet-tracker
chown subnet-tracker:subnet-tracker /var/lib/subnet-tracker/pki
chmod 0700 /var/lib/subnet-tracker/pki

python3.12 -m venv /opt/subnet-tracker
mkdir -p /opt/subnet-tracker/agent
chown root:root /opt/subnet-tracker
chmod 0755 /opt/subnet-tracker
chown root:root /opt/subnet-tracker/agent
chmod 0755 /opt/subnet-tracker/agent
/opt/subnet-tracker/bin/pip install "$ROOT"

mkdir -p /etc/openvpn/server/subnet-tracker
chown subnet-tracker:subnet-tracker /etc/openvpn/server/subnet-tracker
chmod 0755 /etc/openvpn/server/subnet-tracker

mkdir -p /usr/lib/subnet-tracker
chown root:root /usr/lib/subnet-tracker
chmod 0755 /usr/lib/subnet-tracker

join_toml() {
  local out="[" sep="" item
  for item in "$@"; do
    out+="${sep}\"${item}\""
    sep=", "
  done
  printf '%s' "${out}]"
}

# config.toml is changed by hand after the first install.
if [ ! -e /etc/subnet-tracker/config.toml ]; then
  addresses=()
  while IFS= read -r addr; do
    [ -n "$addr" ] && addresses+=("$addr")
  done < <(ip -o addr show scope global | awk '{split($4, a, "/"); print a[1]}' | LC_ALL=C sort -u)
  if [ "${#addresses[@]}" -eq 0 ]; then
    server_addresses="[]"
  else
    server_addresses=$(join_toml "${addresses[@]}")
  fi
  trusted=(127.0.0.0/8 ::1)
  tunnel_networks=(10.8.0.0/24)
  for net in "${tunnel_networks[@]}"; do
    case " ${trusted[*]} " in
      *" ${net} "*) ;;
      *)
        trusted+=("$net")
        ;;
    esac
  done
  trusted_toml=$(join_toml "${trusted[@]}")
  tunnel_toml=$(join_toml "${tunnel_networks[@]}")
  config_tmp=$(mktemp /etc/subnet-tracker/config.toml.XXXXXX)
  cat > "$config_tmp" <<EOF
db = "/var/lib/subnet-tracker/state.db"
pki_dir = "/var/lib/subnet-tracker/pki"

[web]
bind = ["10.8.0.1:8080", "127.0.0.1:8080"]
allowed_hosts = []
trusted_networks = ${trusted_toml}
admin_password_hash = ""
remember_for = "365d"
secret_key_file = "/var/lib/subnet-tracker/secret_key"

[agent_api]
listen = ["10.8.0.1:8443"]
endpoints = ["https://10.8.0.1:8443"]

[routes]
file = "/etc/openvpn/server/subnet-tracker/routes.conf"
server_addresses = ${server_addresses}
tunnel_networks = ${tunnel_toml}

[[openvpn]]
name = "server"
management = "unix:/run/subnet-tracker/ovpn-server.sock"
EOF
  chown root:subnet-tracker "$config_tmp"
  chmod 0640 "$config_tmp"
  mv "$config_tmp" /etc/subnet-tracker/config.toml
fi

install -m 0644 -o root -g root "$ROOT/deploy/systemd/subnet-tracker-tick.service" /etc/systemd/system/subnet-tracker-tick.service
install -m 0644 -o root -g root "$ROOT/deploy/systemd/subnet-tracker-tick.timer" /etc/systemd/system/subnet-tracker-tick.timer
install -m 0644 -o root -g root "$ROOT/deploy/systemd/subnet-tracker-tick.path" /etc/systemd/system/subnet-tracker-tick.path
install -m 0644 -o root -g root "$ROOT/deploy/tmpfiles.d/subnet-tracker.conf" /etc/tmpfiles.d/subnet-tracker.conf
install -m 0644 -o root -g root "$ROOT/deploy/nftables/subnet-tracker.nft" /etc/subnet-tracker/nftables.conf

# lo and tun0 are not external, so they stay out of strict reverse-path filtering.
{
  printf '%s\n' '# rp_filter=1 on external interfaces. lo and tun0 are not external.'
  ip -o link show | awk -F': ' '{print $2}' | while IFS= read -r iface; do
    iface=${iface%%@*}
    case "$iface" in
      lo|tun0) continue ;;
    esac
    [ -n "$iface" ] || continue
    printf 'net.ipv4.conf.%s.rp_filter = 1\n' "${iface//./\/}"
  done
} > /etc/sysctl.d/subnet-tracker.conf
sysctl -p /etc/sysctl.d/subnet-tracker.conf

if [ -f /etc/nftables.conf ] && ! grep -qxF 'include "/etc/subnet-tracker/nftables.conf"' /etc/nftables.conf; then
  printf '\n%s\n' 'include "/etc/subnet-tracker/nftables.conf"' >> /etc/nftables.conf
fi
nft delete table inet subnet-tracker 2>/dev/null || true
nft -f /etc/subnet-tracker/nftables.conf

systemctl daemon-reload
systemd-tmpfiles --create /etc/tmpfiles.d/subnet-tracker.conf
systemctl enable subnet-tracker-tick.timer subnet-tracker-tick.path
systemctl start subnet-tracker-tick.timer subnet-tracker-tick.path
