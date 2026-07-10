#!/usr/bin/env python3
"""
Carga dos zips diários de empenhos (e-Fisco / TJPE) para o Supabase.

Uso:
    python ingest.py [opções] CAMINHO [CAMINHO ...]

Onde CAMINHO pode ser um .zip diário, uma pasta (com as subpastas datadas)
ou um .TXT individual. Pode passar vários de uma vez — os arquivos são
ordenados pela data de geração antes da carga, e arquivos já processados
(mesmo conteúdo) são pulados automaticamente.

A conexão usa a variável de ambiente SUPABASE_DB_URL (ou DATABASE_URL),
ou a opção --db-url. Use a "connection string" do Supabase:
Dashboard -> Connect -> ORMs/URI (postgresql://postgres...).

Lógica de deduplicação (ver docs/ANALISE_ARQUIVOS.md):
  * Cada arquivo diário repete os documentos alterados nos últimos ~3 meses
    (OB/OBS/RN/GR: acumulados do exercício). A carga é um UPSERT por chave
    natural; um arquivo mais antigo nunca sobrescreve dado mais novo
    (guarda pela coluna snapshot_em).
  * Tabelas de itens (NEIT, NECD, LEIT, ALCD) não têm chave natural estável:
    os itens do documento-pai são substituídos por completo quando chega um
    snapshot mais novo daquele documento.
"""

import argparse
import hashlib
import os
import re
import sys
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # permite --dry-run sem o driver instalado
    psycopg = None
    Jsonb = None

DELIM = "<#>"
ENCODING = "latin-1"

