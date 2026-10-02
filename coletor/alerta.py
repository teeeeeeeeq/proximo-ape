"""O perfil (o que o app mostra), o Claude conferindo as fotos e o aviso por e-mail.

Perfil: aluguel de R$ 3.500 a 6.000, só o aluguel (server.ALUGUEL_MIN/MAX; os leitores já buscam só essa faixa),
2 quartos (ou 1 quarto com escritório citado ou 55 m²+; 3 ou mais é grande demais), pelo menos semimobiliado ("planejados" sem dizer se é
mobiliado conta; "sem mobília" não), até 10 min de carro da Humains (em Itajaí, 15), até 10 min a pé da praia, sem
temporada e sem recusa de animais, fora da Barra (longe, mesmo sem endereço). Sem localização fica, com o aviso de que
falta o endereço.

O Claude confere as fotos de todos os anúncios do perfil, uma vez cada (o mesmo imóvel em sites diferentes conta uma
vez; dados/avaliacoes.json). Com a chave ANTHROPIC_API_KEY, pela API, aqui mesmo. Sem ela, pelo plano do dono: a busca
monta uma fila (folhas com as fotos numeradas e o texto de cada anúncio, na branch fila-claude), uma rotina do Claude Code
confere de hora em hora (coletor/rotina.py) e grava os vereditos na branch avaliacoes, que a busca seguinte usa.
Ele dá uma nota ao acabamento (excelente, bom, comum, ruim), diz se a cozinha é integrada à sala (obrigatório), escolhe
a foto que melhor mostra o apartamento por dentro e, quando as fotos não mostram o bastante, estima a chance (prédio,
preço, texto). Veredito:
  excelente - acabamento excelente e cozinha integrada: primeiro no app e por e-mail;
  bom       - acabamento bom e cozinha integrada: logo abaixo, no app;
  perguntar - nada contra, mas as fotos não mostram tudo e a chance é alta ou média: aba "Vale perguntar";
  nao       - cozinha fechada, acabamento comum ou ruim, ou chance baixa: o app esconde.
O e-mail é uma issue que menciona o dono do repositório (o GitHub manda o e-mail); cada imóvel vai uma vez só
(dados/avisos.json).
"""
import base64, io, json, os, re, shutil, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

import server as s

DONO = 'teeeeeeeeq'
APP = 'https://teeeeeeeeq.github.io/proximo-ape/'
SEGURO = 0.10        # seguro-fiança: 10% do aluguel (quando precisa)
PRAIA_A_PE = 10      # minutos
HUMAINS_CARRO = 10   # minutos; em Itajaí, HUMAINS_CARRO_ITAJAI
HUMAINS_CARRO_ITAJAI = 15
QUARTOS_MAX = 2      # 3 quartos ou mais é grande demais
LONGE = {'balneario camboriu': {'barra'}}   # bairros longe: fora mesmo quando o anúncio não dá o endereço
MAX_POR_AVISO = 15
MODELO = 'claude-opus-5-5'
FOTOS_IA = 16        # fotos por anúncio mandadas ao Claude (a cozinha às vezes é a 15ª)
FOTO_PX = 768        # lado maior das fotos mandadas ao Claude
MAX_IA = 40          # anúncios conferidos por rodada (os mais recentes primeiro; o resto fica para a próxima)
MAX_IA_DIA = 250     # e por dia: teto de gasto (~US$ 0,05 cada, ~US$ 12 no dia mais cheio)
PARALELO = 3         # conferidos ao mesmo tempo
GUARDA_DIAS = 30     # avaliações mais velhas que isso são refeitas
VERSAO = 4           # muda quando o que se pede ao Claude muda: o que foi avaliado antes é avaliado de novo
                     # (3 era "como o do Piatã"; 4 é a nota de acabamento com a cozinha integrada obrigatória)
FILA_MAX = 30        # sem a chave da API: anúncios por rodada da rotina do Claude (os mais recentes primeiro)
FILA_FOTOS = 12      # fotos por anúncio na fila, em duas folhas de 6 (512 x 384 cada foto)


def a_pe(m):
    return None if m is None else max(1, round(m * 1.25 / 75))   # ~4,5 km/h, ruas +25%


def de_carro(m):
    if m is None:
        return None
    r = m * 1.35
    return max(1, round(1 + min(r, 2000) / 333 + max(r - 2000, 0) / 583))   # o mesmo do app


