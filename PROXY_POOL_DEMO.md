# Standalone Proxy Pool

The standalone implementation is in [`proxy_pool/`](proxy_pool/README.md). That directory has no
AlphaPumpHunter or `uv` dependency and can be copied or packaged on its own.

```bash
cd proxy_pool
chmod +x install.sh
./install.sh
```

The root [`install_proxy_pool.sh`](install_proxy_pool.sh) remains a compatibility wrapper for the
same standalone installer.
