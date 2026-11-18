<?php
class db_utils {

  public static function getDao($sClasse) {

    include_once(modification("classes/db_{$sClasse}_classe.php"));
    $sNome = "cl_" . $sClasse;
    return eval("return new {$sNome}();");
  }
}
