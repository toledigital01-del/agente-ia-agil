# Cliente: Ágil Persianas

Regras de negócio, dados de catálogo/preço e infraestrutura específicos **deste cliente**. Conhecimento de engenharia genérico (deploy, arquitetura multi-cliente, debugging de TTS, testes) fica no `../SKILL.md` — este arquivo é só o que muda de cliente pra cliente.

Ao trabalhar num cliente novo, copie a estrutura deste arquivo (não o conteúdo) pra `clientes/<novo-cliente>.md`.

## Infraestrutura deste cliente

- **VPS:** `179.198.100.135` (compartilhada com outros clientes — ver arquitetura multi-cliente no SKILL.md principal, seção 20)
- **Nome da instância Evolution API:** `agil` (WhatsApp da Ágil Persianas, número `554831990720`)
- **Pasta de dados:** `/opt/clientes/agil-persianas/` (`config.json`, `dados.sqlite`, `media/`)
- **Serviço systemd:** `agente@agil-persianas.service`
- **`owner_phone`** (recebe alertas de handoff/frustração/pagamento/áudio quebrado): `555399709661` — atualizado em 2026-08-10 (antes era `555596611311`, o próprio número de teste do cliente, o que confundia teste com alerta real)
- **Repositório GitHub:** `toledigital01-del/agente-ia-agil` — branch `main` é a ativa (branch `v2`, marcada como padrão no GitHub, foi sincronizada com `main` em 2026-08-10, ver SKILL.md seção 36)

## Regras de negócio (BANT / prompt de vendas)

Ao modificar o `system_prompt` deste cliente, sempre respeitar:

- **NUNCA mencionar concorrente:** proibido citar "Fácil Persianas" — usar "nossa fábrica" ou "Ágil Persianas".
- **NUNCA mencionar região da fábrica:** não dizer que a fábrica é em Juiz de Fora (MG). Entrega é nacional.
- **UMA pergunta por vez** — nunca acumular duas perguntas na mesma mensagem (exceto a combinação intencional "tem modelo em mente + sugestão", ver abaixo).
- **Desconto de 5% no PIX** sempre oferecido, com valor calculado mostrado.
- **⚠️ Catálogo é EXCLUSIVAMENTE persianas/cortinas rígidas sob medida** — Rolô, Romana, Double Vision, Painel, Horizontal (Alumínio/PVC/Madeira Sintética), Tela Mosquiteira, Toldos. **NÃO vendem cortina de tecido** (voil, linho, microfibra, veludo). Se o cliente pedir por esses nomes, mapear pro catálogo real: tecido leve/decorativo → Rolô ou Romana Translúcida; tecido pesado/térmico (veludo) → Blackout Vedação Total; vãos grandes/portas deslizantes → Painel. **Isso já causou erro real** (2026-07-28): um guia de decisão por ambiente pesquisado de sites de terceiros recomendou "cortina de voil/linho/veludo", produtos que a loja não vende — sempre conferir contra `agilcortinasepersianas.com.br/loja` antes de adicionar conselho de produto novo ao prompt.
- **Manual de medição:** instalação de parede (+10-15cm todos os lados), sanca/gesso (-1cm largura, +10-15cm altura), split lado a lado (largura/2, aviso de vão de 3cm).
- **Marca: "Ágil Persianas"**, nunca "Ágil Cortinas e Persianas" (corrigido 2026-08-03).
- **Fluxo Etapa 1/1.1:** ao saber o ambiente, a IA pergunta se o cliente já tem modelo em mente **e já sugere** opções na mesma mensagem (não são duas perguntas separadas — foi tentado e revertido a pedido do cliente). Preferência do cliente sempre vence a sugestão da IA.
- **Etapa 5 (resumo) e Etapa 6.5 (endereço):** sempre com a tag `[TEXTO_APENAS]` (mecanismo genérico, ver SKILL.md seção 37) — são checklists, mais fáceis de conferir por escrito.

## Preço

**Pricing é intencionalmente igual ao da Fácil Persianas (concorrente) — não "corrigir" isso.** `agent.py` busca preço ao vivo no site da Fácil Persianas (`facilpersianas.com.br`) via API pública do Shopify (`/products/<handle>.json`), com fallback pra tabela estática (`docs/tabela_precos_referencia.json`) se a busca falhar. Confirmado explicitamente pelo cliente (2026-07-25): "nossos preços são os mesmos deles" — isso é a fonte da verdade até ele mandar uma tabela de preço própria.

