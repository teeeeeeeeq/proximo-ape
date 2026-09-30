"""Imobiliárias de Balneário Camboriú com site da Castel Digital.

Todos os sites da Castel têm o mesmo motor, então um leitor só serve para os 15:
  lista:   /imoveis/filtragem/ordem/dataatualizacaorecente/ipp/96/pagina/N?finalidade=F&cidade=4436
           (finalidade 2 = aluguel anual, 3 = aluguel temporada; cidade 4436 = Balneário Camboriú)
  anúncio: /imovel/<slug>-aluguel-ref-<id>/  (HTML com preço, condomínio, IPTU, fotos, descrição e,
           às vezes, data-coords "lat,lon,zoom,raio" e o bloco "Endereço")
Tudo sai com urllib puro, sem Chrome.
"""
import html, re, threading, time, unicodedata, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

NOME = 'Imobiliárias (Castel Digital)'
USA_CHROME = False

FONTE = 'Imobiliárias (Castel Digital)'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'
CIDADE_BC = '4436'
MAX_ALUGUEL = 9000
PAUSA = 0.3          # entre pedidos ao mesmo site
SITES_PARALELOS = 5  # sites lidos ao mesmo tempo (cada site recebe um pedido por vez)

# (slug para o id, nome da imobiliária, endereço do site)
SITES = [
    ('atlantida', 'Atlântida Imóveis', 'https://www.atlantidaimoveis.com.br'),
    ('atlantidabrava', 'Atlântida Praia Brava', 'https://www.atlantidapraiabrava.com.br'),
    ('beatriz', 'Beatriz Lucchese Imóveis', 'https://www.beatrizluccheseimoveis.com.br'),
    ('danisaucedo', 'Dani Saucedo Imóveis', 'https://www.danisaucedoimoveis.com.br'),
    ('dilma', 'Dilma Imóveis', 'https://www.dilmaimoveis.com.br'),
    ('eleven8', 'Eleven8', 'https://www.eleven8.com.br'),
    ('fly', 'Fly Imóveis', 'https://www.flyimoveis.com.br'),
    ('renascenca', 'Imobiliária Renascença', 'https://www.imobiliariarenascenca.com.br'),
    ('mef', 'M & F Imóveis', 'https://www.mefimoveis.com'),
    ('newimoveis', 'New Imóveis', 'https://www.newimoveis.net'),
    ('oceanica', 'Oceânica Imóveis', 'https://www.oceanicaimoveis.com.br'),
    ('padilha', 'Padilha (Camboriú Apartamentos)', 'https://www.camboriuapartamentos.com.br'),
    ('pensky', 'Pensky Imóveis', 'https://www.penskyimoveis.com.br'),
    ('varela', 'Varela Imobiliária', 'https://www.varelaimob.com'),
    ('velar', 'Velar Imóveis', 'https://www.velarimobiliaria.com.br'),
]

TIPOS_APTO = ('apartamento', 'cobertura', 'studio', 'kitnet', 'loft', 'flat', 'garden', 'duplex', 'triplex', 'penthouse')
TIPOS_NAO = ('casa', 'sala', 'sobrado', 'terreno', 'galpao', 'loja', 'ponto', 'predio', 'chacara', 'sitio', 'box', 'garagem')


# ---------- texto e números

def norm(s):
    return unicodedata.normalize('NFKD', str(s or '').lower()).encode('ascii', 'ignore').decode()


def texto(s):
    """HTML -> texto limpo, preservando quebras de parágrafo e itens de lista."""
    s = re.sub(r'<span class="cent[^"]*">(\d+)</span>', r'\1', str(s or ''))
    s = re.sub(r'<li[^>]*>', '\n• ', s)
    s = re.sub(r'<br\s*/?>|</p>|</li>|</h\d>|</div>|</ul>', '\n', s)
    s = html.unescape(re.sub(r'<[^>]+>', ' ', s)).replace('\r', '').replace('\xa0', ' ').replace(' ', ' ')
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r' *\n *', '\n', s)
    s = re.sub(r'\n•\s*(?=\n)', '', s)          # itens de lista vazios
    return re.sub(r'\n{3,}', '\n\n', s).strip()


