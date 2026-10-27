from __future__ import annotations

from dataclasses import dataclass, replace
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch, Q
from django.urls import reverse
from django.utils import timezone

from financeiro.models import (
    Ambito,
    Categoria,
    EventoFinanceiro,
    Fatura,
    ItemImportacao,
    Liquidacao,
    MovimentoConta,
    NaturezaEvento,
    ParcelaCartao,
    Pessoa,
    RateioCategoria,
    SerieLancamentoRecorrente,
    Situacao,
    Titulo,
)
from financeiro.services.cartoes_faturas import STATUS_FATURA_BLOQUEADA, criar_compra_cartao, editar_parcela_cartao
from financeiro.services.dashboard import limites_periodo
from financeiro.services.filtros_financeiros import CICLO_PATTERN, resolver_filtros_financeiros
from financeiro.services.importacao_ofx import recalcular_fatura
from financeiro.services.saldo_contas import reprocessar_saldo_conta
from financeiro.services.titulos import liquidar_titulo
from financeiro.vencimentos_cartao import resolver_vencimento_fatura


ZERO = Decimal("0.00")
ROTULOS_NATUREZA = dict(NaturezaEvento.choices)
ROTULOS_AMBITO = {
    Ambito.NAO_DEFINIDO: "Não definido",
    Ambito.PESSOAL: "Pessoal",
    Ambito.PROFISSIONAL: "Profissional",
}
ROTULOS_SITUACAO = dict(Situacao.choices)


def _data_filtro(valor) -> date | None:
    if not valor:
        return None
    texto = str(valor).strip()
    for formato in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class LinhaLancamento:
    origem: str
    registro_id: int
    evento_id: int
    data_original: date
    descricao: str
    pessoa: str
    pessoa_id: int | None
    ambito: str
    origem_nome: str
    origem_tipo: str
    categoria: str
    categoria_ids: tuple[int, ...]
    natureza_valor: str
    situacao: str
    situacao_valor: str
    valor: Decimal
    documento: str
    parcela_numero: int | None
    parcela_total: int | None
    editar_url: str
    excluir_url: str
    clonar_url: str
    vencimento_fatura: date | None = None
    ciclo_fatura: str = ""
    observacao: str = ""
    recorrencia_numero: int | None = None
    recorrencia_total: int | None = None
    recorrencia_futuros: int = 0
    fatura_id: int | None = None
    fatura_status: str = ""
    agrupada_fatura: bool = False
    quantidade_lancamentos_agrupados: int = 0

    @property
    def valor_positivo(self) -> bool:
        return self.valor > ZERO

    @property
    def possui_parcela(self) -> bool:
        return bool(self.parcela_numero and self.parcela_total and self.parcela_total > 1)

    @property
    def data_referencia(self) -> date:
        if self.origem == "CARTAO" and self.vencimento_fatura:
            return self.vencimento_fatura
        return self.data_original


def _valor_assinado(natureza: str, valor: Decimal) -> Decimal:
    if natureza in {NaturezaEvento.DESPESA, NaturezaEvento.PAGAMENTO_FATURA}:
        return -abs(valor)
    return abs(valor)


def _totalizar_linhas(linhas: list[LinhaLancamento]) -> tuple[Decimal, Decimal]:
    total_creditos = ZERO
    total_debitos = ZERO
    for linha in linhas:
        valor = (
            linha.valor
            if linha.natureza_valor == NaturezaEvento.TRANSFERENCIA
            else _valor_assinado(linha.natureza_valor, linha.valor)
        )
        if valor > ZERO:
            total_creditos += valor
        elif valor < ZERO:
            total_debitos += abs(valor)
    return total_creditos, total_debitos


def _nome_cadastro(nome: str, ativo: bool) -> str:
    return nome if ativo else f"{nome} (inativo)"


def _nome_pessoa_evento(evento: EventoFinanceiro) -> str:
    if evento.pessoa_id:
        return evento.pessoa.nome
    return evento.pessoa_nome_livre or "Pessoa não informada"


def _nome_pessoa_parcela(parcela: ParcelaCartao) -> str:
    if parcela.pessoa_id:
        return parcela.pessoa.nome
    if parcela.pessoa_nome_livre:
        return parcela.pessoa_nome_livre
    return _nome_pessoa_evento(parcela.compra.evento)


def _pessoa_id_parcela(parcela: ParcelaCartao) -> int | None:
    if parcela.pessoa_id:
        return parcela.pessoa_id
    if parcela.pessoa_nome_livre:
        return None
    return parcela.compra.evento.pessoa_id


def _categorias_evento(evento: EventoFinanceiro) -> tuple[str, tuple[int, ...]]:
    categorias = [rateio.categoria for rateio in evento.rateios.all()]
    if not categorias:
        return "A classificar", ()
    unicas = {categoria.pk: categoria.nome for categoria in categorias}
    nomes = ", ".join(nome for _, nome in sorted(unicas.items(), key=lambda item: item[1].casefold()))
    return nomes, tuple(unicas)


def _documento_importado(objeto) -> str:
    itens = getattr(objeto, "itens_origem", None)
    if itens is None and hasattr(objeto, "itemimportacao_set"):
        itens = objeto.itemimportacao_set.all()
    for item in itens or ():
        if item.documento:
            return item.documento
    return ""


def _liquidacao_fatura(movimento: MovimentoConta | None) -> Liquidacao | None:
    if not movimento:
        return None
    liquidacoes = [
        liquidacao
        for liquidacao in movimento.liquidacao_set.all()
        if liquidacao.status == Liquidacao.Status.ATIVA and liquidacao.titulo.fatura_id
    ]
    return min(liquidacoes, key=lambda item: item.pk, default=None)


