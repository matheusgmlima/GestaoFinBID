# GestaoFinBID — Base de empenhos do TJPE no Supabase

Pipeline para alimentar um banco Supabase com os **zips diários de empenhos**
(extração do e-Fisco) recebidos pelo setor: notas de empenho, itens,
cronogramas, anulações, liquidações, ordens bancárias, retenções e guias.

> **Como funciona o envio diário?** Cada arquivo é um recorte móvel dos
> documentos alterados nos **últimos 3 meses** (pagamentos são acumulados do
> exercício) — ou seja, o mesmo empenho se repete por dezenas de envios. A
> carga é feita por *upsert*, então repetição não gera duplicidade. A análise
> completa da lógica está em [`docs/ANALISE_ARQUIVOS.md`](docs/ANALISE_ARQUIVOS.md).

## Configuração (uma vez só)

1. **Criar as tabelas**: no painel do Supabase, abra *SQL Editor*, cole o
   conteúdo de [`supabase/migrations/0001_empenhos_schema.sql`](supabase/migrations/0001_empenhos_schema.sql)
   e execute.

2. **Instalar o cliente** (Python 3.9+):

   ```bash
   pip install -r ingest/requirements.txt
   ```

3. **Configurar a conexão**: copie `.env.example` para `.env` e preencha com
   a connection string do projeto (painel do Supabase → **Connect** →
   *Session pooler* / URI). A senha é a definida na criação do projeto.

## Uso diário

Chegou o zip do dia? Rode:

```bash
python ingest/ingest.py caminho/para/17.06.2025.zip
```

Pode passar vários zips, uma pasta com zips acumulados, ou pastas já
extraídas — tudo de uma vez e **em qualquer ordem**:

```bash
python ingest/ingest.py Downloads/zips-do-efisco/
```

O pipeline:

* ordena os arquivos pela data de geração e carrega do mais antigo ao mais novo;
* **pula arquivos já processados** (controle por hash em `arquivos_processados`);
* nunca deixa um arquivo antigo sobrescrever dado mais novo;
* ignora os arquivos de contabilidade/planejamento (CTB/PLF/PLO), fora do
  escopo de empenhos;
* confere o trailer (contagem de registros) de cada arquivo e avisa sobre
  qualquer divergência.

Para só inspecionar o que seria carregado, sem gravar:

```bash
python ingest/ingest.py --dry-run caminho/para/o.zip
```

## O que vai para o banco

| Tabela | Origem | Conteúdo |
|---|---|---|
| `empenhos` | NEDG | Notas de empenho (credor, dotação, valores, SEI...) |
| `empenho_itens` | NEIT | Itens/materiais de cada NE |
| `empenho_cronograma` | NECD | Cronograma mensal de desembolso |
| `empenho_anulacoes` | ANGR | Notas de anulação de empenho |
| `liquidacoes` | LE | Liquidações (vinculadas à NE) |
| `liquidacao_itens` | LEIT | Itens das liquidações |
| `liquidacao_anulacoes` | ALDG | Anulações de liquidação |
| `liquidacao_anulacao_parcelas` | ALCD | Parcelas das anulações de liquidação |
| `ordens_bancarias` | OB + OBS | Pagamentos (duas séries, coluna `origem`) |
| `retencoes` | RN | Retenções (INSS, ISS...) por OB/NE |
| `guias_recolhimento` | GR | Guias de recolhimento (UG 070001) |
| `arquivos_processados` | — | Controle de carga (idempotência) |

Consultas típicas:

```sql
-- Execução de um empenho
select numero_ne, credor_nome, valor, valor_liquidado, valor_pago
from empenhos where numero_ne = '2025NE000123';

-- Pagamentos de um credor no ano
select numero_ob, data_emissao, valor, status
from ordens_bancarias where credor_doc = '00000000000000'
order by data_emissao;
```

As tabelas têm RLS habilitado sem policies: só o *service role* (e a conexão
direta usada pela carga) acessa. Para liberar leitura no app/dashboard, crie
policies de `select`.
