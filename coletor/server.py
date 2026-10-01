#!/usr/bin/python3
"""Próximo Apê: núcleo do coletor (roda no GitHub Actions a cada 3 horas).

Lê ZAP, OLX e cada leitor em fontes/*.py, completa distâncias e grava docs/anuncios.json.
"""
import html, json, math, os, re, shutil, subprocess, tempfile, threading, time, unicodedata, urllib.parse, urllib.request
import websocket

DIR = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(DIR)
PORT = 8765
def _achar_chrome():
    for c in (os.environ.get('CHROME_BIN'), '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
              shutil.which('google-chrome') if shutil.which('google-chrome') else None, shutil.which('google-chrome-stable'),
              shutil.which('chromium'), shutil.which('chromium-browser')):
        if c and os.path.exists(c):
            return c
    return 'google-chrome'


CHROME = _achar_chrome()
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'
HUMAINS = (-26.98307, -48.64116)
PRAIA = json.load(open(os.path.join(DIR, 'praia.json')))
RUAS_ARQ = os.path.join(DIR, 'ruas.json')
LOCK = threading.Lock()
STATUS = {'rodando': False, 'etapa': '', 'erro': '', 'ultima': None}


def path(n):
    os.makedirs(os.path.join(RAIZ, 'dados'), exist_ok=True)
    return os.path.join(RAIZ, 'dados', n)


def load(n, default):
    try:
        return json.load(open(path(n)))
    except Exception:
        return default


def save(n, data):
    tmp = path(n + '.tmp')
    json.dump(data, open(tmp, 'w'), ensure_ascii=False)
    os.replace(tmp, path(n))


# ---------- texto e números

def norm(s):
    return unicodedata.normalize('NFKD', str(s or '').lower()).encode('ascii', 'ignore').decode()


def clean(s):
    s = re.sub(r'<br\s*/?>', '\n', str(s or ''))
    s = html.unescape(re.sub(r'<[^>]+>', ' ', s))
    return re.sub(r'[ \t]+', ' ', re.sub(r'\n\s*\n+', '\n\n', s)).strip()


def num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = re.sub(r'[^\d,]', '', str(x)).replace(',', '.')
    try:
        return float(s) if s else None
    except ValueError:
        return None


def dist(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h))


def mobilia(texto, marcado):
    n = norm(texto)
    if re.search(r'semi\s*-?\s*mob|parcialmente mobi|meio mobiliad', n):
        return 'semi'
    if re.search(r'(nao|sem) (e |esta )?mobiliad|sem mobilia|desmobiliad|nao possui mobilia|imovel vazio|sem moveis', n):
        return 'nao'
    if re.search(r'mobiliad|mobilhad|mobilia completa|porteira fechada', n):
        return 'texto'
    return 'marcado' if marcado else 'sem info'


def temporada(texto):
    n = norm(texto)
    return bool(re.search(r'temporada|ate (o mes de |o dia )?(\d+ de )?dezembro|marco a dezembro|abril a dezembro|(8|9|10) meses|'
                          r'diaria|por dia|airbnb|reveillon', n))


RUAS = json.load(open(RUAS_ARQ)) if os.path.exists(RUAS_ARQ) else {}


# Onde procurar: cidade -> bairros aceitos (None = a cidade toda)
REGIOES = {
    'balneario camboriu': None,
    'camboriu': None,
    'itajai': {'praia brava', 'praia brava de itajai', 'fazendinha', 'cabecudas', 'fazenda', 'ressacada'},
}
LAZER = ('POOL', 'GYM', 'SAUNA', 'PLAYGROUND', 'SPORTS_COURT', 'SPA', 'TENNIS_COURT', 'SQUASH', 'GAMES_ROOM', 'KIDS_AREA')


def na_regiao(cidade, bairro):
    c = norm(cidade).strip() or 'balneario camboriu'
    if c not in REGIOES:
        return False
    return REGIOES[c] is None or norm(bairro).strip() in REGIOES[c]


PREDIOS_OSM = json.load(open(os.path.join(DIR, 'predios_osm.json'))) if os.path.exists(os.path.join(DIR, 'predios_osm.json')) else {}
PREDIOS = {}
CENTROS = {'balneario camboriu': (-26.99, -48.635), 'camboriu': (-27.025, -48.654), 'itajai': (-26.93, -48.645)}
_PREF = r'(?:edificio|edif\.?|ed\.|residencial|resid\.|condominio|cond\.|predio)'
_PREDIO_RUIM = {'frente', 'frente mar', 'mar', 'novo', 'alto padrao', 'de alto padrao', 'com', 'de', 'do', 'da', 'residencial', 'conta',
                'possui', 'tem', 'fica', 'localizado', 'e', 'a', 'o', 'incluso', 'sem', 'com elevador', 'com piscina', 'bem',
                'praia brava', 'praia dos amores', 'barra sul', 'barra norte', 'centro', 'nacoes', 'pioneiros', 'estados', 'aririba',
                'tabuleiro', 'monte alegre', 'fazenda', 'fazendinha', 'cabecudas', 'ressacada', 'vila real', 'municipios',
                'balneario camboriu', 'camboriu', 'itajai', 'quadra mar'}
_NOME_PARA = {'o', 'a', 'os', 'as', 'com', 'de', 'do', 'da', 'no', 'na', 'em', 'e', 'que', 'localizado', 'localizada', 'situado', 'apartamento',
              'apto', 'conta', 'possui', 'tem', 'oferece', 'fica', 'incluso', 'inclusos', 'iptu', 'completo', 'com', 'ao', 'para', 'frente',
              'quadra', 'mar', 'centro', 'bairro', 'rua', 'avenida', 'av', 'r', 'novo', 'alto', 'padrao', 'mobiliado', 'sendo', 'sem', 'possuem',
              'central', 'elevador', 'localizacao', 'fechado', 'portaria', 'seguro', 'moderno', 'conservado', 'antigo', 'se', 'voce',
              'residencial', 'predio', 'bem', 'excelente', 'otimo', 'lindo', 'amplo', 'com', 'garagem', 'vaga', 'salao', 'possui', 'dispoe',
              'balneario', 'camboriu', 'itajai', 'bc', 'sc', 'brasil', 'praia', 'barra', 'nacoes', 'pioneiros', 'tabuleiro', 'locacao', 'anual'}


