# Próximo Apê

Busca de apartamentos para alugar em Balneário Camboriú. A cada hora (e quando pedido: Actions → Coletar anúncios → Run workflow), o GitHub Actions lê ZAP, OLX, Chaves na Mão, Facebook Marketplace e sites de imobiliárias (`coletor/`), e publica o app (`docs/`) no GitHub Pages.

- O perfil (`coletor/alerta.py`, `no_perfil`): aluguel de R$ 3.500 a 5.500, só o aluguel (`server.ALUGUEL_MIN/MAX`; os leitores já pedem aos sites só essa faixa, o que deixa a busca rápida), 2+ quartos ou 1 com escritório, pelo menos semimobiliado, até 10 min a pé da praia, até 10 min de carro da Humains (15 em Itajaí), sem recusa de animais. O app só recebe o que está no perfil.
- Buscar agora: aba **Actions** → *Coletar anúncios* → **Run workflow**.
- Cada leitor fica em `coletor/fontes/<nome>.py` e expõe `buscar(chrome, progresso) -> list[dict]`.
- O histórico (quando cada anúncio apareceu) fica na branch `dados`.
- Aviso por e-mail: quando o Claude acha um "como o do Piatã", a busca abre uma issue que menciona o dono do repositório e o GitHub manda o e-mail. Os já avisados ficam em `dados/avisos.json`.
- O Claude (secret `ANTHROPIC_API_KEY`) confere as fotos de cada anúncio do perfil, uma vez cada (`dados/avaliacoes.json`): cozinha integrada à sala, cozinha bonita e bem montada, piso sem cara de antigo, a foto que melhor mostra a cozinha com a sala e, sem fotos de dentro, a chance. O app separa "Como o do Piatã" e "Vale perguntar", e esconde o que ele descartou. Para ver o que ele acha dos últimos anúncios: Actions → Testar fotos → Run workflow.

Ruas e linha da praia: © OpenStreetMap contributors (ODbL).
