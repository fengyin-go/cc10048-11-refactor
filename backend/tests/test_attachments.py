"""共享附件转换与监控交付的回归测试。

重点守住两件事：
- 资源清单和导出明细换到共享层后，输出字节与改造前完全一致；
- 交付清单里的编码/行数/字节数来自磁盘实际文件，未写完的不报齐备。
"""
import csv
import io
import json
import os
import random

import pytest
from fastapi import HTTPException

from app.attachments import (
    build_csv_attachment,
    build_inventory_attachment,
    evidence_matches,
    read_file_attachment,
)
from app.routers import chains
from app.routers.domain import data_export_execute
from app.services import SERVICES
from app.services.data_export import DataExportService
from app.services.delivery_store import delivery_store
from app.services.monitor_delivery import MonitorDeliveryService
from app.store import store


def _legacy_inventory(module: str, service) -> str:
    """改造前资源清单入口的原始实现，用于逐字节对照。"""
    items, _ = service.list_entries(page=1, size=1_000_000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return output.getvalue()


def _legacy_export(task_id: int) -> tuple[str, int, list[dict]]:
    """改造前导出任务执行的原始实现，用于逐字节对照。"""
    task = store.find("data_export", task_id)
    fmt = str(task.get("导出格式", "csv"))
    rng = random.Random(task_id)
    n_rows = rng.randint(10, 500)
    headers = ["id", "日期", "指标名称", "维度值", "指标数值", "环比变化", "同比变化"]
    data = []
    for i in range(n_rows):
        data.append({
            "id": i + 1,
            "日期": f"2026-10-{1 + (i % 31):02d}",
            "指标名称": f"指标_{rng.randint(1, 10)}",
            "维度值": f"维度{rng.choice('ABCD')}",
            "指标数值": round(rng.uniform(100, 10000), 2),
            "环比变化": f"{rng.uniform(-10, 10):.1f}%",
            "同比变化": f"{rng.uniform(-20, 20):.1f}%",
        })
    content = ""
    if fmt == "json":
        content = json.dumps(data, ensure_ascii=False, indent=2)
    elif fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers)
        writer.writeheader()
        writer.writerows(data)
        content = buf.getvalue()
    return content, n_rows, data


@pytest.fixture(autouse=True)
def _clean_delivery():
    delivery_store.reset()
    yield
    delivery_store.reset()


def _add_task(fmt: str) -> int:
    rows = store.rows("data_export")
    task_id = max(int(row["id"]) for row in rows) + 1
    rows.append({
        "id": task_id, "status": "排队中", "pending": True, "abnormal": False,
        "任务标识": f"测试导出{task_id}", "查询方案": "测试方案", "导出格式": fmt,
        "字段筛选": "全部", "行数上限": "500", "导出状态": "排队中",
        "文件大小": "", "完成时间": "",
    })
    return task_id


class TestInventoryByteParity:
    @pytest.mark.parametrize("module", sorted(SERVICES))
    def test_inventory_bytes_unchanged(self, module):
        service = SERVICES[module]
        items, _ = service.list_entries(page=1, size=1_000_000)
        attachment = build_inventory_attachment(module, service, items)
        legacy = _legacy_inventory(module, service)
        assert attachment.text == legacy
        assert attachment.data == legacy.encode("utf-8")
        assert attachment.data.startswith("﻿".encode("utf-8"))
        assert attachment.evidence()["bom"] is True
        assert attachment.evidence()["encoding"] == "utf-8"
        assert evidence_matches(attachment)


class TestExportByteParity:
    @pytest.mark.parametrize("fmt", ["csv", "json"])
    def test_export_output_unchanged(self, fmt, tmp_path):
        task_id = _add_task(fmt)
        legacy_content, legacy_rows, legacy_preview = _legacy_export(task_id)
        result = DataExportService().execute_export(task_id)
        assert result["rows_exported"] == legacy_rows
        assert result["preview"] == legacy_preview[:5]
        assert result["file_size_kb"] == round(len(legacy_content.encode("utf-8")) / 1024, 2)
        attachment = DataExportService().build_attachment(task_id, directory=str(tmp_path))
        assert attachment.text == legacy_content
        assert attachment.byte_count == len(legacy_content.encode("utf-8"))
        assert evidence_matches(attachment)


class TestInterruptedExport:
    @pytest.mark.parametrize("fmt", ["csv", "json"])
    def test_fail_at_row_keeps_written_rows_only(self, fmt, tmp_path):
        task_id = _add_task(fmt)
        attachment = DataExportService().build_attachment(
            task_id, fail_at_row=10, directory=str(tmp_path))
        assert attachment.complete is False
        assert attachment.rows == 9
        assert attachment.failed_at_row == 10
        on_disk = read_file_attachment(
            os.path.join(str(tmp_path), attachment.name),
            name=attachment.name, fmt=fmt, complete=False, failed_at_row=10)
        assert on_disk.rows == 9
        assert on_disk.byte_count == attachment.byte_count
        assert on_disk.line_count == attachment.line_count


