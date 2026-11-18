<?php

declare(strict_types=1);

namespace PhpMap\Extractor;

use PhpParser\Node;
use PhpParser\Node\Expr;
use PhpParser\Node\InterpolatedStringPart;
use PhpParser\Node\Scalar\String_;
use PhpParser\Node\Stmt;
use PhpParser\NameContext;
use PhpParser\NodeFinder;
use PhpParser\NodeVisitorAbstract;
use PhpParser\PrettyPrinter\Standard as PrettyPrinter;

/**
 * Coleta, numa única travessia, tudo o que o registro JSONL precisa.
 *
 * Deve rodar depois do NameResolver (replaceNodes=true) no mesmo traverser.
 */
final class CollectorVisitor extends NodeVisitorAbstract
{
    private const SQL_FUNCTIONS = ['db_query' => true, 'pg_query' => true, 'pg_exec' => true];
    private const CALL_USER_FUNCS = ['call_user_func' => true, 'call_user_func_array' => true];
    private const DISPATCH_PROPERTIES = ['exec' => true, 'method' => true];
    private const SOAP_CLIENTS = ['soapclient' => true, 'zend_soap_client' => true];
    /** Funções do container do Laravel que devolvem a instância de `X::class`. */
    private const CONTAINER_FUNCS = ['app' => true, 'resolve' => true];
    /** Fachada `App` (`use Illuminate\Support\Facades\App;` ou o alias global `\App`). */
    private const APP_FACADES = ['app' => true, 'illuminate\\support\\facades\\app' => true];
    /** Registro de binding no container: `->bind|singleton|scoped(A::class, B::class)`. */
    private const BIND_METHODS = ['bind' => true, 'singleton' => true, 'scoped' => true];
    private const STRING_REF_RE = '/([A-Za-z0-9_.\/-]+\.php)(\?[^\'"\s]*)?/';
    /** Alvo `*.js` de `<script src>` e de strings (schema_version 10); não `*.json`. */
    private const STRING_REF_JS_RE = '/([A-Za-z0-9_.\/-]+\.js)(?![A-Za-z0-9_])(\?[^\'"\s]*)?/';
    private const INCLUDE_TYPES = [
        Expr\Include_::TYPE_INCLUDE => 'include',
        Expr\Include_::TYPE_INCLUDE_ONCE => 'include_once',
        Expr\Include_::TYPE_REQUIRE => 'require',
        Expr\Include_::TYPE_REQUIRE_ONCE => 'require_once',
    ];
    /**
     * Métodos do DAO que gravam e leem os campos preenchidos no objeto. Precisa bater com
     * `DAO_WRITE_METHODS` do perfil (`profiles/ecidade.py`).
     */
    private const WRITE_METHODS = ['incluir' => true, 'alterar' => true];
    /** Maior texto do valor atribuído; o resto é cortado com `…`. */
    private const VALUE_MAX = 120;
    /** Funções do e-cidade que desenham o campo do formulário; o 1º argumento é o nome. */
    private const FORM_WIDGETS = [
        'db_input' => true, 'db_inputdata' => true, 'db_textarea' => true, 'db_select' => true,
        'db_selectrecord' => true, 'db_selectmultiple' => true, 'db_multiploselect' => true,
    ];
    /** Botões e controles do formulário padrão: não são campos de tabela. */
    private const FORM_IGNORED = [
        'incluir' => true, 'alterar' => true, 'excluir' => true, 'pesquisar' => true, 'db_opcao' => true,
    ];
    /** Atributo: expressão já consumida como SQL (não reavaliar as partes). */
    private const NO_SQL = 'ecmNoSql';
    /** Atributo: string dentro de um include (não é referência JS/HTML). */
    private const NO_REF = 'ecmNoRef';
    private const SNIPPET_MAX = 200;
    private const DOC_MAX = 8192;
    /** Maior valor de `class_props`: um nome de tabela ou de conexão, não um texto. */
    private const CLASS_PROP_MAX = 200;

    /** @var list<array<string, mixed>> */
    public array $declares = [];
    /** @var list<array<string, mixed>> */
    public array $includes = [];
    /** @var list<array<string, mixed>> */
    public array $calls = [];
    /** @var list<array<string, mixed>> */
    public array $dynamic = [];
    /** @var list<array<string, mixed>> */
    public array $sqlStrings = [];
    /** @var list<array<string, mixed>> */
    public array $stringRefs = [];
    /** @var list<array<string, mixed>> */
    public array $dispatch = [];
    /**
     * Trechos de HTML do arquivo, em ordem (schema_version 10): `[texto, linha]`, a
     * matéria-prima do `JsLexer::inlineHttpLiterals` (o `<script>` inline das telas).
     * @var list<array{0: string, 1: int}>
     */
    public array $inlineHtml = [];
    /**
     * Literais com "frase", ainda sem o filtro de SQL: ver `literalTexts()`.
     * @var list<array{text: string, line: int, scope: string, id: int}>
     */
    private array $literals = [];
    /** @var array<int, true> `spl_object_id` dos literais que entraram num SQL emitido */
    private array $sqlLiteralIds = [];

