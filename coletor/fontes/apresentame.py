"""Leitor dos sites de imobiliárias feitos no sistema Apresenta.me (Bedin, Brava Norte, Sperotto, DESC, Guilherme
Ramalho, I3, Imóveis na Brava, Mega Sul, Nobilitá, Plaza, TAB e Terra).

Os doze sites são o mesmo sistema e usam, no próprio domínio, as mesmas rotas que o navegador usa:
  POST /busca  (FORM_DATA[finalidade]=rent|season, cidade 8357 = Balneário Camboriú, dormitorio=1 -> "1 ou mais")
       -> JSON com o HTML da grade de resultados (18 por vez). Paginação: uns sites andam com LIMIT=<deslocamento>,
          outros (Guilherme) com pg=<página>; mandamos o que a primeira resposta indicar e paramos quando não vem nada novo.
  GET  <link do anúncio>  -> página do imóvel, com um JSON-LD "RealEstateListing" (descrição, fotos, coordenada,
          data de publicação) e os quadros de características, valores (condomínio, IPTU) e localização.
Não precisa de Chrome nem de login: urllib puro. O robots.txt dos sites libera tudo menos /a/, /admin etc.
Os doze domínios ficam atrás do mesmo servidor, que devolve 429 se passar de umas poucas requisições por segundo:
todas as requisições passam por uma só "porta" (uma a cada ~0,45 s, somando todos os sites).
"""
import base64, gzip, html, http.cookiejar, json, re, threading, time, unicodedata, urllib.error, urllib.parse, urllib.request, zlib
from collections import Counter

NOME = 'Imobiliárias (Apresenta.me)'
USA_CHROME = False

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'
SITES = (  # slug do id, domínio, nome da imobiliária
    ('bedin', 'bedinimoveis.com.br', 'Bedin Imóveis'),
    ('bravanorte', 'bravanorteimoveis.com.br', 'Brava Norte Imóveis'),
    ('sperotto', 'sperottoimoveis.com.br', 'Cristina da Sperotto Imóveis'),
    ('desc', 'desc.com.br', 'DESC Imóveis'),
    ('guilherme', 'guilhermeimoveis.com.br', 'Imobiliária Guilherme Ramalho'),
    ('i3', 'i3imobiliaria.com.br', 'I3 Imóveis'),
    ('nabrava', 'imoveisnabrava.com', 'Imóveis na Brava'),
    ('megasul', 'megasulimoveis.com.br', 'Mega Sul Imóveis'),
    ('nobilita', 'nobilitaimobiliaria.com.br', 'Nobilitá Imobiliária'),
    ('plaza', 'plazaimoveis.com.br', 'Plaza Imóveis'),
    ('tab', 'tabimoveis.com.br', 'TAB Imóveis'),
    ('terra', 'imoveisterra.net', 'Terra Imobiliária'),
)
CIDADE_BC = '8357'
import server
PRECO_MIN, PRECO_MAX = server.ALUGUEL_MIN, server.ALUGUEL_MAX
INTERVALO = 0.45      # segundos entre requisições, somando todos os sites (o servidor é um só)
PRAZO = 270           # segundos: depois disso não lê mais fichas (monta o que faltar só com a grade)
TIPOS_APTO = r'apart|cobertura|loft|studio|st[uú]dio|kitnet|kitinete|flat|duplex|triplex|garden|penthouse|apto'


# ---------- texto e números

def norm(s):
    return unicodedata.normalize('NFKD', str(s or '').lower()).encode('ascii', 'ignore').decode()


def texto(s):
    """HTML -> texto limpo, mantendo parágrafos e itens de lista."""
    s = str(s or '')
    s = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', s)
    s = re.sub(r'(?i)<li[^>]*>', '\n• ', s)
    s = re.sub(r'(?i)<br\s*/?>|</(p|div|li|h\d|tr|ul|ol)>', '\n', s)
    s = html.unescape(re.sub(r'<[^>]+>', ' ', s)).replace('\xa0', ' ')
    linhas = [re.sub(r'[ \t]+', ' ', x).strip() for x in s.split('\n')]
    out = '\n'.join(x for x in linhas if x != '•')
    return re.sub(r'\n{3,}', '\n\n', out).strip()