def chave_predio(nome):
    k = norm(nome)
    k = re.sub(r'^' + _PREF + r'\s+', '', k)
    k = re.sub(r'[^a-z0-9 ]', ' ', k)
    return re.sub(r'\s+', ' ', k).strip()


def carregar_predios():
    PREDIOS.clear()
    PREDIOS.update(PREDIOS_OSM)
    PREDIOS.update(load('predios.json', {}))


_NUM = {'um': 1, 'uma': 1, 'dois': 2, 'duas': 2, 'tres': 3, 'quatro': 4, 'cinco': 5, 'seis': 6, 'sete': 7, 'oito': 8, 'nove': 9,
        'dez': 10, 'onze': 11, 'doze': 12, 'treze': 13, 'catorze': 14, 'quatorze': 14, 'quinze': 15, 'dezesseis': 16, 'dezessete': 17,
        'dezoito': 18, 'dezenove': 19, 'vinte': 20, 'trinta': 30, 'quarenta': 40, 'cinquenta': 50, 'sessenta': 60, 'setenta': 70,
        'oitenta': 80, 'noventa': 90, 'cem': 100, 'cento': 100, 'duzentos': 200, 'trezentos': 300, 'quatrocentos': 400,
        'quinhentos': 500, 'seiscentos': 600, 'setecentos': 700, 'oitocentos': 800, 'novecentos': 900}
_PAL = '(?:' + '|'.join(sorted(_NUM, key=len, reverse=True)) + '|mil|e)'


def _extenso(t):
    """(texto já normalizado) 'rua tres mil cento e cinquenta' -> 'rua 3150'."""
    def conv(m):
        total = cur = 0
        for w in m.group(2).split():
            if w == 'e':
                continue
            if w == 'mil':
                total, cur = total + (cur or 1) * 1000, 0
            elif w in _NUM:
                cur += _NUM[w]
        return m.group(1) + str(total + cur)
    return re.sub(r'\b(rua\s+)((?:%s)(?:\s+%s)*)\b(?!\s+d[aeo]s?\b)' % (_PAL, _PAL), conv, t)


def nome_rua(r):
    r = _extenso(re.sub(r'\b(rua|av)(\d)', r'\1 \2', norm(r)))
    r = re.split(r'\s+-\s+|,|;|\(|\bn[o0]?[º°.]?\s*\d|\bnumero\b|\bno\s+\d', r)[0]
    r = re.sub(r'^r\.?\s+', 'rua ', r.strip())
    r = re.sub(r'^av\.?\s+', 'avenida ', r)
    r = r.replace('terceira avenida', '3a avenida').replace('quarta avenida', '4a avenida').replace('quinta avenida', '5a avenida')
    r = re.sub(r'\b([345])\s*(a|ª|º)\s*av(enida)?\b', r'\1a avenida', r)
    return re.sub(r'\s+', ' ', r).strip(' .')


def achar_rua(nome, cidade=''):
    r = nome_rua(nome)
    cl = RUAS.get(r) or RUAS.get(re.sub(r'\s+\d+[a-z]?$', '', r)) if r else None
    if not cl:
        return None
    if len(cl) > 1:
        cen = CENTROS.get(norm(cidade).strip() or 'balneario camboriu', CENTROS['balneario camboriu'])
        cl = sorted(cl, key=lambda x: dist((x['lat'], x['lon']), cen))
    x = cl[0]
    if x['ext'] <= 1500:
        return dict(lat=x['lat'], lon=x['lon'], modo='rua')
    if x['iqr'] <= 200:
        return dict(praia=x['praia'], modo='avenida')
    return None


_RUA_INI = r'\b(rua|r\.|avenida|av\.?|alameda|travessa)\s+'
_PERTO = r'(proxim\w*|perto|a \d+ ?(m|metros|km)\b|metros d|minutos d|passos d|poucos|quadras? d|ao lado|paralela|entre (a|as|o)\b|acesso|saida|via\b|esquina|frente (a|para)|atras d|rodovia|fundos|vista|caminhando|ate a|ate o)'
_FORTE = r'(endereco|localizad[oa]|situad[oa]|fica na|fica no|fica em|localizacao|end\.|localizada em)'


def ruas_da_descricao(texto):
    """[(rua, forte)] na ordem em que aparecem; só nomes que existem no mapa de ruas."""
    t = _extenso(re.sub(r'\b(rua|av)(\d)', r'\1 \2', norm(texto)))
    out = []
    for m in re.finditer(_RUA_INI, t):
        resto = re.split(r'[,;.\n|()!?:/]|\s-\s|\s–\s', t[m.end():m.end() + 70])[0].split()
        achou = None
        for n in range(min(6, len(resto)), 0, -1):
            cand = nome_rua(m.group(1) + ' ' + ' '.join(resto[:n]))
            if cand in RUAS and not re.fullmatch(r'(rua|avenida) (principal|central|sem saida)', cand):
                achou = (cand, n)
                break
        if not achou:
            continue
        antes = t[max(0, m.start() - 35):m.start()]
        depois = t[m.end():m.end() + 70]
        if re.search(_PERTO + r'[^.\n]{0,20}$', antes):
            continue
        if re.search(r'\b(sala|loja|creci|whats\w*|telefone|fone|escritorio|imobiliaria|atendimento|horario)\b', depois[:60]) or \
                re.search(r'\b(creci|whats\w*|telefone|fone|escritorio|imobiliaria|corretor\w*|contato|visite|nossa loja|nosso endereco)\b', t[max(0, m.start() - 90):m.start()]):
            continue
        forte = bool(re.search(_FORTE + r'[^.\n]{0,25}$', antes)) or bool(
            re.match(r'\s*' + r'\S+(?:\s+\S+){%d}' % (achou[1] - 1) + r'\s*,?\s*(n[o0]?[º°.]?\s*)?\d{1,5}\b', depois))
        out.append((achou[0], forte))
    return out


