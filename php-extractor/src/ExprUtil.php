<?php

declare(strict_types=1);

namespace PhpMap\Extractor;

use PhpParser\Node;
use PhpParser\Node\Expr;
use PhpParser\Node\Expr\BinaryOp\Concat;
use PhpParser\Node\InterpolatedStringPart;
use PhpParser\Node\Scalar\InterpolatedString;
use PhpParser\Node\Scalar\String_;

/**
 * Utilitários sobre expressões: strings (literais, interpoladas, concatenadas) e includes.
 */
final class ExprUtil
{
    public const VAR_PLACEHOLDER = '{{VAR}}';

    /** Wrappers de include do e-cidade: modification(X) e Modification::getFile(X). */
    public static function unwrapModification(Node\Expr $expr, bool &$wrapped): Node\Expr
    {
        while (true) {
            if ($expr instanceof Expr\FuncCall
                && $expr->name instanceof Node\Name
                && $expr->name->toLowerString() === 'modification'
                && count($expr->args) === 1
                && $expr->args[0] instanceof Node\Arg
            ) {
                $expr = $expr->args[0]->value;
                $wrapped = true;
                continue;
            }
            if ($expr instanceof Expr\StaticCall
                && $expr->class instanceof Node\Name
                && strtolower(ltrim($expr->class->toString(), '\\')) === 'modification'
                && $expr->name instanceof Node\Identifier
                && $expr->name->toLowerString() === 'getfile'
                && count($expr->args) === 1
                && $expr->args[0] instanceof Node\Arg
            ) {
                $expr = $expr->args[0]->value;
                $wrapped = true;
                continue;
            }
            return $expr;
        }
    }

    /**
     * Classifica o alvo de um include.
     *
     * @return array{target: ?string, expr_kind: string, hint: ?string}
     */
    public static function includeTarget(Node\Expr $expr): array
    {
        $wrapped = false;
        $expr = self::unwrapModification($expr, $wrapped);

        if ($expr instanceof String_) {
            return ['target' => $expr->value, 'expr_kind' => $wrapped ? 'modification' : 'literal', 'hint' => null];
        }

        if ($expr instanceof InterpolatedString) {
            return ['target' => null, 'expr_kind' => 'interpolated', 'hint' => self::literalSuffix(self::parts($expr))];
        }

        if ($expr instanceof Concat) {
            $parts = self::concatParts($expr);
            // Remove o prefixo ECIDADE_PATH . (raiz do e-cidade)
            if ($parts !== [] && $parts[0] instanceof Expr\ConstFetch
                && strtoupper($parts[0]->name->toString()) === 'ECIDADE_PATH'
            ) {
                array_shift($parts);
            }
            $flat = [];
            foreach ($parts as $part) {
                if ($part instanceof InterpolatedString) {
                    foreach (self::parts($part) as $p) {
                        $flat[] = $p;
                    }
                } else {
                    $flat[] = $part;
                }
            }
            $literal = '';
            foreach ($flat as $part) {
                if ($part instanceof String_ || $part instanceof InterpolatedStringPart) {
                    $literal .= $part->value;
                } else {
                    $literal = null;
                    break;
                }
            }
            if ($literal !== null) {
                return ['target' => $literal, 'expr_kind' => $wrapped ? 'modification' : 'literal', 'hint' => null];
            }
            return ['target' => null, 'expr_kind' => 'concat', 'hint' => self::literalSuffix($flat)];
        }

        return ['target' => null, 'expr_kind' => 'dynamic', 'hint' => null];
    }

    /** @return list<Node\Expr> partes de uma concatenação, da esquerda para a direita. */
    public static function concatParts(Concat $concat): array
    {
        $out = [];
        $stack = [$concat->right, $concat->left];
        while ($stack !== []) {
            $node = array_pop($stack);
            if ($node instanceof Concat) {
                $stack[] = $node->right;
                $stack[] = $node->left;
            } else {
                $out[] = $node;
            }
        }
        return $out;
    }

    /** @return list<Node> partes de uma string interpolada. */
    public static function parts(InterpolatedString $string): array
    {
        return array_values($string->parts);
    }

    /** Maior sufixo formado só por partes literais. @param list<Node> $parts */
    public static function literalSuffix(array $parts): ?string
    {
        $suffix = '';
        for ($i = count($parts) - 1; $i >= 0; $i--) {
            $part = $parts[$i];
            if ($part instanceof String_ || $part instanceof InterpolatedStringPart) {
                $suffix = $part->value . $suffix;
            } else {
                break;
            }
        }
        return $suffix === '' ? null : $suffix;
    }