FILENAME_RE = re.compile(
    r"TJPE\.(GFU|GFE)\.(\d{6})\."
    r"(NEDG|NEIT|NECD|ANGR|LEIT|LE|ALDG|ALCD|OBS|OB|RN|GR)\.TXT$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Conversores de campo
# ---------------------------------------------------------------------------

def _texto(v):
    v = v.strip()
    return v or None

def _inteiro(v):
    v = v.strip()
    return int(v) if v.isdigit() else None

def _valor(casas):
    def conv(v):
        v = v.strip()
        if not v or not v.isdigit():
            return None
        try:
            return Decimal(v) / (10 ** casas)
        except InvalidOperation:
            return None
    return conv

_valor2 = _valor(2)
_valor4 = _valor(4)

def _data8(v):
    v = v.strip()
    if len(v) != 8 or not v.isdigit() or v == "00000000":
        return None
    try:
        return datetime.strptime(v, "%Y%m%d").date()
    except ValueError:
        return None

def _ts14(v):
    v = v.strip()
    if len(v) != 14 or not v.isdigit():
        return None
    try:
        return datetime.strptime(v, "%Y%m%d%H%M%S")
    except ValueError:
        return None

# ---------------------------------------------------------------------------
# Layouts: posição do campo na linha (1-based; posição 1 é o tipo "1")
# ---------------------------------------------------------------------------

LAYOUTS = {
    "NEDG": dict(
        table="empenhos", kind="upsert", key="numero_ne",
        cols={
            "exercicio": (2, _inteiro), "numero_ne": (3, _texto),
            "data_emissao": (4, _data8), "gestao": (6, _texto),
            "credor_doc": (7, _texto), "credor_tipo": (8, _texto),
            "unidade_orcamentaria": (9, _texto),
            "funcional_programatica": (10, _texto),
            "natureza_despesa": (11, _texto), "fonte_recurso": (12, _texto),
            "tipo_empenho": (13, _texto), "modalidade_licitacao": (14, _texto),
            "referencia_legal": (15, _texto), "valor": (17, _valor2),
            "processo_sei": (18, _texto), "valor_anulado": (21, _valor2),
            "valor_liquidado": (23, _valor2), "valor_pago": (24, _valor2),
            "numero_se": (25, _texto), "observacao": (26, _texto),
            "credor_nome": (32, _texto), "data_referencia": (33, _data8),
            "tipo_documento": (34, _texto), "atualizado_em": (36, _ts14),
            "usuario": (37, _texto),
        },
    ),
    "NEIT": dict(
        table="empenho_itens", kind="replace", parent="numero_ne",
        cols={
            "numero_ne": (2, _texto), "numero_item": (3, _texto),
            "quantidade": (5, _valor2), "preco_unitario": (6, _valor4),
            "valor_total": (7, _valor2), "codigo_material": (8, _texto),
            "descricao": (9, _texto), "natureza_subitem": (10, _texto),
            "marcador": (11, _texto), "gestao": (12, _texto),
            "unidade_medida": (13, _texto), "atualizado_em": (14, _ts14),
        },
    ),
    "NECD": dict(
        table="empenho_cronograma", kind="replace", parent="numero_ne",
        cols={
            "numero_ne": (2, _texto), "mes": (3, _inteiro),
            "valor": (4, _valor2), "valor_2": (5, _valor2),
            "gestao": (6, _texto), "atualizado_em": (7, _ts14),
        },
    ),
    "ANGR": dict(
        table="empenho_anulacoes", kind="upsert", key="numero_na",
        cols={
            "numero_na": (2, _texto), "numero_ne": (3, _texto),
            "criado_em": (4, _ts14), "valor": (5, _valor2),
            "motivo": (6, _texto), "gestao": (7, _texto),
            "atualizado_em": (8, _ts14),
        },
    ),
    "LE": dict(
        table="liquidacoes", kind="upsert", key="numero_le",
        cols={
            "numero_le": (2, _texto), "tipo": (3, _texto),
            "data_emissao": (4, _data8), "historico": (5, _texto),
            "data_referencia": (6, _data8), "numero_ne": (7, _texto),
            "valor": (8, _valor2), "gestao": (9, _texto),
            "atualizado_em": (10, _ts14), "numero_dh": (11, _texto),
        },
    ),
    "LEIT": dict(
        table="liquidacao_itens", kind="replace", parent="numero_le",
        cols={
            "numero_ne": (2, _texto), "numero_le": (3, _texto),
            "gestao": (5, _texto), "item_ne": (6, _texto),
            "natureza_subitem": (7, _texto),
            "valor_1": (8, _valor2), "valor_2": (9, _valor2),
            "valor_3": (10, _valor2), "valor_4": (11, _valor2),
            "valor_5": (12, _valor2), "atualizado_em": (13, _ts14),
            "codigo_material": (14, _texto), "descricao": (15, _texto),
            "unidade_medida": (16, _texto), "quantidade": (17, _valor2),
        },
    ),
    "ALDG": dict(
        table="liquidacao_anulacoes", kind="upsert", key="numero_al",
        cols={
            "numero_al": (2, _texto), "numero_le": (3, _texto),
            "tipo": (4, _texto), "data_anulacao": (5, _data8),
            "valor": (6, _valor2), "motivo": (7, _texto),
            "gestao": (8, _texto), "atualizado_em": (9, _ts14),
        },
    ),
    "ALCD": dict(
        table="liquidacao_anulacao_parcelas", kind="replace", parent="numero_al",
        cols={
            "numero_al": (2, _texto), "parcela": (3, _texto),
            "valor": (4, _valor2), "gestao": (5, _texto),
            "atualizado_em": (6, _ts14),
        },
    ),
    "OB": dict(
        table="ordens_bancarias", kind="upsert", key="numero_ob",
        const={"origem": "OB"},
        cols={
            "criado_em": (2, _ts14), "numero_ob": (3, _texto),
            "numero_ne": (4, _texto), "banco_origem": (5, _texto),
            "agencia_origem": (6, _texto), "conta_origem": (7, _texto),
            "credor_tipo": (8, _texto), "credor_doc": (9, _texto),
            "banco_destino": (10, _texto), "agencia_destino": (11, _texto),
            "conta_destino": (12, _texto), "data_emissao": (13, _data8),
            "tipo_ob": (14, _texto), "valor": (15, _valor2),
            "historico": (16, _texto), "status": (17, _texto),
            "numero_re": (18, _texto), "credor_nome": (20, _texto),
            "competencia": (21, _texto),
        },
    ),
    "OBS": dict(
        table="ordens_bancarias", kind="upsert", key="numero_ob",
        const={"origem": "OBS"},
        cols={
            "criado_em": (2, _ts14), "numero_ob": (3, _texto),
            "numero_ne": (4, _texto), "banco_origem": (5, _texto),
            "agencia_origem": (6, _texto), "conta_origem": (7, _texto),
            "credor_tipo": (8, _texto), "credor_doc": (9, _texto),
            "banco_destino": (10, _texto), "agencia_destino": (11, _texto),
            "conta_destino": (12, _texto), "data_emissao": (13, _data8),
            "tipo_ob": (14, _texto), "valor": (15, _valor2),
            "historico": (16, _texto), "credor_nome": (17, _texto),
            "status": (18, _texto),
        },
    ),
    "RN": dict(
        table="retencoes", kind="upsert", key="numero_rn",
        cols={
            "numero_rn": (2, _texto), "numero_ob": (3, _texto),
            "numero_ne": (4, _texto), "data_emissao": (5, _data8),
            "valor": (6, _valor2), "historico": (7, _texto),
            "ug_registro": (8, _texto), "status": (9, _texto),
            "tipo": (10, _texto), "data_pagamento": (11, _data8),
            "gestao": (12, _texto), "exercicio": (13, _inteiro),
        },
    ),
    "GR": dict(
        table="guias_recolhimento", kind="upsert", key="numero_gr",
        cols={
            "numero_gr": (2, _texto), "banco": (4, _texto),
            "agencia": (5, _texto), "conta": (6, _texto),
            "data_emissao": (7, _data8), "valor": (8, _valor2),
            "historico": (9, _texto), "ug_registro": (10, _texto),
            "tipo_receita": (11, _texto), "contribuinte_tipo": (12, _texto),
            "contribuinte_doc": (13, _texto), "data_referencia": (15, _data8),
            "gestao": (16, _texto), "exercicio": (19, _inteiro),
            "status": (21, _texto),
        },
    ),
}

# ---------------------------------------------------------------------------
# Parse dos arquivos
# ---------------------------------------------------------------------------

class Arquivo:
    """Um TXT do e-Fisco já parseado (header, registros e trailer)."""

    def __init__(self, nome, tipo, ug, conteudo: bytes):
        self.nome = nome
        self.tipo = tipo
        self.ug = ug
        self.hash = hashlib.sha256(conteudo).hexdigest()
        self.avisos = []
        self.gerado_em = None
        self.registros = []

        texto = conteudo.decode(ENCODING)
        total_trailer = None
        for linha in texto.splitlines():
            if not linha.strip():
                continue
            partes = linha.split(DELIM)
            # alguns trailers/headers vêm com um campo vazio na frente
            if partes and partes[0] == "" and len(partes) > 1 and partes[1] in ("0", "9"):
                partes = partes[1:]
            if not partes:
                continue
            if partes[0] == "1":
                self.registros.append(partes)
            elif partes[0] == "0":
                self.gerado_em = self._ts_header(partes)
            elif partes[0] == "9":
                if len(partes) > 4 and partes[4].strip().isdigit():
                    total_trailer = int(partes[4])
                if self.gerado_em is None:
                    self.gerado_em = self._ts_header(partes)

        if self.gerado_em is None:
            raise ValueError("cabeçalho sem data de geração")
        if total_trailer is not None and total_trailer != len(self.registros):
            self.avisos.append(
                f"trailer indica {total_trailer} registros, arquivo tem {len(self.registros)}"
            )

    @staticmethod
    def _ts_header(partes):
        # partes[1] = ex. "GFU_070002_NEDG_202506170103"
        m = re.search(r"_(\d{12})$", partes[1].strip())
        if m:
            return datetime.strptime(m.group(1), "%Y%m%d%H%M")
        return None


def coletar_arquivos(caminhos):
    """Percorre zips/pastas/arquivos e devolve lista de Arquivo + ignorados."""
    arquivos, ignorados = [], []

    def considerar(nome, conteudo_fn):
        base = os.path.basename(nome)
        m = FILENAME_RE.search(base)
        if not m:
            if base.upper().startswith("TJPE.") and base.upper().endswith(".TXT"):
                ignorados.append(nome)
            return
        tipo = m.group(3).upper()
        ug = m.group(2)
        try:
            arquivos.append(Arquivo(nome, tipo, ug, conteudo_fn()))
        except ValueError as e:
            print(f"  [ERRO] {nome}: {e}", file=sys.stderr)

    for caminho in caminhos:
        p = Path(caminho)
        if not p.exists():
            print(f"[ERRO] caminho não existe: {p}", file=sys.stderr)
            continue
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    if f.suffix.lower() == ".zip":
                        _coletar_zip(f, considerar)
                    else:
                        considerar(str(f), lambda f=f: f.read_bytes())
        elif p.suffix.lower() == ".zip":
            _coletar_zip(p, considerar)
        else:
            considerar(str(p), lambda p=p: p.read_bytes())

    # ordena pela data de geração: snapshots antigos entram antes dos novos
    arquivos.sort(key=lambda a: (a.gerado_em, a.ug, a.tipo))
    return arquivos, ignorados


def _coletar_zip(caminho, considerar):
    with zipfile.ZipFile(caminho) as z:
        for info in sorted(z.infolist(), key=lambda i: i.filename):
            if info.is_dir():
                continue
            nome = f"{caminho}::{info.filename}"
            considerar(nome, lambda z=z, i=info: z.read(i))

# ---------------------------------------------------------------------------
# Carga no banco
# ---------------------------------------------------------------------------

BATCH = 500

def montar_linha(layout, ug, partes, snapshot, arquivo_id):
    linha = {"unidade_gestora": ug, "snapshot_em": snapshot, "arquivo_id": arquivo_id}
    linha.update(layout.get("const", {}))
    n = len(partes)
    for col, (pos, conv) in layout["cols"].items():
        bruto = partes[pos - 1] if pos <= n else ""
        linha[col] = conv(bruto)
    linha["campos"] = Jsonb(partes) if Jsonb else None
    return linha


def sql_upsert(layout):
    tabela = layout["table"]
    cols = ["unidade_gestora", *layout.get("const", {}), *layout["cols"],
            "campos", "snapshot_em", "arquivo_id"]
    pk = ["unidade_gestora", layout["key"]]
    sets = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in pk)
    return (
        f"insert into {tabela} ({', '.join(cols)}) "
        f"values ({', '.join('%(' + c + ')s' for c in cols)}) "
        f"on conflict ({', '.join(pk)}) do update set {sets} "
        f"where {tabela}.snapshot_em <= excluded.snapshot_em"
    ), cols


