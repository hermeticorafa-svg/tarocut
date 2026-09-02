#!/usr/bin/env python3
"""Gera lexicon/terms.txt a partir de material/.

Le os nomes das cartas do zip do baralho e o texto do guia em PDF.
Rode de novo sempre que o material mudar:

    venv\\Scripts\\python.exe lexico.py
"""
import re, sys, zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent
MAT = ROOT / "material"

# Erros que o Whisper realmente cometeu nos videos do curso.
# Va acrescentando conforme aparecerem novos.
ERROS = {
    "Tarô de Yala": ["tarot de ala", "Tarot de Alas", "Tarou Diálamas",
                     "taro de iala", "tarô de ala", "tarot de yala",
                     "Tarô de Alas", "tarodiala"],
    "naipes": ["snipes", "knipe", "snipers", "nipes", "knipes"],
    # "páginas" e palavra real (de livro), mas neste curso, numa lista de
    # cartas, ela sempre quer dizer "pajens". Se algum video falar de
    # paginas do guia de verdade, tire daqui.
    "pajens": ["páginos", "pages", "paginos", "pajés", "páginas"],
    # NAO mapear "as" -> "Ás": "as" e artigo comum e o corretor trocaria
    # o texto inteiro. Ver STOP abaixo.
    "Waite-Smith": ["Wait Smith", "Waite Smith", "Uêit Smith", "Weit Smith"],
    "Tarô de Marselha": ["Tarot de Marcellia", "Tarou de Marçélia",
                         "Taro de Marcelia", "Tarot de Marselha",
                         "Tarô de Marçélia"],
    "Rafael Trabasso": ["Rafael Trabaço", "Rafael Trabasse"],
    "Mariana Pio": ["Mariana Pio", "Mariana Pião"],
    "arcanos": ["arcanus", "arcamos"],
    "arcano": ["arcanho"],
    "deck": ["DEC", "deque"],
    # vistos no IMG_0248
    "Tarô": ["tarot"],
    "tarôs": ["tarots"],
    "filosofia ocultista": ["filosofia cultista"],
    "não presta": ["no presta"],
    "se propor": ["se propô"],
}

# Nomes de arquivo do baralho vem sem acento em alguns casos.
ACENTO = {"A Justica": "A Justiça"}

# Rede de seguranca: uma palavra comum do portugues NUNCA pode virar
# "erro conhecido", senao o corretor reescreve o texto inteiro. Foi o que
# quase aconteceu com "as" -> "Ás".
STOP = {
    "a", "as", "o", "os", "e", "é", "de", "da", "do", "das", "dos", "em",
    "no", "na", "nos", "nas", "um", "uma", "uns", "umas", "por", "para",
    "com", "sem", "que", "se", "ao", "aos", "à", "às", "mas", "ou", "ja",
    "já", "so", "só", "tem", "ser", "sao", "são", "foi", "vai", "pode",
    "mais", "menos", "muito", "bem", "aqui", "ali", "isso", "esse", "essa",
    "rei", "reis", "carta", "cartas", "paus", "copas", "ouros", "espadas",
}


def seguro(erro):
    return erro.strip().lower() not in STOP and len(erro.strip()) > 2

# Vocabulario geral do curso, em ordem de prioridade: o initial_prompt do
# Whisper e curto (~224 tokens), entao os primeiros importam mais.
PRIORIDADE = [
    "Tarô de Yala", "Mariana Pio", "Rafael Trabasso",
    "Arcanos Maiores", "Arcanos Menores", "arcano", "naipes",
    "Copas", "Espadas", "Ouros", "Paus", "Bastões", "Pentáculos",
    "Rainha", "Rei", "Cavaleiro", "Cavaleira", "Pajem", "pajens",
    "cartas da corte", "cartas numeradas", "Ás",
    "Tarô de Marselha", "Waite-Smith", "Rider-Waite",
    "tiragem", "baralho", "deck", "oráculo", "tradição ocultista",
    # Daqui para baixo entram no CORRETOR mas ficam fora do initial_prompt
    # (que usa so os 30 primeiros). Um erro conhecido so e escrito no
    # terms.txt se o termo correto estiver em alguma lista - foi por faltar
    # "Tarô" sozinho aqui que o mapa "tarot -> Tarô" nunca saiu do papel.
    "Tarô", "tarôs", "arcanos", "filosofia ocultista", "não presta",
    "se propor",
]


def nomes_das_cartas():
    z = MAT / "tarodeyala.zip"
    if not z.exists():
        return []
    saida = []
    with zipfile.ZipFile(z) as f:
        for i in f.infolist():
            if not i.filename.lower().endswith(".jpg"):
                continue
            n = i.filename
            if not (i.flag_bits & 0x800):          # nome em cp437
                try:
                    n = n.encode("cp437").decode("utf-8")
                except Exception:
                    pass
            n = n.split("/")[-1].rsplit(".", 1)[0].strip()
            n = re.sub(r"^\d+\s*-\s*", "", n).strip()   # tira "03 - "
            if n:
                saida.append(n)
    return saida


def titula(s):
    """'a roda da fortuna' -> 'A Roda da Fortuna'"""
    peq = {"de", "da", "do", "das", "dos", "e"}
    ps = s.split()
    return " ".join(p if i and p in peq else p.capitalize()
                    for i, p in enumerate(ps))


def main():
    cartas = nomes_das_cartas()
    # "cavaleir\b" nao pega "cavaleira": precisa de [oa]
    RE_CORTE = r"^(rainha|rei|cavaleir[oa]s?|pajens|pajem)\b"
    RE_NUM = r"^(\d+|as)\b"
    tit = [ACENTO.get(titula(c), titula(c)) for c in cartas]
    maiores = [t for t, c in zip(tit, cartas)
               if not re.match(RE_CORTE, c) and not re.match(RE_NUM, c)]
    corte = [t for t, c in zip(tit, cartas) if re.match(RE_CORTE, c)]
    numeradas = [t for t, c in zip(tit, cartas) if re.match(RE_NUM, c)]

    L = ["# GERADO por lexico.py a partir de material/.",
         "# Nao edite a mao: rode 'python lexico.py' de novo.",
         "# Formato:  CORRETO | erro1 | erro2",
         "#",
         "# Alimenta o initial_prompt do Whisper (os primeiros ~30 termos)",
         "# e o corretor de transcricao (a lista toda).",
         ""]

    def bloco(titulo, itens):
        if not itens:
            return
        L.append(f"# --- {titulo} ---")
        for t in itens:
            errs = [e for e in ERROS.get(t, [])
                    if seguro(e) and e.strip().lower() != t.strip().lower()]
            L.append(t + "".join(f" | {e}" for e in errs))
        L.append("")

    bloco("vocabulario do curso (prioridade no prompt)", PRIORIDADE)
    bloco(f"arcanos maiores ({len(maiores)})", sorted(set(maiores)))
    bloco(f"cartas da corte ({len(corte)})", sorted(set(corte)))
    bloco(f"cartas numeradas ({len(numeradas)})", sorted(set(numeradas)))

    alvo = ROOT / "lexicon" / "terms.txt"
    alvo.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"maiores={len(set(maiores))} corte={len(set(corte))} "
          f"numeradas={len(set(numeradas))} prioridade={len(PRIORIDADE)}")
    print(f"-> {alvo}")


if __name__ == "__main__":
    main()