    /** Expressão "de texto": literal, interpolada ou concatenação. */
    public static function isStringy(Node $node): bool
    {
        return $node instanceof String_ || $node instanceof InterpolatedString || $node instanceof Concat;
    }

    /**
     * Texto de uma expressão, com partes não literais trocadas por {{VAR}}.
     *
     * @return array{text: string, complete: bool, has_literal: bool}
     */
    public static function render(Node $node): array
    {
        $text = '';
        $complete = true;
        $hasLiteral = false;
        $stack = [$node];
        while ($stack !== []) {
            $current = array_pop($stack);
            if ($current instanceof Concat) {
                $stack[] = $current->right;
                $stack[] = $current->left;
            } elseif ($current instanceof InterpolatedString) {
                for ($i = count($current->parts) - 1; $i >= 0; $i--) {
                    $stack[] = $current->parts[$i];
                }
            } elseif ($current instanceof String_ || $current instanceof InterpolatedStringPart) {
                $text .= $current->value;
                $hasLiteral = $hasLiteral || $current->value !== '';
            } else {
                $text .= self::VAR_PLACEHOLDER;
                $complete = false;
            }
        }
        return ['text' => $text, 'complete' => $complete, 'has_literal' => $hasLiteral];
    }

    /**
     * `spl_object_id` das partes literais de uma expressão de texto (as mesmas que `render` usa):
     * quando ela vira SQL, esses literais não são texto para pessoas.
     *
     * @return list<int>
     */
    public static function literalIds(Node $node): array
    {
        $ids = [];
        $stack = [$node];
        while ($stack !== []) {
            $current = array_pop($stack);
            if ($current instanceof Concat) {
                $stack[] = $current->right;
                $stack[] = $current->left;
            } elseif ($current instanceof InterpolatedString) {
                foreach ($current->parts as $part) {
                    $stack[] = $part;
                }
            } elseif ($current instanceof String_ || $current instanceof InterpolatedStringPart) {
                $ids[] = spl_object_id($current);
            }
        }
        return $ids;
    }

    /** SQL começa (após trim) com select|insert|update|delete|with. */
    public static function looksLikeSql(string $text): bool
    {
        return preg_match('/^\s*(select|insert|update|delete|with)\b/i', $text) === 1;
    }

    /**
     * Tipo do valor que o código atribui a um campo do DAO (schema_version 12):
     * `literal`, `null`, `parametro` (variável que é parâmetro da função atual), `variavel`,
     * `chamada` ou `expressao`. Não resolve a variável: é a forma da expressão, nada mais.
     *
     * @param list<string> $params nomes dos parâmetros da função ou método aberto
     */
    public static function valueKind(Expr $expr, array $params): string
    {
        if ($expr instanceof String_ || $expr instanceof Node\Scalar\Int_
            || $expr instanceof Node\Scalar\Float_
        ) {
            return 'literal';
        }
        if (($expr instanceof Expr\UnaryMinus || $expr instanceof Expr\UnaryPlus)
            && ($expr->expr instanceof Node\Scalar\Int_ || $expr->expr instanceof Node\Scalar\Float_)
        ) {
            return 'literal';  // `-1`
        }
        if ($expr instanceof Expr\ConstFetch) {
            $const = $expr->name->toLowerString();
            if ($const === 'null') {
                return 'null';
            }
            return $const === 'true' || $const === 'false' ? 'literal' : 'expressao';
        }
        if ($expr instanceof Expr\Variable) {
            return is_string($expr->name) && in_array($expr->name, $params, true) ? 'parametro' : 'variavel';
        }
        if ($expr instanceof Expr\FuncCall || $expr instanceof Expr\MethodCall
            || $expr instanceof Expr\StaticCall || $expr instanceof Expr\NullsafeMethodCall
            || $expr instanceof Expr\New_
        ) {
            return 'chamada';
        }
        return 'expressao';
    }

    /** Texto cortado em `$max` caracteres sem partir UTF-8; `…` no fim quando cortou. */
    public static function clip(string $text, int $max): string
    {
        if (mb_strlen($text, 'UTF-8') <= $max) {
            return $text;
        }
        return mb_substr($text, 0, $max, 'UTF-8') . '…';
    }
}
