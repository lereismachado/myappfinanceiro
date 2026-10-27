from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from decimal import Decimal
from urllib.parse import urlencode

from django.urls import reverse

from financeiro.models import Ambito, Categoria, NaturezaEvento
from financeiro.services.dashboard import (
    ContribuicaoAnalitica,
    _categoria_superior,
    _saldo_contas,
    carregar_contribuicoes_dashboard,
)
from financeiro.services.cobertura_credito import consultar_cobertura_credito
from financeiro.services.lancamentos import LinhaLancamento, _linha_movimento, _linha_parcela


ZERO = Decimal("0.00")
TIPOS_DETALHAMENTO = {"essentiality", "category", "category_nature", "controllable"}
ESSENCIALIDADES_CONTROLAVEIS = {
    Categoria.Essencialidade.ESTILO_VIDA,
    Categoria.Essencialidade.SUPERFLUO,
}


class DetalhamentoDashboardInvalido(ValueError):
    pass


def _resolver_indicador(tipo: str, valor: str, categorias: dict[int, Categoria]) -> dict:
    if tipo not in TIPOS_DETALHAMENTO:
        raise DetalhamentoDashboardInvalido("Tipo de detalhamento inválido.")

    if tipo == "essentiality":
        rotulos = dict(Categoria.Essencialidade.choices)
        if valor not in rotulos or valor == Categoria.Essencialidade.NEUTRO:
            raise DetalhamentoDashboardInvalido("Essencialidade inválida.")
        return {"tipo": tipo, "valor": valor, "rotulo": rotulos[valor]}

    if tipo == "category_nature":
        rotulos = dict(Categoria.Natureza.choices)
        if valor not in rotulos:
            raise DetalhamentoDashboardInvalido("Natureza de categoria inválida.")
        return {"tipo": tipo, "valor": valor, "rotulo": rotulos[valor]}

    if tipo == "controllable":
        if valor not in (None, ""):
            raise DetalhamentoDashboardInvalido("Gastos controláveis não recebem valor.")
        return {"tipo": tipo, "valor": "", "rotulo": "Gastos controláveis"}

    if valor == "unclassified":
        return {"tipo": tipo, "valor": valor, "rotulo": "A classificar", "categoria_id": None}
    if not str(valor or "").isdigit():
        raise DetalhamentoDashboardInvalido("Categoria inválida.")
    categoria = categorias.get(int(valor))
    if not categoria or _categoria_superior(categoria, categorias).pk != categoria.pk:
        raise DetalhamentoDashboardInvalido("Categoria inválida.")
    return {
        "tipo": tipo,
        "valor": str(categoria.pk),
        "rotulo": categoria.nome,
        "categoria_id": categoria.pk,
    }


def _contribuicao_compativel(
    contribuicao: ContribuicaoAnalitica,
    indicador: dict,
    categorias: dict[int, Categoria],
    ambito: str = "all",
) -> bool:
    if contribuicao.natureza_evento not in {NaturezaEvento.DESPESA, NaturezaEvento.ESTORNO}:
        return False
    ambito_contribuicao = (
        contribuicao.registro.evento.ambito
        if contribuicao.origem == "CONTA"
        else contribuicao.registro.ambito_exibicao
    )
    if ambito != "all" and ambito_contribuicao != ambito:
        return False

    categoria = contribuicao.categoria
    tipo = indicador["tipo"]
    if tipo == "essentiality":
        return bool(categoria and categoria.essencialidade == indicador["valor"])
    if tipo == "category_nature":
        return bool(categoria and categoria.natureza == indicador["valor"])
    if tipo == "controllable":
        return bool(categoria and categoria.essencialidade in ESSENCIALIDADES_CONTROLAVEIS)
    if indicador["categoria_id"] is None:
        return categoria is None
    return bool(
        categoria
        and _categoria_superior(categoria, categorias).pk == indicador["categoria_id"]
    )


def _linha_detalhamento(contribuicoes: list[ContribuicaoAnalitica]) -> LinhaLancamento | None:
    registro = contribuicoes[0].registro
    linha_base = _linha_movimento(registro) if contribuicoes[0].origem == "CONTA" else _linha_parcela(registro)
    valor = sum((item.valor_indicador for item in contribuicoes), ZERO)
    if valor == ZERO:
        return None
    categorias = {
        item.categoria.pk: item.categoria.nome
        for item in contribuicoes
        if item.categoria
    }
    categoria = (
        ", ".join(nome for _, nome in sorted(categorias.items(), key=lambda item: item[1].casefold()))
        if categorias
        else "A classificar"
    )
    return replace(
        linha_base,
        categoria=categoria,
        categoria_ids=tuple(categorias),
        valor=valor,
    )


