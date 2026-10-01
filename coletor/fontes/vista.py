"""Leitor dos sites de imobiliárias no CRM Vista (Loft): Broulée, Casa Forte e Vangogh (Van Gogh Imobiliária).

O CRM é o mesmo, mas os sites são de dois fabricantes, e nenhum expõe API aberta (a chave da API Vista não aparece
nos sites e não é usada aqui). Lemos as páginas públicas, com urllib puro:

  * Broulée e Casa Forte: site Next.js da Loft ("loftsites"). A busca
        /busca?finalidade=aluguel&order=menor-preco&page=N
    vem renderizada no servidor com o JSON dos imóveis (12 por página) dentro do fluxo RSC (self.__next_f.push).
    A ficha /imovel/<slug>-<código> traz descrição completa, condomínio, IPTU, diária, todas as fotos e características.
    O servidor da Loft devolve HTTP 429 com poucas requisições seguidas (e conta as recusadas), então as duas são lidas
    em sequência, com intervalo entre requisições, e só as páginas necessárias: a busca ordenada do mais barato até
    passar de R$ 9.000, e a(s) última(s) página(s), onde caem os anúncios sem preço ("consulte").
  * Vangogh: WordPress da Rocket Imob. /aluguel/residencial/ traz os 12 primeiros cards e /u-sr.php os próximos 12
    (pela sessão PHP). A ficha /imovel/<código>/<slug>/ traz o registro Vista inteiro em `var imovelDataLayer = {...}`
    e o pino do mapa (geocodificado pelo site a partir do endereço).
"""
import html, http.cookiejar, json, math, re, threading, time, unicodedata, urllib.error, urllib.request

NOME = 'Imobiliárias (Vista/Loft)'
USA_CHROME = False

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'
LOFT = (  # slug, endereço, imobiliária
    ('broulee', 'https://broulee.com.br', 'Broulée'),
    ('casaforte', 'https://casafortebc.com.br', 'Casa Forte'),
)
VG = ('vangogh', 'https://vgimobiliaria.com.br', 'Vangogh Imobiliária')
PRECO_MAX = 9000
PRAZO_FICHAS = 230   # s desde o início: depois disso não abre mais fichas (monta com o que veio na lista)
PRAZO_TOTAL = 285    # s: limite duro
LOFT_INTERVALO = 8.0  # s entre o fim de uma requisição à Loft e o início da próxima
LOFT_ESPERAS_429 = (20, 40, 60)
MAX_PAGINAS = 15
DEC = json.JSONDecoder()


# ---------- texto e números

def norm(s):
    return unicodedata.normalize('NFKD', str(s or '').lower()).encode('ascii', 'ignore').decode()


def limpo(s):
    """Texto do CRM (às vezes com HTML) -> texto limpo, mantendo parágrafos."""
    s = str(s or '').replace('\r\n', '\n').replace('\r', '\n')
    if re.search(r'<[a-zA-Z/][^>]*>', s):
        s = re.sub(r'(?i)<li[^>]*>', '\n• ', s)
        s = re.sub(r'(?i)<br\s*/?>|</(p|div|li|h\d|tr|ul|ol)>', '\n', s)
        s = re.sub(r'<[^>]+>', ' ', s)
    s = html.unescape(s).replace('\xa0', ' ')
    linhas = [re.sub(r'[ \t]+', ' ', x).strip() for x in s.split('\n')]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(linhas)).strip()


def valor(x):
    """6300 / '6300' / '1.32' / '7.500,00' / 'R$ 7.500,00' -> float ; vazio, zero, '$undefined' -> None"""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x) or None
    s = str(x).strip()
    if re.fullmatch(r'\d+(\.\d+)?', s):
        v = float(s)
    else:
        s = re.sub(r'[^\d,]', '', s).replace(',', '.')
        try:
            v = float(s) if s else 0.0
        except ValueError:
            return None
    return v or None


def inteiro(x):
    v = valor(x)
    return int(v) if v else None


