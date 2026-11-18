<?php
// [fixture] @Entity sem @Table: a tabela vem do nome da classe (0.8, doctrine:classname).
namespace Geral;

/**
 * @Entity
 */
class Cliente
{
    /**
     * @Id
     * @Column(type="integer")
     */
    protected $id = NULL;
}
