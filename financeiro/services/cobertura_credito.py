from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Prefetch, Q

from financeiro.models import (
    Categoria,
    CompraCartao,
    Conta,
    EventoFinanceiro,
    Liquidacao,
    MovimentoConta,
    NaturezaEvento,
    ParcelaCartao,
    ProgramacaoPagamento,
    Situacao,
    Titulo,
)


ZERO = Decimal("0.00")
HUNDRED = Decimal("100")
NATUREZAS = {"all", "personal", "business"}


def _categoria_compativel(categoria: Categoria | None, natureza: str) -> bool:
    if natureza == "all":
        return True
    if not categoria:
        return False
    if natureza == "personal":
        return categoria.natureza in {Categoria.Natureza.PESSOAL, Categoria.Natureza.AMBOS}
    return categoria.natureza in {Categoria.Natureza.EMPRESARIAL, Categoria.Natureza.AMBOS}


def _proporcao_rateios(evento: EventoFinanceiro | None, natureza: str) -> Decimal:
    if natureza == "all":
        return Decimal("1")
    if not evento:
        return ZERO
    rateios = list(evento.rateios.all())
    total = sum((abs(item.valor) for item in rateios), ZERO)
    if total <= ZERO:
        return ZERO
    compativel = sum(
        (abs(item.valor) for item in rateios if _categoria_compativel(item.categoria, natureza)),
        ZERO,
    )
    return min(compativel / total, Decimal("1"))


def _natureza_parcela(parcela: ParcelaCartao) -> str:
    return parcela.natureza_exibicao


def _valor_assinado_cartao(valor: Decimal, natureza_evento: str) -> Decimal:
    if natureza_evento == NaturezaEvento.DESPESA:
        return abs(valor)
    if natureza_evento in {NaturezaEvento.ESTORNO, NaturezaEvento.RECEITA}:
        return -abs(valor)
    return ZERO


def _proporcao_parcelas(parcelas: list[ParcelaCartao], natureza: str) -> Decimal:
    if natureza == "all":
        return Decimal("1")
    elegiveis = [
        parcela
        for parcela in parcelas
        if parcela.situacao != Situacao.CANCELADO
        and _natureza_parcela(parcela) not in {NaturezaEvento.PAGAMENTO_FATURA, NaturezaEvento.TRANSFERENCIA}
    ]
    total = sum((abs(parcela.valor) for parcela in elegiveis), ZERO)
    if total <= ZERO:
        return ZERO
    compativel = sum(
        (
            abs(parcela.valor)
            for parcela in elegiveis
            if _categoria_compativel(parcela.categoria, natureza)
        ),
        ZERO,
    )
    return min(compativel / total, Decimal("1"))


def _proporcao_titulo(titulo: Titulo, natureza: str) -> Decimal:
    if natureza == "all":
        return Decimal("1")
    if titulo.evento_id:
        return _proporcao_rateios(titulo.evento, natureza)
    if titulo.fatura_id:
        return _proporcao_parcelas(list(titulo.fatura.parcelas.all()), natureza)
    return ZERO


def _proporcao_movimento(
    movimento: MovimentoConta,
    natureza: str,
    titulos_por_movimento: dict[int, list[Titulo]],
) -> Decimal:
    if natureza == "all":
        return Decimal("1")
    proporcao_evento = _proporcao_rateios(movimento.evento, natureza)
    if proporcao_evento > ZERO:
        return proporcao_evento
    titulos = titulos_por_movimento.get(movimento.pk, [])
    if not titulos:
        return ZERO
    proporcoes = [_proporcao_titulo(titulo, natureza) for titulo in titulos]
    return max(proporcoes, default=ZERO)


