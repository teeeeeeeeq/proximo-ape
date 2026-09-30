"""Viva Balneário (vivabalneario.com.br): portal de Balneário Camboriú com anúncios de imobiliárias e particulares.

  lista:   /aluguel/apartamentos/N/   36 por página; anual, estudante e temporada juntos; o portal é só de BC
  anúncio: /imoveis/<id>/             preço, condomínio, IPTU, área, quartos, endereço, mapa, fotos, anunciante
  data:    POST /print.php ref=imo&t=<id>   versão de impressão, a única com "última atualização em dd/mm/aaaa"
Tudo sai com urllib puro, sem Chrome.

O portal está quase parado: a maior parte dos anúncios não é atualizada há anos (preço de 2014, temporada de 2018).
Anúncio sem atualização há mais de 2 anos fica de fora; a versão de impressão (com a data) é lida primeiro e a
página completa só é aberta quando o anúncio está em dia. 'publicado' é a data da última atualização.

Armadilhas dos dados do portal:
  quartos   o campo não conta as suítes ('1 suíte + 1 dormitório' = 1, '2 suítes' = 0): vale o maior entre campo e texto
  preço     sem categoria anual/estudante, 'R$ 700,00' sem '/dia' pode ser diária de temporada: sem 'mês/mensal/anual'
            no texto, e com valor baixo ou texto de diária/pacote, não vira aluguel mensal
  cidade    o portal diz sempre 'Balneário Camboriú'; a cidade real vem do endereço do mapa (Camboriú e Itajaí ficam de fora)
"""
import html, re, threading, time, unicodedata, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

NOME = 'Viva Balneário'
USA_CHROME = False

FONTE = 'Viva Balneário'
BASE = 'https://www.vivabalneario.com.br'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'
MAX_ALUGUEL = 9000
DIAS_PARADO = 730     # sem atualização há mais que isso: anúncio abandonado
PAUSA = 0.25          # entre pedidos de cada conexão
PARALELO = 3          # conexões ao mesmo tempo
LIMITE_SEG = 240      # para de ler anúncios depois disso (o total precisa caber em ~5 min)

TIPOS_APTO = ('apartamento', 'cobertura', 'studio', 'loft', 'flat', 'garden', 'duplex', 'triplex', 'penthouse')
ANUAL_TXT = r'(?:anual|ao ano|por ano|/\s*ano|\(ano\))'
MENSAL_TXT = r'(?:mensal|ao m[eê]s|por m[eê]s|/\s*m[eê]s)'


# ---------- texto e números

def norm(s):
    return unicodedata.normalize('NFKD', str(s or '').lower()).encode('ascii', 'ignore').decode()


def texto(s):
    """HTML -> texto limpo, com as quebras de linha do anúncio."""
    s = re.sub(r'\s*\n\s*', ' ', str(s or ''))              # quebra de linha crua no HTML é só espaço
    s = re.sub(r'<br\s*/?>|</p>|</li>|</div>', '\n', s)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = html.unescape(html.unescape(s))          # há descrições com entidades escapadas duas vezes (&amp;#769;)
    s = unicodedata.normalize('NFC', s).replace('\r', '').replace('\xa0', ' ')
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r' *\n *', '\n', s)
    return re.sub(r'\n{3,}', '\n\n', s).strip()


def linha(s):
    return re.sub(r'\s+', ' ', texto(s)).strip()


def reais(s):
    """'R$ 8.500,00' / '2.999,99' / '1500' -> float"""
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


def brl(v):
    return 'R$ ' + f'{v:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def inteiro(s):
    m = re.search(r'\d+', str(s or ''))
    return int(m.group()) if m else None