def linha(s):
    return re.sub(r'\s+', ' ', texto(s)).strip()


def reais(s):
    """'1.234,56' / '1.500.00' / '1500' / '5.500' -> float"""
    s = re.sub(r'[^\d.,]', '', str(s or '')).strip('.,')
    if not s:
        return None
    if ',' in s:
        inteiro, dec = s.rsplit(',', 1)
        inteiro = inteiro.replace('.', '').replace(',', '')
    else:
        partes = s.split('.')
        if len(partes) > 1 and len(partes[-1]) == 2:
            inteiro, dec = ''.join(partes[:-1]), partes[-1]
        else:
            inteiro, dec = ''.join(partes), '0'
    try:
        return float(f'{int(inteiro or 0)}.{dec or 0}')
    except ValueError:
        return None


def bloco_div(t, i):
    """o <div> que começa na posição i, com os <div> internos balanceados"""
    depth = 0
    for m in re.finditer(r'<(/?)div\b[^>]*>', t[i:]):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return t[i:i + m.end()]
    return t[i:i + 30000]


# ---------- rede

class Site:
    def __init__(self, base):
        self.base = base
        self.ultimo = 0.0

    def get(self, path):
        espera = PAUSA - (time.time() - self.ultimo)
        if espera > 0:
            time.sleep(espera)
        url = path if path.startswith('http') else self.base + path
        erro = None
        for tentativa in range(2):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'pt-BR,pt;q=0.9'})
                with urllib.request.urlopen(req, timeout=25) as r:
                    dados = r.read()
                    cs = (r.headers.get_content_charset() or 'iso-8859-1').lower()
                self.ultimo = time.time()
                # sites declaram ISO-8859-1 mas usam cp1252 (aspas, travessão, marcador •)
                return dados.decode('cp1252' if cs in ('iso-8859-1', 'latin-1', 'latin1') else cs, 'replace')
            except Exception as ex:
                erro = ex
                self.ultimo = time.time()
                time.sleep(1.5)
        raise erro


# ---------- lista

def ler_cards(t):
    out = []
    for ch in t.split('<li class="col-im-grid')[1:]:
        ch = ch.split('</li>')[0]
        m = re.search(r'id="im-\d+-ref-(\d+)"', ch)
        h = re.search(r'href="(/imovel/[^"]+)"', ch)
        if not (m and h):
            continue
        cid = re.search(r'<p class="cidade[^"]*">(.*?)</p>', ch, re.S)
        cid = linha(cid.group(1)) if cid else ''
        bai = re.search(r'<span class="bairro">(.*?)</span>', ch, re.S)
        tipo = re.search(r'<div class="tipo">(.*?)</div>', ch, re.S)
        dorms = re.search(r'class="dorms[^"]*"[^>]*title="(\d+)', ch)
        preco = re.search(r'<div class="preco[^"]*">(.*?)</div>', ch, re.S)
        tarjas = [linha(x) for x in re.findall(r'<div class="tarja[^"]*">(.*?)</div>', ch, re.S)]
        out.append(dict(id=m.group(1), href=h.group(1), cidade=cid.split(' - ')[0].strip(),
                        bairro=linha(bai.group(1)) if bai else '', tipo=linha(tipo.group(1)) if tipo else '',
                        quartos=int(dorms.group(1)) if dorms else None, preco=linha(preco.group(1)) if preco else '',
                        tarjas=tarjas))
    return out


def e_apartamento(c):
    slug = c['href'].split('/')[2]
    t = norm(c['tipo']) or slug
    if t.startswith(TIPOS_NAO) or slug.startswith(TIPOS_NAO):
        return False
    return t.startswith(TIPOS_APTO) or slug.startswith(TIPOS_APTO)


