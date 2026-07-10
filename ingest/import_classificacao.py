#!/usr/bin/env python3
"""
Importa a planilha "Descrição das Ações e Subações" para a tabela
classificacao_orcamentaria do Supabase.

Uso:
    python ingest/import_classificacao.py caminho/da/planilha.xlsx

A tabela é a fonte de consulta dos setores (subação virtual). A planilha é
a fonte da verdade, então a importação recarrega a tabela por completo
(esvazia e insere tudo de novo) dentro de uma transação.

Colunas esperadas na planilha (a partir de um cabeçalho "Fonte | Ação |
Subação Real | Subação Virtual | Descrição"):
    Fonte           ex.: "0759 - FERM - TJPE"
    Ação            ex.: 4430
    Subação Real    ex.: 1439, A585, 0000
    Subação Virtual ex.: A594
    Descrição       ex.: Assessoria de Comunicação
"""

import argparse
import os
import sys
from pathlib import Path

try:
    import openpyxl
except ImportError:
    openpyxl = None

try:
    import psycopg
except ImportError:
    psycopg = None


def code(v):
    """Normaliza um código: inteiro vira string de 4 dígitos com zeros à
    esquerda; texto (ex.: 'A585') fica como está."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return f"{v:04d}"
    if isinstance(v, float) and v.is_integer():
        return f"{int(v):04d}"
    return str(v).strip() or None


def separar_fonte(valor):
    """'0759 - FERM - TJPE' -> ('0759', 'FERM - TJPE')."""
    if valor is None:
        return None, None
    partes = str(valor).split(" - ")
    codigo = partes[0].strip()
    nome = " - ".join(p.strip() for p in partes[1:]) or None
    return codigo, nome


def ler_planilha(caminho):
    """Lê as linhas de dados da planilha, localizando o cabeçalho."""
    wb = openpyxl.load_workbook(caminho, data_only=True)
    ws = wb.active
    linhas = []
    cabecalho_visto = False
    for row in ws.iter_rows(values_only=True):
        celulas = [c for c in row if c is not None]
        if not celulas:
            continue
        primeiro = str(row[0]).strip().lower() if row[0] is not None else ""
        if not cabecalho_visto:
            if primeiro == "fonte":
                cabecalho_visto = True
            continue
        fonte, acao, sub_real, sub_virt, descricao = (list(row) + [None] * 5)[:5]
        fonte_codigo, fonte_nome = separar_fonte(fonte)
        if not fonte_codigo or acao is None:
            continue
        linhas.append({
            "fonte_codigo": fonte_codigo,
            "fonte_nome": fonte_nome,
            "acao": code(acao),
            "subacao_real": code(sub_real),
            "subacao_virtual": code(sub_virt),
            "descricao": str(descricao).strip() if descricao is not None else None,
        })
    return linhas


def importar_classificacao(conn, caminho):
    """Recarrega a tabela classificacao_orcamentaria a partir da planilha.
    Devolve a quantidade de linhas importadas."""
    if openpyxl is None:
        raise RuntimeError("Instale a dependência: pip install openpyxl")
    linhas = ler_planilha(caminho)
    if not linhas:
        raise RuntimeError("Nenhuma linha de dados encontrada na planilha.")
    cols = ["fonte_codigo", "fonte_nome", "acao", "subacao_real",
            "subacao_virtual", "descricao"]
    query = (f"insert into classificacao_orcamentaria ({', '.join(cols)}) "
             f"values ({', '.join('%(' + c + ')s' for c in cols)})")
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("truncate classificacao_orcamentaria restart identity")
        cur.executemany(query, linhas)
    return len(linhas)


def main():
    ap = argparse.ArgumentParser(
        description="Importa a planilha de Ações/Subações para o Supabase.")
    ap.add_argument("planilha", help="arquivo .xlsx da classificação")
    ap.add_argument("--db-url", default=None,
                    help="URL do Postgres (padrão: SUPABASE_DB_URL ou DATABASE_URL)")
    args = ap.parse_args()

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for linha in env_path.read_text().splitlines():
            linha = linha.strip()
            if linha and not linha.startswith("#") and "=" in linha:
                chave, _, valor = linha.partition("=")
                os.environ.setdefault(chave.strip(), valor.strip())

    db_url = args.db_url or os.environ.get("SUPABASE_DB_URL") \
        or os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("Defina SUPABASE_DB_URL (ou use --db-url).")
    if psycopg is None:
        sys.exit("Instale as dependências: pip install -r ingest/requirements.txt")

    import schema
    with psycopg.connect(db_url) as conn:
        schema.garantir_schema(conn)
        n = importar_classificacao(conn, args.planilha)
    print(f"Classificação importada: {n} linhas em classificacao_orcamentaria.")


if __name__ == "__main__":
    main()
