# Análise dos zips diários de empenhos (e-Fisco / TJPE)

Análise feita sobre 5 zips de 2025 (26/02, 17/06, 28/07, 28/11 e 19/12),
cada um com duas pastas datadas — ao todo, 10 "dias" de extração.

## 1. O que vem no zip

Cada zip traz **duas pastas datadas** (em geral o dia do envio e o envio
anterior — nem sempre dias consecutivos; ex.: o zip de 17/06 traz 13/06 e
17/06). Dentro de cada pasta há arquivos `.TXT` gerados por um batch noturno
do e-Fisco (entre ~23h do dia anterior e ~01h do dia da pasta), nomeados no
padrão:

```
TJPE.<SISTEMA>.<UG>.<TIPO>.TXT      ex.: TJPE.GFU.070002.NEDG.TXT
```

* **UG (unidade gestora)**: `070000` (sempre vazio nos GFU/GFE), `070001` e
  `070002`. Nos PLF aparece também `072000`.
* **Sistema/Tipo** — os relevantes para empenhos:

| Sistema | Tipo | Conteúdo | Tabela no Supabase |
|---|---|---|---|
| GFU | `NEDG` | Nota de Empenho — dados gerais | `empenhos` |
| GFU | `NEIT` | Itens da NE | `empenho_itens` |
| GFU | `NECD` | Cronograma mensal de desembolso da NE (12 linhas/NE) | `empenho_cronograma` |
| GFU | `ANGR` | Anulações de empenho (notas `NA`) | `empenho_anulacoes` |
| GFU | `LE`   | Liquidações | `liquidacoes` |
| GFU | `LEIT` | Itens da liquidação | `liquidacao_itens` |
| GFU | `ALDG` | Anulações de liquidação (notas `AL`) | `liquidacao_anulacoes` |
| GFU | `ALCD` | Parcelas/cronograma das anulações de liquidação | `liquidacao_anulacao_parcelas` |
| GFE | `OB` / `OBS` | Ordens bancárias (pagamentos) — duas séries distintas | `ordens_bancarias` |
| GFE | `RN`   | Retenções (INSS, ISS...) vinculadas a OB/NE | `retencoes` |
| GFE | `GR`   | Guias de recolhimento (só a UG 070001 usa) | `guias_recolhimento` |
| CTB | `EVENT/EVNT`, `EXOR`, `SDCT` | Eventos e saldos contábeis | *não carregado* |
| PLF / PLO | vários | Programação financeira/orçamentária | *não carregado* |

Os arquivos CTB/PLF/PLO ficam fora do escopo (contabilidade e planejamento,
não empenhos); o pipeline os ignora e informa no log.

## 2. Formato dos arquivos

* Encoding **ISO-8859-1 (latin-1)**, quebra de linha CRLF.
* Campos separados por **`<#>`**.
* Primeiro campo de cada linha é o **tipo de registro**:
  * `0` = cabeçalho: identificador + data/hora da geração
    (`GFU_070002_NEDG_202506170103` → gerado em 17/06/2025 01:03);
  * `1` = registro de dados;
  * `9` = trailer com a **quantidade de registros** (usada para validação).
    Em arquivos vazios o trailer pode vir com um campo vazio na frente
    (`<#>9<#>...`) — o parser trata isso.
* Valores monetários: 17 dígitos sem separador, **2 casas decimais**
  (`00000000326960400` = 3.269.604,00). Exceções nos itens de NE:
  **preço unitário com 4 casas** e **quantidade com 2 casas**
  (conferido: 624,00 × 5.239,7500 = 3.269.604,00 ✓).
* Datas `AAAAMMDD`; carimbos de data/hora `AAAAMMDDHHMMSS`.

## 3. A lógica de envio — o ponto principal

### 3.1 Arquivos GFU (NE, LE, anulações): janela móvel de 3 meses

Cada arquivo diário **não é incremental nem acumulado**, e **não é "de dois
anos pra cá" nem "desde o início do ano passado"**: é um **recorte dos
documentos cuja última alteração ocorreu nos últimos 3 meses de calendário**
(contados da data de geração, com dados até o fim do dia anterior). A janela
é definida pela **data da última movimentação** (campo `atualizado_em`), não
pela data de emissão. Verificado em 12 dias de extração (2025 e 2026) — o
menor `atualizado_em` de cada arquivo cai exatamente 3 meses antes da
geração:

| Pasta | Janela observada no NEDG |
|---|---|
| 26/02/2025 | 26/11/2024 → 25/02/2025 |
| 17/06/2025 | 17/03/2025 → 16/06/2025 |
| 28/07/2025 | 28/04/2025 → 25/07/2025 |
| 28/11/2025 | 28/08/2025 → 27/11/2025 |
| 19/12/2025 | 19/09/2025 → 18/12/2025 |
| 18/06/2026 | 18/03/2026 → 17/06/2026 |
| 09/07/2026 | 09/04/2026 → 08/07/2026 |

