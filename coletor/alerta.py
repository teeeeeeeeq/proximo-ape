"""Aviso por e-mail de apartamento novo que serve (o GitHub manda o e-mail de uma issue que menciona o dono do repositório).

Serve: 2+ quartos, mobiliado ou semimobiliado (os quartos podem estar vazios; "planejados" sem dizer se é mobiliado
conta), aluguel perto do apê do Piatã (R$ 4.000 a 5.000, só o aluguel), até R$ 6.000 com tudo (aluguel + condomínio,
estimado se o anúncio não diz + IPTU + seguro-fiança de 10% do aluguel), até 10 min a pé da praia e até 10 min de carro
da Humains. Recusa de animais já não chega aqui (coletar.py).

E, como o do Piatã: cozinha integrada à sala (o mais importante), cozinha bonita e bem montada (armários planejados,
bom gosto, bem equipada) e piso sem cara de antigo (nada de rejunte grosso e encardido ou azulejo esquisito; madeira
serve). Quem confere é o Claude, olhando as fotos (e o texto) do anúncio, e ele escolhe a foto do e-mail: a que mostra
a cozinha com a sala, ou senão a cozinha. Sem a chave ANTHROPIC_API_KEY, vale o que o texto do anúncio diz.
Cada anúncio é conferido uma vez só (dados/avaliacoes.json).

Novo: entrou no ar há pouco (o coletor viu pela primeira vez, fora da primeira leitura de cada fonte), foi publicado
nos últimos dias, ou o app viu baixar de preço há pouco. Cada imóvel é avisado uma vez só (dados/avisos.json; o aviso
antigo, sem as fotos, usava alertas.json), e o mesmo imóvel em sites diferentes (mesmo aluguel, área e bairro) vira
um aviso só, com os links de todos.
"""
import base64, io, json, os, re, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

import server as s

DONO = 'teeeeeeeeq'
APP = 'https://teeeeeeeeq.github.io/proximo-ape/'
TOTAL_MAX = 6000
ALUGUEL_MIN, ALUGUEL_MAX = 4000, 5000   # perto do Piatã: R$ 4.500 só o aluguel
SEGURO = 0.10        # seguro-fiança: 10% do aluguel
PRAIA_A_PE = 10      # minutos
HUMAINS_CARRO = 10   # minutos
NOVO_DIAS = 3        # entrou no ar / baixou há até 3 dias
PUBLICADO_DIAS = 7   # publicado no site há até 7 dias
MAX_POR_AVISO = 15
MODELO = 'claude-opus-5-5'
FOTOS_IA = 16        # fotos por anúncio mandadas ao Claude (a cozinha às vezes é a 15ª)
FOTO_PX = 900        # lado maior das fotos mandadas ao Claude
MAX_IA = 10          # anúncios conferidos pelo Claude por rodada (o resto fica para a próxima)
GUARDA_DIAS = 30     # avaliações mais velhas que isso são esquecidas
VERSAO = 2           # muda quando o que se pede ao Claude muda: o que foi avaliado antes é avaliado de novo

def a_pe(m):
    return None if m is None else max(1, round(m * 1.25 / 75))   # o mesmo do app antigo: ~4,5 km/h, ruas +25%


def de_carro(m):
    if m is None:
        return None
    r = m * 1.35
    return max(1, round(1 + min(r, 2000) / 333 + max(r - 2000, 0) / 583))   # o mesmo do app


def custo(o):
    cond = o['cond'] if o.get('cond') is not None else (o.get('cond_est') or 0)
    base = max(o['aluguel'] + cond + (o.get('iptu') or 0), o.get('pacote') or 0)
    return round(base + SEGURO * o['aluguel']), cond, o.get('cond') is None and bool(o.get('cond_est'))


def serve(o):
    if not o.get('aluguel') or (o.get('quartos') or 0) < 2 or o.get('temporada'):
        return False
    if not ALUGUEL_MIN <= o['aluguel'] <= ALUGUEL_MAX:
        return False
    texto = (o.get('titulo') or '') + '\n' + (o.get('desc') or '')
    if o.get('mobilia') not in ('texto', 'marcado', 'semi') and not (o.get('mobilia') == 'sem info' and 'planejad' in s.norm(texto)):
        return False
    if custo(o)[0] > TOTAL_MAX:
        return False
    p, h = a_pe(o.get('praia_m')), de_carro(o.get('humains_m'))
    return p is not None and h is not None and p <= PRAIA_A_PE and h <= HUMAINS_CARRO


