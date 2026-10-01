"""Aviso por e-mail de apartamento novo que serve (o GitHub manda o e-mail de uma issue que menciona o dono do repositório).

Serve: 2+ quartos, mobiliado ou semimobiliado (os quartos podem estar vazios), piso de porcelanato (ou o anúncio não diz
o piso), até R$ 6.000 com tudo (aluguel + condomínio, estimado se o anúncio não diz + IPTU + seguro-fiança de 10% do
aluguel), até 10 min a pé da praia e até 10 min de carro da Humains. Recusa de animais já não chega aqui (coletar.py).

Novo: entrou no ar há pouco (o coletor viu pela primeira vez, fora da primeira leitura de cada fonte), foi publicado
nos últimos dias, ou o app viu baixar de preço há pouco. Cada imóvel é avisado uma vez só (dados/alertas.json), e o mesmo
imóvel em sites diferentes (mesmo aluguel, área e bairro) vira um aviso só, com os links de todos.
"""
import json, os, re, time

import server as s

DONO = 'teeeeeeeeq'
APP = 'https://teeeeeeeeq.github.io/proximo-ape/'
TOTAL_MAX = 6000
SEGURO = 0.10        # seguro-fiança: 10% do aluguel
PRAIA_A_PE = 10      # minutos
HUMAINS_CARRO = 10   # minutos
NOVO_DIAS = 3        # entrou no ar / baixou há até 3 dias
PUBLICADO_DIAS = 7   # publicado no site há até 7 dias
MAX_POR_AVISO = 15

_OUTRO_PISO = re.compile(r'\b(laminado|ceramica|taco|tacos|vinilico|ardosia|carpete|assoalho|piso de madeira|madeira no piso)\b')


def a_pe(m):
    return None if m is None else max(1, round(m * 1.25 / 75))   # o mesmo do app antigo: ~4,5 km/h, ruas +25%


def de_carro(m):
    if m is None:
        return None
    r = m * 1.35
    return max(1, round(1 + min(r, 2000) / 333 + max(r - 2000, 0) / 583))   # o mesmo do app


def piso(texto):
    t = s.norm(texto)
    if 'porcelanato' in t:
        return 'porcelanato'
    return None if _OUTRO_PISO.search(t) else 'não informado'


def custo(o):
    cond = o['cond'] if o.get('cond') is not None else (o.get('cond_est') or 0)
    base = max(o['aluguel'] + cond + (o.get('iptu') or 0), o.get('pacote') or 0)
    return round(base + SEGURO * o['aluguel']), cond, o.get('cond') is None and bool(o.get('cond_est'))


def serve(o):
    if not o.get('aluguel') or (o.get('quartos') or 0) < 2 or o.get('temporada'):
        return False
    if o.get('mobilia') not in ('texto', 'marcado', 'semi'):
        return False
    if piso((o.get('titulo') or '') + '\n' + (o.get('desc') or '')) is None:
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


def _chave(o):
    return f"{round(o['aluguel'])}|{round(o.get('area') or 0)}|{s.norm(o.get('bairro'))}"


def _brl(v):
    return 'R$ ' + f'{round(v):,}'.replace(',', '.')


def _data(d):
    return '/'.join(reversed(d[:10].split('-')[1:])) if d else ''


def _bloco(grupo, agora):
    o = grupo[0]
    total, cond, estimado = custo(o)
    zona = o.get('bairro') or 'Bairro não informado'
    if s.norm(o.get('cidade')).strip() not in ('', 'balneario camboriu'):
        zona += ' · ' + o['cidade']
    linhas = [f"### {zona}{' · ' + o['rua'] if o.get('rua') else ''} — {_brl(total)}/mês com tudo"]
    if o.get('fotos'):
        linhas.append(f"<img src=\"{o['fotos'][0]}\" width=\"420\">")
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
    mob = {'texto': 'mobiliado', 'marcado': 'mobiliado (marcado no site)', 'semi': 'semimobiliado'}[o['mobilia']]
    ficha = [f"{o['quartos']} quartos" + (f" ({o['suites']} suíte)" if o.get('suites') else '')]
    if o.get('area'):
        ficha.append(f"{round(o['area'])} m²")
    ficha += [mob, 'piso: ' + piso((o.get('titulo') or '') + '\n' + (o.get('desc') or ''))]
    if o.get('escritorio'):
        ficha.append('cita escritório / home office')
    linhas.append('- ' + ' · '.join(ficha))
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


def preparar(anuncios, publicados_ids, pasta):
    """Escreve alerta_titulo.txt e alerta.md em `pasta` quando há imóvel novo que serve; marca os avisados em alertas.json."""
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
    avisados = s.load('alertas.json', {})
    grupos = {}
    for o in sorted(no_ar, key=lambda o: o.get('fonte') != 'ZAP'):
        if serve(o) and novo(o, primeira, agora):
            grupos.setdefault(_chave(o), []).append(o)
    novos = [g for k, g in grupos.items() if k not in avisados and not any(x['id'] in avisados for x in g)]
    if not novos:
        return 0
    novos.sort(key=lambda g: custo(g[0])[0])
    quando = time.strftime('%Y-%m-%d %H:%M')
    for g in novos:
        avisados[_chave(g[0])] = quando
        for x in g:
            avisados[x['id']] = quando
    s.save('alertas.json', avisados)
    o = novos[0][0]
    if len(novos) == 1:
        titulo = f"Apê novo: {o.get('bairro') or 'BC'} · {o['quartos']} quartos · {_brl(custo(o)[0])} com tudo"
    else:
        titulo = f"{len(novos)} apês novos que servem (a partir de {_brl(custo(o)[0])} com tudo)"
    corpo = [f"@{DONO} {'apareceu um apartamento' if len(novos) == 1 else f'apareceram {len(novos)} apartamentos'} "
             f"que servem: 2+ quartos, mobiliado ou semi, porcelanato (ou piso não informado), até {_brl(TOTAL_MAX)} com seguro-fiança, "
             f"até {PRAIA_A_PE} min a pé da praia e {HUMAINS_CARRO} min de carro da Humains.", '']
    corpo += [_bloco(g, agora) + '\n' for g in novos[:MAX_POR_AVISO]]
    if len(novos) > MAX_POR_AVISO:
        corpo.append(f"…e mais {len(novos) - MAX_POR_AVISO} no app.")
    corpo.append(f"\n[Abrir o app]({APP}) · Tempos estimados pela distância; ~ = endereço aproximado.")
    with open(os.path.join(pasta, 'alerta_titulo.txt'), 'w') as f:
        f.write(titulo[:250])
    with open(os.path.join(pasta, 'alerta.md'), 'w') as f:
        f.write('\n'.join(corpo))
    return len(novos)
