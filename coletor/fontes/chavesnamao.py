"""Chaves na Mão (chavesnamao.com.br): aluguel de apartamentos em Balneário Camboriú, 2+ quartos, até R$ 9.000.

Como lê (verificado em 30/09/2026), sem Chrome e sem login:
  - Listagem: API JSON do próprio site, a mesma que a página usa no mapa:
      /api/realestate/listing/items/?level1=apartamentos-para-alugar&level2=sc-balneario-camboriu
                                     &level3=2-quartos&filtro=pmax:9000&pg=N
    "2-quartos" no site quer dizer 2 ou mais. 15 anúncios por página, pg começa em 0. Depois do último
    resultado de verdade vem um item {"recommendedCount": ...} seguido de recomendações: ignoramos dali em diante.
    Já traz descrição, preço, condomínio, IPTU, endereço e coordenada, mas só as 5 primeiras fotos.
  - Página de cada anúncio (HTML ~60 KB com gzip): traz todas as fotos e a descrição com as quebras de linha.
    Lemos em lotes pequenos (4 ao mesmo tempo). Se uma falhar, fica o que veio da API.
  - Fotos: /imn/1200x0800/N/70/imoveis/<caminho>, o mesmo tamanho que a galeria do site usa (vem em WebP).
  - IPTU: o site diz só "IPTU". A maioria põe o mensal, mas vários põem o anual (ex.: R$ 1.025 com
    "IPTU R$ 85,43 mensal" na descrição). Valor de R$ 800 ou mais, ou acima de 20% do aluguel, é tratado como anual (/12).
  - Coordenada: só quando o anúncio tem rua. Com número, é o prédio (anúncios de imobiliárias diferentes para o
    mesmo endereço caem no mesmo ponto); sem número, é um ponto da rua (local_exato False). Sem rua, o site
    põe um ponto genérico do bairro (11 anúncios do "Centro" no mesmo ponto): descartamos.
"""
import gzip
import html
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import server

NOME = 'Chaves na Mão'
USA_CHROME = False

SITE = 'https://www.chavesnamao.com.br'
API = (SITE + '/api/realestate/listing/items/?level1=apartamentos-para-alugar&level2=%s'
       '&level3=2-quartos&filtro=pmax:9000&pg=%d')
CIDADES = ('sc-balneario-camboriu', 'sc-camboriu', 'sc-itajai')
FOTO = SITE + '/imn/1200x0800/N/70/imoveis/'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'
PRECO_MAX = 9000
LIMITE_SEG = 330  # depois disso, para de abrir páginas de anúncio e fica com o que a API deu


def _get(url, tentativas=3):
    for k in range(tentativas):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'pt-BR,pt;q=0.9',
                                                       'Accept-Encoding': 'gzip'})
            with urllib.request.urlopen(req, timeout=30) as r:
                b = r.read()
                if r.headers.get('Content-Encoding') == 'gzip':
                    b = gzip.decompress(b)
                return b.decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            if e.code in (404, 410) or k == tentativas - 1:
                raise
        except Exception:
            if k == tentativas - 1:
                raise
        time.sleep(2 * (k + 1))


def _norm(s):
    return unicodedata.normalize('NFKD', str(s or '').lower()).encode('ascii', 'ignore').decode()


def _limpa(s):
    s = re.sub(r'<br\s*/?>', '\n', str(s or ''), flags=re.I)
    s = html.unescape(re.sub(r'<[^>]+>', ' ', s))
    s = re.sub(r'[ \t\xa0]+', ' ', s)
    s = re.sub(r' *\n *', '\n', s)
    return re.sub(r'\n{3,}', '\n\n', s).strip()


def _dinheiro(s):
    """'R$ 1.208,50' -> 1208.5; 5000 -> 5000.0; 'R$ Confira' / 0 -> None"""
    if isinstance(s, (int, float)):
        return float(s) or None
    s = re.sub(r'[^\d,]', '', str(s or '')).replace(',', '.')
    try:
        return float(s) or None
    except ValueError:
        return None


def _m2(s):
    try:
        return float(str(s).replace(',', '.')) or None
    except (TypeError, ValueError):
        return None


