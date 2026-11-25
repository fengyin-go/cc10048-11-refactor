"""共享附件转换：资源清单与导出明细复用同一套 CSV/JSON 渲染和格式证据。

两个既有入口的差异只通过参数表达（表头、BOM、换行符、公式转义），
渲染出的字节、行数和编码由同一份证据结构给出，监控交付据此判断文件是否齐备。
"""
from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

FORMULA_PREFIXES = ("=", "+", "-", "@")

BOM_BYTES = "﻿".encode("utf-8")


def escape_formula_value(value: Any) -> Any:
    """按既有口径转义可能被表格软件当作公式的单元格。"""
    if str(value or "").startswith(FORMULA_PREFIXES):
        return "'" + str(value)
    return value


@dataclass
class Attachment:
    """一次实际写出的附件：内容与格式证据放在一起，避免口径漂移。"""

    name: str
    format: str
    data: bytes
    encoding: str = "utf-8"
    rows: int = 0
    complete: bool = True
    written: bool = True
    failed_at_row: int | None = None
    source: str = ""
    path: str | None = None

    @property
    def bom(self) -> bool:
        return self.data.startswith(BOM_BYTES)

    @property
    def text(self) -> str:
        return self.data.decode(self.encoding)

    @property
    def byte_count(self) -> int:
        return len(self.data)

    @property
    def line_count(self) -> int:
        if not self.data:
            return 0
        return len(self.text.splitlines())

    def evidence(self) -> dict[str, Any]:
        """输出可直接与磁盘文件核对的证据字段。"""
        return {
            "file": self.name,
            "format": self.format,
            "encoding": self.encoding,
            "bom": self.bom,
            "rows": self.rows,
            "line_count": self.line_count,
            "byte_count": self.byte_count,
            "complete": self.complete,
            "written": self.written,
            "failed_at_row": self.failed_at_row,
            "path": self.path,
        }

    def snapshot(self) -> dict[str, Any]:
        return dict(self.evidence())


@dataclass
class AttachmentSet:
    """一次交付涉及的文件集合；集合齐备与否由每份附件是否写完决定。"""

    attachments: list[Attachment] = field(default_factory=list)

    def add(self, attachment: Attachment) -> Attachment:
        self.attachments.append(attachment)
        return attachment

    def get(self, name: str) -> Attachment | None:
        for attachment in self.attachments:
            if attachment.name == name:
                return attachment
        return None

    @property
    def complete(self) -> bool:
        return bool(self.attachments) and all(item.complete for item in self.attachments)

    def evidence(self) -> list[dict[str, Any]]:
        return [item.evidence() for item in self.attachments]


def render_csv(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    bom: bool = False,
    lineterminator: str = "\n",
    escape_formula: bool = False,
    fail_at_row: int | None = None,
) -> tuple[str, int, bool]:
    """把记录序列渲染成 CSV 文本，返回 (文本, 已写数据行数, 是否写完)。

    ``fail_at_row`` 为从 1 开始的数据行号；到达该行时中止，之前写完的行保留。
    """
    buffer = io.StringIO()
    if bom:
        buffer.write("﻿")
    writer = csv.writer(buffer, lineterminator=lineterminator)
    writer.writerow(list(columns))
    written = 0
    complete = True
    for index, row in enumerate(rows, start=1):
        if fail_at_row is not None and index == fail_at_row:
            complete = False
            break
        values = [escape_formula_value(value) for value in row] if escape_formula else list(row)
        writer.writerow(values)
        written += 1
    return buffer.getvalue(), written, complete


def build_csv_attachment(
    name: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    bom: bool = False,
    lineterminator: str = "\n",
    escape_formula: bool = False,
    fail_at_row: int | None = None,
    source: str = "",
) -> Attachment:
    text, written, complete = render_csv(
        columns,
        rows,
        bom=bom,
        lineterminator=lineterminator,
        escape_formula=escape_formula,
        fail_at_row=fail_at_row,
    )
    return Attachment(
        name=name,
        format="csv",
        data=text.encode("utf-8"),
        encoding="utf-8",
        rows=written,
        complete=complete,
        written=True,
        failed_at_row=None if complete else fail_at_row,
        source=source,
    )