    /** @var list<string> escopos abertos (função/método) */
    private array $scopes = [];
    /** @var list<array{name: string, parent: ?string, decl: ?int}> classes abertas (decl = índice em declares) */
    private array $classes = [];
    /**
     * Origem da última atribuição de cada variável (schema_version 2):
     * `new X`, chamada estática ou chamada de método (as fábricas de DAO são decididas no Python).
     * @var array<string, array<string, array<string, mixed>>> escopo => variável => origin
     */
    private array $varOrigins = [];
    /** @var array<string, array<string, array{text: string, complete: bool, has_literal: bool, line: int, ids: list<int>}>> */
    private array $sqlBuffers = [];
    /** @var array<string, true> */
    private array $refSeen = [];
    private PrettyPrinter $printer;
    /** Impressora dos valores de campo (sem a barra inicial dos nomes qualificados). */
    private ValuePrinter $valuePrinter;
    private SymbolicRoutes $symbolic;
    /**
     * Rotas Laravel do arquivo (schema_version 7): ver `SymbolicRoutes`. Só é montado
     * se o arquivo chama a fachada `Route`.
     * @var list<array<string, mixed>>
     */
    public array $routes = [];
    /**
     * Chamadas que saem do sistema (`new SoapClient`, `curl_*`), com o destino simbólico.
     * @var list<array<string, mixed>>
     */
    public array $outbound = [];
    private bool $sawRoute = false;
    /**
     * Campos de formulário do arquivo (schema_version 12): `{name, widget, line}`, de
     * `db_input('campo', …)` e afins e de `<input|select|textarea name="campo">` no HTML inline.
     * @var list<array{name: string, widget: string, line: int}>
     */
    public array $formFields = [];
    /** @var array<string, true> `nome@linha` já registrados em `formFields` */
    private array $formSeen = [];
    /**
     * Atribuições `$obj->campo = expr` ainda valendo: escopo => variável => campo =>
     * `{field, value, kind, line}`. Reatribuir a variável zera; as chamadas `incluir`/`alterar` leem
     * sem consumir (o mesmo objeto pode gravar de novo, como no PHP).
     * @var array<string, array<string, array<string, array{field: string, value: string, kind: string, line: int}>>>
     */
    private array $propSets = [];
    /** @var array<string, list<string>> escopo => nomes dos parâmetros da função ou método */
    private array $scopeParams = [];
    /** @var list<array{node: Stmt\Switch_, var: string, property: string, index: int}> `switch` de despacho */
    private array $switches = [];
    /**
     * Bindings literais do container (schema_version 9): `abstract`, `concrete`
     * (`null` quando o 2º argumento é closure ou outra expressão) e `line`.
     * @var list<array{abstract: string, concrete: ?string, line: int}>
     */
    public array $bindings = [];
    /**
     * Contexto de nomes do `NameResolver` (o mesmo traverser). Serve para resolver um `X::class`
     * de um filho que o `NameResolver` ainda não visitou (no `enterNode` do pai).
     */
    private ?NameContext $nameContext;

    public function __construct(?NameContext $nameContext = null)
    {
        $this->printer = new PrettyPrinter();
        $this->valuePrinter = new ValuePrinter();
        $this->symbolic = new SymbolicRoutes();
        $this->nameContext = $nameContext;
    }

    public function enterNode(Node $node)
    {
        $noSql = $node->getAttribute(self::NO_SQL, false) === true;
        $noRef = $node->getAttribute(self::NO_REF, false) === true;
        if ($noSql) {
            self::markChildren($node, self::NO_SQL);
        }
        if ($noRef) {
            self::markChildren($node, self::NO_REF);
        }

        if ($node instanceof Stmt\ClassLike) {
            $this->enterClass($node);
        } elseif ($node instanceof Stmt\ClassMethod) {
            $this->enterMethod($node);
        } elseif ($node instanceof Stmt\Function_) {
            $this->enterFunction($node);
        } elseif ($node instanceof Expr\Include_) {
            $this->onInclude($node);
        } elseif ($node instanceof Expr\Assign) {
            $this->onAssign($node);
        } elseif ($node instanceof Expr\AssignOp\Concat) {
            $this->onConcatAssign($node);
        } elseif ($node instanceof Expr\FuncCall) {
            $this->onFuncCall($node);
        } elseif ($node instanceof Expr\New_) {
            $this->onNew($node);
        } elseif ($node instanceof Expr\StaticCall) {
            $this->onStaticCall($node);
        } elseif ($node instanceof Expr\MethodCall || $node instanceof Expr\NullsafeMethodCall) {
            $this->onMethodCall($node);
        } elseif ($node instanceof Stmt\Switch_) {
            $this->onSwitch($node);
        } elseif ($node instanceof Stmt\InlineHTML) {
            $this->collectRefs($node->value, $node->getStartLine(), 'inline_html');
            $this->inlineHtml[] = [$node->value, $node->getStartLine()];
            $this->collectHtmlFormFields($node->value, $node->getStartLine());
        }

        if ($node instanceof String_ || $node instanceof InterpolatedStringPart) {
            if (!$noRef) {
                $this->collectRefs($node->value, $node->getStartLine(), 'php_string');
            }
            $this->collectLiteral($node);
        }
        if (!$noSql && ExprUtil::isStringy($node)) {
            $this->onLooseString($node);
        }
        return null;
    }

    public function leaveNode(Node $node)
    {
        if ($node instanceof Stmt\ClassMethod || $node instanceof Stmt\Function_) {
            $this->flushScope($this->scope());
            array_pop($this->scopes);
        } elseif ($node instanceof Stmt\ClassLike) {
            array_pop($this->classes);
        } elseif ($node instanceof Expr\Assign) {
            $this->onAssignLeave($node);
        }
        return null;
    }

    public function afterTraverse(array $nodes)
    {
        foreach (array_keys($this->sqlBuffers) as $scope) {
            $this->flushScope((string) $scope);
        }
        usort($this->sqlStrings, static function (array $a, array $b): int {
            return $a['line'] <=> $b['line'];
        });
        foreach ($this->switches as $switch) {
            $this->dispatch[$switch['index']]['outside_params'] = $this->outsideReads($nodes, $switch);
        }
        if ($this->sawRoute) {
            $this->routes = $this->symbolic->collect($nodes);
        }
        return null;
    }

    /**
     * Propriedades lidas do objeto de despacho (`$oParam->x`) **fora** do `switch`: o que um
     * método auxiliar lê não pertence a nenhum `case` e não é atribuído a nenhuma ação.
     *
     * @param array<Node> $nodes
     * @param array{node: Stmt\Switch_, var: string, property: string, index: int} $switch
     * @return list<string>
     */
    private function outsideReads(array $nodes, array $switch): array
    {
        $skip = $switch['property'];
        $start = $switch['node']->getStartFilePos();
        $end = $switch['node']->getEndFilePos();
        $names = [];
        foreach ((new NodeFinder())->findInstanceOf($nodes, Expr\PropertyFetch::class) as $fetch) {
            if (!$this->readsObject($fetch, $switch['var'], $skip)) {
                continue;
            }
            if ($start >= 0 && $fetch->getStartFilePos() >= $start && $fetch->getEndFilePos() <= $end) {
                continue; // dentro do `switch`
            }
            $names[$fetch->name->toString()] = true;
        }
        return array_keys($names);
    }

