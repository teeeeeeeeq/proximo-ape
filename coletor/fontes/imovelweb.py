"""Imovelweb (imovelweb.com.br): aluguel de apartamentos com 2+ quartos em Balneário Camboriú.

O site fica atrás do Cloudflare: por urllib (e curl) toda página e a API respondem 403 "Just a moment...".
Por isso usa o Chrome headless do app: abre a listagem uma vez (o desafio passa sozinho em ~3 s, sem
disfarce nenhum além de ser um navegador de verdade) e, de dentro da página, com os cookies dela, chama
as mesmas rotas que o próprio site usa:
  POST /rplis-api/postings  {city: 108357 (Balneário Camboriú), tipoDePropiedad: 2 (apartamentos),
                             tipoDeOperacion: 2 (aluguel), habitacionesminimo: 2, preciomax: 9000}
        -> 30 anúncios por página: preço, condomínio, quartos, área, bairro, rua, coordenada e 8 fotos.
  GET  /propriedades/<slug>-<id>.html  (a ficha)  -> objeto avisoInfo embutido: descrição com quebras de
        linha, todas as fotos em 1200px, suítes, "Mobiliado", data de publicação, status (ONLINE/OFFLINE),
        visibilidade do mapa e, no HTML, "Condomínio R$ X · IPTU R$ Y".
Temporada (tipoDeOperacion 4) fica de fora: lá o preço é por diária ou pacote, não mensal.

As fichas mudam pouco, então ficam guardadas em imovelweb_fichas.json (ao lado de anuncios.json): a cada
rodada só se leem as fichas novas e as ~100 mais antigas. Sem esse arquivo (primeira rodada), lê todas até
o prazo; as que não couberem saem só com os dados da lista.
"""
import html, json, os, re, time, unicodedata

NOME = 'Imovelweb'
USA_CHROME = True

BASE = 'https://www.imovelweb.com.br'
LISTA = BASE + '/apartamentos-aluguel-balneario-camboriu-sc-mais-de-2-quartos-menos-9000-reales.html'
CIDADE = '108357'          # id do Imovelweb para Balneário Camboriú
import server
PRECO_MIN, PRECO_MAX = server.ALUGUEL_MIN, server.ALUGUEL_MAX
PRAZO = 240                # segundos desde o início: depois disso não pede mais fichas
LOTE = 8                   # fichas pedidas ao mesmo tempo
RELER_H = 12               # ficha guardada há mais que isso pode ser relida...
RELER_MAX = 100            # ...mas no máximo estas por rodada (as mais antigas primeiro)
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'dados', 'imovelweb_fichas.json')

CORPO = {"q": None, "direccion": None, "moneda": 3, "preciomin": PRECO_MIN, "preciomax": PRECO_MAX, "services": "", "general": "",
         "searchbykey": "", "amenidades": "", "caracteristicasprop": None, "comodidades": "", "disposicion": None, "roomType": "",
         "outside": "", "areaPrivativa": "", "areaComun": "", "multipleRets": "", "tipoDePropiedad": "2", "subtipoDePropiedad": None,
         "tipoDeOperacion": "2", "garages": None, "antiguedad": None, "expensasminimo": None, "expensasmaximo": None,
         "habitacionesminimo": 2, "habitacionesmaximo": 0, "ambientesminimo": 0, "ambientesmaximo": 0, "banos": None,
         "superficieCubierta": 1, "idunidaddemedida": 1, "metroscuadradomin": None, "metroscuadradomax": None, "tipoAnunciante": "ALL",
         "grupoTipoDeMultimedia": "", "publicacion": None, "sort": "more_recent", "etapaDeDesarrollo": "", "auctions": None,
         "polygonApplied": None, "idInmobiliaria": None, "excludePostingContacted": "", "banks": "", "places": "", "condominio": "",
         "pagina": 1, "city": CIDADE, "province": None, "zone": None, "valueZone": None, "subZone": None, "coordenates": None}

JS_POST = ("fetch('/rplis-api/postings',{method:'POST',credentials:'include',headers:{'content-type':'application/json',"
           "'accept':'application/json, text/plain, */*'},body:JSON.stringify(%s)})"
           ".then(r=>r.text().then(t=>JSON.stringify({s:r.status,t:t}))).catch(e=>JSON.stringify({s:-1,t:''+e}))")

