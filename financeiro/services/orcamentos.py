from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from financeiro.models import Categoria, Orcamento
from financeiro.services.dashboard import carregar_contribuicoes_dashboard


ZERO = Decimal("0.00")
HUNDRED = Decimal("100.00")
CLONAGEM_COMPLETA = "completa"
CLONAGEM_PARCIAL = "parcial"
IMPORTACAO_COMPLETA = "completa"
IMPORTACAO_PARCIAL = "parcial"


class ClonagemPeriodoOrcamentoInvalida(ValueError):
    pass


class ImportacaoDespesasOrcamentoInvalida(ValueError):
    pass


def _chave_orcamento(orcamento: Orcamento) -> tuple[int, str]:
    return orcamento.categoria_id, orcamento.ambito


def previsualizar_clonagem_periodo(usuario, mes_origem: str, mes_destino: str) -> dict:
    origem = set(
        Orcamento.objects.filter(usuario=usuario, mes=mes_origem).values_list("categoria_id", "ambito")
    )
    destino = set(
        Orcamento.objects.filter(usuario=usuario, mes=mes_destino).values_list("categoria_id", "ambito")
    )
    return {
        "mes_origem": mes_origem,
        "mes_destino": mes_destino,
        "quantidade_origem": len(origem),
        "quantidade_destino": len(destino),
        "quantidade_faltantes": len(origem - destino),
        "quantidade_ja_existentes": len(origem & destino),
    }


@transaction.atomic
def clonar_periodo_orcamentos(usuario, mes_origem: str, mes_destino: str, modo: str) -> dict:
    if mes_origem == mes_destino:
        raise ClonagemPeriodoOrcamentoInvalida(
            "O período de destino deve ser diferente do período de origem."
        )
    if modo not in {CLONAGEM_COMPLETA, CLONAGEM_PARCIAL}:
        raise ClonagemPeriodoOrcamentoInvalida("Escolha uma opção válida de clonagem.")

    orcamentos = list(
        Orcamento.objects.select_for_update()
        .filter(usuario=usuario, mes__in=[mes_origem, mes_destino])
        .order_by("id")
    )
    origem = [orcamento for orcamento in orcamentos if orcamento.mes == mes_origem]
    destino = [orcamento for orcamento in orcamentos if orcamento.mes == mes_destino]
    if not origem:
        raise ClonagemPeriodoOrcamentoInvalida(
            "Nenhum orçamento foi encontrado no período de origem."
        )

    if modo == CLONAGEM_COMPLETA:
        ids_destino = [orcamento.pk for orcamento in destino]
        if ids_destino:
            Orcamento.objects.filter(pk__in=ids_destino, usuario=usuario).delete()
        selecionados = origem
    else:
        chaves_destino = {_chave_orcamento(orcamento) for orcamento in destino}
        selecionados = [
            orcamento for orcamento in origem if _chave_orcamento(orcamento) not in chaves_destino
        ]
        if not selecionados:
            raise ClonagemPeriodoOrcamentoInvalida(
                "Todas as categorias e âmbitos da origem já existem no período de destino."
            )

    novos = [
        Orcamento(
            usuario=usuario,
            categoria_id=orcamento.categoria_id,
            mes=mes_destino,
            ambito=orcamento.ambito,
            limite=orcamento.limite,
        )
        for orcamento in selecionados
    ]
    Orcamento.objects.bulk_create(novos)
    return {
        "modo": modo,
        "copiados": len(novos),
        "substituidos": len(destino) if modo == CLONAGEM_COMPLETA else 0,
        "preservados": len(destino) if modo == CLONAGEM_PARCIAL else 0,
        "ignorados": len(origem) - len(novos),
    }


def _limites_meses(meses: set[str]) -> tuple[date, date]:
    primeiro = min(meses)
    ultimo = max(meses)
    ano_inicial, mes_inicial = (int(parte) for parte in primeiro.split("-"))
    ano_final, mes_final = (int(parte) for parte in ultimo.split("-"))
    return (
        date(ano_inicial, mes_inicial, 1),
        date(ano_final, mes_final, monthrange(ano_final, mes_final)[1]),
    )


def _categoria_superior_id(categoria, categorias: dict) -> int | None:
    if not categoria:
        return None
    atual = categoria
    visitados = set()
    while atual.categoria_pai_id and atual.categoria_pai_id not in visitados:
        visitados.add(atual.pk)
        atual = categorias.get(atual.categoria_pai_id, atual.categoria_pai)
    return atual.pk


