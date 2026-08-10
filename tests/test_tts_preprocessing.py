#!/usr/bin/env python3
"""Testes de regressão pro pré-processamento de texto que vira áudio
(number_to_words, price_to_words, preprocess_text_for_tts em
templates/whatsapp/watcher_template.py).

Cada caso aqui corresponde a um bug real, reportado pelo cliente e
confirmado ouvindo o áudio gerado, corrigido ao longo desta sessão --
ver seções 25 a 35 do SKILL.md (skills/agente-vendas-whatsapp/SKILL.md)
para o histórico completo de cada um.

Roda ANTES de qualquer deploy (`python tests/test_tts_preprocessing.py`)
pra garantir que uma correção nova não quebra silenciosamente uma
correção antiga -- foi exatamente assim que a v3->v2 e a estabilidade
0.3->0.55 regrediram sem ninguém perceber na hora (seção 29 do SKILL.md).

Extrai as funções direto do arquivo fonte (via marcadores de texto) em
vez de importar o módulo inteiro, porque watcher_template.py importa
`client_config`/`agent` no topo do arquivo (não existem fora de uma
implantação real) -- as 3 funções testadas aqui são puras (só usam
`re` e a si mesmas), então não precisam dessas dependências.
"""
import re
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATH = REPO_ROOT / "templates" / "whatsapp" / "watcher_template.py"


def carregar_funcoes():
    """Extrai number_to_words, price_to_words e preprocess_text_for_tts
    do arquivo fonte e devolve num namespace isolado. preprocess_text_for_tts
    usa URL_PATTERN, que é definido bem antes no módulo (fora do trecho de
    funções puras extraído aqui) -- extrai também essa linha específica em
    vez de reimplementar a regex à mão, pra nunca divergir do valor real."""
    src = SOURCE_PATH.read_text(encoding="utf-8")

    url_pattern_linha = next(
        linha for linha in src.splitlines() if linha.strip().startswith("URL_PATTERN = re.compile")
    )

    inicio = src.index("def number_to_words")
    fim = src.index("def add_tone_tags_for_v3")
    trecho = src[inicio:fim]

    namespace = {"re": re}
    exec(compile(url_pattern_linha, str(SOURCE_PATH), "exec"), namespace)
    exec(compile(trecho, str(SOURCE_PATH), "exec"), namespace)
    return namespace


AGENT_SOURCE_PATH = REPO_ROOT / "templates" / "whatsapp" / "agent_template.py"


def carregar_extract_force_text_flag():
    """Mesma ideia, mas extrai TEXTO_APENAS_PATTERN e extract_force_text_flag
    de agent_template.py (função pura, só usa `re`)."""
    src = AGENT_SOURCE_PATH.read_text(encoding="utf-8")
    padrao_linha = next(
        linha for linha in src.splitlines() if linha.strip().startswith("TEXTO_APENAS_PATTERN = re.compile")
    )
    inicio = src.index("def extract_force_text_flag")
    fim = src.index("\n\n\n", inicio)
    trecho = src[inicio:fim]

    namespace = {"re": re}
    exec(compile(padrao_linha, str(AGENT_SOURCE_PATH), "exec"), namespace)
    exec(compile(trecho, str(AGENT_SOURCE_PATH), "exec"), namespace)
    return namespace["extract_force_text_flag"]


FUNCOES = carregar_funcoes()
preprocess_text_for_tts = FUNCOES["preprocess_text_for_tts"]
number_to_words = FUNCOES["number_to_words"]
price_to_words = FUNCOES["price_to_words"]
extract_force_text_flag = carregar_extract_force_text_flag()


