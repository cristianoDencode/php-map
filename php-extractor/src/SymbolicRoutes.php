<?php

declare(strict_types=1);

namespace PhpMap\Extractor;

use PhpParser\Node;
use PhpParser\Node\Expr;
use PhpParser\Node\Scalar;
use PhpParser\Node\Stmt;
use PhpParser\PrettyPrinter\Standard as PrettyPrinter;

/**
 * Serialização "simbólica" de expressões e das cadeias `Route::…` (schema_version 7).
 *
 * O Python não lê PHP, e o que decide uma rota (prefixo, controller, arquivo incluído) é uma
 * expressão: `Route::prefix('x')->group(base_path('routes/a.php'))`, `$controller . "@index"`.
 * Aqui cada expressão vira um dicionário pequeno (`t` = tipo), que o resolvedor avalia:
 *
 *  - `{t:"s", v}`      texto literal (inclui número);
 *  - `{t:"v", n}`      variável;
 *  - `{t:"c", p:[…]}`  concatenação (inclusive `"{$a}texto"`);
 *  - `{t:"k", v}`      `Classe::class`, com o nome já resolvido;
 *  - `{t:"f", n, a}`   chamada de função (`base_path`, `env`…);
 *  - `{t:"a", i}`      array, com itens `{k, v}`;
 *  - `{t:"fn", b}`     closure, com as rotas do corpo;
 *  - `{t:"null"}`, `{t:"b", v}`, `{t:"const", n}`;
 *  - `{t:"?", s}`      o resto, com um trecho do código (nunca é adivinhado).
 */
final class SymbolicRoutes
{
    private const FACADES = ['route' => true, 'illuminate\\support\\facades\\route' => true];
    private const SNIPPET_MAX = 80;

    private PrettyPrinter $printer;

    public function __construct()
    {
        $this->printer = new PrettyPrinter();
    }

    public static function isRouteClass(string $lowerName): bool
    {
        return isset(self::FACADES[$lowerName]);
    }

    // ------------------------------------------------------------------ rotas

    /**
     * Instruções do arquivo que importam para as rotas, na ordem, achatando classe, método,
     * `if` e laços: `{kind:"assign", var, expr, line}` e `{kind:"route", line, chain}`.
     *
     * @param array<Node> $stmts
     * @return list<array<string, mixed>>
     */
    public function collect(array $stmts): array
    {
        $items = [];
        foreach ($stmts as $stmt) {
            if (!$stmt instanceof Stmt) {
                continue;
            }
            if ($stmt instanceof Stmt\Expression) {
                $expr = $stmt->expr;
                if ($expr instanceof Expr\Assign && $expr->var instanceof Expr\Variable
                    && is_string($expr->var->name)
                ) {
                    $items[] = [
                        'kind' => 'assign',
                        'var' => $expr->var->name,
                        'expr' => $this->sym($expr->expr),
                        'line' => $stmt->getStartLine(),
                    ];
                    continue;
                }
                $chain = $this->routeChain($expr);
                if ($chain !== null) {
                    $items[] = ['kind' => 'route', 'line' => $stmt->getStartLine(), 'chain' => $chain];
                }
                continue;
            }
            foreach ($this->nestedStatements($stmt) as $children) {
                $items = array_merge($items, $this->collect($children));
            }
        }
        return $items;
    }

    /**
     * Cadeia `Route::a(...)->b(...)->c(...)` como lista `{name, args}` na ordem da chamada;
     * `null` se a expressão não nasce numa fachada `Route`.
     *
     * @return list<array{name: string, args: list<array<string, mixed>>}>|null
     */
    private function routeChain(Node $expr): ?array
    {
        $calls = [];
        $node = $expr;
        while ($node instanceof Expr\MethodCall) {
            if (!$node->name instanceof Node\Identifier) {
                return null;
            }
            array_unshift($calls, ['name' => $node->name->toString(), 'args' => $this->args($node->args)]);
            $node = $node->var;
        }
        if (!$node instanceof Expr\StaticCall || !$node->class instanceof Node\Name
            || !$node->name instanceof Node\Identifier
        ) {
            return null;
        }
        if (!self::isRouteClass(strtolower(ltrim($node->class->toString(), '\\')))) {
            return null;
        }
        array_unshift($calls, ['name' => $node->name->toString(), 'args' => $this->args($node->args)]);
        return $calls;
    }

    /**
     * Listas de instruções aninhadas em `$stmt` (corpo de classe, método, `if`/`else`, laços…).
     *
     * @return list<array<Stmt>>
     */
    private function nestedStatements(Node $node): array
    {
        $lists = [];
        foreach ($node->getSubNodeNames() as $name) {
            $sub = $node->$name;
            if (is_array($sub)) {
                $stmts = [];
                foreach ($sub as $item) {
                    if ($item instanceof Stmt\ElseIf_ || $item instanceof Stmt\Catch_ || $item instanceof Stmt\Case_) {
                        $lists = array_merge($lists, $this->nestedStatements($item));
                    } elseif ($item instanceof Stmt) {
                        $stmts[] = $item;
                    }
                }
                if ($stmts !== []) {
                    $lists[] = $stmts;
                }
            } elseif ($sub instanceof Stmt\Else_ || $sub instanceof Stmt\Finally_) {
                $lists = array_merge($lists, $this->nestedStatements($sub));
            }
        }
        return $lists;
    }

    // ------------------------------------------------------------------ expressões

