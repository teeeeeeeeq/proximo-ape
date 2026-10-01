"""Leitor dos sites de imobiliárias feitos na plataforma Kenlo (antigo InGaia / imobiliaria.com.br).

Todo site Kenlo tem a mesma API JSON pública, no próprio domínio:
  /api/listings/para-alugar/apartamento/balneario-camboriu?pagina=N   lista (12 por página, com 'count')
  /api/listings/apartamento/balneario-camboriu?finalidade=temporada    temporada/diária (fica fora da lista acima)
  /api/listings/balneario-camboriu?localidade=1                         só coordenadas (as que o site tiver)
  /api/listing/<referência>                                             anúncio completo (todas as fotos, endereço)
Não precisa de navegador: urllib puro.
"""
import html, json, re, time, unicodedata, urllib.parse, urllib.request
from collections import Counter

import server

NOME = 'Imobiliárias (Kenlo)'
USA_CHROME = False
FONTE = 'Imobiliárias (Kenlo)'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'

# (slug do id, nome da imobiliária, domínio). Para incluir outra imobiliária Kenlo, basta uma linha nova.
SITES = (
    ('jpg', 'JPG Imóveis', 'www.jpgimoveis.com.br'),
    ('lafranzoi', 'LA Franzoi', 'www.lafranzoi.com.br'),
    ('conexao', 'Conexão Imobiliária', 'www.imobiliariaconexao.com.br'),
    ('setor', 'Setor Imobiliária', 'www.setorimobiliaria.com.br'),
    ('santini', 'Vinicius Santini', 'www.viniciussantini.com'),
    ('viva', 'Viva Imóveis Itajaí', 'www.vivaimoveisitajai.com.br'),
    ('cati', 'Cati Imóveis', 'www.catiimoveis.com.br'),
)
CIDADES = ('balneario-camboriu', 'camboriu', 'itajai')   # Itajaí: só os bairros do sul (server.na_regiao)
MIN_ALUGUEL, MAX_ALUGUEL = server.ALUGUEL_MIN, server.ALUGUEL_MAX
# quadrado em volta de Balneário Camboriú: coordenada fora disso é geocodificação errada
BC_LAT, BC_LON = (-27.08, -26.89), (-48.74, -48.56)   # BC, Camboriú e o sul de Itajaí
# pontos genéricos que o Kenlo usa quando não acha o endereço (centro da cidade / do bairro Centro)
PONTOS_GENERICOS = {(-26.99107, -48.63521), (-26.99309, -48.63563)}


def norm(s):
    return unicodedata.normalize('NFKD', str(s or '').lower()).encode('ascii', 'ignore').decode()


def clean(s):
    s = re.sub(r'<br\s*/?>', '\n', str(s or '')).replace('\r', '')
    s = html.unescape(re.sub(r'<[^>]+>', ' ', s))
    return re.sub(r'[ \t]+', ' ', re.sub(r'\n\s*\n+', '\n\n', s)).strip()


def primeiro(v):
    """Kenlo manda números como [min, max]; devolve o primeiro como float (ou None)."""
    if isinstance(v, list):
        v = v[0] if v else None
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def valor(v, minimo=10):
    """Preço/taxa: 0 ou 0,01 é 'sob consulta' no Kenlo."""
    v = primeiro(v)
    return round(v, 2) if v is not None and v >= minimo else None


def get_json(url, tentativas=2):
    erro = None
    for t in range(tentativas):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json', 'Accept-Language': 'pt-BR,pt;q=0.9'})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode('utf-8', 'replace'))
        except Exception as ex:
            erro = ex
            time.sleep(2 + 3 * t)
    raise erro


def listar(dom, caminho, progresso, rotulo):
    """Percorre todas as páginas de uma busca da API."""
    itens, pagina, total = [], 1, None
    while pagina <= 40:
        sep = '&' if '?' in caminho else '?'
        d = get_json(f'https://{dom}/api/listings/{caminho}' + (f'{sep}pagina={pagina}' if pagina > 1 else ''))
        total = d.get('count') or 0
        dados = [x for x in d.get('data') or [] if isinstance(x, dict)]
        itens += dados
        progresso(f'{rotulo}: {len(itens)} de {total}')
        if not dados or len(itens) >= total:
            break
        pagina += 1
        time.sleep(0.4)
    return itens