def moeda(v):
    return f'{v:,.0f}'.replace(',', '.')


def caixa(s):
    """'CENTRO' / 'centro' -> 'Centro' (o filtro de bairro do app compara o texto exato)."""
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    if s and (s.islower() or s.isupper()):
        s = ' '.join(w if w in ('de', 'da', 'do', 'das', 'dos', 'e') else w.capitalize() for w in s.lower().split(' '))
    return s


def rua_de(tipo, end):
    """('Rua', '3700') -> 'Rua 3700' ; ('Avenida', 'Avenida Atlantica') -> 'Avenida Atlantica' ; (None, 'Rua 916') -> 'Rua 916'"""
    end = re.sub(r'\s+', ' ', str(end or '')).strip(' ,-')
    tipo = re.sub(r'\s+', ' ', str(tipo or '')).strip()
    if not end or end.startswith('$'):
        return ''
    if re.match(r'(?i)(rua|r\.|avenida|av\.?|alameda|travessa|servid[aã]o|estrada|rodovia|pra[cç]a|largo)\s', end + ' '):
        return re.sub(r'(?i)^(avenida|rua)\s+\1\s+', r'\1 ', end)
    if tipo and not tipo.startswith('$'):
        return f'{tipo} {end}'
    return f'Rua {end}' if re.fullmatch(r'\d{2,4}[a-zA-Z]?', end) else end  # ruas numeradas de BC: sempre "Rua NNNN"


NUMS = {'um': 1, 'uma': 1, 'dois': 2, 'duas': 2, 'tres': 3, 'quatro': 4, 'cinco': 5, 'seis': 6}


def quartos_texto(s):
    """'duas suítes e dois quartos' / '02 dormitórios' -> maior número citado (só quando o cadastro não traz)."""
    achados = []
    for m in re.finditer(r'\b(\d{1,2}|um|uma|dois|duas|tres|quatro|cinco|seis)\s+(?:\(\w+\)\s+)?(?:amplos?\s+|amplas?\s+|otimos?\s+)?'
                         r'(quartos?|dormitorios?|dorms?\b|suites?)', norm(s)):
        n = NUMS.get(m.group(1)) or int(m.group(1))
        if 0 < n <= 8:
            achados.append(n)
    return max(achados) if achados else None


def area_texto(s):
    m = re.search(r'(\d{2,4}(?:[.,]\d{1,2})?)\s*m(?:²|2)\s*(?:de\s+[aá]rea\s+)?privativ', str(s or ''), re.I)
    v = valor(m.group(1).replace('.', ',')) if m else None
    return v if v and 15 <= v <= 2000 else None


def iptu_mensal(v, aluguel):
    """O CRM guarda o IPTU sem dizer se é anual; valor alto para um mês (ou > 1/4 do aluguel) é o anual."""
    if not v:
        return None
    if v >= 1000 or (aluguel and v > aluguel / 4):
        return round(v / 12, 2)
    return v