def taxa_no_texto(desc, qual):
    """Condomínio / IPTU escritos só na descrição ('Condomínio R$ 430,00', 'IPTU anual R$ 1.200')."""
    nome = r'condom[ií]n?io' if qual == 'cond' else r'iptu'
    m = re.search(nome + r'\s*(?:mensal\s*|anual\s*|m[ée]dio\s*)?[:=\-–]?\s*(?:de\s*)?(?:aprox\.?\s*)?R\$\s*([\d.,]*\d)\s*(\S+(?:\s+\S+)?)?', desc, re.I)
    if not m:
        return None
    v = reais(m.group(1))
    if not v:
        return None
    if qual == 'iptu':
        perto = desc[max(0, m.start() - 1):m.end()] + ' ' + (m.group(2) or '')
        if re.search(ANUAL_TXT, perto, re.I):
            return round(v / 12, 2)
        if re.search(MENSAL_TXT, perto, re.I):
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
    """Rua do imóvel quando o anúncio a escreve ('ED. DIMORA DEL SOLE / RUA 1911', 'Rua Bruno Silva, nº 135').
    Ruas só citadas como referência ('perto da Av. Brasil') ficam de fora."""
    for t in txts:
        for m in re.finditer(r'(?i)\b(?:Rua|R\.|Avenida|Av\.?)\s+(\d{3,4})\b', t):   # ruas numeradas de BC
            antes = norm(t[max(0, m.start() - 30):m.start()])
            if not re.search(r'proxim|perto|esquina|quadra|metros|\bm\b|passos|minutos|acesso|entre|frente|\bate\b|\bda\b|\bdo\b', antes):
                return 'Rua ' + m.group(1)
        for m in re.finditer(r'(?i)\b(' + RUA_RX + r')(?:,\s*|\s+)(?:n[º°o]\.?\s*)?\d{1,5}\b', t):
            antes = norm(t[max(0, m.start() - 30):m.start()])
            if not re.search(r'proxim|perto|esquina|quadra|metros|passos|minutos|acesso|entre|\bate\b', antes):
                r = limpar_rua(m.group(1))
                if r:
                    return r
    return ''


def suites_no_texto(*txts):
    nums = {'uma': 1, 'um': 1, 'duas': 2, 'dois': 2, 'tres': 3, 'quatro': 4}
    for t in txts:
        m = re.search(r'\b(\d{1,2}|uma|um|duas|dois|tres|quatro)\s+su[ií]tes?\b', norm(t))
        if m:
            return int(m.group(1)) if m.group(1).isdigit() else nums[m.group(1)]
    return None


NUM_Q = r'(\d{1,2}|uma?|dois|duas|tres|quatro|cinco)'
DORM_Q = r'(?:dormitorios?|quartos?|dorms?\b)'
SUITE_Q = r'(?:(?:demi|semi)[- ]?)?suites?\b'
NUMS_Q = dict(um=1, uma=1, dois=2, duas=2, tres=3, quatro=4, cinco=5)


def quartos_no_texto(*txts):
    """Quartos contados no texto, com as suítes: '1 suíte + 1 dormitório' = 2, '2 suítes' = 2,
    '3 dormitórios sendo 1 suíte' = 3. O campo do portal às vezes não conta as suítes, ou fica em 0."""
    n = lambda s: int(s) if s.isdigit() else NUMS_Q[s]
    melhor = None
    for t in txts:
        t = norm(t)
        achados = []
        for m in re.finditer(r'\b' + NUM_Q + r'\s*\(?\s*(?:' + DORM_Q + '|' + SUITE_Q + ')', t):
            if re.search(r'(\d|\bum|\buma|dois|duas|tres)\s*(e|ou|a|/|,)\s*$', t[max(0, m.start() - 8):m.start()]):
                continue                                  # 'de 1 e 2 quartos', '2 ou 3 dormitórios': opções, não o imóvel
            achados.append(n(m.group(1)))
        # suítes seguidas de mais quartos: '1 suíte + 1 dormitório', '1 suíte casal, 1 quarto', '1 suíte e 2 semi-suítes'
        for m in re.finditer(r'\b' + NUM_Q + r'\s*' + SUITE_Q + r'[^\d\n.;+]{0,25}?\s*(?:\+|\be\b|\bmais\b|,)\s*(?:\+\s*)?'
                             + NUM_Q + r'\s*(?:' + DORM_Q + '|' + SUITE_Q + ')', t):
            achados.append(n(m.group(1)) + n(m.group(2)))
        # quartos e depois suíte só somam com '+' ou 'mais' ('2 dormitórios + 1 suíte'); '2 quartos e 1 suíte' é ambíguo
        for m in re.finditer(r'\b' + NUM_Q + r'\s*' + DORM_Q + r'\s*(?:\+|\bmais\b)\s*' + NUM_Q + r'\s*' + SUITE_Q, t):
            achados.append(n(m.group(1)) + n(m.group(2)))
        for m in re.finditer(r'\b(?:dormitorios|quartos|suites)\s*:\s*(\d)\b', t):
            achados.append(int(m.group(1)))
        achados = [a for a in achados if 0 < a <= 6]
        if achados:
            melhor = max(melhor or 0, max(achados))
    return melhor


