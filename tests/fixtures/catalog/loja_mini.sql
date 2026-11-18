-- SINTETICO: dicionário de dados e menus da loja municipal fictícia (inventado).
-- Coerente com tests/fixtures/schema/loja_mini.sql e tests/fixtures/loja-mini/.
-- Arquivo em latin-1, como os incrementais de um sistema legado.

-- Módulos do dicionário (codmod, nomemod, descricao, dataincl, ativo)
insert into db_sysmodulo values (10, 'pedidos', 'Módulo de pedidos', '2010-01-01', 't');
insert into db_sysmodulo values (11,'estoque','Módulo de estoque','2012-03-24','t');

-- Tabelas (codarq, nomearq, descricao, sigla, dataincl, rotulo, tipotabela, ...)
insert into db_sysarquivo values (101, 'pedido', 'Pedidos', 'ped', '2010-01-01', 'Pedidos', 0, 'f', 'f', 'f', 'f' );
select fc_executa_ddl('insert into db_sysarquivo values (102, \'pedidocancelado\', \'Cancelamentos de pedido\', \'can\', \'2010-01-01\', \'Cancelamentos\', 0, \'f\', \'f\', \'f\', \'f\' );');
insert into db_sysarquivo values (103, 'itempedido', 'Itens do pedido', 'ite', '2010-01-01', 'Itens', 0, 'f', 'f', 'f', 'f' );
insert into db_sysarquivo values (104, 'estoque', 'Saldo de estoque', 'est', '2012-03-24', 'Estoque', 0, 'f', 'f', 'f', 'f' );
-- Tabela que o código da fixture não toca: vai para tables_unreferenced.
insert into db_sysarquivo values (105, 'transportadora', 'Transportadoras', 'tra', '2015-04-27', 'Transportadoras', 0, 'f', 'f', 'f', 'f' );

-- Tabela que só existe no dicionário (não está no DDL): vai para tables_unreferenced.
insert into db_sysarquivo values (106, 'devolucao', 'Devoluções de produto', 'dev', '2016-01-10', 'Devoluções', 0, 'f', 'f', 'f', 'f' );

-- Tabela -> módulo (codmod, codarq)
insert into db_sysarqmod values (10, 101);
insert into db_sysarqmod values (10, 102);
insert into db_sysarqmod values (10, 103);
insert into db_sysarqmod values (11, 104);
insert into db_sysarqmod values (11, 105);
insert into db_sysarqmod values (11, 106);

-- Colunas (codcam, nomecam, conteudo, descricao, valorinicial, rotulo, tamanho, ...)
insert into db_syscampo values(2001,'ped01_codigo','int4','Código do pedido','0', 'Pedido',10,'f','f','f',1,'text','Pedido');
insert into db_syscampo values(2002,'ped01_situacao','int4','Situação do pedido','0', 'Situação',10,'f','f','f',1,'text','Situação');
insert into db_syscampo values(2003,'can01_codigo','int4','Código do cancelamento','0', 'Cancelamento',10,'f','f','f',1,'text','Cancelamento');
insert into db_syscampo values(2004,'est01_saldo','numeric','Saldo do produto em estoque','0', 'Saldo',15,'f','f','f',4,'text','Saldo');

-- Tabela -> coluna (codarq, codcam, seqarq, codsequencia)
insert into db_sysarqcamp values(101,2001,1,0);
insert into db_sysarqcamp values(101,2002,2,0);
insert into db_sysarqcamp values(102,2003,1,0);
insert into db_sysarqcamp values(104,2004,1,0);

-- Módulos do menu (id_item, nome_modulo, descr_modulo, imagem, temexerc)
insert into db_modulos( id_item ,nome_modulo ,descr_modulo ,imagem ,temexerc ) values ( 500 ,'Pedidos' ,'Pedidos' ,'' ,'true' );
insert into db_modulos( id_item ,nome_modulo ,descr_modulo ,imagem ,temexerc ) values ( 501 ,'Estoque' ,'Estoque' ,'' ,'true' );

-- Itens de menu (id_item, descricao, help, funcao, itemativo, manutencao, desctec, libcliente)
insert into db_itensmenu values( 40, 'Consultas', 'Consultas', '', '1', '1', 'Consultas', '1' );
insert into db_itensmenu values( 41, 'Procedimentos', 'Procedimentos', '', '1', '1', 'Procedimentos', '1' );
insert into db_itensmenu values( 500, 'Pedidos', 'Módulo de pedidos', '', '2', '1', 'Módulo de pedidos', '1' );
insert into db_itensmenu values( 501, 'Estoque', 'Módulo de estoque', '', '2', '1', 'Módulo de estoque', '1' );
insert into db_itensmenu values( 7001, 'Cancelar Pedido', 'Cancelamento de pedido', 'ped4_cancelarpedido001.php', '1', '1', 'Cancela um pedido e devolve o estoque', '1' );
-- Item inativo (itemativo 't' vira 'f' mais abaixo): mantém a aresta, com active=false.
insert into db_itensmenu (id_item ,descricao ,help ,funcao ,itemativo ,manutencao ,desctec ,libcliente ) values ( 7002 ,'Baixa (antiga)' ,'Baixa' ,'ped4_baixaestoque004.php' ,'t' ,'1' ,'Tela antiga de baixa' , true );
-- Item com query string, pendurado nos menus de dois módulos.
insert into db_itensmenu (id_item ,descricao ,help ,funcao ,itemativo ,manutencao ,desctec ,libcliente ) values ( 7003 ,'Pedidos' ,'Consulta de pedidos' ,' func_pedido.php?funcao_js=parent.js_mostra ' ,'1' ,'1' ,'Consulta de pedidos' ,'true' );
-- Item para um arquivo que não está no scan: vai para menu_missing_files.
insert into db_itensmenu values( 7004, 'Transportadoras', 'Transportadoras', 'est1_transportadora001.php', '1', '1', 'Transportadoras', '1' );
-- Item inserido e apagado depois (DELETE ... IN).
insert into db_itensmenu values( 7005, 'Cancelamento (removido)', 'Removido', 'ped4_cancelarpedido001.php', '1', '1', 'Removido', '1' );

-- Árvore (id_item, id_item_filho, menusequencia, modulo): módulo -> raiz compartilhada -> item.
insert into db_menu ( id_item ,id_item_filho ,menusequencia ,modulo ) values ( 500 ,40 ,2 ,500 );
insert into db_menu ( id_item ,id_item_filho ,menusequencia ,modulo ) values ( 500 ,41 ,4 ,500 );
insert into db_menu ( id_item ,id_item_filho ,menusequencia ,modulo ) values ( 501 ,40 ,2 ,501 );
insert into db_menu values(41, 7001, 1, 500);
insert into db_menu values(41, 7002, 2, 500);
insert into db_menu values(40, 7003, 1, 500);
insert into db_menu values(40, 7003, 2, 501);
insert into db_menu values(40, 7004, 3, 501);
insert into db_menu values(41, 7005, 3, 500);

update db_itensmenu set itemativo = 'f' where id_item = 7002;
delete from db_menu where id_item_filho in (7005);
delete from db_itensmenu where id_item in (7005);
