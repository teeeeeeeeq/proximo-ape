"""Facebook Marketplace (sem login): aluguéis perto de Balneário Camboriú.

Sem login o Marketplace mostra só os 24 anúncios mais recentes de cada busca, então a leitura
divide por faixa de preço. A descrição e as fotos grandes vêm da página de cada anúncio, que é
aberta uma vez e guardada em dados/facebook_fichas.json.
"""
import json, os, re, time

NOME = 'Facebook Marketplace'
USA_CHROME = True

CIDADE = '108416972513126'   # Balneário Camboriú no Marketplace
FAIXAS = [(2000, 3200), (3200, 4000), (4000, 4700), (4700, 5500), (5500, 7000), (7000, 9000)]
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'dados', 'facebook_fichas.json')
PRAZO_FICHAS = 420   # segundos


def _s(raw):
    try:
        return json.loads('"' + raw + '"')
    except Exception:
        return raw


def _num(x):
    s = re.sub(r'[^\d,]', '', str(x or '')).replace(',', '.')
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _lista(page):
    """Anúncios da página de busca: id, título, quartos, bairro/cidade, preço, miniatura."""
    out = {}
    for m in re.finditer(r'"(?:listing|for_sale_item)":\{"__typename":"GroupCommerceProductItem","id":"(\d+)"', page):
        iid, w = m.group(1), page[m.end():m.end() + 7000]
        if iid in out or '"marketplace_listing_title"' not in w:
            continue
        g = lambda pat: (re.search(pat, w) or [None, None])[1]
        preco = g(r'"listing_price":\{"formatted_amount":"[^"]*","amount_with_offset_in_currency":"\d+","amount":"([\d.]+)"')
        out[iid] = dict(
            titulo=_s(g(r'"marketplace_listing_title":"((?:[^"\\]|\\.)*)"') or ''),
            sub=_s(g(r'"custom_title":"((?:[^"\\]|\\.)*)"') or ''),
            onde=_s(g(r'"custom_sub_titles_with_rendering_flags":\[\{"subtitle":"((?:[^"\\]|\\.)*)"') or ''),
            cidade=_s(g(r'"reverse_geocode":\{"city":"((?:[^"\\]|\\.)*)"') or ''),
            preco=float(preco) if preco else _num(_s(g(r'"formatted_price":\{"text":"((?:[^"\\]|\\.)*)"') or '')),
            vendido='"is_sold":true' in w[:4000] or '"is_pending":true' in w[:4000] or '"is_live":false' in w[:4000],
            foto=_s(g(r'"(?:primary_listing_photo|listing_photos)":\[?\{"__typename":"[A-Za-z]+","image":\{(?:"height":\d+,"width":\d+,)?"uri":"((?:[^"\\]|\\.)*)"') or ''))
    return out


def _ficha(page, iid=''):
    """Da página do anúncio: descrição, data, fotos grandes, grupo/vendedor, local aproximado."""
    d = re.search(r'"redacted_description":\{"text":"((?:[^"\\]|\\.)*)"\}', page)
    t = re.search(r'"redacted_description":\{"text":"(?:[^"\\]|\\.)*"\},"creation_time":(\d+)', page)
    fotos = []
    ph = re.search(r'"listing_photos":\[(.*?)\],"', page)
    if ph:
        fotos = [_s(u) for u in re.findall(r'"image":\{"height":\d+,"width":\d+,"uri":"((?:[^"\\]|\\.)*)"', ph.group(1))]
    grupo = re.search(r'"origin_group":\{"id":"\d+","name":"((?:[^"\\]|\\.)*)"', page)
    vend = re.search(r'"marketplace_listing_seller":\{"__typename":"User","name":"((?:[^"\\]|\\.)*)"', page)
    return dict(desc=_s(d.group(1)) if d else '', criado=int(t.group(1)) if t else None, fotos=fotos,
                anunciante=('grupo ' + _s(grupo.group(1))) if grupo else (_s(vend.group(1)) if vend else 'Facebook'))


def _quartos(*txts):
    for t in txts:
        m = re.search(r'(\d+)\s*(quartos?|dormit|dorms?\b|su[ií]tes?)', t or '', re.I)
        if m:
            return int(m.group(1))
    return None


