<?php
// [fixture] DQL com o verbo SELECT e, para cobrir QUERIES, UPDATES e DELETES, também UPDATE e
// DELETE.
class Fiscal_Model_CadastroDeContas
{
    public function verificar($numeroConta)
    {
        $queryStr = "SELECT c FROM Contabil\ContaContabil c "
                  . "WHERE c.numero_conta = :numero_conta";
        return $queryStr;
    }

    public function encerrar($id)
    {
        $dql = "UPDATE Contabil\ContaContabil c SET c.dt_fim_vigencia = :fim WHERE c.id = :id";
        return $dql;
    }

    public function apagar($id)
    {
        $dql = "DELETE FROM Contabil\ContaContabil c WHERE c.id = :id";
        return $dql;
    }
}