def _chave_contribuicao(contribuicao, categorias: dict) -> tuple[str, int | None, str]:
    if contribuicao.origem == "CONTA":
        competencia = contribuicao.registro.evento.data.strftime("%Y-%m")
        ambito = contribuicao.registro.evento.ambito
    else:
        competencia = contribuicao.registro.vencimento.strftime("%Y-%m")
        ambito = contribuicao.registro.ambito_exibicao
    return competencia, _categoria_superior_id(contribuicao.categoria, categorias), ambito


def _consolidar_realizados(
    usuario,
    meses: set[str],
    dashboard_context=False,
) -> tuple[dict, dict]:
    data_inicial, data_final = _limites_meses(meses)
    dados = carregar_contribuicoes_dashboard(
        usuario,
        date_from=data_inicial,
        date_to=data_final,
        aplicar_composicao_dashboard=dashboard_context,
    )
    realizados = defaultdict(lambda: ZERO)
    for contribuicao in dados["contribuicoes"]:
        valor = contribuicao.valor_indicador
        if valor == ZERO:
            continue
        chave = _chave_contribuicao(contribuicao, dados["categorias"])
        if chave[0] in meses:
            realizados[chave] += valor
    return realizados, dados["categorias"]


def consolidar_despesas_para_orcamento(usuario, mes_despesas: str) -> dict:
    realizados, categorias = _consolidar_realizados(usuario, {mes_despesas})
    itens = []
    ignorados = {
        "sem_categoria": {"quantidade": 0, "valor": ZERO},
        "categoria_inativa": {"quantidade": 0, "valor": ZERO},
        "nao_positivos": {"quantidade": 0, "valor": ZERO},
    }
    rotulos_ambito = dict(Orcamento._meta.get_field("ambito").choices)

    for (_, categoria_id, ambito), valor_bruto in realizados.items():
        valor = valor_bruto.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if categoria_id is None:
            motivo = "sem_categoria"
        else:
            categoria = categorias.get(categoria_id)
            if (
                not categoria
                or not categoria.ativa
                or categoria.tipo != Categoria.Tipo.DESPESA
                or categoria.categoria_pai_id is not None
            ):
                motivo = "categoria_inativa"
            elif valor <= ZERO:
                motivo = "nao_positivos"
            else:
                itens.append(
                    {
                        "categoria_id": categoria_id,
                        "categoria_nome": categoria.nome,
                        "ambito": ambito,
                        "ambito_rotulo": rotulos_ambito.get(ambito, ambito),
                        "valor": valor,
                    }
                )
                continue
        ignorados[motivo]["quantidade"] += 1
        ignorados[motivo]["valor"] += valor

    itens.sort(key=lambda item: (item["categoria_nome"].casefold(), item["ambito"]))
    for resumo in ignorados.values():
        resumo["valor"] = resumo["valor"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "mes_despesas": mes_despesas,
        "itens": itens,
        "quantidade_importavel": len(itens),
        "valor_importavel": sum((item["valor"] for item in itens), ZERO).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ),
        "quantidade_ignorados": sum(item["quantidade"] for item in ignorados.values()),
        "ignorados": ignorados,
    }


def previsualizar_importacao_despesas(usuario, mes_despesas: str, mes_destino: str) -> dict:
    resumo = consolidar_despesas_para_orcamento(usuario, mes_despesas)
    origem = {(item["categoria_id"], item["ambito"]) for item in resumo["itens"]}
    destino = set(
        Orcamento.objects.filter(usuario=usuario, mes=mes_destino).values_list(
            "categoria_id", "ambito"
        )
    )
    return {
        **resumo,
        "mes_destino": mes_destino,
        "quantidade_destino": len(destino),
        "quantidade_faltantes": len(origem - destino),
        "quantidade_ja_existentes": len(origem & destino),
    }


