"""Estado do servidor da interface.

O banco servido pode ser trocado (scan pela interface, troca de projeto): cada requisição lê o
caminho **uma vez**, no início, e termina no banco em que começou. `out_dir` é fixo (a pasta do
`--db` da linha de comando): ali ficam `ui-settings.json`, `projects.json`, o cache de AST e
`grafos/`.

Projetos: cada um tem o seu grafo; o ativo é o que o servidor serve. Sem
`projects.json`, o banco do `--db` vira um projeto implícito com o `meta` dele (nada é gravado
até a primeira mudança). A troca de projeto **abre outro arquivo**, nunca substitui o aberto
(o `os.replace` falha no Windows com o banco aberto).
"""

import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from php_map.access.store import Vault
from php_map.analysis.cross import CrossReport, cross_projects
from php_map.config import Settings
from php_map.errors import ConfigError, IngestError, PhpMapError
from php_map.graph import store
from php_map.ingest import php_extractor
from php_map.profiles.base import PROFILES
from php_map.web.jobs import GRAPHS_DIR, ScanJobManager
from php_map.web.projects import (
    Project,
    ProjectStore,
    build_project,
    change_project,
    check_slug,
    slugify,
)
from php_map.web.settings import UiSettingsStore

if TYPE_CHECKING:
    from php_map.web.tunnel import TunnelManager

logger = logging.getLogger(__name__)

DOCKER_CACHE_SECONDS = 30.0
DEV_PORT = 5173  # `npm run dev` (Vite)
NO_GRAPH = "o projeto {name} ainda não tem grafo: rode o scan"


