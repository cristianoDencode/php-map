"""Projetos que o servidor MCP consulta.

Lê o `projects.json` com um leitor próprio e tolerante: no celular o
`source` é `null`, e o `ProjectStore` da interface descartaria o projeto. Só `slug` e `db` são
obrigatórios; `dsn_env` é ignorado. Os bancos são abertos **somente leitura** e por chamada
(`store.connect_readonly`), nunca carregados inteiros.

O `db` de um projeto (ou um `--db` da CLI) pode ser uma **pasta** — o `.db` mais
novo que já terminou de gravar (tem `<stem>.scan_summary.json` ao lado, como a interface grava)
é escolhido. `from_projects_file`/`from_dbs` devolvem um catálogo `dynamic`: a cada consulta só
os `stat` são refeitos (barato); o JSON e o `meta` só são relidos quando a chave muda.
"""

import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, TypeAlias

from php_map.analysis.cross import CrossReport, cross_projects, project_label
from php_map.errors import ConfigError, PhpMapError, QueryError
from php_map.graph import store

logger = logging.getLogger(__name__)

# Mesmo nome de `pipeline.SUMMARY_NAME`: não importamos `pipeline` aqui para não acoplar o `mcp/`
# (somente leitor do grafo) ao pipeline de scan.
_SCAN_SUMMARY_SUFFIX = "scan_summary.json"
_STAMP_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(-\d+)?\Z")


@dataclass(frozen=True, slots=True)
class CatalogProject:
    slug: str
    name: str
    profile: str
    db: Path
    source: Path | None  # pasta do código-fonte; None = o servidor não tem o fonte
    meta: dict[str, str]
    # `db` do `projects.json`/`--db` era uma pasta (o `db` acima já é o arquivo
    # escolhido dentro dela).
    db_from_folder: bool = False

    @property
    def graph_project(self) -> str:
        """O `meta.project` do banco (o nome que o `cross` usa para este projeto)."""
        return project_label(self.db, self.meta)[0]


def _path(text: str, base: Path) -> Path:
    """Caminho do `projects.json`: absoluto como está; relativo, à pasta do arquivo ou a uma
    pasta acima dela (a interface grava relativo ao diretório de onde rodou, em geral a raiz do
    repositório, e o cliente MCP pode subir o servidor de outro diretório) e, por fim, ao
    diretório atual."""
    raw = text.strip()
    native = Path(raw.replace("\\", "/")) if not PureWindowsPath(raw).drive else Path(raw)
    if native.is_absolute():
        return native
    for folder in (base, *base.resolve().parents):
        if (folder / native).exists():
            return folder / native
    return Path.cwd() / native


def _open(db: Path) -> dict[str, str] | None:
    try:
        return store.load_meta(db)
    except PhpMapError as exc:
        logger.warning("banco ignorado: %s", exc)
        return None


def _pick_latest(folder: Path) -> Path | None:
    """O `.db` mais novo e completo de `folder`, sem log (uso interno). Ordem pelo nome quando
    ele segue o padrão `AAAA-MM-DDTHH-MM-SS[-N]` que a interface grava (ordena como data, tarefa
    86); fora do padrão, cai para `mtime`. Nunca abre o banco — só `stat`/`glob`."""
    best: Path | None = None
    best_key: tuple[bool, str, int] | None = None
    try:
        candidates = list(folder.glob("*.db"))
    except OSError:
        return None
    for db in candidates:
        if not db.is_file():
            continue
        if not db.with_name(f"{db.stem}.{_SCAN_SUMMARY_SUFFIX}").is_file():
            continue  # scan em andamento: o .db existe, o resumo ainda não
        try:
            mtime = db.stat().st_mtime_ns
        except OSError:
            continue
        matches = bool(_STAMP_RE.match(db.stem))
        key = (matches, db.stem if matches else "", mtime)
        if best_key is None or key > best_key:
            best_key, best = key, db
    return best


def latest_graph(folder: Path) -> Path | None:
    """O `.db` mais novo de `folder` que tenha `<stem>.scan_summary.json` ao lado (grafo
    completo). `None`, com `logger.warning`, quando a pasta não tem nenhum."""
    chosen = _pick_latest(folder)
    if chosen is None:
        logger.warning("pasta sem grafo completo: %s", folder)
    return chosen


