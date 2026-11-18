<?php
// [fixture] chamadas a Ecidade::consultar|processar. O nome do método remoto é o 1º argumento:
// literal vira CALLS_REMOTE 1.0; variável vira Endpoint `?` e `dynamic_endpoint`.
class Fiscal_Model_ConsultaEmpresa
{
    public function buscar($cnpj)
    {
        return WebService_Model_Ecidade::consultar('getDadosEmpresa', array($cnpj));
    }

    public function salvar($valores)
    {
        return WebService_Model_Ecidade::processar('DadosPrefeitura', $valores);
    }

    public function porNome($nome, $filtros)
    {
        return WebService_Model_Ecidade::consultar($nome, $filtros);
    }
}
