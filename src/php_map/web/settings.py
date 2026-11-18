"""Configurações da interface em `<out>/ui-settings.json`.

Guarda a pasta do e-cidade escolhida, as opções do último scan, as pastas recentes e o
último grafo gerado pela interface. Gravação atômica (`.tmp` + `os.replace`). Um arquivo
inválido não impede o servidor de subir: começa do zero, com aviso no log.
"""

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from php_map import fsperm
from php_map.web.source import SETTINGS_NAME

logger = logging.getLogger(__name__)

MAX_RECENT = 5
# Fonte do schema do scan pela interface: banco (`--schema-db`), arquivos ou nenhuma.
SCHEMA_SOURCES = ("db", "files", "none")


@dataclass(frozen=True, slots=True)
class UiSettings:
    source: str | None = None
    schema: list[str] = field(default_factory=list)
    # `None` = arquivo antigo, sem a escolha: o padrão sai em `scanDefaults`.
    schema_source: str | None = None
    catalog_from_env: bool = True
    reuse_ast: bool = True
    recent_sources: list[str] = field(default_factory=list)  # a mais recente primeiro
    last_db: str | None = None


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _parse(data: Any) -> UiSettings:
    """Campo a campo: um valor de tipo errado volta ao padrão, sem descartar os outros."""
    if not isinstance(data, dict):
        raise ValueError("o conteúdo não é um objeto JSON")
    default = UiSettings()

    def text(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) and value.strip() else None

    def texts(key: str) -> list[str]:
        value = data.get(key)
        if not isinstance(value, list):
            return []
        return [v for v in value if isinstance(v, str) and v.strip()]

    def flag(key: str, fallback: bool) -> bool:
        value = data.get(key)
        return value if isinstance(value, bool) else fallback

    def choice(key: str, allowed: tuple[str, ...]) -> str | None:
        value = data.get(key)
        return value if value in allowed else None

    return UiSettings(
        source=text("source"),
        schema=texts("schema"),
        schema_source=choice("schema_source", SCHEMA_SOURCES),
        catalog_from_env=flag("catalog_from_env", default.catalog_from_env),
        reuse_ast=flag("reuse_ast", default.reuse_ast),
        recent_sources=texts("recent_sources")[:MAX_RECENT],
        last_db=text("last_db"),
    )


class UiSettingsStore:
    """Leitura e gravação de `<out>/ui-settings.json`, com um lock para as threads do servidor."""

    def __init__(self, out_dir: Path) -> None:
        self.path = out_dir / SETTINGS_NAME
        self._lock = threading.Lock()

    def load(self) -> UiSettings:
        try:
            return _parse(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return UiSettings()
        except (OSError, ValueError) as exc:
            logger.warning("%s inválido, começando do zero: %s", self.path.name, exc)
            return UiSettings()

    def update(self, **changes: Any) -> UiSettings:
        """Aplica `changes` (campos de `UiSettings`) sobre o que está gravado e grava."""
        with self._lock:
            current = replace(self.load(), **changes)
            self._write(current)
            return current

    def remember_source(self, source: str, **changes: Any) -> UiSettings:
        """Usa `source` como pasta atual e a põe no topo das recentes (sem repetir, até 5)."""
        with self._lock:
            current = self.load()
            recent = [source, *(s for s in current.recent_sources if not _same_path(s, source))]
            current = replace(current, source=source, recent_sources=recent[:MAX_RECENT], **changes)
            self._write(current)
            return current

    def _write(self, settings: UiSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        text = json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n"
        tmp.write_text(text, encoding="utf-8")
        fsperm.secure_file(tmp)
        os.replace(tmp, self.path)
