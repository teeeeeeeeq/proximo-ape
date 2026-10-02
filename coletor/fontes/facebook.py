"""Facebook Marketplace: aluguéis perto de Balneário Camboriú.

Com o segredo FACEBOOK_COOKIES (a sessão de uma conta, exportada do navegador do celular), entra logado;
sem ele, tenta sem login (hoje o Facebook não mostra nada assim). Cada busca mostra poucos anúncios de cada vez,
então a leitura divide por faixa de preço e rola a página. A descrição e as fotos grandes vêm da página de cada
anúncio, que é aberta uma vez e guardada em dados/facebook_fichas.json.
"""
import json, os, re, time

import server

NOME = 'Facebook Marketplace'
USA_CHROME = True

CIDADE = '108416972513126'   # Balneário Camboriú no Marketplace
FAIXAS = [(lo, min(lo + 500, server.ALUGUEL_MAX)) for lo in range(server.ALUGUEL_MIN, server.ALUGUEL_MAX, 500)]   # só a faixa do perfil
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'dados', 'facebook_fichas.json')
PRAZO_FICHAS = 420   # segundos por busca abrindo páginas de anúncio (~60 páginas; o resto fica para a próxima hora)
FICHA_V = 2          # versão da leitura da ficha: a 1 pegava só a 1ª foto de muitos anúncios (relê os que ficaram com 0 ou 1)
RENOVA_H = 36        # as fotos do Facebook vêm com link que vence em ~5 dias: relê o anúncio antes
ROLAGENS = 4         # vezes que rola cada busca para carregar mais anúncios (logado)
_SAMESITE = {'lax': 'Lax', 'strict': 'Strict', 'no_restriction': 'None', 'none': 'None'}


def _cookies():
    """Sessão do segredo FACEBOOK_COOKIES: o JSON que a extensão Cookie-Editor exporta
    ([{"name": "c_user", "value": "...", "domain": ".facebook.com", ...}]) ou o texto "c_user=...; xs=...".
    Vazio = sem login."""
    raw = (os.environ.get('FACEBOOK_COOKIES') or '').strip()
    if not raw:
        return []
    try:
        lst = json.loads(raw)
        lst = lst.get('cookies', [lst]) if isinstance(lst, dict) else lst
        pares = [(c.get('name'), c.get('value'), c) for c in lst if isinstance(c, dict)]
    except ValueError:
        pares = [(k.strip(), v.strip(), {}) for k, _, v in (p.partition('=') for p in raw.split(';')) if k.strip()]
    out = []
    for nome, valor, c in pares:
        dom = c.get('domain') or '.facebook.com'
        if not nome or valor is None or 'facebook.com' not in dom:
            continue
        ck = dict(name=nome, value=str(valor), domain=dom, path=c.get('path') or '/', secure=True,
                  httpOnly=bool(c.get('httpOnly', nome in ('xs', 'fr', 'sb', 'datr'))))
        exp = c.get('expirationDate') or c.get('expires')
        if isinstance(exp, (int, float)) and exp > 0:
            ck['expires'] = exp
        if str(c.get('sameSite') or '').lower() in _SAMESITE:
            ck['sameSite'] = _SAMESITE[str(c['sameSite']).lower()]
        out.append(ck)
    nomes = {c['name'] for c in out}
    if not {'c_user', 'xs'} <= nomes:
        raise RuntimeError('o segredo FACEBOOK_COOKIES não tem os cookies c_user e xs: exporte de novo, com a conta aberta no navegador')
    return out


