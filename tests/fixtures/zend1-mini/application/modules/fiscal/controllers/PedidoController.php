<?php
// [fixture] Regra 1 (sufixo Controller -> controllers/) + EXTENDS pela pasta library/ do próprio
// módulo (não é library/ de terceiros: a exclusão de library/ não deve apagar este caso).
class Fiscal_PedidoController extends Fiscal_Lib_Controller_AbstractController
{
    public function consultaAction()
    {
        // consultaAction não tem views/scripts/pedido/consulta.phtml: RENDERS não deve existir.
        new Fiscal_Model_RelatorioPedidos();
        new Fiscal_Form_BuscaCliente();
    }
}