def _percentual_cobertura(recursos: Decimal, pressao: Decimal) -> Decimal | None:
    if pressao <= ZERO:
        return None
    return (recursos / pressao * HUNDRED).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _estado_trajetoria(primeira_data: date | None, saldo_final: Decimal) -> dict:
    if primeira_data is None:
        return {"codigo": "COBERTO", "rotulo": "Coberto"}
    if saldo_final < ZERO:
        return {"codigo": "DEPENDENCIA_FECHAMENTO", "rotulo": "Dependência no fechamento"}
    return {"codigo": "DEPENDENCIA_TEMPORARIA", "rotulo": "Dependência temporária"}


def _item(
    *,
    data: date,
    descricao: str,
    origem: str,
    origem_nome: str,
    situacao: str,
    valor: Decimal,
    estimado: bool = False,
    observacao: str = "",
) -> dict:
    return {
        "data": data,
        "descricao": descricao,
        "origem": origem,
        "origem_nome": origem_nome,
        "situacao": situacao,
        "valor": valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "estimado": estimado,
        "observacao": observacao,
    }


def _movimentos_realizados(
    usuario,
    contas: list[Conta],
    inicio: date,
    fim_exclusivo: date,
    natureza: str,
) -> tuple[list[dict], list[dict]]:
    contas_ids = [conta.pk for conta in contas]
    if not contas_ids:
        return [], []
    movimentos = list(
        MovimentoConta.objects.filter(
            conta_id__in=contas_ids,
            conta__usuario=usuario,
            evento__situacao=Situacao.REALIZADO,
            evento__data__gte=inicio,
            evento__data__lt=fim_exclusivo,
        )
        .exclude(evento__natureza=NaturezaEvento.TRANSFERENCIA)
        .select_related("conta", "evento")
        .prefetch_related("evento__rateios__categoria")
        .order_by("evento__data", "id")
    )
    liquidacoes = list(
        Liquidacao.objects.filter(
            movimento_conta_id__in=[movimento.pk for movimento in movimentos],
            status=Liquidacao.Status.ATIVA,
        )
        .select_related("titulo__evento", "titulo__fatura")
        .prefetch_related(
            "titulo__evento__rateios__categoria",
            "titulo__fatura__parcelas__categoria",
            "titulo__fatura__parcelas__compra__evento",
        )
    )
    titulos_por_movimento: dict[int, list[Titulo]] = defaultdict(list)
    for liquidacao in liquidacoes:
        if liquidacao.movimento_conta_id:
            titulos_por_movimento[liquidacao.movimento_conta_id].append(liquidacao.titulo)

    receitas = []
    saidas = []
    for movimento in movimentos:
        proporcao = _proporcao_movimento(movimento, natureza, titulos_por_movimento)
        if proporcao <= ZERO:
            continue
        valor = (abs(movimento.valor) * proporcao).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        evento = movimento.evento
        if evento.natureza == NaturezaEvento.RECEITA and movimento.valor > ZERO:
            receitas.append(
                _item(
                    data=evento.data,
                    descricao=evento.descricao,
                    origem="CONTA",
                    origem_nome=movimento.conta.nome,
                    situacao="Realizado",
                    valor=valor,
                )
            )
        elif evento.natureza == NaturezaEvento.ESTORNO and movimento.valor > ZERO:
            saidas.append(
                _item(
                    data=evento.data,
                    descricao=evento.descricao,
                    origem="CONTA",
                    origem_nome=movimento.conta.nome,
                    situacao="Realizado",
                    valor=-valor,
                    observacao="Estorno que reduz as saídas do período.",
                )
            )
        elif movimento.valor < ZERO:
            saidas.append(
                _item(
                    data=evento.data,
                    descricao=evento.descricao,
                    origem="CONTA",
                    origem_nome=movimento.conta.nome,
                    situacao="Realizado",
                    valor=valor,
                )
            )
    return receitas, saidas


