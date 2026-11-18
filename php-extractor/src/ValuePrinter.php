<?php

declare(strict_types=1);

namespace PhpMap\Extractor;

use PhpParser\Node\Name;
use PhpParser\PrettyPrinter\Standard;

/**
 * Imprime o valor atribuído a um campo do DAO como o programador escreveu.
 *
 * O `NameResolver` troca `date(...)` e `null` por `\date(...)` e `\null`; para quem lê o valor no
 * dossiê isso é ruído. Esta impressora tira a barra inicial dos nomes totalmente qualificados.
 */
final class ValuePrinter extends Standard
{
    protected function pName_FullyQualified(Name\FullyQualified $node): string
    {
        return implode('\\', $node->getParts());
    }
}
