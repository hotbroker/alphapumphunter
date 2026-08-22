#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./install_proxy_pool.sh [--force-mihomo]

Installs Mihomo for the current user, creates the private proxy-pool files,
and starts user services that refresh the subscription hourly.
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

project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
case "$project_dir" in
    *[[:space:]]*|*'"'*)
        echo "The project path must not contain whitespace or quotes: $project_dir" >&2
        exit 1
        ;;
esac

config_root="${XDG_CONFIG_HOME:-$HOME/.config}"
config_dir="$config_root/alphapumphunter"
unit_dir="$config_root/systemd/user"
subscription_file="$config_dir/proxy_subscription_url.txt"
pool_config="$config_dir/proxy_pool_demo.json"
runtime_config="$config_dir/proxy_pool_runtime/mihomo.json"
mihomo_bin="$HOME/.local/bin/mihomo"

if command -v uv >/dev/null 2>&1; then
    uv_bin="$(command -v uv)"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
    uv_bin="$HOME/.local/bin/uv"
else
    if ! command -v curl >/dev/null 2>&1; then
        echo "curl is required to install uv and Mihomo." >&2
        exit 1
    fi
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    uv_bin="$HOME/.local/bin/uv"
fi

echo "Installing Python dependencies..."
(cd "$project_dir" && "$uv_bin" sync --frozen)
python_bin="$project_dir/.venv/bin/python"

if [[ "$force_mihomo" == "true" || ! -x "$mihomo_bin" ]]; then
    case "$(uname -m)" in
        x86_64|amd64) mihomo_arch="amd64" ;;
        aarch64|arm64) mihomo_arch="arm64" ;;
        *)
            echo "Unsupported CPU architecture: $(uname -m). Install Mihomo manually." >&2
            exit 1
            ;;
    esac

    echo "Downloading the latest official Mihomo release for $mihomo_arch..."
    release_url="$({
        curl -fsSL https://api.github.com/repos/MetaCubeX/mihomo/releases/latest \
            | sed -n "s/.*\"browser_download_url\": \"\(.*mihomo-linux-${mihomo_arch}-compatible-v[^\"]*\\.gz\)\".*/\1/p" \
            | head -n 1
    })"
    if [[ -z "$release_url" ]]; then
        echo "Could not find a compatible Mihomo release asset." >&2
        exit 1
    fi

    download_dir="$(mktemp -d)"
    cleanup_download() {
        rm -rf -- "$download_dir"
    }
    trap cleanup_download EXIT
    curl -fL "$release_url" -o "$download_dir/mihomo.gz"
    gunzip -c "$download_dir/mihomo.gz" > "$download_dir/mihomo"
    install -d -m 755 "$HOME/.local/bin"
    install -m 755 "$download_dir/mihomo" "$mihomo_bin"
    trap - EXIT
    cleanup_download
fi

"$mihomo_bin" -v

install -d -m 700 "$config_dir"
install -d -m 755 "$unit_dir"
if [[ ! -e "$subscription_file" || ! -s "$subscription_file" ]]; then
    if [[ ! -t 0 ]]; then
        echo "Missing $subscription_file. Create it with your subscription URL, mode 600, then rerun." >&2
        exit 1
    fi
    read -r -s -p "Paste the proxy subscription URL: " subscription_url
    printf '\n'
    if [[ -z "$subscription_url" ]]; then
        echo "Subscription URL cannot be empty." >&2
        exit 1
    fi
    printf '%s\n' "$subscription_url" > "$subscription_file"
fi
chmod 600 "$subscription_file"

if [[ ! -e "$pool_config" ]]; then
    install -m 600 "$project_dir/proxy_pool_demo.example.json" "$pool_config"
    echo "Created $pool_config"
fi
chmod 600 "$pool_config"

echo "Generating the filtered Mihomo proxy pool..."
"$python_bin" "$project_dir/proxy_pool_demo.py" --config "$pool_config"

systemctl_bin="$(command -v systemctl)"
cat > "$unit_dir/alphapumphunter-mihomo.service" <<EOF
[Unit]
Description=AlphaPumpHunter local Mihomo proxy pool
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$project_dir
ExecStart=$mihomo_bin -f $runtime_config
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
EOF

cat > "$unit_dir/alphapumphunter-proxy-pool-refresh.service" <<EOF
[Unit]
Description=Refresh AlphaPumpHunter proxy-pool subscription
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$project_dir
ExecStart=$python_bin $project_dir/proxy_pool_demo.py --config $pool_config
ExecStartPost=$systemctl_bin --user try-restart alphapumphunter-mihomo.service
EOF

cat > "$unit_dir/alphapumphunter-proxy-pool-refresh.timer" <<EOF
[Unit]
Description=Refresh AlphaPumpHunter proxy pool every hour

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
Persistent=true
Unit=alphapumphunter-proxy-pool-refresh.service

[Install]
WantedBy=timers.target
EOF

cat > "$unit_dir/alphapumphunter-proxy-pool-refresh.path" <<EOF
[Unit]
Description=Refresh AlphaPumpHunter proxy pool after local configuration changes

[Path]
PathChanged=$subscription_file
PathChanged=$pool_config
Unit=alphapumphunter-proxy-pool-refresh.service

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now alphapumphunter-proxy-pool-refresh.timer
systemctl --user enable --now alphapumphunter-proxy-pool-refresh.path
systemctl --user enable --now alphapumphunter-mihomo.service

cat <<EOF

Proxy pool installation complete.

Mihomo status:
  systemctl --user status alphapumphunter-mihomo.service

Refresh status:
  systemctl --user status alphapumphunter-proxy-pool-refresh.timer

Test the local proxy:
  curl --noproxy '' -sS --max-time 15 -x http://127.0.0.1:7890 https://api.ipify.org

The subscription and generated Mihomo configuration are private files under:
  $config_dir
EOF