def limite_humains(o):
    return HUMAINS_CARRO_ITAJAI if s.norm(o.get('cidade')).strip() == 'itajai' else HUMAINS_CARRO


def no_perfil(o):
    """O que o app mostra (e o Claude confere)."""
    if o.get('no_ar') is False or o.get('ativo') is False or o.get('temporada') or not o.get('aluguel'):
        return False
    if not s.ALUGUEL_MIN <= o['aluguel'] <= s.ALUGUEL_MAX or not s.na_regiao(o.get('cidade'), o.get('bairro')):
        return False
    c = s.norm(o.get('cidade')).strip() or 'balneario camboriu'
    if s.norm(o.get('bairro')).strip() in LONGE.get(c, ()):
        return False
    q = o.get('quartos')
    if q is not None and (q < 1 or q > QUARTOS_MAX or (q == 1 and not (o.get('escritorio') or (o.get('area') or 0) >= s.QUARTO_UNICO_M2))):
        return False   # 1 quarto só com espaço para escritório: citado no anúncio ou área grande
    texto = (o.get('titulo') or '') + '\n' + (o.get('desc') or '')
    if o.get('mobilia') == 'nao' or (o.get('mobilia') == 'sem info' and o.get('_src') != 'Facebook Marketplace'
                                     and 'planejad' not in s.norm(texto)):
        return False   # no Facebook quase ninguém escreve: o Claude vê pelas fotos
    if s.nao_aceita_animais(texto):   # texto inteiro: a recusa costuma vir no fim
        return False
    h, p = de_carro(o.get('humains_m')), a_pe(o.get('praia_m'))
    return (h is None or h <= limite_humains(o)) and (p is None or p <= PRAIA_A_PE)


def custo(o):
    """Aluguel + condomínio (estimado se o anúncio não diz) + IPTU, sem o seguro-fiança."""
    cond = o['cond'] if o.get('cond') is not None else (o.get('cond_est') or 0)
    base = max(o['aluguel'] + cond + (o.get('iptu') or 0), o.get('pacote') or 0)
    return round(base), cond, o.get('cond') is None and bool(o.get('cond_est'))


def _chave(o):
    """O mesmo imóvel em sites diferentes: mesmo aluguel, área e bairro (sem área, cada anúncio é um)."""
    if not o.get('area'):
        return o['id']
    return f"{round(o['aluguel'])}|{round(o['area'])}|{s.norm(o.get('bairro'))}"


# --- O Claude confere as fotos ---

_PEDIDO = """Você confere anúncios de apartamento para alugar em Balneário Camboriú (e arredores) para quem procura um \
apartamento para morar, com acabamento muito bom e a cozinha integrada à sala. As fotos vêm numeradas, e depois vem o \
anúncio. Responda:

- acabamento: a nota do acabamento e do estado do apartamento, pelas fotos de dentro.
  "excelente": impecável. Tudo novo ou recém-reformado, materiais de qualidade e bem feitos: porcelanato ou madeira \
bem assentados, de rejunte fino; bancadas de pedra ou quartzo; marcenaria planejada sob medida, bem acabada, na \
cozinha e nos quartos; banheiro moderno (box de vidro, metais e louças atuais); iluminação embutida, gesso ou sanca. \
Bem conservado e com bom gosto: dá vontade de morar.
  "bom": atual e bem cuidado, com planejados e piso atual, mas sem o capricho do excelente (mais simples, algum \
detalhe datado, gasto ou de gosto duvidoso).
  "comum": apartamento comum ou datado: acabamento antigo, móveis soltos ou cansados, cerâmica ou azulejo antigos, \
rejunte grosso, banheiro antigo.
  "ruim": malconservado, feio ou desagradável.
  "sem foto": as fotos não mostram o apartamento por dentro (só fachada, áreas comuns ou planta).
- cozinha_integrada (obrigatório para servir): a cozinha é aberta para a sala, na mesma área (cozinha americana, \
bancada ou ilha voltada para a sala, sem parede nem porta entre as duas)? "sim", "nao" (cozinha fechada, separada \
da sala) ou "incerto" (as fotos não deixam ver).
- chance: a chance de o apartamento ter acabamento bom ou excelente e cozinha integrada. Se as fotos mostram tudo, \
"alta" quando tem e "baixa" quando não tem. Quando faltam fotos de dentro ou da cozinha, estime pelo que dá para \
ver: prédio novo ou antigo, padrão do que aparece, o texto, e o preço para o tamanho e o lugar (barato demais para a \
região costuma ser apartamento antigo).
- melhor_foto: o número da foto que melhor mostra o apartamento por dentro: de preferência a cozinha integrada com a \
sala na mesma foto; senão a sala ou a cozinha; 0 se nenhuma mostra o interior.
- resumo: uma ou duas frases curtas em português sobre o acabamento (piso, marcenaria, bancadas, banheiro), a \
cozinha e a sala, e o que falta ver (ex.: "Tudo novo: porcelanato claro de rejunte fino, marcenaria sob medida em \
toda a casa, bancada de quartzo e cozinha aberta para a sala; banheiro com box de vidro." ou "Só fotos da fachada \
e dos quartos; prédio dos anos 90, a cozinha não aparece.").

As fotos mandam; o texto do anúncio ajuda. Fotos de áreas comuns do prédio (piscina, academia, fachada) não contam \
como foto do apartamento."""

