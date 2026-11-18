"""Cache incremental do extrator PHP: `php.jsonl` + `meta.json`.

Um cache por pasta de origem: `<out>/ast/<nome>-<hash8>/`. A cópia de
teste (CRLF) e a instalação real (LF) diferem pelo sha1 em 95% dos arquivos, então nunca
compartilham registros. O formato antigo (`<out>/ast/php.jsonl`) é migrado para a subpasta da
origem registrada no `meta.json` dele (`migrate_legacy`).

Inspirado no `SkipCommitedTask` do IPED: um arquivo já extraído, com o mesmo `sha1`, não
volta ao container. O `meta.json` diz de onde o cache veio (`source`) e com qual extrator
(`schema_version` e `extractor_hash`); qualquer diferença nesses dois força a reextração
completa. O cache final é sempre montado em `php.jsonl.tmp`, na ordem de caminho, e só então
troca o definitivo.
"""

import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from php_map import fsperm
from php_map.config import Settings
from php_map.errors import ConfigError, IngestError
from php_map.ingest.records import SCHEMA_VERSION

logger = logging.getLogger(__name__)

AST_DIR = "ast"
CACHE_NAME = "php.jsonl"
META_NAME = "meta.json"
NEW_NAME = "php.new.jsonl"
_CHUNK = 1 << 20
# Chaves de topo do registro, nesta ordem, antes de qualquer objeto aninhado (contrato do
# extrator): a 1ª ocorrência na linha é a do registro. Dentro de strings JSON as aspas vêm
# escapadas, então não há falso positivo.
_PATH = re.compile(rb'"path":("(?:[^"\\]|\\.)*")')
_STATUS = re.compile(rb'"status":"([a-z_]+)"')
_SHA1 = re.compile(rb'"sha1":(?:"([0-9a-f]{40})"|null)')

Reason = str  # "incremental" | "schema_version" | "extractor_hash" | "no_cache" | "disabled"


@dataclass(frozen=True, slots=True)
class AstMeta:
    source: str
    schema_version: int
    extractor_hash: str
    # informativos (o aviso da interface), fora da comparação do plano.
    files: int | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """Posição de um registro no cache (para copiar a linha sem parsear o JSON)."""

    offset: int
    length: int
    sha1: str | None
    status: str


@dataclass(slots=True)
class CachePlan:
    reason: Reason
    to_extract: list[str]
    reused: set[str] = field(default_factory=set)
    # Registros fora do conjunto escaneado (`--limit`) que continuam válidos no cache.
    kept: set[str] = field(default_factory=set)
    removed: int = 0
    entries: dict[str, CacheEntry] = field(default_factory=dict)
    hash_seconds: float = 0.0

    def summary(self) -> dict[str, object]:
        data: dict[str, object] = {
            "reused": len(self.reused),
            "extracted": len(self.to_extract),
            "removed": self.removed,
            "reason": self.reason,
        }
        if self.kept:
            data["kept"] = len(self.kept)
        return data


def source_key(source: Path) -> str:
    """Caminho absoluto normalizado (no Windows, sem diferença de caixa nem de barra)."""
    return os.path.normcase(str(source.resolve()))


def extractor_hash(settings: Settings) -> str:
    """sha1 do conteúdo de `php-extractor/src/*.php` (por nome) seguido de `bin/extract.php`."""
    digest = hashlib.sha1()
    files = sorted((settings.extractor_dir / "src").glob("*.php"), key=lambda p: p.name)
    for path in [*files, settings.extractor_dir / "bin/extract.php"]:
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"")
    return digest.hexdigest()


def current_meta(source: Path, settings: Settings) -> AstMeta:
    return AstMeta(str(source.resolve()), SCHEMA_VERSION, extractor_hash(settings))