def coord(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None
    if -27.2 < lat < -26.8 and -48.8 < lon < -48.45:
        return lat, lon
    return None, None


def e_apto(categoria):
    n = norm(categoria)
    return bool(re.search(r'apart|cobertura|penthouse|loft|studio|flat|kitnet|garden|duplex|triplex', n)) and not re.search(r'casa|sala|terreno|galpao|loja|sobrado', n)


def e_bc(cidade):
    return norm(cidade).strip() == 'balneario camboriu'


def data_iso(s):
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', str(s or ''))
    return m.group(0) if m else ''


# ---------- rede

def pedir(url, opener=None, extra=None, timeout=45):
    h = {'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
         'Accept-Language': 'pt-BR,pt;q=0.9'}
    h.update(extra or {})
    req = urllib.request.Request(url, headers=h)
    with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


class LoftRede:
    """Uma fila só para os dois sites da Loft: o limite de frequência é do servidor deles, não de cada site."""

    def __init__(self, fim):
        self.fim = fim
        self.ultimo = 0.0

    def get(self, url):
        tentativa = 0
        while True:
            dt = self.ultimo + LOFT_INTERVALO - time.time()
            if dt > 0:
                time.sleep(dt)
            try:
                return pedir(url)
            except urllib.error.HTTPError as ex:
                if ex.code != 429 or tentativa >= len(LOFT_ESPERAS_429):
                    raise RuntimeError(f'HTTP {ex.code}')
                espera = LOFT_ESPERAS_429[tentativa]
                if time.time() + espera > self.fim:
                    raise RuntimeError('limite de frequência (HTTP 429) sem tempo para esperar')
                time.sleep(espera)
            except Exception as ex:
                if tentativa >= 1:
                    raise RuntimeError(str(ex)[:100])
                time.sleep(5)
            finally:
                self.ultimo = time.time()
            tentativa += 1


# ---------- Loft: fluxo RSC do Next.js

def fluxo(pagina):
    partes = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,(".*?")\]\)</script>', pagina, re.S):
        try:
            partes.append(json.loads(m.group(1)))
        except ValueError:
            pass
    return ''.join(partes)


def linhas_rsc(s):
    """Linhas 'id:JSON' e linhas de texto 'id:T<tamanho em bytes, hex>,<texto>' (descrições longas vêm assim)."""
    b = s.encode('utf-8')
    rows, textos = {}, {}
    rx = re.compile(rb'([0-9a-f]{1,6}):')
    pos = 0
    while pos < len(b):
        m = rx.match(b, pos)
        if not m:
            nl = b.find(b'\n', pos)
            if nl < 0:
                break
            pos = nl + 1
            continue
        k, p = m.group(1).decode(), m.end()
        if b[p:p + 1] == b'T':
            c = b.find(b',', p)
            try:
                n = int(b[p + 1:c], 16)
            except ValueError:
                pos = p
                continue
            textos[k] = b[c + 1:c + 1 + n].decode('utf-8', 'replace')
            pos = c + 1 + n
            continue
        nl = b.find(b'\n', p)
        nl = len(b) if nl < 0 else nl
        linha = b[p:nl].decode('utf-8', 'replace')
        if linha[:1] in '[{':
            try:
                rows[k] = json.loads(linha)
            except ValueError:
                pass
        pos = nl + 1
    return rows, textos


def _texto_filhos(ch, textos):
    if isinstance(ch, str):
        m = re.fullmatch(r'\$([0-9a-f]+)', ch)
        if m:
            return textos.get(m.group(1), '')
        return '' if ch.startswith('$') else ch
    if isinstance(ch, list) and ch and all(isinstance(x, str) for x in ch):
        return ''.join(_texto_filhos(x, textos) for x in ch)
    return ''


def _elementos(no, textos, acc):
    """Percorre a árvore React serializada: (tag, className, texto direto) na ordem do documento."""
    if isinstance(no, list):
        if len(no) >= 4 and no[0] == '$' and isinstance(no[1], str) and isinstance(no[3], dict):
            p = no[3]
            cls = p.get('className') if isinstance(p.get('className'), str) else ''
            acc.append((no[1], cls, _texto_filhos(p.get('children'), textos)))
            for k, v in p.items():
                if k not in ('className', 'style'):
                    _elementos(v, textos, acc)
        else:
            for x in no:
                _elementos(x, textos, acc)
    elif isinstance(no, dict):
        for v in no.values():
            _elementos(v, textos, acc)


def _objeto(s, marca, chave):
    """Primeiro objeto JSON que começa com `marca` e tem `chave`."""
    for m in re.finditer(re.escape(marca), s):
        try:
            o, _ = DEC.raw_decode(s, m.start())
        except ValueError:
            continue
        if isinstance(o, dict) and chave in o:
            return o
    return None