    private function readsObject(Expr\PropertyFetch $fetch, string $var, string $skip): bool
    {
        return $fetch->name instanceof Node\Identifier
            && self::variableName($fetch->var) === $var
            && $fetch->name->toString() !== $skip;
    }

    /**
     * Propriedades lidas de `$var` dentro das instruções de um ramo do `switch`, sem repetir.
     *
     * @param array<Node> $stmts
     * @return list<string>
     */
    private function branchReads(array $stmts, string $var, string $skip): array
    {
        $names = [];
        foreach ((new NodeFinder())->findInstanceOf($stmts, Expr\PropertyFetch::class) as $fetch) {
            if ($this->readsObject($fetch, $var, $skip)) {
                $names[$fetch->name->toString()] = true;
            }
        }
        return array_keys($names);
    }

    // ---------------------------------------------------------------- declarações

    private function enterClass(Stmt\ClassLike $node): void
    {
        $name = $node->namespacedName !== null
            ? $node->namespacedName->toLowerString()
            : ($node->name !== null ? $node->name->toLowerString() : 'class@anonymous');
        $parent = null;
        $implements = [];
        $kind = 'class';
        if ($node instanceof Stmt\Class_) {
            $parent = $node->extends !== null ? self::className($node->extends) : null;
            foreach ($node->implements as $interface) {
                $implements[] = self::className($interface);
            }
        } elseif ($node instanceof Stmt\Interface_) {
            $kind = 'interface';
            // Interfaces só "estendem" outras interfaces: ficam em implements (extends é escalar).
            foreach ($node->extends as $interface) {
                $implements[] = self::className($interface);
            }
        } elseif ($node instanceof Stmt\Trait_) {
            $kind = 'trait';
        }
        if ($node->name === null) {
            $this->classes[] = ['name' => $name, 'parent' => $parent, 'decl' => null];
            return; // classe anônima: sem declaração
        }
        $this->classes[] = ['name' => $name, 'parent' => $parent, 'decl' => count($this->declares)];
        $this->declares[] = [
            'kind' => $kind,
            'name' => $name,
            'class' => null,
            'line_start' => $node->getStartLine(),
            'line_end' => $node->getEndLine(),
            'extends' => $parent,
            'implements' => $implements,
            'property_types' => [],
            // schema_version 9: a injeção tipada com classe abstrata vira as filhas concretas.
            'abstract' => $node instanceof Stmt\Class_ && $node->isAbstract(),
            // schema_version 5: anotações Doctrine ficam no docblock; a leitura é do Python.
            'doc_comment' => self::docText($node),
            'properties' => self::propertyDocs($node),
            // schema_version 11: valores padrão que são string literal (`$table`, `$connection`).
            'class_props' => self::classProps($node),
            // schema_version 11: a caixa original do nome (`name` é minúsculo): a convenção do Eloquent
            // (`RedeSimImporta → rede_sim_importas`) precisa dela.
            'display_name' => $node->name->toString(),
        ];
    }

    /** Docblock bruto do nó, cortado em DOC_MAX bytes sem partir um caractere UTF-8. */
    private static function docText(Node $node): ?string
    {
        $doc = $node->getDocComment();
        if ($doc === null) {
            return null;
        }
        return mb_strcut($doc->getText(), 0, self::DOC_MAX, 'UTF-8');
    }

    /**
     * Propriedades da classe que têm docblock (as sem docblock não carregam anotação e ficam de fora).
     *
     * @return list<array{name: string, line: int, doc_comment: string}>
     */
    private static function propertyDocs(Stmt\ClassLike $node): array
    {
        $properties = [];
        foreach ($node->getProperties() as $property) {
            $text = self::docText($property);
            if ($text === null) {
                continue;
            }
            foreach ($property->props as $item) {
                $properties[] = [
                    'name' => $item->name->toString(),
                    'line' => $property->getStartLine(),
                    'doc_comment' => $text,
                ];
            }
        }
        return $properties;
    }

    /**
     * Propriedades de instância com valor padrão string literal (`protected $table = 'x'`), com ou
     * sem docblock. Só leitura de literal: constante, concatenação e array ficam de fora.
     *
     * @return array<string, string>
     */
    private static function classProps(Stmt\ClassLike $node): array
    {
        $props = [];
        foreach ($node->getProperties() as $property) {
            if ($property->isStatic()) {
                continue;
            }
            foreach ($property->props as $item) {
                $default = $item->default;
                if ($default instanceof String_ && strlen($default->value) <= self::CLASS_PROP_MAX) {
                    $props[$item->name->toString()] = $default->value;
                }
            }
        }
        return $props;
    }

    private function enterMethod(Stmt\ClassMethod $node): void
    {
        $class = $this->currentClass() ?? 'class@anonymous';
        $name = $node->name->toLowerString();
        $this->declares[] = [
            'kind' => 'method',
            'name' => $name,
            'class' => $class,
            'line_start' => $node->getStartLine(),
            'line_end' => $node->getEndLine(),
            'extends' => null,
            'implements' => [],
            'property_types' => [],
            // schema_version 6: quem serve SOAP precisa do método público, da caixa
            // original (`name` é minúsculo) e da assinatura. `params` só nos públicos.
            'visibility' => $node->isPrivate() ? 'private' : ($node->isProtected() ? 'protected' : 'public'),
            'display_name' => $node->name->toString(),
            'params' => $node->isPublic() ? self::paramInfo($node) : [],
        ];
        $this->scopes[] = "method:{$class}::{$name}";
        $this->seedParams($node->params, $name);
    }

