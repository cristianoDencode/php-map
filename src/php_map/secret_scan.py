"""Varredura de segredos nos grafos e nos textos mostrados fora da máquina.

Usada antes de abrir o túnel do MCP (`php_map.web.tunnel`) e antes de mostrar um briefing
para um assistente de IA (`php_map.briefing`).

O grafo guarda textos do código para a busca (`search_texts`: literais, comentários, HTML e
strings de JS), atributos de nó e evidências de aresta. Credencial escrita no código pode estar
ali. Cada achado tem um `hash` estável; o arquivo de exceções (`secret-allowlist.txt`) libera
um achado revisado, com justificativa. **O valor nunca é impresso**: só os 4 primeiros
caracteres e `***`.

Uso: `python -m php_map.secret_scan <banco.db> [...] [--allowlist <arquivo>]`
(código 3 se houver achado fora da lista de exceções).
"""

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ALLOWLIST = Path(__file__).resolve().parent / "secret_allowlist.txt"
EXIT_FINDINGS = 3

_KEYWORD = r"(?:password|passwd|senha|secret|token|api[_-]?key|client[_-]?secret|apikey)"
PATTERNS: dict[str, re.Pattern[str]] = {
    # atribuição de valor literal a um nome que sugere segredo: $senha = "abc123", 'token' => '…'
    "atribuicao": re.compile(
        rf"(?i)\b\w*{_KEYWORD}\w*['\"]?\s*(?:=>|=|:)\s*['\"]([^'\"\s$]{{6,}})['\"]"
    ),
    # DSN com senha: password=…, postgresql://user:pass@host (valor começando com `$` é variável
    # PHP interpolada, não segredo)
    "dsn": re.compile(
        r"(?i)(?:\bpassword=([^\s;'\"$][^\s;'\"]{2,})|://[^/\s:@'\"$]+:([^@\s/'\"$]{3,})@)"
    ),
    "chave_privada": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "aws": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # string de alta entropia (≥ 32) na mesma linha de uma palavra-chave; a palavra-chave não
    # pode estar dentro de outra (`secretaria`, `tokenizer`) e o valor não é caminho
    "entropia": re.compile(
        rf"(?i)(?<![a-z]){_KEYWORD}(?![a-z]).{{0,40}}?(?<![\w+/.=-])([A-Za-z0-9+_=-]{{32,}})"
    ),
}
_PREFILTER = re.compile(
    rf"(?i){_KEYWORD}|password=|://[^/\s:@]+:[^@\s/]+@|PRIVATE KEY|\beyJ|\bAKIA"
)
MIN_ENTROPY = 3.5  # bits por caractere: hash hexadecimal fica abaixo de 4, base64 aleatório acima


@dataclass(frozen=True, slots=True)
class Finding:
    pattern: str
    table: str  # search_texts | nodes | edges | meta
    key: str  # onde: arquivo:linha, ID do nó, ID da aresta, chave do meta
    masked: str  # trecho com o valor mascarado
    hash: str


def entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def _random_looking(value: str) -> bool:
    """Token de verdade mistura letras e dígitos e tem entropia alta (nome de classe não)."""
    has_digit = any(c.isdigit() for c in value)
    has_alpha = any(c.isalpha() for c in value)
    return has_digit and has_alpha and entropy(value) >= MIN_ENTROPY


def mask(value: str) -> str:
    return value[:4] + "***"


def _secret_of(name: str, match: re.Match[str]) -> str:
    groups = [g for g in match.groups() if g]
    return groups[0] if groups else match.group(0)


def scan_text(text: str) -> Iterator[tuple[str, str]]:
    """`(padrão, valor)` de cada segredo aparente no texto."""
    if not text or not _PREFILTER.search(text):
        return
    for name, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            value = _secret_of(name, match)
            if name == "entropia" and not _random_looking(value):
                continue
            yield name, value


