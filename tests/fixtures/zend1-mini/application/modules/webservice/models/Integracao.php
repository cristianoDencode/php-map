<?php
// [fixture] classe servida por SOAP. Só os métodos públicos viram endpoint (o protected não);
// `PrestacaoContas` e `ReceberCertificado` são nomes que o helper de integração do e-Cidade
// chama (`tests/fixtures/ecidade-comunicacao`).
class WebService_Model_Integracao
{
    protected function acessoAutenticado()
    {
        return true;
    }

    public function Acesso($sXml)
    {
        return '';
    }

    public function PrestacaoContas($sXml, $lDetalhar = false)
    {
        return '';
    }

    public function ReceberCertificado(array $aDados)
    {
        return '';
    }
}
