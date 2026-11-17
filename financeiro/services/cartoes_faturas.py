from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from financeiro.models import (
    Cartao,
    CompraCartao,
    Conta,
    EventoFinanceiro,
    Fatura,
    NaturezaEvento,
    ParcelaCartao,
    RateioCategoria,
    SaldoTransportadoFatura,
    Situacao,
    Titulo,
)
from financeiro.services.importacao_ofx import proximo_ciclo, recalcular_fatura
from financeiro.vencimentos_cartao import resolver_vencimento_fatura


ZERO = Decimal("0.00")
STATUS_FATURA_BLOQUEADA = {Fatura.Status.FECHADA, Fatura.Status.PAGA}
ROTULOS_SITUACAO_ITEM = {
    Situacao.REALIZADO: "Confirmado",
    Situacao.PREVISTO: "Previsto",
    Situacao.PENDENTE: "Pendente",
    Situacao.CANCELADO: "Cancelado",
}


def rotulo_ciclo(ciclo: str) -> str:
    ano, mes = ciclo.split("-")
    return f"{mes}/{ano}"


def _data_no_ciclo(ciclo: str, dia: int) -> date:
    ano, mes = (int(parte) for parte in ciclo.split("-"))
    return date(ano, mes, min(int(dia), monthrange(ano, mes)[1]))


def _ciclo_primeiro_pagamento(cartao, data_compra: date) -> str:
    ciclo_fechamento = f"{data_compra.year:04d}-{data_compra.month:02d}"
    fechamento = _data_no_ciclo(ciclo_fechamento, cartao.dia_fechamento)
    if data_compra > fechamento:
        ciclo_fechamento = proximo_ciclo(ciclo_fechamento)
        fechamento = _data_no_ciclo(ciclo_fechamento, cartao.dia_fechamento)

    vencimento = _data_no_ciclo(ciclo_fechamento, cartao.dia_vencimento)
    if vencimento <= fechamento:
        return proximo_ciclo(ciclo_fechamento)
    return ciclo_fechamento


def calcular_totais_parcelas(parcelas) -> dict[str, Decimal]:
    """Calcula a composição confirmada e projetada de uma fatura."""
    total_confirmado = ZERO
    total_projetado = ZERO
    for parcela in parcelas:
        natureza = parcela.natureza_exibicao
        if natureza == NaturezaEvento.PAGAMENTO_FATURA or parcela.situacao == Situacao.CANCELADO:
            continue
        sinal = Decimal("-1") if natureza in {NaturezaEvento.ESTORNO, NaturezaEvento.RECEITA} else Decimal("1")
        valor = parcela.valor * sinal
        total_projetado += valor
        if parcela.situacao == Situacao.REALIZADO:
            total_confirmado += valor
    return {
        "confirmado": total_confirmado,
        "projetado": total_projetado,
    }


def aviso_ciclo_cartao(
    cartao,
    data_compra: date | None,
    ciclo: str | None,
    *,
    vencimento: date | None = None,
    numero_parcela: int | None = None,
    total_parcelas: int | None = None,
) -> str:
    """Informa divergência não bloqueante entre a referência da compra e o ciclo informado.

    O ciclo representa o mês de vencimento/pagamento. Um vencimento explícito é a
    referência mais precisa; sem ele, os dias de fechamento e vencimento estimam
    em qual mês a fatura será paga. O número da parcela só desloca a estimativa
    derivada da compra, pois cada parcela já possui vencimento próprio quando ele
    é informado.
    """
    if not cartao or not ciclo:
        return ""

    if vencimento:
        ciclo_esperado = f"{vencimento.year:04d}-{vencimento.month:02d}"
        referencia = f"Com base no vencimento ({vencimento:%d/%m/%Y})"
    elif data_compra and cartao.dia_fechamento:
        ciclo_esperado = _ciclo_primeiro_pagamento(cartao, data_compra)
        referencia = f"Com base na data da compra ({data_compra:%d/%m/%Y})"
        if numero_parcela and numero_parcela > 1:
            ciclo_esperado = proximo_ciclo(ciclo_esperado, numero_parcela - 1)
            parcela = f"{numero_parcela}/{total_parcelas}" if total_parcelas else str(numero_parcela)
            referencia += f" e na parcela {parcela}"
    else:
        return ""

    if ciclo == ciclo_esperado:
        return ""
    return (
        f"{referencia}, o ciclo esperado é {rotulo_ciclo(ciclo_esperado)}, "
        f"mas foi informado {rotulo_ciclo(ciclo)}. O ciclo informado será mantido."
    )