def _dias(data, agora):
    try:
        return (agora - time.mktime(time.strptime(data[:16] if len(data) > 10 else data, '%Y-%m-%d %H:%M' if len(data) > 10 else '%Y-%m-%d'))) / 86400
    except (ValueError, TypeError):
        return 1e9


def novo(o, primeira, agora):
    """Entrou no ar há pouco, foi publicado há pouco ou o app viu baixar de preço há pouco. Foto nova / anúncio
    "subido" não conta, nem o preço riscado do OLX (não diz quando baixou: o anúncio pode ser de meses atrás)."""
    src = o.get('_src') or o.get('fonte')
    v = o.get('visto_em') or ''
    chegou = v and v > primeira.get(src, '') and not (o.get('publicado') and _dias(o['publicado'], agora) - _dias(v, agora) > 2)
    baixou_visto = len(o.get('precos') or []) > 1 and o.get('baixou_de') and o.get('baixou_em')
    return bool((chegou and _dias(v, agora) <= NOVO_DIAS)
                or (o.get('publicado') and _dias(o['publicado'], agora) <= PUBLICADO_DIAS)
                or (baixou_visto and _dias(o['baixou_em'], agora) <= NOVO_DIAS))


# --- Como o do Piatã: cozinha integrada à sala, cozinha bonita e bem montada, piso sem cara de antigo ---

_PLANEJADA = re.compile(r'\b(cozinha (toda )?planejad[oa]|(moveis|armarios|marcenaria) planejad[oa]s|planejados)\b')
_INTEGRADA = re.compile(r'\b(cozinha (integrada|americana|aberta)|integrad[oa]s? (a|com) (a )?(sala|cozinha)|'
                        r'(sala|living) (e cozinha )?integrad[oa]s?|ambientes integrados|conceito aberto)\b')
_PISO_ANTIGO = re.compile(r'\b(ceramica|azulejo|ardosia)\b')

_PEDIDO = """Você confere anúncios de apartamento para alugar em Balneário Camboriú para quem quase alugou um \
apartamento de que gostou muito e quer achar outro parecido. As fotos vêm numeradas. Responda:

- cozinha_integrada (o mais importante): a cozinha é aberta para a sala, na mesma área (cozinha americana, bancada \
ou ilha voltada para a sala, sem parede nem porta entre as duas)? "sim", "nao" (cozinha fechada, separada da sala) \
ou "incerto" (as fotos não deixam ver).
- cozinha: como é a cozinha. Eles passam muito tempo na cozinha ou em volta dela; a sala e o sofá são fáceis de \
deixar bonitos e confortáveis, a cozinha não. "bonita": armários planejados, sob medida, feitos com bom gosto, bem \
equipada, agradável de usar e de ficar. "simples": tem planejados, mas é básica, apertada, mal equipada ou com \
acabamento datado. "ruim": sem planejados (armário de aço, móveis soltos de loja), feia ou desagradável. "sem foto": \
nenhuma foto mostra a cozinha.
- piso: "antigo" só se dá para ver claramente piso ou azulejo de cara antiga: rejunte grosso e encardido, cerâmica \
antiga, azulejo esquisito pela casa. "bom": piso atual com rejunte fino ou sem rejunte. Não precisa ser porcelanato \
nem brilhar; madeira, vinílico ou laminado servem. "incerto" se não dá para ver.
- melhor_foto: o número da foto que melhor mostra a cozinha integrada com a sala (as duas na mesma foto); se nenhuma \
mostra as duas, a que melhor mostra a cozinha; 0 se nenhuma mostra a cozinha.
- resumo: uma ou duas frases curtas em português sobre a cozinha, a integração com a sala e o piso (ex.: "Cozinha \
americana aberta para a sala, armários planejados brancos e bancada de quartzo, bem equipada; piso porcelanato claro \
de rejunte fino.").

As fotos mandam; o texto do anúncio ajuda. Fotos de áreas comuns do prédio (piscina, academia, fachada) não contam."""