_RESPOSTA = {
    'type': 'object',
    'properties': {
        'acabamento': {'type': 'string', 'enum': ['excelente', 'bom', 'comum', 'ruim', 'sem foto']},
        'cozinha_integrada': {'type': 'string', 'enum': ['sim', 'nao', 'incerto']},
        'chance': {'type': 'string', 'enum': ['alta', 'media', 'baixa']},
        'melhor_foto': {'type': 'integer'},
        'resumo': {'type': 'string'},
    },
    'required': ['acabamento', 'cozinha_integrada', 'chance', 'melhor_foto', 'resumo'],
    'additionalProperties': False,
}


def veredito(v):
    if v['cozinha_integrada'] == 'nao' or v['acabamento'] in ('comum', 'ruim'):
        return 'nao'
    if v['cozinha_integrada'] == 'sim' and v['acabamento'] in ('excelente', 'bom'):
        return v['acabamento']
    return 'perguntar' if v['chance'] in ('alta', 'media') else 'nao'


def _foto(url):
    """Baixa uma foto do anúncio para mandar ao Claude; None se não deu (fora do ar, grande demais, formato que a API não lê)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=20) as r:
            dado = r.read(3_700_001)
    except Exception:
        return None
    if len(dado) > 3_700_000:   # a API aceita até 5 MB por foto, já em base64
        return None
    try:   # foto menor basta para ver a cozinha e custa bem menos
        from PIL import Image
        im = Image.open(io.BytesIO(dado))
        if max(im.size) > FOTO_PX:
            im.thumbnail((FOTO_PX, FOTO_PX))
            saida = io.BytesIO()
            im.convert('RGB').save(saida, 'JPEG', quality=85)
            dado = saida.getvalue()
    except Exception:
        pass   # sem Pillow ou formato que ele não abre: manda como veio
    tipo = ('image/jpeg' if dado[:3] == b'\xff\xd8\xff' else 'image/png' if dado[:8] == b'\x89PNG\r\n\x1a\n' else
            'image/gif' if dado[:4] == b'GIF8' else 'image/webp' if dado[:4] == b'RIFF' and dado[8:12] == b'WEBP' else None)
    return tipo and {'type': 'image', 'source': {'type': 'base64', 'media_type': tipo, 'data': base64.standard_b64encode(dado).decode()}}


def _ficha(o):
    """O que o Claude lê junto com as fotos: preço, tamanho e lugar (para a chance) e o texto do anúncio."""
    partes = [f"aluguel {_brl(o['aluguel'])}" + (f" + condomínio {_brl(o['cond'])}" if o.get('cond') else '')]
    if o.get('area'):
        partes.append(f"{round(o['area'])} m²")
    if o.get('quartos'):
        partes.append(f"{o['quartos']} quartos")
    partes.append(', '.join(x for x in (o.get('bairro'), o.get('cidade') or 'Balneário Camboriú') if x))
    if o.get('predio'):
        partes.append('prédio ' + o['predio'])
    return f"Anúncio ({' · '.join(partes)}): {o.get('titulo') or ''}\n\n{(o.get('desc') or '')[:3000]}"


def avaliar_ia(o, cliente):
    """O Claude olha as fotos e o texto do anúncio. None se não deu para conferir agora (tenta de novo na próxima rodada)."""
    urls = [u for u in (o.get('fotos') or []) if isinstance(u, str) and u.startswith('http')][:FOTOS_IA]
    with ThreadPoolExecutor(6) as ex:
        baixadas = [(u, f) for u, f in zip(urls, ex.map(_foto, urls)) if f]
    if not baixadas:
        print(f"  {o['id']}: nenhuma das {len(urls)} fotos baixou; fica para a próxima")
        return None
    conteudo = []
    for i, (u, f) in enumerate(baixadas, 1):
        conteudo += [{'type': 'text', 'text': f'Foto {i}'}, f]
    conteudo.append({'type': 'text', 'text': _ficha(o)})
    r = cliente.beta.messages.create(
        model=MODELO, max_tokens=8000, system=_PEDIDO,
        betas=['server-side-fallback-2026-07-01'], fallbacks='default',   # se recusar, outro modelo responde
        output_config={'effort': 'low', 'format': {'type': 'json_schema', 'schema': _RESPOSTA}},
        messages=[{'role': 'user', 'content': conteudo}])
    if r.stop_reason == 'refusal':
        print(f"  {o['id']}: o Claude não avaliou (recusa)")
        v = dict(acabamento='sem foto', cozinha_integrada='incerto', chance='baixa', melhor_foto=0,
                 resumo='(o Claude não avaliou este anúncio)')
    elif r.stop_reason == 'max_tokens':
        return None
    else:
        v = json.loads(next(b.text for b in r.content if b.type == 'text'))
    if 1 <= v['melhor_foto'] <= len(baixadas):
        v['foto'] = baixadas[v['melhor_foto'] - 1][0]
    v['v'] = veredito(v)
    print(f"  {o['id']}: {v['v']} (acabamento {v['acabamento']} · integrada {v['cozinha_integrada']} · "
          f"chance {v['chance']}; {len(baixadas)} fotos, {r.usage.input_tokens} + {r.usage.output_tokens} tokens)")
    return v


def _imagem(url):
    """A foto como imagem do Pillow (para as folhas da fila); None se não deu."""
    try:
        from PIL import Image
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=20) as r:
            im = Image.open(io.BytesIO(r.read(8_000_000)))
            im.load()
        return im.convert('RGB')
    except Exception:
        return None


def _folha(imagens, primeira):
    """Até 6 fotos numeradas (a partir de `primeira`) numa folha de 3 x 2."""
    from PIL import Image, ImageDraw, ImageFont
    W, H = 512, 384
    folha = Image.new('RGB', (W * 3, H * 2), 'white')
    try:
        fonte = ImageFont.load_default(size=40)
    except Exception:
        fonte = ImageFont.load_default()
    d = ImageDraw.Draw(folha)
    for i, im in enumerate(imagens):
        im = im.copy()
        im.thumbnail((W - 6, H - 6))
        x0, y0 = (i % 3) * W, (i // 3) * H
        folha.paste(im, (x0 + (W - im.width) // 2, y0 + (H - im.height) // 2))
        d.rectangle([x0 + 4, y0 + 4, x0 + 70, y0 + 54], fill='black')
        d.text((x0 + 14, y0 + 6), str(primeira + i), fill='white', font=fonte)
    return folha


def montar_fila(grupos, faltam, pasta):
    """Sem a chave da API, quem confere é a rotina do Claude (pelo plano do dono). Para os que faltam (os mais recentes
    primeiro, até FILA_MAX): folhas com as fotos numeradas e o texto do anúncio em `pasta` (o workflow publica na branch
    fila-claude), com o pedido e o formato da resposta, que a rotina segue."""
    shutil.rmtree(pasta, ignore_errors=True)
    os.makedirs(pasta)
    itens = []
    for k in faltam[:FILA_MAX * 3]:
        if len(itens) >= FILA_MAX:
            break
        o, n = grupos[k][0], len(itens) + 1
        urls = _urls(o)
        if len(urls) > FILA_FOTOS:   # espalhadas pelo anúncio: a cozinha costuma vir lá pelo meio
            urls = [urls[round(i * (len(urls) - 1) / (FILA_FOTOS - 1))] for i in range(FILA_FOTOS)]
        with ThreadPoolExecutor(6) as ex:
            ims = [(u, im) for u, im in zip(urls, ex.map(_imagem, urls)) if im]
        if not ims:
            continue
        folhas = []
        for j in range(0, len(ims), 6):
            nome = f"{n:03d}-{'ab'[j // 6]}.jpg"
            _folha([im for _, im in ims[j:j + 6]], j + 1).save(os.path.join(pasta, nome), 'JPEG', quality=82)
            folhas.append(nome)
        itens.append(dict(chave=k, id=o['id'], url=o.get('url'), folhas=folhas, fotos=[u for u, _ in ims], anuncio=_ficha(o)[:2500]))
    with open(os.path.join(pasta, 'fila.json'), 'w') as f:
        json.dump(dict(gerada=time.strftime('%Y-%m-%d %H:%M'), versao=VERSAO, pedido=_PEDIDO, resposta=_RESPOSTA, itens=itens),
                  f, ensure_ascii=False, indent=1)
    return len(itens)


def _urls(o):
    return list(dict.fromkeys(u for u in (o.get('fotos') or []) if isinstance(u, str) and u.startswith('http')))


def _pronto(o, agora):
    """Dá para conferir: 2+ fotos, ou 1 só num anúncio que o app já vê há um dia (no Facebook, as outras fotos chegam
    quando o leitor abre a página do anúncio, às vezes uma ou duas buscas depois)."""
    n = len(_urls(o))
    if n >= 2:
        return True
    try:
        visto = time.mktime(time.strptime((o.get('visto_em') or '')[:16], '%Y-%m-%d %H:%M'))
    except ValueError:
        return n == 1
    return n == 1 and agora - visto > 86400


def _da_rotina(avals):
    """Junta os vereditos que a rotina do Claude gravou na branch avaliacoes (o workflow baixa em dados/avaliacoes_rotina.json)."""
    novos = 0
    for k, v in s.load('avaliacoes_rotina.json', {}).items():
        if not isinstance(v, dict) or v.get('versao') != VERSAO:
            continue
        if any(c not in v or ('enum' in p and v[c] not in p['enum']) for c, p in _RESPOSTA['properties'].items()):
            continue
        if k in avals and (avals[k].get('quando') or '') >= (v.get('quando') or ''):
            continue
        avals[k] = dict(v, v=veredito(v))
        novos += 1
    return novos


def _cliente():
    if not os.environ.get('ANTHROPIC_API_KEY'):
        return None
    import anthropic
    return anthropic.Anthropic(max_retries=4)


def conferir(grupos, agora):
    """Confere pelo Claude os grupos do perfil que ainda não foram conferidos (os mais recentes primeiro, até MAX_IA por
    rodada e MAX_IA_DIA por dia). Devolve as avaliações de todos, por chave."""
    avals = s.load('avaliacoes.json', {})
    limite = time.strftime('%Y-%m-%d', time.localtime(agora - GUARDA_DIAS * 86400))
    avals = {k: v for k, v in avals.items() if (v.get('quando') or '') >= limite and v.get('versao') == VERSAO}
    vindos = _da_rotina(avals)
    if vindos:
        s.save('avaliacoes.json', avals)
    cliente = _cliente()
    faltam = sorted((k for k in grupos if k not in avals and _pronto(grupos[k][0], agora)),
                    key=lambda k: max(x.get('visto_em') or '' for x in grupos[k]), reverse=True)
    if not cliente:
        if os.environ.get('SOMENTE', '').strip().lower() == 'nenhuma':   # rodada só para publicar: a fila fica como está
            print(f'Claude: {vindos} veredito(s) novo(s) da rotina; {len(faltam)} ainda sem conferir')
            return avals
        n = montar_fila(grupos, faltam, os.path.join(s.RAIZ, 'fila'))
        print(f'Claude: {vindos} veredito(s) novo(s) da rotina; {len(faltam)} sem conferir, {n} na fila para a próxima rodada da rotina')
        return avals
    hoje = time.strftime('%Y-%m-%d', time.localtime(agora))
    vez = max(0, min(MAX_IA, MAX_IA_DIA - sum((v.get('quando') or '').startswith(hoje) for v in avals.values())))
    import anthropic
    feitos, parou = 0, ''

    def um(k):
        try:
            return k, avaliar_ia(grupos[k][0], cliente), None
        except Exception as ex:
            return k, None, ex
    for i in range(0, min(vez, len(faltam)), PARALELO):
        with ThreadPoolExecutor(PARALELO) as ex:
            for k, v, erro in ex.map(um, faltam[i:min(i + PARALELO, vez)]):
                if v:
                    v.update(quando=time.strftime('%Y-%m-%d %H:%M', time.localtime(agora)), versao=VERSAO)
                    avals[k] = v
                    feitos += 1
                elif erro:
                    print(f"  {grupos[k][0]['id']}: o Claude não respondeu ({type(erro).__name__}: {str(erro)[:200]})")
                    if isinstance(erro, (anthropic.RateLimitError, anthropic.AuthenticationError, anthropic.PermissionDeniedError)) \
                            or 'credit' in str(erro).lower():
                        parou = type(erro).__name__   # limite de uso, chave errada ou sem crédito: tenta na próxima rodada
        s.save('avaliacoes.json', avals)
        if parou:
            break
    print(f'Claude: {feitos} conferido(s) agora; {len(faltam) - feitos} ainda na fila' + (f' (parou: {parou})' if parou else ''))
    return avals


def para_o_app(v):
    """O que o app mostra da avaliação."""
    return {k: v[k] for k in ('v', 'foto', 'resumo', 'acabamento', 'cozinha_integrada', 'chance') if v.get(k)}


# --- O aviso por e-mail ---

def _brl(v):
    return 'R$ ' + f'{round(v):,}'.replace(',', '.')


def _data(d):
    return '/'.join(reversed(d[:10].split('-')[1:])) if d else ''


def _bloco(grupo, aval):
    o = grupo[0]
    total, cond, estimado = custo(o)
    zona = o.get('bairro') or 'Bairro não informado'
    if s.norm(o.get('cidade')).strip() not in ('', 'balneario camboriu'):
        zona += ' · ' + o['cidade']
    linhas = [f"### {zona}{' · ' + o['rua'] if o.get('rua') else ''} — aluguel {_brl(o['aluguel'])}"]
    foto = aval.get('foto') or (o.get('fotos') or [None])[0]
    if foto:
        linhas.append(f"<img src=\"{foto}\" width=\"420\">")
    if aval.get('resumo'):
        linhas.append(f"- **Pelas fotos:** {aval['resumo']}")
    partes = [f"aluguel {_brl(o['aluguel'])}"]
    if o.get('pacote') and o.get('cond_fonte') == 'pacote':
        partes.append(f"taxas {_brl(cond)} (pacote do anúncio)")
    elif cond:
        partes.append(f"condomínio {'~' if estimado else ''}{_brl(cond)}{' (estimado)' if estimado else ''}")
    elif o.get('cond_fonte') == 'incluso':
        partes.append('condomínio incluso')
    if o.get('iptu'):
        partes.append(f"IPTU {_brl(o['iptu'])}")
    linhas.append(f"- {_brl(total)}/mês: {' + '.join(partes)} (seguro-fiança, se precisar: +{_brl(SEGURO * o['aluguel'])})")
    mob = {'texto': 'mobiliado', 'marcado': 'mobiliado (marcado no site)', 'semi': 'semimobiliado',
           'sem info': 'mobília: o anúncio não diz'}.get(o.get('mobilia'), '')
    ficha = [f"{o['quartos']} quartos" + (f" ({o['suites']} suíte)" if o.get('suites') else '')] if o.get('quartos') else []
    if o.get('area'):
        ficha.append(f"{round(o['area'])} m²")
    ficha += [mob] if mob else []
    if o.get('escritorio'):
        ficha.append('cita escritório / home office')
    linhas.append('- ' + ' · '.join(ficha))
    if o.get('praia_m') is None or o.get('humains_m') is None:
        linhas.append('- **endereço não informado**: pergunte antes de visitar')
    else:
        aprox = '' if o.get('local_exato') else '~'
        linhas.append(f"- praia {aprox}{a_pe(o['praia_m'])} min a pé · Humains {aprox}{de_carro(o['humains_m'])} min de carro"
                      + (f" · prédio {o['predio']}" if o.get('predio') else ''))
    quando = []
    if o.get('baixou_de') and o['baixou_de'] > o['aluguel']:
        quando.append(f"**baixou {_brl(o['baixou_de'] - o['aluguel'])}** (era {_brl(o['baixou_de'])})")
    if o.get('publicado'):
        quando.append('publicado ' + _data(o['publicado']))
    quando.append(f"anunciante: {o.get('anunciante') or '—'}")
    linhas.append('- ' + ' · '.join(quando))
    links, vistos = [], {}
    for x in grupo:
        if not x.get('url'):
            continue
        nome = x.get('fonte') or ''
        if nome.startswith('Imobiliárias') or not nome:   # o leitor de imobiliárias: mostra o site
            nome = re.sub(r'^www\.', '', re.sub(r'^https?://([^/]+).*$', r'\1', x['url']))
        vistos[nome] = vistos.get(nome, 0) + 1
        links.append(f"[Abrir no {nome}{' ' + str(vistos[nome]) if vistos[nome] > 1 else ''}]({x['url']})")
    linhas.append('- ' + ' · '.join(links))
    return '\n'.join(linhas)


def avisar(grupos, avals, pasta):
    """Escreve alerta_titulo.txt e alerta.md em `pasta` com os de acabamento excelente ainda não avisados (avisos.json)."""
    for n in ('alerta_titulo.txt', 'alerta.md'):
        try:
            os.remove(os.path.join(pasta, n))
        except OSError:
            pass
    avisados = s.load('avisos.json', {})
    novos = [g for k, g in grupos.items() if (avals.get(k) or {}).get('v') == 'excelente'
             and (g[0].get('quartos') or 0) >= 2 and k not in avisados and not any(x['id'] in avisados for x in g)]
    if not novos:
        return 0
    novos.sort(key=lambda g: g[0]['aluguel'])
    quando = time.strftime('%Y-%m-%d %H:%M')
    for g in novos:
        avisados[_chave(g[0])] = quando
        for x in g:
            avisados[x['id']] = quando
    s.save('avisos.json', avisados)
    o = novos[0][0]
    if len(novos) == 1:
        titulo = f"Apê de acabamento excelente: {o.get('bairro') or 'BC'} · aluguel {_brl(o['aluguel'])}"
    else:
        titulo = f"{len(novos)} apês de acabamento excelente (aluguel a partir de {_brl(o['aluguel'])})"
    corpo = [f"@{DONO} {'apareceu um apartamento' if len(novos) == 1 else f'apareceram {len(novos)} apartamentos'} "
             f"de acabamento excelente e cozinha integrada à sala, {'conferido' if len(novos) == 1 else 'conferidos'} pelo "
             f"Claude nas fotos. Aluguel de {_brl(s.ALUGUEL_MIN)} a {_brl(s.ALUGUEL_MAX)}, 2+ quartos, até "
             f"{PRAIA_A_PE} min a pé da praia e {HUMAINS_CARRO} min de carro da Humains ({HUMAINS_CARRO_ITAJAI} em Itajaí).", '']
    corpo += [_bloco(g, avals[_chave(g[0])]) + '\n' for g in novos[:MAX_POR_AVISO]]
    if len(novos) > MAX_POR_AVISO:
        corpo.append(f"…e mais {len(novos) - MAX_POR_AVISO} no app.")
    corpo.append(f"\n[Abrir o app]({APP}) · Tempos estimados pela distância; ~ = endereço aproximado.")
    with open(os.path.join(pasta, 'alerta_titulo.txt'), 'w') as f:
        f.write(titulo[:250])
    with open(os.path.join(pasta, 'alerta.md'), 'w') as f:
        f.write('\n'.join(corpo))
    return len(novos)


def processar(anuncios, pub, pasta):
    """Depois da busca: o Claude confere o que falta, cada anúncio publicado ganha o veredito (pub[i]['ia']) e sai o
    aviso dos novos de acabamento excelente. `pub` são os anúncios do app (resumidos); `anuncios`, os completos."""
    agora = time.time()
    grupos = {}
    for x in sorted(pub, key=lambda x: x.get('fonte') != 'ZAP'):
        o = anuncios.get(x['id']) or x
        grupos.setdefault(_chave(o), []).append(o)
    avals = conferir(grupos, agora)
    por_id = {o['id']: avals[k] for k, g in grupos.items() if k in avals for o in g}
    for x in pub:
        if x['id'] in por_id:
            x['ia'] = para_o_app(por_id[x['id']])
    conta = {}
    for k in grupos:
        v = (avals.get(k) or {}).get('v') or 'a conferir'
        conta[v] = conta.get(v, 0) + 1
    print('Claude: ' + ', '.join(f'{n} {v}' for v, n in sorted(conta.items())))
    return avisar(grupos, avals, pasta)
