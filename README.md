# Próximo Apê

Busca de apartamentos para alugar em Balneário Camboriú. A cada 3 horas, o GitHub Actions lê ZAP, OLX, Chaves na Mão e sites de imobiliárias (`coletor/`), e publica o app (`docs/`) no GitHub Pages.

- Buscar agora: aba **Actions** → *Coletar anúncios* → **Run workflow**.
- Cada leitor fica em `coletor/fontes/<nome>.py` e expõe `buscar(chrome, progresso) -> list[dict]`.
- O histórico (quando cada anúncio apareceu) fica na branch `dados`.

Ruas e linha da praia: © OpenStreetMap contributors (ODbL).
