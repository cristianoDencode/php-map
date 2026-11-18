---
id: criar-novo-modelo
titulo: Criar um modelo novo com a IA
descricao: Pede à IA um modelo de briefing novo, no formato do sistema, para a necessidade descrita.
versao: 1
status: oficial
autor: sistema
parametros:
  - {nome: necessidade, tipo: texto, rotulo: "O que você precisa que o modelo faça", obrigatorio: true}
resposta: {formato: livre}
limite_palavras: 30000
---
# Papel
Você cria modelos de briefing para o sistema php-map. Um modelo é um arquivo Markdown no formato descrito abaixo.

# Tarefa
Crie UM modelo novo que atenda à necessidade descrita. Use apenas os blocos e os tipos de parâmetro listados em "Formato e blocos". Devolva somente o arquivo do modelo, dentro de um bloco de código Markdown, sem explicações antes ou depois.

# Regras
- Use SOMENTE blocos que existem na lista. Não invente blocos, tipos de parâmetro nem chaves de cabeçalho.
- O `status` do modelo novo é `rascunho` e o `autor` é o nome de quem pediu (ou `ia`).
- Todo parâmetro declarado precisa ser usado no corpo, e todo marcador precisa apontar para um parâmetro declarado ou para um bloco da lista.
- Inclua no corpo as seções Papel, Tarefa, Regras (com "use SOMENTE o contexto"), Contexto e Formato da resposta.
- Se um bloco necessário estiver "não disponível neste mapa", escolha outro ou avise ao final, fora do bloco de código.

# Necessidade
{{bloco:texto_livre texto={{necessidade}}}}

# Formato e blocos
{{bloco:catalogo_de_blocos}}