def cidade_do_ponto(lat, lon):
    return min(CENTROS, key=lambda c: dist((lat, lon), CENTROS[c]))


def _predio_ok(v, cidade, texto_n):
    c = norm(cidade).strip() or 'balneario camboriu'
    if (v.get('cidade') or cidade_do_ponto(v['lat'], v['lon'])) != c:
        return False
    if re.search(r'frente (ao|para o|pro)? ?mar|quadra (do )?mar|beira mar|pe na areia', texto_n):
        return min(dist((v['lat'], v['lon']), p) for p in PRAIA) <= 350
    return True


def predio_da_descricao(texto, extra=(), cidade=''):
    tn = norm(texto)
    t = ' ' + re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]', ' ', tn)) + ' '
    nome_visto = ''
    for m in re.finditer(r'\b' + _PREF.replace(r'\.', '') + r'[ \t]+((?:[a-z0-9]+[ \t]+){0,5}[a-z0-9]+)', tn.replace('.', ' ')):
        pal = re.sub(r'[^a-z0-9 ]', ' ', m.group(1)).split()
        for n in range(min(5, len(pal)), 0, -1):
            k = ' '.join(pal[:n])
            if k in _PREDIO_RUIM or len(k) < 4:
                continue
            if n < len(pal) and pal[n] in ('de', 'do', 'da', 'dos', 'das', 'di', 'del', 'e'):
                continue   # "jardim das nações" não é o "edifício jardim"
            if k in PREDIOS and _predio_ok(PREDIOS[k], cidade, tn):
                return k, PREDIOS[k]
        if not nome_visto and re.match(r'(edificio|edif|ed |residencial)', m.group(0)):
            nome = []
            for w in pal[:5]:
                if w in ('de', 'do', 'da', 'dos', 'das', 'di', 'del', 'la', 'le', 'e') and nome:
                    nome.append(w)
                    continue
                if w in _NOME_PARA or w in ('esta', 'sera', 'foi'):
                    break
                nome.append(w)
                if len([x for x in nome if len(x) > 3]) >= 3:
                    break
            while nome and nome[-1] in ('de', 'do', 'da', 'dos', 'das', 'di', 'del', 'la', 'le', 'e'):
                nome.pop()
            if nome and len(' '.join(nome)) >= 4:
                nome_visto = ' '.join(nome)
    for k in extra:
        k = chave_predio(k or '')
        if len(k) >= 4 and k in PREDIOS and k not in _PREDIO_RUIM and _predio_ok(PREDIOS[k], cidade, tn):
            return k, PREDIOS[k]
    for k, v in PREDIOS.items():   # nomes compostos ("columbus tower") citados sem "edifício"
        if ' ' in k and len(k) >= 9 and k not in _PREDIO_RUIM and (' ' + k + ' ') in t and not k.startswith(('rua ', 'avenida ')) \
                and (v.get('fonte') == 'zap' or re.search(r'\b(tower|towers|residence|residencial|home|club|garden|park|village|ville|palace|house|flat|condominio|edificio)\b', k)) \
                and _predio_ok(v, cidade, tn):
            return k, v
    return (nome_visto, None) if nome_visto else ('', None)


carregar_predios()


def praia_pelo_texto(texto):
    """Distância da praia que o próprio anúncio declara ("a 180 m da praia", "quadra mar", "5 minutos da praia")."""
    t = norm(texto)
    m = re.search(r'(\d{1,2}\.\d{3}|\d{2,4})\s*(m|mts|metros)\b[^.\n]{0,15}?\b(da|do|ate a|ate o|de)\s+(praia|mar|areia|orla|beira[ -]mar)', t)
    if m:
        v = int(m.group(1).replace('.', ''))
        if 5 <= v <= 3000:
            return v
    m = re.search(r'(\d{1,2})\s*(min|minutos)\b[^.\n]{0,12}?(a pe|caminhando|andando)?[^.\n]{0,6}\b(da|do|ate a)\s+(praia|mar)', t)
    if m and not re.search(r'carro|onibus|bicicleta|bike|uber|de moto', m.group(0) + t[m.end():m.end() + 15]):
        return int(m.group(1)) * 75
    if re.search(r'frente (ao |para o |pro )?mar|beira[ -]mar|pe na areia|vista frontal (para o|pro) mar', t):
        return 30
    if re.search(r'(uma|1|primeira) quadra (do|da) (mar|praia)|quadra[ -]mar|1a quadra', t):
        return 100
    if re.search(r'(duas|2|segunda) quadras? (do|da) (mar|praia)|2a quadra', t):
        return 230
    if re.search(r'(tres|3|terceira) quadras? (do|da) (mar|praia)|3a quadra', t):
        return 350
    return None


def localizar_pelo_bairro(itens):
    """Último recurso: anúncio sem rua nem pista recebe a distância típica do bairro (dos anúncios com local exato)."""
    import statistics
    ref = {}
    for o in itens:
        if o.get('local_exato') and o.get('praia_m') is not None and o.get('humains_m') is not None and o.get('bairro'):
            ref.setdefault((norm(o['bairro']), norm(o.get('cidade')) or 'balneario camboriu'), []).append(o)
    for o in itens:
        if o.get('praia_m') is None and o.get('bairro'):
            g = ref.get((norm(o['bairro']), norm(o.get('cidade')) or 'balneario camboriu'), [])
            if len(g) >= 3:
                o['praia_m'] = round(statistics.median(x['praia_m'] for x in g))
                o['humains_m'] = round(statistics.median(x['humains_m'] for x in g))
                o['local_aprox'] = 'bairro'


def rua_para_mostrar(r):
    return re.split(r'\s+-\s+de\s|\s+-\s+até\s|\s+-\s+lado', r or '')[0].strip()


