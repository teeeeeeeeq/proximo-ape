"""Leitor dos sites de imobiliárias no sistema SIGA (sigastr / Sigasoft): Garbari, Getúlio, Mundial e Primme.

Os quatro sites são o mesmo app Nuxt da SIGA e expõem, no próprio domínio, as rotas que o navegador usa:
  /api/listagem/imoveis?page=N&limite=50&operacao=aluguel|temporada   -> lista (JSON, sem descrição nem taxas)
  /api/imovel/<ID>?pgimovel=1&interno=0&desativado=0                   -> ficha completa (descrição, condomínio,
                                                                          IPTU, coordenada, fotos por pasta)
Não precisa de Chrome nem de login: urllib puro.
"""
import html, json, re, threading, time, unicodedata, urllib.request
from collections import Counter

NOME = 'Imobiliárias (SIGA)'
USA_CHROME = False

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'
SITES = (  # slug, endereço, nome da imobiliária, coordenada do escritório (pino padrão que não é do imóvel)
    ('garbari', 'https://garbariimoveis.com.br', 'Garbari Imóveis'),
    ('getulio', 'https://getulioimoveis.com.br', 'Getúlio Imóveis'),
    ('mundial', 'https://imoveismundial.com.br', 'Imóveis Mundial'),
    ('primme', 'https://primmeimoveis.com', 'Primme Imóveis'),
)
PRECO_MAX = 9000


# ---------- texto e números

def norm(s):
    return unicodedata.normalize('NFKD', str(s or '').lower()).encode('ascii', 'ignore').decode()


def texto(s):
    """HTML do editor da SIGA -> texto limpo, mantendo parágrafos e itens de lista."""
    s = str(s or '')
    s = re.sub(r'(?i)<li[^>]*>', '\n• ', s)
    s = re.sub(r'(?i)<br\s*/?>|</(p|div|li|h\d|tr|ul|ol)>', '\n', s)
    s = html.unescape(re.sub(r'<[^>]+>', ' ', s)).replace('\xa0', ' ')
    linhas = [re.sub(r'[ \t]+', ' ', x).strip() for x in s.split('\n')]
    out = '\n'.join(x for x in linhas if x != '•')
    return re.sub(r'\n{3,}', '\n\n', out).strip()


def valor(x):
    """'9.000,00' -> 9000.0 ; '' / '0,00' / None -> None"""
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


def data_iso(s):
    m = re.match(r'(\d{2})/(\d{2})/(\d{4})', str(s or ''))
    return f'{m.group(3)}-{m.group(2)}-{m.group(1)}' if m else ''


def caixa(s):
    """'centro' / 'CENTRO' -> 'Centro' (o filtro de bairro do app compara o texto exato)."""
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    if s and (s.islower() or s.isupper()):
        s = ' '.join(w if w in ('de', 'da', 'do', 'das', 'dos', 'e') else w.capitalize() for w in s.lower().split(' '))
    return s


# ---------- rede

def get_json(url, tentativas=4):
    ultimo = None
    for t in range(tentativas):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json', 'Accept-Language': 'pt-BR,pt;q=0.9'})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            if isinstance(d, dict) and d.get('error'):  # a SIGA às vezes devolve 503 embrulhado em 200
                raise RuntimeError(d.get('message') or 'erro')
            return d
        except Exception as ex:
            ultimo = ex
            time.sleep(1.5 * (t + 1))
    raise RuntimeError(str(ultimo)[:120])


# ---------- filtro e montagem

def tipo_apto(a):
    """Entre os tipos do imóvel (às vezes vem 'Apartamento 03 Dorm.' + 'Cobertura'), o de apartamento com mais dormitórios."""
    tipos = [t for t in a.get('Tipo') or [] if t.get('Categoria') == 'Apartamentos']
    if not tipos:
        return None
    def dorm(t):
        d = t.get('Dormitorios')
        if not d:
            m = re.search(r'(\d+)\s*dorm', norm(t.get('Tipo')))
            d = int(m.group(1)) if m else 0
        return int(d or 0)
    t = max(tipos, key=dorm)
    return dict(t, _dorm=dorm(t))


def passa(a):
    if a.get('Categoria') not in ('Locação', 'Temporada'):
        return False
    if norm(a.get('Cidade')).strip() != 'balneario camboriu':
        return False
    t = tipo_apto(a)
    if not t or t['_dorm'] < 1:   # 1 quarto entra; só fica se tiver espaço para escritório (coletar.py)
        return False
    v = valor(t.get('Valor'))
    return v is None or v <= PRECO_MAX