# Cada ficha tem ~500 KB; devolve ao Python só o objeto avisoInfo e a linha de taxas.
JS_FICHAS = r'''(async (urls)=>JSON.stringify(await Promise.all(urls.map(async u=>{try{
  const r=await fetch(u,{credentials:'include'}); const t=await r.text();
  const i=t.indexOf('const avisoInfo'); const j=t.indexOf('const dataLayerInfo', i);
  const k=t.indexOf('class="price-extra"');
  return {u:u, s:r.status, a:i>=0?t.slice(i, j>i?j:i+300000):'', x:k>=0?t.slice(k, k+600):''}
}catch(e){return {u:u, s:-1, a:'', x:''}}}))))(%s)'''


# ---------- texto e números

def norm(s):
    return unicodedata.normalize('NFKD', str(s or '').lower()).encode('ascii', 'ignore').decode()


def texto(s):
    """HTML da descrição -> texto limpo, mantendo parágrafos."""
    s = str(s or '').replace('\r', '').replace('﻿', '')
    s = re.sub(r'(?i)<li[^>]*>', '\n• ', s)
    s = re.sub(r'(?i)<br\s*/?>|</(p|div|li|h\d|tr|ul|ol)>', '\n', s)
    s = html.unescape(re.sub(r'<[^>]+>', ' ', s)).replace('\xa0', ' ')
    linhas = [re.sub(r'[ \t]+', ' ', x).strip() for x in s.split('\n')]
    out = '\n'.join(x for x in linhas if x != '•')
    return re.sub(r'\n{3,}', '\n\n', out).strip()


def linha(s):
    return re.sub(r'\s+', ' ', html.unescape(str(s or ''))).strip()


def valor(x):
    """5750 / '5.750' / 'R$ 1.200,50' -> float ; 0, '', None -> None"""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x) or None
    s = re.sub(r'[^\d,]', '', str(x)).replace(',', '.')
    try:
        return float(s) or None
    except ValueError:
        return None


def inteiro(x):
    v = valor(x)
    return int(v) if v else None


def caixa(s):
    s = linha(s)
    if s and (s.islower() or s.isupper()):
        s = ' '.join(w if w in ('de', 'da', 'do', 'das', 'dos', 'e') else w.capitalize() for w in s.lower().split(' '))
    return s


# ---------- ficha (objeto avisoInfo: JS com trechos em JSON)

_DEC = json.JSONDecoder(strict=False)


def campo(bloco, chave):
    """Valor de 'chave': ... dentro do avisoInfo. JSON ({...}, [...], "...") ou string JS entre aspas simples."""
    m = re.search(r"""['"]%s['"]\s*:\s*""" % re.escape(chave), bloco)
    if not m:
        return None
    i = m.end()
    c = bloco[i:i + 1]
    try:
        if c in ('{', '['):
            return _DEC.raw_decode(bloco, i)[0]
        if c == '"':
            s = re.match(r'"((?:[^"\\]|\\.)*)"', bloco[i:], re.S).group(1)
            try:
                return json.loads('"' + s.replace("\\'", "'") + '"', strict=False)
            except ValueError:
                return s.replace('\\n', '\n').replace('\\t', ' ').replace('\\"', '"').replace("\\'", "'")
        if c == "'":
            return re.match(r"'((?:[^'\\]|\\.)*)'", bloco[i:], re.S).group(1).replace("\\'", "'")
    except Exception:
        return None
    m = re.match(r'[\w.-]+', bloco[i:])
    return m.group(0) if m else None


def fotos_ficha(fotos):
    out = []
    for x in sorted((x for x in fotos or [] if isinstance(x, dict)), key=lambda x: x.get('order') or 0):
        if str(x.get('multimediaTypeId', '2')) != '2':
            continue
        u = x.get('url1200x1200') or x.get('resizeUrl1200x1200') or x.get('url730x532')
        if u:
            out.append(u)
    return out