class ProjectCatalog:
    """Os projetos válidos, a conexão somente leitura e o `cross` em cache por `mtime`."""

    def __init__(
        self,
        projects: Sequence[CatalogProject],
        *,
        provider: Callable[[], Sequence[CatalogProject]] | None = None,
    ) -> None:
        if not projects and provider is None:
            raise ConfigError("nenhum projeto com grafo válido para servir")
        self._fixed = {p.slug: p for p in projects}
        self._provider = provider
        self._lock = threading.Lock()
        self._cross: dict[int, tuple[tuple[tuple[str, int], ...], CrossReport]] = {}

    @classmethod
    def dynamic(cls, provider: Callable[[], Sequence[CatalogProject]]) -> "ProjectCatalog":
        """Catálogo que pergunta ao `provider` a cada consulta: o MCP montado na
        interface acompanha a troca de projeto e os scans novos. Pode começar vazio."""
        return cls((), provider=provider)

    @property
    def _projects(self) -> dict[str, CatalogProject]:
        if self._provider is None:
            return self._fixed
        return {p.slug: p for p in self._provider()}

    # ----------------------------------------------------------------------- construção

    @classmethod
    def from_projects_file(cls, path: Path) -> "ProjectCatalog":
        """Catálogo dinâmico: relê `path` por `mtime` e cada pasta de grafos pelo
        `.db` mais novo e completo, sem reiniciar o servidor."""
        projects = _load_projects(path)
        if not projects:
            raise ConfigError("nenhum projeto com grafo válido para servir")
        key = _projects_key(path, path.parent) or (path.stat().st_mtime_ns, ())
        return cls((), provider=_ProjectsFileProvider(path, projects, key))

    @classmethod
    def from_dbs(cls, paths: Sequence[Path]) -> "ProjectCatalog":
        """Catálogo dinâmico: cada `--db` pode ser um arquivo ou uma pasta de
        grafos; a pasta é reavaliada a cada consulta pelo `.db` mais novo e completo."""
        fixed_paths = list(paths)
        projects = _load_dbs(fixed_paths)
        if not projects:
            raise ConfigError("nenhum projeto com grafo válido para servir")
        key = _dbs_key(fixed_paths)
        return cls((), provider=_DbsProvider(fixed_paths, projects, key))

    @staticmethod
    def _parse(item: Any, base: Path) -> CatalogProject | None:
        if not isinstance(item, dict):
            return None
        slug, db_text = item.get("slug"), item.get("db")
        if not (isinstance(slug, str) and slug.strip() and isinstance(db_text, str) and db_text):
            logger.warning("projeto sem slug ou sem db ignorado")
            return None
        db_path = _path(db_text, base)
        db_from_folder = db_path.is_dir()
        db = latest_graph(db_path) if db_from_folder else db_path
        if db is None:
            return None  # pasta sem grafo completo: ignora o projeto, como banco inválido
        meta = _open(db)
        if meta is None:
            return None
        source_text = item.get("source")
        source = _path(source_text, base) if isinstance(source_text, str) and source_text else None
        name, profile = item.get("name"), item.get("profile")
        return CatalogProject(
            slug=slug.strip(),
            name=name if isinstance(name, str) and name else slug,
            profile=profile if isinstance(profile, str) and profile else meta.get("profile", ""),
            db=db,
            source=_existing(source),
            meta=meta,
            db_from_folder=db_from_folder,
        )

    # ----------------------------------------------------------------------- consulta

    @property
    def projects(self) -> list[CatalogProject]:
        return list(self._projects.values())

    def get(self, slug: str) -> CatalogProject:
        projects = self._projects
        project = projects.get(slug)
        if project is None:
            valid = ", ".join(sorted(projects))
            if not valid:
                raise QueryError("nenhum projeto com grafo ainda: rode o scan na interface")
            raise QueryError(f"projeto desconhecido: {slug!r} (válidos: {valid})")
        return project

    def connect(self, slug: str) -> sqlite3.Connection:
        """Conexão nova e somente leitura (`mode=ro`): uma por chamada e por thread."""
        return store.connect_readonly(self.get(slug).db)

    def cross(self, depth: int) -> CrossReport:
        """O `cross` entre todos os projetos, em cache enquanto nenhum banco mudar."""
        projects = self.projects
        if len(projects) < 2:
            raise QueryError("o cruzamento precisa de pelo menos 2 projetos com grafo")
        try:
            key = tuple((str(p.db), p.db.stat().st_mtime_ns) for p in projects)
        except OSError as exc:
            raise QueryError(f"grafo indisponível: {exc}") from exc
        with self._lock:
            cached = self._cross.get(depth)
        if cached is not None and cached[0] == key:
            return cached[1]
        report = cross_projects([p.db for p in projects], depth)
        with self._lock:
            self._cross[depth] = (key, report)
        return report


def _existing(path: Path | None) -> Path | None:
    return path if path is not None and path.is_dir() else None


# --------------------------------------------------------------------------- `projects.json`

_ProjectsKey: TypeAlias = tuple[int, tuple[tuple[str, str, int], ...]]


def _load_projects(path: Path) -> list[CatalogProject]:
    """Lê e resolve os projetos válidos de `path` (JSON, pastas e `meta`).

    `ConfigError` se o arquivo não existir ou não tiver JSON válido com a lista `projects`."""
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"arquivo de projetos inexistente: {path}") from exc
    except (OSError, ValueError) as exc:
        raise ConfigError(f"arquivo de projetos inválido: {path} ({exc})") from exc
    items = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ConfigError(f"{path.name} sem a lista 'projects'")
    projects: list[CatalogProject] = []
    for item in items:
        project = ProjectCatalog._parse(item, path.parent)
        if project is not None and project.slug not in {p.slug for p in projects}:
            projects.append(project)
    return projects


