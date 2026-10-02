#!/usr/bin/env python3
"""Ajuda a rotina do Claude Code (que roda pelo plano do dono, sem a chave da API) a conferir as fotos da fila.

  python3 coletor/rotina.py fila                    baixa a fila (branch fila-claude) e os vereditos já feitos (branch
                                                    avaliacoes) e lista o que falta: o pedido, as folhas de fotos e o anúncio
  python3 coletor/rotina.py gravar CHAVE <<'FIM'    confere o formato da resposta (o JSON, nas linhas seguintes) e
  {...}                                             guarda o veredito
  FIM
  python3 coletor/rotina.py endereco ID <<'FIM'     guarda o endereço pesquisado de um anúncio sem endereço (o JSON,
  {...}                                             nas linhas seguintes)
  FIM
  python3 coletor/rotina.py enviar                  commit e push da branch avaliacoes

A busca seguinte (GitHub Actions) junta os vereditos ao app e manda o aviso por e-mail (alerta.py).
"""
import json, os, subprocess, sys, tempfile, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(tempfile.gettempdir(), 'rotina-proximo-ape')
FILA, AVAL = os.path.join(BASE, 'fila'), os.path.join(BASE, 'avaliacoes')
POR_RODADA = 30
CIDADES = ('Balneário Camboriú', 'Camboriú', 'Itajaí', '')
os.environ['TZ'] = 'America/Sao_Paulo'
time.tzset()


def git(*args, cwd=None):
    r = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"git {' '.join(args[:2])} falhou: {r.stderr.strip()[:300]}")
    return r.stdout


def baixar(branch, pasta):
    if os.path.isdir(os.path.join(pasta, '.git')):
        git('fetch', '-q', '--depth', '1', 'origin', branch, cwd=pasta)
        git('reset', '-q', '--hard', 'FETCH_HEAD', cwd=pasta)
    else:
        os.makedirs(BASE, exist_ok=True)
        git('clone', '-q', '--depth', '1', '--branch', branch, git('remote', 'get-url', 'origin', cwd=REPO).strip(), pasta)


def _fila():
    return json.load(open(os.path.join(FILA, 'fila.json')))


def _feitas():
    return json.load(open(os.path.join(AVAL, 'avaliacoes.json')))


def _enderecos():
    try:
        return json.load(open(os.path.join(AVAL, 'enderecos.json')))
    except FileNotFoundError:
        return {}


def fila():
    baixar('fila-claude', FILA)
    baixar('avaliacoes', AVAL)
    f, feitas = _fila(), _feitas()
    faltam = [i for i in f['itens'] if (feitas.get(i['chave']) or {}).get('versao') != f['versao']][:POR_RODADA]
    end = [e for e in f.get('enderecos') or [] if e['id'] not in _enderecos()]
    if not faltam and not end:
        print(f"Fila montada em {f['gerada']}: {len(f['itens'])} anúncio(s); 0 para conferir agora.")
        return
    print(f"Fila montada em {f['gerada']}: {len(f['itens'])} anúncio(s); {len(faltam) or 'nenhum'} com fotos para conferir agora"
          + (f" e {len(end)} sem endereço para pesquisar (no fim)." if end else '.'))
    if faltam:
        _fotos(f, faltam)
    if end:
        print('\n\nENDEREÇOS (depois das fotos):\n' + f['pedido_endereco'])
        print("\nPara cada anúncio abaixo, pesquise e grave na hora (também quando não achar, com tudo vazio):\n"
              "  python3 coletor/rotina.py endereco '<id>' <<'FIM'\n"
              '  {"predio": "...", "rua": "...", "bairro": "...", "cidade": "...", "fonte": "..."}\n  FIM\n')
        for n, e in enumerate(end, 1):
            print(f"=== endereço {n}. id: {e['id']}  ({e.get('url') or ''})")
            print(e['anuncio'].strip() + '\n')