def _valor_assinado(parcela: ParcelaCartao) -> Decimal:
    if parcela.natureza_exibicao == NaturezaEvento.DESPESA:
        return -parcela.valor
    return parcela.valor


def _chave_ordem(parcela: ParcelaCartao) -> tuple:
    return parcela.data_original_exibicao, parcela.compra.evento_id, parcela.id


def _acoes_fatura(fatura: Fatura, titulo: Titulo | None, pendencias: int) -> dict:
    """Centraliza as transições disponíveis e a explicação exibida na interface."""
    estado_legado_sem_titulo = fatura.status == Fatura.Status.PARCIAL and titulo is None
    estado_permite_fechamento = fatura.status in {
        Fatura.Status.ABERTA,
        Fatura.Status.EM_CORRECAO,
    } or estado_legado_sem_titulo
    pode_fechar = estado_permite_fechamento and pendencias == 0
    saldo_aberto = titulo.saldo_aberto if titulo else ZERO
    pode_operar_saldo = bool(
        titulo
        and saldo_aberto > ZERO
        and fatura.status in {Fatura.Status.FECHADA, Fatura.Status.PARCIAL}
    )
    pode_reabrir = bool(
        titulo
        and fatura.status in {Fatura.Status.FECHADA, Fatura.Status.PARCIAL, Fatura.Status.PAGA}
    )

    motivo_fechamento = ""
    if estado_permite_fechamento and pendencias:
        motivo_fechamento = (
            f"Há {pendencias} previsão(ões) pendente(s). Confirme, transfira ou cancele "
            "esses itens antes do fechamento."
        )

    if pode_fechar:
        orientacao = "A composição está pronta para conferência e fechamento."
    elif motivo_fechamento:
        orientacao = motivo_fechamento
    elif fatura.status == Fatura.Status.EM_CORRECAO:
        orientacao = "Finalize a correção e faça um novo fechamento antes de movimentar o saldo."
    elif pode_operar_saldo:
        orientacao = "O título está disponível para programação, conciliação ou pagamento."
    elif fatura.status == Fatura.Status.PAGA:
        orientacao = "A fatura está paga. Consulte as liquidações ou reabra para correção."
    elif titulo and saldo_aberto <= ZERO:
        orientacao = "Não há saldo exigível para novas ações de pagamento."
    else:
        orientacao = "Não há ação disponível para o estado atual desta fatura."

    return {
        "pode_fechar": pode_fechar,
        "pode_operar_saldo": pode_operar_saldo,
        "pode_reabrir": pode_reabrir,
        "motivo_fechamento": motivo_fechamento,
        "orientacao": orientacao,
    }