    /**
     * Parâmetro tipado com classe é a origem da variável desde o início do corpo:
     * `{kind: param, class, name, method}`. Vale para todos os métodos e funções (o `params` do
     * `Declare` continua só nos públicos). Tipo escalar (`int`, `array`) é `Identifier` e fica de
     * fora; o `NameResolver` já resolveu a assinatura ao entrar no método, então o tipo é FQCN.
     * Reatribuir a variável troca a origem, como antes (`onAssignLeave`).
     *
     * @param list<Node\Param> $params
     */
    private function seedParams(array $params, ?string $method): void
    {
        $scope = $this->scope();
        $this->varOrigins[$scope] = [];
        $this->propSets[$scope] = [];
        $this->scopeParams[$scope] = [];
        foreach ($params as $param) {
            $var = $param->var;
            if ($var instanceof Expr\Variable && is_string($var->name)) {
                $this->scopeParams[$scope][] = $var->name;
            }
            $type = $param->type instanceof Node\NullableType ? $param->type->type : $param->type;
            if (!$var instanceof Expr\Variable || !is_string($var->name) || !$type instanceof Node\Name
                || $param->variadic
            ) {
                continue;
            }
            $class = $this->resolveClassRef($type);
            if ($class === null) {
                continue;
            }
            $this->varOrigins[$scope][$var->name] = [
                'kind' => 'param', 'class' => $class, 'name' => $var->name, 'method' => $method,
            ];
        }
    }

    /**
     * Assinatura do método: nome, se tem valor padrão e o tipo declarado (texto, ou null).
     *
     * @return list<array{name: string, has_default: bool, type: ?string}>
     */
    private static function paramInfo(Stmt\ClassMethod $node): array
    {
        $params = [];
        foreach ($node->params as $param) {
            $var = $param->var;
            if (!$var instanceof Expr\Variable || !is_string($var->name)) {
                continue;
            }
            $params[] = [
                'name' => $var->name,
                'has_default' => $param->default !== null || $param->variadic,
                'type' => self::typeText($param->type),
            ];
        }
        return $params;
    }

    private static function typeText(?Node $type): ?string
    {
        if ($type === null) {
            return null;
        }
        if ($type instanceof Node\NullableType) {
            return '?' . self::typeText($type->type);
        }
        if ($type instanceof Node\UnionType || $type instanceof Node\IntersectionType) {
            $glue = $type instanceof Node\UnionType ? '|' : '&';
            return implode($glue, array_map(static function (Node $part): string {
                return (string) self::typeText($part);
            }, $type->types));
        }
        if ($type instanceof Node\Name || $type instanceof Node\Identifier) {
            return $type->toString();
        }
        return null;
    }

    private function enterFunction(Stmt\Function_ $node): void
    {
        $name = $node->namespacedName !== null
            ? $node->namespacedName->toLowerString()
            : $node->name->toLowerString();
        $this->declares[] = [
            'kind' => 'function',
            'name' => $name,
            'class' => null,
            'line_start' => $node->getStartLine(),
            'line_end' => $node->getEndLine(),
            'extends' => null,
            'implements' => [],
            'property_types' => [],
        ];
        $this->scopes[] = "function:{$name}";
        $this->seedParams($node->params, null);
    }

    // ---------------------------------------------------------------- includes

    private function onInclude(Expr\Include_ $node): void
    {
        $target = ExprUtil::includeTarget($node->expr);
        $scope = $this->scope();
        $this->includes[] = [
            'type' => self::INCLUDE_TYPES[$node->type] ?? 'include',
            'target' => $target['target'],
            'expr_kind' => $target['expr_kind'],
            'hint' => $target['hint'],
            'line' => $node->getStartLine(),
            'scope' => $scope,
            // Foto das origens do topo do arquivo: a propagação de origens a leva para o arquivo incluído.
            'var_types' => (object) ($scope === '' ? ($this->varOrigins[''] ?? []) : []),
        ];
        $node->expr->setAttribute(self::NO_REF, true);
    }

    // ---------------------------------------------------------------- atribuições e SQL

    /**
     * Origem de `$this->prop = <expr>` e de `$var = <expr>`, registrada ao **sair** do `Assign`.
     * O `NameResolver` resolve o `Name` de um `new X` quando entra no `New_`; no
     * `enterNode` do `Assign` o filho ainda tem o nome cru (`RepoX`, sem o namespace do `use`).
     * Na saída os filhos já foram resolvidos e a origem do lado direito ainda é a anterior
     * (`$x = $x->foo()` lê a origem antiga de `$x`).
     */
    private function onAssignLeave(Expr\Assign $node): void
    {
        $property = $this->thisProperty($node->var);
        if ($property !== null) {
            $this->addPropertyType($property, $this->originOf($node->expr));
            return;
        }
        $var = self::variableName($node->var);
        if ($var === null) {
            $this->recordFieldSet($node);
            return;
        }
        $scope = $this->scope();
        unset($this->propSets[$scope][$var]);  // outro objeto: as atribuições anteriores não valem

        // Atribuição sem origem conhecida invalida a anterior (evita falso positivo).
        $origin = $var === 'this' ? null : $this->originOf($node->expr);
        if ($origin !== null) {
            $this->varOrigins[$scope][$var] = $origin;
        } else {
            unset($this->varOrigins[$scope][$var]);
        }
    }

    /**
     * `$obj->campo = expr`: guarda o valor para as chamadas `incluir`/`alterar` seguintes
     * do mesmo objeto. Só variável simples (não `$this`) e nome de campo literal. Ao sair do `Assign`
     * o `NameResolver` já resolveu os nomes do lado direito.
     */
    private function recordFieldSet(Expr\Assign $node): void
    {
        $target = $node->var;
        if (!$target instanceof Expr\PropertyFetch || !$target->name instanceof Node\Identifier) {
            return;
        }
        $object = self::variableName($target->var);
        if ($object === null || $object === 'this') {
            return;
        }
        $scope = $this->scope();
        $field = strtolower($target->name->toString());
        $this->propSets[$scope][$object][$field] = [
            'field' => $field,
            'value' => ExprUtil::clip($this->valuePrinter->prettyPrintExpr($node->expr), self::VALUE_MAX),
            'kind' => ExprUtil::valueKind($node->expr, $this->scopeParams[$scope] ?? []),
            'line' => $node->getStartLine(),
        ];
    }

