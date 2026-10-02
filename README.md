# Próximo Apê

Busca de apartamentos para alugar em Balneário Camboriú. A cada hora (e quando pedido: Actions → Coletar anúncios → Run workflow), o GitHub Actions lê ZAP, OLX, Chaves na Mão, Facebook Marketplace e sites de imobiliárias (`coletor/`), e publica o app (`docs/`) no GitHub Pages.

- O perfil (`coletor/alerta.py`, `no_perfil`): aluguel de R$ 3.500 a 6.000, só o aluguel (`server.ALUGUEL_MIN/MAX`; os leitores já pedem aos sites só essa faixa, o que deixa a busca rápida), 2 quartos ou 1 com escritório (3 ou mais é grande demais), pelo menos semimobiliado, até 10 min a pé da praia, até 10 min de carro da Humains (15 em Itajaí), fora da Barra (longe, mesmo quando o anúncio não dá o endereço), sem recusa de animais. O app só recebe o que está no perfil.
- Buscar agora: aba **Actions** → *Coletar anúncios* → **Run workflow**.
- Cada leitor fica em `coletor/fontes/<nome>.py` e expõe `buscar(chrome, progresso) -> list[dict]`.
- O histórico (quando cada anúncio apareceu) fica na branch `dados`.
- Aviso por e-mail: quando o Claude acha um de acabamento excelente (com cozinha integrada), a busca abre uma issue que menciona o dono do repositório e o GitHub manda o e-mail. Os já avisados ficam em `dados/avisos.json`.
- O Claude confere as fotos de cada anúncio do perfil, uma vez cada (`dados/avaliacoes.json`): a nota do acabamento (excelente, bom, comum, ruim), se a cozinha é integrada à sala (obrigatório), a foto que melhor mostra o apartamento por dentro e, sem fotos de dentro, a chance. Tudo sozinho, a cada hora: o app mostra primeiro os de acabamento excelente, depois os bons e os "vale perguntar", e esconde o que ele descartou. Sem a chave da API (secret `ANTHROPIC_API_KEY`), quem confere é uma rotina do Claude Code, pelo plano do dono: a busca põe na branch `fila-claude` folhas com as fotos numeradas e o texto dos anúncios que faltam; a rotina (de hora em hora) usa `coletor/rotina.py` para ler a fila e gravar os vereditos na branch `avaliacoes`, que a busca seguinte junta ao app e ao aviso. Para publicar na hora: Run workflow com "nenhuma".

Ruas e linha da praia: © OpenStreetMap contributors (ODbL).