def condominio(site, texto):
    """(valor, fonte): o do site; senão o da descrição; 0 se o texto diz que está incluso; None se ninguém diz."""
    t = norm(texto)
    incluso = re.search(r'(condominio|taxas)[^.\n]{0,30}inclus|inclus[oa]s?[^.\n]{0,25}(condominio|taxas)|ja com (condominio|taxas|as taxas)'
                        r'|valor total|total com (condominio|taxas)|pacote|sem condominio|isento de condominio', t)
    if site and site >= 50:
        return float(site), 'site'
    m = re.search(r'condominio[^\d\n]{0,25}?r\$?\s*(\d{1,2}\.?\d{3}|\d{2,4})(,\d{2})?\b', t)
    if m:
        v = float(m.group(1).replace('.', ''))
        if 80 <= v <= 4000:
            return v, 'descrição'
    if incluso:
        return 0.0, 'incluso'
    return None, ''


def _mesmo_predio(o):
    if o.get('predio'):
        return 'p:' + chave_predio(o['predio'])
    if o.get('local_exato') and o.get('lat') is not None:
        return 'c:%.4f,%.4f' % (o['lat'], o['lon'])   # ~10 m: mesmo prédio
    return ''


def estimar_condominios(itens):
    """Sem condomínio informado: mediana do mesmo prédio; senão dos 8 anúncios mais parecidos (área e aluguel)
    do mesmo bairro, com ou sem lazer; senão do bairro. Teste (deixa-um-de-fora): erro mediano 1% / 14% / 24%."""
    import statistics
    base = [o for o in itens if o.get('cond_fonte') in ('site', 'descrição') and (o.get('cond') or 0) >= 80
            and o.get('aluguel') and o['aluguel'] <= 20000]
    por_predio, por_grupo, por_bairro = {}, {}, {}
    for o in base:
        k = _mesmo_predio(o)
        if k:
            por_predio.setdefault(k, []).append(o)
        z = (norm(o.get('bairro')), norm(o.get('cidade')))
        por_grupo.setdefault(z + (bool(o.get('lazer')),), []).append(o)
        por_bairro.setdefault(z, []).append(o)
    for o in itens:
        o.pop('cond_est', None)
        o.pop('cond_base', None)
        if o.get('cond') is None and o.get('aluguel'):
            est, onde = None, ''
            k = _mesmo_predio(o)
            g = [x for x in por_predio.get(k, []) if x is not o] if k else []
            if g:
                if o.get('area') and all(x.get('area') for x in g):
                    est = statistics.median(x['cond'] / x['area'] for x in g) * o['area']
                else:
                    est = statistics.median(x['cond'] for x in g)
                onde = 'mesmo prédio'
            else:
                z = (norm(o.get('bairro')), norm(o.get('cidade')))
                g = [x for x in por_grupo.get(z + (bool(o.get('lazer')),), []) if x is not o]
                if len(g) < 5:
                    g = [x for x in por_bairro.get(z, []) if x is not o]
                if len(g) >= 5:
                    area = o.get('area') or 0
                    viz = sorted(g, key=lambda x: (abs(math.log((x.get('area') or 70) / area)) if area else 0)
                                 + abs(math.log(x['aluguel'] / o['aluguel'])))[:8]
                    est = statistics.median(x['cond'] for x in viz)
                    onde = 'anúncios parecidos do bairro'
            if est:
                o['cond_est'], o['cond_base'] = int(round(min(max(est, 150), 3500) / 10) * 10), onde
        o['fixo'] = round(o['aluguel'] + (o['cond'] if o.get('cond') is not None else o.get('cond_est') or 0) + (o.get('iptu') or 0)) \
            if o.get('aluguel') else None


def completar(o):
    o['bairro'] = re.sub(r'^(bairro\s+)?(d[aeo]s?\s+)', '', (o.get('bairro') or '').strip(), flags=re.I).strip()
    o['bairro'] = o['bairro'][:1].upper() + o['bairro'][1:]
    if norm(o['bairro']) == 'praia brava de itajai':
        o['bairro'] = 'Praia Brava'
    txt = (o.get('titulo') or '') + '\n' + (o.get('desc') or '')
    txt_lazer = norm(txt)
    o['lazer'] = bool(o.get('marcado_lazer') or re.search(
        r'piscina|academia|lazer completo|area de lazer|sauna|\bspa\b|playground|brinquedoteca|quadra (poli|esport)|fitness', txt_lazer))
    if 'cond_site' not in o:
        o['cond_site'] = o.get('cond')
    o['cond'], o['cond_fonte'] = condominio(o['cond_site'], txt)
    o['fixo'] = round((o['aluguel'] or 0) + (o['cond'] or 0) + (o['iptu'] or 0)) if o['aluguel'] else None
    # endereço: o que está na descrição vale mais que o cadastro do site
    if 'rua_site' not in o:
        o['rua_site'], o['lat_site'], o['lon_site'] = o.get('rua') or '', o.get('lat'), o.get('lon')
        o['exato_site'] = bool(o.get('local_exato'))
    rua, lat, lon, exato, fonte = rua_para_mostrar(o['rua_site']), o['lat_site'], o['lon_site'], o['exato_site'], 'site'
    nome_predio, pos = predio_da_descricao(txt, (o.get('predio_site'),), o.get('cidade'))
    ruas = ruas_da_descricao(txt)
    forte = [r for r, f in ruas if f]
    rua_desc = forte[0] if forte else (ruas[0][0] if ruas and not rua else '')
    if pos:
        lat, lon, exato, fonte = pos['lat'], pos['lon'], True, 'prédio'
        if rua_desc:
            rua = rua_desc.title()
    elif rua_desc and nome_rua(rua_desc) != nome_rua(rua):
        rua, lat, lon, exato, fonte = rua_desc.title(), None, None, False, 'descrição'
    o['rua'], o['lat'], o['lon'], o['local_exato'], o['local_fonte'] = rua, lat, lon, exato, fonte
    o['predio'] = (PREDIOS.get(nome_predio, {}).get('nome') if pos else '') or (nome_predio.title() if nome_predio else '') or (o.get('predio_site') or '')
    o.pop('local_aprox', None)
    o.pop('praia_rua', None)
    if o['lat'] is None and o['rua']:
        x = achar_rua(o['rua'], o.get('cidade'))
        if x and x['modo'] == 'rua':
            o['lat'], o['lon'], o['local_aprox'] = x['lat'], x['lon'], 'rua'
        elif x:
            o['praia_rua'], o['local_aprox'] = x['praia'], 'avenida'
    if o.get('lat') is not None and o.get('lon') is not None:
        o['praia_m'] = round(min(dist((o['lat'], o['lon']), p) for p in PRAIA))
        o['humains_m'] = round(dist((o['lat'], o['lon']), HUMAINS))
    else:
        o['praia_m'], o['humains_m'] = o.get('praia_rua'), None
        if o['praia_m'] is None:
            p = praia_pelo_texto(txt)
            if p is not None:
                o['praia_m'], o['local_aprox'] = p, 'texto'
    o['mobilia'] = mobilia(o['titulo'] + ' ' + o['desc'], o.get('marcado_mobiliado'))
    o['temporada'] = temporada(o['titulo'] + ' ' + o['desc'])
    return o


