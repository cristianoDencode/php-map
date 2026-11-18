"""Projetos da interface em `<out>/projects.json`.

Um projeto é um sistema mapeado (e-cidade, zend1): pasta, perfil, grafo e a opção de schema. O
arquivo mora ao lado do `ui-settings.json` e usa a mesma gravação atômica.

**O DSN nunca entra aqui.** O projeto guarda `dsn_env`, o **nome** da variável do `.env` que
tem a conexão (`PHP_MAP_CATALOG_DSN`, `SEGUNDO_PROJETO_DSN`); um valor que pareça uma conexão
(`postgresql://…`, `usuario:senha@host`) é recusado antes de qualquer gravação.
"""

import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from php_map import fsperm
from php_map.errors import ConfigError
from php_map.pipeline import validate_source
from php_map.profiles.base import PROFILES, suggest_profile

logger = logging.getLogger(__name__)

PROJECTS_NAME = "projects.json"
FORMAT_VERSION = 1
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class Project:
    slug: str
    name: str
    profile: str
    source: str
    db: str | None = None  # o grafo servido deste projeto (None = ainda sem scan)
    schema_from_db: bool = False
    dsn_env: str | None = None  # NOME da variável do `.env`, nunca o valor
    last_scan: str | None = None

    def graph_path(self) -> Path | None:
        """O arquivo do grafo, se existe em disco."""
        if not self.db:
            return None
        path = Path(self.db)
        return path if path.is_file() else None

    def has_graph(self) -> bool:
        return self.graph_path() is not None


@dataclass(frozen=True, slots=True)
class ProfileSuggestion:
    exists: bool
    profile: str | None  # o único perfil cujo marcador existe na pasta
    markers: tuple[tuple[str, str, bool], ...]  # (perfil, marcador, existe)


def slugify(text: str) -> str:
    """`Sistema Zend` → `sistema-zend` (só `[a-z0-9-]`, sem `-` nas pontas)."""
    return _SLUG_UNSAFE.sub("-", text.lower()).strip("-")[:63] or "projeto"


def check_slug(slug: str) -> str:
    if not _SLUG.match(slug):
        raise ConfigError(
            f"identificador inválido: {slug!r} (use minúsculas, números, '-' e '_', até 63 letras)"
        )
    return slug


def check_dsn_env(value: str | None) -> str | None:
    """Aceita só o NOME de uma variável de ambiente. Um DSN colado no campo é recusado, e a
    mensagem **não** repete o valor (ele pode ter senha)."""
    if value is None or not value.strip():
        return None
    name = value.strip()
    if not _ENV_NAME.match(name):
        raise ConfigError(
            "dsn_env deve ser o NOME da variável do .env (ex.: SEGUNDO_PROJETO_DSN), "
            "nunca a conexão"
        )
    return name


def check_profile(profile: str) -> str:
    if profile not in PROFILES:
        raise ConfigError(
            f"perfil desconhecido: {profile!r} (válidos: {', '.join(sorted(PROFILES))})"
        )
    return profile


def check_source(source: str, profile: str) -> str:
    """Pasta existente com o marcador **do perfil escolhido**; o erro diz qual falta."""
    text = source.strip()
    if not text:
        raise ConfigError("informe a pasta do projeto")
    folder = validate_source(Path(text), profile, hint="")
    return str(folder.resolve())


def suggest(source: str) -> ProfileSuggestion:
    """Qual perfil a pasta parece ser, pelos marcadores. A interface só sugere: quem confirma
    é o usuário."""
    folder = Path(source.strip())
    exists = bool(source.strip()) and folder.is_dir()
    markers = tuple(
        (spec.name, spec.required_marker, exists and (folder / spec.required_marker).is_file())
        for spec in PROFILES.values()
    )
    return ProfileSuggestion(exists, suggest_profile(folder) if exists else None, markers)