def _url_lancamentos(
    dados: dict,
    indicador: dict,
    ambito: str = "all",
    computacao: str = "COMPUTA",
) -> str:
    filtros = dados["filtros"]
    parametros = {
        "date_from": dados["data_inicial"].strftime("%d/%m/%Y"),
        "date_to": dados["data_final"].strftime("%d/%m/%Y"),
        "account": filtros["conta_selecionada"].pk if filtros["conta_selecionada"] else "all",
        "card": filtros["cartao_selecionado"].pk if filtros["cartao_selecionado"] else "all",
        "origin": {"account": "CONTA", "card": "CARTAO"}.get(dados["origem"], "all"),
        "analysis_type": indicador["tipo"],
        "analysis_value": indicador["valor"],
        "analysis_nature": dados["natureza"],
        "computes": computacao,
    }
    if ambito != "all":
        parametros["analysis_scope"] = ambito
    if dados["aplicar_composicao_dashboard"]:
        parametros["dashboard_context"] = "1"
    if indicador["tipo"] == "essentiality":
        parametros["essentiality"] = indicador["valor"]
    elif indicador["tipo"] == "category" and indicador["categoria_id"] is not None:
        parametros["parent_category"] = indicador["categoria_id"]
    elif indicador["tipo"] == "category_nature":
        parametros["category_nature"] = indicador["valor"]
    return f"{reverse('eventos')}?{urlencode(parametros)}"


def consultar_detalhamento_dashboard(
    usuario,
    tipo,
    valor="",
    periodo=None,
    conta_id=None,
    cartao_id=None,
    origem="all",
    natureza="all",
    computacao="COMPUTA",
    scope="all",
    date_from=None,
    date_to=None,
    dashboard_context=False,
) -> dict:
    scope = scope if scope in Ambito.values else "all"
    if computacao not in {"COMPUTA", "NAO_COMPUTA"}:
        raise DetalhamentoDashboardInvalido("Escopo de resultado inválido.")
    if tipo == "credit_coverage":
        dados = carregar_contribuicoes_dashboard(
            usuario,
            periodo=periodo,
            conta_id=conta_id,
            cartao_id=cartao_id,
            origem="all",
            natureza=natureza,
            date_from=date_from,
            date_to=date_to,
            aplicar_composicao_dashboard=dashboard_context,
        )
        cobertura = consultar_cobertura_credito(
            usuario,
            data_inicial=dados["data_inicial"],
            data_final=dados["data_final"],
            contas=dados["contas_filtradas"],
            cartoes=dados["filtros"]["cartoes_filtrados"],
            saldo_inicial=_saldo_contas(
                usuario,
                dados["contas_filtradas"],
                dados["data_inicial"],
            ),
            natureza=dados["natureza"],
        )
        return {
            "detalhamento_cobertura": True,
            "titulo_detalhamento": "Cobertura dos gastos do mês",
            **cobertura,
        }
    dados = carregar_contribuicoes_dashboard(
        usuario,
        periodo=periodo,
        conta_id=conta_id,
        cartao_id=cartao_id,
        origem=origem,
        natureza=natureza,
        computacao=computacao,
        date_from=date_from,
        date_to=date_to,
        aplicar_composicao_dashboard=dashboard_context,
    )
    indicador = _resolver_indicador(str(tipo or ""), str(valor or ""), dados["categorias"])
    agrupadas = defaultdict(list)
    for contribuicao in dados["contribuicoes"]:
        if _contribuicao_compativel(contribuicao, indicador, dados["categorias"], scope):
            agrupadas[(contribuicao.origem, contribuicao.registro.pk)].append(contribuicao)

    linhas = [
        linha
        for contribuicoes in agrupadas.values()
        if (linha := _linha_detalhamento(contribuicoes)) is not None
    ]
    linhas.sort(
        key=lambda item: (
            item.data_original,
            item.evento_id,
            item.origem,
            item.registro_id,
        )
    )
    total = sum((linha.valor for linha in linhas), ZERO)
    return {
        "titulo_detalhamento": indicador["rotulo"],
        "analysis_type": indicador["tipo"],
        "analysis_value": indicador["valor"],
        "analysis_nature": dados["natureza"],
        "analysis_scope": scope,
        "computes": computacao,
        "linhas": linhas,
        "total_registros": len(linhas),
        "total_indicador": total,
        "data_inicial": dados["data_inicial"],
        "data_final": dados["data_final"],
        "lancamentos_url": _url_lancamentos(dados, indicador, scope, computacao),
    }