def de_zap(w):
    L = w['listing']
    pi = next((x for x in (L.get('pricingInfos') or []) if x.get('businessType') == 'RENTAL'), {})
    a = L.get('address') or {}
    pt = a.get('point') or {}
    return completar(dict(
        id='Z' + str(L['id']), fonte='ZAP', url='https://www.zapimoveis.com.br' + (w.get('link') or {}).get('href', ''),
        titulo=clean(L.get('title')), desc=clean(L.get('description')),
        ativo=L.get('status') == 'ACTIVE' and (pi.get('rentalInfo') or {}).get('period') in ('MONTHLY', None),
        aluguel=num(pi.get('price')), cond=num(pi.get('monthlyCondoFee')), iptu=round((num(pi.get('yearlyIptu')) or 0) / 12) or None,
        quartos=(L.get('bedrooms') or [None])[0], suites=(L.get('suites') or [None])[0], area=(L.get('usableAreas') or [None])[0],
        bairro=a.get('neighborhood') or '', rua=a.get('street') or '', cidade=a.get('city') or '', lat=pt.get('lat') or pt.get('approximateLat'),
        lon=pt.get('lon') or pt.get('approximateLon'), local_exato=a.get('precision') in ('ROOFTOP', 'RANGE_INTERPOLATED'),
        marcado_mobiliado='FURNISHED' in (L.get('amenities') or []), publicado=(L.get('createdAt') or '')[:10],
        marcado_lazer=any(x in (L.get('amenities') or []) for x in LAZER),
        predio_site=L.get('condominiumName') or '',
        anunciante=(w.get('account') or {}).get('name') or '',
        fotos=[m['url'].replace('{description}', 'foto').replace('{action}', 'fit-in').replace('{width}x{height}', '1200x900')
               for m in (w.get('medias') or []) if m.get('type') == 'IMAGE']))


def de_olx(a, det):
    P = {x.get('name'): x.get('value') for x in a.get('properties') or []}
    ld = a.get('locationDetails') or {}
    det = det or {}
    return completar(dict(
        id='O' + str(a['listId']), fonte='OLX', url=a.get('url'), titulo=clean(a.get('subject')), desc=det.get('body', ''),
        ativo=True, aluguel=num(a.get('priceValue')), cond=num(P.get('condominio')), iptu=num(P.get('iptu')),
        quartos=int(P['rooms']) if str(P.get('rooms', '')).isdigit() else None, suites=None, area=num(P.get('size')),
        bairro=ld.get('neighbourhood') or '', cidade=ld.get('municipality') or '', rua=det.get('addr') or '',
        lat=det.get('lat'), lon=det.get('lon'), local_exato=False,
        marcado_mobiliado='Mobiliado' in (P.get('re_features') or ''), publicado=det.get('publicado') or '',
        marcado_lazer=bool(re.search(r'Piscina|Academia|Sauna|Quadra|Playground', P.get('re_complex_features') or '')),
        anunciante=det.get('user') or '', fotos=det.get('images') or [i.get('original') for i in a.get('images') or []]))


# ---------- navegador

