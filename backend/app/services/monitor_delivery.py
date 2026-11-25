"""监控交付领域服务：读取实际写出的附件，维护文件清单、可领取状态和送达记录。

一次交付由两个既有入口写出：
- 资源清单：沿用统一导出入口的表头、BOM 和公式转义；
- 明细导出：沿用导出任务自己的列、预览和字节数口径。
文件清单里的编码、行数、字节数全部来自磁盘上的真实文件，未写完的附件不报齐备。
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from app.attachments import (
    Attachment,
    build_inventory_attachment,
    persist_attachment,
    read_file_attachment,
    registry,
)
from app.config import settings
from app.services.delivery_store import delivery_store
from app.store import store


INVENTORY_ALIASES = {"清单", "资源清单", "inventory", "resource_inventory", "list"}
DETAIL_ALIASES = {"明细", "明细导出", "detail", "export"}


class MonitorDeliveryService:
    """把一次监控交付落成可核对、可领取、可送达的资料。"""

    INVENTORY_MODULE = "data_export"

    def __init__(self, *, directory: str | None = None) -> None:
        self.directory = directory or settings.attachment_dir

    # ---- 入口 ----
    def build_delivery(
        self,
        rule_id: int,
        *,
        detail_task_id: int | None = None,
        detail_format: str | None = None,
        fail_at_row: int | None = None,
        delivery_key: str | None = None,
        refresh: bool = True,
    ) -> dict[str, Any]:
        """执行一次（或重试一次）监控交付，刷新文件清单但保留领取和送达记录。"""
        key = self._key(rule_id, delivery_key)
        delivery = delivery_store.get(key)
        if delivery is None:
            delivery = {
                "delivery_key": key,
                "delivery_id": delivery_store.next_reference("DLV"),
                "rule_id": rule_id,
                "created_at": datetime.now().isoformat(),
                "claims": delivery_store.claims(key),
                "送达记录": delivery_store.delivery_records(key),
            }
            delivery_store.put(key, delivery)
        if refresh:
            # 重试未重新指定时沿用首次交付的配置，避免默认值把明细换回别的任务/格式。
            stored = delivery.get("config") or {}
            self._refresh(
                delivery,
                detail_task_id if detail_task_id is not None else stored.get("detail_task_id"),
                detail_format if detail_format is not None else stored.get("detail_format"),
                fail_at_row,
            )
        return delivery

    def _refresh(
        self,
        delivery: dict[str, Any],
        detail_task_id: int | None,
        detail_format: str | None,
        fail_at_row: int | None,
    ) -> None:
        # Step 1: 资源清单入口（模块清单，固定带 BOM）
        inventory_svc = self._inventory_service()
        items, _ = inventory_svc.list_entries(page=1, size=1_000_000)
        inventory = build_inventory_attachment(self.INVENTORY_MODULE, inventory_svc, items)
        persist_attachment(inventory, self.directory)
        inventory = self._from_disk(inventory.name, "csv", complete=True)

        # Step 2: 明细导出入口（导出任务自己的列、预览、字节口径）
        detail_svc = self._detail_service()
        task = self._detail_task(detail_task_id)
        chosen_format = self._detail_format(task, detail_format)
        built = detail_svc.build_attachment(
            int(task["id"]),
            fail_at_row=fail_at_row,
            format_override=chosen_format,
            directory=self.directory,
        )
        detail = self._from_disk(
            built.name,
            built.format,
            complete=built.complete,
            failed_at_row=built.failed_at_row,
        )

        attachments = [inventory, detail]
        registry.put(f"inventory:{self.INVENTORY_MODULE}", inventory)
        registry.put(f"export:{task['id']}", detail)
        files = {}
        inventory_file = inventory.name
        for attachment in attachments:
            evidence = attachment.evidence()
            evidence["claimable"] = bool(attachment.complete and attachment.written)
            evidence["name"] = self._display_name(attachment.name, inventory_file)
            files[attachment.name] = evidence
        claimable = [item.name for item in attachments if item.complete and item.written]
        incomplete = [item.name for item in attachments if not (item.complete and item.written)]

        delivery.update({
            "updated_at": datetime.now().isoformat(),
            "detail_task_id": int(task["id"]),
            "detail_format": chosen_format,
            "fail_at_row": fail_at_row,
            "attachments": [item.evidence() for item in attachments],
            "files": files,
            "claimable": claimable,
            "incomplete": incomplete,
            "complete": bool(attachments) and not incomplete,
            "config": {
                "inventory_module": self.INVENTORY_MODULE,
                "inventory_file": inventory.name,
                "detail_task_id": int(task["id"]),
                "detail_file": detail.name,
                "detail_format": chosen_format,
                "formats": [inventory.format, detail.format],
            },
        })
        delivery["config_check"] = self._check_config(delivery)

    # ---- 核对配置 ----
    def _check_config(self, delivery: dict[str, Any]) -> dict[str, Any]:
        """核对清单里的证据是否与实际写出的文件一致，并确认两种格式都在。"""
        issues: list[str] = []
        expected = delivery.get("config", {})
        seen_formats: list[str] = []
        for record in delivery.get("files", {}).values():
            path = record.get("path") or os.path.join(self.directory, record["file"])
            if not os.path.exists(path):
                issues.append(f"{record['file']} 未实际写出")
                continue
            try:
                actual = read_file_attachment(
                    path, name=record["file"], fmt=record["format"],
                    encoding=record["encoding"],
                    complete=record["complete"], failed_at_row=record["failed_at_row"],
                )
            except OSError as error:  # pragma: no cover - 文件被外部删除
                issues.append(f"{record['file']} 读取失败：{error}")
                continue
            seen_formats.append(actual.format)
            if actual.byte_count != record["byte_count"]:
                issues.append(f"{record['file']} 字节数 {record['byte_count']} 与实际 {actual.byte_count} 不一致")
            if actual.rows != record["rows"]:
                issues.append(f"{record['file']} 行数 {record['rows']} 与实际 {actual.rows} 不一致")
            if actual.line_count != record["line_count"]:
                issues.append(f"{record['file']} 总行数 {record['line_count']} 与实际 {actual.line_count} 不一致")
            if actual.encoding != record["encoding"]:
                issues.append(f"{record['file']} 编码 {record['encoding']} 与实际 {actual.encoding} 不一致")
        distinct_formats = len(set(seen_formats)) >= 2
        if not distinct_formats:
            issues.append("本次交付要求两种不同格式的文件，但实际只有一种格式")
        if expected.get("formats") and set(seen_formats) != set(expected["formats"]):
            issues.append("文件格式与交付配置不一致")
        return {
            "ok": not issues,
            "issues": issues,
            "checked_at": datetime.now().isoformat(),
            "distinct_formats": distinct_formats,
            "formats": seen_formats,
            "complete": delivery.get("complete", False),
        }

    # ---- 领取 ----
    def claim(
        self,
        rule_id: int,
        *,
        file: str | None = None,
        requester: str = "资料组",
        delivery_key: str | None = None,
    ) -> dict[str, Any]:
        """领取一份资料；先核对配置，再判断该份文件是否真的写完。"""
        delivery = self.build_delivery(rule_id, delivery_key=delivery_key, refresh=False)
        if not delivery.get("files"):
            return {"ok": False, "message": "本次交付还没有写出任何资料", "claimable": False}
        target = self._resolve(delivery, file)
        if target is None:
            return {
                "ok": False,
                "message": f"本次交付不含文件「{file}」",
                "available": list(delivery["files"]),
            }
        record = delivery["files"][target]

        # 领取前重新读取实际文件，避免清单与实际不一致。
        check = self._check_config(delivery)
        if not check["ok"]:
            return {
                "ok": False,
                "file": target,
                "claimable": False,
                "message": "配置核对未通过：" + "；".join(check["issues"]),
                "config_check": check,
            }
        if not (record["complete"] and record["written"] and record.get("claimable")):
            return {
                "ok": False,
                "file": target,
                "claimable": False,
                "delivery_complete": delivery["complete"],
                "message": f"{target} 尚未写完，不能领取",
                "evidence": record,
                "incomplete": delivery["incomplete"],
                "config_check": check,
            }

        existing = next(
            (item for item in delivery["claims"]
             if item["file"] == target and item["requester"] == requester),
            None,
        )
        if existing is not None:
            existing = dict(existing)
            existing["reused"] = True
            existing["delivery_complete"] = delivery["complete"]
            existing["incomplete"] = delivery["incomplete"]
            existing["config_check"] = check
            return {"ok": True, **existing}

        claim = {
            "claim_id": delivery_store.next_reference("CLM"),
            "file": target,
            "requester": requester,
            "claimed_at": datetime.now().isoformat(),
            "delivery_id": delivery["delivery_id"],
            "evidence": {key: record[key] for key in (
                "file", "format", "encoding", "rows", "line_count", "byte_count")},
            "delivery_complete": delivery["complete"],
            "incomplete": delivery["incomplete"],
            "config_check": check,
            "reused": False,
        }
        delivery_store.record_claim(delivery["delivery_key"], claim)
        delivery["claims"] = delivery_store.claims(delivery["delivery_key"])
        return {"ok": True, **claim}

    # ---- 送达 ----
    def deliver(
        self,
        rule_id: int,
        *,
        files: list[str] | str | None = None,
        recipient: str | None = None,
        delivery_key: str | None = None,
    ) -> dict[str, Any]:
        """把已写完的资料送达接收人；同接收人同文件只保留原送达记录。"""
        if not recipient:
            return {"ok": False, "message": "缺少接收人，无法送达"}
        delivery = self.build_delivery(rule_id, delivery_key=delivery_key, refresh=False)
        if not delivery.get("files"):
            return {"ok": False, "message": "本次交付还没有写出任何资料"}
        check = self._check_config(delivery)
        if not check["ok"]:
            return {"ok": False, "message": "配置核对未通过：" + "；".join(check["issues"]),
                    "config_check": check}

        requested = self._requested_files(delivery, files)
        if requested is None:
            return {"ok": False, "message": "送达文件不在本次交付中", "available": list(delivery["files"])}

        delivered: list[dict[str, Any]] = []
        existing: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for name in requested:
            record = delivery["files"][name]
            if not (record["complete"] and record["written"] and record.get("claimable")):
                skipped.append({
                    "file": name,
                    "reason": "附件尚未写完，未送达",
                    "failed_at_row": record.get("failed_at_row"),
                })
                continue
            previous = next(
                (item for item in delivery["送达记录"]
                 if item["file"] == name and item["recipient"] == recipient),
                None,
            )
            if previous is not None:
                existing.append(previous)
                continue
            entry = {
                "reference": delivery_store.next_reference("SND"),
                "file": name,
                "format": record["format"],
                "recipient": recipient,
                "delivered_at": datetime.now().isoformat(),
                "rows": record["rows"],
                "byte_count": record["byte_count"],
                "encoding": record["encoding"],
                "delivery_id": delivery["delivery_id"],
            }
            delivery_store.delivery_records(delivery["delivery_key"]).append(entry)
            delivery["送达记录"] = delivery_store.delivery_records(delivery["delivery_key"])
            delivered.append(entry)

        return {
            "ok": True,
            "delivery_id": delivery["delivery_id"],
            "recipient": recipient,
            "delivered": delivered,
            "existing": existing,
            "skipped": skipped,
            "claimable": list(delivery["claimable"]),
            "送达记录": list(delivery["送达记录"]),
            "delivery_complete": delivery["complete"],
            "config_check": check,
        }

    def manifest(self, rule_id: int, *, delivery_key: str | None = None) -> dict[str, Any]:
        delivery = self.build_delivery(rule_id, delivery_key=delivery_key, refresh=False)
        return {
            "delivery_id": delivery.get("delivery_id"),
            "rule_id": rule_id,
            "complete": delivery.get("complete", False),
            "claimable": delivery.get("claimable", []),
            "incomplete": delivery.get("incomplete", []),
            "files": delivery.get("files", {}),
            "attachments": delivery.get("attachments", []),
            "claims": delivery.get("claims", []),
            "送达记录": delivery.get("送达记录", []),
            "config_check": delivery.get("config_check"),
        }

    # ---- 辅助 ----
    def _key(self, rule_id: int, delivery_key: str | None) -> str:
        return f"rule:{rule_id}:{delivery_key or 'default'}"

    def _from_disk(
        self,
        name: str,
        fmt: str,
        *,
        complete: bool,
        failed_at_row: int | None = None,
    ) -> Attachment:
        path = os.path.join(self.directory, name)
        return read_file_attachment(
            path, name=name, fmt=fmt, complete=complete,
            failed_at_row=failed_at_row, source="monitor_delivery",
        )

    def _inventory_service(self):
        from app.services import service_for
        return service_for(self.INVENTORY_MODULE)

    def _detail_service(self):
        from app.services import service_for
        return service_for("data_export")

    def _detail_task(self, detail_task_id: int | None) -> dict[str, Any]:
        rows = store.rows("data_export")
        if detail_task_id is not None:
            task = store.find("data_export", detail_task_id)
            if task is None:
                raise ValueError(f"导出任务 {detail_task_id} 不存在")
            return task
        if not rows:
            raise ValueError("没有可用的导出任务")
        return rows[0]

    @staticmethod
    def _detail_format(task: dict[str, Any], override: str | None) -> str:
        if override:
            normalized = str(override).strip().lower()
            if normalized not in ("csv", "json"):
                raise ValueError(f"不支持的明细格式：{override}")
            return normalized
        configured = str(task.get("导出格式", "")).strip().lower()
        return configured if configured in ("csv", "json") else "json"

    @staticmethod
    def _display_name(name: str, inventory_file: str) -> str:
        return "资源清单" if name == inventory_file else "明细导出"

    def _resolve(self, delivery: dict[str, Any], file: str | None) -> str | None:
        files = delivery["files"]
        config = delivery.get("config", {})
        inventory_file = config.get("inventory_file", f"{self.INVENTORY_MODULE}.csv")
        detail_file = config.get("detail_file")
        if not file:
            # 未指定时优先明细，没有明细再退到清单。
            return detail_file if detail_file in files else next(iter(files), None)
        if file in files:
            return file
        lowered = file.strip().lower()
        if lowered in INVENTORY_ALIASES or file in INVENTORY_ALIASES:
            return inventory_file if inventory_file in files else None
        if lowered in DETAIL_ALIASES or file in DETAIL_ALIASES:
            return detail_file if detail_file in files else None
        return None

    def _requested_files(
        self, delivery: dict[str, Any], files: list[str] | str | None
    ) -> list[str] | None:
        if files in (None, "", "all", "全部"):
            return list(delivery["files"])
        candidates = [files] if isinstance(files, str) else list(files)
        resolved: list[str] = []
        for candidate in candidates:
            name = self._resolve(delivery, candidate)
            if name is None:
                return None
            if name not in resolved:
                resolved.append(name)
        return resolved


monitor_delivery_service = MonitorDeliveryService()