**Por que às vezes parece "desde o ano passado".** No começo do ano a janela
de 3 meses atravessa a virada: o envio de 26/02/2025 alcançava novembro/2024,
então trazia empenhos de **2024 e 2025** juntos (restos a pagar ainda em
movimento). Isso dá a impressão de "tudo desde o ano passado", mas é só a
janela cruzando o Ano Novo. Prova: os envios de **junho e julho/2026 têm
apenas empenhos de 2026** no NEDG — nenhum de 2025 —, porque em meados do ano
a janela já não alcança mais o exercício anterior.

**Os pagamentos ainda apontam para empenhos que sumiram.** No envio de
09/07/2026, o arquivo de OB traz **676 ordens bancárias de 2026 quitando
empenhos `2025NE`** — empenhos que já não estão no NEDG de 2026 (foram
emitidas quase todas em janeiro/2026, restos a pagar). É a prova concreta de
que **acumular e nunca apagar é obrigatório**: quem só olhasse o arquivo do
dia teria pagamentos órfãos; como a base guardou os empenhos de 2025 das
cargas anteriores, o vínculo se mantém.

Consequências práticas:

* **Um mesmo empenho se repete por até ~90 envios seguidos** (enquanto a
  última alteração dele estiver dentro da janela). A cada dia o registro
  vem com a foto mais atual (valores liquidado/pago acumulados etc.).
* Um empenho **sai do arquivo** quando fica 3 meses sem alteração — e
  **volta a aparecer** se sofrer nova movimentação (conferido: entre 13/06 e
  17/06, saíram 19 NEs cuja última alteração era de 13–17/03 e reapareceram
  NEs antigas como a 2025NE000324, junto com as novas 1720–1733).
* Em fevereiro o arquivo ainda trazia NEs de **2024** (restos a pagar em
  movimentação); a partir de certo ponto do ano, só 2025.
* **Sumir do arquivo não significa cancelamento** — significa apenas
  "3 meses sem movimento". Por isso a base **nunca apaga** registros; só
  insere e atualiza.

### 3.2 Arquivos GFE (OB, OBS, RN): acumulado do exercício

`OB`, `OBS` e `RN` crescem monotonicamente ao longo do ano e retêm
documentos desde janeiro (ex.: OB da UG 070002: 5.631 registros em 17/06 →
7.013 em 28/07 → 15.101 em 19/12, sempre desde 08/01). São reenviados por
inteiro todo dia, com o status atualizado (`Enviada` → `Paga` → `Devolvida`).

`OB` e `OBS` são **duas séries que não se sobrepõem** na numeração
(`2025OBnnnnnn`), com layouts ligeiramente diferentes; a série `OB` tem
dados de relação bancária (`numero_re`) e competência. Ambas vão para a
mesma tabela `ordens_bancarias`, diferenciadas pela coluna `origem`.

### 3.3 Chaves naturais (o que garante a deduplicação)

Cada documento tem numeração única **por UG e exercício**, embutida no
próprio número (`2025NE000123`, `2025LE002378`, `2025NA000044`,
`2025AL000275`, `2025OB002606`, `2025RN002526`, `2025GR000016`).
Atenção: **o mesmo número existe nas duas UGs** (070001 e 070002 têm cada
uma a sua `2025NE000001`), então a chave é sempre `(unidade_gestora, numero)`.

Tabelas de itens (`NEIT`, `NECD`, `LEIT`, `ALCD`) não têm chave natural
estável nos arquivos — a estratégia é **substituir todos os itens do
documento-pai** quando chega um snapshot mais novo dele.

### 3.4 Encadeamento dos documentos (ciclo da despesa)

```
SE (solicitação)  →  NE (empenho)  →  LE (liquidação)  →  OB (pagamento)
                      │                 │                   ├── RN (retenções)
                      └── NA (anulação) └── AL (anulação)   └── RE (relação bancária)
```

O NEDG referencia a SE e o processo SEI; a LE referencia a NE e o documento
hábil (DH); a OB referencia a NE; a RN referencia OB e NE. SE, DH e RE não
têm arquivo próprio no zip — ficam só como referência.

## 4. Regras de carga adotadas pelo pipeline

1. **Idempotência por arquivo**: o SHA-256 de cada TXT fica registrado em
   `arquivos_processados`; rodar o mesmo zip duas vezes não faz nada.
2. **Upsert por chave natural** nas tabelas de documentos, com guarda
   `snapshot_em` — um arquivo antigo ingerido fora de ordem só *insere* o
   que não existe; nunca sobrescreve dado mais novo.