def consultar_cartoes_faturas(usuario, ciclo=None, conta_id=None, cartao_id=None, fatura_id=None) -> dict:
    contas = Conta.objects.filter(usuario=usuario).order_by("nome", "id")
    cartoes_usuario = Cartao.objects.filter(usuario=usuario).select_related("conta_pagamento_padrao").order_by("nome", "id")
    ciclo_atual = timezone.localdate().strftime("%Y-%m")
    ciclos_disponiveis = sorted(
        {
            *Fatura.objects.filter(usuario=usuario).values_list("ciclo", flat=True).distinct(),
            ciclo_atual,
        },
        reverse=True,
    )
    ciclo_selecionado = ciclo if ciclo in ciclos_disponiveis else ciclo_atual

    conta_selecionada = None
    if conta_id not in (None, "", "all"):
        try:
            conta_selecionada = contas.get(pk=int(conta_id))
        except (Conta.DoesNotExist, TypeError, ValueError):
            conta_selecionada = None

    cartoes_compativeis = cartoes_usuario
    if conta_selecionada:
        cartoes_compativeis = cartoes_compativeis.filter(conta_pagamento_padrao=conta_selecionada)

    cartao_selecionado = None
    if cartao_id not in (None, "", "all"):
        try:
            cartao_selecionado = cartoes_compativeis.get(pk=int(cartao_id))
        except (Cartao.DoesNotExist, TypeError, ValueError):
            cartao_selecionado = None

    cartoes_filtrados = cartoes_compativeis
    if cartao_selecionado:
        cartoes_filtrados = cartoes_filtrados.filter(pk=cartao_selecionado.pk)

    parcelas = list(
        ParcelaCartao.objects.filter(
            compra__usuario=usuario,
            fatura__ciclo=ciclo_selecionado,
            fatura__cartao__in=cartoes_filtrados,
        )
        .exclude(natureza=NaturezaEvento.PAGAMENTO_FATURA)
        .select_related(
            "compra__evento",
            "fatura__cartao",
            "categoria",
            "pessoa",
        )
    )
    parcelas = [
        parcela
        for parcela in parcelas
        if parcela.natureza_exibicao != NaturezaEvento.PAGAMENTO_FATURA
    ]
    parcelas.sort(key=_chave_ordem)

    totais_composicao = calcular_totais_parcelas(parcelas)
    debitos = ZERO
    creditos = ZERO
    parcelas_ciclo = ZERO
    itens = []
    for parcela in parcelas:
        natureza = parcela.natureza_exibicao
        if parcela.situacao != Situacao.CANCELADO and natureza == NaturezaEvento.DESPESA:
            debitos += parcela.valor
            if parcela.total > 1:
                parcelas_ciclo += parcela.valor
        elif parcela.situacao != Situacao.CANCELADO and natureza in {NaturezaEvento.ESTORNO, NaturezaEvento.RECEITA}:
            creditos += parcela.valor
        itens.append(
            {
                "parcela": parcela,
                "data_original": parcela.data_original_exibicao,
                "descricao": parcela.descricao_exibicao,
                "cartao": parcela.fatura.cartao,
                "marcador": f"{parcela.numero}/{parcela.total}" if parcela.total > 1 else "—",
                "situacao": ROTULOS_SITUACAO_ITEM.get(parcela.situacao, parcela.get_situacao_display()),
                "bloqueia_fechamento": parcela.situacao in {Situacao.PREVISTO, Situacao.PENDENTE},
                "valor_assinado": _valor_assinado(parcela),
            }
        )

    faturas = list(
        Fatura.objects.filter(
            usuario=usuario,
            ciclo=ciclo_selecionado,
            cartao__in=cartoes_filtrados,
        ).select_related("cartao")
    )
    titulos = {
        titulo.fatura_id: titulo
        for titulo in Titulo.objects.filter(usuario=usuario, fatura_id__in=[fatura.pk for fatura in faturas])
        .select_related("conta", "fatura")
    }
    resumo_faturas = []
    for fatura in faturas:
        pendencias = fatura.parcelas.filter(situacao__in=[Situacao.PREVISTO, Situacao.PENDENTE]).count()
        totais_fatura = calcular_totais_parcelas(
            fatura.parcelas.select_related("compra__evento").all()
        )
        resumo_faturas.append({
            "fatura": fatura,
            "titulo": titulos.get(fatura.pk),
            "total_confirmado": totais_fatura["confirmado"],
            "total_projetado": totais_fatura["projetado"],
            "pendencias": pendencias,
        })
    fatura_selecionada = None
    try:
        fatura_pk = int(fatura_id) if fatura_id else None
    except (TypeError, ValueError):
        fatura_pk = None
    if fatura_pk:
        fatura_selecionada = next((item for item in resumo_faturas if item["fatura"].pk == fatura_pk), None)
    elif cartao_selecionado and len(resumo_faturas) == 1:
        fatura_selecionada = resumo_faturas[0]
    painel = None
    if fatura_selecionada:
        fatura = fatura_selecionada["fatura"]
        titulo = fatura_selecionada["titulo"]
        transportes_entrada = list(
            SaldoTransportadoFatura.objects.filter(fatura_destino=fatura)
            .select_related("fatura_origem", "titulo_origem")
            .order_by("-criado_em")
        )
        painel = {
            **fatura_selecionada,
            "acoes": _acoes_fatura(fatura, titulo, fatura_selecionada["pendencias"]),
            "transportes_entrada": transportes_entrada,
            "transportes_saida": list(
                SaldoTransportadoFatura.objects.filter(fatura_origem=fatura).select_related("fatura_destino").order_by("-criado_em")
            ),
            "programacoes": list(titulo.programacoes.select_related("conta").all()) if titulo else [],
            "liquidacoes": list(
                titulo.liquidacoes.filter(status="ATIVA")
                .select_related("movimento_conta__conta")
                .prefetch_related("evidencias")
                .all()
            ) if titulo else [],
            "historico": list(fatura.historico.all()[:20]),
        }
    return {
        "ciclos": [{"valor": valor, "rotulo": rotulo_ciclo(valor)} for valor in ciclos_disponiveis],
        "ciclo_selecionado": ciclo_selecionado,
        "ciclo_rotulo": rotulo_ciclo(ciclo_selecionado),
        "contas": contas,
        "conta_selecionada": conta_selecionada,
        "cartoes_compativeis": cartoes_compativeis,
        "cartao_selecionado": cartao_selecionado,
        "totais": {
            "debitos": debitos,
            "creditos": creditos,
            "liquido": debitos - creditos,
            "parcelas": parcelas_ciclo,
            "confirmado": totais_composicao["confirmado"],
            "projetado": totais_composicao["projetado"],
        },
        "itens": itens,
        "faturas": faturas,
        "resumo_faturas": resumo_faturas,
        "painel": painel,
    }