DIARIA_TXT = r'di[aá]rias?\b|/\s*dia\b|por dia\b|por noite|pernoite|pacotes?\b|r[eé]veill?on|carnaval|temporada'
MES_TXT = (r'mensa(?:l|is)\b|ao m[eê]s|por m[eê]s|/\s*m[eê]s|\banua(?:l|is)\b|por ano\b|\b\d{1,2}\s*meses|longo prazo|'
           r'estudante|universit[aá]ri|\bat[eé]\s+(?:o m[eê]s de\s+)?(?:junho|julho|agosto|setembro|outubro|novembro|dezembro)\b')


def sem_taxas(s):
    """tira 'condomínio mensal R$ 500', 'IPTU anual R$ 1.200': o período dessas taxas não diz nada do aluguel"""
    return re.sub(r'(?i)(condom\w*|iptu|taxas?|seguro)[^\n.;]{0,40}', ' ', s)


def preco_temporada(acao, titulo, desc, valor):
    """Sem categoria anual/estudante, preço sem '/dia' pode ser diária ou pacote ('Diárias: Janeiro R$ 800 / Pacote carnaval').
    Devolve None quando é mensal, ou o aviso que abre a descrição quando não dá para dizer que é mensal."""
    if 'anual' in acao or 'estudante' in acao or not valor:
        return None
    txt = sem_taxas(titulo + '\n' + desc)
    if re.search(MES_TXT, txt, re.I):
        return None
    if valor < 1500:
        return f'Temporada: preço do anúncio {brl(valor)}, provavelmente por diária (baixo demais para aluguel mensal)'
    if 'temporada' in acao or re.search(DIARIA_TXT, txt, re.I):
        return f'Temporada: preço do anúncio {brl(valor)}, sem dizer se é diária, pacote ou mês'
    return None


def anual_no_texto(titulo, desc):
    """'Alugo por até dezembro por $ 2750 ou anual $4.300 + condomínio $ 550' -> 4300.0"""
    m = re.search(r'(?i)\banual\b[^\d\n$]{0,25}?(?:R\s*)?\$\s*([\d.,]*\d)', sem_taxas(titulo + '\n' + desc))
    v = reais(m.group(1)) if m else None
    return v if v and 1000 <= v <= 30000 else None


# ---------- rede

class Portal:
    def __init__(self):
        self.local = threading.local()

    def get(self, path, dados=None):
        """GET (ou POST com dados); cada conexão espera PAUSA entre pedidos."""
        ultimo = getattr(self.local, 'ultimo', 0.0)
        espera = PAUSA - (time.time() - ultimo)
        if espera > 0:
            time.sleep(espera)
        corpo = urllib.parse.urlencode(dados).encode() if dados else None
        erro = None
        for tentativa in range(2):
            try:
                req = urllib.request.Request(BASE + path, data=corpo, headers={'User-Agent': UA, 'Accept-Language': 'pt-BR,pt;q=0.9'})
                with urllib.request.urlopen(req, timeout=25) as r:
                    b = r.read()
                self.local.ultimo = time.time()
                return b.decode('cp1252', 'replace')      # declara ISO-8859-1, usa cp1252
            except Exception as ex:
                erro = ex
                self.local.ultimo = time.time()
                if getattr(ex, 'code', None) == 404:
                    break
                time.sleep(1.5)
        raise erro


# ---------- lista

