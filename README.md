# Próximo Apê

Busca de apartamentos para alugar em Balneário Camboriú. A cada 3 horas (e quando pedido: Actions → Coletar anúncios → Run workflow), o GitHub Actions lê ZAP, OLX, Chaves na Mão e sites de imobiliárias (`coletor/`), e publica o app (`docs/`) no GitHub Pages.

- Buscar agora: aba **Actions** → *Coletar anúncios* → **Run workflow**.
- Cada leitor fica em `coletor/fontes/<nome>.py` e expõe `buscar(chrome, progresso) -> list[dict]`.
- O histórico (quando cada anúncio apareceu) fica na branch `dados`.
- Aviso por e-mail: quando aparece apartamento novo que serve (`coletor/alerta.py`), a busca abre uma issue que menciona o dono do repositório e o GitHub manda o e-mail. Os já avisados ficam em `dados/avisos.json`.
- O aviso só sai para apartamento como o do Piatã (aluguel de R$ 4.000 a 5.000, cozinha integrada à sala, cozinha bonita e bem montada, piso sem rejunte grosso nem azulejo antigo), com a foto que melhor mostra a cozinha com a sala. Quem confere pelas fotos é o Claude, com a chave da API no secret `ANTHROPIC_API_KEY` (Settings → Secrets and variables → Actions); sem ela, vale só o texto do anúncio. O que já foi conferido fica em `dados/avaliacoes.json`.

Ruas e linha da praia: © OpenStreetMap contributors (ODbL).
