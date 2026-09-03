#!/usr/bin/env python3
"""baralho.py - catalogo do baralho e casador de mencao de carta na fala.

Por que existe
--------------
"Adicionar cartas" precisa saber duas coisas a partir de um so lugar: o
nome de arquivo em `baralho completo/tarodeyala/`. Dele sai a imagem para
o overlay E a sequencia de palavras que, ditas juntas, significam aquela
carta - as duas vem do mesmo nome de proposito, para nunca discordarem
entre si.

O casador usa o mesmo truque de `corrige_frases()` (tarocut.py): compara
tuplas de `chave()` contra os tokens seguintes, a mais comprida primeiro,
para uma chave de 3 palavras nao perder para uma de 1 que comeca igual. E
o mesmo problema que `classe_num()`/`protege_enumeracoes()` (tarocut.py)
ja resolvem para a contagem de cartas ("As, dois, tres... dez"): o "As"
sozinho e artigo comum, so vira carta com o naipe do lado - por isso toda
carta numerada exige a tupla completa (numero, "de", naipe), nunca so o
numero.

Fica fora de tarocut.py de proposito, como alinhar.py e lexico.py: modulo
standalone, mesma razao dos dois (roda sem carregar o resto do pipeline,
e testavel sozinho). tarocut.py importa isto de dentro do comando que
precisar, do jeito que ja faz com `import alinhar`.

Risco conhecido, para o passe de revisao (checkpoint 1 do plano): as
chaves dos Arcanos Maiores sao 1-2 palavras de portugues comum ("o carro",
"o mundo", "a torre") - podem casar uma frase que nao fala da carta
nenhuma. Por isso a chave leva o artigo (reduz falso positivo, custa
recall quando ela omite o artigo) e por isso este modulo so ACHA menção;
quem decide se a menção vira exibição de carta e o comando `cartas`, que
imprime tudo para conferência antes de gerar video.
"""
import re, unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BARALHO = ROOT / "baralho completo" / "tarodeyala"


def sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def chave(s):
    """Igual a tarocut.chave() - duplicada, nao importada: ver nota do
    modulo sobre por que fica standalone."""
    return sem_acento(s).lower().strip(" .,!?;:\"'()[]…-")


# Nome de arquivo -> nome falado do naipe (o filesystem usa so o plural).
NAIPES = {"copas": "Copas", "espadas": "Espadas", "ouros": "Ouros",
          "paus": "Paus"}

# Numero de arquivo ("2".."10", "as") -> formas faladas equivalentes. O
# Whisper transcreve numero por EXTENSO (RE_NUM em tarocut.py casa "dois",
# "tres" etc., no digito) - o digito entra tambem, de graca, para o dia em
# que ele decidir escrever "7" em vez de "sete".
NUM_ALT = {
    "as": ["as"],
    "2": ["dois", "duas", "2"], "3": ["tres", "3"], "4": ["quatro", "4"],
    "5": ["cinco", "5"], "6": ["seis", "6"], "7": ["sete", "7"],
    "8": ["oito", "8"], "9": ["nove", "9"], "10": ["dez", "10"],
}

# Titulo de arquivo -> formas faladas equivalentes. So "pajens" muda: o
# arquivo esta no plural (convencao do material do Yala) mas a fala e no
# singular - "pajem de espadas", nunca "pajens de espadas".
CORTE_ALT = {"cavaleira": ["cavaleira"], "cavaleiro": ["cavaleiro"],
             "pajens": ["pajem", "pajens"], "rainha": ["rainha"],
             "rei": ["rei"]}

RE_MENOR = re.compile(r"^(as|10|[2-9]) de (copas|espadas|ouros|paus)$")
RE_CORTE = re.compile(
    r"^(cavaleira|cavaleiro|pajens|rainha|rei) de (copas|espadas|ouros|paus)$")
RE_MAIOR = re.compile(r"^\d+\s*-\s*(.+)$")

# Mencao de CATEGORIA - "as rainhas", "os reis", "os cavaleiros e
# cavaleiras", "os pajens" - sem dizer o naipe. Comum quando Mariana fala
# de varias cartas JUNTAS (tipo de carta, nao carta especifica) - o video
# de teste deste modo (IMG_0246) enumera as quatro cartas da corte desse
# jeito o video inteiro e nunca diz um naipe.
#
# Cada categoria aponta para a chave de UMA carta representante do
# catalogo (a primeira em ordem alfabetica de arquivo, que por acaso e
# sempre "de copas" quando existe, e "de espadas" para cavaleira, que nao
# tem copas no baralho do Yala) - sempre a MESMA carta, de proposito: se a
# representante mudasse a cada mencao, `_agrupa_spans_cartas` veria
# "cartas" diferentes a cada vez e abriria/fecharia o overlay em vez de
# fundir mencoes seguidas da mesma categoria.
CATEGORIA_ALT = {
    "cavaleira": ["cavaleira", "cavaleiras"],
    "cavaleiro": ["cavaleiro", "cavaleiros"],
    "pajens": ["pajem", "pajens"],
    "rainha": ["rainha", "rainhas"],
    "rei": ["rei", "reis"],
}