def _obter_fatura_editavel(usuario, cartao, ciclo: str) -> Fatura:
    fatura, _ = Fatura.objects.select_for_update().get_or_create(
        usuario=usuario,
        cartao=cartao,
        ciclo=ciclo,
        defaults={"status": Fatura.Status.ABERTA},
    )
    if fatura.status in STATUS_FATURA_BLOQUEADA:
        raise ValidationError(f"A fatura {rotulo_ciclo(ciclo)} está fechada e não pode ser alterada.")
    return fatura


@transaction.atomic
def criar_compra_cartao(usuario, dados: dict) -> CompraCartao:
    cartao = Cartao.objects.select_for_update().get(pk=dados["cartao"].pk, usuario=usuario, ativo=True)
    quantidade = dados["quantidade_parcelas"]
    valor_unitario_parcela = dados.get("valor_unitario_parcela", False)
    valor_informado = dados["valor"]
    valor_total = valor_informado * quantidade if valor_unitario_parcela else valor_informado
    if valor_unitario_parcela:
        valores_parcelas = [valor_informado] * quantidade
    else:
        centavos = int(valor_total * 100)
        valor_base, resto = divmod(centavos, quantidade)
        valores_parcelas = [
            Decimal(valor_base + (1 if indice < resto else 0)) / 100
            for indice in range(quantidade)
        ]

    faturas = []
    for indice in range(quantidade):
        ciclo = proximo_ciclo(dados["ciclo"], indice)
        faturas.append(_obter_fatura_editavel(usuario, cartao, ciclo))

    evento = EventoFinanceiro.objects.create(
        usuario=usuario,
        natureza=dados["natureza"],
        descricao=dados["descricao"],
        data=dados["data_original"],
        vencimento=dados.get("vencimento"),
        valor=valor_total,
        pessoa=dados.get("pessoa"),
        pessoa_nome_livre=dados.get("pessoa_nome_livre", ""),
        ambito=dados["ambito"],
        situacao=dados["situacao"],
        observacao=dados.get("observacao", "Lançamento manual de compra no cartão."),
    )
    if dados.get("categoria"):
        RateioCategoria.objects.create(evento=evento, categoria=dados["categoria"], valor=valor_total)
    compra = CompraCartao.objects.create(
        usuario=usuario,
        cartao=cartao,
        evento=evento,
        estabelecimento=dados["descricao"][:180],
        data_compra=dados["data_original"],
        valor_total=valor_total,
        valor_total_estimado=valor_unitario_parcela and quantidade > 1,
        total_parcelas=quantidade,
        chave_compra="",
    )
    for indice, fatura in enumerate(faturas):
        ParcelaCartao.objects.create(
            compra=compra,
            fatura=fatura,
            natureza=dados["natureza"],
            descricao=dados["descricao"],
            data_original=dados["data_original"],
            vencimento=resolver_vencimento_fatura(fatura),
            categoria=dados.get("categoria"),
            pessoa=dados.get("pessoa"),
            pessoa_nome_livre=dados.get("pessoa_nome_livre", ""),
            ambito=dados["ambito"],
            numero=indice + 1,
            total=quantidade,
            valor=valores_parcelas[indice],
            situacao=dados["situacao"] if indice == 0 else Situacao.PREVISTO,
        )
    for fatura in faturas:
        recalcular_fatura(fatura.pk)
    return compra