def eh_candidato(x):
    """Filtro básico antes de abrir o anúncio: BC, apartamento, aluguel, 1+ quartos, aluguel na faixa do perfil."""
    fins = x.get('property_purposes') or []
    fins = [fins] if isinstance(fins, str) else fins
    if 'FOR_RENT' not in fins:
        return False
    if not server.na_regiao(x.get('city'), x.get('neighborhood_display') or x.get('neighborhood')) \
            or 'APARTMENT' not in str(x.get('property_type') or ''):
        return False
    q = primeiro(x.get('bedrooms'))
    if q is None or q < 1:   # 1 quarto entra; só fica se tiver espaço para escritório (coletar.py)
        return False
    diaria = str(x.get('rent_payment') or '').upper() in ('DAILY', 'WEEKLY')
    aluguel = valor(x.get('rent_price'), 100)
    return diaria or aluguel is None or MIN_ALUGUEL <= aluguel <= MAX_ALUGUEL


def rua_de(endereco, tipo, bairro):
    """'Avenida Atlântica - de 3511/3512 a 3999/4000, 3640 - Centro - Balneário Camboriú/SC' -> 'Avenida Atlântica'."""
    if not endereco:
        return ''
    seg = re.sub(r',.*$', '', endereco.split(' - ')[0]).strip()
    if not seg or norm(seg) == norm(bairro) or 'camboriu' in norm(seg):
        return ''
    if tipo and not norm(seg).startswith(norm(tipo)):
        seg = f'{tipo} {seg}'
    return seg


def coord_ok(la, lo, genericos):
    if la is None or lo is None:
        return False
    if not (BC_LAT[0] <= la <= BC_LAT[1] and BC_LON[0] <= lo <= BC_LON[1]):
        return False
    return (round(la, 5), round(lo, 5)) not in genericos


def montar(slug, nome_site, dom, x, det, coords, genericos):
    D = det or {}
    g = lambda k: D.get(k) if D.get(k) not in (None, '', [], [0, 0]) else x.get(k)
    ref = x.get('property_full_reference') or D.get('property_full_reference')
    pagamento = str(g('rent_payment') or '').upper()
    diaria = pagamento in ('DAILY', 'WEEKLY')
    aluguel = None if diaria else valor(g('rent_price'), 100)
    iptu = valor(g('property_tax'), 1)
    if iptu and str(g('property_tax_payment') or '').upper() == 'YEARLY':
        iptu = round(iptu / 12, 2)
    area = next((a for a in (primeiro(D.get('private_floor_area')), primeiro(D.get('usable_floor_area')), primeiro(x.get('area')))
                 if a and a > 5), None)
    bairro = clean(g('neighborhood_display') or g('neighborhood'))
    endereco = clean(D.get('full_address') or '')
    rua = rua_de(endereco, D.get('street_type'), bairro) or rua_de(clean(x.get('full_address') or ''), D.get('street_type'), bairro)
    # coordenada: a do anúncio, senão a do mapa de busca do site; descarta a genérica e a fora de BC
    la, lo = primeiro(D.get('latitude')), primeiro(D.get('longitude'))
    if not coord_ok(la, lo, genericos):
        la, lo = coords.get(ref, (None, None))
    if not coord_ok(la, lo, genericos):
        la = lo = None
    exato = la is not None and bool(re.search(r',\s*\d+', endereco))
    fotos = []
    for p in D.get('photos') or x.get('photos') or []:
        u = p.get('picture_full') or p.get('picture_full_fallback') or p.get('picture_thumb')
        if u and str(p.get('media_type') or 'PHOTO').upper() == 'PHOTO' and u not in fotos:
            fotos.append(u if u.startswith('http') else 'https:' + u if u.startswith('//') else f'https://{dom}{u}')
    titulo = clean(D.get('title') or g('heading1') or g('website_title'))
    if diaria:
        dv = valor(g('rent_price'), 1)
        titulo = 'Temporada/diária' + (f' (R$ {dv:.0f}/dia)' if dv and pagamento == 'DAILY' else '') + ' · ' + titulo
    dono = clean(D.get('listing_owner_name'))
    parceiro = dono and D.get('listing_owner_cid') and D.get('agency_cid') and str(D['listing_owner_cid']) != str(D['agency_cid'])
    q, s = primeiro(g('bedrooms')), primeiro(g('suites'))
    return dict(
        id=f'KN{slug}-{ref}', fonte=FONTE, url=f'https://{dom}' + (g('url') or ''), titulo=titulo,
        desc=clean(g('listing_description')), ativo=True, aluguel=aluguel, cond=valor(g('condo_fees'), 10), iptu=iptu,
        quartos=int(q) if q is not None else None, suites=int(s) if s is not None else None, area=area,
        bairro=bairro, rua=rua, cidade=clean(g('city')), lat=la, lon=lo, local_exato=exato,
        marcado_mobiliado='FURNISHED' in (g('amenities') or []), publicado='',
        anunciante=dono if parceiro else nome_site, fotos=fotos)