# Cada caso: (nome, texto de entrada, lista de substrings que TÊM que
# aparecer na saída, lista de substrings que NÃO PODEM aparecer na saída)
CASOS = [
    (
        "nº -> número (seção 31)",
        "Rua Coronel Américo, nº 193",
        ["número 193"],
        ["nº"],
    ),
    (
        "6x -> seis vezes, sem tocar em medida 10x20 (seção 31)",
        "em até 6x sem juros, mede 10x20",
        ["seis vezes"],
        ["6x"],
    ),
    (
        "m² preço por unidade -> singular 'o metro quadrado' (seção 31)",
        "a partir de R$ 180,00 o m²",
        ["o metro quadrado"],
        ["metros quadrados", "m²"],
    ),
    (
        "m² quantidade -> plural 'metros quadrados' (seção 31)",
        "mínimo de 1,80 m²",
        ["metros quadrados"],
        ["m²"],
    ),
    (
        "cartão -> crédito em contexto de parcelamento (seção 32)",
        "em até 6x sem juros no cartão",
        ["crédito"],
        ["cartão"],
    ),
    (
        "cartão de crédito -> crédito, sem duplicar (seção 32)",
        "sem juros no cartão de crédito",
        ["crédito"],
        ["cartão", "crédito de crédito"],
    ),
    (
        "'no cartão' não pode virar 'número' por engano (guarda de regressão pro bug do nº)",
        "pode pagar no cartão ou no boleto",
        [],
        ["número cartão", "número boleto"],
    ),
    (
        "16mm/25mm/50mm -> por extenso (seção 33)",
        "Horizontal Alumínio 16mm, 25mm ou 50mm",
        ["dezesseis milímetros", "vinte e cinco milímetros", "cinquenta milímetros"],
        ["16mm", "25mm", "50mm"],
    ),
    (
        "emoji removido antes do TTS (seção 34)",
        "Perfeito! 😊 Vou confirmar ✅ a medida.",
        ["Perfeito!", "Vou confirmar", "a medida."],
        ["😊", "✅"],
    ),
    (
        "travessão comum não deve ser removido (seção 34 -- testado, não é bug)",
        "ótimo pra esse ambiente — bloqueia a luz",
        ["—"],
        [],
    ),
    (
        "markdown removido antes do TTS (seção 27)",
        "**Cidade:** Florianópolis/SC\n- item da lista\n# Cabeçalho",
        ["Cidade:", "Florianópolis, Santa Catarina", "item da lista", "Cabeçalho"],
        ["**", "Florianópolis/SC", "- item", "# Cabeçalho"],
    ),
    (
        "UF colada expandida por extenso (seção 27)",
        "a entrega é em Juiz de Fora, MG",
        ["Minas Gerais"],
        [", MG"],
    ),
    (
        "CEP com hífen falado dígito por dígito (seção 28)",
        "CEP 88085-250",
        ["oito, oito, zero, oito, cinco, dois, cinco, zero"],
        ["88085-250"],
    ),
    (
        "link nunca é falado em voz alta (removido antes do TTS)",
        "clica aqui: https://exemplo.com/checkout/abc123",
        ["clica aqui:"],
        ["https://", "exemplo.com"],
    ),
    (
        "preço R$ convertido por extenso",
        "fica R$ 54,01 no total",
        ["cinquenta e quatro reais", "um centavo"],
        ["R$ 54,01"],
    ),
    (
        "medida decimal '1,50 metros' convertida por extenso",
        "a altura é 1,50 metros",
        ["um metro e cinquenta centímetros"],
        ["1,50 metros"],
    ),
    (
        "link 'www.algo' sem http(s):// também nunca é falado (seção 37)",
        "acesse www.asaas.com/i/xyz123 pra pagar",
        ["acesse", "pra pagar"],
        ["www.", "asaas.com"],
    ),
    (
        "medida '1,00 metros' -> singular, sem centímetros (regressão da seção 36)",
        "a largura é 1,00 metros",
        ["um metro"],
        ["um metros", "centímetros"],
    ),
]


def rodar_testes():
    falhas = []
    for nome, entrada, deve_conter, nao_deve_conter in CASOS:
        saida = preprocess_text_for_tts(entrada)
        for trecho in deve_conter:
            if trecho not in saida:
                falhas.append(
                    f"[{nome}] esperava '{trecho}' na saída, não achou.\n"
                    f"  entrada: {entrada!r}\n  saída:   {saida!r}"
                )
        for trecho in nao_deve_conter:
            if trecho in saida:
                falhas.append(
                    f"[{nome}] '{trecho}' não podia aparecer na saída, mas apareceu.\n"
                    f"  entrada: {entrada!r}\n  saída:   {saida!r}"
                )

    # Casos extras de number_to_words / price_to_words isolados
    extras = [
        (number_to_words(1), "um"),
        (number_to_words(16), "dezesseis"),
        (number_to_words(25), "vinte e cinco"),
        (number_to_words(100), "cem"),
        (price_to_words("R$ 1,00"), "um real"),
    ]
    for obtido, esperado in extras:
        if obtido != esperado:
            falhas.append(f"esperava {esperado!r}, obteve {obtido!r}")

    # extract_force_text_flag (tag [TEXTO_APENAS], seção 37)
    casos_forcar_texto = [
        ("[TEXTO_APENAS]\nResumo: Rolô Blackout, 1,20x1,50m, branco.", "Resumo: Rolô Blackout, 1,20x1,50m, branco.", True),
        ("Oi! Tudo bem?", "Oi! Tudo bem?", False),
    ]
    for entrada, texto_esperado, flag_esperada in casos_forcar_texto:
        texto_obtido, flag_obtida = extract_force_text_flag(entrada)
        if texto_obtido != texto_esperado or flag_obtida != flag_esperada:
            falhas.append(
                f"[extract_force_text_flag] entrada={entrada!r}\n"
                f"  esperado: ({texto_esperado!r}, {flag_esperada}) obtido: ({texto_obtido!r}, {flag_obtida})"
            )

    total = len(CASOS) + len(extras) + len(casos_forcar_texto)
    if falhas:
        print(f"❌ {len(falhas)}/{total} verificações falharam:\n")
        for f in falhas:
            print(f)
            print()
        return False

    print(f"✅ {total}/{total} verificações passaram.")
    return True


if __name__ == "__main__":
    ok = rodar_testes()
    sys.exit(0 if ok else 1)