def read_meta(ast_dir: Path) -> AstMeta | None:
    """`None` se não houver `meta.json` ou se ele estiver ilegível (tratado como sem cache)."""
    try:
        data = json.loads((ast_dir / META_NAME).read_text(encoding="utf-8"))
        files, updated_at = data.get("files"), data.get("updated_at")
        return AstMeta(
            str(data["source"]),
            int(data["schema_version"]),
            str(data["extractor_hash"]),
            files if isinstance(files, int) and not isinstance(files, bool) else None,
            updated_at if isinstance(updated_at, str) else None,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def write_meta(ast_dir: Path, meta: AstMeta) -> None:
    ast_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(asdict(meta), ensure_ascii=False, indent=2) + "\n"
    (ast_dir / META_NAME).write_text(text, encoding="utf-8")
    fsperm.secure_file(ast_dir / META_NAME)


def cache_key(source: Path) -> str:
    """`<nome>-<hash8>`: o nome da pasta (minúsculas, só `[a-z0-9-]`) e os 8 primeiros hex do
    sha1 de `source_key`. As duas pastas `e-cidade` (cópia e real) só se separam pelo hash."""
    name = re.sub(r"[^a-z0-9-]+", "-", source.resolve().name.lower()).strip("-") or "origem"
    digest = hashlib.sha1(source_key(source).encode("utf-8")).hexdigest()[:8]
    return f"{name}-{digest}"


def cache_dir(out_dir: Path, source: Path) -> Path:
    return out_dir / AST_DIR / cache_key(source)


def migrate_legacy(out_dir: Path) -> Path | None:
    """Formato antigo (`<out>/ast/php.jsonl` + `meta.json` válido) → a subpasta da origem
    do `meta.json`, por `os.replace`, sem reextrair. Apaga `php.new.jsonl` e `php.jsonl.tmp`
    soltos em `<out>/ast/`. Devolve o destino, ou `None` se não havia o que migrar (ou se o
    destino já tem cache: aí não sobrescreve e deixa o antigo onde está)."""
    root = out_dir / AST_DIR
    meta = read_meta(root)
    if meta is None or not (root / CACHE_NAME).is_file():
        return None
    target = cache_dir(out_dir, Path(meta.source))
    if (target / CACHE_NAME).exists():
        logger.warning("cache de AST antigo em %s não migrado: %s já tem cache", root, target.name)
        return None
    target.mkdir(parents=True, exist_ok=True)
    os.replace(root / CACHE_NAME, target / CACHE_NAME)
    os.replace(root / META_NAME, target / META_NAME)
    for leftover in (NEW_NAME, CACHE_NAME + ".tmp"):
        (root / leftover).unlink(missing_ok=True)
    return target


@dataclass(frozen=True, slots=True)
class CacheInfo:
    """O que o próximo scan com `--reuse-ast` encontra para uma pasta (aviso da interface)."""

    exists: bool  # há `php.jsonl` + `meta.json` desta pasta (novo ou no formato antigo)
    reusable: bool  # existe e tem o `schema_version` e o `extractor_hash` atuais
    files: int | None
    updated_at: str | None
    reason: Reason  # "reusable" | "no_cache" | "schema_version" | "extractor_hash"


def cache_info(out_dir: Path, source: Path, settings: Settings) -> CacheInfo:
    """Lê só os `meta.json` (o da subpasta e o do formato antigo), nunca o `php.jsonl`."""
    candidates = [cache_dir(out_dir, source), out_dir / AST_DIR]
    key = source_key(source)
    for folder in candidates:
        meta = read_meta(folder)
        if meta is None or not (folder / CACHE_NAME).is_file():
            continue
        if os.path.normcase(meta.source) != key:
            continue  # o formato antigo de outra pasta
        current = current_meta(source, settings)
        if meta.schema_version != current.schema_version:
            reason = "schema_version"
        elif meta.extractor_hash != current.extractor_hash:
            reason = "extractor_hash"
        else:
            reason = "reusable"
        return CacheInfo(True, reason == "reusable", meta.files, meta.updated_at, reason)
    return CacheInfo(False, False, None, None, "no_cache")


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def index_cache(cache: Path) -> dict[str, CacheEntry]:
    """Caminho → posição, `sha1` e `status` de cada linha, sem validar o registro inteiro."""
    entries: dict[str, CacheEntry] = {}
    offset = 0
    with cache.open("rb") as handle:
        for line in handle:
            length = len(line)
            path_match = _PATH.search(line)
            if path_match is not None:
                sha1 = _SHA1.search(line)
                status = _STATUS.search(line)
                entries[json.loads(path_match.group(1))] = CacheEntry(
                    offset,
                    length,
                    sha1.group(1).decode("ascii") if sha1 and sha1.group(1) else None,
                    status.group(1).decode("ascii") if status else "unknown",
                )
            offset += length
    return entries


def plan_cache(
    source: Path,
    files: Sequence[str],
    all_files: Sequence[str],
    ast_dir: Path,
    settings: Settings,
    reuse: bool,
) -> CachePlan:
    """Decide o que extrair. `files` é o conjunto escaneado; `all_files`, tudo o que foi
    descoberto (diferem só com `--limit`). Recusa um cache de outra origem (`ConfigError`)."""
    cache = ast_dir / CACHE_NAME
    meta = read_meta(ast_dir)
    if not reuse:
        return CachePlan("disabled", list(files))
    if meta is None or not cache.is_file():
        return CachePlan("no_cache", list(files))
    current = current_meta(source, settings)
    if os.path.normcase(meta.source) != source_key(source):
        raise ConfigError(
            f"o cache de AST em {ast_dir} é de outra origem ({meta.source}); "
            "use outro --out ou apague o cache"
        )
    if meta.schema_version != current.schema_version:
        return CachePlan("schema_version", list(files))
    if meta.extractor_hash != current.extractor_hash:
        return CachePlan("extractor_hash", list(files))

    entries = index_cache(cache)
    started = time.perf_counter()
    plan = CachePlan("incremental", [], entries=entries)
    for path in files:
        entry = entries.get(path)
        if entry is not None and entry.sha1 is not None and entry.sha1 == file_sha1(source / path):
            plan.reused.add(path)
        else:
            plan.to_extract.append(path)
    plan.hash_seconds = round(time.perf_counter() - started, 2)
    scanned = set(files)
    plan.kept = {path for path in all_files if path in entries and path not in scanned}
    plan.removed = len(entries.keys() - set(all_files))
    return plan


def assemble(ast_dir: Path, plan: CachePlan, all_files: Sequence[str]) -> Counter[str]:
    """Monta o cache final (reaproveitados + recém-extraídos em `php.new.jsonl` + mantidos),
    na ordem de `all_files`, e troca o definitivo. Devolve a contagem de `status` do que foi
    escaneado (reaproveitado ou extraído)."""
    cache, new, tmp = ast_dir / CACHE_NAME, ast_dir / NEW_NAME, ast_dir / (CACHE_NAME + ".tmp")
    extracted = set(plan.to_extract)
    statuses: Counter[str] = Counter()
    old = cache.open("rb") if plan.entries else None
    fresh = new.open("rb") if extracted else None
    try:
        with tmp.open("wb") as out:
            for path in all_files:
                if path in extracted:
                    assert fresh is not None
                    line = fresh.readline()
                    match = _PATH.search(line)
                    if match is None or json.loads(match.group(1)) != path:
                        raise IngestError(f"cache novo fora de ordem: esperado {path!r}")
                    status = _STATUS.search(line)
                    statuses[status.group(1).decode("ascii") if status else "unknown"] += 1
                elif path in plan.reused or path in plan.kept:
                    assert old is not None
                    entry = plan.entries[path]
                    old.seek(entry.offset)
                    line = old.read(entry.length)
                    if path in plan.reused:
                        statuses[entry.status] += 1
                else:
                    continue
                out.write(line if line.endswith(b"\n") else line + b"\n")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if old is not None:
            old.close()
        if fresh is not None:
            fresh.close()
    os.replace(tmp, cache)
    new.unlink(missing_ok=True)
    return statuses