def ler_cards(t):
    out = []
    for b in t.split('<li class="list-item">')[1:]:
        m = re.search(r'href="/imoveis/(\d+)/"', b)
        if not m:
            continue
        g = lambda c: (re.search(r'<li class="%s">(?:<span class="amount">)?([^<]*)' % c, b) or [None, ''])[1].strip()
        out.append(dict(id=m.group(1), preco=g('preco'), quartos=inteiro(g('quartos'))))
    return out


def listar(site, progresso):
    cards, p = {}, 1
    while p <= 60:
        t = site.get(f'/aluguel/apartamentos/{p}/')
        got = ler_cards(t)
        if not got:
            break
        for c in got:
            cards.setdefault(c['id'], c)
        if p % 3 == 0:
            progresso(f'lista: página {p}, {len(cards)} apartamentos para alugar')
        if f'/aluguel/apartamentos/{p + 1}/' not in t:
            break
        p += 1
    return list(cards.values())


# ---------- anúncio

def ler_print(pr):
    """versão de impressão: 'Balneário Camboriú - Pioneiros - 3 dormitórios (última atualização em 11/09/2026)'"""
    cab = re.search(r'color:#888888;">\s*(.*?)</span>', pr or '', re.S)
    cab = linha(cab.group(1)) if cab else ''
    up = re.search(r'atualiza\S+ em (\d\d)/(\d\d)/(\d{4})', cab)
    partes = [x.strip() for x in re.sub(r'\(.*?\)', '', cab).split(' - ')]
    return dict(atualizado=f'{up.group(3)}-{up.group(2)}-{up.group(1)}' if up else '',
                cidade_print=partes[0] if len(partes) >= 3 else '', bairro_print=partes[1] if len(partes) >= 3 else '')


def parado(data):
    try:
        return time.time() - time.mktime(time.strptime(data, '%Y-%m-%d')) > DIAS_PARADO * 86400
    except ValueError:
        return False


def ler_anuncio(t, p):
    """t = página /imoveis/<id>/, p = ler_print() da versão de impressão"""
    d = dict(p)
    h1 = re.search(r'<h1[^>]*>(.*?)</h1>', t, re.S)
    d['titulo'] = linha(h1.group(1)) if h1 else ''
    migalhas = [linha(x) for x in re.findall(r'property="v:title">([^<]*)</a>', t)]
    d['acao'] = norm(' '.join(migalhas[1:]))                  # 'aluguel anual' / 'aluguel temporada' / 'aluguel'
    tipo = re.search(r'Tipo de im[^<]*<br\s*/?>\s*<span class="amount">([^<]*)', t)
    d['tipo'] = linha(tipo.group(1)) if tipo else ''
    val = re.search(r'<li class="valor">(.*?)</li>', t, re.S)
    val = val.group(1) if val else ''
    pre = re.search(r'Pre[^<]*<br\s*/?>\s*<span class="amount">([^<]*)', val)
    d['preco'] = linha(pre.group(1)) if pre else ''
    cond = re.search(r'Condom[^<]*<br\s*/?>\s*<span class="amount">([^<]*)', val)
    d['cond'] = reais(cond.group(1)) if cond else None
    iptu = re.search(r'IPTU[^<]*<br\s*/?>\s*<span class="amount">([^<]*)', val)
    d['iptu'] = reais(iptu.group(1)) if iptu else None
    util = re.search(r'[ÁA]rea [ÚU]til<br\s*/?>\s*<span class="amount">([\d.,]+)', t)
    total = re.search(r'[ÁA]rea Total<br\s*/?>\s*<span class="amount">([\d.,]+)', t)
    d['area'] = next((a for a in (reais(util.group(1)) if util else None, reais(total.group(1)) if total else None) if a), None)
    q = re.search(r'<li class="quartos">.*?<span class="amount">([^<]*)', t, re.S)
    d['quartos'] = inteiro(q.group(1)) if q else None
    i = t.find('<h2>Descri')
    desc = re.search(r'<p class="text-container descricao">(.*?)</p>', t[i:], re.S) if i >= 0 else None
    d['desc'] = texto(desc.group(1)) if desc else ''
    i = t.find('atributos-imovel')
    d['atributos'] = [linha(x) for x in re.findall(r'</i>\s*([^<]+)</li>', t[i:t.find('</div>', i)])] if i >= 0 else []
    ad = re.search(r'<p class="iaddress">(.*?)</p>', t, re.S)
    d['endereco'] = linha(ad.group(1)) if ad else ''
    ll = re.search(r'LatLng\((-?\d+\.\d+),\s*(-?\d+\.\d+)\)', t)
    d['latlon'] = (float(ll.group(1)), float(ll.group(2))) if ll else None
    j = t.find('class="pergunte-corretor"')
    anun = re.search(r'<h3>(.*?)</h3>', t[j:j + 3000], re.S) if j >= 0 else None
    d['anunciante'] = linha(anun.group(1)) if anun else ''
    fotos = []
    s = t.find('<ul class="slides">')
    for u in re.findall(r'<img src="([^"]+)"', t[s:t.find('</ul>', s)] if s >= 0 else ''):
        u = html.unescape(u).split('?')[0]
        u = 'https:' + u if u.startswith('//') else u
        if u.startswith('http') and u not in fotos:
            fotos.append(u)
    if not fotos:
        og = re.search(r'property="og:image" content="([^"]+)"', t)
        if og and '/imov/' in og.group(1):
            fotos.append(og.group(1).replace('http://', 'https://'))
    d['fotos'] = fotos
    return d