def carregar_upsert(cur, layout, arq, arquivo_id, stats):
    query, _ = sql_upsert(layout)
    chave_pos = layout["cols"][layout["key"]][0]
    # dentro de um mesmo arquivo a chave é única; por garantia, fica a última
    por_chave = {}
    for partes in arq.registros:
        por_chave[partes[chave_pos - 1]] = partes
    linhas = [montar_linha(layout, arq.ug, p, arq.gerado_em, arquivo_id)
              for p in por_chave.values()]
    afetadas = 0
    for i in range(0, len(linhas), BATCH):
        cur.executemany(query, linhas[i:i + BATCH])
        afetadas += max(cur.rowcount, 0)
    stats["gravados"] += afetadas
    stats["ignorados"] += len(linhas) - afetadas


def carregar_replace(cur, layout, arq, arquivo_id, stats):
    tabela = layout["table"]
    parent = layout["parent"]
    parent_pos = layout["cols"][parent][0]

    grupos = {}  # valor do pai -> [partes...] na ordem do arquivo
    for partes in arq.registros:
        grupos.setdefault(partes[parent_pos - 1], []).append(partes)
    if not grupos:
        return

    pais = list(grupos)
    cur.execute(
        f"select {parent}, max(snapshot_em) from {tabela} "
        f"where unidade_gestora = %s and {parent} = any(%s) group by {parent}",
        (arq.ug, pais),
    )
    existentes = dict(cur.fetchall())
    aceitos = [p for p in pais
               if p not in existentes or existentes[p] <= arq.gerado_em]
    if not aceitos:
        stats["ignorados"] += len(arq.registros)
        return

    cur.execute(
        f"delete from {tabela} where unidade_gestora = %s and {parent} = any(%s)",
        (arq.ug, aceitos),
    )
    cols = ["unidade_gestora", *layout.get("const", {}), *layout["cols"],
            "seq", "campos", "snapshot_em", "arquivo_id"]
    query = (
        f"insert into {tabela} ({', '.join(cols)}) "
        f"values ({', '.join('%(' + c + ')s' for c in cols)})"
    )
    linhas = []
    for pai in aceitos:
        for seq, partes in enumerate(grupos[pai], start=1):
            linha = montar_linha(layout, arq.ug, partes, arq.gerado_em, arquivo_id)
            linha["seq"] = seq
            linhas.append(linha)
    for i in range(0, len(linhas), BATCH):
        cur.executemany(query, linhas[i:i + BATCH])
    stats["gravados"] += len(linhas)
    stats["ignorados"] += len(arq.registros) - len(linhas)