_RESPOSTA = {
    'type': 'object',
    'properties': {
        'cozinha_integrada': {'type': 'string', 'enum': ['sim', 'nao', 'incerto']},
        'cozinha': {'type': 'string', 'enum': ['bonita', 'simples', 'ruim', 'sem foto']},
        'piso': {'type': 'string', 'enum': ['bom', 'antigo', 'incerto']},
        'melhor_foto': {'type': 'integer'},
        'resumo': {'type': 'string'},
    },
    'required': ['cozinha_integrada', 'cozinha', 'piso', 'melhor_foto', 'resumo'],
    'additionalProperties': False,
}


def _ok(v):
    return v['cozinha_integrada'] == 'sim' and v['cozinha'] == 'bonita' and v['piso'] != 'antigo'


def avaliar_texto(o):
    """Sem a chave do Claude: vale o que o anúncio diz (cozinha integrada e planejada escritas no texto)."""
    t = s.norm((o.get('titulo') or '') + '\n' + (o.get('desc') or ''))
    v = dict(cozinha_integrada='sim' if _INTEGRADA.search(t) else 'incerto',
             cozinha='bonita' if _PLANEJADA.search(t) else 'sem foto',
             piso='antigo' if _PISO_ANTIGO.search(t) else 'incerto', resumo='')
    return dict(v, ok=_ok(v), metodo='texto')