def loft_lista(pagina):
    """Página de busca -> (imóveis, total, por página, {código: caminho da ficha})."""
    s = fluxo(pagina)
    i = s.find('"properties":[')
    if i < 0:
        return None
    arr, fim = DEC.raw_decode(s, i + len('"properties":'))
    m = re.search(r'"total":(\d+)', s[fim:fim + 300]) or re.search(r'"total":(\d+)', s[max(0, i - 300):i])
    q = re.search(r'"quantity":(\d+)', s[fim:fim + 300])
    hrefs = {}
    for caminho, cod in re.findall(r'href="(/imovel/[^"?#]*?-(\d+))"', pagina):
        hrefs.setdefault(cod, caminho)
    arr = [{k: (None if v == '$undefined' else v) for k, v in a.items()} for a in arr if isinstance(a, dict) and a.get('Codigo')]
    return (arr, int(m.group(1)) if m else len(arr),
            int(q.group(1)) if q else 12, hrefs)


def loft_ficha(pagina):
    s = fluxo(pagina)
    if not s:
        return None
    rows, textos = linhas_rsc(s)
    f = {'valores': _objeto(s, '{"codigo":"', 'valorLocacao') or {}, 'geo': _objeto(s, '{"latitude":', 'latitude') or {}}
    fotos = []
    i = s.find('"foto":[')
    if i >= 0:
        try:
            fotos, _ = DEC.raw_decode(s, i + len('"foto":'))
        except ValueError:
            fotos = []
    f['fotos'] = fotos if isinstance(fotos, list) else []
    desc, titulo, carac = '', '', []
    for k in sorted(rows, key=lambda x: int(x, 16)):
        acc = []
        _elementos(rows[k], textos, acc)
        if any(t == 'h2' and 'semelhantes' in norm(x) for t, _, x in acc):
            continue
        for n, (tag, cls, txt) in enumerate(acc):
            if tag == 'h4' and norm(txt).strip() == 'descricao' and not desc:
                desc = next((x for t, _, x in acc[n + 1:] if t == 'p' and x.strip()), '')
            elif tag == 'h2' and 'line-clamp-2' in cls and 'bg-white' in cls and txt.strip() and not titulo:
                titulo = txt.strip()
            elif tag == 'span' and cls.strip() == 'text-sm font-normal' and txt.strip() and not txt.strip()[0].isdigit():
                if txt.strip() not in carac:
                    carac.append(txt.strip())
    f.update(desc=limpo(desc), titulo=titulo, carac=carac)
    return f


def loft_fotos(ficha, item):
    out = []
    lst = [x for x in (ficha or {}).get('fotos') or [] if isinstance(x, dict) and x.get('Foto')]
    lst = [x for x in lst if norm(x.get('ExibirNoSite')) != 'nao']
    lst.sort(key=lambda x: (norm(x.get('Destaque')) != 'sim', inteiro(x.get('Ordem')) or 999))
    for u in [x['Foto'] for x in lst] + list(item.get('Vitrine') or []) + [item.get('FotoDestaque')]:
        if isinstance(u, str) and u.startswith('//'):
            u = 'https:' + u
        if isinstance(u, str) and u.startswith('http') and u not in out:
            out.append(u)
        if lst and len(out) >= len(lst):
            break
    return out


def loft_quartos(item, texto=''):
    q = max(inteiro(item.get('Dormitorios')) or 0, inteiro(item.get('Suites')) or 0)
    return q or quartos_texto(texto)


def loft_candidato(a):
    """Filtro com o que vem na lista (a ficha confirma depois)."""
    if not (a.get('FinalidadeStatus') or {}).get('ALUGUEL') and 'alug' not in norm(a.get('Status')):
        return False
    if not e_bc(a.get('Cidade')) or not e_apto(a.get('Categoria')):
        return False
    q = loft_quartos(a, f"{a.get('TituloSite') or ''} {a.get('DescricaoWebResumo') or ''}")
    if q is not None and q < 1:   # 1 quarto entra; só fica se tiver espaço para escritório (coletar.py)
        return False
    v = valor(a.get('ValorLocacao'))
    return v is None or v <= PRECO_MAX