def _linha_movimento(movimento: MovimentoConta) -> LinhaLancamento:
    evento = movimento.evento
    categoria, categoria_ids = _categorias_evento(evento)
    tipo = ROTULOS_NATUREZA.get(evento.natureza, evento.natureza)
    return LinhaLancamento(
        origem="CONTA",
        registro_id=movimento.pk,
        evento_id=evento.pk,
        data_original=evento.data,
        descricao=evento.descricao,
        pessoa=_nome_pessoa_evento(evento),
        pessoa_id=evento.pessoa_id,
        ambito=ROTULOS_AMBITO.get(evento.ambito, evento.ambito or "Não definido"),
        origem_nome=_nome_cadastro(movimento.conta.nome, movimento.conta.ativa),
        origem_tipo=f"Conta • {tipo}",
        categoria=categoria,
        categoria_ids=categoria_ids,
        natureza_valor=evento.natureza,
        situacao=ROTULOS_SITUACAO.get(evento.situacao, evento.situacao),
        situacao_valor=evento.situacao,
        valor=movimento.valor,
        documento=movimento.documento or _documento_importado(movimento),
        parcela_numero=None,
        parcela_total=None,
        editar_url=reverse("evento_editar", args=[evento.pk]),
        excluir_url=reverse("evento_excluir", args=[evento.pk]),
        clonar_url=reverse("evento_clonar", args=[evento.pk]),
        observacao=(evento.observacao or "").strip(),
        recorrencia_numero=evento.numero_recorrencia,
        recorrencia_total=evento.serie_recorrente.quantidade_ocorrencias if evento.serie_recorrente_id else None,
        recorrencia_futuros=contar_recorrencias_futuras(evento),
    )


def _linha_previsao(titulo: Titulo) -> LinhaLancamento:
    evento = titulo.evento
    categoria, categoria_ids = _categorias_evento(evento)
    tipo = ROTULOS_NATUREZA.get(evento.natureza, evento.natureza)
    conta_nome = _nome_cadastro(titulo.conta.nome, titulo.conta.ativa) if titulo.conta_id else "Conta não informada"
    return LinhaLancamento(
        origem="CONTA",
        registro_id=titulo.pk,
        evento_id=evento.pk,
        data_original=evento.data,
        descricao=evento.descricao,
        pessoa=_nome_pessoa_evento(evento),
        pessoa_id=evento.pessoa_id,
        ambito=ROTULOS_AMBITO.get(evento.ambito, evento.ambito or "Não definido"),
        origem_nome=conta_nome,
        origem_tipo=f"Conta • {tipo}",
        categoria=categoria,
        categoria_ids=categoria_ids,
        natureza_valor=evento.natureza,
        situacao="Previsto",
        situacao_valor=Situacao.PREVISTO,
        valor=_valor_assinado(evento.natureza, titulo.valor_original),
        documento=_documento_importado(evento),
        parcela_numero=None,
        parcela_total=None,
        editar_url=reverse("evento_editar", args=[evento.pk]),
        excluir_url=reverse("evento_excluir", args=[evento.pk]),
        clonar_url=reverse("evento_clonar", args=[evento.pk]),
        observacao=(evento.observacao or "").strip(),
        recorrencia_numero=evento.numero_recorrencia,
        recorrencia_total=evento.serie_recorrente.quantidade_ocorrencias if evento.serie_recorrente_id else None,
        recorrencia_futuros=contar_recorrencias_futuras(evento),
    )


def _linha_parcela(parcela: ParcelaCartao) -> LinhaLancamento:
    evento = parcela.compra.evento
    natureza = parcela.natureza_exibicao
    categoria = parcela.categoria.nome if parcela.categoria_id else "A classificar"
    situacao = (
        "Previsto"
        if parcela.situacao == Situacao.PREVISTO
        else "Cobrado em fatura"
        if parcela.situacao == Situacao.REALIZADO
        else ROTULOS_SITUACAO.get(parcela.situacao, parcela.situacao)
    )
    return LinhaLancamento(
        origem="CARTAO",
        registro_id=parcela.pk,
        evento_id=evento.pk,
        data_original=parcela.data_original_exibicao,
        descricao=parcela.descricao_exibicao,
        pessoa=_nome_pessoa_parcela(parcela),
        pessoa_id=_pessoa_id_parcela(parcela),
        ambito=ROTULOS_AMBITO.get(parcela.ambito_exibicao, parcela.ambito_exibicao or "Não definido"),
        origem_nome=_nome_cadastro(parcela.fatura.cartao.nome, parcela.fatura.cartao.ativo),
        origem_tipo=f"Cartão • {ROTULOS_NATUREZA.get(natureza, natureza)}",
        categoria=categoria,
        categoria_ids=(parcela.categoria_id,) if parcela.categoria_id else (),
        natureza_valor=natureza,
        situacao=situacao,
        situacao_valor=parcela.situacao,
        valor=_valor_assinado(natureza, parcela.valor),
        documento=_documento_importado(parcela),
        parcela_numero=parcela.numero,
        parcela_total=parcela.total,
        editar_url=reverse("parcela_cartao_editar", args=[parcela.pk]),
        excluir_url=reverse("parcela_lancamento_excluir", args=[parcela.pk]),
        clonar_url=reverse("evento_clonar", args=[evento.pk]),
        vencimento_fatura=_vencimento_referencia_parcela(parcela),
        ciclo_fatura=_rotulo_ciclo_fatura(parcela.fatura.ciclo),
        fatura_id=parcela.fatura_id,
        fatura_status=parcela.fatura.status,
        observacao=(evento.observacao or "").strip(),
        recorrencia_numero=evento.numero_recorrencia,
        recorrencia_total=evento.serie_recorrente.quantidade_ocorrencias if evento.serie_recorrente_id else None,
        recorrencia_futuros=contar_recorrencias_futuras(evento),
    )