def listar(site, fin):
    """todos os cards de uma finalidade em BC; devolve (cards, total anunciado pelo site)"""
    cards, vistos, total = [], set(), None
    for pag in range(1, 21):
        q = urllib.parse.urlencode({'finalidade': fin, 'cidade': CIDADE_BC})
        t = site.get(f'/imoveis/filtragem/ordem/dataatualizacaorecente/ipp/96/pagina/{pag if pag > 1 else ""}?{q}')
        if pag == 1:
            if 'col-im-grid' not in t and 'total-results' not in t and 'form_filtragem' not in t:
                raise RuntimeError('página de busca irreconhecível')
            # só conta se o site realmente filtrou por BC nessa finalidade
            m = re.search(r'class="num">([\d.]+)</span>', t)
            total = int(m.group(1).replace('.', '')) if m else 0
            if not re.search(r'<option value="%s"[^>]*>' % CIDADE_BC, t):
                return [], 0
        novos = [c for c in ler_cards(t) if c['id'] not in vistos]
        for c in novos:
            vistos.add(c['id'])
            cards.append(c)
        if not novos or len(cards) >= (total or 0):
            break
    return cards, total


# ---------- anúncio

def precos(main):
    """[(rótulo, valor, período)] de cada bloco de preço do anúncio"""
    out = []
    for b in re.findall(r'<div class="precoaluguel[^"]*">(.*?)</div>', main, re.S):
        lbl = re.search(r'class="lbl">(.*?)</span>', b, re.S)
        val = re.search(r'class="aluguel_valor">(.*?)</span>\s*(?:<span class="aluguel_periodo|$)', b, re.S)
        per = re.search(r'class="aluguel_periodo">(.*?)</span>', b, re.S)
        v = linha(val.group(1)) if val else linha(b)
        out.append((norm(linha(lbl.group(1))) if lbl else '', reais(v) if 'R$' in v else None, norm(linha(per.group(1))) if per else ''))
    return out


def taxa(txt, nome, padrao_anual):
    """Condomínio/IPTU do bloco estruturado: 'IPTU: R$ 3.242,37 (valor anual)' -> valor mensal"""
    m = re.search(nome + r'\s*:\s*R\$\s*([\d.,]+)\s*(?:\(\s*(?:valor\s*)?(mensal|anual)\s*\))?', txt, re.I)
    if not m:
        return None
    v = reais(m.group(1))
    if not v:
        return None
    anual = (m.group(2) or '').lower() == 'anual' or (not m.group(2) and padrao_anual)
    return round(v / 12, 2) if anual else v


ANUAL_TXT = r'(?:anual|ao ano|por ano|/\s*ano|\(ano\))'
MENSAL_TXT = r'(?:mensal|ao m[eê]s|por m[eê]s|/\s*m[eê]s)'


def taxa_no_texto(desc, qual):
    """Plano B: condomínio / IPTU escritos na descrição ('Condomínio R$ 430,00', 'R$ 60,00 Lixo/IPTU')."""
    if qual == 'cond':
        nome = r'condom[ií]n?io(?:\s*/\s*[áa]gua)?'
    else:
        nome = r'(?:lixo\s*/\s*)?iptu(?:\s*/\s*lixo)?'
    pos = re.search(nome + r'\s*(?:mensal\s*|anual\s*|m[ée]dio\s*)?[:=\-–]?\s*(?:de\s*)?(?:aprox\.?\s*)?R\$\s*([\d.,]*\d)\s*(\S+(?:\s+\S+)?)?', desc, re.I)
    if not pos:
        pos = re.search(r'R\$\s*([\d.,]*\d)\s*(?:de\s*)?[-–]?\s*' + nome + r'\b\s*(\S+(?:\s+\S+)?)?', desc, re.I)
    if not pos:
        return None
    v = reais(pos.group(1))
    if not v:
        return None
    depois = pos.group(2) or ''
    antes = desc[max(0, pos.start() - 1):pos.end()]
    if qual == 'iptu':
        if re.search(ANUAL_TXT, depois + ' ' + antes, re.I):
            return round(v / 12, 2)
        if re.search(MENSAL_TXT, depois + ' ' + antes, re.I):
            return v
        return round(v / 12, 2) if v > 600 else v   # sem período: acima de R$ 600 só pode ser o valor do ano
    return v


RUA_RX = r'(?:Rua|R\.|Avenida|Av\.?|Alameda|Al\.|Travessa|Tv\.)\s+[A-Za-zÀ-ÿ0-9][^,;\n()|/]{0,40}?'