def linha(s):
    return re.sub(r'\s+', ' ', texto(s)).strip()


def valor(x):
    """'5.300,00' -> 5300.0 ; '3.800' -> 3800.0 ; '150.00' -> 150.0 ; '' / '0,00' / 'Consulte' -> None"""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x) or None
    m = re.search(r'\d[\d.,]*', str(x))
    if not m:
        return None
    s = m.group(0).rstrip('.,')
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    elif re.fullmatch(r'\d{1,3}(\.\d{3})+', s):
        s = s.replace('.', '')
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v >= 1 else None  # 0,01 é marcador de "não informado" em alguns sites


def inteiro(x):
    v = valor(x)
    return int(v) if v else None


def caixa(s):
    """'CENTRO' / 'centro' -> 'Centro' (o filtro de bairro do app compara o texto exato)."""
    s = re.sub(r'\s+', ' ', html.unescape(str(s or ''))).strip()
    if s and (s.islower() or s.isupper()):
        s = ' '.join(w if w in ('de', 'da', 'do', 'das', 'dos', 'e') else w.capitalize() for w in s.lower().split(' '))
    return s


def data_iso(s):
    s = str(s or '')
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return m.group(0)
    m = re.match(r'(\d{2})/(\d{2})/(\d{4})', s)
    return f'{m.group(3)}-{m.group(2)}-{m.group(1)}' if m else ''


# ---------- rede: uma porta só para os doze sites

class Porta:
    def __init__(self, intervalo):
        self.lock, self.prox, self.intervalo = threading.Lock(), 0.0, intervalo

    def esperar(self):
        with self.lock:
            agora = time.time()
            t = max(agora, self.prox)
            self.prox = t + self.intervalo
        if t > agora:
            time.sleep(t - agora)

    def pausar(self, s):
        with self.lock:
            self.prox = max(self.prox, time.time() + s)