def _parse(item: Any) -> Project | None:
    if not isinstance(item, dict):
        return None

    def text(key: str) -> str | None:
        value = item.get(key)
        return value if isinstance(value, str) and value.strip() else None

    slug, name, profile, source = text("slug"), text("name"), text("profile"), text("source")
    if not (slug and profile and source and _SLUG.match(slug) and profile in PROFILES):
        return None
    try:
        dsn_env = check_dsn_env(text("dsn_env"))
    except ConfigError:
        dsn_env = None  # arquivo editado à mão com um valor que parece conexão: descarta
        logger.warning("projects.json: dsn_env de %s não é um nome de variável; ignorado", slug)
    return Project(
        slug=slug,
        name=name or slug,
        profile=profile,
        source=source,
        db=text("db"),
        schema_from_db=item.get("schema_from_db") is True,
        dsn_env=dsn_env,
        last_scan=text("last_scan"),
    )


class ProjectStore:
    """Leitura e gravação de `<out>/projects.json`, com um lock para as threads do servidor."""

    def __init__(self, out_dir: Path) -> None:
        self.path = out_dir / PROJECTS_NAME
        self._lock = threading.Lock()

    # ----------------------------------------------------------------------- leitura

    def load(self) -> tuple[list[Project], str | None]:
        """`(projetos, slug ativo)`. Arquivo ausente ou inválido → vazio (com aviso no log)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], None
        except (OSError, ValueError) as exc:
            logger.warning("%s inválido, começando do zero: %s", self.path.name, exc)
            return [], None
        if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
            logger.warning("%s sem a lista 'projects', começando do zero", self.path.name)
            return [], None
        projects: list[Project] = []
        for item in data["projects"]:
            project = _parse(item)
            if project is not None and all(p.slug != project.slug for p in projects):
                projects.append(project)
        active = data.get("active")
        return projects, active if isinstance(active, str) else None

    def exists(self) -> bool:
        return self.path.is_file()

    # ----------------------------------------------------------------------- gravação

    def save(self, projects: list[Project], active: str | None) -> None:
        with self._lock:
            self._write(projects, active)

    def _write(self, projects: list[Project], active: str | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        body = {
            "version": FORMAT_VERSION,
            "active": active,
            "projects": [asdict(project) for project in projects],
        }
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        fsperm.secure_file(tmp)
        os.replace(tmp, self.path)


def build_project(
    *,
    slug: str | None,
    name: str,
    profile: str,
    source: str,
    schema_from_db: bool,
    dsn_env: str | None,
    taken: set[str],
) -> Project:
    """Valida os campos de um projeto novo. `slug` omitido sai do nome, sem repetir um
    existente (`sistema`, `sistema-2`…); um `slug` informado e já usado é erro."""
    title = name.strip()
    if not title and not slug:
        raise ConfigError("informe o nome do projeto")
    check_profile(profile)
    folder = check_source(source, profile)
    env = check_dsn_env(dsn_env)
    if slug:
        chosen = check_slug(slug.strip())
        if chosen in taken:
            raise ConfigError(f"já existe um projeto com o identificador {chosen!r}")
    else:
        base = slugify(title)
        chosen, count = base, 2
        while chosen in taken:
            chosen, count = f"{base}-{count}", count + 1
    return Project(chosen, title or chosen, profile, folder, None, schema_from_db, env, None)


def change_project(
    current: Project,
    *,
    name: str | None,
    profile: str | None,
    source: str | None,
    schema_from_db: bool | None,
    dsn_env: str | None,
    clear_dsn_env: bool,
) -> Project:
    """Aplica só os campos informados e revalida a pasta contra o perfil final."""
    new_profile = check_profile(profile) if profile is not None else current.profile
    new_source = current.source
    if source is not None or new_profile != current.profile:
        new_source = check_source(source if source is not None else current.source, new_profile)
    env = current.dsn_env
    if clear_dsn_env:
        env = None
    elif dsn_env is not None:
        env = check_dsn_env(dsn_env)
    use_db = current.schema_from_db if schema_from_db is None else schema_from_db
    title = name.strip() if name is not None and name.strip() else current.name
    return replace(
        current,
        name=title,
        profile=new_profile,
        source=new_source,
        schema_from_db=use_db,
        dsn_env=env,
    )