def _projects_key(path: Path, base: Path) -> _ProjectsKey | None:
    """A chave de releitura: `mtime` de `path` + `(slug, db escolhido, mtime)` de
    cada projeto resolvível, só com `stat`/`glob` (sem abrir banco nem ler `meta`). `None`
    quando `path` não pôde ser lido ou não tem JSON válido agora — quem chama mantém a lista
    anterior."""
    try:
        path_mtime = path.stat().st_mtime_ns
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    entries: list[tuple[str, str, int]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        slug, db_text = item.get("slug"), item.get("db")
        if not (isinstance(slug, str) and slug.strip() and isinstance(db_text, str) and db_text):
            continue
        db_path = _path(db_text, base)
        chosen = _pick_latest(db_path) if db_path.is_dir() else db_path
        if chosen is None:
            entries.append((slug.strip(), db_text, -1))
            continue
        try:
            entries.append((slug.strip(), str(chosen), chosen.stat().st_mtime_ns))
        except OSError:
            entries.append((slug.strip(), db_text, -1))
    return (path_mtime, tuple(entries))


class _ProjectsFileProvider:
    """Releitura do `projects.json` por `mtime`.

    A cada chamada, refaz só os `stat`/`glob` (barato); relê o JSON inteiro e os `meta` só
    quando a chave muda. Protegido por `threading.Lock`. JSON inválido no meio de uma gravação,
    ou sem nenhum projeto válido no momento, mantém a lista anterior, com aviso."""

    def __init__(self, path: Path, projects: list[CatalogProject], key: _ProjectsKey) -> None:
        self._path, self._base = path, path.parent
        self._lock = threading.Lock()
        self._cache, self._key = projects, key

    def __call__(self) -> Sequence[CatalogProject]:
        new_key = _projects_key(self._path, self._base)
        if new_key is None:
            logger.warning(
                "projects.json ilegível por ora, mantendo a lista anterior: %s", self._path
            )
            with self._lock:
                return self._cache
        with self._lock:
            if new_key == self._key:
                return self._cache
        try:
            projects = _load_projects(self._path)
        except ConfigError as exc:
            logger.warning("projects.json inválido por ora, mantendo a lista anterior: %s", exc)
            with self._lock:
                return self._cache
        if not projects:
            logger.warning(
                "projects.json sem nenhum projeto válido por ora, mantendo a lista anterior: %s",
                self._path,
            )
            with self._lock:
                return self._cache
        with self._lock:
            self._cache, self._key = projects, new_key
            return self._cache


# --------------------------------------------------------------------------- `--db`

_DbsKey: TypeAlias = tuple[tuple[str, int], ...]


def _load_dbs(paths: Sequence[Path]) -> list[CatalogProject]:
    projects: list[CatalogProject] = []
    for raw in paths:
        db_from_folder = raw.is_dir()
        db = latest_graph(raw) if db_from_folder else raw
        if db is None:
            continue
        meta = _open(db)
        if meta is None:
            continue
        slug = project_label(db, meta)[0]
        if slug in {p.slug for p in projects}:
            logger.warning("banco ignorado: %s repete o projeto %s", db, slug)
            continue
        name = meta.get("project_name") or slug
        source = Path(meta["source"]) if meta.get("source") else None
        projects.append(
            CatalogProject(
                slug,
                name,
                meta.get("profile", "ecidade"),
                db,
                _existing(source),
                meta,
                db_from_folder=db_from_folder,
            )
        )
    return projects


def _dbs_key(paths: Sequence[Path]) -> _DbsKey:
    entries: list[tuple[str, int]] = []
    for raw in paths:
        chosen = _pick_latest(raw) if raw.is_dir() else raw
        if chosen is None:
            entries.append((str(raw), -1))
            continue
        try:
            entries.append((str(chosen), chosen.stat().st_mtime_ns))
        except OSError:
            entries.append((str(raw), -1))
    return tuple(entries)


class _DbsProvider:
    """Releitura dos `--db`: sem `projects.json`, só os caminhos dados na linha de
    comando; cada um que for pasta é reavaliado a cada consulta pelo `.db` mais novo e
    completo."""

    def __init__(self, paths: list[Path], projects: list[CatalogProject], key: _DbsKey) -> None:
        self._paths = paths
        self._lock = threading.Lock()
        self._cache, self._key = projects, key

    def __call__(self) -> Sequence[CatalogProject]:
        new_key = _dbs_key(self._paths)
        with self._lock:
            if new_key == self._key:
                return self._cache
        projects = _load_dbs(self._paths)
        if not projects:
            logger.warning("nenhum projeto com grafo válido por ora; mantendo a lista anterior")
            with self._lock:
                return self._cache
        with self._lock:
            self._cache, self._key = projects, new_key
            return self._cache
