# Mihomo Proxy Pool

This directory is standalone. Copy only `proxy_pool/` to another server or repository, then run:

```bash
chmod +x install.sh
./install.sh
```

It installs Mihomo under `~/.local/bin`, asks once for a subscription URL without placing it in
shell history or source code, removes US nodes by default, and creates a localhost-only HTTP/SOCKS
proxy on `127.0.0.1:7890`.

It refreshes the subscription hourly and restarts Mihomo after a successful refresh. Private files
are kept under `~/.config/mihomo-proxy-pool/` with mode `0600`:

- `subscription_url.txt`: subscription URL;
- `proxy_pool.json`: filters and proxy port;
- `runtime/mihomo.json`: generated node credentials and proxy configuration.

The default filters remove node names matching `US`, `USA`, `United States`, `America`, `美国`, or
the US flag. Edit `proxy_pool.json` to add an exclusion regular expression; saving it refreshes the
pool immediately.

```bash
systemctl --user status mihomo-proxy-pool.service
journalctl --user -u mihomo-proxy-pool.service -f
systemctl --user disable --now mihomo-proxy-pool.service
systemctl --user disable --now mihomo-proxy-pool-refresh.timer
systemctl --user disable --now mihomo-proxy-pool-refresh.path
```

The pool uses Mihomo `load-balance` with `round-robin`: each new proxy connection selects the next
healthy node. HTTP clients that reuse a connection can retain its current exit IP until reconnect.