class Site:
    def __init__(self, slug, dominio, imob, porta):
        self.slug, self.dominio, self.imob, self.porta = slug, dominio, imob, porta
        self.base = 'https://' + dominio
        self.sid, self.geo_escritorio = '', None
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.op.addheaders = [('User-Agent', UA), ('Accept-Language', 'pt-BR,pt;q=0.9'), ('Accept-Encoding', 'gzip')]

    def pedir(self, url, dados=None, tentativas=4):
        ultimo = None
        for t in range(tentativas):
            self.porta.esperar()
            try:
                hdr = {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json, text/javascript, */*; q=0.01'} if dados else {}
                req = urllib.request.Request(url, data=urllib.parse.urlencode(dados).encode() if dados else None, headers=hdr)
                with self.op.open(req, timeout=30) as r:
                    b = r.read()
                    if r.headers.get('Content-Encoding') == 'gzip':
                        b = gzip.decompress(b)
                    return b.decode('utf-8', 'replace')
            except urllib.error.HTTPError as ex:
                ultimo = f'HTTP {ex.code}'
                if ex.code in (404, 410):
                    raise RuntimeError(ultimo)
                if ex.code == 429 or ex.code >= 500:
                    self.porta.pausar(8 * (t + 1))  # o servidor pediu calma: todos os sites esperam
                else:
                    time.sleep(2)
            except Exception as ex:
                ultimo = str(ex)[:80]
                time.sleep(2 * (t + 1))
        raise RuntimeError(ultimo or 'sem resposta')

    def abrir(self):
        page = self.pedir(self.base + '/')
        m = re.search(r'"SESSION_ID":"([^"]*)"', page)
        self.sid = m.group(1) if m else ''
        m = re.search(r'"DOMAIN":"([^"]*)"', page)
        self.dominio_cfg = m.group(1) if m else self.dominio
        # coordenada do escritório (às vezes vira o pino de imóveis sem mapa próprio)
        for j in ld_json(page):
            for g in ((j.get('@graph') or [j]) if isinstance(j, dict) else []):
                geo = (g or {}).get('geo') if isinstance(g, dict) else None
                if isinstance(geo, dict) and g.get('@type') in ('RealEstateAgent', 'Organization', 'LocalBusiness'):
                    try:
                        self.geo_escritorio = (round(float(geo['latitude']), 5), round(float(geo['longitude']), 5))
                    except Exception:
                        pass

    def grade(self, finalidade, deslocamento, pagina):
        form = [('SESSION_ID', self.sid), ('DOMAIN', self.dominio_cfg), ('url', ''), ('SHOW_SEARCH', 'true'),
                ('FORM_DATA[finalidade]', finalidade), ('FORM_DATA[cidade][]', CIDADE_BC), ('FORM_DATA[dormitorio]', '1'),
                ('searchMap', '0'), ('GRID_ONLY', '1'), ('LIMIT', str(deslocamento))]
        if pagina > 1:
            form.append(('pg', str(pagina)))
        d = json.loads(self.pedir(self.base + '/busca', form))
        return d.get('html') or '', int(d.get('total') or 0)


# ---------- leitura da grade

def ld_json(page):
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            out.append(json.loads(m.group(1)))
        except Exception:
            pass
    return out


def precos(h):
    """[(classe, rótulo, valor)] de cada bloco de preço ('Preço de Aluguel (Mensal)', 'Condomínio', 'IPTU'...)."""
    out = []
    for m in re.finditer(r'R\$</span>\s*([\d.,]*)[^<]*(?:</span>\s*)+.*?<span class="ValorDesc\s*([^"]*)">\s*<span[^>]*>([^<]*)',
                         h, re.S):
        out.append((m.group(2).strip(), html.unescape(m.group(3)).strip(), valor(m.group(1))))
    return out


def grupo(padrao, s):
    m = re.search(padrao, s, re.S)
    return m.group(1) if m else ''


def classe(h, nome, tag='span'):
    m = re.search(r'<%s class="%s[ "][^>]*>(.*?)</%s>' % (tag, nome, tag), h, re.S)
    return linha(m.group(1)) if m else ''


def resumo(h, item):
    m = re.search(r'<span class="ResumoItem %s\b.*?<span class="val">([^<]*)</span>' % item, h, re.S)
    return m.group(1).strip() if m else ''


def cartoes(h):
    ms = list(re.finditer(r'<div id="(\d+)" class="Imovel_\d+ ImovelItem', h))
    for k, m in enumerate(ms):
        yield m.group(1), h[m.start(): ms[k + 1].start() if k + 1 < len(ms) else len(h)]


def ler_cartao(bid, s):
    link = re.search(r'<a[^>]+href="([^"]+)"[^>]*class="Title"', s) or re.search(r'href="([^"]+)"[^>]*class="Image ImovelLinkClick"', s)
    tit = re.search(r'class="Title"[^>]*>(.*?)</a>', s, re.S)
    img = re.search(r'<img[^>]+class="BannerImage[^"]*"[^>]*src="(https://[^"]+)"', s)
    tarjas = [linha(x) for x in re.findall(r'class="TarjaImovel[^"]*">.*?<span class="desc">([^<]*)</span>', s, re.S)]
    return dict(id=bid, link=html.unescape(link.group(1)).strip() if link else '', titulo=linha(tit.group(1)) if tit else '',
                tipo=linha(re.search(r'<span class="SubCategoria">([^<]*)', s).group(1)) if 'SubCategoria' in s else '',
                precos=precos(s), rua=classe(s, 'Rua'), bairro=classe(s, 'Bairro'), cidade=classe(s, 'cidade'),
                quartos=resumo(s, 'BEDROOM'), suites=resumo(s, 'SUITE'),
                area=resumo(s, 'AREA_PRIVATE') or resumo(s, 'AREA_USEFUL') or resumo(s, 'AREA_TOTAL'),
                mobilia=classe(s, 'furnishing', 'div'), resumo=texto(re.search(r'<div class="ResumoDescritivo">(.*?)</div>', s, re.S).group(1))
                if 'ResumoDescritivo' in s else '', foto=img.group(1) if img else '', tarja=' / '.join(t for t in tarjas if t),
                detalhes=[linha(x) for x in re.findall(r'<span class="[A-Za-z]+">([^<]{2,60})</span>',
                                                        grupo(r'<div class="groupDetails">(.*?)</div>', s))])


def preco_mensal(ps, finalidade):
    """Aluguel mensal anunciado no bloco de preços (nunca o 'pacote' nem o de venda)."""
    for cls, rot, v in ps:
        r = norm(rot)
        if 'pacote' in r or 'Venda' in cls:
            continue
        if finalidade == 'rent' and 'LocacaoMensal' in cls and 'diari' not in r:
            return v, True
        if finalidade == 'season' and 'Temporada' in cls and 'mensal' in r:
            return v, True
    return None, False


def passa_cartao(c, finalidade):
    if not re.search(TIPOS_APTO, norm(c['tipo'])):
        return False
    if c['cidade'] and norm(c['cidade']).strip() != 'balneario camboriu':
        return False
    q = inteiro(c['quartos'])
    if q is not None and q < 1:   # 1 quarto entra; só fica se tiver espaço para escritório (coletar.py)
        return False
    v, tem = preco_mensal(c['precos'], finalidade)
    if finalidade == 'season' and not tem:
        return False  # temporada só com preço por mês (diárias de férias não servem para morar)
    return v is None or PRECO_MIN <= v <= PRECO_MAX


def ler_grade(site, finalidade, progresso):
    vistos, cards = set(), []
    html1, total = site.grade(finalidade, 0, 1)
    por_pagina = 'PageNumber' in html1  # sites com páginas numeradas ignoram LIMIT e usam pg
    h, pag, desloc = html1, 1, 0
    while True:
        novos = [(i, s) for i, s in cartoes(h) if i not in vistos]
        for i, s in novos:
            vistos.add(i)
            cards.append(ler_cartao(i, s))
        if not novos or len(vistos) >= total or pag >= 40:
            break
        pag, desloc = pag + 1, desloc + 18
        h, _ = site.grade(finalidade, 0 if por_pagina else desloc, pag if por_pagina else 1)
    return cards, total


# ---------- ficha do imóvel

def widget(page, nome):
    i = page.find('WidgetName="%s"' % nome)
    if i < 0:
        return ''
    j = page.find('WidgetName="', i + 20)
    return page[i: j if j > 0 else len(page)]


def decodifica_foto(u):
    """img.apre.me/<token>.jpg: o token é 'c=<empresa>&i=<caminho>&s=<tamanho>[&wi=marca d'água]' comprimido.
    Só serve para não repetir a mesma foto em vários tamanhos; a URL usada é sempre a que o site publicou."""
    try:
        s = u.rsplit('/', 1)[1].split('.')[0].translate(str.maketrans('-_', '+/'))
        q = zlib.decompress(base64.b64decode(s + '=' * (-len(s) % 4)), -15)[::-1].decode('latin1')
        d = dict(x.split('=', 1) for x in q.split('&') if '=' in x)
        return d.get('i'), d.get('s'), 'wi' in d
    except Exception:
        return None, None, None


def fotos_de(lista):
    """Uma URL por foto: prefere a de tamanho 5 (1200-1600 px, a que o próprio site usa) sem marca d'água."""
    melhor, ordem = {}, []
    for u in lista:
        if not isinstance(u, str) or not u.startswith('http'):
            continue
        cam, tam, marca = decodifica_foto(u)
        k = cam or u
        nota = (tam == '5') * 2 + (not marca)
        if k not in melhor:
            ordem.append(k)
            melhor[k] = (nota, u)
        elif nota > melhor[k][0]:
            melhor[k] = (nota, u)
    return [melhor[k][1] for k in ordem]


def ler_ficha(page):
    L = next((j for j in ld_json(page) if isinstance(j, dict) and j.get('@type') == 'RealEstateListing'), {})
    car = widget(page, 'IMOVEL_CARACTERISTICA')
    linhas = {}
    for m in re.finditer(r'<tr class="([a-z_]+)[^"]*">\s*<td class="Label">.*?</td>\s*<td class="Value">(.*?)</td>\s*</tr>', car, re.S):
        linhas.setdefault(m.group(1), linha(re.sub(r'<span class="(buildingId|hour)[^"]*">.*?</span>', '', m.group(2), flags=re.S)))
    val = widget(page, 'IMOVEL_VALOR')
    iptu_txt = classe(val, 'iptuValue')
    iptu_rot = classe(val, 'textIptuValue')
    desc_html = re.search(r'<div class="BoxImovelDesc[^"]*">\s*<div class="TextBox">(.*?)</div>\s*</div>', widget(page, 'IMOVEL_DESC'), re.S)
    loc = widget(page, 'IMOVEL_LOCALIZACAO')
    bai = re.search(r'Bairro:</td>\s*<td class="Value[^"]*">(.*?)</td>', loc, re.S)
    cid = re.search(r'Cidade:</td>\s*<td class="Value[^"]*">(.*?)</td>', loc, re.S)
    grupos = []
    for m in re.finditer(r'<div class="GroupName">([^<]*)</div>(.*?)</div>', widget(page, 'IMOVEL_DETALHE'), re.S):
        itens = [linha(x) for x in re.findall(r'<li[^>]*>(.*?)</li>', m.group(2), re.S)]
        if [x for x in itens if x]:
            grupos.append(f'{linha(m.group(1))}: ' + ', '.join(x for x in itens if x) + '.')
    galeria = re.findall(r'https://img\.apre\.me/[A-Za-z0-9_\-]+\.(?:jpe?g|png|webp)', widget(page, 'IMOVEL_GALERIA') or widget(page, 'BUILDING_SLIDE_GALLERY'))
    return dict(ld=L, linhas=linhas, precos=precos(val), iptu_txt=iptu_txt, iptu_rot=iptu_rot,
                cond=valor(classe(val, 'condominiumValue')), taxa=valor(classe(val, 'taxValue')),
                condicao=classe(val, 'Condicao', 'div'), aceita=classe(val, 'Aceita', 'div'),
                aceita_obs=texto(grupo(r'<div class="AceitaObs">(.*?)</div>', val)),
                desc=texto(desc_html.group(1)) if desc_html else '', rua=classe(loc, 'Rua'),
                bairro=linha(bai.group(1)) if bai else '', cidade=linha(cid.group(1)) if cid else '', grupos=grupos,
                fotos=fotos_de((L.get('image') if isinstance(L.get('image'), list) else []) or galeria))


def iptu_mensal(txt, rot, aluguel):
    v = valor(txt)
    if not v:
        return None
    n = re.search(r'(\d+)\s*parcela', norm(txt + ' ' + rot))
    n = int(n.group(1)) if n else 0
    r = norm(rot + ' ' + txt)
    if 1 < n <= 12:
        # o site mostra 'R$ X divididos em N parcelas', mas cada imobiliária preenche X de um jeito:
        # uns o total do ano (TAB: 1.200 em 12), outros o valor da parcela (Bedin: 74 em 11)
        if v >= 600 or (aluguel and v > aluguel * 0.15):
            return round(v / 12, 2)
        return round(v * n / 12, 2)
    if 'anual' in r or (n == 0 and 'mensal' not in r and (v >= 1000 or (aluguel and v > aluguel / 4))):
        return round(v / 12, 2)
    return v


def rua_sem_numero(s):
    """'Rua 3300 80' -> 'Rua 3300' ; 'Avenida Atlântica 702' -> 'Avenida Atlântica' (o número da casa não interessa)."""
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    m = re.match(r'(?i)((?:rua|avenida|av\.?|alameda|travessa)\s+\d+[a-z]?)\b', s)
    if m:
        return m.group(1)
    return re.sub(r'[\s,]+(n[ºo°.]\s*)?\d+\w*$', '', s).strip()


def montar(site, c, finalidade, f):
    f = f or {}
    L = f.get('ld') or {}
    li = f.get('linhas') or {}
    aluguel, _ = preco_mensal(f.get('precos') or [], finalidade)
    if aluguel is None:
        aluguel, _ = preco_mensal(c['precos'], finalidade)
    cond = f.get('cond') or next((v for cls, r, v in c['precos'] if norm(r).startswith('condom')), None)
    pacotes = [v for cls, r, v in (f.get('precos') or []) + c['precos'] if 'pacote' in norm(r) and 'Venda' not in cls and v]
    pacote = max(pacotes) if pacotes else None
    if pacote and aluguel and pacote > aluguel:
        vt_pacote = f"Pacote de locação (mensal): R$ {pacote:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
    else:
        pacote, vt_pacote = None, ''
    if cond is None and f.get('taxa') and not pacote:
        cond = f.get('taxa')
    iptu = iptu_mensal(f.get('iptu_txt') or '', f.get('iptu_rot') or '', aluguel) if f.get('iptu_txt') else \
        iptu_mensal(str(next((v for cls, r, v in c['precos'] if norm(r) == 'iptu'), '') or ''), '', aluguel)
    # a grade traz o número do cadastro; a ficha varia por modelo de site (a da Terra conta só os quartos sem suíte)
    quartos_txt = li.get('bedroom') or ''
    quartos = inteiro(c['quartos']) or (L.get('numberOfRooms') if isinstance(L.get('numberOfRooms'), int) and
                                        L.get('numberOfRooms') > 0 else None) or inteiro(quartos_txt)
    m = re.search(r'(\d+)\s*su[ií]te', quartos_txt)
    suites = inteiro(c['suites']) or (int(m.group(1)) if m else inteiro(li.get('suite')))
    area = valor(li.get('area_private')) or valor(li.get('area_useful')) or valor(li.get('area_total')) or valor(c['area'])
    mob = li.get('furnishing') or c['mobilia']
    lat = lon = None
    geo = L.get('geo') if isinstance(L.get('geo'), dict) else {}
    try:
        lat, lon = float(geo.get('latitude')), float(geo.get('longitude'))
        if not (-27.2 < lat < -26.8 and -48.8 < lon < -48.45) or (round(lat, 5), round(lon, 5)) == site.geo_escritorio:
            lat = lon = None
    except (TypeError, ValueError):
        lat = lon = None
    rua_ld = str(L['address'].get('streetAddress') or '') if isinstance(L.get('address'), dict) else ''
    titulo = linha(L.get('name')) or c['titulo']
    # descrição completa: aviso de temporada + destaque da tarja + texto do anúncio + valores/taxas + características
    partes = []
    if finalidade == 'season':
        partes.append('Locação por temporada (preço mensal).')
    if c['tarja']:
        partes.append(f"Destaque do anúncio: {c['tarja']}.")
    corpo_ld, corpo_html = texto(L.get('description')), f.get('desc') or ''
    corpo = (corpo_html if len(corpo_html) >= len(corpo_ld) else corpo_ld) or c['resumo']
    if corpo:
        partes.append(corpo)
    vt = [x for x in (f.get('condicao'), f.get('aceita'), f.get('aceita_obs')) if x]
    if f.get('iptu_txt'):
        vt.append(f"{f.get('iptu_rot') or 'IPTU'}: R$ {f['iptu_txt']}")
    if f.get('taxa'):
        vt.append(f"Taxas diversas: R$ {f['taxa']:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'))
    if vt_pacote:
        vt.append(vt_pacote)
    if vt:
        partes.append('Valores e taxas:\n' + '\n'.join(vt))
    ficha = []
    for k, rot in (('category', 'Tipo'), ('stage', 'Estágio'), ('furnishing', 'Mobília'), ('garage', 'Vagas'),
                   ('bathroom', 'Banheiros'), ('accommodates', 'Acomoda'), ('built_year', 'Ano de construção')):
        if li.get(k):
            ficha.append(f'{rot}: {li[k]}')
    if not li and c['mobilia']:
        ficha.append(f"Mobília: {c['mobilia']}")
    carac = list(f.get('grupos') or [])
    if not carac and c['detalhes']:
        carac.append('Características: ' + ', '.join(c['detalhes']) + '.')
    if ficha:
        carac.append(' · '.join(ficha))
    if carac:
        partes.append('\n'.join(carac))
    status = norm(titulo + ' ' + c['tarja'])
    return dict(
        id=f"AP{site.slug}{c['id']}", fonte=NOME, url=c['link'] or L.get('url') or f"{site.base}/imovel/{c['id']}",
        titulo=titulo, desc='\n\n'.join(p for p in partes if p).strip(),
        ativo=not re.search(r'\b(alugad[oa]|locad[oa]|indisponivel)\b', status),
        aluguel=aluguel, cond=cond, iptu=iptu, pacote=pacote, quartos=quartos, suites=suites, area=area,
        bairro=caixa(c['bairro'] or f.get('bairro')), rua=caixa(c['rua'] or f.get('rua') or rua_sem_numero(rua_ld)),
        cidade=caixa(c['cidade'] or f.get('cidade')) or 'Balneário Camboriú',
        lat=lat, lon=lon, local_exato=lat is not None,
        marcado_mobiliado=norm(mob).strip() == 'mobiliado',
        publicado=data_iso(L.get('datePosted')), anunciante=site.imob,
        fotos=f.get('fotos') or ([c['foto']] if c['foto'] else []))


# ---------- tudo junto

def buscar(chrome, progresso):
    inicio = time.time()
    porta = Porta(INTERVALO)
    sites = [Site(s, d, n, porta) for s, d, n in SITES]
    erros, fila, vistos = [], [], set()
    trava = threading.Lock()

    def listar(site):
        try:
            site.abrir()
            achados = []
            for fin in ('rent', 'season'):
                cards, total = ler_grade(site, fin, progresso)
                achados += [(c, fin) for c in cards if passa_cartao(c, fin)]
            with trava:
                for c, fin in achados:  # 'rent' vem antes: imóvel nas duas finalidades fica como aluguel
                    c['link'] = urllib.parse.urljoin(site.base + '/', c['link']) if c['link'] else ''
                    if c['id'] not in vistos:
                        vistos.add(c['id'])
                        fila.append((site, c, fin))
                progresso(f'Apresenta.me: {site.imob}: {len(achados)} apartamentos de 1+ quartos em BC')
        except Exception as ex:
            with trava:
                erros.append(f'{site.imob}: {str(ex)[:60]}')

    progresso('Apresenta.me: lendo a busca de 12 imobiliárias')
    ths = [threading.Thread(target=listar, args=(s,), daemon=True) for s in sites]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=max(5, PRAZO - (time.time() - inicio)))
    if erros or any(t.is_alive() for t in ths):
        # todas saem com o mesmo nome de fonte: devolver só parte faria o app achar que os anúncios das que
        # falharam saíram do ar. Melhor falhar inteiro e manter a lista anterior.
        raise RuntimeError('Apresenta.me: busca falhou em ' + ('; '.join(erros) or 'alguns sites (tempo esgotado)'))

    res, prox = [], [0]
    ordem = {s[0]: k for k, s in enumerate(SITES)}
    total = len(fila)
    progresso(f'Apresenta.me: {total} apartamentos; lendo as fichas')

    def trabalhar():
        while True:
            with trava:
                if prox[0] >= total:
                    return
                k = prox[0]
                prox[0] += 1
            site, c, fin = fila[k]
            try:
                f = None
                if time.time() - inicio < PRAZO:
                    try:
                        f = ler_ficha(site.pedir(c['link'] or f"{site.base}/imovel/{c['id']}", tentativas=3))
                    except Exception:
                        f = None
                o = montar(site, c, fin, f)
                if f and f.get('cidade') and norm(f['cidade']).strip() != 'balneario camboriu':
                    continue
                if o['quartos'] is not None and o['quartos'] < 1:
                    continue
                if o['aluguel'] is not None and not PRECO_MIN <= o['aluguel'] <= PRECO_MAX:
                    continue  # a ficha desmentiu a grade
                with trava:
                    res.append((ordem[site.slug], c['id'], o))
                    if len(res) % 20 == 0:
                        progresso(f'Apresenta.me: fichas {len(res)} de {total}')
            except Exception:
                pass

    ws = [threading.Thread(target=trabalhar, daemon=True) for _ in range(3)]
    for t in ws:
        t.start()
    for t in ws:
        t.join(timeout=max(5, PRAZO + 25 - (time.time() - inicio)))
    with trava:
        res = [o for _, _, o in sorted(res, key=lambda x: (x[0], x[1]))]
    # pino repetido em vários anúncios diferentes = coordenada padrão, não a do imóvel
    rep = Counter((o['lat'], o['lon']) for o in res if o['lat'] is not None)
    for o in res:
        if o['lat'] is not None and rep[(o['lat'], o['lon'])] >= 4:
            o['lat'] = o['lon'] = None
            o['local_exato'] = False
    progresso(f'Apresenta.me: {len(res)} apartamentos')
    return res
