from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db.models import Prefetch, Q

from financeiro.models import Fatura, NaturezaEvento, ProgramacaoPagamento, RateioCategoria, Situacao, Titulo


ZERO = Decimal("0.00")
NATUREZAS_DIRETAS = {
    NaturezaEvento.DESPESA,
    NaturezaEvento.RECEITA,
    NaturezaEvento.ESTORNO,
}
STATUS_PROGRAMACAO_ATIVA = {
    ProgramacaoPagamento.Status.ATIVA,
    ProgramacaoPagamento.Status.PARCIAL,
}


def _moeda(valor: Decimal) -> str:
    return f"{valor.quantize(Decimal('0.01')):.2f}"


def _categorias_titulo(titulo: Titulo) -> list[str]:
    if titulo.fatura_id or not titulo.evento_id:
        return []
    return sorted(
        {
            rateio.categoria.rotulo_com_pai
            for rateio in titulo.evento.rateios.all()
            if rateio.categoria.usuario_id == titulo.usuario_id
        },
        key=str.casefold,
    )


def _dados_titulo(titulo: Titulo) -> dict:
    if titulo.fatura_id:
        return {
            "tipo": "DESPESA",
            "natureza": NaturezaEvento.PAGAMENTO_FATURA,
            "origem": "FATURA",
            "categorias": [],
            "fatura": {
                "cartao": titulo.fatura.cartao.nome,
                "ciclo": titulo.fatura.ciclo,
            },
        }
    return {
        "tipo": "RECEITA" if titulo.tipo == Titulo.Tipo.RECEBER else "DESPESA",
        "natureza": titulo.evento.natureza,
        "origem": "CONTA",
        "categorias": _categorias_titulo(titulo),
        "fatura": None,
    }


def _alocacoes_titulo(titulo: Titulo) -> list[dict]:
    saldo_restante = titulo.saldo_aberto
    alocacoes = []
    for programacao in titulo.programacoes.all():
        if programacao.status not in STATUS_PROGRAMACAO_ATIVA or saldo_restante <= ZERO:
            continue
        restante_programado = programacao.valor_programado - programacao.valor_realizado
        if restante_programado <= ZERO:
            continue
        valor = min(restante_programado, saldo_restante)
        alocacoes.append({
            "programacao_id": programacao.pk,
            "conta": programacao.conta,
            "data": programacao.data_programada,
            "valor": valor,
        })
        saldo_restante -= valor

    if saldo_restante > ZERO:
        alocacoes.append({
            "programacao_id": None,
            "conta": titulo.conta,
            "data": titulo.vencimento,
            "valor": saldo_restante,
        })
    return alocacoes


def consultar_previsoes(usuario, inicio: date, fim: date) -> dict:
    elegibilidade = Q(
        fatura__usuario=usuario,
        fatura__cartao__usuario=usuario,
        fatura__status__in=[Fatura.Status.FECHADA, Fatura.Status.PARCIAL],
    ) | Q(
        fatura__isnull=True,
        evento__usuario=usuario,
        evento__situacao=Situacao.PREVISTO,
        evento__natureza__in=NATUREZAS_DIRETAS,
    )
    titulos = (
        Titulo.objects.filter(
            elegibilidade,
            usuario=usuario,
            ativo=True,
            status=Titulo.Status.ABERTO,
            saldo_aberto__gt=ZERO,
        )
        .select_related("conta", "evento", "fatura__cartao")
        .prefetch_related(
            "programacoes__conta",
            Prefetch(
                "evento__rateios",
                queryset=RateioCategoria.objects.select_related("categoria__categoria_pai"),
            ),
        )
        .order_by("vencimento", "id")
    )

    grupos = {}
    totais = {"receitas": ZERO, "despesas": ZERO}
    for titulo in titulos:
        dados_titulo = _dados_titulo(titulo)
        for alocacao in _alocacoes_titulo(titulo):
            if not inicio <= alocacao["data"] <= fim:
                continue
            conta = alocacao["conta"]
            if conta and conta.usuario_id != usuario.id:
                conta = None
            conta_id = conta.pk if conta else None
            grupo = grupos.setdefault(
                conta_id,
                {
                    "conta": {
                        "id": conta_id,
                        "nome": conta.nome if conta else "Sem conta definida",
                    },
                    "receitas": ZERO,
                    "despesas": ZERO,
                    "itens": [],
                },
            )
            chave_total = "receitas" if dados_titulo["tipo"] == "RECEITA" else "despesas"
            grupo[chave_total] += alocacao["valor"]
            totais[chave_total] += alocacao["valor"]
            grupo["itens"].append({
                "tituloId": titulo.pk,
                "programacaoId": alocacao["programacao_id"],
                "tipo": dados_titulo["tipo"],
                "natureza": dados_titulo["natureza"],
                "origem": dados_titulo["origem"],
                "descricao": titulo.descricao,
                "data": alocacao["data"].isoformat(),
                "categorias": dados_titulo["categorias"],
                "valor": _moeda(alocacao["valor"]),
                "fatura": dados_titulo["fatura"],
            })

    contas = []
    for grupo in grupos.values():
        grupo["itens"].sort(key=lambda item: (item["data"], item["tituloId"], item["programacaoId"] or 0))
        receitas = grupo.pop("receitas")
        despesas = grupo.pop("despesas")
        grupo["totais"] = {
            "receitas": _moeda(receitas),
            "despesas": _moeda(despesas),
            "saldoPrevisto": _moeda(receitas - despesas),
        }
        contas.append(grupo)
    contas.sort(key=lambda grupo: (grupo["conta"]["id"] is None, grupo["conta"]["nome"].casefold()))

    return {
        "ok": True,
        "periodo": {
            "dataConsulta": inicio.isoformat(),
            "dataFinal": fim.isoformat(),
            "diasConsulta": (fim - inicio).days,
        },
        "totais": {
            "receitas": _moeda(totais["receitas"]),
            "despesas": _moeda(totais["despesas"]),
            "saldoPrevisto": _moeda(totais["receitas"] - totais["despesas"]),
        },
        "contas": contas,
    }