def loft_montar(slug, base, imob, a, caminho, ficha):
    cod = str(a['Codigo'])
    val = (ficha or {}).get('valores') or {}
    fonte_val = val if val else {'valorLocacao': a.get('ValorLocacao'), 'valorDiaria': a.get('ValorDiaria')}
    aluguel = valor(fonte_val.get('valorLocacao'))
    diaria = valor(fonte_val.get('valorDiaria'))
    desc_base = (ficha or {}).get('desc') or ''
    if not desc_base:
        resumo = limpo(a.get('DescricaoWebResumo'))
        desc_base = resumo + (' [...]' if resumo else '')
    titulo = limpo(a.get('TituloSite')) or (ficha or {}).get('titulo') or f"{a.get('Categoria') or 'Apartamento'} em {caixa(a.get('Bairro'))}"
    quartos = loft_quartos(a, f'{titulo}\n{desc_base}')
    partes = []
    if not aluguel and diaria:
        partes.append(f'Locação por temporada (diária a partir de R$ {moeda(diaria)}).')
    partes.append(desc_base)
    carac = (ficha or {}).get('carac') or []
    if carac:
        partes.append('Características: ' + ', '.join(carac) + '.')
    geo = (ficha or {}).get('geo') or {}
    lat, lon = coord(a.get('Latitude'), a.get('Longitude'))
    if lat is None:
        lat, lon = coord(geo.get('latitude'), geo.get('longitude'))
    area = valor(a.get('AreaPrivativa'))
    area = area if area and area >= 15 else (area_texto(desc_base) or None)
    return dict(
        id=f'VS{slug}{cod}', fonte=NOME,
        url=base + (caminho or f'/imovel/{cod}'),
        titulo=titulo, desc='\n\n'.join(p for p in partes if p).strip(), ativo=True,
        aluguel=aluguel, cond=valor(val.get('valorCondominio')), iptu=iptu_mensal(valor(val.get('valorIptu')), aluguel),
        quartos=quartos, suites=inteiro(a.get('Suites')), area=area,
        bairro=caixa(a.get('Bairro')), rua=rua_de(a.get('TipoEndereco'), a.get('Endereco')), cidade=caixa(a.get('Cidade')),
        lat=lat, lon=lon, local_exato=bool(lat is not None and geo.get('showPropertyExactLocation') is True),
        marcado_mobiliado=any(norm(c) == 'mobiliado' for c in carac),
        publicado=data_iso(a.get('DataCadastro')), anunciante=imob, fotos=loft_fotos(ficha, a))