def _titulos_previstos(
    usuario,
    contas: list[Conta],
    cartoes_ids: set[int],
    inicio: date,
    fim_exclusivo: date,
    natureza: str,
) -> tuple[list[dict], list[dict]]:
    contas_ids = {conta.pk for conta in contas}
    if not contas_ids:
        return [], []
    parcelas_prefetch = Prefetch(
        "fatura__parcelas",
        queryset=ParcelaCartao.objects.select_related("categoria", "compra__evento").exclude(
            situacao=Situacao.CANCELADO
        ),
    )
    titulos = list(
        Titulo.objects.filter(
            usuario=usuario,
            ativo=True,
            status=Titulo.Status.ABERTO,
            saldo_aberto__gt=ZERO,
            conta_id__in=contas_ids,
        )
        .filter(Q(fatura__isnull=True) | Q(fatura__cartao_id__in=cartoes_ids))
        .select_related("conta", "evento", "fatura__cartao")
        .prefetch_related(
            "programacoes__conta",
            "evento__rateios__categoria",
            parcelas_prefetch,
        )
        .order_by("vencimento", "id")
    )
    receitas = []
    saidas = []
    for titulo in titulos:
        proporcao = _proporcao_titulo(titulo, natureza)
        if proporcao <= ZERO:
            continue
        programado = ZERO
        destinos = receitas if titulo.tipo == Titulo.Tipo.RECEBER else saidas
        situacao = "Previsto"
        origem_nome = titulo.conta.nome if titulo.conta else "Conta não informada"
        for programacao in titulo.programacoes.all():
            if programacao.status not in {
                ProgramacaoPagamento.Status.ATIVA,
                ProgramacaoPagamento.Status.PARCIAL,
            }:
                continue
            restante = min(
                max(programacao.valor_programado - programacao.valor_realizado, ZERO),
                max(titulo.saldo_aberto - programado, ZERO),
            )
            if restante <= ZERO:
                continue
            programado += restante
            if programacao.conta_id not in contas_ids or not (inicio <= programacao.data_programada < fim_exclusivo):
                continue
            destinos.append(
                _item(
                    data=programacao.data_programada,
                    descricao=f"{titulo.descricao} (programado)",
                    origem="TITULO",
                    origem_nome=programacao.conta.nome,
                    situacao=situacao,
                    valor=restante * proporcao,
                    observacao="Saldo ainda aberto do título.",
                )
            )
        residual = max(titulo.saldo_aberto - programado, ZERO)
        if residual > ZERO and inicio <= titulo.vencimento < fim_exclusivo:
            destinos.append(
                _item(
                    data=titulo.vencimento,
                    descricao=titulo.descricao,
                    origem="TITULO",
                    origem_nome=origem_nome,
                    situacao=situacao,
                    valor=residual * proporcao,
                    observacao="Saldo ainda aberto do título.",
                )
            )
    return receitas, saidas


def _valor_compra(compra: CompraCartao, natureza: str) -> tuple[Decimal, bool]:
    todas_parcelas = list(compra.parcelas.all())
    parcelas = [
        parcela
        for parcela in todas_parcelas
        if parcela.situacao != Situacao.CANCELADO
        and _natureza_parcela(parcela) not in {NaturezaEvento.PAGAMENTO_FATURA, NaturezaEvento.TRANSFERENCIA}
    ]
    numeros = {parcela.numero for parcela in todas_parcelas if parcela.numero > 0}
    materializada = bool(compra.total_parcelas and len(numeros) >= compra.total_parcelas)
    if materializada:
        valor = sum(
            (
                _valor_assinado_cartao(parcela.valor, _natureza_parcela(parcela))
                for parcela in parcelas
                if _categoria_compativel(parcela.categoria, natureza)
            ),
            ZERO,
        )
        return valor, False

    proporcao = _proporcao_parcelas(parcelas, natureza)
    if proporcao <= ZERO:
        proporcao = _proporcao_rateios(compra.evento, natureza)
    if proporcao <= ZERO:
        return ZERO, bool(compra.valor_total_estimado or compra.valor_total is None)

    natureza_evento = compra.evento.natureza
    if compra.valor_total is not None and not compra.valor_total_estimado:
        return _valor_assinado_cartao(compra.valor_total, natureza_evento) * proporcao, False

    valores_conhecidos = [
        _valor_assinado_cartao(parcela.valor, _natureza_parcela(parcela))
        for parcela in parcelas
    ]
    if not valores_conhecidos:
        valor_base = compra.valor_total or ZERO
        return _valor_assinado_cartao(valor_base, natureza_evento) * proporcao, True
    media = sum(valores_conhecidos, ZERO) / Decimal(len(valores_conhecidos))
    quantidade = compra.total_parcelas or len(valores_conhecidos)
    return media * Decimal(quantidade) * proporcao, True


