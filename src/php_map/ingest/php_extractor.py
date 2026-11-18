"""Wrapper do extrator PHP (php-extractor/) executado em container descartável.

O e-cidade é montado
sempre como somente leitura e os containers do usuário nunca são alterados.
"""

import os
import shutil
import subprocess
import sys
import threading
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path, PurePath
from typing import IO

from rich.progress import Progress

from php_map.config import (
    PHP_IMAGE_ENV,
    PHP_IMAGE_OWN,
    PHP_IMAGE_WINDOWS,
    Settings,
    env_value,
    job_id,
)
from php_map.errors import IngestError
from php_map.ingest.records import PhpFileRecord, parse_record

CONTAINER_EXTRACTOR = "/extractor"
CONTAINER_SOURCE = "/src"
SETUP_HINT = "rode `php-map setup-extractor`"
BUILD_HINT = "rode `php-map setup-extractor --build-image` ou informe `--php-image`"
NO_DOCKER = "Docker não encontrado no PATH (o extrator PHP roda em container)"
# Containers do scan: nome `ecmap-scan-<job>-<lote>` e este rótulo. O cancelamento
# pela interface encerra só os containers com o prefixo do job (`ambiente.md`: pelo nome).
CONTAINER_PREFIX = "ecmap-scan-"
CONTAINER_LABEL = "php-map=scan"
_STDERR_TAIL = 20


def docker_path(path: PurePath) -> str:
    """Caminho do host no formato aceito por `docker -v` (Windows: `D:/...`)."""
    if isinstance(path, Path):
        path = path.resolve()
    return path.as_posix()


def container_name(job: str, batch: int) -> str:
    return f"{CONTAINER_PREFIX}{job}-{batch}"


def image_exists(image: str) -> bool:
    probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False)
    return probe.returncode == 0


def resolve_image(settings: Settings) -> str:
    """Imagem do extrator: `settings.php_image` (`--php-image`), a variável
    `PHP_MAP_PHP_IMAGE` (ou o `.env`), a do pacote Windows do e-cidade se existir,
    senão a do `php-extractor/Dockerfile`. Nenhuma → `IngestError` dizendo como construir."""
    if shutil.which("docker") is None:
        raise IngestError(NO_DOCKER)
    chosen = settings.php_image or env_value(PHP_IMAGE_ENV)
    if chosen:
        if not image_exists(chosen):
            raise IngestError(
                f"imagem Docker '{chosen}' não encontrada ou o Docker não está rodando"
            )
        return chosen
    for image in (PHP_IMAGE_WINDOWS, PHP_IMAGE_OWN):
        if image_exists(image):
            return image
    raise IngestError(
        f"nenhuma imagem do extrator PHP ('{PHP_IMAGE_WINDOWS}' ou '{PHP_IMAGE_OWN}'), "
        f"ou o Docker não está rodando: {BUILD_HINT}"
    )


def with_image(settings: Settings) -> Settings:
    """`settings` com a imagem já escolhida (uma resolução por scan)."""
    return replace(settings, php_image=resolve_image(settings))


def _image(settings: Settings) -> str:
    return settings.php_image or resolve_image(settings)


def _run_as_user() -> list[str]:
    """POSIX: o container do `composer install` roda como o usuário do host, para o `vendor/`
    não ficar com dono root (no Windows o Docker Desktop já mapeia o dono)."""
    if sys.platform == "win32":
        return []
    return ["--user", f"{os.getuid()}:{os.getgid()}", "-e", "COMPOSER_HOME=/tmp/composer"]


def build_image_command(settings: Settings, image: str = PHP_IMAGE_OWN) -> list[str]:
    return ["docker", "build", "-t", image, docker_path(settings.extractor_dir)]


def build_docker_command(
    source: PurePath, settings: Settings, name: str | None = None
) -> list[str]:
    named = ["--name", name] if name else []
    return [
        "docker", "run", "--rm", "-i", *named, "--label", CONTAINER_LABEL,
        "-v", f"{docker_path(settings.extractor_dir)}:{CONTAINER_EXTRACTOR}:ro",
        "-v", f"{docker_path(source)}:{CONTAINER_SOURCE}:ro",
        _image(settings),
        "php", "-d", "short_open_tag=1", "-d", "memory_limit=1G",
        f"{CONTAINER_EXTRACTOR}/bin/extract.php",
        f"--root={CONTAINER_SOURCE}", "--files-from=-",
    ]  # fmt: skip


def build_setup_command(settings: Settings) -> list[str]:
    return [
        "docker", "run", "--rm", *_run_as_user(),
        "-v", f"{docker_path(settings.extractor_dir)}:{CONTAINER_EXTRACTOR}",
        "-w", CONTAINER_EXTRACTOR,
        _image(settings), "composer", "install", "--no-interaction",
    ]  # fmt: skip