    private function onAssign(Expr\Assign $node): void
    {
        if ($this->thisProperty($node->var) !== null) {
            return; // só a origem importa (onAssignLeave)
        }
        $var = self::variableName($node->var);
        if ($var === null) {
            return;
        }
        $scope = $this->scope();

        $this->flushBuffer($scope, $var);
        if (ExprUtil::isStringy($node->expr)) {
            $rendered = ExprUtil::render($node->expr);
            $this->sqlBuffers[$scope][$var] = $rendered + [
                'line' => $node->getStartLine(),
                'ids' => ExprUtil::literalIds($node->expr),
            ];
            $node->expr->setAttribute(self::NO_SQL, true);
        }
    }

    private function onConcatAssign(Expr\AssignOp\Concat $node): void
    {
        $var = self::variableName($node->var);
        if ($var === null) {
            return;
        }
        $scope = $this->scope();
        $rendered = ExprUtil::render($node->expr);
        if (isset($this->sqlBuffers[$scope][$var])) {
            $buffer = &$this->sqlBuffers[$scope][$var];
            $buffer['text'] .= $rendered['text'];
            $buffer['complete'] = $buffer['complete'] && $rendered['complete'];
            $buffer['has_literal'] = $buffer['has_literal'] || $rendered['has_literal'];
            $buffer['ids'] = array_merge($buffer['ids'], ExprUtil::literalIds($node->expr));
            unset($buffer);
        } else {
            $this->sqlBuffers[$scope][$var] = $rendered + [
                'line' => $node->getStartLine(),
                'ids' => ExprUtil::literalIds($node->expr),
            ];
        }
        $node->expr->setAttribute(self::NO_SQL, true);
    }

    private function onLooseString(Node $node): void
    {
        self::markChildren($node, self::NO_SQL);
        $rendered = ExprUtil::render($node);
        $this->emitSql($rendered, $node->getStartLine(), 'other', ExprUtil::literalIds($node));
    }

    /**
     * @param array{text: string, complete: bool, has_literal: bool} $rendered
     * @param list<int> $literalIds partes literais da expressão (saem dos textos se for SQL)
     */
    private function emitSql(array $rendered, int $line, string $via, array $literalIds): void
    {
        if (!$rendered['has_literal'] || !ExprUtil::looksLikeSql($rendered['text'])) {
            return;
        }
        $this->markSql($literalIds);
        $this->sqlStrings[] = [
            'text' => $rendered['text'],
            'complete' => $rendered['complete'],
            'via' => $via,
            'line' => $line,
            'scope' => $this->scope(),
        ];
    }

    private function flushBuffer(string $scope, string $var): void
    {
        if (!isset($this->sqlBuffers[$scope][$var])) {
            return;
        }
        $buffer = $this->sqlBuffers[$scope][$var];
        unset($this->sqlBuffers[$scope][$var]);
        if (!$buffer['has_literal'] || !ExprUtil::looksLikeSql($buffer['text'])) {
            return;
        }
        $this->markSql($buffer['ids']);
        $this->sqlStrings[] = [
            'text' => $buffer['text'],
            'complete' => $buffer['complete'],
            'via' => 'assignment',
            'line' => $buffer['line'],
            'scope' => $scope,
        ];
    }

    private function flushScope(string $scope): void
    {
        foreach (array_keys($this->sqlBuffers[$scope] ?? []) as $var) {
            $this->flushBuffer($scope, (string) $var);
        }
        unset($this->sqlBuffers[$scope]);
    }

    // ---------------------------------------------------------------- chamadas

    /** `curl_init($url)`, `curl_setopt($ch, CURLOPT_URL, $url)` e `curl_setopt_array`. */
    private function onCurl(string $name, Expr\FuncCall $node): void
    {
        if ($name === 'curl_init') {
            // Sem argumento a URL vem depois (`curl_setopt`) ou em outra função: a saída existe
            // do mesmo jeito, com destino desconhecido (`url: null`); o Python descarta a que
            // sobrar se o mesmo escopo informar a URL.
            $first = $node->args[0] ?? null;
            $url = $first instanceof Node\Arg ? $this->symbolic->sym($first->value) : null;
            $this->addOutbound('http', $name, $node->getStartLine(), $url);
        } elseif ($name === 'curl_setopt' || $name === 'curl_setopt_array') {
            $url = $this->symbolic->curlOptUrl($name, $node->args);
            if ($url !== null) {
                $this->addOutbound('http', $name, $node->getStartLine(), $url);
            }
        }
    }

    private function onFuncCall(Expr\FuncCall $node): void
    {
        if (!$node->name instanceof Node\Name) {
            $this->addDynamic('variable_function', $node);
            return;
        }
        $name = strtolower(ltrim($node->name->toString(), '\\'));
        if (isset(self::CALL_USER_FUNCS[$name])) {
            $this->addDynamic('call_user_func', $node);
            return;
        }
        $this->addCall('func', $name, null, null, null, $node->getStartLine());
        $this->onCurl($name, $node);
        if (isset(self::FORM_WIDGETS[$name])) {
            $first = $node->args[0] ?? null;
            if ($first instanceof Node\Arg && $first->value instanceof String_) {
                $this->addFormField(strtolower($first->value->value), $name, $node->getStartLine());
            }
        }

        if (isset(self::SQL_FUNCTIONS[$name])) {
            foreach ($node->args as $arg) {
                if ($arg instanceof Node\Arg && ExprUtil::isStringy($arg->value)
                    && $arg->value->getAttribute(self::NO_SQL, false) !== true
                ) {
                    $this->emitSql(
                        ExprUtil::render($arg->value), $arg->value->getStartLine(), $name,
                        ExprUtil::literalIds($arg->value)
                    );
                    $arg->value->setAttribute(self::NO_SQL, true);
                }
            }
        }
    }