def _cards(itens):
    """Cards que a página desenhou ao rolar (o que veio depois do HTML inicial): [{h: href, t: innerText}]."""
    out = {}
    for it in itens or []:
        m = re.search(r'/marketplace/item/(\d+)', it.get('h') or '')
        linhas = [l.strip() for l in (it.get('t') or '').split('\n') if l.strip()]
        if not m or not linhas:
            continue
        precos = [l for l in linhas if re.match(r'R\$\s*[\d.,]+', l)]
        resto = [l for l in linhas if l not in precos]
        if not precos or not resto:
            continue
        onde = resto[-1] if len(resto) > 1 else ''
        cidade = next((c for c in ('Balneário Camboriú', 'Camboriú', 'Itajaí') if c.lower() in onde.lower()), '')
        # 'R$ 4.200R$ 4.500': o primeiro é o atual, o segundo é o antigo riscado
        valores = [_num(v) for v in re.findall(r'R\$\s*([\d.]+(?:,\d+)?)', ' '.join(precos))]
        out[m.group(1)] = dict(titulo=resto[0], sub='', onde=onde, cidade=cidade, preco=valores[0],
                               antes=valores[1] if len(valores) > 1 and (valores[1] or 0) > (valores[0] or 0) else None,
                               vendido=bool(re.search(r'\b(alugado|vendido|pendente)\b', ' '.join(linhas), re.I)),
                               foto=it.get('i') if (it.get('i') or '').startswith('http') else '')
    return out


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
        riscado = g(r'"strikethrough_price":\{"formatted_amount":"[^"]*",(?:"amount_with_offset_in_currency":"\d+",)?"amount":"([\d.]+)"')
        out[iid] = dict(
            titulo=_s(g(r'"marketplace_listing_title":"((?:[^"\\]|\\.)*)"') or ''),
            sub=_s(g(r'"custom_title":"((?:[^"\\]|\\.)*)"') or ''),
            onde=_s(g(r'"custom_sub_titles_with_rendering_flags":\[\{"subtitle":"((?:[^"\\]|\\.)*)"') or ''),
            cidade=_s(g(r'"reverse_geocode":\{"city":"((?:[^"\\]|\\.)*)"') or ''),
            preco=float(preco) if preco else _num(_s(g(r'"formatted_price":\{"text":"((?:[^"\\]|\\.)*)"') or '')),
            antes=float(riscado) if riscado else None,
            vendido='"is_sold":true' in w[:4000] or '"is_pending":true' in w[:4000] or '"is_live":false' in w[:4000],
            foto=_miniatura(w))
    return out


def _miniatura(w):
    """A foto de capa do card (primary_listing_photo ou a 1ª de listing_photos), com as chaves em qualquer ordem."""
    for chave in ('"primary_listing_photo":', '"listing_photos":'):
        i = w.find(chave)
        m = i >= 0 and re.search(r'"uri":"((?:[^"\\]|\\.)*)"', w[i:i + 1500])
        if m:
            return _s(m.group(1))
    return ''


def _bloco(texto, i):
    """O array ou objeto JSON que começa em texto[i] ('[' ou '{'), respeitando as strings; None se não fecha."""
    prof, em_str, esc = 0, False, False
    for k in range(i, min(len(texto), i + 400_000)):
        c = texto[k]
        if em_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                em_str = False
        elif c == '"':
            em_str = True
        elif c in '[{':
            prof += 1
        elif c in ']}':
            prof -= 1
            if prof == 0:
                return texto[i:k + 1]
    return None


def _fotos_ficha(page):
    """Todas as fotos grandes do anúncio: o array listing_photos inteiro (o maior, se a página tiver mais de um).
    Antes, uma regex parava no primeiro '],"' e muitos anúncios ficavam só com a 1ª foto."""
    melhor = []
    for m in re.finditer(r'"listing_photos":\[', page):
        bloco = _bloco(page, m.end() - 1) or ''
        try:
            urls = [((it or {}).get('image') or {}).get('uri') for it in json.loads(bloco) if isinstance(it, dict)]
        except ValueError:   # não é JSON puro: as URIs das imagens, com as chaves em qualquer ordem
            urls = [_s(u) for u in re.findall(r'"image":\{[^{}]*?"uri":"((?:[^"\\]|\\.)*)"', bloco)]
        urls = list(dict.fromkeys(u for u in urls if u))
        if len(urls) > len(melhor):
            melhor = urls
    return melhor