def _rotulo_ciclo_fatura(ciclo: str) -> str:
    try:
        ano, mes = (int(item) for item in ciclo.split("-", 1))
        return f"{mes:02d}/{ano:04d}"
    except (AttributeError, TypeError, ValueError):
        return ciclo or ""


def _vencimento_referencia_parcela(parcela: ParcelaCartao) -> date | None:
    return resolver_vencimento_fatura(parcela.fatura)


def _agrupar_linhas_cartao_por_fatura(linhas: list[LinhaLancamento]) -> list[LinhaLancamento]:
    grupos: dict[int, list[LinhaLancamento]] = {}
    resultado: list[LinhaLancamento] = []
    for linha in linhas:
        if linha.origem != "CARTAO" or not linha.fatura_id:
            resultado.append(linha)
            continue
        grupos.setdefault(linha.fatura_id, []).append(linha)

    for grupo in grupos.values():
        referencia = grupo[0]
        resultado.append(
            replace(
                referencia,
                data_original=referencia.data_referencia,
                descricao=f"Fatura {referencia.origem_nome} • {referencia.ciclo_fatura}",
                pessoa="",
                pessoa_id=None,
                ambito="",
                origem_tipo="Cartão • Lançamentos agrupados",
                categoria="Lançamentos de cartão",
                categoria_ids=(),
                situacao=dict(Fatura.Status.choices).get(referencia.fatura_status, referencia.fatura_status),
                valor=sum((linha.valor for linha in grupo), ZERO),
                documento="",
                parcela_numero=None,
                parcela_total=None,
                editar_url="",
                excluir_url="",
                clonar_url="",
                observacao="",
                recorrencia_numero=None,
                recorrencia_total=None,
                recorrencia_futuros=0,
                agrupada_fatura=True,
                quantidade_lancamentos_agrupados=len(grupo),
            )
        )
    return resultado