class TestDeliveryEvidence:
    def test_manifest_reads_actual_files(self, tmp_path):
        service = MonitorDeliveryService(directory=str(tmp_path))
        detail_task = _add_task("json")
        delivery = service.build_delivery(1, detail_task_id=detail_task, delivery_key="t")
        assert delivery["complete"] is True
        formats = {record["format"] for record in delivery["files"].values()}
        assert formats == {"csv", "json"}
        for name, record in delivery["files"].items():
            path = os.path.join(str(tmp_path), name)
            raw = open(path, "rb").read()
            assert record["byte_count"] == len(raw)
            assert record["encoding"] == "utf-8"
            assert record["line_count"] == len(raw.decode("utf-8").splitlines())
            assert record["claimable"] is True

    def test_incomplete_detail_not_claimable(self, tmp_path):
        service = MonitorDeliveryService(directory=str(tmp_path))
        detail_task = _add_task("json")
        delivery = service.build_delivery(
            1, detail_task_id=detail_task, fail_at_row=10, delivery_key="t")
        assert delivery["complete"] is False
        assert delivery["incomplete"] == [f"data_export_{detail_task}.json"]
        assert delivery["claimable"] == ["data_export.csv"]
        detail = delivery["files"][f"data_export_{detail_task}.json"]
        assert detail["complete"] is False
        assert detail["rows"] == 9
        assert detail["claimable"] is False

        claimed_inventory = service.claim(1, file="清单", delivery_key="t")
        assert claimed_inventory["ok"] is True
        assert claimed_inventory["delivery_complete"] is False
        claimed_detail = service.claim(1, file="明细", delivery_key="t")
        assert claimed_detail["ok"] is False
        assert claimed_detail["claimable"] is False

    def test_claim_idempotent_per_requester(self, tmp_path):
        service = MonitorDeliveryService(directory=str(tmp_path))
        detail_task = _add_task("json")
        service.build_delivery(1, detail_task_id=detail_task, delivery_key="t")
        first = service.claim(1, file="清单", delivery_key="t")
        second = service.claim(1, file="清单", delivery_key="t")
        assert first["claim_id"] == second["claim_id"]
        assert second["reused"] is True
        assert len(delivery_store.claims("rule:1:t")) == 1

    def test_delivery_keeps_original_record_for_same_recipient(self, tmp_path):
        service = MonitorDeliveryService(directory=str(tmp_path))
        detail_task = _add_task("json")
        service.build_delivery(1, detail_task_id=detail_task, fail_at_row=10, delivery_key="t")
        first = service.deliver(1, files="清单", recipient="资料组", delivery_key="t")
        assert [item["file"] for item in first["delivered"]] == ["data_export.csv"]

        service.build_delivery(1, delivery_key="t")
        retry_one = service.deliver(1, files="全部", recipient="资料组", delivery_key="t")
        retry_two = service.deliver(1, files="全部", recipient="资料组", delivery_key="t")
        assert [item["file"] for item in retry_one["existing"]] == ["data_export.csv"]
        assert [item["file"] for item in retry_one["delivered"]] == [f"data_export_{detail_task}.json"]
        assert retry_two["delivered"] == []
        records = service.manifest(1, delivery_key="t")["送达记录"]
        assert len(records) == 2
        assert len({(item["file"], item["recipient"]) for item in records}) == 2

    def test_tampered_file_fails_config_check(self, tmp_path):
        service = MonitorDeliveryService(directory=str(tmp_path))
        detail_task = _add_task("json")
        service.build_delivery(1, detail_task_id=detail_task, delivery_key="t")
        path = os.path.join(str(tmp_path), "data_export.csv")
        with open(path, "ab") as handle:
            handle.write(b"\n999,x\n")
        result = service.claim(1, file="清单", delivery_key="t")
        assert result["ok"] is False
        assert "不一致" in result["message"]


class TestChainMonitorDelivery:
    def test_chain_keeps_original_sections(self, tmp_path, monkeypatch):
        monkeypatch.setattr(chains.monitor_delivery_service, "directory", str(tmp_path))
        detail_task = _add_task("json")
        result = chains.chain_monitor_delivery(1, detail_task_id=detail_task)
        assert result["chain"] == "监控交付链路"
        assert result["anomaly_detection"]
        assert len(result["data_freshness"]) >= 1
        assert len(result["data_export"]) == 2
        assert {item["format"] for item in result["data_export"]} == {"csv", "json"}

    def test_chain_unknown_rule(self):
        with pytest.raises(HTTPException):
            chains.chain_monitor_delivery(99999)

    def test_domain_export_fail_at_row(self, tmp_path):
        task_id = _add_task("csv")
        result = data_export_execute(task_id, fail_at_row=10)
        assert result["attachment"]["complete"] is False
        assert result["attachment"]["failed_at_row"] == 10
        assert result["attachment"]["rows"] == 9
