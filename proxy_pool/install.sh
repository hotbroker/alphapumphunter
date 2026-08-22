#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./install.sh [--force-mihomo]

Installs an isolated local Mihomo proxy pool for the current user.
EOF
}

force_mihomo=false
case "${1:-}" in
    "") ;;
    --force-mihomo) force_mihomo=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac

if [[ "$(id -u)" == "0" ]]; then
    echo "Run this installer as the deployment user, not with sudo." >&2
    exit 1
fi
for required in curl gunzip install systemctl python3; do
    command -v "$required" >/dev/null 2>&1 || {
        echo "Missing required command: $required" >&2
        exit 1
    }
done

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
case "$script_dir" in
    *[[:space:]]*|*'"'*)
        echo "The package path must not contain whitespace or quotes: $script_dir" >&2
        exit 1
        ;;
esac

python_bin="$(command -v python3)"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/mihomo-proxy-pool"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
subscription_file="$config_dir/subscription_url.txt"
pool_config="$config_dir/proxy_pool.json"
runtime_config="$config_dir/runtime/mihomo.json"
mihomo_bin="$HOME/.local/bin/mihomo"

if [[ "$force_mihomo" == "true" || ! -x "$mihomo_bin" ]]; then
    case "$(uname -m)" in
        x86_64|amd64) mihomo_arch="amd64" ;;
        aarch64|arm64) mihomo_arch="arm64" ;;
        *) echo "Unsupported CPU architecture: $(uname -m)" >&2; exit 1 ;;
    esac
    echo "Downloading the latest official Mihomo release for $mihomo_arch..."
    release_url="$(curl -fsSL https://api.github.com/repos/MetaCubeX/mihomo/releases/latest \
        | sed -n "s/.*\"browser_download_url\": \"\(.*mihomo-linux-${mihomo_arch}-compatible-v[^\"]*\\.gz\)\".*/\1/p" \
        | head -n 1)"
    [[ -n "$release_url" ]] || { echo "Could not find a compatible Mihomo release asset." >&2; exit 1; }
    download_dir="$(mktemp -d)"
    trap 'rm -rf -- "$download_dir"' EXIT
    curl -fL "$release_url" -o "$download_dir/mihomo.gz"
    gunzip -c "$download_dir/mihomo.gz" > "$download_dir/mihomo"
    install -d -m 755 "$HOME/.local/bin"
    install -m 755 "$download_dir/mihomo" "$mihomo_bin"
fi
"$mihomo_bin" -v

install -d -m 700 "$config_dir"
install -d -m 755 "$unit_dir"
if [[ ! -s "$subscription_file" ]]; then
    [[ -t 0 ]] || { echo "Missing $subscription_file; create it with your subscription URL, then rerun." >&2; exit 1; }
    read -r -s -p "Paste the proxy subscription URL: " subscription_url
    printf '\n'
    [[ -n "$subscription_url" ]] || { echo "Subscription URL cannot be empty." >&2; exit 1; }
    printf '%s\n' "$subscription_url" > "$subscription_file"
fi
chmod 600 "$subscription_file"
if [[ ! -e "$pool_config" ]]; then
    install -m 600 "$script_dir/proxy_pool.example.json" "$pool_config"
fi
chmod 600 "$pool_config"

"$python_bin" "$script_dir/proxy_pool.py" --config "$pool_config"

systemctl_bin="$(command -v systemctl)"
cat > "$unit_dir/mihomo-proxy-pool.service" <<EOF
[Unit]
Description=Local Mihomo proxy pool
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$mihomo_bin -f $runtime_config
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
EOF
cat > "$unit_dir/mihomo-proxy-pool-refresh.service" <<EOF
[Unit]
Description=Refresh Mihomo proxy-pool subscription
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$python_bin $script_dir/proxy_pool.py --config $pool_config
ExecStartPost=$systemctl_bin --user try-restart mihomo-proxy-pool.service
EOF
cat > "$unit_dir/mihomo-proxy-pool-refresh.timer" <<EOF
[Unit]
Description=Refresh Mihomo proxy pool every hour

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
Persistent=true
Unit=mihomo-proxy-pool-refresh.service

[Install]
WantedBy=timers.target
EOF
cat > "$unit_dir/mihomo-proxy-pool-refresh.path" <<EOF
[Unit]
Description=Refresh Mihomo proxy pool after private config changes

[Path]
PathChanged=$subscription_file
PathChanged=$pool_config
Unit=mihomo-proxy-pool-refresh.service

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now mihomo-proxy-pool-refresh.timer
systemctl --user enable --now mihomo-proxy-pool-refresh.path
systemctl --user enable --now mihomo-proxy-pool.service

cat <<EOF

Installed. Test the local proxy with:
  curl --noproxy '' -sS --max-time 15 -x http://127.0.0.1:7890 https://api.ipify.org

Status:
  systemctl --user status mihomo-proxy-pool.service
  systemctl --user status mihomo-proxy-pool-refresh.timer
EOF