EXTENSO = dict(um=1, uma=1, dois=2, duas=2, tres=3, quatro=4, cinco=5, seis=6, sete=7, oito=8, nove=9, dez=10, onze=11, doze=12,
               treze=13, quatorze=14, catorze=14, quinze=15, dezesseis=16, dezessete=17, dezoito=18, dezenove=19, vinte=20,
               trinta=30, quarenta=40, cinquenta=50, sessenta=60, setenta=70, oitenta=80, noventa=90, cem=100, cento=100,
               duzentos=200, trezentos=300, quatrocentos=400, quinhentos=500, seiscentos=600, setecentos=700,
               oitocentos=800, novecentos=900)


def rua_em_numero(rua):
    """'Rua Mil Cento e Trinta e Um' -> 'Rua 1131' (o endereço do mapa às vezes escreve as ruas numeradas por extenso)"""
    m = re.match(r'(?i)^(rua|avenida)\s+(.+)$', rua)
    if not m:
        return rua
    total = atual = 0
    for w in norm(m.group(2)).split():
        if w == 'e':
            continue
        if w == 'mil':
            total, atual = total + (atual or 1) * 1000, 0
        elif w in EXTENSO:
            atual += EXTENSO[w]
        else:
            return rua
    n = total + atual
    return f'{m.group(1).capitalize()} {n}' if n >= 100 else rua


def endereco(s):
    """'Edifício X - Av. Atlântica, 2554 - Centro, Balneário Camboriú - SC, Brasil' -> (rua, número, bairro, cidade)"""
    m = re.match(r'^(.*) - ([^,]+?), ([^,]+?) - [^,]+(?:, [^,]+)?$', s)       # há endereços em italiano: 'Center, ... - Santa Catarina, Brasile'
    if not m:
        return endereco_curto(s)
    logr, bairro, cidade = m.groups()
    bairro = 'Centro' if norm(bairro).strip() == 'center' else bairro
    trechos = logr.split(' - ')
    rua = next((x for x in reversed(trechos) if re.match(r'(?i)(rua|r\.|av\.?|avenida|alameda|travessa|\d+\s*[ªa]\s*avenida)\b', x.strip())),
               trechos[-1]).strip()
    num = re.search(r',\s*(?:n[º°o]\.?\s*)?(\d{1,5})\b', rua)
    return rua_em_numero(limpar_rua(rua) or rua.split(',')[0].strip()), num.group(1) if num else '', bairro.strip(), cidade.strip()


