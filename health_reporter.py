import atexit
import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOGGER = logging.getLogger(__name__)
DEFAULT_CONFIG_PATH = Path(__file__).with_name("uptime_kuma_monitors.json")


class KumaHealthReporter:
    def __init__(
        self,
        monitor_key: str,
        *,
        config_path: Optional[str] = None,
        request_timeout: float = 5.0,
    ) -> None:
        self.monitor_key = monitor_key
        self.request_timeout = request_timeout
        self.push_url = self._load_push_url(config_path)
        self._queue: queue.Queue[Optional[dict]] = queue.Queue(maxsize=32)
        self._worker: Optional[threading.Thread] = None
        self._periodic_worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._last_warning_at = 0.0
        atexit.register(self.stop)

    @property
    def enabled(self) -> bool:
        return bool(self.push_url)

    def report_up(self, message: str = "running", ping_ms: Optional[float] = None) -> None:
        self.report("up", message, ping_ms)

    def report_down(self, message: str, ping_ms: Optional[float] = None) -> None:
        self.report("down", message, ping_ms)

    def report(self, status: str, message: str, ping_ms: Optional[float] = None) -> None:
        if not self.enabled:
            return
        payload = {
            "status": status,
            "msg": self._truncate(message),
        }
        if ping_ms is not None:
            payload["ping"] = max(0, round(ping_ms, 2))
        self._ensure_worker()
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                pass

    def start_periodic(self, interval_seconds: float, message: str = "running") -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._periodic_worker and self._periodic_worker.is_alive():
                return
            self._periodic_worker = threading.Thread(
                target=self._periodic_loop,
                args=(max(1.0, interval_seconds), message),
                name=f"kuma-periodic-{self.monitor_key}",
                daemon=True,
            )
            self._periodic_worker.start()

    def stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def _periodic_loop(self, interval_seconds: float, message: str) -> None:
        self.report_up(message)
        while not self._stop_event.wait(interval_seconds):
            self.report_up(message)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._send_loop,
                name=f"kuma-reporter-{self.monitor_key}",
                daemon=True,
            )
            self._worker.start()

    def _send_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if payload is None:
                return
            try:
                self._send(payload)
            except Exception as exc:
                now = time.monotonic()
                if now - self._last_warning_at >= 60:
                    LOGGER.warning("Uptime Kuma health report failed for %s: %s", self.monitor_key, exc)
                    self._last_warning_at = now

    def _send(self, payload: dict) -> None:
        separator = "&" if "?" in self.push_url else "?"
        request_url = f"{self.push_url}{separator}{urlencode(payload)}"
        request = Request(
            request_url,
            headers={"User-Agent": "alphapumphunter-health-reporter/1.0"},
        )
        with urlopen(request, timeout=self.request_timeout) as response:
            response.read(512)

    def _load_push_url(self, config_path: Optional[str]) -> Optional[str]:
        normalized_key = re.sub(r"[^A-Za-z0-9]+", "_", self.monitor_key).upper().strip("_")
        env_url = os.getenv(f"UPTIME_KUMA_PUSH_URL_{normalized_key}") or os.getenv("UPTIME_KUMA_PUSH_URL")
        if env_url:
            return env_url.strip()

        path = Path(config_path or os.getenv("UPTIME_KUMA_CONFIG", DEFAULT_CONFIG_PATH))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None

        monitor = data.get("monitors", {}).get(self.monitor_key)
        if isinstance(monitor, str):
            return monitor.strip() or None
        if isinstance(monitor, dict):
            push_url = monitor.get("push_url")
            if isinstance(push_url, str):
                return push_url.strip() or None
        return None

    @staticmethod
    def _truncate(message: str, limit: int = 250) -> str:
        normalized = " ".join(str(message).split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."
