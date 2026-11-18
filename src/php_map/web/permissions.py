"""Papéis na API: só o `admin` executa mutações.

Uma extensão do Strawberry olha a operação **antes** de validar e executar: quem não é `admin`
(papel `leitor`) só roda consultas. Negação por padrão: uma mutação nova nasce restrita e só
passa para o leitor se entrar em `READER_MUTATIONS`. A interface esconde os botões, mas quem
decide é este módulo.
"""

from collections.abc import Iterator
from typing import Any

from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    InlineFragmentNode,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
)
from strawberry.extensions import SchemaExtension

from php_map.access import users

ADMIN_ONLY = "ação restrita ao administrador"
FORBIDDEN = "FORBIDDEN"

# Mutações que o leitor pode chamar. Vazio: a troca da própria senha é a rota `/auth/password`.
READER_MUTATIONS: frozenset[str] = frozenset()


def _root_fields(
    selection_set: SelectionSetNode, fragments: dict[str, FragmentDefinitionNode], seen: set[str]
) -> Iterator[str]:
    """Nomes reais (nunca o alias) dos campos da raiz, entrando em fragmentos."""
    for node in selection_set.selections:
        if isinstance(node, FieldNode):
            yield node.name.value
        elif isinstance(node, InlineFragmentNode):
            yield from _root_fields(node.selection_set, fragments, seen)
        elif isinstance(node, FragmentSpreadNode):
            name = node.name.value
            fragment = fragments.get(name)
            if fragment is not None and name not in seen:
                seen.add(name)
                yield from _root_fields(fragment.selection_set, fragments, seen)


class AdminOnlyMutations(SchemaExtension):
    """Recusa, para quem não é administrador, toda operação que não seja consulta e não esteja
    na lista `READER_MUTATIONS`."""

    def on_validate(self) -> Iterator[None]:
        run = self.execution_context
        role = getattr(run.context, "role", users.ADMIN)
        if role != users.ADMIN and (refusal := self._refusal(run)) is not None:
            run.pre_execution_errors = [refusal]
        yield

    @staticmethod
    def _refusal(run: Any) -> GraphQLError | None:
        document = run.graphql_document
        if document is None:
            return None
        fragments = {
            d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)
        }
        for definition in document.definitions:
            if not isinstance(definition, OperationDefinitionNode):
                continue
            if definition.operation == OperationType.QUERY:
                continue
            fields = set(_root_fields(definition.selection_set, fragments, set()))
            if definition.operation != OperationType.MUTATION or any(
                not name.startswith("__") and name not in READER_MUTATIONS for name in fields
            ):
                return GraphQLError(ADMIN_ONLY, extensions={"code": FORBIDDEN})
        return None
