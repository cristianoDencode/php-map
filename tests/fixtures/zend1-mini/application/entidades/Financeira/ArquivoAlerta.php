<?php
// [fixture] @Entity + @Table(name=): o caso canônico, confiança 1.0.
// Fora de application/modules/: não deve ganhar BELONGS_TO (arquivo "sem módulo").
namespace Financeira;

/**
 * @Entity
 * @Table(name="arquivo_alerta")
 */
class ArquivoAlerta
{
    /**
     * @var int
     * @Id
     * @Column(type="integer")
     * @GeneratedValue
     */
    protected $id = NULL;

    /**
     * @var int
     * @Column(type="integer")
     */
    protected $id_arquivo = NULL;
}
