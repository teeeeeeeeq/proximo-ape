#!/usr/bin/env python3
"""Roda todas as fontes e publica docs/anuncios.json (o que o app lê) e docs/meta.json."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as s

RAIZ = s.RAIZ
CAMPOS = ('id', 'fonte', '_src', 'url', 'titulo', 'desc', 'aluguel', 'cond', 'iptu', 'fixo', 'quartos', 'suites', 'area', 'bairro', 'rua',
          'cidade', 'praia_m', 'humains_m', 'local_exato', 'local_aprox', 'mobilia', 'temporada', 'publicado', 'anunciante', 'fotos', 'visto_em')


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
        if o.get('no_ar') is False or o.get('ativo') is False or not o.get('aluguel') or o['aluguel'] < 2000 or o['aluguel'] > 9000:
            continue
        if o.get('quartos') and o['quartos'] < 2:
            continue
        x = {k: o.get(k) for k in CAMPOS if o.get(k) not in (None, '', [], False)}
        if x.get('desc'):
            x['desc'] = x['desc'][:1000]
        x['fotos'] = (o.get('fotos') or [])[:15]
        pub.append(x)
    meta = s.load('meta.json', {})
    info = dict(ultima=s.STATUS['ultima'], anterior=meta_antes.get('ultima'), fontes=meta.get('fontes', {}), erro=s.STATUS['erro'])
    meta['ultima'] = s.STATUS['ultima']
    s.save('meta.json', meta)
    os.makedirs(os.path.join(RAIZ, 'docs'), exist_ok=True)
    pub.sort(key=lambda x: x.get('visto_em') or '', reverse=True)
    json.dump(pub, open(os.path.join(RAIZ, 'docs', 'anuncios.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
    json.dump(info, open(os.path.join(RAIZ, 'docs', 'meta.json'), 'w'), ensure_ascii=False)
    print(f"{len(pub)} anúncios publicados; fontes: " + '; '.join(f"{n}: {i.get('n', 0)}{' ERRO ' + i['erro'] if i.get('erro') else ''}" for n, i in info['fontes'].items()))


if __name__ == '__main__':
    main()
