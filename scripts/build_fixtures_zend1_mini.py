"""Gera tests/fixtures/zend1-mini: um mini projeto Zend Framework 1 inteiramente sintético.

Todo o conteúdo é escrito à mão e inventado (domínio de pedidos, clientes e contas contábeis):
nenhum trecho vem de um sistema real. Os módulos se chamam `fiscal` e `webservice` porque o
perfil `zend1` só prevê a regra `<Modulo>_<Resto>` para módulos que ele conhece. Cada arquivo
tem um comentário `[fixture]` dizendo que caso do scan ele exercita.

Os nomes de método remoto que o cliente SOAP chama (`getDadosEmpresa`, `DadosPrefeitura`) são do
e-Cidade (software livre) e existem em `tests/fixtures/ecidade-mini`.

Uso:  python scripts/build_fixtures_zend1_mini.py
Saída: a lista dos arquivos gerados, em JSON, no stdout.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DST = ROOT / "tests/fixtures/zend1-mini"

FIXTURES: dict[str, str] = {
    "application/configs/application.ini": """\
; [fixture] sintético: só as chaves que o perfil lê (sem credenciais de banco)
[production]
appnamespace                     = "Application"
autoloadernamespaces.dbseller    = "DBSeller_"
autoloadernamespaces.doctrine    = "Doctrine_"
autoloadernamespaces.twitter     = "Twitter_"
resources.frontController.moduleDirectory = APPLICATION_PATH "/modules"
""",
    "application/Bootstrap.php": """\
<?php
// [fixture] só confirma o perfil do projeto; fora de application/modules/, sem módulo.
class Bootstrap extends Zend_Application_Bootstrap_Bootstrap
{
}
""",
    "application/modules/fiscal/library/Controller/AbstractController.php": """\
<?php
// [fixture] Regra 1 (Lib -> library/, mais um nível): Fiscal_Lib_Controller_AbstractController.
class Fiscal_Lib_Controller_AbstractController extends Zend_Controller_Action
{
}
""",
    "application/modules/fiscal/models/RelatorioPedidos.php": """\
<?php
// [fixture] Regra 1 (Model -> models/): Fiscal_Model_RelatorioPedidos.
class Fiscal_Model_RelatorioPedidos
{
    public function buscar()
    {
        return array();
    }
}
""",
    "application/modules/fiscal/forms/BuscaCliente.php": """\
<?php
// [fixture] Regra 1 (Form -> forms/): Fiscal_Form_BuscaCliente.
class Fiscal_Form_BuscaCliente extends Twitter_Bootstrap_Form_Vertical
{
}
""",
    "application/modules/fiscal/controllers/PedidoController.php": """\
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
""",
    "application/modules/fiscal/controllers/CancelamentoPedidoController.php": """\
<?php
// [fixture] Kebab-case: CancelamentoPedido -> cancelamento-pedido.
class Fiscal_CancelamentoPedidoController extends Fiscal_Lib_Controller_AbstractController
{
    public function consultarAction()
    {
        // tem views/scripts/cancelamento-pedido/consultar.phtml: RENDERS deve existir.
    }
}
""",
    "application/modules/fiscal/views/scripts/cancelamento-pedido/consultar.phtml": """\
<p>[fixture] tela de consultarAction: HTML puro, ok sem símbolos (não é parse_error).</p>
""",
    "application/modules/fiscal/views/scripts/pagina-orfa/tela.phtml": """\
<p>[fixture] tela que nenhuma action renderiza: candidata a orphan_views.</p>
""",
    "application/modules/webservice/models/Sample.php": """\
<?php
// [fixture] 2º módulo, só para provar que o BELONGS_TO por diretório não fica restrito a um
// módulo só.
class Webservice_Model_Sample
{
}
""",
    "application/entidades/Financeira/ArquivoAlerta.php": r"""<?php
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
""",
    "application/entidades/Contabil/ContaContabil.php": r"""<?php
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
""",
    "application/entidades/Geral/Cliente.php": r"""<?php
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
""",
    "application/modules/fiscal/models/CadastroDeContas.php": r"""<?php
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
""",
    "application/modules/webservice/models/Ecidade.php": r"""<?php
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
""",
    "application/modules/fiscal/models/ConsultaEmpresa.php": r"""<?php
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
""",
    "application/modules/webservice/models/Integracao.php": r"""<?php
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
""",
    "application/modules/webservice/controllers/IndexController.php": r"""<?php
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
""",
    "application/modules/fiscal/models/RetificacaoPedidos.php": r"""<?php
// [fixture] SQL de verdade (`from pedido x`): tem de seguir pelo pglast, nunca pelo caminho da
// DQL.
class Fiscal_Model_RetificacaoPedidos
{
    public function buscar()
    {
        $sSql = "select x.vl_total, x.vl_desconto
      from pedido x
      inner join pedido_itens y
        on y.id_pedido = x.id";
        return $sSql;
    }
}
""",
}


def build() -> list[str]:
    for rel, content in FIXTURES.items():
        dest = DST / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8", newline="\n")
    return sorted(FIXTURES)


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=1))