def limpar_rua(s):
    s = re.sub(r'\s+', ' ', s).strip(' .-–,')
    s = re.split(r',|\s+n[º°o]\.?\s*\d|\s+nº|\s+-\s+|\s+\d+\s*$', s)[0].strip(' .-–,')
    s = re.sub(r'\s+\d{1,5}$', '', s) if not re.match(r'(?i)^(rua|r\.|avenida|av\.?)\s+\d{3,4}$', s) else s
    s = re.sub(r'^(?i:r\.)\s*', 'Rua ', s)
    s = re.sub(r'^(?i:av\.?)\s+', 'Avenida ', s)
    s = re.sub(r'^(?i:al\.)\s*', 'Alameda ', s)
    s = re.sub(r'^(?i:tv\.)\s*', 'Travessa ', s)
    s = re.sub(r'^(?i:rua)\b', 'Rua', s)
    s = re.sub(r'^(?i:avenida)\b', 'Avenida', s)
    return s if len(s) > 5 else ''


def rua_no_texto(*txts):
    """Rua do imóvel quando o anúncio a escreve: 'Rua 3604, n° 165', 'rua Maria Mansoto, nº 315', 'RUA 1131'.
    Ruas só citadas como referência ('perto da Av. Brasil') ficam de fora."""
    for t in txts:
        for m in re.finditer(r'(?i)\b(?:Rua|R\.|Avenida|Av\.?)\s+(\d{3,4})\b', t):  # ruas numeradas de BC
            antes = norm(t[max(0, m.start() - 30):m.start()])
            if not re.search(r'proxim|perto|esquina|quadra|metros|\bm\b|passos|minutos|acesso|entre|frente|\bate\b|\bda\b|\bdo\b', antes):
                return 'Rua ' + m.group(1)
        for m in re.finditer(r'(?i)\b(' + RUA_RX + r')(?:,\s*|\s+)(?:n[º°o]\.?\s*)?\d{1,5}\b', t):
            antes = norm(t[max(0, m.start() - 30):m.start()])
            if not re.search(r'proxim|perto|esquina|quadra|metros|passos|minutos|acesso|entre|\bate\b', antes):
                r = limpar_rua(m.group(1))
                if r:
                    return r
        m = re.search(r'(?i)localizad[oa]\s+na\s+(' + RUA_RX + r')(?=[,.;\n]|\s+(?:em|no|na|,)\b)', t)
        if m:
            r = limpar_rua(m.group(1))
            if r:
                return r
    return ''


def descricao(t):
    m = re.search(r'<div[^>]*class="[^"]*\bdesc\b[^"]*"', t)
    if not m:
        return ''
    b = bloco_div(t, m.start())
    # o endereço estruturado vira uma linha; o "ver mapa" some
    b = re.sub(r'<a href="#gmap-imovel">.*?</a>', '', b)
    # listas de características: "Características do Imóvel: a, b, c"
    i = b.find('<div id="caracs_demais"')
    caracs = ''
    if i >= 0:
        cd = bloco_div(b, i)
        partes = []
        for col in re.findall(r'<div class="colunavel">(.*?)</ul>', cd, re.S):
            tit = re.search(r'<h4>(.*?)</h4>', col, re.S)
            itens = [linha(x) for x in re.findall(r'<li[^>]*>(.*?)</li>', col, re.S)]
            tit = linha(tit.group(1)) if tit else 'Características'
            if 'empreend' in norm(tit):
                # "Hall Decorado e Mobiliado" é do prédio, não do apartamento
                itens = [x for x in itens if 'mobiliad' not in norm(x)]
            if itens:
                partes.append(f'{tit}: ' + ', '.join(itens) + '.')
        caracs = '\n\n'.join(partes)
        b = b.replace(cd, '')
    d = texto(b)
    return (d + ('\n\n' + caracs if caracs else '')).strip()