class Chrome:
    def __init__(self, port=9671):
        self.port, self.mid = port, 0

    def __enter__(self):
        self.prof = tempfile.mkdtemp(prefix='apto_app_')
        self.p = subprocess.Popen([CHROME, '--headless=new', '--no-first-run', '--disable-gpu', '--remote-allow-origins=*', '--no-sandbox', '--disable-dev-shm-usage',
                                   f'--remote-debugging-port={self.port}', f'--user-data-dir={self.prof}', f'--user-agent={UA}',
                                   '--window-size=1400,3000', 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                tabs = json.load(urllib.request.urlopen(f'http://127.0.0.1:{self.port}/json'))
                page = [t for t in tabs if t.get('type') == 'page'][0]
                break
            except Exception:
                time.sleep(0.25)
        self.ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=120, suppress_origin=True)
        self.cmd('Page.enable')
        self.cmd('Network.setUserAgentOverride', userAgent=UA, acceptLanguage='pt-BR,pt;q=0.9')
        return self

    def __exit__(self, *a):
        try:
            self.ws.close()
        except Exception:
            pass
        self.p.kill()
        self.p.wait()
        shutil.rmtree(self.prof, ignore_errors=True)

    def cmd(self, method, **params):
        self.mid += 1
        self.ws.send(json.dumps({'id': self.mid, 'method': method, 'params': params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get('id') == self.mid:
                return m.get('result', {})

    def go(self, url, wait=8):
        self.cmd('Page.navigate', url=url)
        time.sleep(wait)

    def js(self, code):
        r = self.cmd('Runtime.evaluate', expression=code, awaitPromise=True, returnByValue=True)
        return r.get('result', {}).get('value')


def _desc_da_pagina_zap(page):
    """Descrição escrita pelo anunciante (no payload RSC da página do anúncio; a <meta> é texto padrão do site)."""
    parts = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,(".*?")\]\)</script>', page, re.S):
        try:
            parts.append(json.loads(m.group(1)))
        except Exception:
            pass
    rsc = ''.join(parts)
    cands = []
    for m in re.finditer(r'"description":"((?:[^"\\]|\\.){40,})"', rsc):
        try:
            d = json.loads('"' + m.group(1) + '"')
        except Exception:
            continue
        if not re.match(r'(Apartamentos?|Imóveis|Casas?) (em|para) ', d):
            cands.append(d)
    cn = re.search(r'"condominiumName":"([^"]{2,80})"', rsc)
    return (clean(max(cands, key=len)) if cands else ''), (cn.group(1) if cn else '')


def _zap_descricoes(b, itens, progresso, prazo=330):
    """Descrição de cada anúncio novo do ZAP (página do anúncio, 1 a cada 1,3 s: o ZAP bloqueia pedidos rápidos).
    Guarda em dados/zap_desc.json; o que não der tempo fica para a próxima busca."""
    cache = load('zap_desc.json', {})
    faltam = [o for o in itens if not o.get('desc') and o['id'] not in cache and (o.get('aluguel') or 0) <= 9000]
    if faltam:
        b.go('https://www.zapimoveis.com.br/aluguel/apartamentos/sc+balneario-camboriu/', 6)
        t0, seguidos, pausa, k = time.time(), 0, 1.3, 0
        while k < len(faltam):
            o = faltam[k]
            if time.time() - t0 > prazo:
                progresso(f'descrições: {len(faltam) - k} ficam para a próxima busca')
                break
            if k % 20 == 0:
                progresso(f'descrições {k} de {len(faltam)} (pausa {pausa:.1f}s)')
            try:
                r = json.loads(b.js("(()=>{const c=new AbortController();setTimeout(()=>c.abort(),15000);"
                                    "return fetch(%s,{signal:c.signal}).then(r=>r.text().then(t=>JSON.stringify({s:r.status,t:t})))"
                                    ".catch(e=>JSON.stringify({s:-1,t:''}))})()" % json.dumps(o['url'])) or '{"s":-1,"t":""}')
            except Exception:
                break
            if r['s'] == 429:   # o ZAP limita o volume: espera, desacelera e tenta de novo
                seguidos += 1
                if seguidos > 5:
                    break
                pausa = min(pausa + 0.5, 5)
                if time.time() - t0 + 60 * seguidos > prazo:
                    break
                time.sleep(60 * seguidos)
                continue
            seguidos = 0
            k += 1
            if r['s'] in (404, 410):
                cache[o['id']] = {'d': '', 'c': ''}
            elif r['s'] == 200:
                d, c = _desc_da_pagina_zap(r['t'])
                cache[o['id']] = {'d': d, 'c': c}
            if k % 40 == 0:
                save('zap_desc.json', cache)
            time.sleep(pausa)
        save('zap_desc.json', cache)
    for o in itens:
        x = cache.get(o['id'])
        if isinstance(x, dict):
            if not o.get('desc') and x.get('d'):
                o['desc'] = x['d']
            if not o.get('predio_site') and x.get('c'):
                o['predio_site'] = x['c']
            completar(o)
    return itens


def buscar_zap(b, progresso=lambda m: None):
    """ZAP e VivaReal têm o mesmo estoque e a mesma API. Tenta pela página do ZAP; se bloquear, pela do VivaReal."""
    origens = [('https://www.zapimoveis.com.br/aluguel/apartamentos/sc+balneario-camboriu/2-quartos/', '.zapimoveis.com.br', 'ZAP', 'https://www.zapimoveis.com.br'),
               ('https://www.vivareal.com.br/aluguel/santa-catarina/balneario-camboriu/apartamento_residencial/', '.vivareal.com.br', 'VIVAREAL', 'https://www.vivareal.com.br')]
    falhas = []
    for pagina, dominio, portal, site in origens:
        b.go(pagina, 12)
        try:
            itens = _zap_api(b, progresso, dominio, portal, site)
            try:
                return _zap_descricoes(b, itens, progresso)
            except Exception:
                return itens
        except RuntimeError as ex:
            falhas.append(f"{portal}: {ex} (página: {(b.js('document.title') or '')[:60]})")
    # plano C: a API bloqueou (acontece em servidores); lê as próprias páginas de busca, 30 por página
    try:
        itens = _zap_paginas(b, progresso)
        try:
            return _zap_descricoes(b, itens, progresso)
        except Exception:
            return itens
    except RuntimeError as ex:
        falhas.append(f'páginas: {ex}')
    raise RuntimeError('; '.join(falhas))


def de_zap_pagina(x):
    r = (x.get('prices') or {}).get('rental') or {}
    a = x.get('address') or {}
    c = a.get('coordinates') or {}
    am = x.get('amenities') or {}
    one = lambda v: (v or [None])[0] if isinstance(v, list) else v
    fotos = [(m.get('dangerousSrc') or '').replace('{description}', 'foto').replace('{action}', 'fit-in').replace('{width}x{height}', '1200x900')
             for m in ((x.get('medias') or {}).get('images') or [])]
    return completar(dict(
        id='Z' + str(x['id']), fonte='ZAP', url=x.get('href') or '', titulo=clean(x.get('title')), desc='',
        ativo=r.get('period') in ('MONTHLY', None), aluguel=num(r.get('value')), cond=num(r.get('condominium')) or None,
        iptu=round((num(r.get('iptu')) or 0) / 12) or None, quartos=one(am.get('bedrooms')), suites=one(am.get('suites')),
        area=one(am.get('usableAreas')), bairro=a.get('neighborhood') or '', rua=a.get('street') or '', cidade=a.get('city') or '',
        lat=c.get('latitude'), lon=c.get('longitude'), local_exato=not a.get('isApproximateLocation', True),
        marcado_mobiliado='FURNISHED' in (am.get('values') or []), publicado='',
        marcado_lazer=any(x in (am.get('values') or []) for x in LAZER),
        predio_site=x.get('condominiumName') or '',
        anunciante=(x.get('advertiser') or {}).get('name') or '', fotos=[f for f in fotos if f]))


def _zap_paginas(b, progresso):
    out, dec = {}, json.JSONDecoder()
    for quartos, (cidade, _, slug) in [(q, c) for c in CIDADES_ZAP for q in ('2-quartos', '3-quartos')]:
        total = None
        for p in range(1, 60):
            progresso(f'{cidade}, {quartos}, página {p}' + (f' de {math.ceil(total / 30)}' if total else ''))
            b.go(f'https://www.zapimoveis.com.br/aluguel/apartamentos/{slug}/{quartos}/?pagina={p}', 6)
            page = b.js('document.documentElement.outerHTML') or ''
            parts = []
            for m in re.finditer(r'self\.__next_f\.push\(\[1,(".*?")\]\)</script>', page, re.S):
                try:
                    parts.append(json.loads(m.group(1)))
                except Exception:
                    pass
            t = ''.join(parts)
            i = t.find('"listings":[')
            if i < 0:
                if p == 1 and quartos == '2-quartos' and slug == 'sc+balneario-camboriu':
                    raise RuntimeError('a página não trouxe anúncios')
                break
            L, _ = dec.raw_decode(t[i + len('"listings":'):])
            m = re.search(r'"totalCount":(\d+)', t)
            total = total or (int(m.group(1)) if m else None)
            novos = 0
            for x in L:
                if x.get('business') == 'RENTAL' and str(x.get('id')) not in out:
                    try:
                        out[str(x['id'])] = de_zap_pagina(x)
                        novos += 1
                    except Exception:
                        pass
            if not L or not novos or (total and p * 30 >= total):
                break
    return [o for o in out.values() if na_regiao(o.get('cidade'), o.get('bairro'))]


CIDADES_ZAP = [('Balneário Camboriú', 'BR>Santa Catarina>NULL>Balneario Camboriu', 'sc+balneario-camboriu'),
               ('Camboriú', 'BR>Santa Catarina>NULL>Camboriu', 'sc+camboriu'),
               ('Itajaí', 'BR>Santa Catarina>NULL>Itajai', 'sc+itajai')]


def _zap_api(b, progresso, dominio, portal, site):
    out = []
    for cidade, loc, _ in CIDADES_ZAP:
        out += _zap_api_cidade(b, progresso, dominio, portal, site, cidade, loc)
    return [o for o in out if na_regiao(o.get('cidade'), o.get('bairro'))]


def _zap_api_cidade(b, progresso, dominio, portal, site, cidade, loc):
    out = []
    for quartos in ('2', '3'):
        frm = 0
        while True:
            progresso(f'{cidade}, {quartos} quartos, {frm} lidos ({portal})')
            q = urllib.parse.urlencode({'business': 'RENTAL', 'categoryPage': 'RESULT', 'listingType': 'USED', 'unitTypes': 'APARTMENT',
                                        'usageTypes': 'RESIDENTIAL', 'bedrooms': quartos, 'addressCity': cidade,
                                        'addressState': 'Santa Catarina', 'addressLocationId': loc,
                                        'size': '30', 'from': str(frm), 'portal': portal, 'sort': 'MOST_RECENT'}, quote_via=urllib.parse.quote)
            code = ("fetch('https://glue-api.zapimoveis.com.br/v4/listings?%s',{headers:{'x-domain':'%s'}})"
                    ".then(r=>r.text().then(t=>JSON.stringify({s:r.status,t:t}))).catch(e=>JSON.stringify({s:-1,t:''+e}))") % (q, dominio)
            r = json.loads(b.js(code) or '{"s":-1,"t":""}')
            if r['s'] != 200:
                time.sleep(5)
                r = json.loads(b.js(code) or '{"s":-1,"t":""}')
                if r['s'] != 200:
                    raise RuntimeError(f"a API respondeu {r['s']} {r['t'][:60]}")
            d = json.loads(r['t'])
            L = d['search']['result']['listings']
            for w in L:
                o = de_zap(w)
                o['url'] = site + (w.get('link') or {}).get('href', '')
                out.append(o)
            frm += 30
            if not L or frm >= d['search']['totalCount'] or frm >= 1500:
                break
            time.sleep(0.5)
    return out


def rsc_ads(page):
    parts = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,(".*?")\]\)</script>', page, re.S):
        try:
            parts.append(json.loads(m.group(1)))
        except Exception:
            pass
    t = ''.join(parts)
    ads = []
    for m in re.finditer(r'"ads":\[', t):
        i, depth, instr, esc = m.end() - 1, 0, False, False
        for k in range(i, len(t)):
            c = t[k]
            if instr:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    instr = False
            elif c == '"':
                instr = True
            elif c in '[{':
                depth += 1
            elif c in ']}':
                depth -= 1
                if depth == 0:
                    try:
                        ads += [a for a in json.loads(t[i:k + 1]) if isinstance(a, dict) and a.get('listId')]
                    except Exception:
                        pass
                    break
    return ads


