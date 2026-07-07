-- ============================================================================
-- GestaoFinBID — Schema da base de empenhos (e-Fisco / TJPE)
--
-- Alimentada pelos arquivos TXT do zip diário recebido do e-Fisco.
-- Cada arquivo diário é um RECORTE MÓVEL: contém os documentos cuja última
-- atualização ocorreu nos últimos ~3 meses (exceto OB/OBS/RN/GR, que são
-- acumulados do exercício). Por isso a carga é feita por UPSERT: registros
-- repetidos entre dias são simplesmente atualizados, nunca duplicados.
--
-- Convenções:
--  * Valores monetários vêm com 17 dígitos sem separador; escala 2 casas
--    (preço unitário de item: 4 casas; quantidade: 2 casas).
--  * "unidade_gestora" (UG): 070001 e 070002 (070000 vem sempre vazio).
--  * "snapshot_em": data/hora de geração do arquivo de origem do registro —
--    usada para garantir que um arquivo antigo nunca sobrescreva dado novo.
--  * "campos": array JSON com TODOS os campos originais da linha, na ordem,
--    para auditoria e reinterpretação futura sem reprocessar os zips.
--  * Campos com interpretação provável (não confirmada) estão comentados.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Controle de arquivos processados (idempotência da carga)
-- ----------------------------------------------------------------------------
create table if not exists arquivos_processados (
    id              bigint generated always as identity primary key,
    nome_arquivo    text not null,
    tipo            text not null,              -- NEDG, NEIT, NECD, ANGR, LE, LEIT, ALDG, ALCD, OB, OBS, RN, GR
    unidade_gestora text not null,
    gerado_em       timestamp not null,         -- do cabeçalho do arquivo
    registros       integer not null,
    hash_conteudo   text not null unique,       -- sha256 do arquivo; evita reprocessar
    processado_em   timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- EMPENHOS — Notas de Empenho (arquivo NEDG, "dados gerais")
-- ----------------------------------------------------------------------------
create table if not exists empenhos (
    unidade_gestora        text not null,
    numero_ne              text not null,       -- ex.: 2025NE000123
    exercicio              smallint not null,
    gestao                 text,
    data_emissao           date,
    credor_tipo            text,                -- 1=CNPJ, 2=CPF, 3=inscrição genérica (provável)
    credor_doc             text,                -- CNPJ/CPF ou inscrição genérica (ex.: PF77777770)
    credor_nome            text,
    unidade_orcamentaria   text,
    funcional_programatica text,                -- 17 dígitos: função/subfunção/programa/ação...
    natureza_despesa       text,                -- 8 dígitos, ex.: 33903000
    fonte_recurso          text,                -- 10 dígitos
    tipo_empenho           text,                -- 1=ordinário, 2=estimativo, 3=global (provável)
    modalidade_licitacao   text,
    referencia_legal       text,
    valor                  numeric(17,2),       -- valor empenhado
    processo_sei           text,
    valor_anulado          numeric(17,2),       -- acumulado (provável)
    valor_liquidado        numeric(17,2),       -- acumulado (provável)
    valor_pago             numeric(17,2),       -- acumulado (provável)
    numero_se              text,                -- solicitação de empenho
    observacao             text,
    data_referencia        date,                -- campo 33 (provável: data da SE)
    tipo_documento         text,                -- NORMAL, FOLHA DE PAGAMENTO, SUPRIMENTO INSTITUCIONAL...
    atualizado_em          timestamp,           -- última alteração no e-Fisco (define a janela de 3 meses)
    usuario                text,
    campos                 jsonb not null,
    snapshot_em            timestamp not null,
    arquivo_id             bigint references arquivos_processados(id),
    primary key (unidade_gestora, numero_ne)
);
create index if not exists idx_empenhos_credor    on empenhos (credor_doc);
create index if not exists idx_empenhos_natureza  on empenhos (natureza_despesa);
create index if not exists idx_empenhos_emissao   on empenhos (data_emissao);
create index if not exists idx_empenhos_exercicio on empenhos (exercicio);

-- ----------------------------------------------------------------------------
-- Itens da Nota de Empenho (arquivo NEIT) — substituídos por completo a cada
-- snapshot mais novo da NE ("seq" é a ordem da linha dentro da NE no arquivo)
-- ----------------------------------------------------------------------------
create table if not exists empenho_itens (
    unidade_gestora  text not null,
    numero_ne        text not null,
    seq              smallint not null,
    numero_item      text,
    quantidade       numeric(17,2),
    preco_unitario   numeric(17,4),
    valor_total      numeric(17,2),
    codigo_material  text,
    descricao        text,
    natureza_subitem text,                      -- natureza de despesa detalhada (subelemento)
    marcador         text,                      -- E/X (significado não confirmado)
    gestao           text,
    unidade_medida   text,
    atualizado_em    timestamp,
    campos           jsonb not null,
    snapshot_em      timestamp not null,
    arquivo_id       bigint references arquivos_processados(id),
    primary key (unidade_gestora, numero_ne, seq)
);

-- ----------------------------------------------------------------------------
-- Cronograma de desembolso da NE (arquivo NECD) — 12 linhas por NE (mensal);
-- presente apenas para parte das NEs (estimativas/globais, provável)
-- ----------------------------------------------------------------------------
create table if not exists empenho_cronograma (
    unidade_gestora text not null,
    numero_ne       text not null,
    seq             smallint not null,
    mes             smallint,                   -- 01..12
    valor           numeric(17,2),
    valor_2         numeric(17,2),              -- segundo valor do layout (provável: previsto atualizado)
    gestao          text,
    atualizado_em   timestamp,
    campos          jsonb not null,
    snapshot_em     timestamp not null,
    arquivo_id      bigint references arquivos_processados(id),
    primary key (unidade_gestora, numero_ne, seq)
);

-- ----------------------------------------------------------------------------
-- Anulações de empenho — Notas de Anulação (arquivo ANGR)
-- ----------------------------------------------------------------------------
create table if not exists empenho_anulacoes (
    unidade_gestora text not null,
    numero_na       text not null,              -- ex.: 2025NA000044
    numero_ne       text,
    criado_em       timestamp,
    valor           numeric(17,2),
    motivo          text,
    gestao          text,
    atualizado_em   timestamp,
    campos          jsonb not null,
    snapshot_em     timestamp not null,
    arquivo_id      bigint references arquivos_processados(id),
    primary key (unidade_gestora, numero_na)
);
create index if not exists idx_emp_anul_ne on empenho_anulacoes (numero_ne);

-- ----------------------------------------------------------------------------
-- Liquidações (arquivo LE)
-- ----------------------------------------------------------------------------
create table if not exists liquidacoes (
    unidade_gestora text not null,
    numero_le       text not null,              -- ex.: 2025LE002378
    tipo            text,                       -- P=parcial, T=total (provável)
    data_emissao    date,
    historico       text,
    data_referencia date,
    numero_ne       text,
    valor           numeric(17,2),
    gestao          text,
    atualizado_em   timestamp,
    numero_dh       text,                       -- documento hábil vinculado
    campos          jsonb not null,
    snapshot_em     timestamp not null,
    arquivo_id      bigint references arquivos_processados(id),
    primary key (unidade_gestora, numero_le)
);
create index if not exists idx_liq_ne on liquidacoes (numero_ne);

-- ----------------------------------------------------------------------------
-- Itens da liquidação (arquivo LEIT) — substituídos por completo por LE
-- ----------------------------------------------------------------------------
create table if not exists liquidacao_itens (
    unidade_gestora  text not null,
    numero_le        text not null,
    seq              smallint not null,
    numero_ne        text,
    item_ne          text,                      -- item da NE a que se refere
    natureza_subitem text,
    valor_1          numeric(17,2),             -- interpretações não confirmadas; ordem original
    valor_2          numeric(17,2),
    valor_3          numeric(17,2),
    valor_4          numeric(17,2),
    valor_5          numeric(17,2),
    codigo_material  text,
    descricao        text,
    unidade_medida   text,
    quantidade       numeric(17,2),
    gestao           text,
    atualizado_em    timestamp,
    campos           jsonb not null,
    snapshot_em      timestamp not null,
    arquivo_id       bigint references arquivos_processados(id),
    primary key (unidade_gestora, numero_le, seq)
);
create index if not exists idx_liqit_ne on liquidacao_itens (numero_ne);

-- ----------------------------------------------------------------------------
-- Anulações de liquidação (arquivo ALDG)
-- ----------------------------------------------------------------------------
create table if not exists liquidacao_anulacoes (
    unidade_gestora text not null,
    numero_al       text not null,              -- ex.: 2025AL000275
    numero_le       text,
    tipo            text,                       -- T=total, P=parcial (provável)
    data_anulacao   date,
    valor           numeric(17,2),
    motivo          text,
    gestao          text,
    atualizado_em   timestamp,
    campos          jsonb not null,
    snapshot_em     timestamp not null,
    arquivo_id      bigint references arquivos_processados(id),
    primary key (unidade_gestora, numero_al)
);
create index if not exists idx_liqanul_le on liquidacao_anulacoes (numero_le);

-- ----------------------------------------------------------------------------
-- Parcelas/cronograma da anulação de liquidação (arquivo ALCD)
-- ----------------------------------------------------------------------------
create table if not exists liquidacao_anulacao_parcelas (
    unidade_gestora text not null,
    numero_al       text not null,
    seq             smallint not null,
    parcela         text,                       -- provável mês (01..12)
    valor           numeric(17,2),
    gestao          text,
    atualizado_em   timestamp,
    campos          jsonb not null,
    snapshot_em     timestamp not null,
    arquivo_id      bigint references arquivos_processados(id),
    primary key (unidade_gestora, numero_al, seq)
);

-- ----------------------------------------------------------------------------
-- Ordens Bancárias (arquivos OB e OBS — duas séries distintas, sem sobreposição
-- de numeração; ambas acumuladas do exercício). "origem" indica o arquivo.
-- ----------------------------------------------------------------------------
create table if not exists ordens_bancarias (
    unidade_gestora text not null,
    numero_ob       text not null,              -- ex.: 2025OB002606
    origem          text not null,              -- 'OB' ou 'OBS'
    criado_em       timestamp,
    numero_ne       text,
    banco_origem    text,
    agencia_origem  text,
    conta_origem    text,
    credor_tipo     text,
    credor_doc      text,
    credor_nome     text,
    banco_destino   text,
    agencia_destino text,
    conta_destino   text,
    data_emissao    date,
    tipo_ob         text,                       -- código (10..17; significado não confirmado)
    valor           numeric(17,2),
    historico       text,
    status          text,                       -- PAGA / Enviada / Devolvida...
    numero_re       text,                       -- relação de OB (apenas série OB)
    competencia     text,                       -- AAAAMM (apenas série OB)
    campos          jsonb not null,
    snapshot_em     timestamp not null,
    arquivo_id      bigint references arquivos_processados(id),
    primary key (unidade_gestora, numero_ob)
);
create index if not exists idx_ob_ne     on ordens_bancarias (numero_ne);
create index if not exists idx_ob_credor on ordens_bancarias (credor_doc);

-- ----------------------------------------------------------------------------
-- Retenções (arquivo RN) — vinculadas a OB e NE; acumuladas do exercício
-- ----------------------------------------------------------------------------
create table if not exists retencoes (
    unidade_gestora  text not null,
    numero_rn        text not null,             -- ex.: 2025RN002526
    numero_ob        text,
    numero_ne        text,
    data_emissao     date,
    valor            numeric(17,2),
    historico        text,
    ug_registro      text,
    status           text,
    tipo             text,                      -- ex.: INSS PESSOA JURÍDICA
    data_pagamento   date,
    gestao           text,
    exercicio        smallint,
    campos           jsonb not null,
    snapshot_em      timestamp not null,
    arquivo_id       bigint references arquivos_processados(id),
    primary key (unidade_gestora, numero_rn)
);
create index if not exists idx_rn_ne on retencoes (numero_ne);
create index if not exists idx_rn_ob on retencoes (numero_ob);

-- ----------------------------------------------------------------------------
-- Guias de Recolhimento (arquivo GR) — usado apenas pela UG 070001
-- ----------------------------------------------------------------------------
create table if not exists guias_recolhimento (
    unidade_gestora  text not null,
    numero_gr        text not null,             -- ex.: 2025GR000016
    banco            text,
    agencia          text,
    conta            text,
    data_emissao     date,
    valor            numeric(17,2),
    historico        text,
    ug_registro      text,
    tipo_receita     text,                      -- ex.: RECS/TRIBT
    contribuinte_tipo text,
    contribuinte_doc text,
    data_referencia  date,
    gestao           text,
    exercicio        smallint,
    status           text,
    campos           jsonb not null,
    snapshot_em      timestamp not null,
    arquivo_id       bigint references arquivos_processados(id),
    primary key (unidade_gestora, numero_gr)
);

-- ----------------------------------------------------------------------------
-- Segurança (Supabase): RLS ligado em tudo; sem policies, apenas o service
-- role / conexão direta ao Postgres consegue ler e escrever. Para liberar
-- leitura a usuários autenticados, crie policies "for select using (true)".
-- ----------------------------------------------------------------------------
alter table arquivos_processados          enable row level security;
alter table empenhos                      enable row level security;
alter table empenho_itens                 enable row level security;
alter table empenho_cronograma            enable row level security;
alter table empenho_anulacoes             enable row level security;
alter table liquidacoes                   enable row level security;
alter table liquidacao_itens              enable row level security;
alter table liquidacao_anulacoes          enable row level security;
alter table liquidacao_anulacao_parcelas  enable row level security;
alter table ordens_bancarias              enable row level security;
alter table retencoes                     enable row level security;
alter table guias_recolhimento            enable row level security;
