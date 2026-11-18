<?php
$sPath = 'libs/';
require_once modification("{$sPath}db_conn.php");
$conn = pg_connect("host=$DB_SERVIDOR dbname=$DB_BASE port=$DB_PORTA user=$DB_USUARIO");
