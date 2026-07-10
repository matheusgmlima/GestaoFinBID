#!/usr/bin/env python3
"""
Garante que o schema do banco existe, aplicando as migrações .sql.

As migrações são idempotentes (create table if not exists, add column if not
exists, create or replace view), então podem ser executadas a cada conexão
sem risco — é como o programa cria a estrutura sozinho na primeira vez e a
mantém em dia depois.
"""

import sys
from pathlib import Path


def _dir_migrations():
    """Localiza a pasta de migrações tanto em desenvolvimento quanto dentro
    do executável empacotado (PyInstaller descompacta os dados em _MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        d = Path(base) / "migrations"
        if d.is_dir():
            return d
    return Path(__file__).resolve().parent.parent / "supabase" / "migrations"


def garantir_schema(conn):
    """Aplica, em ordem, todas as migrações .sql. Devolve a lista de arquivos
    aplicados. Cada arquivo roda na sua própria transação."""
    pasta = _dir_migrations()
    arquivos = sorted(pasta.glob("*.sql"))
    if not arquivos:
        raise RuntimeError(f"Nenhuma migração encontrada em {pasta}")
    aplicadas = []
    for f in arquivos:
        sql = f.read_text(encoding="utf-8")
        with conn.transaction():
            conn.execute(sql)
        aplicadas.append(f.name)
    return aplicadas
