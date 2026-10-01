#!/usr/bin/env python3
"""Teste do aviso: o Claude confere os N anúncios mais recentes que servem ao aviso, sem olhar se já foram conferidos ou
avisados, e escreve o que viu no resumo da rodada (Actions → Testar fotos → a rodada). Não avisa nem guarda nada."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as s
import alerta


def main():
    quantos = int(os.environ.get('QUANTOS') or 10)
    cliente = alerta._cliente()
    if not cliente:
        sys.exit('sem ANTHROPIC_API_KEY nos secrets do repositório')
    anuncios = s.load('anuncios.json', {})
    grupos = {}
    for o in sorted(anuncios.values(), key=lambda o: o.get('visto_em') or '', reverse=True):
        if o.get('no_ar') is False or o.get('ativo') is False or not s.na_regiao(o.get('cidade'), o.get('bairro')):
            continue
        if s.nao_aceita_animais((o.get('titulo') or '') + '\n' + (o.get('desc') or '')) or not alerta.serve(o):
            continue
        grupos.setdefault(alerta._chave(o), []).append(o)
    linhas = [f'# Teste: o Claude conferiu os {quantos} anúncios mais recentes que servem ao aviso', '',
              'Aviso só sai com ✅: cozinha integrada "sim", cozinha "bonita" e piso que não seja "antigo".', '']
    bons = 0
    for g in list(grupos.values())[:quantos]:
        o = g[0]
        try:
            v = alerta.avaliar_ia(o, cliente)
        except Exception as ex:
            v, erro = None, f'{type(ex).__name__}: {str(ex)[:200]}'
        else:
            erro = 'não deu para baixar as fotos ou a resposta veio cortada'
        total = alerta.custo(o)[0]
        linhas.append(f"## {'✅' if v and v['ok'] else '❌'} {o.get('bairro') or 'Bairro não informado'}"
                      f"{' · ' + o['rua'] if o.get('rua') else ''} — aluguel {alerta._brl(o['aluguel'])} · {alerta._brl(total)} com tudo")
        if v is None:
            linhas.append(f'- não conferido: {erro}')
        else:
            bons += v['ok']
            foto = v.get('foto') or (o.get('fotos') or [None])[0]
            if foto:
                linhas.append(f'<img src="{foto}" width="360">')
                linhas.append('')
            linhas.append(f"- cozinha integrada: **{v['cozinha_integrada']}** · cozinha: **{v['cozinha']}** · "
                          f"piso: **{v['piso']}** · foto escolhida: {v.get('melhor_foto', 0) or 'nenhuma mostra a cozinha'}")
            if v.get('resumo'):
                linhas.append(f"- {v['resumo']}")
        linhas.append(f"- entrou {(o.get('visto_em') or '')[:16]} · {o.get('quartos')} quartos"
                      f"{' · ' + str(round(o['area'])) + ' m²' if o.get('area') else ''} · {o.get('fonte')} · [abrir]({o['url']})")
        linhas.append('')
    linhas.insert(3, f'**{bons} de {min(quantos, len(grupos))} passariam.**\n')
    texto = '\n'.join(linhas)
    print(texto)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write(texto)


if __name__ == '__main__':
    main()
