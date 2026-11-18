<?php
//MODULO: loja
// DAO da tabela pedido (inventada).
class cl_pedido {

  var $rotulo = null;
  var $campos = "ped01_codigo ped01_cliente ped01_data ped01_total ped01_situacao";

  function cl_pedido() {
    $this->rotulo = new rotulo("pedido");
  }

  function incluir($codigo) {
    $sql  = "insert into pedido(ped01_codigo,ped01_cliente,ped01_data,ped01_total,ped01_situacao)";
    $sql .= " values ($this->ped01_codigo,$this->ped01_cliente,$this->ped01_data,$this->ped01_total,$this->ped01_situacao)";
    $result = db_query($sql);
    if ($result == false) {
      $this->erro_status = "0";
    }
    return true;
  }

  function alterar($codigo) {
    $sql  = "update pedido set ";
    $sql .= "ped01_situacao = $this->ped01_situacao";
    $sql .= " where ped01_codigo = $codigo";
    $result = db_query($sql);
    return $result;
  }

  function sql_record($sql) {
    $result = db_query($sql);
    return $result;
  }

  function sql_query_file($codigo = null, $campos = "*", $ordem = null) {
    $sql = "select ";
    $sql .= $campos;
    $sql .= " from pedido ";
    if ($codigo != null) {
      $sql .= " where ped01_codigo = $codigo";
    }
    return $sql;
  }
}
?>