def ficha(r):
    """Resposta do fetch de uma ficha -> o que interessa dela, compacto (vai para o cache)."""
    d = {'s': r.get('s'), 'lido': int(time.time())}
    a = r.get('a') or ''
    d['online'] = r.get('s') == 200   # anúncio finalizado: 410 e status OFFLINE
    if not a:
        return d
    st, res = campo(a, 'status'), str(campo(a, 'reserved')).lower()
    d['online'] = d['online'] and st in (None, 'ONLINE') and res != 'true'
    d['desc'] = texto(campo(a, 'description'))
    d['pub'] = campo(a, 'publicationDateFormatted') or ''
    m = re.search(r"'visibility'\s*:\s*'(\w+)'", a)   # a do mapa (a do endereço vem em JSON, com aspas duplas)
    d['mapa'] = m.group(1) if m else ''
    main = campo(a, 'mainFeatures') or {}
    d['main'] = {k: (main.get(k) or {}).get('value') for k in ('CFT2', 'CFT4', 'CFT100', 'CFT101') if isinstance(main.get(k), dict)}
    d['mob'] = 'mobiliado' in rotulos(campo(a, 'generalFeatures'))
    d['fotos'] = fotos_ficha(campo(a, 'pictures'))
    x = linha(re.sub(r'<[^>]+>', ' ', r.get('x') or ''))
    m = re.search(r'IPTU\s*R\$\s*([\d.,]+)', x, re.I)
    d['iptu'] = valor(m.group(1)) if m else None
    m = re.search(r'Condom[ií]nio\s*R\$\s*([\d.,]+)', x, re.I)
    d['cond'] = valor(m.group(1)) if m else None
    return d


def rotulos(gerais):
    out = []
    for grupo in (gerais or {}).values() if isinstance(gerais, dict) else []:
        if isinstance(grupo, dict):
            out += [norm(f.get('label')) for f in grupo.values() if isinstance(f, dict)]
    return out