- **Preço NÃO é linear por m²** — tem degraus por faixa de tamanho (tag `11a-autopriced-by-table-pricing` no site deles) e muda por cor (ex: Rolô Blackout Branca R$147,39 vs Preta R$160,76). `interpolar_preco()` retorna o preço do primeiro ponto real com área ≥ pedida (arredonda pra cima, não interpola) — validado ao vivo trocando os seletores de largura/altura no site da Fácil, ver histórico de commits pra detalhes de teste.
- **Limitação conhecida, não resolvida:** o preço da Fácil também varia por **largura**, não só área — nossa tabela só tem pontos pra largura ~100cm. Investigado em 2026-08-10: os dados de largura real **não vêm** do endpoint JSON usado hoje (só tem 2 larguras reais nas variantes) — vêm de um app de precificação via chamada AJAX na página ao vivo, não visível em HTML/JSON estático. Corrigir isso exigiria automação de navegador (Playwright/Selenium) — tarefa maior, não tentar sem confirmação explícita do cliente (mexe direto no que o cliente final paga).
- **Também não diferencia por variação de tecido** (Liso/Texturizado/Vedação Total usam o mesmo handle/tabela "Liso") — as variações aparecem como tags no Shopify, não como produtos separados óbvios. Não implementado.
- **Tela Mosquiteira e Toldo:** sem calculadora automática (não achado no site da Fácil nem de concorrentes). Preços de referência **fictícios, autorizados pelo cliente só pra teste** (2026-08-03): Tela Mosquiteira Fixa a partir de R$180/m² (mín. R$250/peça); Retrátil a partir de R$280/m² (mín. R$380/peça); Toldo a partir de R$300/m² (mín. R$450/peça). Sempre apresentar como estimativa provisória, nunca como preço final. **Toldo não aciona mais handoff humano** (removido 2026-08-10) — a IA responde direto com o valor médio, igual Tela Mosquiteira.
- **Handles de produto por cor** (`HANDLES_ROLO_BLACKOUT`, `HANDLES_ROLO_TELA_SOLAR`, `HANDLES_DOUBLE_VISION` em `agent_template.py`): mapeiam cor → handle Shopify da Fácil Persianas. Default "branca" se cor ainda não informada.

## Motorização

Baseado no catálogo real `CÁTALOGO DIGITAL INVICTA DECOR - V1.0.pdf` (fornecido pelo cliente, 2026-08-03) — nunca inventar especificação além disso:
- Motor por **Rádio Frequência (RF)** ou **Wi-Fi**
- Acionamento por controle remoto, app do celular, ou comando de voz (Alexa/Google Home)
- **Motor a bateria recarregável** (não precisa ponto de energia, bom pra casa pronta)
- Painel tem opção de **trilho motorizado**
- Se o cliente perguntar algo muito específico não coberto aqui (marca exata do motor, alcance do Wi-Fi), dizer que vai confirmar com o setor técnico.

## Banco de mídia (fotos/vídeos reais)

Catálogo em `/opt/clientes/agil-persianas/media/catalog.json` (mecanismo `[FOTO:.]`/`[VIDEO:.]` é genérico, ver SKILL.md seção 22). Chaves atuais (9):

| Chave | Produto |
|---|---|
| `rolo-blackout-tecido-liso` | Rolô Blackout, Tecido Liso (fotos + vídeo) |
| `rolo-blackout-vedacao-total` | Rolô Blackout, Vedação Total — foto de ambiente escurecido (bloqueio 100% de luz) |
| `rolo-pinpoint-preto` | Rolô Blackout, Tecido Pinpoint Preto, com Box |
| `romana-blackout-texturizado` | Romana Blackout, Texturizado |
| `romana-blackout` | Romana Blackout, variação genérica/não especificada |
| `painel-blackout-tecido-liso` | Painel Blackout, Tecido Liso (renomeado de `romana-blackout-tecido-liso` — fotos eram de um Painel, não Romana) |
| `double-vision` | Double Vision, qualquer variação (fotos + vídeo) |
| `tela-mosquiteira-fixa` | Tela Mosquiteira Fixa |
| `tela-mosquiteira-retratil` | Tela Mosquiteira Retrátil |

## Histórico de incidentes específicos deste cliente

- **2026-07-28:** guia de decisão por ambiente pesquisado externamente recomendou produto fora do catálogo (voil/linho/veludo) — ver regra de catálogo acima.
- **2026-07-29:** 3 números WhatsApp seguidos banidos por reconectar rápido demais (ver regras gerais de anti-ban no SKILL.md seção 13 — aplicam a qualquer cliente, mas o incidente aconteceu aqui).
- **2026-08-02:** handoff humano travava pra sempre — lead Fernando (555596611311) ficou dias sem resposta depois de pedir atendente (bug genérico, corrigido, ver SKILL.md seção 19).
- **2026-08-06 a 2026-08-10:** áudio quebrado por 4 dias por chave ElevenLabs inválida, só percebido pelo cliente manualmente (ver SKILL.md seção 35 — lição genérica, causa raiz específica desse cliente foi a chave dele expirar/ser revogada).
- **2026-08-10:** WhatsApp da Ágil (`agil`, 554831990720) desconectado de verdade (`device_removed`) — reconectado via novo QR Code. Causa não identificada com certeza (não foi ação via API/manager UI da nossa parte).