def consultar_lancamentos(
    usuario,
    periodo=None,
    conta_id=None,
    cartao_id=None,
    busca="",
    origem="all",
    categoria="all",
    situacao="all",
    data_inicial=None,
    data_final=None,
    categoria_superior="all",
    natureza="all",
    natureza_categoria="all",
    essencialidade="all",
    computa_resultado="all",
    pessoa="all",
    analysis_type="",
    analysis_value="",
    analysis_nature="all",
    analysis_scope="all",
    group_card_invoices=False,
    card_purchase_date=False,
    dashboard_context=False,
) -> dict:
    origem = origem if origem in {"all", "CONTA", "CARTAO"} else "all"
    situacao = situacao if situacao in {"all", Situacao.REALIZADO, Situacao.PREVISTO} else "all"
    periodo_filtro = periodo if CICLO_PATTERN.fullmatch(str(periodo or "")) else ""
    filtros = resolver_filtros_financeiros(
        usuario,
        periodo_filtro or None,
        conta_id,
        cartao_id,
        aplicar_composicao_dashboard=dashboard_context,
    )
    if data_inicial is None and data_final is None:
        hoje = timezone.localdate()
        inicio, fim_exclusivo = limites_periodo(hoje.strftime("%Y-%m"))
        fim = fim_exclusivo - timedelta(days=1)
    else:
        inicio = _data_filtro(data_inicial)
        fim = _data_filtro(data_final)
    if inicio and fim and inicio > fim:
        inicio, fim = None, None
    conta = filtros["conta_selecionada"]
    cartao = filtros["cartao_selecionado"]
    card_purchase_date = str(card_purchase_date) == "1"
    categorias = list(
        Categoria.objects.filter(usuario=usuario)
        .select_related("categoria_pai")
        .order_by("nome", "id")
    )
    categorias_superiores = [item for item in categorias if item.categoria_pai_id is None]
    categorias_com_superior = [item for item in categorias if item.categoria_pai_id is not None]
    categorias_por_id = {item.pk: item for item in categorias}
    pessoas = list(Pessoa.objects.filter(usuario=usuario).order_by("nome", "id"))

    natureza = natureza if natureza in dict(NaturezaEvento.choices) else "all"
    natureza_categoria = natureza_categoria if natureza_categoria in dict(Categoria.Natureza.choices) else "all"
    essencialidade = essencialidade if essencialidade in dict(Categoria.Essencialidade.choices) else "all"
    computa_resultado = computa_resultado if computa_resultado in {"all", "COMPUTA", "NAO_COMPUTA"} else "all"
    categoria_id = int(categoria) if str(categoria).isdigit() and int(categoria) in categorias_por_id else None
    categoria_superior_id = (
        int(categoria_superior)
        if str(categoria_superior).isdigit() and int(categoria_superior) in categorias_por_id
        else None
    )
    pessoa_id = int(pessoa) if str(pessoa).isdigit() and any(item.pk == int(pessoa) for item in pessoas) else None

    categorias_hierarquia = set()
    if categoria_superior_id:
        categorias_hierarquia.add(categoria_superior_id)
        fronteira = {categoria_superior_id}
        while fronteira:
            filhos = {item.pk for item in categorias if item.categoria_pai_id in fronteira}
            filhos -= categorias_hierarquia
            categorias_hierarquia.update(filhos)
            fronteira = filhos
    linhas: list[LinhaLancamento] = []

    itens_importacao = ItemImportacao.objects.order_by("importacao_id", "posicao", "id")
    if origem != "CARTAO":
        movimentos = (
            MovimentoConta.objects.filter(
                conta__usuario=usuario,
                evento__situacao=Situacao.REALIZADO,
            )
            .select_related("conta", "evento__pessoa")
            .prefetch_related(
                "evento__rateios__categoria",
                Prefetch("itemimportacao_set", queryset=itens_importacao, to_attr="itens_origem"),
                "liquidacao_set__titulo__fatura__cartao",
            )
        )
        if dashboard_context:
            movimentos = movimentos.filter(conta__nao_compoe_dashboard=False)
        if periodo_filtro:
            periodo_inicio, periodo_fim = limites_periodo(periodo_filtro)
            movimentos = movimentos.filter(evento__data__gte=periodo_inicio, evento__data__lt=periodo_fim)
        if inicio:
            movimentos = movimentos.filter(evento__data__gte=inicio)
        if fim:
            movimentos = movimentos.filter(evento__data__lte=fim)
        if conta:
            movimentos = movimentos.filter(conta=conta)
        for movimento in movimentos:
            liquidacao = _liquidacao_fatura(movimento)
            if conta and not cartao and liquidacao:
                continue
            if cartao and (not liquidacao or liquidacao.titulo.fatura.cartao_id != cartao.pk):
                continue
            linhas.append(_linha_movimento(movimento))

        if not cartao:
            titulos = (
                Titulo.objects.filter(
                    usuario=usuario,
                    evento__isnull=False,
                    ativo=True,
                )
                .select_related("conta", "evento__pessoa")
                .prefetch_related("evento__rateios__categoria", "evento__itemimportacao_set")
            )
            if dashboard_context:
                titulos = titulos.filter(conta__nao_compoe_dashboard=False)
            if periodo_filtro:
                periodo_inicio, periodo_fim = limites_periodo(periodo_filtro)
                titulos = titulos.filter(evento__data__gte=periodo_inicio, evento__data__lt=periodo_fim)
            if inicio:
                titulos = titulos.filter(evento__data__gte=inicio)
            if fim:
                titulos = titulos.filter(evento__data__lte=fim)
            if conta:
                titulos = titulos.filter(conta=conta)
            linhas.extend(_linha_previsao(titulo) for titulo in titulos)

    if origem != "CONTA" and (not conta or cartao):
        cartoes_ids = [item.pk for item in filtros["cartoes_filtrados"]]
        parcelas = (
            ParcelaCartao.objects.filter(
                compra__usuario=usuario,
                fatura__cartao_id__in=cartoes_ids,
            )
            .exclude(
                Q(natureza=NaturezaEvento.PAGAMENTO_FATURA)
                | Q(natureza="", compra__evento__natureza=NaturezaEvento.PAGAMENTO_FATURA)
            )
            .select_related("compra__evento__pessoa", "fatura__cartao", "categoria", "pessoa")
            .prefetch_related(Prefetch("itemimportacao_set", queryset=itens_importacao, to_attr="itens_origem"))
        )
        if periodo_filtro:
            parcelas = parcelas.filter(fatura__ciclo=periodo_filtro)
        if inicio:
            if card_purchase_date:
                parcelas = parcelas.filter(
                    Q(data_original__gte=inicio)
                    | Q(data_original__isnull=True, compra__data_compra__gte=inicio)
                )
            else:
                parcelas = parcelas.filter(vencimento__gte=inicio)
        if fim:
            if card_purchase_date:
                parcelas = parcelas.filter(
                    Q(data_original__lte=fim)
                    | Q(data_original__isnull=True, compra__data_compra__lte=fim)
                )
            else:
                parcelas = parcelas.filter(vencimento__lte=fim)
        linhas.extend(_linha_parcela(parcela) for parcela in parcelas)

    detalhe_analitico = None
    if analysis_type:
        from financeiro.services.detalhamento_dashboard import consultar_detalhamento_dashboard

        detalhe_analitico = consultar_detalhamento_dashboard(
            usuario,
            tipo=analysis_type,
            valor=analysis_value,
            periodo=periodo_filtro or None,
            conta_id=conta_id,
            cartao_id=cartao_id,
            origem={"CONTA": "account", "CARTAO": "card"}.get(origem, "all"),
            natureza=analysis_nature,
            computacao=computa_resultado if computa_resultado != "all" else "COMPUTA",
            scope=analysis_scope,
            date_from=inicio,
            date_to=fim,
            dashboard_context=dashboard_context,
        )
        linhas = list(detalhe_analitico["linhas"])

    busca_normalizada = busca.strip().casefold()
    filtros_categoria_ativos = any(
        (
            categoria_id,
            categoria_superior_id,
            natureza_categoria != "all",
            essencialidade != "all",
            computa_resultado != "all",
        )
    )

    def categoria_compativel(item: Categoria) -> bool:
        if categoria_id and item.pk != categoria_id:
            return False
        if categoria_superior_id and item.pk not in categorias_hierarquia:
            return False
        if natureza_categoria != "all" and item.natureza != natureza_categoria:
            return False
        if essencialidade != "all" and item.essencialidade != essencialidade:
            return False
        if computa_resultado == "COMPUTA" and not item.computa_resultado:
            return False
        if computa_resultado == "NAO_COMPUTA" and item.computa_resultado:
            return False
        return True

    filtradas = []
    for linha in linhas:
        categorias_linha = [categorias_por_id[item_id] for item_id in linha.categoria_ids if item_id in categorias_por_id]
        if filtros_categoria_ativos and not any(categoria_compativel(item) for item in categorias_linha):
            continue
        if natureza != "all" and linha.natureza_valor != natureza:
            continue
        if pessoa_id and linha.pessoa_id != pessoa_id:
            continue
        if situacao != "all" and linha.situacao_valor != situacao:
            continue
        if busca_normalizada:
            texto = " ".join((linha.descricao, linha.pessoa, linha.categoria, linha.documento)).casefold()
            if busca_normalizada not in texto:
                continue
        filtradas.append(linha)
    filtradas.sort(
        key=lambda item: (
            item.data_original,
            item.evento_id,
            item.origem,
            item.registro_id,
        )
    )
    total_creditos, total_debitos = _totalizar_linhas(filtradas)
    group_card_invoices = str(group_card_invoices) == "1"
    linhas_exibidas = _agrupar_linhas_cartao_por_fatura(filtradas) if group_card_invoices else filtradas
    linhas_exibidas.sort(
        key=lambda item: (
            item.data_original,
            item.evento_id,
            item.origem,
            item.registro_id,
        )
    )

    parametros_filtro = {
        "period": periodo_filtro,
        "date_from": inicio.strftime("%d/%m/%Y") if inicio else "",
        "date_to": fim.strftime("%d/%m/%Y") if fim else "",
        "account": conta.pk if conta else "all",
        "card": cartao.pk if cartao else "all",
        "q": busca,
        "origin": origem,
        "category": categoria_id or "all",
        "parent_category": categoria_superior_id or "all",
        "nature": natureza,
        "category_nature": natureza_categoria,
        "essentiality": essencialidade,
        "computes": computa_resultado,
        "person": pessoa_id or "all",
        "status": situacao,
        "analysis_type": analysis_type,
        "analysis_value": analysis_value,
        "analysis_nature": analysis_nature,
        "analysis_scope": analysis_scope,
    }
    if group_card_invoices:
        parametros_filtro["group_card_invoices"] = "1"
    if card_purchase_date:
        parametros_filtro["card_purchase_date"] = "1"
    if dashboard_context:
        parametros_filtro["dashboard_context"] = "1"

    return {
        **{chave: valor for chave, valor in filtros.items() if chave not in {"cartoes_filtrados", "periodo_selecionado", "periodo_rotulo"}},
        "periodo_selecionado": periodo_filtro,
        "periodo_rotulo": periodo_filtro[5:7] + "/" + periodo_filtro[:4] if periodo_filtro else "Todos os ciclos",
        "linhas": linhas_exibidas,
        "total_registros": len(linhas_exibidas),
        "total_creditos": total_creditos,
        "total_debitos": total_debitos,
        "categorias": categorias_com_superior,
        "categorias_superiores": categorias_superiores,
        "pessoas": pessoas,
        "busca": busca,
        "data_inicial": inicio,
        "data_final": fim,
        "origem_filtro": origem,
        "categoria_filtro": str(categoria_id or "all"),
        "categoria_superior_filtro": str(categoria_superior_id or "all"),
        "natureza_filtro": natureza,
        "natureza_categoria_filtro": natureza_categoria,
        "essencialidade_filtro": essencialidade,
        "computa_resultado_filtro": computa_resultado,
        "pessoa_filtro": str(pessoa_id or "all"),
        "situacao_filtro": situacao,
        "naturezas_lancamento": NaturezaEvento.choices,
        "naturezas_categoria": Categoria.Natureza.choices,
        "essencialidades": Categoria.Essencialidade.choices,
        "analysis_active": bool(detalhe_analitico),
        "analysis_type": analysis_type,
        "analysis_value": analysis_value,
        "analysis_nature": analysis_nature,
        "analysis_scope": analysis_scope,
        "analysis_label": detalhe_analitico["titulo_detalhamento"] if detalhe_analitico else "",
        "total_indicador": sum((linha.valor for linha in filtradas), ZERO) if detalhe_analitico else ZERO,
        "group_card_invoices": group_card_invoices,
        "card_purchase_date": card_purchase_date,
        "dashboard_context": dashboard_context,
        "filtros_query": urlencode(parametros_filtro),
    }