def _fotos(f, faltam):
    campos = ', '.join(f'"{c}": ' + ('|'.join(f'"{x}"' for x in p['enum']) if 'enum' in p else 'N' if p['type'] == 'integer' else '"..."')
                       for c, p in f['resposta']['properties'].items())
    print('\nO PEDIDO (siga à risca):\n' + f['pedido'])
    print(f"\nPara cada anúncio: abra as folhas com a ferramenta Read (as fotos têm o número no canto) e leia o anúncio; depois:\n"
          f"  python3 coletor/rotina.py gravar '<chave>' <<'FIM'\n  {{{campos}}}\n  FIM\n")
    for n, i in enumerate(faltam, 1):
        print(f"=== {n}. chave: {i['chave']}  ({len(i['fotos'])} fotos)")
        print('folhas: ' + ' '.join(os.path.join(FILA, x) for x in i['folhas']))
        print(i['anuncio'].strip() + '\n')


def gravar(chave, texto):
    f = _fila()
    item = next((i for i in f['itens'] if i['chave'] == chave), None)
    if not item:
        sys.exit(f'a chave {chave!r} não está na fila')
    try:
        v = json.loads(texto)
    except ValueError as ex:
        sys.exit(f'JSON inválido: {ex}')
    for c, p in f['resposta']['properties'].items():
        if c not in v:
            sys.exit(f'falta o campo {c}')
        if 'enum' in p and v[c] not in p['enum']:
            sys.exit(f"{c} tem de ser um destes: {', '.join(p['enum'])}")
        if p['type'] == 'integer' and not isinstance(v[c], int):
            sys.exit(f'{c} tem de ser um número inteiro')
    v = {c: v[c] for c in f['resposta']['properties']}
    if 1 <= v['melhor_foto'] <= len(item['fotos']):
        v['foto'] = item['fotos'][v['melhor_foto'] - 1]
    v.update(id=item['id'], versao=f['versao'], quando=time.strftime('%Y-%m-%d %H:%M'))
    feitas = _feitas()
    feitas[chave] = v
    with open(os.path.join(AVAL, 'avaliacoes.json'), 'w') as arq:
        json.dump(feitas, arq, ensure_ascii=False, indent=0)
    print(f"ok ({len(feitas)} vereditos guardados)")


def endereco(id_, texto):
    if not any(e['id'] == id_ for e in _fila().get('enderecos') or []):
        sys.exit(f'o id {id_!r} não está na lista de endereços')
    try:
        v = json.loads(texto)
    except ValueError as ex:
        sys.exit(f'JSON inválido: {ex}')
    campos = ('predio', 'rua', 'bairro', 'cidade', 'fonte')
    if not isinstance(v, dict) or any(not isinstance(v.get(c), str) for c in campos):
        sys.exit('responda com os cinco campos, todos texto: ' + ', '.join(campos))
    v = {c: v[c].strip() for c in campos}
    if v['cidade'] not in CIDADES:
        sys.exit('cidade tem de ser "Balneário Camboriú", "Camboriú", "Itajaí" ou ""')
    if v['rua'] and not (v['cidade'] and v['fonte'].startswith('http')):
        sys.exit('com a rua, diga também a cidade e o link (fonte) que confirma o endereço')
    v['quando'] = time.strftime('%Y-%m-%d %H:%M')
    feitas = _enderecos()
    feitas[id_] = v
    with open(os.path.join(AVAL, 'enderecos.json'), 'w') as arq:
        json.dump(feitas, arq, ensure_ascii=False, indent=0)
    print(f"ok ({sum(1 for x in feitas.values() if x.get('rua'))} endereços achados de {len(feitas)} pesquisados)")


def enviar():
    git('add', 'avaliacoes.json', *(['enderecos.json'] if os.path.exists(os.path.join(AVAL, 'enderecos.json')) else []), cwd=AVAL)
    if not git('status', '--porcelain', cwd=AVAL).strip():
        print('nada novo para enviar')
        return
    git('-c', 'user.name=Claude', '-c', 'user.email=noreply@anthropic.com', 'commit', '-qm',
        'vereditos ' + time.strftime('%d/%m %H:%M'), cwd=AVAL)
    git('push', '-q', 'origin', 'HEAD:avaliacoes', cwd=AVAL)
    print('enviado')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'fila':
        fila()
    elif cmd == 'gravar' and len(sys.argv) in (3, 4):
        gravar(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else sys.stdin.read())
    elif cmd == 'endereco' and len(sys.argv) in (3, 4):
        endereco(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else sys.stdin.read())
    elif cmd == 'enviar':
        enviar()
    else:
        sys.exit(__doc__)