    private function onNew(Expr\New_ $node): void
    {
        if ($node->class instanceof Stmt\Class_) {
            return; // classe anônima
        }
        if (!$node->class instanceof Node\Name) {
            $this->addDynamic('variable_class', $node);
            return;
        }
        $class = $this->resolveClassRef($node->class);
        if ($class !== null) {
            $this->addCall('new', '__construct', $class, null, null, $node->getStartLine());
            if (isset(self::SOAP_CLIENTS[$class])) {
                $this->addOutbound('soap', $class, $node->getStartLine(), $this->symbolic->soapUrl($node->args));
            }
        }
    }

    /**
     * Chamada que sai do sistema (schema_version 7): `kind` = `soap` ou `http`;
     * `url` é o destino simbólico (ver `SymbolicRoutes`) ou `null` se não está no ponto da chamada.
     *
     * @param array<string, mixed>|null $url
     */
    private function addOutbound(string $kind, string $callee, int $line, ?array $url): void
    {
        $this->outbound[] = [
            'kind' => $kind,
            'callee' => $callee,
            'line' => $line,
            'scope' => $this->scope(),
            'url' => $url,
        ];
    }

    private function onStaticCall(Expr\StaticCall $node): void
    {
        if (!$node->class instanceof Node\Name) {
            $this->addDynamic('variable_class', $node);
            return;
        }
        if (!$node->name instanceof Node\Identifier) {
            $this->addDynamic('variable_method', $node);
            return;
        }
        $class = $this->resolveClassRef($node->class);
        if ($class !== null) {
            if (SymbolicRoutes::isRouteClass($class)) {
                $this->sawRoute = true;  // as rotas são montadas em `afterTraverse`
            }
            $this->addCall(
                'static', $node->name->toLowerString(), $class, null, null, $node->getStartLine(),
                self::argInfo($node->args)
            );
        }
    }

    /** @param Expr\MethodCall|Expr\NullsafeMethodCall $node */
    private function onMethodCall(Node $node): void
    {
        if (!$node->name instanceof Node\Identifier) {
            $this->addDynamic('variable_method', $node);
            return;
        }
        if (isset(self::BIND_METHODS[$node->name->toLowerString()])) {
            $this->addBinding($node);
        }
        $receiverVar = self::variableName($node->var);
        $receiverClass = null;
        $origin = null;
        if ($receiverVar === 'this') {
            $receiverClass = $this->currentClass();
        } else {
            $origin = $this->originOf($node->var);
            if ($origin !== null && $origin['kind'] === 'new') {
                $receiverClass = $origin['class'];
            }
        }
        $extra = self::argInfo($node->args) + [
            'receiver_origin' => $origin,
            'receiver_property' => $this->thisProperty($node->var),
        ];
        // O que o código já pôs no objeto antes de gravar pelo DAO.
        $sets = $receiverVar === null || !isset(self::WRITE_METHODS[$node->name->toLowerString()])
            ? [] : array_values($this->propSets[$this->scope()][$receiverVar] ?? []);
        if ($sets !== []) {
            $extra['sets'] = $sets;
        }
        $this->addCall(
            'method', $node->name->toLowerString(), null, $receiverVar, $receiverClass,
            $node->getStartLine(), $extra
        );
    }

    private function addFormField(string $name, string $widget, int $line): void
    {
        if ($name === '' || isset(self::FORM_IGNORED[$name])) {
            return;
        }
        $key = "{$name}@{$line}";
        if (isset($this->formSeen[$key])) {
            return;
        }
        $this->formSeen[$key] = true;
        $this->formFields[] = ['name' => $name, 'widget' => $widget, 'line' => $line];
    }

    /** `<input|select|textarea name="campo">` do HTML inline; só nome literal, sem botões. */
    private function collectHtmlFormFields(string $html, int $startLine): void
    {
        if (stripos($html, 'name') === false
            || preg_match_all('/<(input|select|textarea)\b([^>]*)/i', $html, $tags, PREG_OFFSET_CAPTURE | PREG_SET_ORDER) < 1
        ) {
            return;
        }
        foreach ($tags as $tag) {
            $attrs = $tag[2][0];
            if (preg_match('/\bname\s*=\s*["\']?([A-Za-z_][A-Za-z0-9_]*)/i', $attrs, $name) !== 1) {
                continue;
            }
            if (preg_match('/\btype\s*=\s*["\']?(submit|button)\b/i', $attrs) === 1) {
                continue;
            }
            $offset = $tag[0][1];
            $line = $startLine + ($offset > 0 ? substr_count(substr($html, 0, $offset), "\n") : 0);
            $this->addFormField(strtolower($name[1]), 'html:' . strtolower($tag[1][0]), $line);
        }
    }

    /** @param array<string, mixed> $extra campos só de static/method (schema_version 2) */
    private function addCall(
        string $kind,
        string $name,
        ?string $class,
        ?string $receiverVar,
        ?string $receiverClass,
        int $line,
        array $extra = []
    ): void {
        $this->calls[] = [
            'kind' => $kind,
            'name' => $name,
            'class' => $class,
            'receiver_var' => $receiverVar,
            'receiver_class' => $receiverClass,
            'line' => $line,
            'scope' => $this->scope(),
        ] + $extra;
    }

    // ---------------------------------------------------------------- origens

    /**
     * De onde vem o valor de uma expressão: `new X`, chamada estática ou de método (com os
     * argumentos literais) ou a origem já conhecida de uma variável. `null` se desconhecida.
     * @return array<string, mixed>|null
     */
    private function originOf(Node\Expr $expr): ?array
    {
        $container = $this->containerClass($expr);
        if ($container !== null) {
            return ['kind' => 'container', 'class' => $container];
        }
        if ($expr instanceof Expr\Variable) {
            $var = self::variableName($expr);
            if ($var === null || $var === 'this') {
                return null;
            }
            return $this->varOrigins[$this->scope()][$var] ?? null;
        }
        if ($expr instanceof Expr\New_) {
            if (!$expr->class instanceof Node\Name) {
                return null;
            }
            $class = $this->resolveClassRef($expr->class);
            return $class === null ? null : ['kind' => 'new', 'class' => $class];
        }
        if ($expr instanceof Expr\StaticCall) {
            if (!$expr->class instanceof Node\Name || !$expr->name instanceof Node\Identifier) {
                return null;
            }
            $class = $this->resolveClassRef($expr->class);
            if ($class === null) {
                return null;
            }
            return ['kind' => 'static_call', 'class' => $class, 'name' => $expr->name->toLowerString()]
                + self::argInfo($expr->args);
        }
        if ($expr instanceof Expr\MethodCall || $expr instanceof Expr\NullsafeMethodCall) {
            if (!$expr->name instanceof Node\Identifier) {
                return null;
            }
            $receiver = self::variableName($expr->var);
            return [
                'kind' => 'method_call',
                'receiver' => $receiver,
                'receiver_class' => $receiver === 'this' ? $this->currentClass() : null,
                'name' => $expr->name->toLowerString(),
            ] + self::argInfo($expr->args);
        }
        return null;
    }