def _titulo_fatura(usuario, cartao, ciclo: str, vencimento) -> Titulo:
    try:
        fatura = Fatura.objects.select_for_update(of=("self",)).select_related("cartao__conta_pagamento_padrao").get(
            usuario=usuario, cartao=cartao, ciclo=ciclo
        )
    except Fatura.DoesNotExist as exc:
        raise ValidationError("A fatura informada não foi encontrada.") from exc
    titulo = Titulo.objects.select_for_update().filter(usuario=usuario, fatura=fatura).first()
    if titulo:
        return titulo
    if fatura.status not in {Fatura.Status.FECHADA, Fatura.Status.PARCIAL, Fatura.Status.PAGA}:
        raise ValidationError("A fatura deve ser fechada antes de registrar seu pagamento.")
    total_exigido = fatura.total_exigido or fatura.total
    if total_exigido <= ZERO:
        raise ValidationError("A fatura não possui saldo para pagamento.")
    return Titulo.objects.create(
        usuario=usuario, tipo=Titulo.Tipo.PAGAR,
        descricao=f"Fatura {fatura.cartao.nome} {fatura.ciclo}",
        vencimento=fatura.vencimento or vencimento,
        valor_original=total_exigido, saldo_aberto=total_exigido,
        conta=fatura.cartao.conta_pagamento_padrao, fatura=fatura,
    )


def _adicionar_meses(data_base: date, meses: int) -> date:
    indice = data_base.year * 12 + (data_base.month - 1) + meses
    ano, mes_zero = divmod(indice, 12)
    mes = mes_zero + 1
    return date(ano, mes, min(data_base.day, monthrange(ano, mes)[1]))