def catalogo():
    """Le baralho completo/tarodeyala/ e devolve uma carta por arquivo:

        {"nome": nome de exibicao, "img": Path, "chaves": [tupla, ...]}

    Uma carta pode ter mais de uma chave (numero por extenso E em digito,
    "pajem" E "pajens") porque a fala tem mais de um jeito certo de dizer
    a mesma carta; a lista existe para `acha_mencoes` casar qualquer um
    deles. A carta representante de cada titulo de corte ganha tambem as
    chaves de CATEGORIA_ALT, de um so token (sem "de <naipe>") - ver nota
    acima do dict."""
    cartas = []
    representante = {}  # titulo ("rainha" etc.) -> indice em `cartas`
    for p in sorted(BARALHO.glob("*.jpg")):
        stem = chave(p.stem)  # chave() so baixa e tira acento/pontuacao;
        # nao precisa reimplementar o parsing por causa disso, os nomes de
        # arquivo ja vem em minusculo e sem acentos problematicos.

        m = RE_MENOR.match(stem)
        if m:
            num, naipe_arq = m.group(1), m.group(2)
            naipe = NAIPES[naipe_arq]
            rotulo = "As" if num == "as" else num
            nome = f"{rotulo} de {naipe}"
            chaves = [(chave(a), "de", naipe_arq) for a in NUM_ALT[num]]
            cartas.append({"nome": nome, "img": p, "chaves": chaves})
            continue

        m = RE_CORTE.match(stem)
        if m:
            titulo, naipe_arq = m.group(1), m.group(2)
            naipe = NAIPES[naipe_arq]
            nome = f"{titulo.capitalize()} de {naipe}"
            chaves = [(chave(a), "de", naipe_arq) for a in CORTE_ALT[titulo]]
            cartas.append({"nome": nome, "img": p, "chaves": chaves})
            representante.setdefault(titulo, len(cartas) - 1)
            continue

        m = RE_MAIOR.match(stem)
        if m:
            palavras = m.group(1).split()
            nome = " ".join(w.capitalize() for w in palavras)
            cartas.append({"nome": nome, "img": p,
                            "chaves": [tuple(chave(w) for w in palavras)]})
            continue

        print(f"[baralho] arquivo nao reconhecido, ignorado: {p.name}")

    for titulo, alternativas in CATEGORIA_ALT.items():
        idx = representante.get(titulo)
        if idx is not None:
            cartas[idx]["chaves"] += [(chave(a),) for a in alternativas]

    return cartas


def acha_mencoes(stream, cartas=None):
    """Varre os tokens 'palavra' do stream (tempo de FONTE) procurando as
    chaves do catalogo - mesma tecnica de `corrige_frases()` (tarocut.py):
    tupla de `chave()`, a mais comprida primeiro, andando token a token.

    Devolve uma mencao por casamento: {"carta", "s", "e"}. Nao decide
    span de exibicao nem tempo de saida - isso e do comando `cartas`, que
    passa o resultado por `intervalos()` (a mesma funcao que o render usa)
    antes de decidir o que aparece na tela."""
    cartas = cartas if cartas is not None else catalogo()
    pares = [(chv, c["nome"]) for c in cartas for chv in c["chaves"]]
    pares.sort(key=lambda p: len(p[0]), reverse=True)

    idx = [i for i, t in enumerate(stream) if t["k"] == "palavra"]
    ks = [chave(stream[i]["t"]) for i in idx]

    mencoes, i = [], 0
    while i < len(idx):
        for chv, nome in pares:
            L = len(chv)
            if tuple(ks[i:i + L]) == chv:
                mencoes.append({"carta": nome,
                                 "s": stream[idx[i]]["s"],
                                 "e": stream[idx[i + L - 1]]["e"]})
                i += L
                break
        else:
            i += 1
    return mencoes


if __name__ == "__main__":
    c = catalogo()
    print(f"[baralho] {len(c)} cartas no catalogo")
    for x in c:
        print(f"  {x['nome']:20s} {x['chaves']}")
