"""Índice global de símbolos: quem declara cada função, classe e método."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    class_id,
    file_id,
    function_id,
    method_id,
)
from php_map.ingest.records import Declare, Origin, PhpFileRecord
from php_map.profiles.base import ClassResolver
from php_map.resolve.classify import is_infra_file, is_infra_symbol, module_hint
from php_map.resolve.includes import IncludeGraph


@dataclass(frozen=True, slots=True)
class Resolution:
    """Símbolo resolvido. Mais de um candidato = ambíguo (`confidence = 1/n`)."""

    name: str
    candidates: tuple[str, ...]

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1

    @property
    def confidence(self) -> float:
        return 1.0 / len(self.candidates)


@dataclass(frozen=True, slots=True)
class _FileInfo:
    status: str
    lines: int


@dataclass(slots=True)
class _Declaration:
    path: str
    decl: Declare


@dataclass(slots=True)
class SymbolIndex:
    # Desempate de classe por convenção, na ordem: o perfil `ecidade`
    # passa os dois resolvedores que antes estavam fixos aqui (`_by_autoload`); `zend1` passa
    # `()` quando não há convenção de classe.
    class_resolvers: tuple[ClassResolver, ...] = ()
    files: set[str] = field(default_factory=set)
    functions: dict[str, list[str]] = field(default_factory=dict)
    classes: dict[str, list[str]] = field(default_factory=dict)
    methods: dict[str, set[str]] = field(default_factory=dict)
    parents: dict[str, str] = field(default_factory=dict)
    # classe → propriedade → origens de `$this->prop = ...`
    properties: dict[str, dict[str, list[Origin]]] = field(default_factory=dict)
    # tipo abstrato (interface ou `abstract class`), quem estende ou
    # implementa cada tipo e os bindings literais do container (abstrata → concretas).
    abstract_types: set[str] = field(default_factory=set)
    concrete_classes: set[str] = field(default_factory=set)
    subtypes: dict[str, list[str]] = field(default_factory=dict)
    bindings: dict[str, list[tuple[str | None, str, int]]] = field(default_factory=dict)
    _file_info: dict[str, _FileInfo] = field(default_factory=dict)
    _declarations: list[_Declaration] = field(default_factory=list)

    def add_record(self, record: PhpFileRecord) -> None:
        path = record.path
        self.files.add(path)
        self._file_info[path] = _FileInfo(record.status, record.lines)
        for decl in record.declares:
            name = decl.name.lower()
            if decl.kind == "function":
                _append_unique(self.functions.setdefault(name, []), path)
            elif decl.kind == "method":
                if decl.class_ is None:
                    continue
                self.methods.setdefault(decl.class_.lower(), set()).add(name)
            else:
                _append_unique(self.classes.setdefault(name, []), path)
                if decl.extends and decl.kind == "class":
                    self.parents.setdefault(name, decl.extends.lower())
                self._add_type(name, decl)
                for item in decl.property_types:
                    origins = self.properties.setdefault(name, {}).setdefault(item.prop, [])
                    if item.origin not in origins:
                        origins.append(item.origin)
            self._declarations.append(_Declaration(path, decl))
        for binding in record.bindings:
            concrete = binding.concrete.lower() if binding.concrete else None
            items = self.bindings.setdefault(binding.abstract.lower(), [])
            if (concrete, path, binding.line) not in items:
                items.append((concrete, path, binding.line))

    def _add_type(self, name: str, decl: Declare) -> None:
        if decl.kind == "interface" or decl.abstract:
            self.abstract_types.add(name)
        elif decl.kind == "class":
            self.concrete_classes.add(name)
        supers = [decl.extends] if decl.extends and decl.kind == "class" else []
        for parent in [*supers, *decl.implements]:
            _append_unique(self.subtypes.setdefault(parent.lower(), []), name)

    def is_abstract_type(self, name: str) -> bool:
        """Interface ou `abstract class` declarada (em qualquer arquivo) e nunca como concreta."""
        key = name.lower()
        return key in self.abstract_types and key not in self.concrete_classes

    def concrete_subtypes(self, name: str) -> list[str]:
        """Classes concretas que estendem ou implementam `name`, direta ou indiretamente."""
        found: set[str] = set()
        seen = {name.lower()}
        pending = [name.lower()]
        while pending:
            for child in self.subtypes.get(pending.pop(), ()):
                if child in seen:
                    continue
                seen.add(child)
                pending.append(child)
                if child in self.concrete_classes and child not in self.abstract_types:
                    found.add(child)
        return sorted(found)

    def bound_concretes(self, name: str) -> list[tuple[str, str, int]]:
        """Bindings literais de `name` com classe concreta: `(concreta, arquivo, linha)`."""
        return [
            (concrete, path, line)
            for concrete, path, line in self.bindings.get(name.lower(), ())
            if concrete is not None
        ]

    def resolve_function(
        self, name: str, from_path: str, includes: IncludeGraph
    ) -> Resolution | None:
        """Declarante único; senão o único no fechamento de includes; senão ambíguo."""
        key = name.lower()
        declared = self.functions.get(key)
        if not declared:
            return None
        return Resolution(key, _by_closure(declared, from_path, includes))

    def resolve_class(self, name: str, from_path: str, includes: IncludeGraph) -> Resolution | None:
        """Como funções, mas o desempate final segue `self.class_resolvers`."""
        key = name.lower().lstrip("\\")
        declared = self.classes.get(key)
        if not declared:
            return None
        candidates = _by_closure(declared, from_path, includes)
        if len(candidates) > 1:
            candidates = _apply_class_resolvers(key, candidates, self.class_resolvers)
        return Resolution(key, candidates)

    def find_method(self, class_name: str, method: str) -> str | None:
        """Classe que declara `method`, subindo pela herança; `None` se ninguém declara."""
        current: str | None = class_name.lower()
        seen: set[str] = set()
        while current is not None and current not in seen:
            if method in self.methods.get(current, ()):
                return current
            seen.add(current)
            current = self.parents.get(current)
        return None

    def property_origins(self, class_name: str, prop: str) -> list[Origin]:
        """Origens de `$this->prop` na classe ou, se ela não atribui, na mais próxima da herança."""
        current: str | None = class_name.lower()
        seen: set[str] = set()
        prop = prop.lower()
        while current is not None and current not in seen:
            origins = self.properties.get(current, {}).get(prop)
            if origins:
                return origins
            seen.add(current)
            current = self.parents.get(current)
        return []

    def emit_nodes(self, builder: GraphBuilder, settings: Settings) -> None:
        """Nós `File`/`Function`/`Class`/`Method` e arestas `DECLARES`."""
        for path, info in self._file_info.items():
            builder.add_node(
                Node(
                    file_id(path),
                    NodeLabel.FILE,
                    {
                        "path": path,
                        "name": path.rsplit("/", 1)[-1],
                        "parse_status": info.status,
                        "lines": info.lines,
                        "module_hint": module_hint(path),
                        "is_infra": is_infra_file(path, settings),
                    },
                )
            )
        for item in self._declarations:
            self._emit_declaration(item.path, item.decl, builder, settings)

    def _emit_declaration(
        self, path: str, decl: Declare, builder: GraphBuilder, settings: Settings
    ) -> None:
        name = decl.name.lower()
        evidence = {"path": path, "line": decl.line_start}
        attrs = {
            "name": name,
            "path": path,
            "line_start": decl.line_start,
            "line_end": decl.line_end,
        }
        if decl.kind == "method":
            assert decl.class_ is not None
            owner = decl.class_.lower()
            node = method_id(owner, name)
            if node not in builder:  # classe declarada 2x: fica o 1º local
                builder.add_node(
                    Node(node, NodeLabel.METHOD, {**attrs, "class": owner, "is_infra": False})
                )
            builder.add_edge(
                Edge(class_id(owner), node, EdgeType.DECLARES, EdgeSource.PHP_AST, 1.0, evidence)
            )
            return
        if decl.kind == "function":
            node, label, declared = function_id(name), NodeLabel.FUNCTION, self.functions[name]
        else:
            node, label, declared = class_id(name), NodeLabel.CLASS, self.classes[name]
            attrs["kind"] = decl.kind
        if node not in builder:
            extra = {"declared_in": list(declared)} if len(declared) > 1 else {}
            builder.add_node(
                Node(node, label, {**attrs, **extra, "is_infra": is_infra_symbol(name, settings)})
            )
        builder.add_edge(
            Edge(file_id(path), node, EdgeType.DECLARES, EdgeSource.PHP_AST, 1.0, evidence)
        )

    def emit_inheritance(self, builder: GraphBuilder, includes: IncludeGraph) -> None:
        """`EXTENDS`/`IMPLEMENTS` para classes-pai conhecidas no índice."""
        for item in self._declarations:
            decl = item.decl
            if decl.kind == "method" or decl.kind == "function":
                continue
            src = class_id(decl.name)
            targets = [(EdgeType.EXTENDS, decl.extends)] if decl.extends else []
            targets += [(EdgeType.IMPLEMENTS, name) for name in decl.implements]
            for edge_type, parent in targets:
                assert parent is not None
                resolution = self.resolve_class(parent, item.path, includes)
                if resolution is not None:
                    builder.add_edge(
                        symbol_edge(
                            src,
                            class_id(resolution.name),
                            edge_type,
                            resolution,
                            {
                                "path": item.path,
                                "line": decl.line_start,
                            },
                        )
                    )


def symbol_edge(
    src: str,
    dst: str,
    edge_type: EdgeType,
    resolution: Resolution,
    evidence: dict[str, object],
) -> Edge:
    """Aresta para um símbolo resolvido: `PHP_AST` 1.0 ou `PHP_AST_AMBIGUOUS` `1/n`."""
    if resolution.ambiguous:
        evidence = {**evidence, "candidates": list(resolution.candidates)}
        return Edge(
            src, dst, edge_type, EdgeSource.PHP_AST_AMBIGUOUS, resolution.confidence, evidence
        )
    return Edge(
        src,
        dst,
        edge_type,
        EdgeSource.PHP_AST,
        1.0,
        {**evidence, "declared_in": resolution.candidates[0]},
    )


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _by_closure(declared: list[str], from_path: str, includes: IncludeGraph) -> tuple[str, ...]:
    if len(declared) == 1:
        return (declared[0],)
    closure = includes.closure(from_path)
    reachable = tuple(path for path in declared if path in closure)
    return reachable if reachable else tuple(declared)


def _apply_class_resolvers(
    name: str, candidates: tuple[str, ...], resolvers: Sequence[ClassResolver]
) -> tuple[str, ...]:
    """Tenta cada `ClassResolver` em ordem; o 1º que decidir vence.
    Nenhum decidindo, `candidates` volta como veio (ambiguidade original)."""
    for resolver in resolvers:
        choice = resolver.resolve(name, candidates)
        if choice is not None:
            return (choice,)
    return candidates