@transaction.atomic
def criar_lancamento(usuario, dados: dict) -> EventoFinanceiro:
    if dados.get("modo_lancamento") == "RECORRENTE":
        return criar_lancamentos_recorrentes(usuario, dados)
    return _criar_lancamento_individual(usuario, dados)


@transaction.atomic
def criar_lancamentos_recorrentes(usuario, dados: dict) -> EventoFinanceiro:
    quantidade = dados.get("quantidade_repeticoes") or 1
    intervalo = dados.get("intervalo_meses") or 1
    if dados["origem"] == "CONTA" and quantidade > 1 and not dados.get("vencimento"):
        raise ValidationError("Informe o vencimento da recorrência para gerar as previsões futuras.")
    serie = SerieLancamentoRecorrente.objects.create(
        usuario=usuario,
        quantidade_ocorrencias=quantidade,
        intervalo_meses=intervalo,
    )
    primeiro = None
    for indice in range(quantidade):
        ocorrencia = dict(dados)
        deslocamento = indice * intervalo
        ocorrencia["modo_lancamento"] = "UNICO"
        ocorrencia["quantidade_parcelas"] = 1
        ocorrencia["quantidade_repeticoes"] = 1
        ocorrencia["intervalo_meses"] = 1
        ocorrencia["data"] = _adicionar_meses(dados["data"], deslocamento)
        if dados.get("vencimento"):
            ocorrencia["vencimento"] = _adicionar_meses(dados["vencimento"], deslocamento)
        if dados["origem"] == "CARTAO":
            from financeiro.services.importacao_ofx import proximo_ciclo

            ocorrencia["ciclo"] = proximo_ciclo(dados["ciclo"], deslocamento)
        if indice:
            ocorrencia["situacao"] = Situacao.PREVISTO
        evento = _criar_lancamento_individual(usuario, ocorrencia)
        evento.serie_recorrente = serie
        evento.numero_recorrencia = indice + 1
        evento.save(update_fields=["serie_recorrente", "numero_recorrencia", "atualizado_em"])
        if primeiro is None:
            primeiro = evento
    return primeiro


def _criar_lancamento_individual(usuario, dados: dict) -> EventoFinanceiro:
    if dados["origem"] == "CARTAO":
        compra = criar_compra_cartao(usuario, {
            "descricao": dados["descricao"], "natureza": dados["natureza"],
            "data_original": dados["data"], "valor": dados["valor"],
            "cartao": dados["cartao"], "categoria": dados.get("categoria"),
            "pessoa": dados.get("pessoa"), "ambito": dados["ambito"],
            "pessoa_nome_livre": dados.get("pessoa_nome_livre", ""),
            "observacao": dados.get("observacao", ""),
            "situacao": dados["situacao"], "vencimento": dados.get("vencimento"),
            "ciclo": dados["ciclo"], "quantidade_parcelas": dados.get("quantidade_parcelas") or 1,
        })
        return compra.evento

    evento = EventoFinanceiro.objects.create(
        usuario=usuario, natureza=dados["natureza"], descricao=dados["descricao"],
        data=dados["data"], vencimento=dados.get("vencimento"), valor=dados["valor"],
        pessoa=dados.get("pessoa"), pessoa_nome_livre=dados.get("pessoa_nome_livre", ""),
        ambito=dados["ambito"], situacao=dados["situacao"], observacao=dados.get("observacao", ""),
    )
    if dados.get("categoria"):
        RateioCategoria.objects.create(evento=evento, categoria=dados["categoria"], valor=dados["valor"])
    if dados["situacao"] == Situacao.PREVISTO:
        tipo = Titulo.Tipo.RECEBER if dados["natureza"] in {NaturezaEvento.RECEITA, NaturezaEvento.ESTORNO} else Titulo.Tipo.PAGAR
        Titulo.objects.create(
            usuario=usuario, tipo=tipo, descricao=dados["descricao"], vencimento=dados["vencimento"],
            valor_original=dados["valor"], saldo_aberto=dados["valor"], conta=dados["conta"], evento=evento,
        )
        return evento
    movimento = MovimentoConta.objects.create(
        conta=dados["conta"], evento=evento, data_efetiva=dados["data"],
        valor=_valor_assinado(dados["natureza"], dados["valor"]), historico_original=dados["descricao"],
    )
    if dados["natureza"] == NaturezaEvento.PAGAMENTO_FATURA:
        titulo = _titulo_fatura(usuario, dados["cartao"], dados["ciclo"], dados.get("vencimento") or dados["data"])
        liquidar_titulo(titulo, movimento)
    reprocessar_saldo_conta(dados["conta"].pk, dados["data"])
    return evento


def dados_edicao_lancamento(usuario, evento: EventoFinanceiro) -> dict:
    movimento = evento.movimentos_conta.select_related("conta").first()
    titulo = Titulo.objects.filter(usuario=usuario, evento=evento).select_related("conta").first()
    liquidacao = _liquidacao_fatura(movimento)
    rateio = evento.rateios.select_related("categoria").first()
    conta = movimento.conta if movimento else titulo.conta if titulo else None
    return {
        "descricao": evento.descricao, "natureza": evento.natureza,
        "data": movimento.data_efetiva if movimento else evento.data,
        "valor": abs(movimento.valor) if movimento else titulo.valor_original if titulo else evento.valor,
        "origem": "CONTA", "conta": conta,
        "cartao": liquidacao.titulo.fatura.cartao if liquidacao else None,
        "categoria": rateio.categoria if rateio else None, "pessoa": evento.pessoa,
        "pessoa_nome_livre": _nome_pessoa_evento(evento) if evento.pessoa_id else evento.pessoa_nome_livre,
        "ambito": evento.ambito, "situacao": evento.situacao, "observacao": evento.observacao,
        "vencimento": titulo.vencimento if titulo else evento.vencimento,
        "ciclo": liquidacao.titulo.fatura.ciclo if liquidacao else "", "quantidade_parcelas": 1,
    }


