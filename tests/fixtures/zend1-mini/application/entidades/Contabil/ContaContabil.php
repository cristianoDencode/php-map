<?php
// [fixture] entidade com namespace, alvo da DQL de CadastroDeContas.
namespace Contabil;

/**
 * @Entity
 * @Table(name="contas_contabeis")
 */
class ContaContabil
{
    /**
     * @var int
     * @Id
     * @Column(type="integer")
     * @GeneratedValue
     */
    protected $id = NULL;

    /**
     * @var string
     * @Column(type="string")
     */
    protected $numero_conta = NULL;
}