def ler_anuncio(t):
    i = t.find('id="imov_main"')
    j = t.find('mais_whats', i)
    main = t[i:j if j > i else i + 12000] if i >= 0 else ''
    txt_main = linha(main)
    d = {}
    dm = re.search(r'class="detalhe dorms"[^>]*>(.*?)</p>', main, re.S)
    if dm:
        s = linha(dm.group(1))
        q = re.match(r'(\d+)', s)
        d['quartos'] = int(q.group(1)) if q else None
        su = re.search(r'(\d+)\s*su[ií]te', s)
        d['suites'] = int(su.group(1)) if su else 0
    ar = re.search(r'class="area-util"[^>]*>(.*?)</p>', main, re.S)
    if ar:
        d['area'] = reais(linha(ar.group(1)).split('m')[0])
    ct = re.search(r'class="detalhe caracs-txt"[^>]*>(.*?)</p>', main, re.S)
    d['caracs'] = [norm(x).strip() for x in linha(ct.group(1)).split(' - ')] if ct else []
    d['tarjas'] = [linha(x) for x in re.findall(r'<div class="tarja[^"]*">(.*?)</div>', main, re.S)]
    d['precos'] = precos(main)
    d['cond'] = taxa(txt_main, r'Condom[ií]nio', False)
    d['iptu'] = taxa(txt_main, r'IPTU', True)
    h1 = re.search(r'<h1>(.*?)</h1>', t, re.S)
    if h1:
        tp = re.search(r'<a [^>]*>(.*?)</a>', h1.group(1), re.S)
        sm = re.search(r'<small>(.*?)</small>', h1.group(1), re.S)
        d['tipo_h1'] = linha(tp.group(1)) if tp else ''
        d['titulo_h1'] = linha(sm.group(1)) if sm else ''
    co = re.search(r'id="gmap-imovel"[^>]*data-coords="([^"]+)"', t) or re.search(r'data-coords="([^"]+)"[^>]*id="gmap-imovel"', t)
    if co:
        p = co.group(1).split(',')
        try:
            lat, lon = float(p[0]), float(p[1])
            raio = int(float(p[3])) if len(p) > 3 and p[3].strip() else 250
            # só aceita se cair na região de BC (evita coordenada zerada ou de outra cidade)
            if -27.10 < lat < -26.90 and -48.70 < lon < -48.55:
                d['lat'], d['lon'], d['exato'] = lat, lon, 0 < raio <= 10
        except (ValueError, IndexError):
            pass
    en = re.search(r'<div class="detalhe-endereco">(.*?)</div>', t, re.S)
    if en:
        primeira = re.split(r'<br\s*/?>', re.sub(r'<h4>.*?</h4>', '', en.group(1), flags=re.S))[0]
        d['rua'] = limpar_rua(linha(primeira))
    pub = re.search(r'article:published_time" content="(\d{4}-\d\d-\d\d)', t)
    d['publicado'] = pub.group(1) if pub else ''
    fotos = []
    for u in re.findall(r'data-rsBigImg="([^"]+)"', t):
        u = html.unescape(u)
        u = 'https:' + u if u.startswith('//') else u
        if u.startswith('http') and u not in fotos:
            fotos.append(u)
    if not fotos:
        og = re.search(r'property="og:image" content="([^"]+)"', t)
        if og and '/imoveis/' in og.group(1):
            fotos.append(og.group(1))
    d['fotos'] = fotos
    d['desc'] = descricao(t)
    return d


# ---------- montagem