def fotos_de(d):
    out = []
    F = d.get('Fotos')
    pastas = list(F.values()) if isinstance(F, dict) else [F or []]
    if isinstance(F, dict) and 'Apresentacao' in F:
        pastas = [F['Apresentacao']] + [v for k, v in F.items() if k != 'Apresentacao']
    pastas.append(d.get('FotosEdificio') or [])
    for p in pastas:
        for f in sorted((x for x in p or [] if isinstance(x, dict)), key=lambda x: x.get('Posicao') or 0):
            u = f.get('Foto_Grande') or f.get('Foto_Media') or f.get('Foto_Pequena')
            if u and u.startswith('//'):
                u = 'https:' + u
            if u and u.startswith('http') and u not in out:
                out.append(u)
    return out


def descricao(d, t, aluguel_mensal):
    partes = []
    if d.get('Categoria') == 'Temporada':
        v = valor(t.get('Valor'))
        tv = (t.get('TipoValor') or 'diária').lower()
        partes.append('Locação por temporada' + (f' (valor anunciado: R$ {v:,.0f} por {tv}).'.replace(',', '.') if v and not aluguel_mensal else '.'))
    elif norm(d.get('TipoLocacao')) == 'estudante':
        partes.append('Locação para estudante (período de temporada, não é contrato anual).')
    for b in d.get('Descricao') or []:
        if not isinstance(b, dict) or b.get('Restrito'):
            continue
        corpo = texto(b.get('Texto'))
        tit = texto(b.get('Titulo'))
        if corpo:
            partes.append((tit + '\n' if tit and norm(tit) not in norm(corpo)[:len(tit) + 5] else '') + corpo)
    obs = [texto(d.get('ObsTaxasValores'))]
    if d.get('MostraObsValores'):
        obs.append(texto(d.get('ObsValores')))
    if d.get('MostraObsValoresAdicionais'):
        obs.append(texto(d.get('ObsValoresAdicionais')))
    obs = [x for x in obs if x]
    if obs:
        partes.append('Valores e taxas:\n' + '\n'.join(obs))
    carac = []
    C = d.get('Caracteristicas')
    for cat, itens in (C.items() if isinstance(C, dict) else []):
        nomes = [re.sub(r'^\W+', '', x.get('Nome') or '').strip() for x in itens or [] if isinstance(x, dict)]
        nomes = [n for n in nomes if n]
        if nomes:
            carac.append(f'{caixa(cat).capitalize()}: ' + ', '.join(nomes) + '.')
    it = d.get('ItensTemporada')
    if isinstance(it, list) and it:
        carac.append('Itens: ' + ', '.join(f"{x.get('Item', '').strip()}" + (f" ({x['Quantidade']})" if x.get('Quantidade') else '')
                                           for x in it if isinstance(x, dict) and x.get('Item')) + '.')
    ficha = []
    if d.get('Mobilia'):
        ficha.append(f"Mobília: {d['Mobilia']}")
    if isinstance(d.get('AceitaPet'), str) and d['AceitaPet']:
        ficha.append(d['AceitaPet'])
    if inteiro(d.get('Garagem')):
        ficha.append(f"Vagas: {inteiro(d.get('Garagem'))}")
    if isinstance(d.get('Andar'), str) and d['Andar']:
        ficha.append(d['Andar'])
    if isinstance(d.get('DistanciaMar'), str) and d['DistanciaMar']:
        ficha.append(d['DistanciaMar'])
    if ficha:
        carac.append(' · '.join(ficha))
    if carac:
        partes.append('\n'.join(carac))
    return '\n\n'.join(p for p in partes if p).strip()


