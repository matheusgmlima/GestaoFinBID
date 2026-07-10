-- ============================================================================
-- GestaoFinBID — Classificação orçamentária (Ações / Subações / Setores)
--
-- Liga os empenhos à planilha "Descrição das Ações e Subações".
--
-- Como o vínculo funciona (ver docs/ANALISE_ARQUIVOS.md, seção 7):
--  * A funcional_programatica do empenho (17 dígitos) codifica:
--      função(2) + subfunção(3) + programa(4) + AÇÃO(4) + SUBAÇÃO(4)
--    Ex.: 0212204224430 1439  -> ação 4430, subação 1439
--         0206105774428 A585  -> ação 4428, subação A585
--  * O empenho carrega a SUBAÇÃO REAL. A "subação virtual" (o setor fino,
--    ex.: "Assessoria de Comunicação") só existe na planilha e NÃO vem no
--    e-Fisco. Por isso o setor só é resolvido automaticamente quando a
--    combinação (ação, subação real) aponta para uma única descrição.
--    Onde a subação real se abre em vários setores (ex.: 4430/1439 -> 7
--    setores), o empenho fica com setor = NULL ("não classificado").
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1) Colunas derivadas nos empenhos (geradas automaticamente da
--    funcional_programatica e da fonte_recurso — não exigem mudança na carga)
-- ----------------------------------------------------------------------------
alter table empenhos
    add column if not exists fonte_codigo text
        generated always as (nullif(substring(fonte_recurso from 1 for 4), '')) stored,
    add column if not exists acao text
        generated always as (
            case when length(funcional_programatica) = 17
                 then substring(funcional_programatica from 10 for 4) end
        ) stored,
    add column if not exists subacao text
        generated always as (
            case when length(funcional_programatica) = 17
                 then substring(funcional_programatica from 14 for 4) end
        ) stored;

create index if not exists idx_empenhos_acao_subacao on empenhos (acao, subacao);

-- ----------------------------------------------------------------------------
-- 2) Catálogo completo, carregado da planilha (36 linhas hoje).
--    Recarregado por completo a cada importação (a planilha é a fonte da
--    verdade). Guarda a subação virtual e a descrição/setor.
-- ----------------------------------------------------------------------------
create table if not exists classificacao_orcamentaria (
    id              bigint generated always as identity primary key,
    fonte_codigo    text not null,             -- ex.: 0759
    fonte_nome      text,                       -- ex.: FERM - TJPE, FUNSEG, BID
    acao            text not null,              -- ex.: 4430
    subacao_real    text not null,              -- ex.: 1439, A585, 0000
    subacao_virtual text,                       -- ex.: A594 (setor fino)
    descricao       text,                       -- ex.: Assessoria de Comunicação
    importado_em    timestamptz not null default now()
);
create index if not exists idx_classif_acao_subreal
    on classificacao_orcamentaria (acao, subacao_real);

alter table classificacao_orcamentaria enable row level security;

-- ----------------------------------------------------------------------------
-- 3) Resolução no nível (ação, subação real): expõe o setor único quando a
--    combinação não é ambígua; marca as ambíguas e lista os setores possíveis.
-- ----------------------------------------------------------------------------
create or replace view vw_classificacao_subacao_real as
select
    acao,
    subacao_real,
    min(fonte_codigo)                                              as fonte_codigo,
    min(fonte_nome)                                                as fonte_nome,
    count(distinct descricao)                                     as setores_possiveis,
    (count(distinct descricao) > 1)                              as ambigua,
    case when count(distinct descricao) = 1 then min(descricao) end as setor,
    string_agg(distinct descricao, ' | ' order by descricao)     as setores_lista
from classificacao_orcamentaria
group by acao, subacao_real;

-- ----------------------------------------------------------------------------
-- 4) Empenhos já com o setor resolvido (LEFT JOIN: empenho sem correspondência
--    ou com subação ambígua aparece com setor = NULL).
-- ----------------------------------------------------------------------------
create or replace view vw_empenhos_classificados as
select
    e.*,
    c.setor,
    c.fonte_nome            as fonte_nome_planilha,
    c.ambigua               as classificacao_ambigua,
    c.setores_possiveis,
    c.setores_lista
from empenhos e
left join vw_classificacao_subacao_real c
    on c.acao = e.acao and c.subacao_real = e.subacao;
