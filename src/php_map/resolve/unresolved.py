"""Pendências de resolução (`out/unresolved.jsonl`), lidas pela etapa de IA e pelos relatórios."""

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import TextIO

# Tipos gerados pela resolução PHP. O SQL embutido acrescenta `sql_parse_error`/`dynamic_sql`.
PHP_KINDS = frozenset(
    {
        "dynamic_include",
        "unresolved_include",
        "variable_function",
        "call_user_func",
        "variable_method",
        "variable_class",
        "dynamic_call",
        "ambiguous_function",
        "ambiguous_class",
        "unknown_receiver",
        # a convenção do perfil previu um caminho e ele não existe entre os
        # arquivos escaneados (só quando `settings.module_source == "directory"`, zend1).
        "class_not_found",
        # `@Entity` cuja tabela não existe no schema lido do banco (entidade
        # obsoleta) e DQL que cita uma classe fora do mapa de entidades.
        "doctrine_table_missing",
        "dql_entity_unknown",
        # nome de método remoto que não é literal; XML de requisição
        # malformado; `<classe>` do XML que o código escaneado não declara; nome de endpoint
        # repetido entre dois XML.
        "dynamic_endpoint",
        "ws_xml_parse_error",
        "ws_class_missing",
        "ws_endpoint_duplicate",
        # `group(base_path('x'))` para arquivo que não existe; rota cuja URI ou cujo
        # prefixo não se resolve; controller que o código escaneado não declara.
        "route_file_missing",
        "route_dynamic",
        "route_controller_missing",
        # interface/abstrata injetada (construtor, parâmetro ou
        # `app(X::class)`) sem binding literal nem classe concreta; `hint` = a interface.
        "di_unbound_interface",
        # URL literal de um arquivo que monta a base da API (`requestApi`/`v4/api`)
        # e não casa com nenhuma `Route`; `hint` = a URI testada, `snippet` = o literal.
        "http_url_dynamic",
        # model Eloquent cuja tabela (do `$table` ou da convenção) não existe no
        # schema lido do banco; `candidates` = a `Table:` prevista, `hint` = a classe.
        "eloquent_table_missing",
    }
)


@dataclass(slots=True)
class Unresolved:
    kind: str
    path: str
    line: int
    snippet: str = ""
    candidates: list[str] = field(default_factory=list)
    hint: str | None = None
    # Pendências agregadas (uma por arquivo e nome) somam aqui as ocorrências.
    count: int = 1


class UnresolvedWriter:
    """Grava pendências em JSONL (ou guarda em memória quando `sink` é `None`).

    `add` grava na hora. `add_aggregated` junta ocorrências com a mesma chave
    `(kind, path, hint)` e só grava em `close()`: usado onde o volume é enorme
    (`ambiguous_function` de `db_query` em toda DAO, `unknown_receiver`).
    """

    def __init__(self, sink: TextIO | None = None) -> None:
        self._sink = sink
        self._aggregated: dict[tuple[str, str, str | None], Unresolved] = {}
        self.records: list[Unresolved] = []
        self.counts: Counter[str] = Counter()

    def add(
        self,
        kind: str,
        path: str,
        line: int,
        snippet: str = "",
        candidates: Sequence[str] = (),
        hint: str | None = None,
    ) -> None:
        self._emit(Unresolved(kind, path, line, snippet, list(candidates), hint))

    def add_aggregated(
        self,
        kind: str,
        path: str,
        line: int,
        snippet: str = "",
        candidates: Sequence[str] = (),
        hint: str | None = None,
    ) -> None:
        key = (kind, path, hint)
        current = self._aggregated.get(key)
        if current is None:
            self._aggregated[key] = Unresolved(kind, path, line, snippet, list(candidates), hint)
        else:
            current.count += 1

    def close(self) -> None:
        """Grava as pendências agregadas. Pode ser chamado mais de uma vez."""
        aggregated, self._aggregated = self._aggregated, {}
        for record in aggregated.values():
            self._emit(record)
        if self._sink is not None:
            self._sink.flush()

    def _emit(self, record: Unresolved) -> None:
        self.counts[record.kind] += record.count
        if self._sink is None:
            self.records.append(record)
        else:
            self._sink.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    def __enter__(self) -> "UnresolvedWriter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