def montar(slug, base, imob, lista, det):
    d = det or lista  # sem a ficha, monta com o que veio na lista (sem descrição e sem taxas)
    t = tipo_apto(d) or tipo_apto(lista)
    temporada = d.get('Categoria') == 'Temporada'
    tv = norm(t.get('TipoValor'))
    mensal = (not tv and not temporada) or tv.startswith('mens')
    v = valor(t.get('Valor'))
    aluguel = v if (mensal and not d.get('ValorRestrito')) else None
    cond = valor(d.get('ValorCondominio'))
    iptu = valor(d.get('ValorIPTU'))
    if iptu:
        tipo_iptu = norm(d.get('ValorIPTUTipo'))
        # sem tipo informado, valor alto demais para um mês (ou maior que 1/4 do aluguel) é o anual
        if tipo_iptu.startswith('anu') or (not tipo_iptu.startswith('mens') and (iptu >= 1000 or (aluguel and iptu > aluguel / 4))):
            iptu = round(iptu / 12, 2)
    try:
        lat, lon = float(d.get('Latitude')), float(d.get('Longitude'))
        if not (-27.2 < lat < -26.8 and -48.8 < lon < -48.45):
            lat = lon = None
    except (TypeError, ValueError):
        lat = lon = None
    area = valor(d.get('AreaPrivativa')) or valor(d.get('AreaTotalPrivativa'))
    link = d.get('Link') or lista.get('Link') or re.sub(r'^https?://[^/]+', '', d.get('URL') or lista.get('URL') or '')
    situacao = norm(d.get('Situacao'))
    return dict(
        id=f"SG{slug}{d.get('ID') or lista.get('ID')}", fonte=NOME, url=base + link,
        titulo=re.sub(r'\s+', ' ', html.unescape(str(d.get('Anuncio') or lista.get('Anuncio') or ''))).strip(),
        desc=descricao(d, t, mensal) if det else '',
        ativo=(d.get('Status', 1) in (1, '1', True)) and not re.search(r'locad|alugad|indispon', situacao),
        aluguel=aluguel, cond=cond, iptu=iptu, quartos=t['_dorm'] or None, suites=inteiro(d.get('Suites')),
        area=area, bairro=caixa(d.get('Bairro')), rua=caixa(d.get('Endereco')) if isinstance(d.get('Endereco'), str) else '',
        cidade=caixa(d.get('Cidade')), lat=lat, lon=lon, local_exato=lat is not None,
        marcado_mobiliado=norm(d.get('Mobilia')) == 'mobiliado',
        publicado=data_iso(d.get('DataPublicacao')) or data_iso(d.get('DataCadastro')),
        anunciante=imob, fotos=fotos_de(d) or fotos_de(lista))


def ler_site(slug, base, imob, progresso, res, erros):
    try:
        brutos = {}
        for op in ('aluguel', 'temporada'):
            page, ultima = 1, 1
            while page <= ultima and page <= 20:
                j = get_json(f'{base}/api/listagem/imoveis?page={page}&limite=50&operacao={op}'
                             f'&valorMinimo=0&valorMaximo=100000000&mobilia=0&ordem=1')
                for a in j.get('data') or []:
                    if isinstance(a, dict) and a.get('ID'):
                        brutos[a['ID']] = a
                ultima = int((j.get('meta') or {}).get('last_page') or 1)
                page += 1
                time.sleep(0.3)
    except Exception as ex:
        erros.append(f'{imob}: {ex}')
        return
    cand = [a for a in brutos.values() if passa(a)]
    progresso(f'{imob}: {len(brutos)} imóveis para alugar, {len(cand)} apartamentos de 1+ quartos em BC; lendo as fichas')
    for n, a in enumerate(cand, 1):
        try:
            try:
                det = get_json(f"{base}/api/imovel/{a['ID']}?pgimovel=1&interno=0&desativado=0", tentativas=2).get('data')
                det = det if isinstance(det, dict) and det.get('ID') else None
            except Exception:
                det = None
            if det and not passa(dict(det, Categoria=det.get('Categoria') or a.get('Categoria'))):
                continue  # a ficha desmentiu a lista (mudou de preço, cidade ou tipo)
            res.append(montar(slug, base, imob, a, det))
        except Exception:
            pass
        if n % 15 == 0:
            progresso(f'{imob}: fichas {n} de {len(cand)}')
        time.sleep(0.15)


def buscar(chrome, progresso):
    progresso('Imobiliárias SIGA: lendo Garbari, Getúlio, Mundial e Primme')
    res, erros, lock_res = [], [], threading.Lock()
    por_site = {s[0]: [] for s in SITES}
    ths = [threading.Thread(target=ler_site, args=(slug, base, imob, progresso, por_site[slug], erros), daemon=True)
           for slug, base, imob in SITES]  # um site por vez em cada fio: no máximo 1 requisição simultânea por site
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=280)
    if erros:
        # as quatro imobiliárias saem com o mesmo nome de fonte: devolver só parte faria o app achar que os anúncios
        # das que falharam saíram do ar. Melhor falhar inteiro e manter a lista anterior.
        raise RuntimeError('sites SIGA fora do ar: ' + '; '.join(erros))
    with lock_res:
        for slug, _, _ in SITES:
            res += por_site[slug]
    # pino repetido em muitos anúncios diferentes = coordenada padrão (escritório/centro), não a do imóvel
    rep = Counter((o['lat'], o['lon']) for o in res if o['lat'] is not None)
    for o in res:
        if o['lat'] is not None and rep[(o['lat'], o['lon'])] >= 4:
            o['local_exato'] = False
    vistos, out = set(), []
    for o in res:
        if o['id'] not in vistos:
            vistos.add(o['id'])
            out.append(o)
    progresso(f'Imobiliárias SIGA: {len(out)} apartamentos')
    return out