    /**
     * Classe pedida ao container do Laravel: `app(X::class)`, `resolve(X::class)`,
     * `App::make(X::class)` e `app()->make(X::class)`. `null` se não é isso ou se o argumento não
     * é um `X::class` literal.
     */
    private function containerClass(Node\Expr $expr): ?string
    {
        if ($expr instanceof Expr\FuncCall && $expr->name instanceof Node\Name
            && isset(self::CONTAINER_FUNCS[self::className($expr->name)])
        ) {
            return $this->classConstArg($expr->args, 0);
        }
        if (!($expr instanceof Expr\StaticCall || $expr instanceof Expr\MethodCall)
            || !$expr->name instanceof Node\Identifier || $expr->name->toLowerString() !== 'make'
        ) {
            return null;
        }
        if ($expr instanceof Expr\StaticCall && $expr->class instanceof Node\Name
            && isset(self::APP_FACADES[self::className($expr->class)])
        ) {
            return $this->classConstArg($expr->args, 0);
        }
        if ($expr instanceof Expr\MethodCall && $expr->var instanceof Expr\FuncCall
            && $expr->var->name instanceof Node\Name && self::className($expr->var->name) === 'app'
            && $expr->var->args === []
        ) {
            return $this->classConstArg($expr->args, 0);
        }
        return null;
    }

    /**
     * `->bind|singleton|scoped(A::class, B::class)`: `concrete` = `null` se não é `X::class`.
     * @param Expr\MethodCall|Expr\NullsafeMethodCall $node
     */
    private function addBinding(Node $node): void
    {
        $abstract = $this->classConstArg($node->args, 0);
        if ($abstract === null) {
            return;
        }
        $this->bindings[] = [
            'abstract' => $abstract,
            // Sem 2º argumento o Laravel liga a classe a ela mesma.
            'concrete' => isset($node->args[1]) ? $this->classConstArg($node->args, 1) : $abstract,
            'line' => $node->getStartLine(),
        ];
    }

    /**
     * Classe de um argumento `X::class` (FQCN, minúsculo). O `NameResolver` só troca o `Name`
     * ao entrar no `ClassConstFetch`; aqui ele pode não ter passado, então resolve pelo contexto.
     *
     * @param array<Node\Arg|Node\VariadicPlaceholder> $args
     */
    private function classConstArg(array $args, int $position): ?string
    {
        $arg = $args[$position] ?? null;
        if (!$arg instanceof Node\Arg || !$arg->value instanceof Expr\ClassConstFetch) {
            return null;
        }
        $fetch = $arg->value;
        if (!$fetch->name instanceof Node\Identifier || $fetch->name->toLowerString() !== 'class'
            || !$fetch->class instanceof Node\Name
        ) {
            return null;
        }
        $name = $fetch->class;
        if (!$name->isFullyQualified() && !$name->isSpecialClassName() && $this->nameContext !== null) {
            $name = $this->nameContext->getResolvedClassName($name);
        }
        return $this->resolveClassRef($name);
    }

    /** Nome da propriedade em `$this->prop` (acesso direto), senão `null`. */
    private function thisProperty(Node\Expr $expr): ?string
    {
        if ($expr instanceof Expr\PropertyFetch && $expr->name instanceof Node\Identifier
            && self::variableName($expr->var) === 'this'
        ) {
            return strtolower($expr->name->toString());
        }
        return null;
    }

    /** @param array<string, mixed>|null $origin */
    private function addPropertyType(string $property, ?array $origin): void
    {
        if ($origin === null || $this->classes === []) {
            return;
        }
        $decl = $this->classes[count($this->classes) - 1]['decl'];
        if ($decl === null) {
            return;
        }
        $entry = ['prop' => $property, 'origin' => $origin];
        if (!in_array($entry, $this->declares[$decl]['property_types'], true)) {
            $this->declares[$decl]['property_types'][] = $entry;
        }
    }

    /**
     * Argumentos que decidem uma fábrica: `arg0` (string literal), `arg1` (bool literal) e `argc`.
     * @param array<Node\Arg|Node\VariadicPlaceholder> $args
     * @return array{arg0: ?string, arg1: ?bool, argc: int}
     */
    private static function argInfo(array $args): array
    {
        $first = $args[0] ?? null;
        $second = $args[1] ?? null;
        $arg1 = null;
        if ($second instanceof Node\Arg && $second->value instanceof Expr\ConstFetch) {
            $const = $second->value->name->toLowerString();
            $arg1 = $const === 'true' ? true : ($const === 'false' ? false : null);
        }
        $arg0 = null;
        if ($first instanceof Node\Arg && $first->value instanceof String_) {
            $arg0 = $first->value->value;
        }
        return ['arg0' => $arg0, 'arg1' => $arg1, 'argc' => count($args)];
    }

    private function addDynamic(string $kind, Node\Expr $node): void
    {
        $snippet = $this->printer->prettyPrintExpr($node);
        if (strlen($snippet) > self::SNIPPET_MAX) {
            $snippet = substr($snippet, 0, self::SNIPPET_MAX) . '...';
        }
        $this->dynamic[] = [
            'kind' => $kind,
            'snippet' => $snippet,
            'line' => $node->getStartLine(),
            'scope' => $this->scope(),
        ];
    }

    // ---------------------------------------------------------------- despacho e referências