def processar(conn, arquivos, dry_run=False):
    total = {"arquivos": 0, "pulados": 0, "gravados": 0, "ignorados": 0}
    for arq in arquivos:
        rotulo = f"{arq.tipo:<4} UG {arq.ug} {arq.gerado_em:%d/%m/%Y %H:%M}"
        for aviso in arq.avisos:
            print(f"  [AVISO] {rotulo}: {aviso}")
        if dry_run:
            print(f"  [DRY]  {rotulo}: {len(arq.registros)} registros ({arq.nome})")
            total["arquivos"] += 1
            continue
        with conn.transaction():
            cur = conn.cursor()
            cur.execute(
                "select 1 from arquivos_processados where hash_conteudo = %s",
                (arq.hash,),
            )
            if cur.fetchone():
                total["pulados"] += 1
                continue
            cur.execute(
                "insert into arquivos_processados "
                "(nome_arquivo, tipo, unidade_gestora, gerado_em, registros, hash_conteudo) "
                "values (%s, %s, %s, %s, %s, %s) returning id",
                (arq.nome, arq.tipo, arq.ug, arq.gerado_em,
                 len(arq.registros), arq.hash),
            )
            arquivo_id = cur.fetchone()[0]
            layout = LAYOUTS[arq.tipo]
            stats = {"gravados": 0, "ignorados": 0}
            if layout["kind"] == "upsert":
                carregar_upsert(cur, layout, arq, arquivo_id, stats)
            else:
                carregar_replace(cur, layout, arq, arquivo_id, stats)
            total["arquivos"] += 1
            total["gravados"] += stats["gravados"]
            total["ignorados"] += stats["ignorados"]
            print(f"  [OK]   {rotulo}: {len(arq.registros)} registros "
                  f"({stats['gravados']} gravados, {stats['ignorados']} já atualizados)")
    return total