def buscar_olx(b, progresso=lambda m: None):
    ads = {}
    for cidade in ('balneario-camboriu', 'camboriu', 'itajai'):
        base = f'https://www.olx.com.br/imoveis/aluguel/apartamentos/estado-sc/norte-de-santa-catarina/{cidade}?ros=2&sf=1'
        total = None
        for p in range(1, 21):
            progresso(f'{cidade}, página {p}' + (f' de {math.ceil(total / 50)}' if total else ''))
            b.go(base + (f'&o={p}' if p > 1 else ''), 6)
            page = b.js('document.documentElement.outerHTML') or ''
            got = rsc_ads(page)
            m = re.search(r'totalOfAds\\?":(\d+)', page)
            total = total or (int(m.group(1)) if m else None)
            if not got:
                if p == 1 and cidade == 'balneario-camboriu':
                    raise RuntimeError('a OLX não devolveu anúncios')
                break
            for a in got:
                ld = a.get('locationDetails') or {}
                if na_regiao(ld.get('municipality'), ld.get('neighbourhood')):
                    ads[str(a['listId'])] = a
            if total and p * 50 >= total:
                break
    # descrição, endereço e todas as fotos: só dos que ainda não temos
    cache = load('olx_detalhes.json', {})
    faltam = [a for k, a in ads.items() if k not in cache and (num(a.get('priceValue')) or 0) <= 9000]
    if faltam:
        b.go(faltam[0]['url'], 6)
        js = r'''(async (urls)=>Promise.all(urls.map(async u=>{try{const r=await fetch(u,{credentials:'include'});const t=await r.text();
                 const m=t.match(/id="initial-data"[^>]*data-json="([^"]*)"/);return {u:u,d:m?m[1]:null}}catch(e){return {u:u,d:null}}})))(%s)'''
        for k in range(0, len(faltam), 6):
            progresso(f'detalhes {k} de {len(faltam)}')
            chunk = faltam[k:k + 6]
            for a, r in zip(chunk, b.js(js % json.dumps([a['url'] for a in chunk])) or []):
                if not (r and r.get('d')):
                    continue
                ad = json.loads(html.unescape(r['d'])).get('ad') or {}
                L = ad.get('location') or {}
                ot = ad.get('origListTime') or ad.get('listTime')
                cache[str(a['listId'])] = dict(body=clean(ad.get('body')), lat=L.get('mapLati'), lon=L.get('mapLong'), addr=L.get('address') or '',
                                               user=(ad.get('user') or {}).get('name') or '',
                                               publicado=time.strftime('%Y-%m-%d', time.localtime(ot)) if isinstance(ot, (int, float)) else '',
                                               images=[i.get('original') for i in ad.get('images') or []])
            if k % 60 == 0:
                save('olx_detalhes.json', cache)
        save('olx_detalhes.json', cache)
    return [de_olx(a, cache.get(k)) for k, a in ads.items()]