def _qtd(o):
    c = (o or {}).get('count') if isinstance(o, dict) else None
    return int(c) if isinstance(c, (int, float)) or (isinstance(c, str) and c.isdigit()) else None


# ---------- listagem

def _listar(progresso):
    anuncios = {}
    for c in CIDADES:
        try:
            anuncios.update(_listar_cidade(progresso, c))
        except RuntimeError:
            if c == CIDADES[0]:
                raise
    return list(anuncios.values())


def _listar_cidade(progresso, cidade):
    anuncios, total, paginas, pg = {}, None, None, 0
    while True:
        progresso(f'{cidade[3:]}, página {pg + 1}' + (f' de {paginas}' if paginas else ''))
        try:
            d = json.loads(_get(API % (cidade, pg)))
            if (d.get('metadata') or {}).get('degraded') or not isinstance(d.get('items'), list):
                time.sleep(3)
                d = json.loads(_get(API % (cidade, pg)))
        except Exception as ex:
            if pg == 0:
                raise RuntimeError(f'o Chaves na Mão não respondeu ({str(ex)[:80]})')
            raise RuntimeError(f'o Chaves na Mão parou de responder na página {pg + 1}')
        meta = d.get('metadata') or {}
        if total is None:
            total, paginas = meta.get('realResults'), meta.get('totalPages')
        reais = 0
        for x in d.get('items') or []:
            if 'recommendedCount' in x:  # daqui para baixo são recomendações, não resultados
                break
            if x.get('id'):
                anuncios.setdefault(str(x['id']), x)
                reais += 1
        pg += 1
        if not reais or (paginas and pg >= paginas) or (total and len(anuncios) >= total) or pg >= 200:
            break
        time.sleep(0.2)
    if not anuncios and total != 0:
        raise RuntimeError('o Chaves na Mão não devolveu anúncios')
    return anuncios


def _passa(x):
    L = x.get('location') or {}
    preco = _dinheiro((x.get('prices') or {}).get('rawPrice'))
    return (x.get('transaction') == 'RENT'
            and server.na_regiao((L.get('city') or {}).get('name'), (L.get('neighborhood') or {}).get('name'))
            and (_qtd(x.get('bedrooms')) or 0) >= 2 and (preco is None or preco <= PRECO_MAX))


# ---------- página do anúncio

def _rsc(page):
    partes = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,(".*?")\]\)</script>', page, re.S):
        try:
            partes.append(json.loads(m.group(1)))
        except Exception:
            pass
    return ''.join(partes)


def _detalhe(x):
    """Todas as fotos e a descrição com quebras de linha, do objeto do anúncio embutido na página."""
    t = _rsc(_get(SITE + x['url']))
    m = re.search(r'"data":(\{"id":%s,"title":)' % re.escape(str(x['id'])), t)
    if not m:
        return None
    o, _ = json.JSONDecoder().raw_decode(t, m.start(1))
    fotos = (o.get('pictures') or {}).get('list')
    desc = o.get('description')
    return dict(fotos=fotos if isinstance(fotos, list) and fotos else None,
                desc=desc if isinstance(desc, str) and not desc.startswith('$') else None,
                periodo=o.get('locationPeriod') if isinstance(o.get('locationPeriod'), str) else '')


# ---------- conversão

def _rua_da_foto(x):
    """O nome do arquivo da foto traz a rua mesmo quando o anúncio não mostra o endereço:
    sc-balneario-camboriu-<bairro>-rua-julieta-lins-apartamento-para-alugar-2-quartos-6a650e7a-00.jpg"""
    L = x.get('location') or {}
    ini = '%s-%s-' % ((L.get('city') or {}).get('url') or '', (L.get('neighborhood') or {}).get('url') or '')
    for p in (x.get('pictures') or {}).get('list') or []:
        f = str(p).rsplit('/', 1)[-1]
        if not f.startswith(ini):
            continue
        m = re.match(r'((?:rua|avenida|alameda|travessa|rodovia|estrada|servidao|passagem)-[a-z0-9-]+?)'
                     r'-(?:apartamento|cobertura|[a-z]+)-(?:para-alugar|a-venda)-', f[len(ini):])
        if m:
            pal = m.group(1).split('-')
            return ' '.join(w if w.isdigit() else (w if w in ('de', 'da', 'do', 'das', 'dos', 'e') else w.capitalize())
                            for w in pal)
    return ''