@transaction.atomic
def importar_despesas_para_orcamento(
    usuario,
    mes_despesas: str,
    mes_destino: str,
    modo: str,
) -> dict:
    mes_atual = timezone.localdate().strftime("%Y-%m")
    if mes_despesas > mes_atual:
        raise ImportacaoDespesasOrcamentoInvalida(
            "O período das despesas não pode ser posterior ao mês atual."
        )
    if mes_destino <= mes_despesas:
        raise ImportacaoDespesasOrcamentoInvalida(
            "O período de destino deve ser posterior ao período das despesas."
        )
    if modo not in {IMPORTACAO_COMPLETA, IMPORTACAO_PARCIAL}:
        raise ImportacaoDespesasOrcamentoInvalida("Escolha uma opção válida de importação.")

    resumo = consolidar_despesas_para_orcamento(usuario, mes_despesas)
    if not resumo["itens"]:
        raise ImportacaoDespesasOrcamentoInvalida(
            "Nenhuma despesa elegível foi encontrada no período informado."
        )

    destino = list(
        Orcamento.objects.select_for_update()
        .filter(usuario=usuario, mes=mes_destino)
        .order_by("id")
    )
    chaves_destino = {_chave_orcamento(orcamento) for orcamento in destino}
    if modo == IMPORTACAO_COMPLETA:
        ids_destino = [orcamento.pk for orcamento in destino]
        if ids_destino:
            Orcamento.objects.filter(usuario=usuario, pk__in=ids_destino).delete()
        selecionados = resumo["itens"]
    else:
        selecionados = [
            item
            for item in resumo["itens"]
            if (item["categoria_id"], item["ambito"]) not in chaves_destino
        ]
        if not selecionados:
            raise ImportacaoDespesasOrcamentoInvalida(
                "Todas as categorias e âmbitos importáveis já existem no período de destino."
            )

    novos = [
        Orcamento(
            usuario=usuario,
            categoria_id=item["categoria_id"],
            mes=mes_destino,
            ambito=item["ambito"],
            limite=item["valor"],
        )
        for item in selecionados
    ]
    Orcamento.objects.bulk_create(novos)
    return {
        "modo": modo,
        "incluidos": len(novos),
        "substituidos": len(destino) if modo == IMPORTACAO_COMPLETA else 0,
        "preservados": len(destino) if modo == IMPORTACAO_PARCIAL else 0,
        "ignorados": resumo["quantidade_ignorados"] + len(resumo["itens"]) - len(novos),
        "valor_importado": sum((item["valor"] for item in selecionados), ZERO).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ),
    }


def _percentual_realizado(realizado: Decimal, limite: Decimal) -> Decimal | None:
    if limite <= ZERO:
        return None
    if realizado <= ZERO:
        return ZERO
    return (realizado / limite * HUNDRED).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _faixa_percentual(percentual: Decimal | None) -> str:
    if percentual is None:
        return "neutral"
    if percentual <= Decimal("80.00"):
        return "blue"
    if percentual <= Decimal("90.00"):
        return "orange"
    return "red"


def _urls_detalhamento_categoria(
    mes_referencia: str,
    categoria_id: int,
    ambito: str = "all",
    dashboard_context=False,
) -> tuple[str, str]:
    ano, mes = (int(parte) for parte in mes_referencia.split("-"))
    data_inicial = date(ano, mes, 1)
    data_final = date(ano, mes, monthrange(ano, mes)[1])
    base = {
        "date_from": data_inicial.strftime("%d/%m/%Y"),
        "date_to": data_final.strftime("%d/%m/%Y"),
        "account": "all",
        "card": "all",
        "origin": "all",
    }
    detalhe = {
        **base,
        "nature": "all",
        "type": "category",
        "value": categoria_id,
    }
    lancamentos = {
        **base,
        "analysis_type": "category",
        "analysis_value": categoria_id,
        "analysis_nature": "all",
        "parent_category": categoria_id,
        "computes": "COMPUTA",
    }
    if ambito != "all":
        detalhe["scope"] = ambito
        lancamentos["analysis_scope"] = ambito
    if dashboard_context:
        detalhe["dashboard_context"] = "1"
        lancamentos["dashboard_context"] = "1"
    return (
        f"{reverse('dashboard_detalhamento')}?{urlencode(detalhe)}",
        f"{reverse('eventos')}?{urlencode(lancamentos)}",
    )


def _urls_detalhamento(
    orcamento: Orcamento,
    dashboard_context=False,
) -> tuple[str, str]:
    return _urls_detalhamento_categoria(
        orcamento.mes,
        orcamento.categoria_id,
        orcamento.ambito,
        dashboard_context=dashboard_context,
    )


