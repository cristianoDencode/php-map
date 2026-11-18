"""RPC/AJAX do e-cidade: um `Endpoint` por ação de `switch ($oParam->exec)`.

Os `*.RPC.php` despacham por `switch ($oParam->exec) { case 'salvar': … }`. O extrator
(schema_version 7) entrega uma ação por `case` literal, com a linha do `case` e as propriedades
que o ramo lê do objeto de despacho (`$oParam->iCodigo`); aqui cada uma vira
`Endpoint:<sistema>:rpc:<arquivo>#<ação>`. A ação só existe dentro do arquivo, e o nome sozinho se
repetiria entre os 779 arquivos, por isso o arquivo entra no ID.

- Os `case` em cascata (`case 'a': case 'b': corpo`) já chegam do extrator com os mesmos parâmetros.
- O que um método auxiliar lê **fora** do `switch` não pertence a nenhuma ação: só é contado
  (`param_outside_case`), sem inventar vínculo.
- O `File` do RPC continua com `dispatch_actions` (tela de menus e `trace` dependem dele).
"""

from dataclasses import dataclass

from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    endpoint_id,
    file_id,
)
from php_map.ingest.records import PhpFileRecord

RPC_FILE_SUFFIX = ".rpc.php"
RPC_KIND = "rpc_action"


@dataclass(slots=True)
class RpcStats:
    switches: int = 0  # `switch` de despacho vistos
    files: int = 0  # arquivos com pelo menos uma ação
    actions: int = 0  # `Endpoint` (arquivo, ação)
    with_params: int = 0  # ações que leem pelo menos um parâmetro
    params: int = 0  # soma dos parâmetros por ação
    named_files: int = 0  # arquivos `*.RPC.php` escaneados
    named_files_without_action: int = 0  # desses, os sem nenhuma ação literal
    param_outside_case: int = 0  # nomes lidos fora do `switch` (`rpc_param_outside_case`)


def emit_rpc(record: PhpFileRecord, builder: GraphBuilder, system: str, stats: RpcStats) -> None:
    """Cria os endpoints RPC de um arquivo e o `SERVES` do `File` para cada um."""
    named = record.path.lower().endswith(RPC_FILE_SUFFIX)
    stats.named_files += named
    actions: dict[str, tuple[int, list[str]]] = {}
    for dispatch in record.dispatch:
        stats.switches += 1
        stats.param_outside_case += len(dispatch.outside_params)
        for action in dispatch.actions:
            known = actions.get(action.name)
            if known is None:
                actions[action.name] = (action.line, list(action.params))
                continue
            # a mesma ação em dois `switch` do arquivo: um endpoint só, com a união dos parâmetros
            known[1].extend(p for p in action.params if p not in known[1])
    if not actions:
        stats.named_files_without_action += named
        return
    stats.files += 1
    for name, (line, params) in actions.items():
        key = f"{record.path}#{name}"
        eid = endpoint_id(system, "rpc", key)
        declared = [
            {"name": p, "required": False, "type": None, "source": "php_ast"} for p in params
        ]
        builder.add_node(
            Node(
                eid,
                NodeLabel.ENDPOINT,
                {
                    "name": key,
                    "action": name,
                    "system": system,
                    "protocol": "rpc",
                    "kind": RPC_KIND,
                    "params": declared,
                    "path": record.path,
                    "line": line,
                    "wsdl": None,
                },
            )
        )
        evidence = {"path": record.path, "line": line}
        builder.add_edge(
            Edge(file_id(record.path), eid, EdgeType.SERVES, EdgeSource.PHP_AST, 1.0, evidence)
        )
        stats.actions += 1
        stats.with_params += bool(params)
        stats.params += len(params)