def _estrutura(page):
    """Só os nomes dos campos do 1º item de listing_photos (e quantos itens), sem os valores."""
    def nomes(x, fundo=3):
        if isinstance(x, dict):
            return {k: nomes(v, fundo - 1) for k, v in x.items()} if fundo else '{…}'
        if isinstance(x, list):
            return [nomes(x[0], fundo - 1), f'+{len(x) - 1}'] if x and fundo else f'[{len(x)}]'
        return type(x).__name__
    m = re.search(r'"listing_photos":\[', page)
    if not m:
        return 'não aparece na página'
    try:
        itens = json.loads(_bloco(page, m.end() - 1) or '')
        return f'{len(itens)} item(ns): ' + json.dumps(nomes(itens[0]) if itens else None, ensure_ascii=False)[:400]
    except ValueError:
        return 'não é JSON puro: ' + str(len(re.findall(r'"uri":"', page[m.end():m.end() + 20000]))) + ' "uri" logo depois'


def _vence(fotos):
    """Quando o primeiro link de foto vence (parâmetro oe=, hora em hexa), ou None."""
    ts = [int(m.group(1), 16) for u in fotos or [] for m in [re.search(r'[?&]oe=([0-9A-Fa-f]{8})', u)] if m]
    return min(ts) if ts else None


