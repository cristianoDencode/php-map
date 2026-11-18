<?php
// [fixture] o SoapServer com `setClass` literal; o WSDL chega numa variável (nunca literal).
class WebService_IndexController extends Zend_Controller_Action
{
    public function integracaoAction()
    {
        $sWsdl = realpath(PUBLIC_PATH . '/webservice/wsdlValidations/integracao/modelo1.wsdl');
        $server = new SoapServer($sWsdl, array('soap_version' => SOAP_1_1));
        $server->setClass('WebService_Model_Integracao');
        $server->handle();
    }
}
