# Proxy Pool Demo

`proxy_pool_demo.py` downloads a Base64 VMess subscription, removes nodes whose names match
the configured exclusion patterns, randomly shuffles the remaining node order, and writes a
private Mihomo-compatible configuration. The generated `load-balance` group uses round-robin
selection for each new proxy connection.

The subscription URL and generated node credentials are never stored in Git.

For server deployment, use the one-command installer. It installs the official Mihomo binary
for the current user, asks for the subscription URL only when the private URL file is absent,
then enables the local proxy, hourly subscription refresh timer, and config-file watcher:

```bash
chmod +x install_proxy_pool.sh
./install_proxy_pool.sh
```

The installer creates these user services:

- `alphapumphunter-mihomo.service`: local `127.0.0.1:7890` proxy;
- `alphapumphunter-proxy-pool-refresh.timer`: refreshes the subscription hourly;
- `alphapumphunter-proxy-pool-refresh.path`: refreshes immediately after the private URL or
  filter configuration changes.

Service inspection and shutdown:

```bash
systemctl --user status alphapumphunter-mihomo.service
journalctl --user -u alphapumphunter-mihomo.service -f
systemctl --user disable --now alphapumphunter-mihomo.service
systemctl --user disable --now alphapumphunter-proxy-pool-refresh.timer
systemctl --user disable --now alphapumphunter-proxy-pool-refresh.path
```

```bash
install -d -m 700 ~/.config/alphapumphunter
printf '%s\n' 'YOUR_SUBSCRIPTION_URL' > ~/.config/alphapumphunter/proxy_subscription_url.txt
chmod 600 ~/.config/alphapumphunter/proxy_subscription_url.txt

cp proxy_pool_demo.example.json ~/.config/alphapumphunter/proxy_pool_demo.json
uv run python proxy_pool_demo.py --config ~/.config/alphapumphunter/proxy_pool_demo.json
```

The output is `~/.config/alphapumphunter/proxy_pool_runtime/mihomo.json`, mode `0600`. Run a
Mihomo binary with that file to expose the local HTTP/SOCKS proxy at `127.0.0.1:7890`:

```bash
mihomo -f ~/.config/alphapumphunter/proxy_pool_runtime/mihomo.json
```

The default exclusion patterns remove node names containing `US`, `USA`, `United States`,
`America`, `美国`, or the US flag. Add a regular expression to `exclude_name_patterns` to
exclude additional locations. Refresh the generated config periodically, then restart Mihomo
after a successful refresh.
