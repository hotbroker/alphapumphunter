#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./install_kol.sh [--start]

Without --start, install dependencies, example configs, systemd user units,
and automatic config watchers. Use --start after filling the real configs.
EOF
}

start_services=false
case "${1:-}" in
    "") ;;
    --start) start_services=true ;;
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
publisher_config="$config_dir/kol_config.json"
sources_config="$config_dir/kol_sources.json"

if command -v uv >/dev/null 2>&1; then
    uv_bin="$(command -v uv)"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
    uv_bin="$HOME/.local/bin/uv"
else
    if ! command -v curl >/dev/null 2>&1; then
        echo "curl is required to install uv." >&2
        exit 1
    fi
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    uv_bin="$HOME/.local/bin/uv"
fi

echo "Installing Python dependencies..."
(cd "$project_dir" && "$uv_bin" sync --frozen)

install -d -m 700 "$config_dir"
install -d -m 755 "$unit_dir"
if [[ ! -e "$publisher_config" ]]; then
    install -m 600 "$project_dir/kol_config.example.json" "$publisher_config"
    echo "Created $publisher_config"
fi
if [[ ! -e "$sources_config" ]]; then
    install -m 600 "$project_dir/kol_sources.example.json" "$sources_config"
    echo "Created $sources_config"
fi
chmod 600 "$publisher_config" "$sources_config"

systemctl_bin="$(command -v systemctl)"

cat > "$unit_dir/alphapumphunter-kol-publisher.service" <<EOF
[Unit]
Description=AlphaPumpHunter Binance Square KOL Publisher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$project_dir
Environment=PYTHONUNBUFFERED=1
Environment="KOL_CONFIG_PATH=$publisher_config"
ExecStart="$project_dir/.venv/bin/python" "$project_dir/kol_publisher.py" run
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
EOF

cat > "$unit_dir/alphapumphunter-kol-sources.service" <<EOF
[Unit]
Description=AlphaPumpHunter KOL Signal Sources
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$project_dir
Environment=PYTHONUNBUFFERED=1
Environment="KOL_SOURCES_CONFIG=$sources_config"
ExecStart=/usr/bin/env bash "$project_dir/run_kol_sources.sh"
Restart=on-failure
RestartSec=10
KillMode=control-group
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
EOF

cat > "$unit_dir/alphapumphunter-kol-publisher-reload.service" <<EOF
[Unit]
Description=Reload AlphaPumpHunter KOL publisher configuration

[Service]
Type=oneshot
ExecStart=$systemctl_bin --user try-restart alphapumphunter-kol-publisher.service
EOF

cat > "$unit_dir/alphapumphunter-kol-publisher-reload.path" <<EOF
[Unit]
Description=Watch AlphaPumpHunter KOL publisher configuration

[Path]
PathChanged=$publisher_config
Unit=alphapumphunter-kol-publisher-reload.service

[Install]
WantedBy=default.target
EOF

cat > "$unit_dir/alphapumphunter-kol-sources-reload.service" <<EOF
[Unit]
Description=Reload AlphaPumpHunter KOL source configuration

[Service]
Type=oneshot
ExecStart=$systemctl_bin --user try-restart alphapumphunter-kol-sources.service
EOF

cat > "$unit_dir/alphapumphunter-kol-sources-reload.path" <<EOF
[Unit]
Description=Watch AlphaPumpHunter KOL source configuration

[Path]
PathChanged=$sources_config
Unit=alphapumphunter-kol-sources-reload.service

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now \
    alphapumphunter-kol-publisher-reload.path \
    alphapumphunter-kol-sources-reload.path

if [[ "$start_services" == "true" ]]; then
    echo "Validating enabled Binance Square accounts..."
    "$project_dir/.venv/bin/python" "$project_dir/kol_publisher.py" \
        --config "$publisher_config" validate-accounts
    systemctl --user enable --now alphapumphunter-kol-publisher.service

    if (cd "$project_dir" && "$project_dir/.venv/bin/python" - "$sources_config") <<'PY'
import sys
from kol_runtime import configure_source

names = (
    "alpha_surge",
    "top_pump_energy",
    "pullback_consolidation",
    "instant_drop_trigger",
    "instant_opportunity",
)
configured = [configure_source(name, sys.argv[1]) for name in names]
raise SystemExit(0 if all(item.get("selected_proxy") != "direct" for item in configured) else 1)
PY
    then
        systemctl --user enable --now alphapumphunter-kol-sources.service
    else
        echo "Signal sources were not started: configure a proxy for every source first."
    fi
fi

cat <<EOF

Installation complete.

Publisher config: $publisher_config
Signal/proxy config: $sources_config

After editing the configs, start or re-check services with:
  $project_dir/install_kol.sh --start

Once running, saving kol_config.json automatically reloads the publisher;
saving kol_sources.json automatically reloads all signal sources.

For startup after logout/reboot, run once:
  sudo loginctl enable-linger "$USER"
EOF
