#!/usr/bin/env python3
"""Confere um anúncio avulso do Facebook (link do Marketplace ou link de compartilhar, como o celular manda): abre com a
sessão do FACEBOOK_COOKIES, vê se ainda está disponível e guarda a ficha (descrição, fotos, anunciante) em
dados/facebook_fichas.json, como a busca faz. O log mostra só o número do anúncio, o título, o preço e a situação.

Uso: Actions → Coletar anúncios → Run workflow com o campo "abrir" preenchido (a rodada não busca nos sites)."""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as s
from fontes import facebook as fb


def situacao(page, iid=''):
    """Pelos campos do próprio anúncio (a página traz outros anúncios sugeridos, com os campos deles)."""
    for m in re.finditer(r'"id":"%s"' % re.escape(iid), page) if iid else []:
        w = page[max(0, m.start() - 3000):m.end() + 6000]
        if re.search(r'"is_(?:sold|pending|live)":', w):
            page = w
            break
    if re.search(r'não está mais disponível|no longer available', page, re.I):
        return 'fora do ar'
    if '"is_sold":true' in page:
        return 'alugado ou vendido (marcado no anúncio)'
    if '"is_pending":true' in page:
        return 'pendente (alguém já está negociando)'
    if '"is_live":false' in page:
        return 'fora do ar'
    return 'disponível'


def abrir(url):
    ck = fb._cookies()
    with s.Chrome(port=s.porta_livre()) as b:
        if ck:
            fb._sessao(b, ck)
        b.go(url, 10)
        final = b.js('location.href') or ''
        page = b.js('document.documentElement.outerHTML') or ''
        m = re.search(r'/marketplace/item/(\d+)', final) or re.search(r'/marketplace/item/(\d+)', page)
        if not m:
            print(f'abrir: o link não levou a um anúncio do Marketplace (foi para {re.sub(r"[?#].*", "", final)[:80]})')
            return None
        iid = m.group(1)
        if '/marketplace/item/' not in final:
            b.go(f'https://www.facebook.com/marketplace/item/{iid}/', 8)
            page = b.js('document.documentElement.outerHTML') or ''
        ficha = fb._ficha(page, iid)
        titulo = fb._s((re.search(r'"marketplace_listing_title":"((?:[^"\\]|\\.)*)"', page) or [None, ''])[1])
        preco = (re.search(r'"listing_price":\{"formatted_amount":"((?:[^"\\]|\\.)*)"', page) or [None, ''])[1]
        ficha.update(titulo=titulo, preco=fb._s(preco), situacao=situacao(page, iid), aberto_em=s.time.strftime('%Y-%m-%d %H:%M'))
    try:
        cache = json.load(open(fb.CACHE))
    except Exception:
        cache = {}
    cache[iid] = dict(cache.get(iid) or {}, **{k: v for k, v in ficha.items() if v not in (None, '', [])})
    os.makedirs(os.path.dirname(fb.CACHE), exist_ok=True)
    json.dump(cache, open(fb.CACHE, 'w'), ensure_ascii=False)
    print(f"abrir: anúncio {iid} · {titulo[:60]} · {fb._s(preco)} · {ficha['situacao']} · {len(ficha['fotos'])} foto(s) · "
          f"descrição com {len(ficha['desc'])} letras (na ficha, em dados/facebook_fichas.json)")
    return iid


if __name__ == '__main__':
    abrir(sys.argv[1])