def ler_loft(progresso, res, erros, t0):
    rede = LoftRede(t0 + PRAZO_TOTAL - 10)
    brutos = {}  # slug -> {código: (item, caminho)}
    for slug, base, imob in LOFT:
        try:
            achados = {}
            url = f'{base}/busca?finalidade=aluguel&order=menor-preco'
            lidas = set()

            def pagina(p):
                progresso(f'{imob}: página {p} da busca')
                r = loft_lista(rede.get(url + (f'&page={p}' if p > 1 else '')))
                if r is None:
                    raise RuntimeError('a busca veio sem a lista de imóveis')
                lidas.add(p)
                for a in r[0]:
                    achados[str(a['Codigo'])] = (a, r[3].get(str(a['Codigo'])))
                return r

            itens, total, qtd, _ = pagina(1)
            ultima = min(max(1, math.ceil(total / max(qtd, 1))), MAX_PAGINAS)
            todas = False
            p = 1
            while p < ultima:
                precos = [valor(a.get('ValorLocacao')) for a in itens if valor(a.get('ValorLocacao'))]
                if precos != sorted(precos):
                    todas = True  # a ordenação não veio: lê tudo
                if not todas and precos and max(precos) > PRECO_MAX:
                    break
                p += 1
                itens = pagina(p)[0]
            # sem preço ("consulte") vai para o fim da ordenação
            q = ultima
            while q not in lidas and q > 1:
                itens_q = pagina(q)[0]
                if not todas and itens_q and valor(itens_q[0].get('ValorLocacao')):
                    break
                q -= 1
            brutos[slug] = achados
        except Exception as ex:
            erros.append(f'{imob}: {ex}')
            return
    cand = [(slug, base, imob, a, cam) for slug, base, imob in LOFT for a, cam in brutos[slug].values() if loft_candidato(a)]
    progresso(f'Broulée e Casa Forte: {sum(len(v) for v in brutos.values())} imóveis para alugar, {len(cand)} apartamentos em BC até R$ 9 mil; lendo as fichas')
    for n, (slug, base, imob, a, cam) in enumerate(cand, 1):
        try:
            ficha = None
            if cam and time.time() < t0 + PRAZO_FICHAS:
                try:
                    progresso(f'{imob}: ficha {n} de {len(cand)}')
                    ficha = loft_ficha(rede.get(base + cam))
                except Exception:
                    ficha = None
            o = loft_montar(slug, base, imob, a, cam, ficha)
            if o['aluguel'] and o['aluguel'] > PRECO_MAX:
                continue  # a ficha desmentiu a lista
            if o['quartos'] is None or o['quartos'] < 1:   # 1 quarto entra; só fica se tiver espaço para escritório (coletar.py)
                continue
            res.append(o)
        except Exception:
            pass


# ---------- Vangogh (Rocket Imob)

def vg_cards(pagina):
    out = []
    for p in re.split(r'<div class="col-xs-12 imovel-box-single" data-codigo="', pagina)[1:]:
        try:
            cod = p[:p.find('"')]
            href = re.search(r'href="(https?://[^"]+/imovel/' + re.escape(cod) + r'/[^"]*)"', p)
            tit = re.search(r'<h2 class="titulo-grid">(.*?)</h2>', p, re.S)
            end = re.search(r'<h3 itemprop="streetAddress">(.*?)(?:<font|<br|</h3>)', p, re.S)
            preco = re.search(r'<span class="thumb-price"[^>]*>(.*?)</span>', p, re.S) or re.search(r'<span class="item-price-rent">(.*?)</span>', p, re.S)
            am = {norm(k): inteiro(v) for v, k in re.findall(r'<span>([^<]*)</span><small>([^<]*)</small>', p)}
            fotos = []
            for u in re.findall(r'(https://cdn\.vistahost\.com\.br/[^"\')\s]+\.(?:jpe?g|png|webp))', p, re.I):
                if u not in fotos:
                    fotos.append(u)
            endereco = limpo(end.group(1)) if end else ''
            cidade = endereco.rsplit(' - ', 1)[1] if ' - ' in endereco else ''
            bairro = endereco.rsplit(' - ', 1)[0].rsplit(',', 1)[-1].strip() if ' - ' in endereco else ''
            txt_preco = limpo(preco.group(1)) if preco else ''
            out.append(dict(cod=cod, url=href.group(1) if href else '', titulo=limpo(tit.group(1)) if tit else '',
                            endereco=endereco, cidade=cidade, bairro=bairro,
                            quartos=am.get('quartos') or am.get('quarto'), suites=am.get('suites') or am.get('suite'),
                            aluguel=valor(txt_preco) if re.search(r'\d', txt_preco) else None, fotos=fotos))
        except Exception:
            pass
    return out


def vg_candidato(c):
    if not e_bc(c['cidade']) or not e_apto(c['titulo'].split(' ')[0] if c['titulo'] else ''):
        return False
    q = max(c['quartos'] or 0, c['suites'] or 0)
    if q and q < 1:
        return False
    return c['aluguel'] is None or c['aluguel'] <= PRECO_MAX


