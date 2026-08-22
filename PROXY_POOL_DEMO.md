# Proxy Pool Demo

`proxy_pool_demo.py` downloads a Base64 VMess subscription, removes nodes whose names match
the configured exclusion patterns, randomly shuffles the remaining node order, and writes a
private Mihomo-compatible configuration. The generated `load-balance` group uses round-robin
selection for each new proxy connection.

The subscription URL and generated node credentials are never stored in Git.

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