def endereco_curto(s):
    """Endereço sem ' - ' antes do bairro: 'Cedros, Camboriú - SC, Brasil', 'Bairro Centro, Balneário Camboriú - SC',
    'Av. Atlântica, 2700, Balneário Camboriú - SC, Brasil' -> (rua, número, bairro, cidade)"""
    m = re.search(r'([^,]+?) - (?:SC|(?:State of )?Santa Catarina)\b', s)
    if not m:
        return '', '', '', ''
    trechos = m.group(1).split(' - ')
    cidade = trechos[-1].strip()
    rua = numero = bairro = ''
    for x in [x.strip() for x in s[:m.start()].split(',') + trechos[:-1] if x.strip()]:
        if not rua and re.match(r'(?i)(rua|r\.|av\.?|avenida|alameda|travessa)\s', x):
            rua = rua_em_numero(limpar_rua(x) or x)
        elif rua and not numero and re.fullmatch(r'(?:n[º°o]\.?\s*)?\d{1,5}', x):
            numero = re.sub(r'\D', '', x)
        else:
            bairro = re.sub(r'(?i)^bairro\s+', '', x)
    return rua, numero, bairro, cidade


def montar(c, d):
    if 'aluguel' not in d['acao'] and d['acao']:
        return None                                              # venda
    if d['tipo'] and not any(x in norm(d['tipo']) for x in TIPOS_APTO):
        return None
    quartos = d['quartos'] if d['quartos'] is not None else c['quartos']
    no_texto = quartos_no_texto(d['titulo'], d['desc'])          # o campo às vezes não conta as suítes ('1 suíte + 1 dormitório' = 1)
    quartos = max(quartos or 0, no_texto or 0)
    if quartos < 2:
        return None
    rua, numero, bairro, cidade = endereco(d['endereco'])
    cidade = cidade or d['cidade_print'] or 'Balneário Camboriú'
    if 'balneario camboriu' not in norm(cidade):
        return None                                              # Camboriú (Tabuleiro, Cedros), Itajaí: anunciados no portal de BC
    preco = d['preco'] or c['preco']
    diaria = bool(re.search(r'/\s*dia|di[aá]ria', preco, re.I))
    valor = reais(preco)
    aluguel = None
    aviso = ''
    if not diaria and valor and valor >= 300:                    # R$ 0,01 / R$ 9,50 / R$ 14,00: valor de mentira
        anual = anual_no_texto(d['titulo'], d['desc']) if 'anual' not in d['acao'] and 'estudante' not in d['acao'] else None
        if anual and abs(anual - valor) > 0.05 * valor:
            # 'Alugo por até dezembro por $ 2750 ou anual $4.300' com R$ 2.500 no campo: vale o anual que o texto dá
            aviso = f'Aluguel anual: {brl(anual)} pelo texto do anúncio (o campo de preço do portal diz {brl(valor)})'
            valor = anual
        else:
            aviso = preco_temporada(d['acao'], d['titulo'], d['desc'], valor) or ''
        if valor > MAX_ALUGUEL:
            return None
        if not aviso or aviso.startswith('Aluguel anual'):
            aluguel = valor
    desc = d['desc']
    cab = []
    if aviso:
        cab.append(aviso)
    elif 'temporada' in d['acao']:
        cab.append('Aluguel temporada' + (f': {brl(valor)} por diária' if diaria and valor else ''))
    elif diaria:
        cab.append(f'Preço por diária: {brl(valor)}' if valor else 'Preço por diária')
    elif 'estudante' in d['acao']:
        cab.append('Aluguel para estudante')
    elif 'anual' in d['acao']:
        cab.append('Aluguel anual')
    if cab:
        desc = cab[0] + ('\n\n' + desc if desc else '')
    if d['atributos']:
        desc += ('\n\n' if desc else '') + 'Atributos: ' + ', '.join(d['atributos'])
    cond = d['cond'] if d['cond'] else taxa_no_texto(d['desc'], 'cond')
    iptu = d['iptu']
    if iptu and iptu > 600:
        iptu = iptu / 12                                         # o campo é mensal; acima de R$ 600 foi digitado o do ano
    iptu = iptu or taxa_no_texto(d['desc'], 'iptu')
    suites = suites_no_texto(d['titulo'], d['desc'])
    if suites is not None and suites > quartos:
        suites = None
    lat = lon = None
    if numero and d['latlon'] and -27.12 < d['latlon'][0] < -26.93 and -48.70 < d['latlon'][1] < -48.55:
        lat, lon = d['latlon']                                   # sem número o mapa é só o meio da rua; fora de BC é erro do mapa
    anunciante = re.sub(r'\s*-\s*CRECI.*$', '', d['anunciante'], flags=re.I).strip()
    anunciante = re.sub(r'\s*-\s*Particular\s*$', ' (particular)', anunciante, flags=re.I)
    return dict(
        id='B' + c['id'], fonte=FONTE, url=f'{BASE}/imoveis/{c["id"]}/', titulo=d['titulo'] or 'Apartamento', desc=desc,
        ativo=not re.search(r'\b(alugad[oa]|locad[oa])\b', norm(d['titulo'])),
        aluguel=aluguel, cond=cond or None, iptu=round(iptu, 2) if iptu else None,
        quartos=int(quartos), suites=suites, area=d['area'],
        bairro=bairro or d['bairro_print'], rua=rua or rua_no_texto(d['titulo'], d['desc']), cidade=cidade,
        lat=lat, lon=lon, local_exato=lat is not None,
        marcado_mobiliado='mobiliado' in [norm(a) for a in d['atributos']], publicado=d['atualizado'],
        anunciante=anunciante, fotos=d['fotos'])