def vg_ficha(pagina):
    i = pagina.find('var imovelDataLayer=')
    if i < 0:
        i = pagina.find('var imovelDataLayer =')
    if i < 0:
        return None
    d, _ = DEC.raw_decode(pagina, pagina.index('{', i))
    m = re.search(r'maps/embed/v1/place\?[^"\']*?&q=(-?[\d.]+),\s*(-?[\d.]+)', pagina)
    d['_mapa'] = (m.group(1), m.group(2)) if m else None
    return d if isinstance(d, dict) and d.get('Codigo') else None


def vg_fotos(d, card):
    F = d.get('Foto') if d else None
    lst = list(F.values()) if isinstance(F, dict) else (F if isinstance(F, list) else [])
    lst = [x for x in lst if isinstance(x, dict) and x.get('Foto') and norm(x.get('ExibirNoSite')) != 'nao']
    lst.sort(key=lambda x: norm(x.get('Destaque')) != 'sim')  # estável: mantém a ordem do site
    out = []
    for u in [x['Foto'] for x in lst] or card.get('fotos') or []:
        if isinstance(u, str) and u.startswith('//'):
            u = 'https:' + u
        if isinstance(u, str) and u.startswith('http') and u not in out:
            out.append(u)
    return out


def vg_montar(slug, base, imob, card, d):
    if not d:  # sem a ficha: só o que o card mostra
        cid, q = card['cidade'], max(card['quartos'] or 0, card['suites'] or 0) or None
        return dict(id=f'VS{slug}{card["cod"]}', fonte=NOME, url=card['url'], titulo=card['titulo'], desc='', ativo=True,
                    aluguel=card['aluguel'], cond=None, iptu=None, quartos=q, suites=card['suites'], area=None,
                    bairro=caixa(card['bairro']), rua=rua_de('', card['endereco'].split(',')[0]), cidade=caixa(re.sub(r'/\w+$', '', cid)),
                    lat=None, lon=None, local_exato=False, marcado_mobiliado=False, publicado='', anunciante=imob,
                    fotos=card['fotos'])
    aluguel = valor(d.get('ValorLocacao'))
    diaria = valor(d.get('ValorDiaria'))
    desc_base = limpo(d.get('DescricaoWeb'))
    sim = lambda D: [k for k, v in D.items() if str(v).strip().lower() == 'sim'] if isinstance(D, dict) else []
    carac, infra = sim(d.get('Caracteristicas')), sim(d.get('InfraEstrutura'))
    partes = []
    if not aluguel and diaria:
        partes.append(f'Locação por temporada (diária a partir de R$ {moeda(diaria)}).')
    partes.append(desc_base)
    if carac:
        partes.append('Características: ' + ', '.join(carac) + '.')
    if infra:
        partes.append('Infraestrutura: ' + ', '.join(infra) + '.')
    desc = '\n\n'.join(p for p in partes if p).strip()
    quartos = max(inteiro(d.get('Dormitorios')) or 0, inteiro(d.get('Suites')) or 0) or quartos_texto(desc)
    area = valor(d.get('AreaPrivativa'))
    area = area if area and area >= 15 else area_texto(desc_base)  # o site às vezes grava 117 como '1.17'
    lat, lon = coord(d.get('Latitude'), d.get('Longitude'))
    exato = lat is not None
    if lat is None and d.get('_mapa'):
        lat, lon = coord(*d['_mapa'])  # pino do mapa do site: geocodificado pelo endereço, aproximado
    cat = d.get('Categoria') or 'Apartamento'
    nome = limpo(d.get('TituloSite') or d.get('Empreendimento'))
    titulo = ' - '.join(x for x in (f'{cat} {quartos} quartos' if quartos else cat, nome) if x)
    return dict(
        id=f'VS{slug}{d["Codigo"]}', fonte=NOME, url=card['url'], titulo=titulo, desc=desc,
        ativo=not re.search(r'locad|alugad|vendid|suspens|indispon', norm(d.get('Situacao'))),
        aluguel=aluguel, cond=valor(d.get('ValorCondominio')), iptu=iptu_mensal(valor(d.get('ValorIptu')), aluguel),
        quartos=quartos, suites=inteiro(d.get('Suites')), area=area,
        bairro=caixa(d.get('Bairro')), rua=rua_de(d.get('TipoEndereco'), d.get('Endereco')), cidade=caixa(d.get('Cidade')),
        lat=lat, lon=lon, local_exato=exato,
        marcado_mobiliado=norm((d.get('Caracteristicas') or {}).get('Mobiliado') if isinstance(d.get('Caracteristicas'), dict) else '') == 'sim',
        publicado=data_iso(d.get('DataCadastro')), anunciante=imob, fotos=vg_fotos(d, card))


