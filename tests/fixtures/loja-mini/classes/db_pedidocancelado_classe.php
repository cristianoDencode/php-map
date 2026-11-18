<?php
//MODULO: loja
// DAO da tabela pedidocancelado (inventada).
class cl_pedidocancelado {

  var $rotulo = null;
  var $campos = "can01_codigo can01_pedido can01_data can01_motivo";

  function cl_pedidocancelado() {
    $this->rotulo = new rotulo("pedidocancelado");
  }

  function incluir($codigo) {
    $sql  = "insert into pedidocancelado(can01_codigo,can01_pedido,can01_data,can01_motivo)";
    $sql .= " values ($this->can01_codigo,$this->can01_pedido,$this->can01_data,'$this->can01_motivo')";
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
    $sql .= " from pedidocancelado ";
    if ($codigo != null) {
      $sql .= " where can01_codigo = $codigo";
    }
    return $sql;
  }
}
?>
