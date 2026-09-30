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


def localizar_por_rua(o):
    """Sem coordenada: estima pela rua (ruas curtas) ou só a distância da praia (avenidas paralelas ao mar)."""
    r = norm(o.get('rua') or '')
    r = re.split(r'\s+-\s+|,|\bn[oº°]\b|\bnumero\b', r)[0].strip()
    r = re.sub(r'^r\.?\s', 'rua ', r)
    r = re.sub(r'^av\.?\s', 'avenida ', r)
    r = r.replace('terceira avenida', '3a avenida').replace('quarta avenida', '4a avenida').replace('quinta avenida', '5a avenida')
    r = re.sub(r'\b([345])(a|ª)\s*av(enida)?\b', r'\1a avenida', r)
    cands = [r, re.sub(r'\s+\d+[a-z]?$', '', r)]
    for c in cands:
        x = RUAS.get(c)
        if not x:
            continue
        if x['ext'] <= 1500:
            o['lat'], o['lon'], o['local_aprox'] = x['lat'], x['lon'], 'rua'
            return
        if x['iqr'] <= 200:
            o['praia_rua'] = x['praia']
            o['local_aprox'] = 'avenida'
            return


def completar(o):
    o['bairro'] = re.sub(r'^(bairro\s+)?(d[aeo]s?\s+)', '', (o.get('bairro') or '').strip(), flags=re.I).strip()
    o['bairro'] = o['bairro'][:1].upper() + o['bairro'][1:]
    o['fixo'] = round((o['aluguel'] or 0) + (o['cond'] or 0) + (o['iptu'] or 0)) if o['aluguel'] else None
    o.pop('local_aprox', None)
    o.pop('praia_rua', None)
    if o.get('lat') is None and o.get('rua'):
        localizar_por_rua(o)
    if o.get('lat') is not None and o.get('lon') is not None:
        o['praia_m'] = round(min(dist((o['lat'], o['lon']), p) for p in PRAIA))
        o['humains_m'] = round(dist((o['lat'], o['lon']), HUMAINS))
    else:
        o['praia_m'], o['humains_m'] = o.get('praia_rua'), None
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


def buscar_zap(b, progresso=lambda m: None):
    out = []
    b.go('https://www.zapimoveis.com.br/aluguel/apartamentos/sc+balneario-camboriu/2-quartos/', 10)
    for quartos in ('2', '3'):
        frm = 0
        while True:
            progresso(f'{quartos} quartos, {frm} lidos')
            q = urllib.parse.urlencode({'business': 'RENTAL', 'categoryPage': 'RESULT', 'listingType': 'USED', 'unitTypes': 'APARTMENT',
                                        'usageTypes': 'RESIDENTIAL', 'bedrooms': quartos, 'addressCity': 'Balneário Camboriú',
                                        'addressState': 'Santa Catarina', 'addressLocationId': 'BR>Santa Catarina>NULL>Balneario Camboriu',
                                        'size': '30', 'from': str(frm), 'portal': 'ZAP', 'sort': 'MOST_RECENT'}, quote_via=urllib.parse.quote)
            code = ("fetch('https://glue-api.zapimoveis.com.br/v4/listings?%s',{headers:{'x-domain':'.zapimoveis.com.br'}})"
                    ".then(r=>r.text().then(t=>JSON.stringify({s:r.status,t:t}))).catch(e=>JSON.stringify({s:-1,t:''+e}))") % q
            r = json.loads(b.js(code) or '{"s":-1,"t":""}')
            if r['s'] != 200:
                time.sleep(5)
                r = json.loads(b.js(code) or '{"s":-1,"t":""}')
                if r['s'] != 200:
                    raise RuntimeError(f'o ZAP respondeu {r["s"]}')
            d = json.loads(r['t'])
            L = d['search']['result']['listings']
            out += [de_zap(w) for w in L]
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
    base = 'https://www.olx.com.br/imoveis/aluguel/apartamentos/estado-sc/norte-de-santa-catarina/balneario-camboriu?ros=2&sf=1'
    ads, total = {}, None
    for p in range(1, 21):
        progresso(f'página {p}' + (f' de {math.ceil(total / 50)}' if total else ''))
        b.go(base + (f'&o={p}' if p > 1 else ''), 6)
        page = b.js('document.documentElement.outerHTML') or ''
        got = rsc_ads(page)
        m = re.search(r'totalOfAds\\?":(\d+)', page)
        total = total or (int(m.group(1)) if m else None)
        if not got:
            if p == 1:
                raise RuntimeError('a OLX não devolveu anúncios')
            break
        for a in got:
            ads[str(a['listId'])] = a
        if total and p * 50 >= total:
            break
    # descrição, endereço e todas as fotos: só dos que ainda não temos
    cache = load('olx_detalhes.json', {})
    faltam = [a for k, a in ads.items() if k not in cache and 'camboriu' in norm((a.get('locationDetails') or {}).get('municipality'))
              and (num(a.get('priceValue')) or 0) <= 9000]
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
        for k, o in novos.items():
            o['visto_em'] = (antigos.get(k) or {}).get('visto_em') or agora
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