def ler_vg(progresso, res, erros, t0):
    slug, base, imob = VG
    try:
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        progresso(f'{imob}: página 1')
        pagina = pedir(base + '/aluguel/residencial/', op)
        m = re.search(r'Encontramos\s+(\d+)\s+im', pagina)
        total = int(m.group(1)) if m else None
        cards = vg_cards(pagina)
        if not cards and total != 0:
            raise RuntimeError('a lista de aluguel veio vazia')
        vistos = {c['cod'] for c in cards}
        for p in range(2, MAX_PAGINAS + 1):
            if total is not None and len(cards) >= total:
                break
            time.sleep(1.0)
            progresso(f'{imob}: página {p}')
            mais = [c for c in vg_cards(pedir(base + '/u-sr.php?queryData=&reload=0', op,
                                              {'X-Requested-With': 'XMLHttpRequest', 'Referer': base + '/aluguel/residencial/'}))
                    if c['cod'] not in vistos]
            if not mais:
                break
            cards += mais
            vistos |= {c['cod'] for c in mais}
    except Exception as ex:
        erros.append(f'{imob}: {str(ex)[:120]}')
        return
    cand = [c for c in cards if vg_candidato(c)]
    progresso(f'{imob}: {len(cards)} imóveis para alugar, {len(cand)} apartamentos em BC até R$ 9 mil; lendo as fichas')
    for c in cand:
        try:
            d = None
            if c['url'] and time.time() < t0 + PRAZO_FICHAS:
                time.sleep(1.0)
                try:
                    d = vg_ficha(pedir(c['url'], op))
                except Exception:
                    try:
                        time.sleep(3)
                        d = vg_ficha(pedir(c['url'], op))
                    except Exception:
                        d = None
            if d and (not e_bc(d.get('Cidade')) or not e_apto(d.get('Categoria'))):
                continue  # a ficha desmentiu o card
            o = vg_montar(slug, base, imob, c, d)
            if (o['aluguel'] and o['aluguel'] > PRECO_MAX) or o['quartos'] is None or o['quartos'] < 1 or not o['url']:
                continue
            res.append(o)
        except Exception:
            pass


# ---------- entrada

def buscar(chrome, progresso):
    t0 = time.time()
    progresso('Imobiliárias Vista/Loft: lendo Broulée, Casa Forte e Vangogh')
    loft, vg, erros = [], [], []
    ths = [threading.Thread(target=ler_loft, args=(progresso, loft, erros, t0), daemon=True),
           threading.Thread(target=ler_vg, args=(progresso, vg, erros, t0), daemon=True)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=max(1.0, t0 + PRAZO_TOTAL - time.time()))
    if any(t.is_alive() for t in ths):
        erros.append('demorou mais de 5 minutos')
    if erros:
        # as três imobiliárias saem com o mesmo nome de fonte: devolver só parte faria o app achar que os anúncios
        # das que falharam saíram do ar. Melhor falhar inteiro e manter a lista anterior.
        raise RuntimeError('Imobiliárias Vista/Loft fora do ar: ' + '; '.join(erros))
    vistos, out = set(), []
    for o in list(loft) + list(vg):
        if o['id'] not in vistos:
            vistos.add(o['id'])
            out.append(o)
    progresso(f'Imobiliárias Vista/Loft: {len(out)} apartamentos')
    return out