def _ficha(page, iid=''):
    """Da página do anúncio: descrição, data, fotos grandes, grupo/vendedor, local aproximado."""
    d = re.search(r'"redacted_description":\{"text":"((?:[^"\\]|\\.)*)"\}', page)
    t = re.search(r'"redacted_description":\{"text":"(?:[^"\\]|\\.)*"\},"creation_time":(\d+)', page) \
        or re.search(r'"creation_time":(\d{10})\b', page)   # logado, a data não vem colada na descrição
    fotos = _fotos_ficha(page)
    grupo = re.search(r'"origin_group":\{"id":"\d+","name":"((?:[^"\\]|\\.)*)"', page)
    vend = re.search(r'"marketplace_listing_seller":\{"__typename":"User","name":"((?:[^"\\]|\\.)*)"', page)
    return dict(v=FICHA_V, desc=_s(d.group(1)) if d else '', criado=int(t.group(1)) if t and 1.5e9 < int(t.group(1)) < time.time() + 86400 else None, fotos=fotos,
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


def _regiao(x):
    """Na lista: Balneário, Camboriú e Itajaí (a cidade inteira; quem decide é o tempo de carro até a Humains)."""
    t = (x['onde'] + ' ' + x['cidade']).lower()
    return 'camboriú' in t or 'itajaí' in t


def _sessao(b, ck):
    """Põe a sessão no navegador e confere se o Facebook aceitou. As mensagens não levam texto da página:
    logado, ele traz nome e dados da conta, e o erro vai para o meta.json publicado."""
    b.cmd('Network.setCookies', cookies=ck)
    b.go('https://www.facebook.com/marketplace/', 8)
    url = b.js('location.href') or ''
    if '/checkpoint' in url:
        raise RuntimeError('o Facebook pediu para confirmar a conta (checkpoint): abra o Facebook no celular, confirme, '
                           'e exporte os cookies de novo')
    uid = next(c['value'] for c in ck if c['name'] == 'c_user')
    page = b.js('document.documentElement.outerHTML') or ''
    if f'"USER_ID":"{uid}"' not in page and f'"actorID":"{uid}"' not in page:
        raise RuntimeError('o Facebook não aceitou a sessão (cookies vencidos ou derrubados): exporte os cookies de novo')


def buscar(b, progresso=lambda m: None):
    ck = _cookies()
    if ck:
        progresso('entrando na conta')
        _sessao(b, ck)
    lista = {}
    for lat, raio in ((-26.99, 7), (-26.935, 6)):   # Balneário/Camboriú e Itajaí até o Centro
        for lo, hi in FAIXAS:
            progresso(f'lista R$ {lo}–{hi} ({lat})')
            b.go(f'https://www.facebook.com/marketplace/{CIDADE}/propertyrentals?minPrice={lo}&maxPrice={hi}&minBedrooms=1'
                 f'&sortBy=creation_time_descend&exact=false&latitude={lat}&longitude=-48.645&radius={raio}', 9)
            lista.update(_lista(b.js('document.documentElement.outerHTML') or ''))
            for _ in range(ROLAGENS if ck else 0):
                b.js('window.scrollTo(0, document.body.scrollHeight)')
                time.sleep(2.5)
            if ck:
                cards = _cards(b.js('[...document.querySelectorAll(\'a[href*="/marketplace/item/"]\')]'
                                    '.map(a => ({h: a.getAttribute("href"), t: a.innerText, i: (a.querySelector("img") || {}).src || ""}))'))
                lista.update({i: x for i, x in cards.items() if i not in lista})
    if not lista:
        if ck:
            raise RuntimeError('logado, mas o Marketplace não mostrou anúncios (a página pode ter mudado)')
        txt = (b.js('document.body ? document.body.innerText : ""') or '')[:160].replace('\n', ' | ')
        raise RuntimeError(f"o Facebook não mostrou anúncios sem login (página: {(b.js('document.title') or '')[:50]} | {txt})")
    alvo = {i: x for i, x in lista.items()
            if _regiao(x) and server.na_faixa(x['preco']) and not re.search(r'\bcasa\b|kitnet|sala comercial|quarto para', x['titulo'], re.I)}
    try:
        cache = json.load(open(CACHE))
    except Exception:
        cache = {}
    t0 = time.time()
    # primeiro os que nunca foram lidos (ou vieram vazios), depois os que estão com as fotos para vencer
    novos = [i for i in alvo if not (cache.get(i) or {}).get('desc') and not (cache.get(i) or {}).get('fotos')]
    novos += [i for i in alvo if i not in novos and (cache[i].get('v') or 1) < FICHA_V and len(cache[i].get('fotos') or []) <= 1]
    vencendo = sorted((i for i in alvo if i not in novos and (_vence(cache[i].get('fotos')) or 9e9) < time.time() + RENOVA_H * 3600),
                      key=lambda i: _vence(cache[i].get('fotos')))
    faltam = novos + vencendo
    lidas_1, contagem = [], [0, 0, 0]
    for k, iid in enumerate(faltam):
        if time.time() - t0 > PRAZO_FICHAS:
            break
        progresso(f'anúncio {k + 1} de {len(faltam)}')
        try:
            b.go(f'https://www.facebook.com/marketplace/item/{iid}/', 7)
            page = b.js('document.documentElement.outerHTML') or ''
            f = _ficha(page, iid)
            if not f['fotos'] and not f['desc'] and cache.get(iid):
                continue   # a página não carregou: fica o que já tinha
            if len(f['fotos']) <= 1 and not lidas_1:   # para entender pelo log, sem publicar o conteúdo da página
                lidas_1.append(iid)
                progresso(f'ficha {iid} com {len(f["fotos"])} foto(s); estrutura de listing_photos: {_estrutura(page)}')
            cache[iid] = f
            contagem[min(len(f['fotos']), 2)] += 1
        except Exception:
            pass
    if sum(contagem):
        progresso(f'fichas lidas: {sum(contagem)} ({contagem[0]} sem foto, {contagem[1]} com 1, {contagem[2]} com 2 ou mais)')
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
            desc=f.get('desc', ''), ativo=not x['vendido'], aluguel=x['preco'], preco_antes_site=x.get('antes'), cond=None, iptu=None, quartos=q, suites=None, area=None,
            bairro=bairro, rua=_rua(x['onde'], f.get('desc')), cidade=x['cidade'] or ('Itajaí' if 'itajaí' in x['onde'].lower() else 'Camboriú' if 'camboriú' in x['onde'].lower() and 'balneário' not in x['onde'].lower() else 'Balneário Camboriú'), lat=None, lon=None, local_exato=False,
            marcado_mobiliado=False, publicado=time.strftime('%Y-%m-%d', time.localtime(f['criado'])) if f.get('criado') else '',
            anunciante=f.get('anunciante') or 'Facebook', fotos=f.get('fotos') or ([x['foto']] if x['foto'] else [])))
    return out
