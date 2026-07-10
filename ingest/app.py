#!/usr/bin/env python3
"""
Interface gráfica simples para carregar os zips diários de empenhos no Supabase.

Uso sem instalar nada além do Python:  python ingest/app.py
Ou gere o executável Windows pelo workflow "Gerar executável" no GitHub Actions.
"""

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext

import ingest
import import_classificacao
import schema

# a connection string fica salva no perfil do usuário após a primeira carga
CONFIG = Path.home() / ".gestaofinbid.conf"


def carregar_url_salva():
    try:
        return CONFIG.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def salvar_url(url):
    try:
        CONFIG.write_text(url.strip() + "\n", encoding="utf-8")
    except OSError:
        pass


class EscritorFila:
    """Redireciona os prints do pipeline para a caixa de log da janela."""

    def __init__(self, fila):
        self.fila = fila

    def write(self, texto):
        if texto:
            self.fila.put(texto)

    def flush(self):
        pass


class App:
    def __init__(self, root):
        self.root = root
        root.title("GestaoFinBID — Carga de empenhos no Supabase")
        root.geometry("820x560")

        topo = tk.Frame(root, padx=10, pady=8)
        topo.pack(fill="x")
        tk.Label(topo, text="Conexão com o Supabase (connection string):").pack(anchor="w")
        self.url = tk.Entry(topo)
        self.url.insert(0, carregar_url_salva())
        self.url.pack(fill="x", pady=(2, 6))
        tk.Label(
            topo, fg="#666",
            text="No painel do Supabase: Connect → Session pooler → URI "
                 "(postgresql://postgres...). Fica salva para as próximas cargas.",
        ).pack(anchor="w")

        botoes = tk.Frame(root, padx=10, pady=4)
        botoes.pack(fill="x")
        self.btn_zips = tk.Button(botoes, text="Selecionar zips e carregar...",
                                  command=self.escolher_zips, height=2)
        self.btn_zips.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.btn_pasta = tk.Button(botoes, text="Selecionar pasta inteira...",
                                   command=self.escolher_pasta, height=2)
        self.btn_pasta.pack(side="left", expand=True, fill="x", padx=(4, 0))

        extra = tk.Frame(root, padx=10)
        extra.pack(fill="x", pady=(0, 4))
        self.btn_planilha = tk.Button(
            extra, text="Atualizar planilha de Ações/Subações (classificação)...",
            command=self.escolher_planilha)
        self.btn_planilha.pack(fill="x")

        self.log = scrolledtext.ScrolledText(root, state="disabled", height=20,
                                             font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        self.fila = queue.Queue()
        self.rodando = False
        self.root.after(100, self.despejar_fila)

    # ------------------------------------------------------------------
    def escolher_zips(self):
        caminhos = filedialog.askopenfilenames(
            title="Selecione o(s) zip(s) diário(s)",
            filetypes=[("Zips do e-Fisco", "*.zip"), ("Todos os arquivos", "*.*")],
        )
        if caminhos:
            self.iniciar(list(caminhos))

    def escolher_pasta(self):
        pasta = filedialog.askdirectory(title="Selecione a pasta com os zips")
        if pasta:
            self.iniciar([pasta])

    def escolher_planilha(self):
        caminho = filedialog.askopenfilename(
            title="Selecione a planilha de Ações/Subações",
            filetypes=[("Planilha Excel", "*.xlsx"), ("Todos os arquivos", "*.*")],
        )
        if caminho:
            self.iniciar_planilha(caminho)

    def _preparar(self):
        """Valida a conexão e bloqueia os botões. Devolve a URL ou None."""
        if self.rodando:
            return None
        url = self.url.get().strip()
        if not url.startswith("postgres"):
            messagebox.showwarning(
                "Conexão", "Preencha a connection string do Supabase primeiro\n"
                "(painel do Supabase → Connect → Session pooler → URI).")
            return None
        if ingest.psycopg is None:
            messagebox.showerror("Dependência", "O driver do banco (psycopg) não está instalado.")
            return None
        salvar_url(url)
        self.rodando = True
        for b in (self.btn_zips, self.btn_pasta, self.btn_planilha):
            b.config(state="disabled")
        self.limpar_log()
        return url

    def iniciar(self, caminhos):
        url = self._preparar()
        if url:
            threading.Thread(target=self.trabalhar, args=(url, caminhos), daemon=True).start()

    def iniciar_planilha(self, caminho):
        url = self._preparar()
        if url:
            threading.Thread(target=self.trabalhar_planilha, args=(url, caminho),
                             daemon=True).start()

    def trabalhar_planilha(self, url, caminho):
        saida = EscritorFila(self.fila)
        sys.stdout = saida
        sys.stderr = saida
        try:
            print(f"Lendo a planilha de classificação...\n  {caminho}\n")
            print("Conectando ao Supabase...")
            with ingest.psycopg.connect(url) as conn:
                print("Verificando a estrutura do banco...")
                schema.garantir_schema(conn)
                n = import_classificacao.importar_classificacao(conn, caminho)
            msg = (f"Planilha de classificação atualizada!\n\n"
                   f"{n} linhas carregadas em classificacao_orcamentaria.")
            print("\n" + msg)
            self.fila.put(("fim", msg))
        except Exception as e:  # noqa: BLE001
            print(f"\n[ERRO] {e}")
            self.fila.put(("erro", f"A importação da planilha falhou:\n\n{e}"))
        finally:
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__

    def trabalhar(self, url, caminhos):
        saida = EscritorFila(self.fila)
        sys.stdout = saida
        sys.stderr = saida
        try:
            print("Analisando os arquivos selecionados...")
            arquivos, ignorados = ingest.coletar_arquivos(caminhos)
            print(f"  {len(arquivos)} arquivos de empenhos reconhecidos "
                  f"({len(ignorados)} fora do escopo, ignorados)\n")
            if not arquivos:
                self.fila.put(("fim", "Nenhum arquivo de empenhos encontrado na seleção."))
                return
            print("Conectando ao Supabase...")
            with ingest.psycopg.connect(url) as conn:
                print("Verificando a estrutura do banco...")
                schema.garantir_schema(conn)
                total = ingest.processar(conn, arquivos)
            resumo = (f"Concluído!\n\n"
                      f"Arquivos carregados: {total['arquivos']}\n"
                      f"Já processados antes (pulados): {total['pulados']}\n"
                      f"Registros gravados/atualizados: {total['gravados']}\n"
                      f"Descartados por já haver versão mais nova: {total['ignorados']}")
            print("\n" + resumo)
            self.fila.put(("fim", resumo))
        except Exception as e:  # noqa: BLE001 — mostra qualquer erro ao usuário
            print(f"\n[ERRO] {e}")
            self.fila.put(("erro", f"A carga falhou:\n\n{e}"))
        finally:
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__

    # ------------------------------------------------------------------
    def despejar_fila(self):
        try:
            while True:
                item = self.fila.get_nowait()
                if isinstance(item, tuple):
                    tipo, msg = item
                    self.rodando = False
                    for b in (self.btn_zips, self.btn_pasta, self.btn_planilha):
                        b.config(state="normal")
                    if tipo == "erro":
                        messagebox.showerror("GestaoFinBID", msg)
                    else:
                        messagebox.showinfo("GestaoFinBID", msg)
                else:
                    self.escrever_log(item)
        except queue.Empty:
            pass
        self.root.after(100, self.despejar_fila)

    def escrever_log(self, texto):
        self.log.config(state="normal")
        self.log.insert("end", texto)
        self.log.see("end")
        self.log.config(state="disabled")

    def limpar_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