def enriquecer_orcamentos_com_realizado(
    usuario,
    orcamentos: list[Orcamento],
    dashboard_context=False,
) -> list[Orcamento]:
    if not orcamentos:
        return orcamentos

    meses = {orcamento.mes for orcamento in orcamentos}
    realizados, _ = _consolidar_realizados(
        usuario,
        meses,
        dashboard_context=dashboard_context,
    )

    for orcamento in orcamentos:
        chave = (orcamento.mes, orcamento.categoria_id, orcamento.ambito)
        realizado = realizados[chave].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        limite = Decimal(orcamento.limite)
        percentual = _percentual_realizado(realizado, limite)
        orcamento.realizado = realizado
        orcamento.percentual_realizado = percentual
        orcamento.percentual_css = (
            f"{min(max(percentual, ZERO), HUNDRED):.2f}"
            if percentual is not None
            else "0.00"
        )
        orcamento.faixa_percentual = _faixa_percentual(percentual)
        orcamento.detalhe_url, orcamento.lancamentos_url = _urls_detalhamento(
            orcamento,
            dashboard_context=dashboard_context,
        )
    return orcamentos


def resumir_orcamento_dashboard(usuario, mes: str, dashboard_context=False) -> dict:
    orcamentos = list(
        Orcamento.objects.filter(usuario=usuario, mes=mes)
        .select_related("categoria", "categoria__categoria_pai")
        .order_by("categoria__nome", "ambito", "id")
    )
    enriquecer_orcamentos_com_realizado(
        usuario,
        orcamentos,
        dashboard_context=dashboard_context,
    )

    consolidados = {}
    for orcamento in orcamentos:
        item = consolidados.setdefault(
            orcamento.categoria_id,
            {
                "categoria_id": orcamento.categoria_id,
                "nome": orcamento.categoria.rotulo_com_pai,
                "orcado": ZERO,
                "realizado": ZERO,
            },
        )
        item["orcado"] += Decimal(orcamento.limite)
        item["realizado"] += orcamento.realizado

    total_orcado = sum((item["orcado"] for item in consolidados.values()), ZERO)
    categorias = []
    for item in consolidados.values():
        item["orcado"] = item["orcado"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        item["realizado"] = item["realizado"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        participacao = _percentual_realizado(item["orcado"], total_orcado)
        comprometimento = _percentual_realizado(item["realizado"], item["orcado"])
        item.update(
            {
                "participacao": participacao,
                "participacao_css": (
                    f"{min(max(participacao, ZERO), HUNDRED):.2f}"
                    if participacao is not None
                    else "0.00"
                ),
                "participacao_aria_max": max(participacao or HUNDRED, HUNDRED),
                "percentual_comprometido": comprometimento,
                "percentual_comprometido_css": (
                    f"{min(max(comprometimento, ZERO), HUNDRED):.2f}"
                    if comprometimento is not None
                    else "0.00"
                ),
                "percentual_comprometido_aria_max": max(comprometimento or HUNDRED, HUNDRED),
                "faixa_percentual": _faixa_percentual(comprometimento),
            }
        )
        item["detalhe_url"], item["lancamentos_url"] = _urls_detalhamento_categoria(
            mes,
            item["categoria_id"],
            dashboard_context=dashboard_context,
        )
        categorias.append(item)

    categorias.sort(
        key=lambda item: (
            item["percentual_comprometido"] is None,
            -(item["percentual_comprometido"] or ZERO),
            item["nome"].casefold(),
        )
    )
    return {
        "mes": mes,
        "mes_rotulo": f"{mes[5:7]}/{mes[:4]}",
        "total_orcado": total_orcado.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "categorias": categorias,
    }


def alertas_orcamento_dashboard(resumo: dict) -> list[dict]:
    alertas = []
    for categoria in resumo["categorias"]:
        percentual = categoria["percentual_comprometido"]
        if percentual is None or percentual < Decimal("90.00"):
            continue
        if percentual > HUNDRED:
            situacao = "Orçamento estourado"
        elif percentual == HUNDRED:
            situacao = "Limite do orçamento atingido"
        else:
            situacao = "Orçamento próximo do limite"
        alertas.append(
            {
                "tipo": "orcamento",
                "titulo": categoria["nome"],
                "mensagem": f"{situacao} · {percentual:.2f}% comprometido",
                "realizado": categoria["realizado"],
                "orcado": categoria["orcado"],
                "percentual": percentual,
            }
        )
    return alertas