_NUM = {'um': 1, 'uma': 1, 'dois': 2, 'duas': 2, 'tres': 3, 'quatro': 4, 'cinco': 5, 'seis': 6, 'sete': 7, 'oito': 8, 'nove': 9,
        'dez': 10, 'onze': 11, 'doze': 12, 'treze': 13, 'catorze': 14, 'quatorze': 14, 'quinze': 15, 'dezesseis': 16, 'dezessete': 17,
        'dezoito': 18, 'dezenove': 19, 'vinte': 20, 'trinta': 30, 'quarenta': 40, 'cinquenta': 50, 'sessenta': 60, 'setenta': 70,
        'oitenta': 80, 'noventa': 90, 'cem': 100, 'cento': 100, 'duzentos': 200, 'trezentos': 300, 'quatrocentos': 400,
        'quinhentos': 500, 'seiscentos': 600, 'setecentos': 700, 'oitocentos': 800, 'novecentos': 900}


def _extenso(txt):
    """'Rua Três Mil Cento e Cinquenta' -> 'Rua 3150' (as ruas numeradas de BC)."""
    import unicodedata
    def conv(m):
        ws = unicodedata.normalize('NFKD', m.group(2).lower()).encode('ascii', 'ignore').decode().split()
        total = cur = 0
        for w in ws:
            if w == 'e':
                continue
            if w == 'mil':
                total += (cur or 1) * 1000
                cur = 0
            elif w in _NUM:
                cur += _NUM[w]
            else:
                return m.group(0)
        return m.group(1) + str(total + cur)
    pal = r'(?:um|uma|dois|duas|tr[eê]s|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|treze|c?quatorze|catorze|quinze|dezesseis|dezessete|dezoito|dezenove|vinte|trinta|quarenta|cinquenta|sessenta|setenta|oitenta|noventa|cem|cento|duzentos|trezentos|quatrocentos|quinhentos|seiscentos|setecentos|oitocentos|novecentos|mil|e)'
    return re.sub(r'((?:Rua|R\.)\s+)((?:%s)(?:\s+%s)*)\b' % (pal, pal), conv, txt or '', flags=re.I)


def _rua(*txts):
    for t in txts:
        m = re.search(r'\b((?:Rua|Avenida|Av\.|R\.)\s+[^,\n]{1,40})', _extenso(t))
        if m:
            return m.group(1).strip()
    return ''


def buscar(b, progresso=lambda m: None):
    lista = {}
    for lo, hi in FAIXAS:
        progresso(f'lista R$ {lo}–{hi}')
        b.go(f'https://www.facebook.com/marketplace/{CIDADE}/propertyrentals?minPrice={lo}&maxPrice={hi}&minBedrooms=2'
             f'&sortBy=creation_time_descend&exact=false&latitude=-26.99&longitude=-48.635&radius=8', 9)
        lista.update(_lista(b.js('document.documentElement.outerHTML') or ''))
    if not lista:
        raise RuntimeError('o Facebook não mostrou anúncios sem login')
    alvo = {i: x for i, x in lista.items()
            if 'balneário camboriú' in (x['onde'] + ' ' + x['cidade']).lower() and not re.search(r'\bcasa\b|kitnet|sala comercial|quarto para', x['titulo'], re.I)}
    try:
        cache = json.load(open(CACHE))
    except Exception:
        cache = {}
    t0 = time.time()
    faltam = [i for i in alvo if i not in cache]
    for k, iid in enumerate(faltam):
        if time.time() - t0 > PRAZO_FICHAS:
            break
        progresso(f'anúncio {k + 1} de {len(faltam)}')
        try:
            b.go(f'https://www.facebook.com/marketplace/item/{iid}/', 7)
            cache[iid] = _ficha(b.js('document.documentElement.outerHTML') or '', iid)
        except Exception:
            pass
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(cache, open(CACHE, 'w'), ensure_ascii=False)
    out = []
    for iid, x in alvo.items():
        f = cache.get(iid) or {}
        onde = [p.strip() for p in x['onde'].split(',')]
        bairro = onde[0] if len(onde) >= 3 and not re.match(r'(rua|avenida|av\.)', onde[0], re.I) else ''
        q = _quartos(x['sub'], x['titulo'], f.get('desc'))
        out.append(dict(
            id='F' + iid, fonte='Facebook', url=f'https://www.facebook.com/marketplace/item/{iid}/', titulo=x['titulo'],
            desc=f.get('desc', ''), ativo=not x['vendido'], aluguel=x['preco'], cond=None, iptu=None, quartos=q, suites=None, area=None,
            bairro=bairro, rua=_rua(x['onde'], f.get('desc')), cidade='Balneário Camboriú', lat=None, lon=None, local_exato=False,
            marcado_mobiliado=False, publicado=time.strftime('%Y-%m-%d', time.localtime(f['criado'])) if f.get('criado') else '',
            anunciante=f.get('anunciante') or 'Facebook', fotos=f.get('fotos') or ([x['foto']] if x['foto'] else [])))
    return out