@dataclass
class AppState:
    db_path: Path | None
    arg_source: Path | None = None  # `--source`; `setSource` passa a valer por cima dele
    out_dir: Path | None = None  # padrão: a pasta de `db_path`
    read_only: bool = False
    port: int = 8765
    scan_command: Sequence[str] | None = None  # testes: um `scan` falso
    explicit_db: bool = True  # o `--db` foi pedido: vale por cima do projeto ativo salvo
    # Hosts públicos: o `Host` que um túnel repassa; só `/mcp` responde.
    public_hosts: list[str] = field(default_factory=list)
    # `source` no MCP exposto: desligado até o analista ligar (em memória).
    mcp_expose_source: bool = False
    tunnel: "TunnelManager | None" = None  # túnel do `/mcp`; montado pelo `create_app`
    vault: Vault | None = None  # cofre de acesso; None = sem login e sem tokens
    mcp_available: bool = False  # o MCP está montado em `/mcp` (extra `[mcp]` instalado)
    settings: UiSettingsStore = field(init=False)
    jobs: ScanJobManager = field(init=False)
    project_store: ProjectStore = field(init=False)
    _projects: list[Project] = field(init=False, default_factory=list)
    _active: str | None = field(init=False, default=None)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)
    _docker: tuple[float, bool, str | None] | None = field(init=False, default=None)
    _cross: tuple[tuple[tuple[str, int], ...], int, CrossReport | None, str | None] | None = field(
        init=False, default=None
    )

    def __post_init__(self) -> None:
        if self.out_dir is None:
            if self.db_path is None:
                raise ConfigError("sem banco e sem pasta de saída: nada para servir")
            self.out_dir = self.db_path.parent
        self.settings = UiSettingsStore(self.out_dir)
        self.project_store = ProjectStore(self.out_dir)
        self.jobs = ScanJobManager(
            self.out_dir,
            self.settings,
            self.switch_db,
            command=self.scan_command,
            on_project_scanned=self.project_scanned,
        )
        self._load_projects()

    # ----------------------------------------------------------------------- projetos

    def _load_projects(self) -> None:
        saved, active = self.project_store.load()
        self._projects, self._active = saved, active
        if not saved:
            if self.db_path is not None:
                self._adopt_implicit(self.db_path)
            return
        if self.explicit_db and self.db_path is not None:
            self._adopt_db(self.db_path)
        else:
            self._serve_active()

    @staticmethod
    def _implicit_project(db: Path) -> Project | None:
        """Um projeto com o `meta` do banco (anterior ao perfil no `meta`: o nome da pasta)."""
        try:
            meta = store.load_meta(db)
        except PhpMapError as exc:
            logger.warning("banco sem meta legível, sem projeto implícito: %s", exc)
            return None
        source = meta.get("source") or ""
        slug = meta.get("project") or slugify(Path(source).name if source else db.stem)
        try:
            check_slug(slug)
        except ConfigError:
            slug = slugify(slug)
        profile = meta.get("profile")
        return Project(
            slug=slug,
            name=meta.get("project_name") or slug,
            profile=profile if profile is not None and profile in PROFILES else "ecidade",
            source=source,
            db=str(db),
            last_scan=meta.get("scanned_at"),
        )

    def _adopt_implicit(self, db: Path) -> None:
        """Sem projetos salvos: o banco do `--db` vira um projeto com o `meta` dele."""
        implicit = self._implicit_project(db)
        if implicit is not None:
            self._projects, self._active = [implicit], implicit.slug

    def _adopt_db(self, db: Path) -> None:
        """`--db` explícito com projetos salvos: serve este banco, dentro do projeto de mesmo
        slug (só na memória; vira definitivo na próxima gravação)."""
        try:
            meta = store.load_meta(db)
        except PhpMapError:
            self._serve_active()
            return
        slug = meta.get("project")
        match = next((p for p in self._projects if p.slug == slug), None)
        if match is None:  # um sistema que a lista não tem: entra como projeto implícito
            implicit = self._implicit_project(db)
            if implicit is None or self._get(implicit.slug) is not None:
                self._serve_active(fallback=db)
                return
            self._projects = [*self._projects, implicit]
            self._active = implicit.slug
            return
        self._replace(replace(match, db=str(db)))
        self._active = match.slug

    def _serve_active(self, fallback: Path | None = None) -> None:
        active = self._get(self._active) if self._active else None
        if active is None and self._projects:
            active = self._projects[0]
            self._active = active.slug
        self.db_path = active.graph_path() if active is not None else None
        if self.db_path is None:
            self.db_path = fallback

    def _get(self, slug: str | None) -> Project | None:
        return next((p for p in self._projects if p.slug == slug), None)

    def _replace(self, project: Project) -> None:
        self._projects = [project if p.slug == project.slug else p for p in self._projects]

    def projects(self) -> list[Project]:
        with self._lock:
            return list(self._projects)

    def active_project(self) -> Project | None:
        with self._lock:
            return self._get(self._active)

    def project(self, slug: str) -> Project:
        with self._lock:
            found = self._get(slug)
        if found is None:
            raise ConfigError(f"projeto não encontrado: {slug}")
        return found

    def _persist(self) -> None:
        self.project_store.save(self._projects, self._active)

    def add_project(
        self,
        *,
        name: str,
        profile: str,
        source: str,
        slug: str | None = None,
        schema_from_db: bool = False,
        dsn_env: str | None = None,
    ) -> Project:
        with self._lock:
            project = build_project(
                slug=slug,
                name=name,
                profile=profile,
                source=source,
                schema_from_db=schema_from_db,
                dsn_env=dsn_env,
                taken={p.slug for p in self._projects},
            )
            self._projects = [*self._projects, project]
            if self._active is None:
                self._active = project.slug
            self._persist()
            return project

    def update_project(
        self,
        slug: str,
        *,
        name: str | None = None,
        profile: str | None = None,
        source: str | None = None,
        schema_from_db: bool | None = None,
        dsn_env: str | None = None,
        clear_dsn_env: bool = False,
    ) -> Project:
        with self._lock:
            current = self._get(slug)
            if current is None:
                raise ConfigError(f"projeto não encontrado: {slug}")
            changed = change_project(
                current,
                name=name,
                profile=profile,
                source=source,
                schema_from_db=schema_from_db,
                dsn_env=dsn_env,
                clear_dsn_env=clear_dsn_env,
            )
            self._replace(changed)
            self._persist()
            return changed

    def remove_project(self, slug: str) -> None:
        """Tira o projeto da lista. Os arquivos (grafo, cache de AST) ficam em disco."""
        with self._lock:
            if self._get(slug) is None:
                raise ConfigError(f"projeto não encontrado: {slug}")
            if slug == self._active:
                raise ConfigError("troque de projeto antes de remover o projeto ativo")
            running = self.jobs.current_slug()
            if running == slug and self.jobs.is_running():
                raise ConfigError("há um scan deste projeto em andamento: cancele antes")
            self._projects = [p for p in self._projects if p.slug != slug]
            self._persist()

    def set_active(self, slug: str) -> Project:
        """Passa a servir o grafo do projeto. Sem grafo ainda, o servidor fica sem banco e a
        interface oferece o scan."""
        with self._lock:
            project = self._get(slug)
            if project is None:
                raise ConfigError(f"projeto não encontrado: {slug}")
            self._active = slug
            self.db_path = project.graph_path()
            self._persist()
            return project

    def project_scanned(self, slug: str, db: Path) -> None:
        """Fim de um scan de projeto com sucesso: guarda o grafo novo e, se o projeto é o
        ativo, passa a servi-lo."""
        with self._lock:
            project = self._get(slug)
            if project is None:
                return  # removido durante o scan: o grafo fica só em disco
            scanned = datetime.now().astimezone().isoformat(timespec="seconds")
            self._replace(replace(project, db=str(db), last_scan=scanned))
            if slug == self._active:
                self.db_path = db
            self._persist()

    # ----------------------------------------------------------------------- banco servido

    @property
    def settings_path(self) -> Path:
        return self.settings.path

    def current_db(self) -> Path | None:
        with self._lock:
            return self.db_path

    def switch_db(self, path: Path) -> None:
        """Novas requisições passam a abrir `path`; as em curso terminam no banco anterior."""
        with self._lock:
            self.db_path = path

    def from_interface(self, db: Path) -> bool:
        """O banco foi gerado pela interface (fica em `<out>/grafos/`, ou numa subpasta por
        projeto, `<out>/grafos/<slug>/`)."""
        assert self.out_dir is not None
        graphs = (self.out_dir / GRAPHS_DIR).resolve()
        parent = db.resolve().parent
        return parent == graphs or parent.parent == graphs

    # ----------------------------------------------------------------------- hosts públicos

    def public_host_list(self) -> list[str]:
        with self._lock:
            return list(self.public_hosts)

    def set_public_hosts(self, hosts: Sequence[str]) -> None:
        clean = [h.strip().lower() for h in hosts if h.strip()]
        with self._lock:
            self.public_hosts = list(dict.fromkeys(clean))

    def add_public_host(self, host: str) -> None:
        self.set_public_hosts([*self.public_host_list(), host])

    @property
    def allowed_origins(self) -> frozenset[str]:
        return frozenset(
            {
                f"http://127.0.0.1:{self.port}",
                f"http://localhost:{self.port}",
                f"http://localhost:{DEV_PORT}",
                f"http://127.0.0.1:{DEV_PORT}",
            }
        )

    # ----------------------------------------------------------------------- cruzamento

    def cross_report(self, depth: int = 3) -> tuple[CrossReport | None, str | None]:
        """`(relatório, motivo)` do `cross` entre o projeto ativo e os outros que têm grafo.
        Sem outro grafo, `(None, motivo)`. Em cache por arquivo e data de modificação."""
        with self._lock:
            active = self._get(self._active)
            others = [p for p in self._projects if p.slug != self._active and p.has_graph()]
            db = self.db_path
        if active is None or db is None:
            return None, "o projeto ativo não tem grafo"
        if not others:
            return None, "nenhum outro projeto cadastrado tem grafo"
        paths = [db, *(Path(str(p.db)) for p in others)]
        try:
            key = tuple((str(p), p.stat().st_mtime_ns) for p in paths)
        except OSError as exc:
            return None, f"grafo indisponível: {exc}"
        with self._lock:
            cached = self._cross
        if cached is not None and cached[0] == key and cached[1] == depth:
            return cached[2], cached[3]
        try:
            report: CrossReport | None = cross_projects(paths, depth)
            reason = None
        except PhpMapError as exc:
            report, reason = None, str(exc)
        with self._lock:
            self._cross = (key, depth, report, reason)
        return report, reason

    # ----------------------------------------------------------------------- Docker

    def docker_status(self) -> tuple[bool, str | None]:
        """`php_extractor.check_environment`, em cache por 30 s (chama `docker image inspect`)."""
        with self._lock:
            cached = self._docker
        if cached is not None and time.monotonic() - cached[0] < DOCKER_CACHE_SECONDS:
            return cached[1], cached[2]
        try:
            php_extractor.check_environment(Settings())
            status: tuple[bool, str | None] = (True, None)
        except IngestError as exc:
            status = (False, str(exc))
        with self._lock:
            self._docker = (time.monotonic(), *status)
        return status


def served_db(db: Path) -> Path:
    """O banco a abrir no `php-map ui --db X`: o último gerado pela interface
    (`last_db`), se existe, está em `<out>/grafos/` e é mais novo que `X`; senão, `X`."""
    out = db.parent
    last = UiSettingsStore(out).load().last_db
    if not last:
        return db
    candidate = Path(last)
    try:
        graphs = (out / GRAPHS_DIR).resolve()
        parent = candidate.resolve().parent
        inside = parent == graphs or parent.parent == graphs
        newer = candidate.stat().st_mtime > db.stat().st_mtime
    except OSError:
        return db
    return candidate if inside and newer and candidate.is_file() else db