def lancamento_possui_dados_protegidos(evento: EventoFinanceiro) -> bool:
    return ItemImportacao.objects.filter(
        Q(evento=evento) | Q(movimento_conta__evento=evento) | Q(parcela__compra__evento=evento)
    ).exists()


def _eventos_recorrentes_futuros(evento: EventoFinanceiro):
    if not evento.serie_recorrente_id or not evento.numero_recorrencia:
        return EventoFinanceiro.objects.none()
    return EventoFinanceiro.objects.filter(
        usuario=evento.usuario,
        serie_recorrente_id=evento.serie_recorrente_id,
        numero_recorrencia__gt=evento.numero_recorrencia,
        situacao=Situacao.PREVISTO,
    ).order_by("numero_recorrencia", "pk")


def _evento_recorrente_editavel(evento: EventoFinanceiro) -> bool:
    if lancamento_possui_dados_protegidos(evento):
        return False
    parcela = evento.compras_cartao.prefetch_related("parcelas__fatura").first()
    if parcela:
        return not parcela.parcelas.filter(fatura__status__in=STATUS_FATURA_BLOQUEADA).exists()
    titulo = Titulo.objects.filter(usuario=evento.usuario, evento=evento).first()
    return not bool(titulo and (titulo.liquidacoes.exists() or titulo.programacoes.exists()))


def contar_recorrencias_futuras(evento: EventoFinanceiro) -> int:
    return sum(1 for futuro in _eventos_recorrentes_futuros(evento) if _evento_recorrente_editavel(futuro))


def _dados_edicao_parcela(parcela: ParcelaCartao, dados: dict) -> dict:
    return {
        "descricao": dados["descricao"],
        "natureza": dados["natureza"],
        "data_original": parcela.data_original_exibicao,
        "valor": dados["valor"],
        "cartao": parcela.fatura.cartao,
        "categoria": dados.get("categoria"),
        "pessoa": dados.get("pessoa"),
        "pessoa_nome_livre": dados.get("pessoa_nome_livre", ""),
        "ambito": dados["ambito"],
        "situacao": parcela.situacao,
        "ciclo": parcela.fatura.ciclo,
        "numero_parcela": parcela.numero,
        "total_parcelas": parcela.total,
        "observacao": dados.get("observacao", ""),
    }


@transaction.atomic
def replicar_edicao_recorrencia(usuario, evento: EventoFinanceiro, dados: dict) -> int:
    """Replica apenas os dados aprovados para ocorrências futuras ainda previstas."""
    atualizados = 0
    futuros = _eventos_recorrentes_futuros(evento).select_for_update().select_related("serie_recorrente")
    for futuro in futuros:
        if not _evento_recorrente_editavel(futuro):
            continue
        compra = futuro.compras_cartao.prefetch_related("parcelas__fatura").first()
        if compra:
            parcela = compra.parcelas.select_related("fatura__cartao").first()
            if not parcela:
                continue
            editar_parcela_cartao(usuario, parcela.pk, _dados_edicao_parcela(parcela, dados))
            futuro.observacao = dados.get("observacao", "")
            futuro.save(update_fields=["observacao", "atualizado_em"])
        else:
            futuros_dados = dados_edicao_lancamento(usuario, futuro)
            futuros_dados.update({
                "descricao": dados["descricao"],
                "natureza": dados["natureza"],
                "valor": dados["valor"],
                "conta": dados.get("conta"),
                "categoria": dados.get("categoria"),
                "pessoa": dados.get("pessoa"),
                "pessoa_nome_livre": dados.get("pessoa_nome_livre", ""),
                "ambito": dados["ambito"],
                "observacao": dados.get("observacao", ""),
            })
            editar_lancamento_conta(usuario, futuro.pk, futuros_dados)
        atualizados += 1
    return atualizados


def _sincronizar_rateio(
    evento: EventoFinanceiro,
    categoria: Categoria | None,
    valor: Decimal,
    preservar_multiplos=False,
) -> None:
    categorias_atuais = set(evento.rateios.values_list("categoria_id", flat=True))
    if preservar_multiplos and len(categorias_atuais) > 1 and categoria and categoria.pk in categorias_atuais:
        return
    evento.rateios.all().delete()
    if categoria:
        RateioCategoria.objects.create(evento=evento, categoria=categoria, valor=valor)


