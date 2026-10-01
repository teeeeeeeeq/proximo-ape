#!/usr/bin/env python3
"""Roda todas as fontes e publica docs/anuncios.json (o que o app lê: só o perfil, com o veredito do Claude) e docs/meta.json."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as s
import alerta

RAIZ = s.RAIZ
CAMPOS = ('id', 'fonte', '_src', 'url', 'titulo', 'desc', 'aluguel', 'pacote', 'cond', 'cond_fonte', 'cond_est', 'cond_base', 'iptu', 'fixo', 'quartos', 'suites', 'area', 'bairro', 'rua',
          'cidade', 'praia_m', 'humains_m', 'local_exato', 'local_aprox', 'local_fonte', 'predio', 'mobilia', 'temporada', 'lazer', 'publicado', 'anunciante', 'fotos', 'visto_em',
          'baixou_de', 'baixou_em', 'atualizado', 'escritorio')


def main():
    meta_antes = s.load('meta.json', {})
    s.atualizar()
    if s.STATUS['erro'] and not s.STATUS['ultima']:
        print('falhou:', s.STATUS['erro'])
        sys.exit(1)
    anuncios = s.load('anuncios.json', {})
    # esquece o que saiu do ar há mais de 14 dias
    limite = time.strftime('%Y-%m-%d', time.localtime(time.time() - 14 * 86400))
    anuncios = {k: o for k, o in anuncios.items() if o.get('no_ar', True) or (o.get('visto_em') or '') >= limite}
    s.save('anuncios.json', anuncios)
    pub = []
    for o in anuncios.values():
        if not alerta.no_perfil(o):   # aluguel, quartos, mobília, distâncias, temporada, animais
            continue
        x = {k: o.get(k) for k in CAMPOS if o.get(k) not in (None, '', [], False)}
        if x.get('desc'):
            x['desc'] = x['desc'][:1000]
        x['fotos'] = (o.get('fotos') or [])[:15]
        pub.append(x)
    meta = s.load('meta.json', {})
    parcial = bool(os.environ.get('SOMENTE', '').strip())
    # "Novos" compara com a última busca COMPLETA; rodadas parciais não mexem nessa referência
    completa_antes = meta_antes.get('completa') or meta_antes.get('ultima')
    info = dict(ultima=s.STATUS['ultima'], anterior=(meta_antes.get('anterior') if parcial else completa_antes),
                fontes=meta.get('fontes', {}), erro=s.STATUS['erro'])
    meta['ultima'] = s.STATUS['ultima']
    meta['anterior'] = info['anterior']
    if not parcial:
        meta['completa'] = s.STATUS['ultima']
    s.save('meta.json', meta)
    os.makedirs(os.path.join(RAIZ, 'docs'), exist_ok=True)
    pub.sort(key=lambda x: x.get('visto_em') or '', reverse=True)
    try:   # o Claude confere as fotos (cada anúncio ganha 'ia') e sai o aviso por e-mail; uma falha aqui não derruba a busca
        n = alerta.processar(anuncios, pub, os.path.join(RAIZ, 'dados'))
        print(f'aviso: {n} apartamento(s) como o do Piatã' if n else 'aviso: nada novo como o do Piatã')
    except Exception as ex:
        import traceback
        traceback.print_exc()
        print('aviso falhou:', ex)
    json.dump(pub, open(os.path.join(RAIZ, 'docs', 'anuncios.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
    json.dump(info, open(os.path.join(RAIZ, 'docs', 'meta.json'), 'w'), ensure_ascii=False)
    print(f"{len(pub)} anúncios publicados; fontes: " + '; '.join(f"{n}: {i.get('n', 0)}{' ERRO ' + i['erro'] if i.get('erro') else ''}" for n, i in info['fontes'].items()))


if __name__ == '__main__':
    main()
