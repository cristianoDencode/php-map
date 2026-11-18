"""Chamadas: `CALLS` (funções, métodos, estáticos) e `INSTANTIATES`."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from php_map.config import DaoFactory, Settings
from php_map.graph.builder import GraphBuilder
from php_map.graph.field_writes import FieldWritesCollector, PendingWrite
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    class_id,
    file_id,
    function_id,
    method_id,
)
from php_map.ingest.records import Call, Origin, PhpFileRecord
from php_map.resolve.includes import IncludeGraph
from php_map.resolve.symbols import Resolution, SymbolIndex, symbol_edge
from php_map.resolve.unresolved import UnresolvedWriter


def origin_id(path: str, scope: str) -> str:
    """Nó de onde parte a aresta: `File` no topo do arquivo, senão `Function`/`Method`."""
    kind, _, name = scope.partition(":")
    if kind == "function" and name:
        return function_id(name)
    if kind == "method" and "::" in name:
        cls, _, method = name.partition("::")
        return method_id(cls, method)
    return file_id(path)


def scope_class(scope: str) -> str | None:
    """Classe do escopo `method:<classe>::<m>`; `None` fora de métodos."""
    kind, _, name = scope.partition(":")
    return name.partition("::")[0] if kind == "method" and "::" in name else None


@dataclass(frozen=True, slots=True)
class TypedReceiver:
    """Tipo inferido para um receptor e de onde a inferência veio (vai para `evidence`)."""

    class_name: str
    evidence: dict[str, object]
    context_path: str | None = None  # arquivo cujo fechamento desempata a classe
    # teto da confiança (concreta deduzida de uma interface: `1/n`, ou 0,5 se é a
    # única, porque `PHP_AST_AMBIGUOUS` não aceita 1,0) e interface sem binding nem concreta.
    max_confidence: float = 1.0
    unbound: bool = False


DI_PREFIX = "di:"


class CallResolver:
    """Resolve as chamadas de um registro por vez (2ª passada), com o índice já completo."""

    def __init__(
        self,
        index: SymbolIndex,
        includes: IncludeGraph,
        builder: GraphBuilder,
        unresolved: UnresolvedWriter,
        stats: Counter[str],
        settings: Settings,
        field_writes: FieldWritesCollector | None = None,
    ) -> None:
        self._index = index
        self._includes = includes
        self._builder = builder
        self._unresolved = unresolved
        self._stats = stats
        self._factories = settings.dao_factories
        # a chamada que grava pelo DAO vira uma pendência de `php_field_writes`.
        self._field_writes = field_writes
        self._write_methods = frozenset(settings.dao_write_methods)
        self._class_resolvers = settings.class_resolvers
        # só mede "convenção previu um caminho que não existe" quando o módulo vem
        # da pasta (perfil `zend1`) — nos resolvedores do `ecidade`, `expected_paths`
        # não é seletivo (sempre prevê 3 sufixos de autoload) e isso encheria `unresolved.jsonl`
        # de pendências novas para toda classe nativa/de vendor, uma regressão fora do escopo.
        self._predict_missing = settings.module_source == "directory"
        # Comparar sem diferenciar caixa: `expected_paths` prevê um caminho a partir
        # de `name`, que já chega em minúsculas (`resolve_class`); os arquivos de
        # verdade (`index.files`) têm a caixa original.
        self._files_lower = (
            {path.lower() for path in index.files} if self._predict_missing else set()
        )
        # Caminho previsto em pasta de terceiros (`library/Zend`...) fica fora do scan de
        # propósito: a convenção acertou, não é `class_not_found` (medido num projeto zend1 em
        # 2026-10-02: sem isto, 788 pendências, quase todas `Zend_*`/`Twitter_*`).
        self._third_party_lower = tuple(
            f"{folder.lower().rstrip('/')}/" for folder in settings.third_party_dirs
        )

    def resolve(self, record: PhpFileRecord) -> None:
        if self._field_writes is not None:
            self._field_writes.add_record(record)
        for call in record.calls:
            self._stats[f"calls_{call.kind}"] += 1
            if call.kind == "func":
                self._function(record.path, call)
            elif call.kind == "new":
                self._new(record.path, call)
            elif call.kind == "static":
                self._static(record.path, call)
                self._factory_call(record.path, call, call.class_)
            else:
                self._method(record.path, call)
                self._factory_call(record.path, call, call.receiver_class)
        for use in record.dynamic:
            self._unresolved.add(use.kind, record.path, use.line, use.snippet)

    def _evidence(self, path: str, call: Call) -> dict[str, object]:
        return {"path": path, "line": call.line}

    # ------------------------------------------------------------------ funções e classes

    def _function(self, path: str, call: Call) -> None:
        resolution = self._index.resolve_function(call.name, path, self._includes)
        if resolution is None:
            self._stats["unknown_function"] += 1  # nativa do PHP ou de vendor: sem nó
            return
        if resolution.ambiguous:
            self._unresolved.add_aggregated(
                "ambiguous_function",
                path,
                call.line,
                f"{call.name}()",
                resolution.candidates,
                hint=resolution.name,
            )
        src = origin_id(path, call.scope)
        dst = function_id(resolution.name)
        self._builder.add_edge(
            symbol_edge(src, dst, EdgeType.CALLS, resolution, self._evidence(path, call))
        )

    def _class(
        self, path: str, call: Call, name: str, from_path: str | None = None
    ) -> Resolution | None:
        resolution = self._index.resolve_class(name, from_path or path, self._includes)
        if resolution is None:
            self._stats["unknown_class"] += 1  # nativa (Exception, stdClass) ou de vendor
            if self._predict_missing:
                self._register_class_not_found(path, call, name)
            return None
        if resolution.ambiguous:
            self._unresolved.add_aggregated(
                "ambiguous_class",
                path,
                call.line,
                name,
                resolution.candidates,
                hint=resolution.name,
            )
        return resolution

    def _register_class_not_found(self, path: str, call: Call, name: str) -> None:
        """A convenção previu um caminho e nenhum deles está entre os arquivos
        escaneados — mede quanto a convenção erra."""
        predicted: list[str] = []
        for resolver in self._class_resolvers:
            for candidate in resolver.expected_paths(name):
                lowered = candidate.lower()
                if lowered.startswith(self._third_party_lower):
                    return  # vendor fora do scan por design, não erro da convenção
                if lowered not in self._files_lower and candidate not in predicted:
                    predicted.append(candidate)
        if predicted:
            self._unresolved.add_aggregated(
                "class_not_found", path, call.line, name, candidates=predicted, hint=name.lower()
            )

    def _instantiates(self, path: str, call: Call, name: str, evidence: dict[str, object]) -> bool:
        resolution = self._class(path, call, name)
        if resolution is None:
            return False
        src = origin_id(path, call.scope)
        self._builder.add_edge(
            symbol_edge(src, class_id(resolution.name), EdgeType.INSTANTIATES, resolution, evidence)
        )
        return True

    def _new(self, path: str, call: Call) -> None:
        if call.class_ is not None:
            self._instantiates(path, call, call.class_, self._evidence(path, call))

    def _static(self, path: str, call: Call) -> None:
        if call.class_ is None:
            return
        resolution = self._class(path, call, call.class_)
        if resolution is not None:
            self._call_method(path, call, resolution, self._evidence(path, call))

    # ------------------------------------------------------------------ fábricas de DAO

    def _factory(self, receiver_class: str | None, method: str) -> DaoFactory | None:
        for factory in self._factories:
            if factory.method == method and factory.receiver in ("*", receiver_class):
                return factory
        return None

    @staticmethod
    def _instance_flag(factory: DaoFactory, arg1: bool | None, argc: int) -> bool | None:
        """Se a chamada instancia: literal do 2º argumento, o padrão da fábrica ou `None`."""
        if arg1 is not None:
            return arg1
        return factory.instantiates_by_default if argc < 2 else None

    def _factory_call(self, path: str, call: Call, receiver_class: str | None) -> None:
        """`INSTANTIATES` na chamada da fábrica com 1º argumento literal."""
        factory = self._factory(receiver_class, call.name)
        if factory is None:
            return
        self._stats["dao_factory_calls"] += 1
        if call.arg0 is None:
            self._stats["dao_factory_dynamic"] += 1
            self._unresolved.add(
                "dynamic_call", path, call.line, f"{call.name}(<não literal>)", hint=call.name
            )
            return
        instance = self._instance_flag(factory, call.arg1, call.argc)
        if instance is not True:
            # False: só faz o require; None: 2º argumento não literal, não arriscamos.
            self._stats[
                "dao_factory_no_instance" if instance is False else "dao_factory_maybe"
            ] += 1
            return
        name = factory.class_template.format(arg0=call.arg0.strip().lower())
        evidence = {**self._evidence(path, call), "via": f"dao_factory:{factory.method}"}
        if self._instantiates(path, call, name, evidence):
            self._stats["dao_factory_resolved"] += 1
        else:
            self._stats["dao_factory_unknown_class"] += 1

    def _origin_type(self, origin: Origin | str) -> tuple[str, str | None] | None:
        """Classe de uma origem: `new X`, fábrica com argumento literal ou injeção de
        dependência (parâmetro tipado ou `app(X::class)`). `(classe, via)`."""
        if isinstance(origin, str):  # schema_version 1: já é o nome da classe
            return origin.lower(), None
        if origin.kind == "new":
            return (origin.class_.lower(), None) if origin.class_ else None
        if origin.kind == "param":
            via = "di:constructor" if origin.method == "__construct" else "di:param"
            return (origin.class_.lower(), via) if origin.class_ else None
        if origin.kind == "container":
            return (origin.class_.lower(), "di:container") if origin.class_ else None
        receiver = origin.class_ if origin.kind == "static_call" else origin.receiver_class
        factory = self._factory(receiver, origin.name or "")
        if factory is None or origin.arg0 is None:
            return None
        if self._instance_flag(factory, origin.arg1, origin.argc) is False:
            return None
        name = factory.class_template.format(arg0=origin.arg0.strip().lower())
        return name, f"dao_factory:{factory.method}"

    # ------------------------------------------------------------------ métodos

    def _call_method(
        self,
        path: str,
        call: Call,
        resolution: Resolution,
        evidence: dict[str, object],
        confidence_factor: float = 1.0,
    ) -> None:
        owner = self._index.find_method(resolution.name, call.name)
        if owner is None:
            self._stats["unknown_method"] += 1  # método não declarado: sem aresta nem placeholder
            return
        src = origin_id(path, call.scope)
        dst = method_id(owner, call.name)
        edge = symbol_edge(src, dst, EdgeType.CALLS, resolution, evidence)
        if confidence_factor < 1.0:
            edge = Edge(
                edge.src,
                edge.dst,
                edge.type,
                EdgeSource.PHP_AST_AMBIGUOUS,
                edge.confidence * confidence_factor,
                {**edge.evidence, "candidates": evidence.get("candidates", [])},
            )
        self._builder.add_edge(edge)
        self._note_dao_write(path, call, src, dst, edge.confidence)

    def _note_dao_write(self, path: str, call: Call, src: str, dst: str, confidence: float) -> None:
        """`incluir`/`alterar` de um DAO: guarda a pendência com as atribuições do
        extrator. No topo do arquivo (escopo vazio) guarda mesmo sem atribuição, porque o
        formulário da tela preenche os campos pelo `$_POST`. É o mesmo ponto que decide o DAO."""
        if (
            self._field_writes is None
            or call.kind != "method"
            or call.name not in self._write_methods
            or not (call.sets or call.scope == "")
        ):
            return
        self._field_writes.add_call(
            PendingWrite(
                src, dst, call.name, path, call.scope, call.line, tuple(call.sets), confidence
            )
        )

    def _method(self, path: str, call: Call) -> None:
        if call.receiver_class is not None:
            self._stats["receiver_by_class"] += 1
            resolution = self._class(path, call, call.receiver_class)
            if resolution is not None:
                self._call_method(path, call, resolution, self._evidence(path, call))
            return
        typed: list[TypedReceiver] = []
        how = ""
        if call.receiver_origin is not None:
            typed, how = self._typed_by_origin(path, call, call.receiver_origin), "origin"
        elif call.receiver_property is not None:
            typed, how = self._typed_by_property(path, call, call.receiver_property), "property"
        elif call.scope == "" and call.receiver_var not in (None, "this"):
            assert call.receiver_var is not None
            typed, how = self._typed_by_includers(path, call, call.receiver_var), "includers"
        if typed:
            # Contadores usados por scripts/measure_precision.py.
            self._stats[f"receiver_by_{how}"] += 1
            self._call_typed(path, call, typed)
            return
        self._stats["unknown_receiver"] += 1
        self._unresolved.add_aggregated(
            "unknown_receiver", path, call.line, f"{_receiver_text(call)}->{call.name}()"
        )

    def _call_typed(self, path: str, call: Call, typed: Sequence[TypedReceiver]) -> None:
        """Um tipo → `PHP_AST` 1.0; vários → uma aresta por tipo, `1/n`."""
        factor = 1.0 / len(typed)
        names = sorted(t.class_name for t in typed)
        resolved = False
        for item in typed:
            if item.unbound:
                continue
            resolution = self._class(path, call, item.class_name, from_path=item.context_path)
            if resolution is None:
                continue
            resolved = True
            evidence = {**self._evidence(path, call), **item.evidence}
            if len(typed) > 1:
                evidence["candidates"] = names
            self._call_method(path, call, resolution, evidence, min(factor, item.max_confidence))
        if resolved:
            return
        snippet = f"{_receiver_text(call)}->{call.name}()"
        unbound = sorted(t.class_name for t in typed if t.unbound)
        if unbound:
            # interface/abstrata injetada sem binding literal nem classe concreta.
            self._unresolved.add_aggregated(
                "di_unbound_interface", path, call.line, snippet, hint=unbound[0]
            )
            return
        if all(str(t.evidence.get("via", "")).startswith(DI_PREFIX) for t in typed):
            # tipo injetado de fora do código escaneado (`Request`, vendor). É
            # declarado, então não é pendência; só conta.
            self._stats["di_external_type"] += 1
            return
        # Tipo conhecido, mas a classe não está no índice: a chamada não pode sumir.
        self._unresolved.add_aggregated(
            "unknown_receiver",
            path,
            call.line,
            snippet,
            candidates=names if len(names) > 1 else (),
            hint=names[0],
        )

    def _injected(self, name: str, evidence: dict[str, object], method: str) -> list[TypedReceiver]:
        """Tipo injetado. Classe concreta vale como está; interface ou
        `abstract class`: o binding literal único (`di:bind`, 1,0), senão as classes concretas
        que a estendem/implementam (`1/n`), senão fica sem tipo (`di_unbound_interface`).
        Das concretas, ficam as que declaram (ou herdam) `method`: o objeto real precisa ter o
        método. Se nenhuma tiver, ficam todas (a chamada conta como `unknown_method`)."""
        if not self._index.is_abstract_type(name):
            return [TypedReceiver(name, evidence)]
        bound = self._index.bound_concretes(name)
        if len({concrete for concrete, _, _ in bound}) == 1:
            concrete, where, line = bound[0]
            self._stats["di_bound"] += 1
            return [
                TypedReceiver(
                    concrete,
                    {
                        **evidence,
                        "via": "di:bind",
                        "injected": evidence.get("via"),
                        "abstract": name,
                        "bound_in": f"{where}:{line}",
                    },
                )
            ]
        concretes = self._index.concrete_subtypes(name)
        if not concretes:
            self._stats["di_unbound"] += 1
            return [TypedReceiver(name, evidence, unbound=True)]
        self._stats["di_abstract_candidates"] += 1
        with_method = [c for c in concretes if self._index.find_method(c, method) is not None]
        concretes = with_method or concretes
        ceiling = 1.0 / len(concretes) if len(concretes) > 1 else 0.5
        return [
            TypedReceiver(
                concrete,
                {**evidence, "abstract": name, "candidates": concretes},
                max_confidence=ceiling,
            )
            for concrete in concretes
        ]

    def _typed_by_origin(self, path: str, call: Call, origin: Origin) -> list[TypedReceiver]:
        found = self._origin_type(origin)
        if found is None:
            return []
        name, via = found
        evidence: dict[str, object] = {"via": via} if via else {}
        if via is not None and via.startswith(DI_PREFIX):
            return self._injected(name, evidence, call.name)
        return [TypedReceiver(name, evidence)]

    def _typed_by_property(self, path: str, call: Call, prop: str) -> list[TypedReceiver]:
        """`$this->prop->m()`: tipos atribuídos a `$this->prop` na classe (ou na herança)."""
        owner = scope_class(call.scope)
        if owner is None:
            return []
        by_class: dict[str, TypedReceiver] = {}
        for origin in self._index.property_origins(owner, prop):
            found = self._origin_type(origin)
            if found is None:
                continue
            name, via = found
            if via is not None and via.startswith(DI_PREFIX):
                # `$this->prop = $param` tipado; a via é a injeção (+ a propriedade).
                items = self._injected(name, {"via": via, "property": prop}, call.name)
            else:
                items = [TypedReceiver(name, {"via": f"property:{prop}"})]
            for item in items:
                by_class.setdefault(item.class_name, item)
        return [by_class[name] for name in sorted(by_class)]

    def _typed_by_includers(self, path: str, call: Call, var: str) -> list[TypedReceiver]:
        """Tela instancia, form usa: tipo de `$var` pelos `var_types` de quem inclui `path`."""
        vias: dict[str, list[str]] = {}
        factory_via: dict[str, str | None] = {}
        for include in self._includes.includers_of(path):
            origin = include.var_types.get(var)
            found = self._origin_type(origin) if origin is not None else None
            if found is None:
                continue
            name, via = found
            sources = vias.setdefault(name, [])
            factory_via.setdefault(name, via)
            if include.src not in sources:
                sources.append(include.src)
        typed = []
        for name in sorted(vias):
            evidence: dict[str, object] = {"via_include": vias[name][0]}
            if factory_via[name]:
                evidence["via"] = factory_via[name]
            typed.append(TypedReceiver(name, evidence, context_path=vias[name][0]))
        return typed


def _receiver_text(call: Call) -> str:
    """Receptor como aparece no código, para o `snippet` das pendências."""
    if call.receiver_property is not None:
        return f"$this->{call.receiver_property}"
    return f"${call.receiver_var}" if call.receiver_var else "<expr>"
