#!/usr/bin/env python3
"""Teste: o Claude confere os N anúncios do perfil que entraram por último, sem olhar se já foram conferidos, e escreve
o que viu no resumo da rodada (Actions → Testar fotos → a rodada). Não avisa nem guarda nada."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as s
import alerta

ROTULO = {'piata': '✅ Como o do Piatã', 'perguntar': '❔ Vale perguntar', 'nao': '❌ Não serve'}


def main():
    quantos = int(os.environ.get('QUANTOS') or 10)
    cliente = alerta._cliente()
    if not cliente:
        sys.exit('sem ANTHROPIC_API_KEY nos secrets do repositório (Settings → Secrets and variables → Actions)')
    grupos = {}
    for o in sorted(s.load('anuncios.json', {}).values(), key=lambda o: o.get('visto_em') or '', reverse=True):
        if alerta.no_perfil(o):
            grupos.setdefault(alerta._chave(o), []).append(o)
    linhas, conta = [], {}
    for g in list(grupos.values())[:quantos]:
        o = g[0]
        try:
            v, erro = alerta.avaliar_ia(o, cliente), 'nenhuma foto baixou ou a resposta veio cortada'
        except Exception as ex:
            v, erro = None, f'{type(ex).__name__}: {str(ex)[:200]}'
        conta[v['v'] if v else 'erro'] = conta.get(v['v'] if v else 'erro', 0) + 1
        linhas.append(f"## {ROTULO[v['v']] if v else '⚠️ Não conferido'} — {o.get('bairro') or 'Bairro não informado'}"
                      f"{' · ' + o['rua'] if o.get('rua') else ''} · aluguel {alerta._brl(o['aluguel'])}")
        if v is None:
            linhas.append(f'- {erro}')
        else:
            foto = v.get('foto') or (o.get('fotos') or [None])[0]
            if foto:
                linhas += [f'<img src="{foto}" width="360">', '']
            linhas.append(f"- cozinha integrada **{v['cozinha_integrada']}** · cozinha **{v['cozinha']}** · piso **{v['piso']}** · "
                          f"chance **{v['chance']}** · foto escolhida: {v.get('melhor_foto') or 'nenhuma mostra a cozinha'}")
            linhas.append(f"- {v['resumo']}")
        linhas.append(f"- entrou {(o.get('visto_em') or '')[:16]} · {o.get('quartos') or '?'} quartos"
                      f"{' · ' + str(round(o['area'])) + ' m²' if o.get('area') else ''} · {o.get('fonte')} · [abrir]({o['url']})")
        linhas.append('')
    topo = [f'# Teste: o Claude conferiu os {len(linhas) and min(quantos, len(grupos))} anúncios do perfil que entraram por último', '',
            ' · '.join(f"{ROTULO.get(k, '⚠️ Não conferido')}: {n}" for k, n in sorted(conta.items())), '']
    texto = '\n'.join(topo + linhas)
    print(texto)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write(texto)


if __name__ == '__main__':
    main()