def main():
    ap = argparse.ArgumentParser(
        description="Carga dos zips diários de empenhos (e-Fisco/TJPE) para o Supabase."
    )
    ap.add_argument("caminhos", nargs="+",
                    help="zip(s) diário(s), pasta(s) ou TXT(s) individuais")
    ap.add_argument("--db-url", default=None,
                    help="URL do Postgres (padrão: SUPABASE_DB_URL ou DATABASE_URL)")
    ap.add_argument("--dry-run", action="store_true",
                    help="só analisa os arquivos, sem gravar no banco")
    args = ap.parse_args()

    # carrega .env simples, se existir (sem depender de python-dotenv)
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for linha in env_path.read_text().splitlines():
            linha = linha.strip()
            if linha and not linha.startswith("#") and "=" in linha:
                chave, _, valor = linha.partition("=")
                os.environ.setdefault(chave.strip(), valor.strip())

    print("Coletando arquivos...")
    arquivos, ignorados = coletar_arquivos(args.caminhos)
    print(f"  {len(arquivos)} arquivos reconhecidos, {len(ignorados)} ignorados "
          f"(CTB/PLF/PLO e outros fora do escopo de empenhos)")

    if args.dry_run:
        total = processar(None, arquivos, dry_run=True)
        print(f"\nDry-run: {total['arquivos']} arquivos analisados.")
        return

    db_url = args.db_url or os.environ.get("SUPABASE_DB_URL") \
        or os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("Defina SUPABASE_DB_URL (ou use --db-url). Veja .env.example.")
    if psycopg is None:
        sys.exit("Instale as dependências: pip install -r ingest/requirements.txt")

    import schema
    with psycopg.connect(db_url) as conn:
        schema.garantir_schema(conn)
        total = processar(conn, arquivos)

    print(
        f"\nConcluído: {total['arquivos']} arquivos carregados, "
        f"{total['pulados']} pulados (já processados), "
        f"{total['gravados']} registros gravados, "
        f"{total['ignorados']} descartados por já haver versão mais nova."
    )


if __name__ == "__main__":
    main()