def montar(slug, nome, base, c, d, fins):
    """c = card da lista, d = página do anúncio, fins = finalidades em que apareceu (2 anual, 3 temporada)"""
    mensal = next((v for l, v, p in d['precos'] if v and 'mensal' in p), None)
    diaria = next((v for l, v, p in d['precos'] if v and 'diaria' in p), None)
    if mensal is None and not d['precos'] and '2' in fins and 'diaria' not in norm(c['preco']) and 'R$' in c['preco']:
        mensal = reais(c['preco'])   # anúncio sem bloco de preço: vale o do card
    if mensal is not None and mensal > MAX_ALUGUEL:
        return None
    quartos = d.get('quartos', c['quartos'])
    if not quartos or quartos < 2:
        return None
    desc = d['desc']
    extras = [x for x in c['tarjas'] + d['tarjas'] if x and norm(x) not in norm(desc)]
    so_temporada = '2' not in fins or (mensal is None and diaria is not None)
    if so_temporada and diaria:
        extras.insert(0, 'Locação temporada: R$ %s por diária (sem valor mensal no anúncio)' % f'{diaria:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.'))
    elif so_temporada:
        extras.insert(0, 'Locação temporada')
    if extras:
        desc = ' · '.join(dict.fromkeys(extras)) + ('\n\n' + desc if desc else '')
    cond = d['cond'] if d['cond'] is not None else taxa_no_texto(d['desc'], 'cond')
    iptu = d['iptu'] if d['iptu'] is not None else taxa_no_texto(d['desc'], 'iptu')
    tipo = d.get('tipo_h1') or c['tipo'] or 'Apartamento'
    tit = d.get('titulo_h1') or ''
    if not tit:
        titulo = tipo
    elif norm(tit).startswith(norm(tipo).split(' ')[0]):
        titulo = tit
    else:
        titulo = f'{tipo} - {tit}'
    tudo = ' '.join(c['tarjas'] + d['tarjas'])
    return dict(
        id=f'CD{slug}-{c["id"]}', fonte=FONTE, url=base + c['href'], titulo=titulo, desc=desc,
        ativo=not re.search(r'locad|alugad|vendid|reservad|indisponi', norm(tudo)),
        aluguel=mensal, cond=cond, iptu=round(iptu, 2) if iptu else None,
        quartos=int(quartos), suites=d.get('suites'), area=d.get('area'),
        bairro=c['bairro'], rua=d.get('rua') or rua_no_texto(d.get('titulo_h1') or '', ' '.join(c['tarjas'] + d['tarjas']), d['desc']),
        cidade=c['cidade'] or 'Balneário Camboriú',
        lat=d.get('lat'), lon=d.get('lon'), local_exato=bool(d.get('exato')),
        marcado_mobiliado='mobiliado' in d['caracs'], publicado=d['publicado'], anunciante=nome, fotos=d['fotos'])


def ler_site(slug, nome, base, progresso):
    site = Site(base)
    por_id, fins, total_bc = {}, {}, 0
    for fin in ('2', '3'):
        cards, total = listar(site, fin)
        total_bc += total or 0
        for c in cards:
            fins.setdefault(c['id'], set()).add(fin)
            if fin == '2' or c['id'] not in por_id:
                por_id[c['id']] = c
    # filtro básico pelo card: BC, apartamento, 2+ quartos, aluguel mensal até 9.000
    alvo = []
    for k, c in por_id.items():
        if 'balneario camboriu' not in norm(c['cidade']) or not e_apartamento(c):
            continue
        if c['quartos'] is not None and c['quartos'] < 2:
            continue
        p = norm(c['preco'])
        if '2' in fins[k] and 'diaria' not in p and (reais(c['preco']) or 0) > MAX_ALUGUEL:
            continue
        alvo.append(c)
    out = []
    for c in alvo:
        try:
            d = ler_anuncio(site.get(c['href']))
            o = montar(slug, nome, base, c, d, fins[c['id']])
            if o:
                out.append(o)
        except Exception:
            continue    # um anúncio com problema não derruba o resto
    progresso(f'{nome}: {len(out)} apartamentos ({total_bc} imóveis para alugar em BC no site)')
    return out


def buscar(chrome, progresso):
    progresso(f'Castel Digital: lendo {len(SITES)} imobiliárias')
    lock = threading.Lock()
    todos, ok, falhas = {}, [], []

    def um(s):
        slug, nome, base = s
        try:
            r = ler_site(slug, nome, base, progresso)
            with lock:
                ok.append(nome)
                for o in r:
                    todos[o['id']] = o
        except Exception as ex:
            with lock:
                falhas.append(f'{nome} ({str(ex)[:60]})')
            progresso(f'{nome}: falhou ({str(ex)[:60]})')

    with ThreadPoolExecutor(SITES_PARALELOS) as ex:
        list(ex.map(um, SITES))
    progresso(f'Castel Digital: {len(ok)} de {len(SITES)} sites responderam, {len(todos)} apartamentos'
              + (f'; falharam: {", ".join(falhas)}' if falhas else ''))
    if not ok:
        raise RuntimeError('nenhum site da Castel Digital respondeu')
    return list(todos.values())