    /**
     * @param array<Node\Arg|Node\VariadicPlaceholder> $args
     * @return list<array<string, mixed>>
     */
    public function args(array $args): array
    {
        $out = [];
        foreach ($args as $arg) {
            if ($arg instanceof Node\Arg) {
                $out[] = $this->sym($arg->value);
            }
        }
        return $out;
    }

    /** @return array<string, mixed> */
    public function sym(Node $e): array
    {
        if ($e instanceof Scalar\String_) {
            return ['t' => 's', 'v' => $e->value];
        }
        if ($e instanceof Scalar\Int_ || $e instanceof Scalar\Float_) {
            return ['t' => 's', 'v' => (string) $e->value];
        }
        if ($e instanceof Scalar\InterpolatedString) {
            $parts = [];
            foreach ($e->parts as $part) {
                $parts[] = $part instanceof Node\InterpolatedStringPart
                    ? ['t' => 's', 'v' => $part->value]
                    : $this->sym($part);
            }
            return ['t' => 'c', 'p' => $parts];
        }
        if ($e instanceof Expr\BinaryOp\Concat) {
            return ['t' => 'c', 'p' => $this->concatParts($e)];
        }
        if ($e instanceof Expr\Variable && is_string($e->name)) {
            return ['t' => 'v', 'n' => $e->name];
        }
        if ($e instanceof Expr\ClassConstFetch && $e->class instanceof Node\Name
            && $e->name instanceof Node\Identifier && strtolower($e->name->toString()) === 'class'
        ) {
            return ['t' => 'k', 'v' => ltrim($e->class->toString(), '\\')];
        }
        if ($e instanceof Expr\FuncCall && $e->name instanceof Node\Name) {
            return [
                't' => 'f',
                'n' => strtolower(ltrim($e->name->toString(), '\\')),
                'a' => $this->args($e->args),
            ];
        }
        if ($e instanceof Expr\Array_) {
            $items = [];
            foreach ($e->items as $item) {
                $items[] = [
                    'k' => $item->key !== null ? $this->sym($item->key) : null,
                    'v' => $this->sym($item->value),
                ];
            }
            return ['t' => 'a', 'i' => $items];
        }
        if ($e instanceof Expr\Closure) {
            return ['t' => 'fn', 'b' => $this->collect($e->stmts)];
        }
        if ($e instanceof Expr\ArrowFunction) {
            return ['t' => 'fn', 'b' => []];
        }
        if ($e instanceof Expr\ConstFetch) {
            $name = strtolower($e->name->toString());
            if ($name === 'null') {
                return ['t' => 'null'];
            }
            if ($name === 'true' || $name === 'false') {
                return ['t' => 'b', 'v' => $name === 'true'];
            }
            return ['t' => 'const', 'n' => $e->name->toString()];
        }
        return ['t' => '?', 's' => substr($this->printer->prettyPrintExpr($e), 0, self::SNIPPET_MAX)];
    }

    /** @return list<array<string, mixed>> */
    private function concatParts(Expr\BinaryOp\Concat $e): array
    {
        $parts = [];
        foreach ([$e->left, $e->right] as $side) {
            if ($side instanceof Expr\BinaryOp\Concat) {
                $parts = array_merge($parts, $this->concatParts($side));
            } else {
                $parts[] = $this->sym($side);
            }
        }
        return $parts;
    }

    // ------------------------------------------------------------------ chamadas de saída

    /**
     * Destino de um `new SoapClient(...)`: o 1º argumento (WSDL) ou, no modo sem WSDL
     * (`null`), a chave `location` das opções.
     *
     * @param array<Node\Arg|Node\VariadicPlaceholder> $args
     * @return array<string, mixed>|null
     */
    public function soapUrl(array $args): ?array
    {
        $first = $args[0] ?? null;
        if ($first instanceof Node\Arg && !self::isNull($first->value)) {
            return $this->sym($first->value);
        }
        $options = $args[1] ?? null;
        if ($options instanceof Node\Arg && $options->value instanceof Expr\Array_) {
            foreach ($options->value->items as $item) {
                if ($item->key instanceof Scalar\String_ && strtolower($item->key->value) === 'location') {
                    return $this->sym($item->value);
                }
            }
            return null;
        }
        // `new SoapClient(null, $aOptions)`: o destino está nas opções, numa variável; ela é a pista.
        return $options instanceof Node\Arg ? $this->sym($options->value) : null;
    }

    /**
     * Destino de `curl_setopt($ch, CURLOPT_URL, $v)` ou de `curl_setopt_array($ch, [CURLOPT_URL => $v])`.
     *
     * @param array<Node\Arg|Node\VariadicPlaceholder> $args
     * @return array<string, mixed>|null
     */
    public function curlOptUrl(string $function, array $args): ?array
    {
        if ($function === 'curl_setopt') {
            $option = $args[1] ?? null;
            $value = $args[2] ?? null;
            if ($option instanceof Node\Arg && self::isUrlOption($option->value) && $value instanceof Node\Arg) {
                return $this->sym($value->value);
            }
            return null;
        }
        $options = $args[1] ?? null;
        if ($options instanceof Node\Arg && $options->value instanceof Expr\Array_) {
            foreach ($options->value->items as $item) {
                if ($item->key !== null && self::isUrlOption($item->key)) {
                    return $this->sym($item->value);
                }
            }
        }
        return null;
    }

    private static function isUrlOption(Node $node): bool
    {
        return $node instanceof Expr\ConstFetch && strtolower($node->name->toString()) === 'curlopt_url';
    }

    private static function isNull(Node $node): bool
    {
        return $node instanceof Expr\ConstFetch && strtolower($node->name->toString()) === 'null';
    }
}