def _compras_cartao(
    usuario,
    cartoes_ids: set[int],
    inicio: date,
    fim_exclusivo: date,
    natureza: str,
) -> list[dict]:
    if not cartoes_ids:
        return []
    compras = list(
        CompraCartao.objects.filter(
            usuario=usuario,
            cartao_id__in=cartoes_ids,
            data_compra__gte=inicio,
            data_compra__lt=fim_exclusivo,
        )
        .exclude(evento__situacao=Situacao.CANCELADO)
        .select_related("cartao", "evento")
        .prefetch_related(
            "evento__rateios__categoria",
            "parcelas__categoria",
            "parcelas__fatura",
        )
        .order_by("data_compra", "id")
    )
    itens = []
    for compra in compras:
        if compra.evento.natureza in {NaturezaEvento.PAGAMENTO_FATURA, NaturezaEvento.TRANSFERENCIA}:
            continue
        valor, estimado = _valor_compra(compra, natureza)
        if valor == ZERO:
            continue
        situacao = "Realizado" if compra.evento.situacao == Situacao.REALIZADO else "Previsto"
        itens.append(
            _item(
                data=compra.data_compra,
                descricao=compra.estabelecimento or compra.evento.descricao,
                origem="CARTAO",
                origem_nome=compra.cartao.nome,
                situacao=situacao,
                valor=valor,
                estimado=estimado,
                observacao=(
                    f"Compra reconhecida integralmente no mês ({compra.total_parcelas} parcela(s))."
                ),
            )
        )
    return itens


def _resumir(itens: list[dict]) -> dict:
    realizados = sum((item["valor"] for item in itens if item["situacao"] == "Realizado"), ZERO)
    previstos = sum((item["valor"] for item in itens if item["situacao"] != "Realizado"), ZERO)
    return {
        "realizados": realizados,
        "previstos": previstos,
        "total": realizados + previstos,
        "itens": itens,
    }


def _evolucao_diaria(
    inicio: date,
    fim: date,
    saldo_inicial: Decimal,
    receitas: list[dict],
    saidas: list[dict],
    compras: list[dict],
) -> dict:
    por_data = defaultdict(lambda: {"receitas": ZERO, "saidas": ZERO, "compras": ZERO})
    for item in receitas:
        por_data[item["data"]]["receitas"] += item["valor"]
    for item in saidas:
        por_data[item["data"]]["saidas"] += item["valor"]
    for item in compras:
        por_data[item["data"]]["compras"] += item["valor"]

    saldo_renda = ZERO
    saldo_com_saldo = saldo_inicial
    primeira_renda = inicio if saldo_renda < ZERO else None
    primeira_com_saldo = inicio if saldo_com_saldo < ZERO else None
    menor_renda = min(saldo_renda, ZERO)
    menor_com_saldo = min(saldo_com_saldo, ZERO)
    linhas = []
    data_atual = inicio
    while data_atual <= fim:
        valores = por_data.get(data_atual)
        if valores:
            saldo_renda += valores["receitas"] - valores["saidas"] - valores["compras"]
            saldo_com_saldo += valores["receitas"] - valores["saidas"] - valores["compras"]
            if saldo_renda < ZERO and primeira_renda is None:
                primeira_renda = data_atual
            if saldo_com_saldo < ZERO and primeira_com_saldo is None:
                primeira_com_saldo = data_atual
            menor_renda = min(menor_renda, saldo_renda)
            menor_com_saldo = min(menor_com_saldo, saldo_com_saldo)
            linhas.append(
                {
                    "data": data_atual,
                    **valores,
                    "saldo_renda": saldo_renda,
                    "saldo_com_saldo": saldo_com_saldo,
                }
            )
        data_atual += timedelta(days=1)
    return {
        "linhas": linhas,
        "saldo_final_renda": saldo_renda,
        "saldo_final_com_saldo": saldo_com_saldo,
        "primeira_data_renda": primeira_renda,
        "primeira_data_com_saldo": primeira_com_saldo,
        "maior_deficit_renda": abs(min(menor_renda, ZERO)),
        "maior_deficit_com_saldo": abs(min(menor_com_saldo, ZERO)),
    }