def fontes():
    """ZAP e OLX (aqui) + cada leitor em fontes/*.py."""
    import importlib, glob
    lista = [('ZAP', True, buscar_zap), ('OLX', True, buscar_olx)]
    somente = [x.strip().lower() for x in os.environ.get('SOMENTE', '').split(',') if x.strip()]
    for f in sorted(glob.glob(os.path.join(DIR, 'fontes', '*.py'))):
        n = os.path.basename(f)[:-3]
        if n.startswith('_'):
            continue
        try:
            m = importlib.import_module('fontes.' + n)
            importlib.reload(m)
            lista.append((getattr(m, 'NOME', n), getattr(m, 'USA_CHROME', True), m.buscar))
        except Exception as ex:
            lista.append((n, False, lambda *a, ex=ex: (_ for _ in ()).throw(RuntimeError(f'leitor com defeito: {ex}'))))
    if somente:
        lista = [t for t in lista if any(x in norm(t[0]) for x in somente)]
    return lista


def porta_livre():
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def rodar_fonte(nome, usa_chrome, fn, andamento):
    prog = lambda m: andamento.__setitem__(nome, m)
    prog('começando')
    if usa_chrome:
        with Chrome(port=porta_livre()) as b:
            itens = fn(b, prog)
    else:
        itens = fn(None, prog)
    prog('pronto')
    return itens


def atualizar():
    from concurrent.futures import ThreadPoolExecutor
    if not LOCK.acquire(blocking=False):
        return
    STATUS.update(rodando=True, erro='', etapa='abrindo o navegador')
    andamento = {}
    try:
        antigos = load('anuncios.json', {})
        meta = load('meta.json', {})
        info = meta.get('fontes', {})
        novos, erros, ok = {}, [], set()
        lista = fontes()
        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = {ex.submit(rodar_fonte, n, c, f, andamento): n for n, c, f in lista}
            while any(not f.done() for f in futs):
                STATUS['etapa'] = ' · '.join(f'{n}: {m}' for n, m in andamento.items() if m != 'pronto') or 'terminando'
                time.sleep(1)
            agora = time.strftime('%Y-%m-%d %H:%M')
            for f, n in futs.items():
                try:
                    itens = f.result()
                    for o in itens:
                        try:
                            o['_src'] = n
                            novos[o['id']] = completar(o)
                        except Exception:
                            pass
                    ok.add(n)
                    info[n] = dict(n=len(itens), quando=agora, erro='')
                except Exception as e:
                    erros.append(f'{n}: {e}')
                    info[n] = dict((info.get(n) or {}), erro=str(e)[:200], quando=agora)
        if not novos:
            raise RuntimeError('; '.join(erros) or 'nada encontrado')
        predios = load('predios.json', {})
        for o in novos.values():
            k = chave_predio(o.get('predio_site') or '')
            if len(k) >= 4 and k not in _PREDIO_RUIM and o.get('exato_site') and o.get('lat_site') is not None and k not in predios:
                predios[k] = dict(lat=o['lat_site'], lon=o['lon_site'], nome=o['predio_site'], fonte='zap',
                                  cidade=norm(o.get('cidade')).strip() or 'balneario camboriu')
        save('predios.json', predios)
        carregar_predios()
        for o in novos.values():
            completar(o)
        estimar_condominios(list(novos.values()))
        localizar_pelo_bairro(list(novos.values()))
        for k, o in novos.items():
            o['visto_em'] = (antigos.get(k) or {}).get('visto_em') or agora
            if not o.get('desc') and (antigos.get(k) or {}).get('desc'):
                o['desc'] = antigos[k]['desc']
                completar(o)
            o['no_ar'] = True
        rotulo = {n: n for n in ok}
        for k, o in antigos.items():
            if k not in novos:
                o['no_ar'] = False if o.get('_src', o.get('fonte')) in rotulo else o.get('no_ar', True)
                novos[k] = o
        save('anuncios.json', novos)
        meta['fontes'] = info
        save('meta.json', meta)
        STATUS.update(ultima=agora, erro=('Algumas fontes falharam: ' + '; '.join(erros)) if erros else '', etapa='')
    except Exception as ex:
        STATUS.update(erro=str(ex), etapa='')
    finally:
        STATUS['rodando'] = False
        LOCK.release()


