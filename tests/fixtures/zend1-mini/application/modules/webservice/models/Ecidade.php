<?php
// [fixture] o cliente SOAP do e-Cidade: só a casca (`consultar` e `processar` estáticos); as
// chamadas partem dos modelos que o usam.
class WebService_Model_Ecidade
{
    public static function consultar($metodo, $arguments)
    {
        return array();
    }

    public static function processar($metodo, $valores)
    {
        return array();
    }
}