def consultar_cobertura_credito(
    usuario,
    *,
    data_inicial: date,
    data_final: date,
    contas: list[Conta],
    cartoes: list,
    saldo_inicial: Decimal,
    natureza: str = "all",
) -> dict:
    natureza = natureza if natureza in NATUREZAS else "all"
    fim_exclusivo = data_final + timedelta(days=1)
    cartoes_ids = {cartao.pk for cartao in cartoes}
    receitas_realizadas, saidas_realizadas = _movimentos_realizados(
        usuario,
        contas,
        data_inicial,
        fim_exclusivo,
        natureza,
    )
    receitas_previstas, saidas_previstas = _titulos_previstos(
        usuario,
        contas,
        cartoes_ids,
        data_inicial,
        fim_exclusivo,
        natureza,
    )
    compras = _compras_cartao(
        usuario,
        cartoes_ids,
        data_inicial,
        fim_exclusivo,
        natureza,
    )
    receitas = _resumir(receitas_realizadas + receitas_previstas)
    saidas = _resumir(saidas_realizadas + saidas_previstas)
    compras_resumo = _resumir(compras)
    cobertura_renda = receitas["total"] - saidas["total"] - compras_resumo["total"]
    cobertura_com_saldo = saldo_inicial + cobertura_renda
    pressao = saidas["total"] + compras_resumo["total"]
    evolucao = _evolucao_diaria(
        data_inicial,
        data_final,
        saldo_inicial,
        receitas["itens"],
        saidas["itens"],
        compras_resumo["itens"],
    )
    return {
        "data_inicial": data_inicial,
        "data_final": data_final,
        "saldo_inicial": saldo_inicial,
        "receitas": receitas,
        "saidas": saidas,
        "compras_cartao": compras_resumo,
        "pressao_financeira": pressao,
        "cobertura_renda": cobertura_renda,
        "cobertura_com_saldo": cobertura_com_saldo,
        "percentual_cobertura_renda": _percentual_cobertura(receitas["total"], pressao),
        "percentual_cobertura_com_saldo": _percentual_cobertura(
            saldo_inicial + receitas["total"],
            pressao,
        ),
        "primeira_data_dependencia_renda": evolucao["primeira_data_renda"],
        "primeira_data_dependencia_com_saldo": evolucao["primeira_data_com_saldo"],
        "maior_deficit_renda": evolucao["maior_deficit_renda"],
        "maior_deficit_com_saldo": evolucao["maior_deficit_com_saldo"],
        "estado_renda": _estado_trajetoria(
            evolucao["primeira_data_renda"],
            cobertura_renda,
        ),
        "estado_com_saldo": _estado_trajetoria(
            evolucao["primeira_data_com_saldo"],
            cobertura_com_saldo,
        ),
        "evolucao_diaria": evolucao["linhas"],
        "tem_estimativas": any(item["estimado"] for item in compras),
    }
