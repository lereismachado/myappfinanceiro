from __future__ import annotations

import re

from django.utils import timezone

from financeiro.models import Cartao, Conta, Fatura, MovimentoConta, Titulo


CICLO_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def rotulo_periodo(ciclo: str) -> str:
    ano, mes = ciclo.split("-")
    return f"{mes}/{ano}"


def ciclos_disponiveis(usuario, ciclo_solicitado: str | None = None) -> list[str]:
    ciclos = {timezone.localdate().strftime("%Y-%m")}
    ciclos.update(
        valor.strftime("%Y-%m")
        for valor in MovimentoConta.objects.filter(conta__usuario=usuario)
        .values_list("data_efetiva", flat=True)
        .distinct()
    )
    ciclos.update(
        valor.strftime("%Y-%m")
        for valor in Titulo.objects.filter(usuario=usuario)
        .values_list("vencimento", flat=True)
        .distinct()
    )
    ciclos.update(Fatura.objects.filter(usuario=usuario).values_list("ciclo", flat=True).distinct())
    if ciclo_solicitado and CICLO_PATTERN.fullmatch(ciclo_solicitado):
        ciclos.add(ciclo_solicitado)
    return sorted(ciclos, reverse=True)


def resolver_filtros_financeiros(
    usuario,
    periodo=None,
    conta_id=None,
    cartao_id=None,
    aplicar_composicao_dashboard=False,
) -> dict:
    contas_queryset = Conta.objects.filter(usuario=usuario)
    if aplicar_composicao_dashboard:
        contas_queryset = contas_queryset.filter(nao_compoe_dashboard=False)
    contas = list(contas_queryset.order_by("nome", "id"))
    cartoes = list(
        Cartao.objects.filter(usuario=usuario)
        .select_related("conta_pagamento_padrao")
        .order_by("nome", "id")
    )
    ciclos = ciclos_disponiveis(usuario, periodo)
    periodo_atual = timezone.localdate().strftime("%Y-%m")
    periodo_selecionado = periodo if periodo in ciclos and CICLO_PATTERN.fullmatch(periodo or "") else periodo_atual
    if periodo_selecionado not in ciclos:
        ciclos.append(periodo_selecionado)
        ciclos.sort(reverse=True)

    conta_selecionada = None
    if conta_id not in (None, "", "all"):
        try:
            conta_pk = int(conta_id)
        except (TypeError, ValueError):
            conta_pk = None
        conta_selecionada = next((conta for conta in contas if conta.pk == conta_pk), None)

    cartoes_compativeis = [
        cartao
        for cartao in cartoes
        if not conta_selecionada or cartao.conta_pagamento_padrao_id == conta_selecionada.pk
    ]
    cartao_selecionado = None
    if cartao_id not in (None, "", "all"):
        try:
            cartao_pk = int(cartao_id)
        except (TypeError, ValueError):
            cartao_pk = None
        cartao_selecionado = next(
            (cartao for cartao in cartoes_compativeis if cartao.pk == cartao_pk),
            None,
        )

    return {
        "contas": contas,
        "conta_selecionada": conta_selecionada,
        "cartoes_compativeis": cartoes_compativeis,
        "cartao_selecionado": cartao_selecionado,
        "cartoes_filtrados": [cartao_selecionado] if cartao_selecionado else cartoes_compativeis,
        "periodos": [{"valor": ciclo, "rotulo": rotulo_periodo(ciclo)} for ciclo in ciclos],
        "periodo_selecionado": periodo_selecionado,
        "periodo_rotulo": rotulo_periodo(periodo_selecionado),
    }
