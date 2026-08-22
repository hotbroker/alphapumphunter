import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from proxy_pool_demo import (
    PoolConfig,
    build_mihomo_config,
    filter_nodes,
    parse_vmess_subscription,
    write_private_json,
)


def vmess_uri(name: str, host: str, network: str = "ws") -> str:
    payload = {
        "v": "2",
        "ps": name,
        "add": host,
        "port": "443",
        "id": "00000000-0000-0000-0000-000000000001",
        "aid": "0",
        "scy": "auto",
        "net": network,
        "type": "none",
        "host": "edge.example.test",
        "path": "/ws",
        "tls": "tls",
        "sni": "edge.example.test",
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return f"vmess://{encoded}"


class ProxyPoolDemoTests(unittest.TestCase):
    def test_filters_us_nodes_and_builds_round_robin_mihomo_config(self):
        subscription = base64.b64encode(
            "\n".join(
                [
                    vmess_uri("US-01", "us.example.test"),
                    vmess_uri("香港-01", "hk.example.test"),
                    vmess_uri("Japan 01", "jp.example.test", "grpc"),
                ]
            ).encode("utf-8")
        ).decode("ascii")
        nodes = parse_vmess_subscription(subscription)
        allowed, excluded = filter_nodes(
            nodes,
            (r"(?i)(?:^|[^A-Z])(?:US|USA)(?:$|[^A-Z])", r"美国"),
        )
        self.assertEqual(excluded, 1)
        self.assertEqual([node.name for node in allowed], ["香港-01", "Japan 01"])

        config = PoolConfig(
            subscription_url_file=Path("subscription_url.txt"),
            output_path=Path("mihomo.json"),
            exclude_name_patterns=(),
            listen_port=7890,
            health_check_url="https://example.test/health",
            health_check_interval_seconds=300,
        )
        mihomo = build_mihomo_config(allowed, config)
        self.assertEqual(mihomo["bind-address"], "127.0.0.1")
        self.assertEqual(mihomo["proxy-groups"][0]["strategy"], "round-robin")
        self.assertEqual(len(mihomo["proxies"]), 2)
        self.assertTrue(all("US" not in proxy["name"] for proxy in mihomo["proxies"]))
        grpc_proxy = next(proxy for proxy in mihomo["proxies"] if proxy["name"] == "Japan 01")
        self.assertEqual(grpc_proxy["network"], "grpc")

    def test_output_is_private_json(self):
        with TemporaryDirectory() as directory:
            output_path = Path(directory) / "runtime" / "mihomo.json"
            write_private_json(output_path, {"proxies": []})
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), {"proxies": []})
            self.assertEqual(output_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
