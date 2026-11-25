"""交付资料仓库：保存每次监控交付写出的文件清单、可领取状态和送达记录。

全部状态只在这里流转；路由层只做转发。
"""
from __future__ import annotations

from typing import Any


class DeliveryStore:
    """按交付键保存一次完整资料；重试命中同一键时复用已有记录。"""

    def __init__(self) -> None:
        self._deliveries: dict[str, dict[str, Any]] = {}
        self._claims: dict[str, list[dict[str, Any]]] = {}
        self._reference = {"value": 0}

    def reset(self) -> None:
        self._deliveries.clear()
        self._claims.clear()
        self._reference["value"] = 0

    def get(self, key: str) -> dict[str, Any] | None:
        return self._deliveries.get(key)

    def put(self, key: str, delivery: dict[str, Any]) -> dict[str, Any]:
        self._deliveries[key] = delivery
        return delivery

    def keys(self) -> list[str]:
        return list(self._deliveries)

    def next_reference(self, prefix: str) -> str:
        self._reference["value"] += 1
        return f"{prefix}-{self._reference['value']:04d}"

    def claims(self, key: str) -> list[dict[str, Any]]:
        return self._claims.setdefault(key, [])

    def record_claim(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        self.claims(key).append(record)
        return record

    def delivery_records(self, key: str) -> list[dict[str, Any]]:
        delivery = self.get(key)
        if not delivery:
            return []
        return delivery.setdefault("送达记录", [])

    def record_delivery(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        self.delivery_records(key).append(record)
        return record


delivery_store = DeliveryStore()