@transaction.atomic
def editar_parcela_cartao(usuario, parcela_id: int, dados: dict) -> ParcelaCartao:
    parcela = (
        ParcelaCartao.objects.select_for_update()
        .select_related("compra__evento", "fatura__cartao")
        .get(pk=parcela_id, compra__usuario=usuario)
    )
    fatura_anterior = parcela.fatura
    if fatura_anterior.status in STATUS_FATURA_BLOQUEADA:
        raise ValidationError("Esta fatura está fechada e deve ser reaberta antes da edição.")
    if dados["cartao"].pk != fatura_anterior.cartao_id:
        raise ValidationError("O cartão da compra original não pode ser alterado na edição da parcela.")
    nova_fatura = _obter_fatura_editavel(usuario, fatura_anterior.cartao, dados["ciclo"])
    parcela.fatura = nova_fatura
    parcela.natureza = dados["natureza"]
    parcela.descricao = dados["descricao"]
    parcela.data_original = dados["data_original"]
    parcela.vencimento = resolver_vencimento_fatura(nova_fatura)
    parcela.categoria = dados.get("categoria")
    parcela.pessoa = dados.get("pessoa")
    parcela.pessoa_nome_livre = dados.get("pessoa_nome_livre", "")
    parcela.ambito = dados["ambito"]
    parcela.numero = dados["numero_parcela"]
    parcela.total = dados["total_parcelas"]
    parcela.valor = dados["valor"]
    parcela.situacao = dados["situacao"]
    parcela.save()
    evento = parcela.compra.evento
    evento.natureza = dados["natureza"]
    evento.descricao = dados["descricao"]
    evento.valor = dados["valor"]
    evento.pessoa = dados.get("pessoa")
    evento.pessoa_nome_livre = dados.get("pessoa_nome_livre", "")
    evento.ambito = dados["ambito"]
    evento.observacao = dados.get("observacao", evento.observacao)
    evento.save(update_fields=[
        "natureza", "descricao", "valor", "pessoa", "pessoa_nome_livre", "ambito", "observacao", "atualizado_em",
    ])
    evento.rateios.all().delete()
    if dados.get("categoria"):
        RateioCategoria.objects.create(evento=evento, categoria=dados["categoria"], valor=dados["valor"])
    recalcular_fatura(fatura_anterior.pk)
    if nova_fatura.pk != fatura_anterior.pk:
        recalcular_fatura(nova_fatura.pk)
    return parcela


@transaction.atomic
def sincronizar_vencimentos_faturas_abertas_cartao(cartao: Cartao) -> int:
    atualizadas = 0
    faturas = (
        Fatura.objects.select_for_update()
        .filter(
            cartao=cartao,
            status=Fatura.Status.ABERTA,
            vencimento__isnull=True,
        )
        .select_related("cartao")
    )
    for fatura in faturas:
        vencimento = resolver_vencimento_fatura(fatura)
        atualizadas += (
            ParcelaCartao.objects.filter(fatura=fatura)
            .exclude(vencimento=vencimento)
            .update(vencimento=vencimento)
        )
    return atualizadas
