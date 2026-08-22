import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from proxy_pool.proxy_pool import PoolConfig, build_mihomo_config, filter_nodes, parse_vmess_subscription, write_private_json


def vmess_uri(name: str, host: str) -> str:
    payload = {
        "ps": name,
        "add": host,
        "port": "443",
        "id": "00000000-0000-0000-0000-000000000001",
        "aid": "0",
        "net": "ws",
        "host": "edge.example.test",
        "path": "/ws",
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return f"vmess://{encoded}"


class ProxyPoolTests(unittest.TestCase):
    def test_filters_us_nodes_and_builds_pool(self):
        subscription = base64.b64encode(
            "\n".join([vmess_uri("US-01", "us.example.test"), vmess_uri("香港-01", "hk.example.test")]).encode()
        ).decode()
        nodes = parse_vmess_subscription(subscription)
        allowed, excluded = filter_nodes(nodes, (r"(?i)(?:^|[^A-Z])(?:US|USA)(?:$|[^A-Z])",))
        self.assertEqual(excluded, 1)

        config = PoolConfig(Path("url.txt"), Path("mihomo.json"), (), 7890, "https://example.test", 300)
        generated = build_mihomo_config(allowed, config)
        self.assertEqual(generated["bind-address"], "127.0.0.1")
        self.assertEqual(generated["proxy-groups"][0]["strategy"], "round-robin")
        self.assertEqual(generated["proxies"][0]["network"], "ws")

    def test_private_output_permissions(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime" / "mihomo.json"
            write_private_json(path, {"proxies": []})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