def build_json_attachment(
    name: str,
    records: list[dict[str, Any]],
    *,
    fail_at_row: int | None = None,
    source: str = "",
) -> Attachment:
    """渲染 JSON 明细；``fail_at_row`` 表示写到该行前中断，只保留之前的行。"""
    complete = True
    failed_at_row = None
    payload = records
    if fail_at_row is not None and fail_at_row <= len(records):
        payload = records[: max(fail_at_row - 1, 0)]
        complete = False
        failed_at_row = fail_at_row
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return Attachment(
        name=name,
        format="json",
        data=text.encode("utf-8"),
        encoding="utf-8",
        rows=len(payload),
        complete=complete,
        written=True,
        failed_at_row=failed_at_row,
        source=source,
    )


def build_inventory_attachment(module: str, service: Any, items: list[dict[str, Any]]) -> Attachment:
    """资源清单：沿用原有表头（id + 模块字段 + status）与 BOM。"""
    columns = ["id", *service.LIST_FIELDS, "status"]
    rows = [
        [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS), item.get("status", "")]
        for item in items
    ]
    return build_csv_attachment(
        f"{module}.csv",
        columns,
        rows,
        bom=True,
        lineterminator="\n",
        escape_formula=True,
        source="resource_inventory",
    )


def persist_attachment(attachment: Attachment, directory: str) -> Attachment:
    """把附件真正写到磁盘，再以磁盘字节回填证据，确保证据与文件一致。"""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, attachment.name)
    with open(path, "wb") as handle:
        handle.write(attachment.data)
    with open(path, "rb") as handle:
        actual = handle.read()
    if actual != attachment.data:
        raise OSError(f"附件 {attachment.name} 写入后字节不一致")
    attachment.data = actual
    attachment.path = path
    return attachment


def _count_rows(data: bytes, fmt: str, encoding: str) -> int:
    """从文件字节数出行数：CSV 去掉表头，JSON 数顶层记录。"""
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        return 0
    if fmt == "csv":
        if not text:
            return 0
        reader = csv.reader(io.StringIO(text))
        try:
            next(reader, None)
        except csv.Error:
            return 0
        return sum(1 for _ in reader)
    if fmt == "json":
        try:
            parsed = json.loads(text) if text else []
        except json.JSONDecodeError:
            return 0
        return len(parsed) if isinstance(parsed, list) else 0
    return max(len(text.splitlines()), 0)


def read_file_attachment(
    path: str,
    *,
    name: str,
    fmt: str,
    encoding: str = "utf-8",
    complete: bool = True,
    failed_at_row: int | None = None,
    source: str = "",
) -> Attachment:
    """读取已经落盘的文件，用磁盘上的真实字节重算行数/字节数证据。"""
    with open(path, "rb") as handle:
        data = handle.read()
    return Attachment(
        name=name,
        format=fmt,
        data=data,
        encoding=encoding,
        rows=_count_rows(data, fmt, encoding),
        complete=complete,
        written=True,
        failed_at_row=failed_at_row,
        source=source,
        path=path,
    )


class AttachmentRegistry:
    """记录本次进程内实际写出的附件，供监控交付读取真实字节而不是重新估算。"""

    def __init__(self) -> None:
        self._items: dict[str, Attachment] = {}

    def put(self, key: str, attachment: Attachment) -> Attachment:
        self._items[key] = attachment
        return attachment

    def get(self, key: str) -> Attachment | None:
        return self._items.get(key)

    def keys(self) -> list[str]:
        return list(self._items)

    def clear(self) -> None:
        self._items.clear()


registry = AttachmentRegistry()


def evidence_matches(attachment: Attachment) -> bool:
    """自检：证据里的行数/字节数必须和实际内容一致。"""
    evidence = attachment.evidence()
    return (
        evidence["byte_count"] == len(attachment.data)
        and evidence["line_count"] == len(attachment.text.splitlines())
        and evidence["rows"] <= max(evidence["line_count"], 0)
        and evidence["complete"] == attachment.complete
    )