def buscar(chrome, progresso):
    out, falhas, ok = {}, [], 0
    lidos = []   # (slug, nome, dom, candidatos, coords do site)
    todas_coords = []  # para achar pontos genéricos repetidos entre sites
    for slug, nome_site, dom in SITES:
        try:
            achados = {}
            coords = {}
            for cid in CIDADES:
                for caminho, rot in ((f'para-alugar/apartamento/{cid}', 'aluguel'),
                                     (f'apartamento/{cid}?finalidade=temporada', 'temporada')):
                    try:
                        for x in listar(dom, caminho, progresso, f'{nome_site} ({cid}, {rot})'):
                            if x.get('property_full_reference') and eh_candidato(x):
                                achados.setdefault(x['property_full_reference'], x)
                    except Exception:
                        if cid == CIDADES[0]:
                            raise
                    time.sleep(0.4)
                try:
                    d = get_json(f'https://{dom}/api/listings/{cid}?localidade=1')
                    for c in d.get('coordinates') or []:
                        la, lo = primeiro(c.get('latitude')), primeiro(c.get('longitude'))
                        if la is not None and lo is not None:
                            coords[c.get('property_full_reference')] = (la, lo)
                            todas_coords.append((dom, round(la, 5), round(lo, 5)))
                except Exception:
                    pass
            lidos.append((slug, nome_site, dom, achados, coords))
            ok += 1
        except Exception as ex:
            falhas.append(f'{nome_site}: {ex}')
            progresso(f'{nome_site}: falhou ({ex})')
    if falhas:
        # se um site sumir, os anúncios dele seriam marcados como fora do ar: melhor avisar e não devolver nada
        raise RuntimeError('Kenlo: ' + ('nenhum site respondeu' if not ok else 'site fora do ar') + ' - ' + '; '.join(falhas)[:300])
    # ponto repetido em muitos anúncios de imobiliárias diferentes = geocodificação genérica, não o prédio
    rep = Counter((la, lo) for _, la, lo in todas_coords)
    doms = {}
    for dom, la, lo in todas_coords:
        doms.setdefault((la, lo), set()).add(dom)
    genericos = PONTOS_GENERICOS | {p for p, n in rep.items() if n >= 8 and len(doms[p]) >= 2}
    total = sum(len(a) for *_, a, _c in lidos)
    feitos = 0
    for slug, nome_site, dom, achados, coords in lidos:
        for ref, x in achados.items():
            feitos += 1
            progresso(f'Kenlo: anúncio {feitos} de {total} ({nome_site})')
            det = None
            try:
                det = get_json(f'https://{dom}/api/listing/' + urllib.parse.quote(ref, safe=''))
                if not isinstance(det, dict) or det.get('property_full_reference') != ref:
                    det = None
            except Exception:
                det = None
            try:
                o = montar(slug, nome_site, dom, x, det, coords, genericos)
                out[o['id']] = o
            except Exception as ex:
                progresso(f'Kenlo: erro em {ref}: {ex}')
            time.sleep(0.3)
    return list(out.values())