# ---------- tudo

def buscar(chrome, progresso):
    t0 = time.time()
    site = Portal()
    try:
        cards = listar(site, progresso)
    except Exception as ex:
        raise RuntimeError(f'o Viva Balneário não respondeu ({str(ex)[:60]})')
    if not cards:
        raise RuntimeError('o Viva Balneário não devolveu nenhum anúncio (o site mudou?)')
    # pelo card só sai preço mensal acima de R$ 9.000; os quartos do card não contam as suítes ('1 suíte + 1 dormitório' = 1,
    # '2 suítes' = 0), então 0 ou 1 quarto no card ainda é lido e decidido pelo texto em montar()
    alvo = []
    for c in cards:
        v = reais(c['preco'])
        if v and v > MAX_ALUGUEL and not re.search(r'/\s*dia', c['preco']):
            continue
        alvo.append(c)
    progresso(f'{len(cards)} apartamentos para alugar no portal, {len(alvo)} com preço até R$ 9.000; lendo os anúncios')
    out, falhas, sem_data = [], [], []
    conta = dict(lidos=0, parados=0)
    lock = threading.Lock()

    def lido(parado_=False):
        with lock:
            conta['lidos'] += 1
            conta['parados'] += parado_
            if conta['lidos'] % 40 == 0:
                progresso(f'{conta["lidos"]} de {len(alvo)} anúncios vistos, {len(out)} em dia')

    def um(c):
        if time.time() - t0 > LIMITE_SEG:
            return
        try:
            try:
                p = ler_print(site.get('/print.php', {'ref': 'imo', 't': c['id']}))
            except Exception:
                p = ler_print('')
            if not p['atualizado']:
                sem_data.append(c['id'])          # sem a data: lê o anúncio mesmo assim
            elif parado(p['atualizado']):
                return lido(True)
            o = montar(c, ler_anuncio(site.get(f'/imoveis/{c["id"]}/'), p))
            if o:
                with lock:
                    out.append(o)
            lido()
        except Exception:
            with lock:
                falhas.append(c['id'])

    with ThreadPoolExecutor(PARALELO) as ex:
        list(ex.map(um, alvo))
    if alvo and not out and falhas:
        raise RuntimeError(f'o Viva Balneário não abriu os anúncios ({len(falhas)} falhas)')
    progresso(f'{len(out)} apartamentos em dia; {conta["parados"]} anúncios sem atualização há mais de 2 anos ficaram de fora'
              + (f'; {len(sem_data)} sem data de atualização' if sem_data else '')
              + (f'; {len(falhas)} não abriram' if falhas else '')
              + (f'; parou por tempo com {conta["lidos"]} de {len(alvo)} vistos' if conta['lidos'] + len(falhas) < len(alvo) else ''))
    out.sort(key=lambda o: o['publicado'], reverse=True)
    return out