    private function onSwitch(Stmt\Switch_ $node): void
    {
        $cond = $node->cond;
        if (!$cond instanceof Expr\PropertyFetch || !$cond->name instanceof Node\Identifier) {
            return;
        }
        $var = self::variableName($cond->var);
        $property = $cond->name->toString();
        if ($var === null || !isset(self::DISPATCH_PROPERTIES[strtolower($property)])) {
            return;
        }
        $cases = [];
        foreach ($node->cases as $case) {
            if ($case->cond instanceof String_ && !in_array($case->cond->value, $cases, true)) {
                $cases[] = $case->cond->value;
            }
        }
        $this->switches[] = [
            'node' => $node,
            'var' => $var,
            'property' => $property,
            'index' => count($this->dispatch),
        ];
        $this->dispatch[] = [
            'subject' => "\${$var}->{$property}",
            'cases' => $cases,
            'line' => $node->getStartLine(),
            'scope' => $this->scope(),
            // schema_version 7: uma ação por `case` literal, com a linha do `case` e as
            // propriedades que o ramo lê do objeto de despacho; `outside_params` vem no fim.
            'actions' => $this->dispatchActions($node, $var, $property),
            'outside_params' => [],
        ];
    }

    /**
     * Ações de um `switch` de despacho. Os `case` em cascata (sem corpo e sem `break`) dividem o
     * corpo do próximo `case` que tem instruções e, por isso, os mesmos parâmetros.
     *
     * @return list<array{name: string, line: int, params: list<string>}>
     */
    private function dispatchActions(Stmt\Switch_ $node, string $var, string $property): array
    {
        $actions = [];
        $seen = [];
        $pending = [];
        foreach ($node->cases as $case) {
            if ($case->cond instanceof String_) {
                $pending[] = [$case->cond->value, $case->getStartLine()];
            }
            if ($case->stmts === []) {
                continue;
            }
            $params = $this->branchReads($case->stmts, $var, $property);
            foreach ($pending as [$label, $line]) {
                if (!isset($seen[$label])) {
                    $seen[$label] = true;
                    $actions[] = ['name' => (string) $label, 'line' => $line, 'params' => $params];
                }
            }
            $pending = [];
        }
        foreach ($pending as [$label, $line]) {  // cascata no fim, sem corpo
            if (!isset($seen[$label])) {
                $seen[$label] = true;
                $actions[] = ['name' => (string) $label, 'line' => $line, 'params' => []];
            }
        }
        return $actions;
    }

    private function collectRefs(string $text, int $startLine, string $origin): void
    {
        foreach ([['.php', self::STRING_REF_RE], ['.js', self::STRING_REF_JS_RE]] as [$needle, $re]) {
            if (stripos($text, $needle) !== false
                && preg_match_all($re, $text, $matches, PREG_OFFSET_CAPTURE) !== false
            ) {
                $this->addRefs($matches[1], $text, $startLine, $origin);
            }
        }
    }

    /** @param list<array{0: string, 1: int}> $found */
    private function addRefs(array $found, string $text, int $startLine, string $origin): void
    {
        foreach ($found as $match) {
            $target = $match[0];
            $line = $startLine + ($match[1] > 0 ? substr_count(substr($text, 0, $match[1]), "\n") : 0);
            $key = "{$target}@{$line}";
            if (isset($this->refSeen[$key])) {
                continue;
            }
            $this->refSeen[$key] = true;
            $this->stringRefs[] = ['target' => $target, 'line' => $line, 'origin' => $origin];
        }
    }

    // ---------------------------------------------------------------- textos

    /** @param String_|InterpolatedStringPart $node */
    private function collectLiteral(Node $node): void
    {
        $text = TextCollector::literalText($node->value);
        if ($text !== null) {
            $this->literals[] = [
                'text' => $text,
                'line' => $node->getStartLine(),
                'scope' => $this->scope(),
                'id' => spl_object_id($node),
            ];
        }
    }

    /** @param list<int> $ids */
    private function markSql(array $ids): void
    {
        foreach ($ids as $id) {
            $this->sqlLiteralIds[$id] = true;
        }
    }

    /**
     * Literais com frase que não fazem parte de um SQL emitido em `sql_strings`.
     * Chamar depois da travessia: os buffers de SQL só são decididos no fim do escopo.
     *
     * @return list<array{kind: string, text: string, line: int, scope: string}>
     */
    public function literalTexts(): array
    {
        $out = [];
        foreach ($this->literals as $literal) {
            if (isset($this->sqlLiteralIds[$literal['id']])) {
                continue;
            }
            $item = TextCollector::item('literal', $literal['text'], $literal['line'], []);
            $item['scope'] = $literal['scope'];
            $out[] = $item;
        }
        return $out;
    }

    // ---------------------------------------------------------------- utilitários

    private function scope(): string
    {
        return $this->scopes === [] ? '' : $this->scopes[count($this->scopes) - 1];
    }

    private function currentClass(): ?string
    {
        return $this->classes === [] ? null : $this->classes[count($this->classes) - 1]['name'];
    }

    private function resolveClassRef(Node\Name $name): ?string
    {
        $lower = $name->toLowerString();
        if ($lower === 'self' || $lower === 'static') {
            return $this->currentClass();
        }
        if ($lower === 'parent') {
            return $this->classes === [] ? null : $this->classes[count($this->classes) - 1]['parent'];
        }
        return self::className($name);
    }

    private static function className(Node\Name $name): string
    {
        return strtolower(ltrim($name->toString(), '\\'));
    }

    private static function variableName(Node $node): ?string
    {
        return $node instanceof Expr\Variable && is_string($node->name) ? $node->name : null;
    }

    private static function markChildren(Node $node, string $attribute): void
    {
        foreach ($node->getSubNodeNames() as $subName) {
            $sub = $node->$subName;
            if ($sub instanceof Node) {
                $sub->setAttribute($attribute, true);
            } elseif (is_array($sub)) {
                foreach ($sub as $item) {
                    if ($item instanceof Node) {
                        $item->setAttribute($attribute, true);
                    }
                }
            }
        }
    }
}
