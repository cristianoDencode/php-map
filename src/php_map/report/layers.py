"""Saída em português das camadas de um fonte: uma linha por camada."""

from php_map.analysis.layers import DAO, LAYERS, DownNode, LayerPath, Layers, LayerStep

LAYER_TITLES = {
    "menu": "menu",
    "tela": "tela",
    "js": "JavaScript",
    "rota": "rota",
    "controller": "controller",
    "service": "service",
    "repository": "repository",
    "model": "model",
    "dao": "DAO",
    "tabela": "tabela",
    "outro": "outros",
}
PER_LAYER = 6
PATHS_SHOWN = 10
MEMBERS_SHOWN = 4
NOTE = "A camada é deduzida do nome e do tipo do nó (não é uma aresta do grafo)."


def _step(step: LayerStep) -> str:
    title = LAYER_TITLES.get(step.layer, step.layer)
    return f"{title} {step.name}"


def _down_item(item: DownNode) -> str:
    """`nome (aresta, confiança)`; o DAO diz por onde chegou e o que o fonte chama dele."""
    if item.layer != DAO:
        return f"{item.name} ({item.edge_type}, {item.confidence:.2f})"
    text = item.name
    if item.factory:
        argument = item.name.removeprefix("cl_") if item.name.startswith("cl_") else ""
        text += f" — via {item.factory}('{argument}')" if argument else f" — via {item.factory}"
    if item.members:
        shown = ", ".join(f"{m.name}()" for m in item.members[:MEMBERS_SHOWN])
        more = len(item.members) - MEMBERS_SHOWN
        text += f": {shown}" + (f" +{more}" if more > 0 else "")
    return text


def _path_lines(index: int, path: LayerPath) -> list[str]:
    head = f"{index}. {path.menu}"
    if path.menu_path:
        head += f" — {path.menu_path}"
    head += f"  [distância {path.distance}, confiança {path.confidence:.2f}]"
    chain = " → ".join([*(_step(s) for s in path.steps), "[este fonte]"])
    return [head, f"   {chain}"]


def to_text(result: Layers, per_layer: int = PER_LAYER, paths_shown: int = PATHS_SHOWN) -> str:
    where = result.node.partition(":")[2]
    lines = [
        f"Fonte: {where}",
        f"Camada deste fonte: {LAYER_TITLES.get(result.layer, result.layer)}. {NOTE}",
        f"Confiança mínima das arestas: {result.min_confidence:.2f}",
        "",
    ]
    if result.up:
        lines.append(f"Para cima — {result.up_total} caminho(s) até o menu:")
        for index, path in enumerate(result.up[:paths_shown], start=1):
            lines.extend(_path_lines(index, path))
        hidden = result.up_total - min(len(result.up), paths_shown)
        if hidden > 0:
            lines.append(f"   … e mais {hidden} caminho(s)")
    else:
        lines.append("Para cima — nenhum menu chega neste fonte pelo mapa.")
    if result.up_truncated:
        lines.append("   (a busca para cima bateu no teto de nós: pode haver mais caminhos)")
    lines.append("")
    if not result.down:
        lines.append("Para baixo — este fonte não chama nada que o mapa conheça.")
        return "\n".join(lines) + "\n"
    shown = f", mostrando {len(result.down)}" if len(result.down) < result.down_total else ""
    lines.append(f"Para baixo — {result.down_total} nó(s) alcançados{shown}:")
    by_layer: dict[str, list[str]] = {}
    for item in result.down:
        by_layer.setdefault(item.layer, []).append(_down_item(item))
    for layer in LAYERS:
        names = by_layer.get(layer)
        if not names:
            continue
        total = result.down_by_layer.get(layer, len(names))
        hidden_nodes = total - min(len(names), per_layer)
        if layer == DAO:  # linhas longas: uma por classe
            lines.append(f"  {LAYER_TITLES[layer]} ({total}):")
            lines.extend(f"    {name}" for name in names[:per_layer])
            if hidden_nodes > 0:
                lines.append(f"    … +{hidden_nodes}")
            continue
        extra = f", +{hidden_nodes}" if hidden_nodes > 0 else ""
        lines.append(f"  {LAYER_TITLES[layer]} ({total}): {'; '.join(names[:per_layer])}{extra}")
    if result.down_truncated:
        lines.append("  (a busca para baixo bateu no teto de nós)")
    return "\n".join(lines) + "\n"