def _foto(p):
    p = str(p)
    return p if p.startswith('http') else FOTO + urllib.parse.quote(p.lstrip('/'), safe='/')


def _anuncio(x, det):
    det = det or {}
    L = x.get('location') or {}
    rua = L.get('street') or {}
    P = x.get('prices') or {}
    aluguel = _dinheiro(P.get('rawPrice'))
    iptu = _dinheiro(P.get('iptuValue'))
    if iptu and (iptu >= 800 or (aluguel and aluguel >= 1500 and iptu > 0.2 * aluguel)):
        iptu = round(iptu / 12, 2)  # veio o anual

    lat = lon = None
    gp = L.get('geoposition')
    if isinstance(gp, dict) and rua.get('name'):
        try:
            lat, lon = float(gp['lat']), float(gp['lon'])
        except (KeyError, TypeError, ValueError):
            lat = lon = None
        centros = [(c or {}).get('geoposition') for c in (L.get('neighborhood'), L.get('city'))]
        if lat is not None and (any(isinstance(g, dict) and _m2(g.get('lat')) == lat and _m2(g.get('lon')) == lon for g in centros)
                                or not (-27.10 < lat < -26.90 and -48.70 < lon < -48.55)):
            lat = lon = None
    exato = lat is not None and bool(re.search(r'\d', str(rua.get('addressNumber') or '')))

    area = x.get('area') or {}
    titulo = _limpa(x.get('title'))
    if 'temporada' in _norm(det.get('periodo')) and 'temporada' not in _norm(titulo):
        titulo += ' (temporada)'
    criado = str(x.get('createdAt') or '')[:10]
    return dict(
        id='C' + str(x['id']), fonte=NOME, url=SITE + x['url'], titulo=titulo,
        desc=_limpa(det.get('desc') or x.get('description') or x.get('descriptionRaw')),
        ativo=bool(x.get('active', True)), aluguel=aluguel, cond=_dinheiro(P.get('condominiumFee')), iptu=iptu,
        quartos=_qtd(x.get('bedrooms')), suites=_qtd(x.get('suites')), area=_m2(area.get('useful')) or _m2(area.get('total')),
        bairro=(L.get('neighborhood') or {}).get('name') or '', rua=rua.get('name') or _rua_da_foto(x),
        cidade=(L.get('city') or {}).get('name') or '', lat=lat, lon=lon, local_exato=exato,
        marcado_mobiliado=any((i or {}).get('name') == 'Mobiliado' for i in x.get('privativeItems') or []),
        publicado=criado if re.match(r'\d{4}-\d{2}-\d{2}$', criado) else '',
        anunciante=(x.get('publisher') or {}).get('name') or '',
        fotos=[_foto(p) for p in (det.get('fotos') or (x.get('pictures') or {}).get('list') or [])])


def buscar(chrome, progresso):
    t0 = time.time()
    itens = [x for x in _listar(progresso) if _passa(x)]
    detalhes = {}

    def um(x):
        try:
            return _detalhe(x)
        except Exception:
            return None

    lote = 24
    with ThreadPoolExecutor(max_workers=4) as ex:
        for k in range(0, len(itens), lote):
            if time.time() - t0 > LIMITE_SEG:
                progresso(f'tempo curto: {len(itens) - k} anúncios ficam só com 5 fotos')
                break
            progresso(f'detalhes {k} de {len(itens)}')
            chunk = itens[k:k + lote]
            for x, d in zip(chunk, ex.map(um, chunk)):
                detalhes[x['id']] = d
            time.sleep(0.3)

    out = []
    for x in itens:
        try:
            out.append(_anuncio(x, detalhes.get(x['id'])))
        except Exception:
            pass
    progresso(f'{len(out)} anúncios')
    return out