@transaction.atomic
def editar_lancamento_conta(usuario, evento_id: int, dados: dict) -> EventoFinanceiro:
    evento = EventoFinanceiro.objects.select_for_update().get(pk=evento_id, usuario=usuario)
    if evento.compras_cartao.exists():
        raise ValidationError("Edite compras de cartão pela parcela exibida na listagem.")
    movimento = evento.movimentos_conta.select_for_update().first()
    titulo = Titulo.objects.select_for_update().filter(usuario=usuario, evento=evento).first()
    liquidacao = _liquidacao_fatura(movimento)
    protegido = lancamento_possui_dados_protegidos(evento) or bool(liquidacao)
    saldos_afetados = {}
    if movimento:
        saldos_afetados[movimento.conta_id] = movimento.data_efetiva

    evento.descricao = dados["descricao"]
    evento.pessoa = dados.get("pessoa")
    evento.pessoa_nome_livre = dados.get("pessoa_nome_livre", "")
    evento.ambito = dados["ambito"]
    evento.observacao = dados.get("observacao", "")
    _sincronizar_rateio(evento, dados.get("categoria"), dados["valor"], preservar_multiplos=protegido)
    if protegido:
        evento.save(update_fields=["descricao", "pessoa", "pessoa_nome_livre", "ambito", "observacao", "atualizado_em"])
        return evento

    evento.natureza = dados["natureza"]
    evento.data = dados["data"]
    evento.vencimento = dados.get("vencimento")
    evento.valor = dados["valor"]
    evento.situacao = dados["situacao"]
    evento.save()

    if dados["situacao"] == Situacao.PREVISTO:
        if movimento:
            movimento.delete()
        tipo = Titulo.Tipo.RECEBER if dados["natureza"] in {NaturezaEvento.RECEITA, NaturezaEvento.ESTORNO} else Titulo.Tipo.PAGAR
        if titulo:
            if titulo.liquidacoes.exists() or titulo.programacoes.exists():
                raise ValidationError("A previsão possui vínculos e não pode ser alterada por este formulário.")
            titulo.tipo = tipo
            titulo.descricao = dados["descricao"]
            titulo.vencimento = dados["vencimento"]
            titulo.valor_original = dados["valor"]
            titulo.saldo_aberto = dados["valor"]
            titulo.conta = dados["conta"]
            titulo.save()
        else:
            Titulo.objects.create(
                usuario=usuario, tipo=tipo, descricao=dados["descricao"], vencimento=dados["vencimento"],
                valor_original=dados["valor"], saldo_aberto=dados["valor"], conta=dados["conta"], evento=evento,
            )
    else:
        if titulo:
            if titulo.liquidacoes.exists() or titulo.programacoes.exists():
                raise ValidationError("O título possui vínculos e não pode ser realizado por este formulário.")
            titulo.delete()
        if movimento:
            movimento.conta = dados["conta"]
            movimento.data_efetiva = dados["data"]
            movimento.valor = _valor_assinado(dados["natureza"], dados["valor"])
            if not movimento.chave_idempotencia:
                movimento.historico_original = dados["descricao"]
            movimento.save()
        else:
            MovimentoConta.objects.create(
                conta=dados["conta"], evento=evento, data_efetiva=dados["data"],
                valor=_valor_assinado(dados["natureza"], dados["valor"]), historico_original=dados["descricao"],
            )
        saldos_afetados[dados["conta"].pk] = min(
            saldos_afetados.get(dados["conta"].pk, dados["data"]),
            dados["data"],
        )
    for conta_id, desde in saldos_afetados.items():
        reprocessar_saldo_conta(conta_id, desde)
    return evento


@transaction.atomic
def excluir_lancamento_conta(usuario, evento_id: int, excluir_futuros=False) -> int:
    evento = EventoFinanceiro.objects.select_for_update().get(pk=evento_id, usuario=usuario)
    if lancamento_possui_dados_protegidos(evento):
        raise ValidationError("Este lançamento veio de uma importação. Use o fluxo de revisão ou desfazimento para preservar a auditoria.")
    if evento.compras_cartao.exists():
        raise ValidationError("Exclua a parcela pela linha correspondente do cartão.")
    movimento = evento.movimentos_conta.select_for_update().first()
    if movimento and movimento.liquidacao_set.exists():
        raise ValidationError("Este pagamento possui liquidação. Corrija-o pelo fluxo da fatura.")
    saldo_afetado = (movimento.conta_id, movimento.data_efetiva) if movimento else None
    titulo = Titulo.objects.select_for_update().filter(usuario=usuario, evento=evento).first()
    if titulo:
        if titulo.liquidacoes.exists() or titulo.programacoes.exists() or titulo.transportes_saida.exists() or titulo.transportes_entrada.exists():
            raise ValidationError("Este título possui vínculos financeiros e não pode ser excluído.")
        titulo.delete()
    if movimento:
        movimento.delete()
    evento.delete()
    if saldo_afetado:
        reprocessar_saldo_conta(*saldo_afetado)
    excluidos = 0
    if excluir_futuros:
        for futuro in _eventos_recorrentes_futuros(evento):
            if _evento_recorrente_editavel(futuro) and not futuro.compras_cartao.exists():
                excluir_lancamento_conta(usuario, futuro.pk)
                excluidos += 1
    return excluidos


@transaction.atomic
def excluir_parcela_lancamento(usuario, parcela_id: int, excluir_futuros=False) -> int:
    parcela = (
        ParcelaCartao.objects.select_for_update()
        .select_related("fatura", "compra__evento")
        .get(pk=parcela_id, compra__usuario=usuario)
    )
    if parcela.fatura.status in STATUS_FATURA_BLOQUEADA:
        raise ValidationError("A fatura está fechada e deve ser reaberta antes da exclusão.")
    if ItemImportacao.objects.filter(Q(parcela=parcela) | Q(evento=parcela.compra.evento)).exists():
        raise ValidationError("Esta parcela veio de uma importação. Use o fluxo de revisão ou desfazimento para preservar a auditoria.")
    compra = parcela.compra
    evento = compra.evento
    fatura_id = parcela.fatura_id
    parcela.delete()
    if not compra.parcelas.exists():
        compra.delete()
        if not evento.movimentos_conta.exists() and not Titulo.objects.filter(evento=evento).exists():
            evento.delete()
    recalcular_fatura(fatura_id)
    excluidos = 0
    if excluir_futuros:
        for futuro in _eventos_recorrentes_futuros(evento):
            if not _evento_recorrente_editavel(futuro):
                continue
            futura_parcela = futuro.compras_cartao.prefetch_related("parcelas").first()
            if futura_parcela and futura_parcela.parcelas.exists():
                excluir_parcela_lancamento(usuario, futura_parcela.parcelas.first().pk)
                excluidos += 1
    return excluidos