3. **Substituição por documento-pai** nas tabelas de itens, com a mesma
   guarda de `snapshot_em`.
4. **Nada é apagado** — documento que saiu da janela permanece na base com
   a última foto conhecida.
5. A coluna `campos` (JSONB) guarda **todos os campos originais da linha**,
   inclusive os de significado ainda não confirmado, permitindo
   reinterpretar sem reprocessar os zips.

## 5. Interpretações de campo ainda não confirmadas

Nomeadas por evidência, mas vale confirmar com o setor/e-Fisco:

* `empenhos.tipo_empenho`: 1/2/3 — provável ordinário/estimativo/global;
* `empenhos.credor_tipo`: 1=CNPJ, 2=CPF, 3=inscrição genérica (ex.:
  `PF77777770` "folha de pagamento");
* `empenhos.valor_anulado/valor_liquidado/valor_pago`: acumulados — batem
  com a ordem de grandeza das LEs/OBs, mas sem conferência formal;
* `liquidacao_itens.valor_1..valor_5`: cinco colunas de valor na ordem
  original do layout;
* `ordens_bancarias.tipo_ob`: códigos 10–17;
* `empenho_itens.marcador` (E/X) e o 4º campo do NEIT (fica só no JSONB).

## 6. Observações operacionais

* A pasta `13.0.2025` (zip de 17/06) tem o nome truncado — seria 13/06. O
  pipeline não depende do nome da pasta: usa a data de geração do cabeçalho.
* Os zips de 2026 vêm com **uma única pasta datada** (os de 2025 vinham com
  duas). O pipeline lida com os dois formatos.
* Alguns dias não têm arquivo (fins de semana/feriados); o batch roda em
  dias úteis.
* O trailer de cada arquivo é conferido com a contagem real; divergências
  geram aviso no log (não bloqueiam a carga).

## 7. Classificação orçamentária (Ações / Subações / Setores)

Fonte: planilha **"Descrição das Ações e Subações"** (mantida pelo setor).
Liga cada empenho a uma Ação, Subação e, quando possível, ao Setor.

### 7.1 Onde a classificação está no empenho

A `funcional_programatica` do empenho tem **17 dígitos** decodificados assim:

```
função(2) + subfunção(3) + programa(4) + AÇÃO(4) + SUBAÇÃO(4)
02122 0422 4430 1439   ->  ação 4430, subação 1439
02061 0577 4428 A585   ->  ação 4428, subação A585   (subação pode ser alfanumérica)
```

E a **fonte** são os 4 primeiros dígitos de `fonte_recurso` (`0759240000` →
`0759`). A migração `0002` cria colunas **geradas** `acao`, `subacao` e
`fonte_codigo` nos empenhos, extraídas automaticamente desses campos.

### 7.2 O empenho tem a Subação REAL; o Setor (Subação Virtual) só na planilha

A planilha tem cinco colunas: Fonte, Ação, **Subação Real**, **Subação
Virtual** e Descrição (o setor, ex.: "Assessoria de Comunicação"). O empenho
carrega **apenas a Subação Real** — a Subação Virtual (o setor fino) **não
existe no e-Fisco**, é uma camada interna do TJPE. Conferido: os códigos
virtuais (A594, A570, A644…) nunca aparecem em nenhum arquivo de empenho.

Consequência (decisão de negócio adotada — "ligar até a subação real"):

* Quando `(ação, subação real)` aponta para **uma única descrição**, o setor
  é resolvido automaticamente. Vale para a maioria dos grupos.
* Quando a subação real se abre em **vários setores**, o empenho fica com
  **setor = NULL ("não classificado")**. O caso principal é
  **Ação 4430 / Subação 1439** — o maior balde (~44% dos empenhos) — que se
  abre em 7 setores (Comunicação, Cerimonial, SAD, DIMAN, DIPAT, Apoio
  Administrativo, Corregedoria). Também 3537/0000 (FUNSEG vs Suporte
  Segurança) e 4728/0000. Refinar o setor desses exige uma regra/fonte
  adicional ou atribuição manual — a definir.
* **Ação 2596 / Subação 1975** aparece nos empenhos mas **não está na
  planilha** (fica sem match) — verificar com o setor.

### 7.3 Objetos criados (migração 0002)

* `classificacao_orcamentaria` — catálogo completo da planilha (recarregado
  por inteiro a cada importação; a planilha é a fonte da verdade).
* `vw_classificacao_subacao_real` — resolve o setor no nível
  `(ação, subação real)`, marcando as combinações ambíguas.
* `vw_empenhos_classificados` — empenhos já com `setor` resolvido (NULL onde
  ambíguo ou sem match). É a view para consulta/relatório.

Números do teste (empenhos de 2026): 730 com setor resolvido, 725 ambíguos
(setor NULL), 6 sem match na planilha.
