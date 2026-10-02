#!/usr/bin/env python3
"""Confere um anúncio avulso do Facebook (link do Marketplace ou link de compartilhar, como o celular manda): abre com a
sessão do FACEBOOK_COOKIES, vê se ainda está disponível e guarda a ficha (descrição, fotos, anunciante) em
dados/facebook_fichas.json, como a busca faz. O log mostra só o número do anúncio, o título, o preço e a situação.

Com uma palavra no lugar do link (ex.: sicredi), pesquisa no Marketplace de Balneário, abre os anúncios que vierem e diz
quais têm a palavra na descrição.

Uso: Actions → Coletar anúncios → Run workflow com o campo "abrir" preenchido (a rodada não busca nos sites)."""
import json, os, re, sys, urllib.parse
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


def _ler(b, iid, page=None):
    """A ficha do anúncio, com título, preço e situação (abre a página dele se `page` não veio)."""
    if page is None:
        b.go(f'https://www.facebook.com/marketplace/item/{iid}/', 8)
        page = b.js('document.documentElement.outerHTML') or ''
    ficha = fb._ficha(page, iid)
    titulo = fb._s((re.search(r'"marketplace_listing_title":"((?:[^"\\]|\\.)*)"', page) or [None, ''])[1])
    preco = (re.search(r'"listing_price":\{"formatted_amount":"((?:[^"\\]|\\.)*)"', page) or [None, ''])[1]
    ficha.update(titulo=titulo, preco=fb._s(preco), situacao=situacao(page, iid), aberto_em=s.time.strftime('%Y-%m-%d %H:%M'))
    return ficha


def _guardar(fichas):
    try:
        cache = json.load(open(fb.CACHE))
    except Exception:
        cache = {}
    for iid, ficha in fichas.items():
        cache[iid] = dict(cache.get(iid) or {}, **{k: v for k, v in ficha.items() if v not in (None, '', [])})
    os.makedirs(os.path.dirname(fb.CACHE), exist_ok=True)
    json.dump(cache, open(fb.CACHE, 'w'), ensure_ascii=False)


def abrir(url):
    if not re.match(r'https?://', url):
        return procurar(url)
    ck = fb._cookies()
    with s.Chrome(port=s.porta_livre()) as b:
        if ck:
            fb._sessao(b, ck)
        b.go(url, 10)
        final = b.js('location.href') or ''
        page = b.js('document.documentElement.outerHTML') or ''
        m = re.search(r'/marketplace/item/(\d+)', final) or re.search(r'/marketplace/item/(\d+)', page)
        if '/marketplace/ineligible' in final:
            fb.marcar_bloqueio(True)
            print(f'abrir: {fb.INELEGIVEL}')
            return None
        if not m:
            print(f'abrir: o link não levou a um anúncio do Marketplace (foi para {re.sub(r"[?#].*", "", final)[:80]})')
            return None
        iid = m.group(1)
        ficha = _ler(b, iid, page if '/marketplace/item/' in final else None)
    _guardar({iid: ficha})
    print(f"abrir: anúncio {iid} · {ficha['titulo'][:60]} · {ficha['preco']} · {ficha['situacao']} · {len(ficha['fotos'])} foto(s) · "
          f"descrição com {len(ficha['desc'])} letras (na ficha, em dados/facebook_fichas.json)")
    return iid


def procurar(palavra, limite=40):
    """Pesquisa a palavra no Marketplace de Balneário e abre os anúncios que vierem (primeiro os da faixa do perfil, até
    `limite`); o log diz quais têm a palavra no título ou na descrição."""
    ck = fb._cookies()
    q, alvo = urllib.parse.quote(palavra), s.norm(palavra).strip()
    with s.Chrome(port=s.porta_livre()) as b:
        if ck:
            fb._sessao(b, ck)
        lista = {}
        for url in (f'https://www.facebook.com/marketplace/{fb.CIDADE}/search/?query={q}&exact=false',
                    f'https://www.facebook.com/marketplace/{fb.CIDADE}/propertyrentals?query={q}&exact=false'):
            b.go(url, 9)
            lista.update(fb._lista(b.js('document.documentElement.outerHTML') or ''))
            for _ in range(fb.ROLAGENS if ck else 0):
                b.js('window.scrollTo(0, document.body.scrollHeight)')
                s.time.sleep(2.5)
            cards = fb._cards(b.js('[...document.querySelectorAll(\'a[href*="/marketplace/item/"]\')]'
                                   '.map(a => ({h: a.getAttribute("href"), t: a.innerText, i: (a.querySelector("img") || {}).src || ""}))'))
            lista.update({i: x for i, x in cards.items() if i not in lista})
        ordem = sorted(lista, key=lambda i: not s.na_faixa(lista[i].get('preco')))[:limite]
        print(f'procurar: "{palavra}" no Marketplace trouxe {len(lista)} anúncio(s); abrindo {len(ordem)}')
        fichas, achados = {}, []
        for iid in ordem:
            try:
                fichas[iid] = f = _ler(b, iid)
            except Exception as ex:
                print(f'  {iid}: não abriu ({str(ex)[:60]})')
                continue
            tem = alvo in s.norm(f['titulo'] + ' ' + f['desc'])
            achados += [iid] if tem else []
            print(f"  {iid} · {f['titulo'][:50]} · {f['preco']} · {f['situacao']} · "
                  f"{'TEM' if tem else 'não tem'} a palavra · https://www.facebook.com/marketplace/item/{iid}/")
    _guardar(fichas)
    print(f'procurar: {len(achados)} com "{palavra}": ' + (', '.join(achados) or 'nenhum'))
    return achados


if __name__ == '__main__':
    abrir(sys.argv[1])
