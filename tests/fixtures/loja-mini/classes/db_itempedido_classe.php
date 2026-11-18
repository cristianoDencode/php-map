<?php
//MODULO: loja
// DAO da tabela itempedido (inventada).
class cl_itempedido {

  var $rotulo = null;
  var $campos = "ite01_codigo ite01_pedido ite01_produto ite01_quantidade";

  function cl_itempedido() {
    $this->rotulo = new rotulo("itempedido");
  }

  function incluir($codigo) {
    $sql  = "insert into itempedido(ite01_codigo,ite01_pedido,ite01_produto,ite01_quantidade)";
    $sql .= " values ($this->ite01_codigo,$this->ite01_pedido,$this->ite01_produto,$this->ite01_quantidade)";
    $result = db_query($sql);
    if ($result == false) {
      $this->erro_status = "0";
    }
    return true;
  }

  function sql_record($sql) {
    $result = db_query($sql);
    return $result;
  }

  function sql_query_file($codigo = null, $campos = "*", $ordem = null) {
    $sql = "select ";
    $sql .= $campos;
    $sql .= " from itempedido ";
    if ($codigo != null) {
      $sql .= " where ite01_codigo = $codigo";
    }
    return $sql;
  }
}
?>