def check_environment(settings: Settings, *, require_vendor: bool = True) -> str:
    """Falha cedo, com mensagem clara, se Docker, imagem ou dependências faltarem.
    Devolve a imagem escolhida (`resolve_image`)."""
    image = resolve_image(settings)
    if not (settings.extractor_dir / "bin/extract.php").is_file():
        raise IngestError(f"extrator PHP não encontrado em {settings.extractor_dir}")
    if require_vendor and not (settings.extractor_dir / "vendor/autoload.php").is_file():
        raise IngestError(f"dependências do extrator ausentes: {SETUP_HINT}")
    return image


def build_image(settings: Settings, image: str = PHP_IMAGE_OWN) -> None:
    """`docker build` do `php-extractor/Dockerfile` (PHP 7.4 + composer), para quem não tem a
    imagem do ambiente do e-cidade (Linux, CI)."""
    if shutil.which("docker") is None:
        raise IngestError(NO_DOCKER)
    if not (settings.extractor_dir / "Dockerfile").is_file():
        raise IngestError(f"Dockerfile do extrator não encontrado em {settings.extractor_dir}")
    result = subprocess.run(build_image_command(settings, image), check=False)
    if result.returncode != 0:
        raise IngestError(f"docker build falhou (código {result.returncode})")


def setup_extractor(settings: Settings) -> str:
    """`composer install` do extrator num container descartável (saída vai para o console).
    Devolve a imagem usada."""
    image = check_environment(settings, require_vendor=False)
    settings = replace(settings, php_image=image)
    result = subprocess.run(build_setup_command(settings), check=False)
    if result.returncode != 0:
        raise IngestError(f"composer install falhou (código {result.returncode})")
    return image


def extract(
    source: Path,
    files: Sequence[str],
    settings: Settings,
    cache_path: Path,
    progress: Progress | None = None,
) -> Iterator[PhpFileRecord]:
    """Extrai `files` (relativos a `source`) em lotes; grava cada linha crua em `cache_path`."""
    settings = replace(settings, php_image=check_environment(settings))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    task = progress.add_task("Extraindo AST PHP", total=len(files)) if progress else None
    batch_size = max(1, settings.ast_batch_size)
    job = job_id()
    with cache_path.open("w", encoding="utf-8", newline="\n") as cache:
        for start in range(0, len(files), batch_size):
            batch = files[start : start + batch_size]
            name = container_name(job, start // batch_size + 1)
            for record, raw in _run_batch(source, batch, settings, name):
                cache.write(raw + "\n")
                counts[record.status] += 1
                if progress is not None and task is not None:
                    progress.update(
                        task,
                        advance=1,
                        description="Extraindo AST PHP "
                        + " ".join(f"{k}={v}" for k, v in sorted(counts.items())),
                    )
                yield record


def read_cache(cache_path: Path) -> Iterator[PhpFileRecord]:
    """Relê registros de um JSONL gravado por `extract`."""
    if not cache_path.is_file():
        raise IngestError(f"cache de AST inexistente: {cache_path}")
    with cache_path.open(encoding="utf-8") as cache:
        for number, line in enumerate(cache, start=1):
            if not line.strip():
                continue
            try:
                yield parse_record(line)
            except IngestError as exc:
                raise IngestError(f"{cache_path}:{number}: {exc}") from exc


def _run_batch(
    source: Path, batch: Sequence[str], settings: Settings, name: str | None = None
) -> Iterator[tuple[PhpFileRecord, str]]:
    process = subprocess.Popen(
        build_docker_command(source, settings, name),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin and process.stdout and process.stderr
    stderr_lines: list[str] = []
    # stdin e stderr em threads: escrever a lista inteira antes de ler o stdout pode travar
    # quando o buffer do pipe enche.
    writer = threading.Thread(target=_write_list, args=(process.stdin, batch), daemon=True)
    drainer = threading.Thread(target=_drain, args=(process.stderr, stderr_lines), daemon=True)
    writer.start()
    drainer.start()

    expected = iter(batch)
    finished = False
    try:
        for raw_bytes in process.stdout:
            raw = raw_bytes.decode("utf-8").rstrip("\r\n")
            if not raw:
                continue
            record = parse_record(raw)
            wanted = next(expected, None)
            if record.path != wanted:
                raise IngestError(
                    f"ordem inesperada na saída do extrator: {record.path!r} (esperado {wanted!r})"
                )
            yield record, raw
        finished = True
    finally:
        if not finished:
            # Consumidor parou no meio ou houve erro: sem isso o wait() travaria com o
            # container bloqueado escrevendo no stdout.
            process.kill()
        returncode = process.wait()
        writer.join()
        drainer.join()
    missing = next(expected, None)
    if returncode != 0 or missing is not None:
        tail = "\n".join(stderr_lines[-_STDERR_TAIL:])
        raise IngestError(
            f"extrator falhou (código {returncode}; primeiro arquivo sem registro: {missing!r})"
            f"\n{tail}"
        )


def _write_list(stream: IO[bytes], batch: Sequence[str]) -> None:
    try:
        stream.write("".join(f"{path}\n" for path in batch).encode("utf-8"))
    except BrokenPipeError:
        pass  # o processo morreu; o erro aparece pelo código de saída
    finally:
        stream.close()


def _drain(stream: IO[bytes], lines: list[str]) -> None:
    for raw in stream:
        lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