def carregar_cache():
    try:
        with open(CACHE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def gravar_cache(c):
    try:
        tmp = CACHE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(c, f, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        os.replace(tmp, CACHE)
    except Exception:
        pass


# ---------- navegador

def abrir(b, progresso):
    progresso('abrindo o Imovelweb')
    b.go(LISTA, 3)
    for _ in range(25):
        t = b.js('document.title') or ''
        u = b.js('location.href') or ''
        if 'imovelweb.com.br' in u and t and not re.search(r'just a moment|um momento|attention required|access denied|blocked|verif', t, re.I):
            return
        time.sleep(1.5)
    raise RuntimeError('o Imovelweb bloqueou o navegador automático (o desafio do Cloudflare não passou)')


def pagina(b, n, progresso):
    corpo = dict(CORPO, pagina=n)
    r = {'s': -1}
    for t in range(3):
        r = json.loads(b.js(JS_POST % json.dumps(corpo)) or '{"s":-1,"t":""}')
        if r['s'] == 200:
            try:
                return json.loads(r['t'])
            except ValueError:
                pass
        if r['s'] in (403, 429, 503) and t == 0:
            abrir(b, progresso)  # o Cloudflare pediu o desafio de novo: reabre a página e tenta outra vez
        else:
            time.sleep(3 * (t + 1))
    raise RuntimeError(f'a busca do Imovelweb falhou (HTTP {r["s"]})')


def listar(b, progresso):
    postings, total, paginas, n = {}, None, 1, 1
    while n <= paginas and n <= 60:
        progresso(f'lista: página {n}' + (f' de {paginas}' if total is not None else ''))
        d = pagina(b, n, progresso)
        pg = d.get('paging') or {}
        if total is None:
            total = pg.get('total') or 0
            paginas = pg.get('totalPages') or 1
        L = d.get('listPostings') or []
        for p in L:
            if p.get('postingId') and p.get('url'):
                postings.setdefault(str(p['postingId']), p)
        if not L:
            break
        n += 1
        time.sleep(0.4)
    return postings, total


def ler_fichas(b, postings, cache, progresso, fim):
    """Lê as fichas que faltam no cache (e as mais antigas), em lotes, até o prazo. Atualiza o cache."""
    agora = time.time()
    novas = [k for k in postings if k not in cache]
    velhas = sorted((k for k in postings if k in cache and agora - cache[k].get('lido', 0) > RELER_H * 3600),
                    key=lambda k: cache[k].get('lido', 0))[:RELER_MAX]
    fila = novas + velhas
    url = {k: BASE + postings[k]['url'] if postings[k]['url'].startswith('/') else postings[k]['url'] for k in fila}
    feitas, bloqueios = 0, 0
    for _ in range(2):  # segunda volta: só as que falharam por rede
        falhas = []
        for i in range(0, len(fila), LOTE):
            if time.time() > fim:
                return
            progresso(f'fichas: {feitas} de {len(fila)}')
            lote = fila[i:i + LOTE]
            try:
                res = json.loads(b.js(JS_FICHAS % json.dumps([url[k] for k in lote])) or '[]')
            except Exception:
                res = []
            por_url = {r.get('u'): r for r in res if isinstance(r, dict)}
            for k in lote:
                r = por_url.get(url[k]) or {'s': -1}
                try:
                    if r.get('s') in (200, 404, 410):
                        cache[k] = ficha(r)
                        feitas += 1
                        bloqueios = 0
                    else:
                        falhas.append(k)
                        bloqueios += r.get('s') in (403, 429, 503)
                except Exception:
                    falhas.append(k)
            if bloqueios >= 2 * LOTE:  # o site começou a barrar: fica com o que já tem
                return
            time.sleep(0.2)
        fila = falhas
        if not fila:
            break
        time.sleep(3)


# ---------- montagem

def aluguel_de(p):
    for op in p.get('priceOperationTypes') or []:
        t = op.get('operationType') or {}
        if str(t.get('operationTypeId')) == '2' or norm(t.get('name')) == 'aluguel':
            for pr in op.get('prices') or []:
                if str(pr.get('currencyId')) == '3' or pr.get('currency') == 'R$':
                    return valor(pr.get('amount'))
    return None


def iptu_mensal(v, desc):
    """O campo IPTU do Imovelweb é livre: quase todos põem o mensal, alguns o anual. Vale o que a descrição
    disser ('IPTU R$ 1.850/ano'); sem pista, acima de R$ 1.000 é tratado como anual."""
    if not v or v < 5:            # 1,00 = anunciante que não quis informar
        return None
    for m in re.finditer(r'iptu[^0-9\n]{0,25}[\d.,]+([^\n]{0,16})', norm(desc)):
        depois = m.group(1)
        if re.search(r'/\s*ano|anua|ao ano|por ano', depois):
            return round(v / 12, 2)
        if re.search(r'/\s*mes|mensa|ao mes|por mes', depois):
            return v
    return round(v / 12, 2) if v > 1000 else v


AVENIDAS = {'atlantica': 'Avenida Atlântica', 'brasil': 'Avenida Brasil', 'do estado': 'Avenida do Estado',
            'normando tedesco': 'Avenida Normando Tedesco'}


def rua_de(end):
    r = linha((end or {}).get('name') if isinstance(end, dict) else '')
    r = re.sub(r'[\s,.-]+$', '', r)
    if norm(r) in ('', 'rua', 'r', 'avenida', 'av'):
        return ''
    r = re.sub(r'^(.*[^\s,])[\s,]+1$', r'\1', r)   # número "1" = anunciante que não quis pôr o número
    if re.match(r'\d{3,4}\b', r):                   # '3000 514' = Rua 3000, 514 (as ruas numeradas de BC)
        r = 'Rua ' + r
    base = norm(re.split(r'[\s,]+\d', r)[0]).strip()
    if base in AVENIDAS:
        r = AVENIDAS[base] + r[len(re.split(r'[\s,]+\d', r)[0]):]
    return r


def tem_numero(rua):
    """'Rua 1061, 245' / 'Avenida Brasil 1500' -> True ; 'Rua 3100' / 'Avenida Atlântica' -> False"""
    n = norm(rua)
    n = re.sub(r'^(rua|r\.?|avenida|av\.?|alameda|travessa|estrada|rodovia)\s+(\d+[a-z]?\b)?', '', n)
    return bool(re.search(r'(?<![\w])\d{1,5}(?![\w])', n))


def geo_de(p):
    g = ((p.get('postingLocation') or {}).get('postingGeolocation') or {}).get('geolocation') or {}
    try:
        lat, lon = float(g.get('latitude')), float(g.get('longitude'))
    except (TypeError, ValueError):
        return None
    return (lat, lon) if -27.2 < lat < -26.8 and -48.8 < lon < -48.5 else None


def pontos_genericos(postings):
    """Coordenadas que o Imovelweb dá para endereços diferentes (centro da cidade ou do bairro): não são do imóvel."""
    ruas = {}
    for p in postings.values():
        g = geo_de(p)
        if g:
            ruas.setdefault((round(g[0], 4), round(g[1], 4)), set()).add(norm(rua_de((p.get('postingLocation') or {}).get('address'))))
    return {k for k, v in ruas.items() if len(v) >= 2}


def montar(k, p, f, genericos):
    f = f or {}
    tipo = norm((p.get('realEstateType') or {}).get('name'))
    if tipo and not tipo.startswith('apartamento'):
        return None
    loc = (p.get('postingLocation') or {}).get('location') or {}
    cidade = loc.get('name') if loc.get('label') == 'CIUDAD' else (loc.get('parent') or {}).get('name') or ''
    if cidade and norm(cidade) != 'balneario camboriu':
        return None
    bairro = loc.get('name') if loc.get('label') not in ('CIUDAD', 'PROVINCIA', 'PAIS') else ''
    main = {c: (v or {}).get('value') for c, v in (p.get('mainFeatures') or {}).items() if isinstance(v, dict)}
    main.update({c: v for c, v in (f.get('main') or {}).items() if v})
    quartos = inteiro(main.get('CFT2'))
    if quartos is not None and quartos < 2:
        return None
    aluguel = aluguel_de(p)
    if aluguel is not None and not PRECO_MIN <= aluguel <= PRECO_MAX:
        return None

    rua = rua_de((p.get('postingLocation') or {}).get('address'))
    # coordenada só quando o anúncio mostra o mapa com endereço exato, com número, e o ponto não é genérico
    g = geo_de(p)
    exato = bool(g and f.get('mapa', 'EXACT') == 'EXACT' and tem_numero(rua)
                 and (round(g[0], 4), round(g[1], 4)) not in genericos)
    lat, lon = (g if exato else (None, None))

    desc = f.get('desc') or texto(p.get('descriptionNormalized'))
    cond = valor((p.get('expenses') or {}).get('amount')) or f.get('cond')
    if cond is not None and cond < 10:
        cond = None
    ativo = (p.get('status') in (None, 'ONLINE') and not p.get('reserved') and f.get('online', True))
    fotos = list(f.get('fotos') or [])
    if not fotos:  # sem ficha: as 8 fotos da lista, trocando 720x532 pelo tamanho grande (mesmo arquivo, 1200px)
        for x in ((p.get('visiblePictures') or {}).get('pictures') or []):
            u = x.get('url730x532') or x.get('url360x266')
            if u:
                fotos.append(re.sub(r'/(720x532|360x266)/', '/1200x1200/', u))
    fotos = list(dict.fromkeys(('https:' + u if u.startswith('//') else u).split('?')[0] for u in fotos if isinstance(u, str)))
    pub = f.get('pub') or ''
    return dict(
        id='W' + k, fonte='Imovelweb', url=BASE + p['url'] if p['url'].startswith('/') else p['url'],
        titulo=re.sub(r'[\s,;·–-]+$', '', caixa(p.get('title') or p.get('generatedTitle'))), desc=desc, ativo=bool(ativo),
        aluguel=aluguel, cond=cond, iptu=iptu_mensal(f.get('iptu') or valor(p.get('iptu')), desc),
        quartos=quartos, suites=inteiro(main.get('CFT4')),
        area=valor(main.get('CFT101')) or valor(main.get('CFT100')),
        bairro=caixa(bairro), rua=rua, cidade=cidade or 'Balneário Camboriú',
        lat=lat, lon=lon, local_exato=exato,
        marcado_mobiliado=bool(f.get('mob')) or 'mobiliado' in rotulos(p.get('generalFeatures')),
        publicado=pub[:10] if re.match(r'\d{4}-\d\d-\d\d', pub) else '',
        anunciante=linha((p.get('publisher') or {}).get('name')),
        fotos=[u for u in fotos if u.startswith('http')])


def buscar(chrome, progresso):
    t0 = time.time()
    if chrome is None:
        raise RuntimeError('o Imovelweb precisa do navegador (o Cloudflare bloqueia a leitura direta)')
    try:
        abrir(chrome, progresso)
        postings, total = listar(chrome, progresso)
    except RuntimeError:
        raise
    except Exception as ex:
        raise RuntimeError(f'falha ao ler a lista do Imovelweb: {str(ex)[:120]}')
    if not postings:
        if total == 0:
            return []
        raise RuntimeError('o Imovelweb não devolveu anúncios')
    cache = {k: v for k, v in carregar_cache().items() if k in postings}
    try:
        ler_fichas(chrome, postings, cache, progresso, t0 + PRAZO)
    finally:
        gravar_cache(cache)
    genericos = pontos_genericos(postings)
    out = []
    for k, p in postings.items():
        try:
            o = montar(k, p, cache.get(k), genericos)
            if o:
                out.append(o)
        except Exception:
            pass
    progresso(f'{len(out)} anúncios ({sum(1 for k in postings if k in cache)} com ficha)')
    return out
