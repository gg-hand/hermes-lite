"""协议文件 JSON Schema 校验。

加载 data/schemas/multiagent/ 下的 7 个 schema 文件，提供 validate 方法。
schema_validation=false 时跳过校验（对齐 config.multiagent.schema_validation）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import validate as jsonschema_validate, ValidationError

from teage_liu.logging_setup import logger

# schema 文件目录（运行时动态获取，避免硬编码）
_SCHEMA_DIR = Path(__file__).parent.parent.parent / "data" / "schemas" / "multiagent"


class SchemaValidator:
    """协议文件 schema 校验器。"""

    def __init__(self, schema_dir: Path | None = None, enabled: bool = True) -> None:
        self._schema_dir = schema_dir or _SCHEMA_DIR
        self._enabled = enabled
        self._schemas: dict[str, dict] = {}
        self._load_schemas()

    def _load_schemas(self) -> None:
        """加载所有 schema 文件。"""
        schema_files = {
            "status_json": "status_json.schema.json",
            "agent_card": "agent_card.schema.yaml",
            "messages_md": "messages_md.schema.yaml",
            "audit_record": "audit_record.schema.json",
        }
        for name, filename in schema_files.items():
            path = self._schema_dir / filename
            if not path.exists():
                logger.warning(f"schema file not found: {path}")
                continue
            if path.suffix == ".json":
                with open(path, "r", encoding="utf-8") as f:
                    self._schemas[name] = json.load(f)
            else:
                with open(path, "r", encoding="utf-8") as f:
                    self._schemas[name] = yaml.safe_load(f)

    def validate_status(self, status: dict) -> None:
        """校验 status.json。"""
        self._validate("status_json", status)

    def validate_agent_card(self, frontmatter: dict) -> None:
        """校验 agent_card frontmatter。"""
        self._validate("agent_card", frontmatter)

    def validate_messages_record(self, record: dict) -> None:
        """校验 messages.md 单条记录。"""
        self._validate("messages_md", record)

    def validate_audit_record(self, record: dict) -> None:
        """校验 audit.jsonl 单条记录。"""
        self._validate("audit_record", record)

    def _validate(self, schema_name: str, instance: Any) -> None:
        """内部校验方法。enabled=false 时跳过。"""
        if not self._enabled:
            return
        schema = self._schemas.get(schema_name)
        if schema is None:
            logger.warning(f"schema '{schema_name}' not loaded, skip validation")
            return
        try:
            jsonschema_validate(instance=instance, schema=schema)
        except ValidationError as e:
            # 抛出含字段路径的错误，便于定位
            field_path = ".".join(str(p) for p in e.absolute_path) or "(root)"
            raise ValidationError(
                f"schema validation failed for '{schema_name}' at field '{field_path}': {e.message}"
            ) from e