def _rows(conn: sqlite3.Connection) -> Iterator[tuple[str, str, str]]:
    """`(tabela, chave, texto)` de tudo o que pode carregar texto do código."""
    for file_id, kind, text, line in conn.execute(
        "SELECT file_id, kind, text, line FROM search_texts"
    ):
        yield "search_texts", f"{file_id}:{line} ({kind})", text or ""
    for node_id, attrs in conn.execute("SELECT id, attrs FROM nodes"):
        yield "nodes", node_id, attrs or ""
    for edge_id, src, evidence in conn.execute("SELECT id, src, evidence FROM edges"):
        yield "edges", f"{edge_id} ({src})", evidence or ""
    for key, value in conn.execute("SELECT key, value FROM meta"):
        yield "meta", key, value or ""
    if _has_table(conn, "routine_sources"):  # O texto das funções da base
        for node_id, ddl in conn.execute("SELECT node_id, ddl FROM routine_sources"):
            yield "routine_sources", node_id, ddl or ""


def _excerpt(text: str, value: str) -> str:
    """Trecho de até ~160 caracteres com o valor mascarado. Texto longo (corpo de função de
    centenas de KB): o trecho é o entorno do achado, não o começo do texto."""
    if not value:
        return " ".join(text.split())[:160]
    if len(text) > 400 and value in text:
        at = text.index(value)
        text = text[max(0, at - 60) : at + len(value) + 60]
    return " ".join(text.replace(value, mask(value)).split())[:160]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    found = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))
    return found.fetchone() is not None


def scan(db_path: Path) -> list[Finding]:
    """Achados do banco, sem repetir o mesmo (tabela, chave, valor)."""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    found: dict[str, Finding] = {}
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        if not _has_table(conn, "search_texts"):
            conn.execute("CREATE TEMP TABLE search_texts (file_id, kind, text, line)")
        for table, key, text in _rows(conn):
            for name, value in scan_text(text):
                digest = hashlib.sha256(f"{table}|{key}|{value}".encode()).hexdigest()[:16]
                if digest not in found:
                    excerpt = _excerpt(text, value)
                    found[digest] = Finding(name, table, key, excerpt, digest)
    return sorted(found.values(), key=lambda f: (f.pattern, f.table, f.key))


def load_allowlist(path: Path) -> dict[str, str]:
    """`hash → justificativa`. Linhas `hash  # justificativa`; `#` sozinho é comentário."""
    allowed: dict[str, str] = {}
    if not path.is_file():
        return allowed
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, reason = line.partition("#")
        digest = digest.strip()
        if not reason.strip():
            raise ValueError(f"exceção sem justificativa em {path.name}: {digest}")
        allowed[digest] = reason.strip()
    return allowed


def open_findings(findings: Iterable[Finding], allowlist: dict[str, str]) -> list[Finding]:
    return [f for f in findings if f.hash not in allowlist]


def report(db: Path, findings: list[Finding], opened: list[Finding]) -> str:
    counts = Counter(f.pattern for f in findings)
    lines = [
        f"{db}: {len(findings)} achado(s), {len(opened)} sem revisão"
        + (f" ({', '.join(f'{k} {v}' for k, v in sorted(counts.items()))})" if counts else "")
    ]
    for f in opened:
        lines.append(f"  {f.hash}  [{f.pattern}] {f.table} {f.key}: {f.masked}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dbs", nargs="+", type=Path)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--json", action="store_true", help="Contagem por padrão em JSON.")
    args = parser.parse_args(argv)
    allowlist = load_allowlist(args.allowlist)
    total_open = 0
    summary: dict[str, dict[str, int]] = {}
    for db in args.dbs:
        findings = scan(db)
        opened = open_findings(findings, allowlist)
        total_open += len(opened)
        summary[str(db)] = dict(Counter(f.pattern for f in findings))
        if not args.json:
            print(report(db, findings, opened))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return EXIT_FINDINGS if total_open else 0


if __name__ == "__main__":
    sys.exit(main())
