from typing import Any

import pytest

from php_map.ingest.records import SCHEMA_VERSION, PhpFileRecord


class PhpRecords:
    """Monta registros sintéticos do extrator (contrato do extrator) para testes do resolver."""

    @staticmethod
    def record(path: str, **fields: Any) -> PhpFileRecord:
        base: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "path": path,
            "status": "ok",
            "errors": [],
            "sha1": None,
            "lines": 10,
            "declares": [],
            "includes": [],
            "calls": [],
            "dynamic": [],
            "sql_strings": [],
            "string_refs": [],
            "dispatch": [],
        }
        return PhpFileRecord.model_validate({**base, **fields})

    @staticmethod
    def decl(
        kind: str,
        name: str,
        cls: str | None = None,
        extends: str | None = None,
        implements: list[str] | None = None,
        line: int = 1,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "name": name,
            "class": cls,
            "line_start": line,
            "line_end": line + 1,
            "extends": extends,
            "implements": implements or [],
        }

    @staticmethod
    def inc(
        target: str | None,
        type_: str = "require_once",
        kind: str = "literal",
        hint: str | None = None,
        line: int = 1,
        var_types: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": type_,
            "target": target,
            "expr_kind": kind,
            "hint": hint,
            "line": line,
            "scope": "",
            "var_types": var_types or {},
        }

    @staticmethod
    def call(
        kind: str,
        name: str,
        cls: str | None = None,
        var: str | None = None,
        receiver: str | None = None,
        line: int = 1,
        scope: str = "",
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "name": name,
            "class": cls,
            "receiver_var": var,
            "receiver_class": receiver,
            "line": line,
            "scope": scope,
        }


@pytest.fixture
def php() -> PhpRecords:
    return PhpRecords()