def _foto(url):
    """Baixa uma foto do anúncio para mandar ao Claude; None se não deu (fora do ar, grande demais, formato que a API não lê)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=20) as r:
            dado = r.read(3_700_001)
    except Exception:
        return None
    if len(dado) > 3_700_000:   # a API aceita até 5 MB por foto, já em base64
        return None
    try:   # 900 px bastam para ver a cozinha e custam um terço de uma foto grande
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


def avaliar_ia(o, cliente):
    """O Claude olha as fotos e o texto do anúncio. None se não deu para conferir agora (tenta de novo na próxima rodada)."""
    urls = [u for u in (o.get('fotos') or []) if isinstance(u, str) and u.startswith('http')][:FOTOS_IA]
    with ThreadPoolExecutor(6) as ex:
        baixadas = [(u, f) for u, f in zip(urls, ex.map(_foto, urls)) if f]
    if len(baixadas) < 3:
        print(f"  {o['id']}: só {len(baixadas)} foto(s) baixada(s) de {len(urls)}; fica para a próxima")
        return None
    conteudo = []
    for i, (u, f) in enumerate(baixadas, 1):
        conteudo += [{'type': 'text', 'text': f'Foto {i}'}, f]
    conteudo.append({'type': 'text', 'text': f"Anúncio: {o.get('titulo') or ''}\n\n{(o.get('desc') or '')[:3000]}"})
    r = cliente.beta.messages.create(
        model=MODELO, max_tokens=8000, system=_PEDIDO,
        betas=['server-side-fallback-2026-07-01'], fallbacks='default',   # se recusar, outro modelo responde
        output_config={'format': {'type': 'json_schema', 'schema': _RESPOSTA}},
        messages=[{'role': 'user', 'content': conteudo}])
    if r.stop_reason == 'refusal':
        print(f"  {o['id']}: o Claude não avaliou (recusa)")
        return dict(cozinha_integrada='incerto', cozinha='sem foto', piso='incerto', ok=False, metodo='ia',
                    resumo='(o Claude não avaliou)')
    if r.stop_reason == 'max_tokens':
        return None
    v = json.loads(next(b.text for b in r.content if b.type == 'text'))
    if 1 <= v['melhor_foto'] <= len(baixadas):
        v['foto'] = baixadas[v['melhor_foto'] - 1][0]
    print(f"  {o['id']}: integrada {v['cozinha_integrada']} · cozinha {v['cozinha']} · piso {v['piso']} · "
          f"foto {v['melhor_foto']} ({len(baixadas)} fotos, {r.usage.input_tokens} + {r.usage.output_tokens} tokens)")
    return dict(v, ok=_ok(v), metodo='ia')


def _cliente():
    if not os.environ.get('ANTHROPIC_API_KEY'):
        return None
    import anthropic
    return anthropic.Anthropic()


def _chave(o):
    return f"{round(o['aluguel'])}|{round(o.get('area') or 0)}|{s.norm(o.get('bairro'))}"


def _brl(v):
    return 'R$ ' + f'{round(v):,}'.replace(',', '.')


def _data(d):
    return '/'.join(reversed(d[:10].split('-')[1:])) if d else ''


def _bloco(grupo, agora, aval):
    o = grupo[0]
    total, cond, estimado = custo(o)
    zona = o.get('bairro') or 'Bairro não informado'
    if s.norm(o.get('cidade')).strip() not in ('', 'balneario camboriu'):
        zona += ' · ' + o['cidade']
    linhas = [f"### {zona}{' · ' + o['rua'] if o.get('rua') else ''} — {_brl(total)}/mês com tudo"]
    foto = aval.get('foto') or (o.get('fotos') or [None])[0]
    if foto:
        linhas.append(f"<img src=\"{foto}\" width=\"420\">")
    partes = [f"aluguel {_brl(o['aluguel'])}"]
    if o.get('pacote') and o.get('cond_fonte') == 'pacote':
        partes.append(f"taxas {_brl(cond)} (pacote do anúncio)")
    elif cond:
        partes.append(f"condomínio {'~' if estimado else ''}{_brl(cond)}{' (estimado)' if estimado else ''}")
    elif o.get('cond_fonte') == 'incluso':
        partes.append('condomínio incluso')
    if o.get('iptu'):
        partes.append(f"IPTU {_brl(o['iptu'])}")
    partes.append(f"seguro-fiança {_brl(SEGURO * o['aluguel'])}")
    linhas.append('- ' + ' + '.join(partes))
    mob = {'texto': 'mobiliado', 'marcado': 'mobiliado (marcado no site)', 'semi': 'semimobiliado',
           'sem info': 'planejados (não diz se é mobiliado)'}[o['mobilia']]
    ficha = [f"{o['quartos']} quartos" + (f" ({o['suites']} suíte)" if o.get('suites') else '')]
    if o.get('area'):
        ficha.append(f"{round(o['area'])} m²")
    ficha.append(mob)
    if o.get('escritorio'):
        ficha.append('cita escritório / home office')
    linhas.append('- ' + ' · '.join(ficha))
    if aval.get('resumo'):
        linhas.append(f"- **Pelas fotos:** {aval['resumo']}")
    aprox = '' if o.get('local_exato') else '~'
    linhas.append(f"- praia {aprox}{a_pe(o['praia_m'])} min a pé · Humains {aprox}{de_carro(o['humains_m'])} min de carro"
                  + (f" · prédio {o['predio']}" if o.get('predio') else ''))
    quando = []
    if o.get('baixou_de') and o['baixou_de'] > o['aluguel']:
        quando.append(f"**baixou {_brl(o['baixou_de'] - o['aluguel'])}** (era {_brl(o['baixou_de'])})")
    if o.get('publicado'):
        quando.append('publicado ' + _data(o['publicado']))
    if o.get('atualizado') and o['atualizado'] != o.get('publicado'):
        quando.append('mexido ' + _data(o['atualizado']))
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


def _como_o_do_piata(novos, agora):
    """Fica só com os grupos como o do Piatã. Cada imóvel é conferido uma vez (avaliacoes.json); o que só o texto conferiu
    (sem a chave do Claude) ou foi conferido com outro pedido (VERSAO) é conferido de novo."""
    avals = s.load('avaliacoes.json', {})
    limite = time.strftime('%Y-%m-%d', time.localtime(agora - GUARDA_DIAS * 86400))
    avals = {k: v for k, v in avals.items() if (v.get('quando') or '') >= limite and v.get('versao') == VERSAO}
    cliente = _cliente()
    if not cliente:
        print('aviso: sem ANTHROPIC_API_KEY, confere só pelo texto do anúncio')
    ficam, feitas = [], 0
    for g in novos:
        k = _chave(g[0])
        v = avals.get(k)
        if not v or (cliente and v.get('metodo') == 'texto'):
            if not cliente:
                v = avaliar_texto(g[0])
            elif feitas >= MAX_IA:
                continue   # fica para a próxima rodada
            else:
                feitas += 1
                try:
                    v = avaliar_ia(g[0], cliente)
                except Exception as ex:   # chave errada, sem crédito, fora do ar: tenta na próxima rodada
                    print(f"  {g[0]['id']}: o Claude não respondeu ({type(ex).__name__}: {str(ex)[:200]})")
                    v = None
                if v is None:
                    continue
            v.update(quando=time.strftime('%Y-%m-%d %H:%M'), versao=VERSAO)
            avals[k] = v
        if v['ok']:
            ficam.append(g)
    s.save('avaliacoes.json', avals)
    print(f'aviso: {len(novos)} novo(s) no perfil, {feitas} conferido(s) pelo Claude agora, {len(ficam)} como o do Piatã')
    return ficam, avals


def preparar(anuncios, publicados_ids, pasta):
    """Escreve alerta_titulo.txt e alerta.md em `pasta` quando há imóvel novo que serve; marca os avisados em avisos.json."""
    for n in ('alerta_titulo.txt', 'alerta.md'):
        try:
            os.remove(os.path.join(pasta, n))
        except OSError:
            pass
    agora = time.time()
    no_ar = [anuncios[k] for k in publicados_ids if k in anuncios]
    primeira = {}
    for o in anuncios.values():
        src, v = o.get('_src') or o.get('fonte'), o.get('visto_em') or ''
        if v and (src not in primeira or v < primeira[src]):
            primeira[src] = v
    avisados = s.load('avisos.json', {})
    grupos = {}
    for o in sorted(no_ar, key=lambda o: o.get('fonte') != 'ZAP'):
        if serve(o) and novo(o, primeira, agora):
            grupos.setdefault(_chave(o), []).append(o)
    novos = [g for k, g in grupos.items() if k not in avisados and not any(x['id'] in avisados for x in g)]
    novos, avals = _como_o_do_piata(novos, agora)
    if not novos:
        return 0
    novos.sort(key=lambda g: custo(g[0])[0])
    quando = time.strftime('%Y-%m-%d %H:%M')
    for g in novos:
        avisados[_chave(g[0])] = quando
        for x in g:
            avisados[x['id']] = quando
    s.save('avisos.json', avisados)
    o = novos[0][0]
    if len(novos) == 1:
        titulo = f"Apê como o do Piatã: {o.get('bairro') or 'BC'} · aluguel {_brl(o['aluguel'])} · {_brl(custo(o)[0])} com tudo"
    else:
        titulo = f"{len(novos)} apês como o do Piatã (a partir de {_brl(custo(o)[0])} com tudo)"
    quem = 'o Claude conferiu pelas fotos' if any(avals[_chave(g[0])]['metodo'] == 'ia' for g in novos) else 'o anúncio diz'
    corpo = [f"@{DONO} {'apareceu um apartamento' if len(novos) == 1 else f'apareceram {len(novos)} apartamentos'} "
             f"como o do Piatã ({quem}): cozinha integrada à sala, cozinha bonita e bem montada, piso sem cara de "
             f"antigo. E servem: aluguel de {_brl(ALUGUEL_MIN)} a {_brl(ALUGUEL_MAX)}, 2+ quartos, mobiliado ou semi, até "
             f"{_brl(TOTAL_MAX)} com seguro-fiança, até {PRAIA_A_PE} min a pé da praia e {HUMAINS_CARRO} min de carro da Humains.", '']
    corpo += [_bloco(g, agora, avals[_chave(g[0])]) + '\n' for g in novos[:MAX_POR_AVISO]]
    if len(novos) > MAX_POR_AVISO:
        corpo.append(f"…e mais {len(novos) - MAX_POR_AVISO} no app.")
    corpo.append(f"\n[Abrir o app]({APP}) · Tempos estimados pela distância; ~ = endereço aproximado.")
    with open(os.path.join(pasta, 'alerta_titulo.txt'), 'w') as f:
        f.write(titulo[:250])
    with open(os.path.join(pasta, 'alerta.md'), 'w') as f:
        f.write('\n'.join(corpo))
    return len(novos)
