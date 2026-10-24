from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
import unicodedata
from urllib.parse import urlencode

from django.db.models import Exists, OuterRef, Q, Sum
from django.utils import timezone

from financeiro.models import (
    Categoria,
    Cartao,
    Conta,
    EventoFinanceiro,
    Fatura,
    Liquidacao,
    MovimentoConta,
    NaturezaEvento,
    ParcelaCartao,
    ProgramacaoPagamento,
    SaldoTransportadoFatura,
    Situacao,
    Titulo,
)
from financeiro.services.filtros_financeiros import resolver_filtros_financeiros
from financeiro.services.cartoes_faturas import calcular_totais_parcelas
from financeiro.services.parcelas_futuras import contribuicoes_futuras
from financeiro.services.saldo_contas import saldo_conta_ate


ZERO = Decimal("0.00")
HUNDRED = Decimal("100")
ORIGENS = {"all", "account", "card"}
NATUREZAS_ANALITICAS = {"all", "personal", "business"}
COMPUTACOES_ANALITICAS = {"COMPUTA", "NAO_COMPUTA"}
ESSENCIALIDADES_COMPOSICAO = (
    Categoria.Essencialidade.ESSENCIAL,
    Categoria.Essencialidade.NECESSARIO_VARIAVEL,
    Categoria.Essencialidade.PROFISSIONAL,
    Categoria.Essencialidade.ESTILO_VIDA,
    Categoria.Essencialidade.SUPERFLUO,
    Categoria.Essencialidade.FINANCEIRO,
)
NATUREZAS_DISTRIBUICAO = (
    Categoria.Natureza.PESSOAL,
    Categoria.Natureza.EMPRESARIAL,
    Categoria.Natureza.AMBOS,
)


@dataclass(frozen=True)
class ContribuicaoAnalitica:
    origem: str
    registro: MovimentoConta | ParcelaCartao
    valor: Decimal
    natureza_evento: str
    categoria: Categoria | None

    @property
    def valor_indicador(self) -> Decimal:
        if self.natureza_evento == NaturezaEvento.DESPESA:
            return abs(self.valor)
        if (
            self.natureza_evento == NaturezaEvento.ESTORNO
            and self.categoria
            and self.categoria.tipo == Categoria.Tipo.DESPESA
        ):
            return -abs(self.valor)
        return ZERO


def limites_periodo(ciclo: str) -> tuple[date, date]:
    ano, mes = (int(parte) for parte in ciclo.split("-"))
    inicio = date(ano, mes, 1)
    fim = date(ano + (mes == 12), 1 if mes == 12 else mes + 1, 1)
    return inicio, fim


def _ultimo_dia_mes(valor: date) -> date:
    return valor.replace(day=monthrange(valor.year, valor.month)[1])


def _interpretar_data(valor) -> date | None:
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    if not valor:
        return None
    for formato in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(valor).strip(), formato).date()
        except (TypeError, ValueError):
            continue
    return None


def _intervalo_dashboard(date_from=None, date_to=None, periodo=None) -> tuple[date, date]:
    """Resolve o intervalo inclusivo, preservando ``periodo`` como compatibilidade."""
    datas_informadas = date_from not in (None, "") or date_to not in (None, "")
    if datas_informadas:
        inicio = _interpretar_data(date_from)
        fim = _interpretar_data(date_to)
        if inicio and fim and inicio <= fim:
            return inicio, fim
    elif periodo:
        try:
            inicio, fim_exclusivo = limites_periodo(periodo)
        except (TypeError, ValueError):
            pass
        else:
            return inicio, fim_exclusivo - timedelta(days=1)

    hoje = timezone.localdate()
    return hoje.replace(day=1), _ultimo_dia_mes(hoje)


def _intervalo_anterior(inicio: date, fim: date) -> tuple[date, date]:
    """Retorna limites [início, fim) para a comparação imediatamente anterior."""
    mes_completo = inicio.day == 1 and fim == _ultimo_dia_mes(inicio)
    if mes_completo:
        fim_anterior = inicio
        inicio_anterior = (inicio - timedelta(days=1)).replace(day=1)
        return inicio_anterior, fim_anterior
    duracao = (fim - inicio).days + 1
    return inicio - timedelta(days=duracao), inicio


def _ciclo_anterior(ciclo: str) -> str:
    ano, mes = (int(parte) for parte in ciclo.split("-"))
    if mes == 1:
        ano, mes = ano - 1, 12
    else:
        mes -= 1
    return f"{ano:04d}-{mes:02d}"


def _ciclos_terminando(ciclo: str, quantidade: int) -> list[str]:
    ciclos = []
    atual = ciclo
    for _ in range(quantidade):
        ciclos.append(atual)
        atual = _ciclo_anterior(atual)
    return list(reversed(ciclos))


def _rotulo_ciclo(ciclo: str) -> str:
    ano, mes = ciclo.split("-")
    return f"{mes}/{ano}"


def _percentual(valor: Decimal, base: Decimal) -> Decimal:
    if not base or valor <= ZERO:
        return ZERO
    return (valor / base * HUNDRED).quantize(Decimal("0.01"))


def _percentual_css(valor: Decimal) -> str:
    """Serializa percentuais sem localização para uso em atributos CSS."""
    return f"{valor:.2f}"


def _proporcao_computavel_titulo(titulo: Titulo) -> Decimal:
    """Retorna a proporção do título que participa do saldo projetado."""
    evento = getattr(titulo, "evento", None)
    if not evento:
        return Decimal("1")
    rateios = list(evento.rateios.all())
    if not rateios:
        return Decimal("1")
    total_rateios = sum((abs(rateio.valor) for rateio in rateios), ZERO)
    if total_rateios <= ZERO:
        return ZERO
    computavel = sum(
        (abs(rateio.valor) for rateio in rateios if rateio.categoria.computa_resultado),
        ZERO,
    )
    return min(computavel / total_rateios, Decimal("1"))


def _valor_projetado_titulo(titulo: Titulo, valor: Decimal) -> Decimal:
    return (valor * _proporcao_computavel_titulo(titulo)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _normalizar_texto(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    return "".join(caractere for caractere in texto if not unicodedata.combining(caractere)).casefold()


def _normalizar_filtro(valor: str | None, permitidos: set[str]) -> str:
    return valor if valor in permitidos else "all"


def _natureza_compativel(categoria: Categoria | None, filtro: str) -> bool:
    if not categoria:
        return filtro == "all"
    if filtro == "all":
        return True
    if filtro == "personal":
        return categoria.natureza in {Categoria.Natureza.PESSOAL, Categoria.Natureza.AMBOS}
    return categoria.natureza in {Categoria.Natureza.EMPRESARIAL, Categoria.Natureza.AMBOS}


def _mapa_categorias(usuario) -> dict[int, Categoria]:
    return {
        categoria.pk: categoria
        for categoria in Categoria.objects.filter(usuario=usuario).select_related("categoria_pai")
    }


def _categoria_superior(categoria: Categoria | None, categorias: dict[int, Categoria]) -> Categoria | None:
    if not categoria:
        return None
    atual = categoria
    visitados = set()
    while atual.categoria_pai_id and atual.categoria_pai_id not in visitados:
        visitados.add(atual.pk)
        atual = categorias.get(atual.categoria_pai_id, atual.categoria_pai)
    return atual


def _nova_analise() -> dict:
    return {
        "receitas": ZERO,
        "despesas": ZERO,
        "creditos": ZERO,
        "categorias": {},
        "essencialidade": {chave: ZERO for chave in ESSENCIALIDADES_COMPOSICAO},
        "natureza": {chave: ZERO for chave in NATUREZAS_DISTRIBUICAO},
    }


def _despesas_liquidas(analise: dict) -> Decimal:
    return analise["despesas"] - analise["creditos"]


def _criar_contribuicao_analitica(
    origem: str,
    registro: MovimentoConta | ParcelaCartao,
    valor: Decimal,
    natureza_evento: str,
    categoria: Categoria | None,
    filtro_natureza: str,
    computacao: str = "COMPUTA",
) -> ContribuicaoAnalitica | None:
    if natureza_evento in {NaturezaEvento.PAGAMENTO_FATURA, NaturezaEvento.TRANSFERENCIA}:
        return None
    if natureza_evento == NaturezaEvento.ESTORNO and (
        not categoria or categoria.tipo != Categoria.Tipo.DESPESA
    ):
        return None
    if computacao == "NAO_COMPUTA":
        if (
            natureza_evento not in {NaturezaEvento.DESPESA, NaturezaEvento.ESTORNO}
            or not categoria
            or categoria.computa_resultado
            or categoria.tipo != Categoria.Tipo.DESPESA
        ):
            return None
    elif categoria and not categoria.computa_resultado:
        return None
    if categoria and not _natureza_compativel(categoria, filtro_natureza):
        return None
    if not categoria and filtro_natureza != "all":
        return None

    return ContribuicaoAnalitica(
        origem=origem,
        registro=registro,
        valor=abs(valor),
        natureza_evento=natureza_evento,
        categoria=categoria,
    )


def _adicionar_contribuicao_analitica(
    analise: dict,
    contribuicao: ContribuicaoAnalitica,
    categorias: dict[int, Categoria],
) -> None:
    valor = contribuicao.valor
    natureza_evento = contribuicao.natureza_evento
    categoria = contribuicao.categoria

    if natureza_evento == NaturezaEvento.RECEITA:
        analise["receitas"] += valor
        return
    if natureza_evento not in {NaturezaEvento.DESPESA, NaturezaEvento.ESTORNO}:
        return

    sinal = Decimal("1") if natureza_evento == NaturezaEvento.DESPESA else Decimal("-1")
    if natureza_evento == NaturezaEvento.DESPESA:
        analise["despesas"] += valor
    else:
        analise["creditos"] += valor

    categoria_superior = _categoria_superior(categoria, categorias)
    chave = categoria_superior.pk if categoria_superior else None
    item = analise["categorias"].setdefault(
        chave,
        {
            "categoria_id": chave,
            "codigo": categoria_superior.codigo if categoria_superior else "",
            "nome": categoria_superior.nome if categoria_superior else "A classificar",
            "valor": ZERO,
        },
    )
    item["valor"] += valor * sinal

    if categoria and categoria.essencialidade in analise["essencialidade"]:
        analise["essencialidade"][categoria.essencialidade] += valor * sinal
    if categoria and categoria.natureza in analise["natureza"]:
        analise["natureza"][categoria.natureza] += valor * sinal


def gerar_contribuicoes_analiticas(
    movimentos: list[MovimentoConta],
    parcelas: list[ParcelaCartao],
    origem: str,
    filtro_natureza: str,
    computacao: str = "COMPUTA",
) -> list[ContribuicaoAnalitica]:
    computacao = computacao if computacao in COMPUTACOES_ANALITICAS else "COMPUTA"
    contribuicoes = []
    if origem != "card":
        for movimento in movimentos:
            rateios = list(movimento.evento.rateios.all())
            candidatos = (
                ((rateio.valor, rateio.categoria) for rateio in rateios)
                if rateios
                else ((movimento.evento.valor, None),)
            )
            for valor, categoria in candidatos:
                contribuicao = _criar_contribuicao_analitica(
                    "CONTA",
                    movimento,
                    valor,
                    movimento.evento.natureza,
                    categoria,
                    filtro_natureza,
                    computacao,
                )
                if contribuicao:
                    contribuicoes.append(contribuicao)

    if origem != "account":
        for parcela in parcelas:
            contribuicao = _criar_contribuicao_analitica(
                "CARTAO",
                parcela,
                parcela.valor,
                parcela.natureza_exibicao,
                parcela.categoria,
                filtro_natureza,
                computacao,
            )
            if contribuicao:
                contribuicoes.append(contribuicao)
    return contribuicoes


def _analisar_contribuicoes(
    contribuicoes: list[ContribuicaoAnalitica],
    categorias: dict[int, Categoria],
) -> dict:
    analise = _nova_analise()
    for contribuicao in contribuicoes:
        _adicionar_contribuicao_analitica(analise, contribuicao, categorias)
    return analise


def _movimentos_conta(usuario, contas: list[Conta], inicio: date, fim: date) -> list[MovimentoConta]:
    if not contas:
        return []
    return list(
        MovimentoConta.objects.filter(
            conta_id__in=[conta.pk for conta in contas],
            conta__usuario=usuario,
            evento__situacao=Situacao.REALIZADO,
            evento__data__gte=inicio,
            evento__data__lt=fim,
        )
        .select_related("conta", "evento__pessoa")
        .prefetch_related("evento__rateios__categoria")
    )


def _carregar_parcelas_cartao(usuario, cartoes: list[Cartao]) -> list[ParcelaCartao]:
    if not cartoes:
        return []
    parcelas = list(
        ParcelaCartao.objects.filter(
            compra__usuario=usuario,
            compra__cartao_id__in=[cartao.pk for cartao in cartoes],
        )
        .exclude(
            Q(natureza=NaturezaEvento.PAGAMENTO_FATURA)
            | Q(natureza="", compra__evento__natureza=NaturezaEvento.PAGAMENTO_FATURA)
        )
        .select_related(
            "compra__evento__pessoa",
            "categoria",
            "fatura__cartao",
            "pessoa",
        )
        .order_by("compra_id", "numero", "id")
    )
    return parcelas


def _parcelas_cartao(
    parcelas: list[ParcelaCartao],
    inicio: date,
    fim: date,
) -> list[ParcelaCartao]:
    return [
        parcela
        for parcela in parcelas
        if inicio <= parcela.vencimento < fim
    ]


def _analisar_dados(
    movimentos: list[MovimentoConta],
    parcelas: list[ParcelaCartao],
    origem: str,
    filtro_natureza: str,
    categorias: dict[int, Categoria],
) -> dict:
    return _analisar_contribuicoes(
        gerar_contribuicoes_analiticas(movimentos, parcelas, origem, filtro_natureza),
        categorias,
    )


def _fluxo_movimentos(movimentos: list[MovimentoConta]) -> dict:
    entradas = sum((movimento.valor for movimento in movimentos if movimento.valor > ZERO), ZERO)
    saidas = sum((abs(movimento.valor) for movimento in movimentos if movimento.valor < ZERO), ZERO)
    return {"entradas": entradas, "saidas": saidas}


def _saldo_contas(usuario, contas: list[Conta], fim: date) -> Decimal:
    # Os resumos persistidos representam meses completos. Para uma data no
    # meio do mês, parte do último fechamento e soma somente o trecho parcial.
    if fim.day != 1:
        inicio_mes = fim.replace(day=1)
        saldo = _saldo_contas(usuario, contas, inicio_mes)
        historico_q = None
        for conta in contas:
            referencia = conta.data_saldo_inicial
            if referencia and referencia >= fim:
                continue
            inicio_conta = inicio_mes
            if referencia and referencia >= inicio_mes:
                saldo += conta.saldo_inicial
                inicio_conta = referencia
            condicao = Q(
                conta_id=conta.pk,
                data_efetiva__gte=inicio_conta,
                data_efetiva__lt=fim,
            )
            historico_q = condicao if historico_q is None else historico_q | condicao
        if historico_q is None:
            return saldo
        realizado = (
            MovimentoConta.objects.filter(
                historico_q,
                conta__usuario=usuario,
                evento__situacao=Situacao.REALIZADO,
            ).aggregate(total=Sum("valor", default=ZERO))["total"]
            or ZERO
        )
        return saldo + realizado

    saldo = ZERO
    historico_q = None
    for conta in contas:
        resumo = saldo_conta_ate(conta, fim)
        if resumo is not None:
            saldo += resumo
            continue
        referencia = conta.data_saldo_inicial
        if referencia and referencia >= fim:
            continue
        if referencia:
            saldo += conta.saldo_inicial
            condicao = Q(conta_id=conta.pk, data_efetiva__gte=referencia)
        else:
            saldo += conta.saldo_inicial
            condicao = Q(conta_id=conta.pk)
        historico_q = condicao if historico_q is None else historico_q | condicao

    if historico_q is None:
        return saldo
    realizado = (
        MovimentoConta.objects.filter(
            historico_q,
            conta__usuario=usuario,
            evento__situacao=Situacao.REALIZADO,
            data_efetiva__lt=fim,
        ).aggregate(total=Sum("valor", default=ZERO))["total"]
        or ZERO
    )
    return saldo + realizado


def _metricas_conta(usuario, contas: list[Conta], inicio: date, fim: date) -> dict:
    movimentos = _movimentos_conta(usuario, contas, inicio, fim)
    return {**_fluxo_movimentos(movimentos), "saldo": _saldo_contas(usuario, contas, fim)}


def _valor_fatura(parcelas: list[ParcelaCartao]) -> Decimal:
    return calcular_totais_parcelas(parcelas)["projetado"]


def _categorias_formatadas(analise: dict) -> list[dict]:
    total = max(_despesas_liquidas(analise), ZERO)
    categorias = []
    for item in sorted(analise["categorias"].values(), key=lambda valor: (-valor["valor"], valor["nome"])):
        if item["valor"] == ZERO:
            continue
        percentual = _percentual(max(item["valor"], ZERO), total)
        categorias.append({
            **item,
            "percentual": percentual,
            "percentual_css": _percentual_css(percentual),
        })
    return categorias


def _composicao_formatada(analise: dict) -> list[dict]:
    total = max(_despesas_liquidas(analise), ZERO)
    rotulos = dict(Categoria.Essencialidade.choices)
    itens = []
    for tipo in ESSENCIALIDADES_COMPOSICAO:
        percentual = _percentual(max(analise["essencialidade"][tipo], ZERO), total)
        itens.append({
            "tipo": tipo,
            "nome": rotulos[tipo],
            "valor": analise["essencialidade"][tipo],
            "percentual": percentual,
            "percentual_css": _percentual_css(percentual),
        })
    return itens


def _natureza_formatada(analise: dict) -> list[dict]:
    total = max(_despesas_liquidas(analise), ZERO)
    rotulos = dict(Categoria.Natureza.choices)
    itens = []
    for tipo in NATUREZAS_DISTRIBUICAO:
        percentual = _percentual(max(analise["natureza"][tipo], ZERO), total)
        itens.append({
            "tipo": tipo,
            "nome": rotulos[tipo],
            "valor": analise["natureza"][tipo],
            "percentual": percentual,
            "percentual_css": _percentual_css(percentual),
        })
    return itens


def _simulador_formatado(categorias: list[dict]) -> list[dict]:
    return [
        {
            **item,
            "base_centavos": int(max(item["valor"], ZERO) * 100),
            "percentual_inicial": 20,
            "economia_mensal": (max(item["valor"], ZERO) * Decimal("0.20")).quantize(Decimal("0.01")),
            "economia_anual": (max(item["valor"], ZERO) * Decimal("2.40")).quantize(Decimal("0.01")),
        }
        for item in categorias[:10]
        if item["valor"] > ZERO and item["nome"] != "A classificar"
    ]


def _variacao(atual: Decimal, anterior: Decimal) -> Decimal:
    if anterior <= ZERO:
        return ZERO
    return ((atual - anterior) / anterior * HUNDRED).quantize(Decimal("0.01"))


def _comparacao_resumo(atual: Decimal, anterior: Decimal) -> dict:
    if anterior == ZERO:
        return {"percentual": None, "texto": "Sem base anterior"}
    percentual = ((atual - anterior) / abs(anterior) * HUNDRED).quantize(Decimal("0.01"))
    prefixo = "+" if percentual > ZERO else ""
    return {"percentual": percentual, "texto": f"{prefixo}{percentual:.2f}% vs. período anterior"}


def _pontos_atencao(atual: dict, anterior: dict) -> list[dict]:
    pontos = []
    anteriores = {item["nome"]: item["valor"] for item in anterior["categorias"].values()}
    categorias_atencao = []
    for item in atual["categorias"].values():
        anterior_valor = anteriores.get(item["nome"], ZERO)
        variacao = _variacao(item["valor"], anterior_valor)
        if anterior_valor > ZERO and item["valor"] > anterior_valor and variacao >= Decimal("20"):
            categorias_atencao.append((variacao, item["valor"] - anterior_valor, item["nome"]))
    for variacao, diferenca, nome in sorted(categorias_atencao, reverse=True)[:3]:
        pontos.append({
            "tipo": "variacao",
            "titulo": nome,
            "mensagem": f"+{variacao:.2f}% em relação ao período anterior",
            "valor": diferenca,
            "variacao": variacao,
        })

    despesas = _despesas_liquidas(atual)
    controlaveis = sum(
        (valor for tipo, valor in atual["essencialidade"].items() if tipo in {
            Categoria.Essencialidade.ESTILO_VIDA,
            Categoria.Essencialidade.SUPERFLUO,
        }),
        ZERO,
    )
    if despesas > ZERO and controlaveis / despesas >= Decimal("0.30") and len(pontos) < 3:
        pontos.append({
            "tipo": "controlavel",
            "titulo": "Gastos controláveis",
            "mensagem": f"{_percentual(max(controlaveis, ZERO), despesas):.2f}% das despesas",
            "valor": controlaveis,
            "variacao": ZERO,
        })

    termos = ("juros", "tarifa", "taxa", "encargo")
    nomes_ja_exibidos = {item["titulo"] for item in pontos}
    for variacao, diferenca, nome in sorted(categorias_atencao, reverse=True):
        if len(pontos) >= 3:
            break
        if nome not in nomes_ja_exibidos and any(termo in _normalizar_texto(nome) for termo in termos):
            pontos.append({
                "tipo": "tarifa",
                "titulo": nome,
                "mensagem": f"+{variacao:.2f}% em relação ao período anterior",
                "valor": diferenca,
                "variacao": variacao,
            })
            nomes_ja_exibidos.add(nome)
    return pontos[:3]


def _evolucao(
    usuario,
    contas,
    parcelas_cartao,
    ciclo,
    origem,
    filtro_natureza,
    categorias,
) -> list[dict]:
    evolucao = []
    for item_ciclo in _ciclos_terminando(ciclo, 6):
        inicio, fim = limites_periodo(item_ciclo)
        analise = _analisar_dados(
            _movimentos_conta(usuario, contas, inicio, fim),
            _parcelas_cartao(parcelas_cartao, inicio, fim),
            origem,
            filtro_natureza,
            categorias,
        )
        despesas = _despesas_liquidas(analise)
        evolucao.append({
            "ciclo": item_ciclo,
            "rotulo": _rotulo_ciclo(item_ciclo),
            "receitas": analise["receitas"],
            "despesas": despesas,
            "resultado": analise["receitas"] - despesas,
        })
    maior_valor = max(
        (max(item["receitas"], max(item["despesas"], ZERO), abs(item["resultado"])) for item in evolucao),
        default=ZERO,
    )
    for item in evolucao:
        item["receitas_percentual"] = _percentual(item["receitas"], maior_valor)
        item["despesas_percentual"] = _percentual(max(item["despesas"], ZERO), maior_valor)
        item["resultado_percentual"] = _percentual(abs(item["resultado"]), maior_valor)
        item["receitas_percentual_css"] = _percentual_css(item["receitas_percentual"])
        item["despesas_percentual_css"] = _percentual_css(item["despesas_percentual"])
        item["resultado_percentual_css"] = _percentual_css(item["resultado_percentual"])
    return evolucao


def _previstos_intervalo(
    usuario,
    contas: list[Conta],
    inicio: date,
    fim: date,
    tipos: tuple[str, ...] = (Titulo.Tipo.PAGAR,),
) -> dict:
    titulos = list(
        Titulo.objects.filter(
            usuario=usuario,
            tipo__in=tipos,
            ativo=True,
            saldo_aberto__gt=ZERO,
        )
        .select_related("conta", "fatura__cartao")
        .prefetch_related("programacoes__conta", "evento__rateios__categoria")
        .order_by("vencimento", "id")
    )
    contas_ids = {conta.pk for conta in contas}
    itens = []
    for titulo in titulos:
        programado = ZERO
        for programacao in titulo.programacoes.all():
            if programacao.status not in {ProgramacaoPagamento.Status.ATIVA, ProgramacaoPagamento.Status.PARCIAL}:
                continue
            restante = programacao.valor_programado - programacao.valor_realizado
            if restante <= ZERO:
                continue
            programado += restante
            valor_projetado = _valor_projetado_titulo(titulo, restante)
            if valor_projetado > ZERO and programacao.conta_id in contas_ids and inicio <= programacao.data_programada < fim:
                itens.append(SimpleNamespace(
                    descricao=f"{titulo.descricao} (programado)",
                    saldo_aberto=valor_projetado,
                    vencimento=programacao.data_programada,
                    conta=programacao.conta,
                ))
        residual = titulo.saldo_aberto - programado
        valor_residual_projetado = _valor_projetado_titulo(titulo, residual)
        if valor_residual_projetado > ZERO and titulo.conta_id in contas_ids and inicio <= titulo.vencimento < fim:
            itens.append(SimpleNamespace(
                descricao=titulo.descricao,
                saldo_aberto=valor_residual_projetado,
                vencimento=titulo.vencimento,
                conta=titulo.conta,
            ))
    return {"itens": itens, "total": sum((item.saldo_aberto for item in itens), ZERO)}


def _debitos_previstos(usuario, contas: list[Conta], inicio: date, fim: date) -> dict:
    return _previstos_intervalo(usuario, contas, inicio, fim)


def _previsao_fluxo(
    usuario,
    contas: list[Conta],
    inicio: date,
    fim: date,
    saidas: dict | None = None,
) -> dict:
    receitas = _previstos_intervalo(usuario, contas, inicio, fim, (Titulo.Tipo.RECEBER,))
    saidas = saidas or _previstos_intervalo(usuario, contas, inicio, fim, (Titulo.Tipo.PAGAR,))
    return {
        "receitas_previstas": receitas["total"],
        "saidas_previstas": saidas["total"],
    }


def _compromissos_futuros(usuario, contas: list[Conta]) -> dict:
    hoje = timezone.localdate()
    proximos_7 = _previstos_intervalo(usuario, contas, hoje, hoje + timedelta(days=7))
    proximos_30 = _previstos_intervalo(usuario, contas, hoje, hoje + timedelta(days=30))
    return {
        "proximos_7_dias": proximos_7["itens"],
        "total_7_dias": proximos_7["total"],
        "proximos_30_dias": proximos_30["itens"],
        "total_30_dias": proximos_30["total"],
    }


def _faturas_fechadas_sem_baixa(usuario, cartoes: list[Cartao], ciclo: str) -> Decimal:
    """Soma faturas fechadas do ciclo que ainda não receberam baixa em conta."""
    cartoes_ids = [cartao.pk for cartao in cartoes]
    if not cartoes_ids:
        return ZERO
    baixas_confirmadas = Liquidacao.objects.filter(
        titulo_id=OuterRef("pk"),
        status=Liquidacao.Status.ATIVA,
        movimento_conta__isnull=False,
    )
    return (
        Titulo.objects.filter(
            usuario=usuario,
            tipo=Titulo.Tipo.PAGAR,
            ativo=True,
            status=Titulo.Status.ABERTO,
            saldo_aberto__gt=ZERO,
            fatura__usuario=usuario,
            fatura__cartao_id__in=cartoes_ids,
            fatura__ciclo=ciclo,
            fatura__status=Fatura.Status.FECHADA,
        )
        .annotate(tem_baixa_confirmada=Exists(baixas_confirmadas))
        .filter(tem_baixa_confirmada=False)
        .aggregate(total=Sum("saldo_aberto", default=ZERO))["total"]
        or ZERO
    )


def calcular_posicao_contas(usuario, contas: list[Conta], hoje: date, fim_previsao: date) -> list[dict]:
    """Monta saldos por conta sem misturar fluxo efetivo e previsões."""
    if not contas:
        return []

    contas_por_id = {conta.pk: conta for conta in contas}
    saldos = {}
    for conta in contas:
        referencia = conta.data_saldo_inicial
        saldos[conta.pk] = conta.saldo_inicial if not referencia or referencia <= hoje else ZERO

    historico_q = None
    for conta in contas:
        referencia = conta.data_saldo_inicial
        if referencia and referencia > hoje:
            continue
        condicao = Q(conta_id=conta.pk, data_efetiva__lte=hoje)
        if referencia:
            condicao &= Q(data_efetiva__gte=referencia)
        historico_q = condicao if historico_q is None else historico_q | condicao
    if historico_q is not None:
        totais = (
            MovimentoConta.objects.filter(
                historico_q,
                conta__usuario=usuario,
                evento__situacao=Situacao.REALIZADO,
            )
            .values("conta_id")
            .annotate(total=Sum("valor"))
        )
        for movimento in totais:
            saldos[movimento["conta_id"]] += movimento["total"] or ZERO

    previsoes = {conta_id: {"entradas": ZERO, "saidas": ZERO} for conta_id in contas_por_id}
    titulos = list(
        Titulo.objects.filter(
            usuario=usuario,
            ativo=True,
            saldo_aberto__gt=ZERO,
            tipo__in=[Titulo.Tipo.RECEBER, Titulo.Tipo.PAGAR],
        )
        .select_related("conta")
        .prefetch_related("programacoes", "evento__rateios__categoria")
    )
    for titulo in titulos:
        programado = ZERO
        for programacao in titulo.programacoes.all():
            if programacao.status not in {ProgramacaoPagamento.Status.ATIVA, ProgramacaoPagamento.Status.PARCIAL}:
                continue
            restante = programacao.valor_programado - programacao.valor_realizado
            if restante <= ZERO:
                continue
            programado += restante
            valor_projetado = _valor_projetado_titulo(titulo, restante)
            if valor_projetado > ZERO and programacao.conta_id in previsoes and programacao.data_programada < fim_previsao:
                chave = "entradas" if titulo.tipo == Titulo.Tipo.RECEBER else "saidas"
                previsoes[programacao.conta_id][chave] += valor_projetado

        residual = titulo.saldo_aberto - programado
        valor_residual_projetado = _valor_projetado_titulo(titulo, residual)
        if valor_residual_projetado > ZERO and titulo.conta_id in previsoes and titulo.vencimento < fim_previsao:
            chave = "entradas" if titulo.tipo == Titulo.Tipo.RECEBER else "saidas"
            previsoes[titulo.conta_id][chave] += valor_residual_projetado

    itens = []
    for conta in contas:
        previsto = previsoes[conta.pk]
        identificacao = " · ".join(parte for parte in (conta.banco, conta.numero) if parte)
        itens.append({
            "conta": conta,
            "identificacao": identificacao,
            "saldo_atual": saldos[conta.pk],
            "entradas_previstas": previsto["entradas"],
            "saidas_previstas": previsto["saidas"],
            "saldo_projetado": saldos[conta.pk] + previsto["entradas"] - previsto["saidas"],
        })
    return itens


def _valor_parcela_comprometida(parcela: ParcelaCartao) -> Decimal:
    if parcela.situacao == Situacao.CANCELADO:
        return ZERO
    natureza = parcela.natureza_exibicao
    if natureza == NaturezaEvento.DESPESA:
        return abs(parcela.valor)
    if natureza in {NaturezaEvento.ESTORNO, NaturezaEvento.RECEITA}:
        return -abs(parcela.valor)
    return ZERO


def _faixa_comprometimento(percentual: Decimal | None) -> str:
    if percentual is None:
        return "neutral"
    if percentual <= Decimal("80.00"):
        return "blue"
    if percentual <= Decimal("90.00"):
        return "orange"
    return "red"


def calcular_limites_cartoes(usuario, cartoes: list[Cartao], ciclo_base: str) -> list[dict]:
    """Calcula a obrigação conhecida por cartão sem contar fatura e itens duas vezes."""
    if not cartoes:
        return []
    cartoes_por_id = {cartao.pk: cartao for cartao in cartoes}
    faturas = list(
        Fatura.objects.filter(usuario=usuario, cartao_id__in=cartoes_por_id)
        .select_related("cartao")
        .order_by("cartao_id", "ciclo", "id")
    )
    faturas_por_id = {fatura.pk: fatura for fatura in faturas}
    titulos = {
        titulo.fatura_id: titulo
        for titulo in Titulo.objects.filter(usuario=usuario, fatura_id__in=faturas_por_id)
        .only("id", "fatura_id", "saldo_aberto", "ativo", "status")
    }
    parcelas_por_fatura = {fatura.pk: [] for fatura in faturas}
    parcelas_materializadas = set()
    for parcela in (
        ParcelaCartao.objects.filter(compra__usuario=usuario, fatura_id__in=faturas_por_id)
        .select_related("compra__evento")
        .order_by("fatura_id", "compra_id", "numero", "id")
    ):
        parcelas_por_fatura[parcela.fatura_id].append(parcela)
        parcelas_materializadas.add((parcela.compra_id, parcela.numero))

    transportes = list(
        SaldoTransportadoFatura.objects.filter(
            fatura_destino_id__in=faturas_por_id,
            status__in=[SaldoTransportadoFatura.Status.CONFIRMADO, SaldoTransportadoFatura.Status.INCORPORADO],
        ).values("fatura_destino_id", "valor")
    )
    transportes_por_destino = {fatura.pk: ZERO for fatura in faturas}
    for transporte in transportes:
        transportes_por_destino[transporte["fatura_destino_id"]] += transporte["valor"]

    transportes_pendentes = {cartao_id: 0 for cartao_id in cartoes_por_id}
    for transporte in SaldoTransportadoFatura.objects.filter(
        Q(fatura_origem__cartao_id__in=cartoes_por_id) | Q(fatura_destino__cartao_id__in=cartoes_por_id),
        status=SaldoTransportadoFatura.Status.IDENTIFICADO,
    ).values("fatura_origem__cartao_id", "fatura_destino__cartao_id"):
        for cartao_id in {transporte["fatura_origem__cartao_id"], transporte["fatura_destino__cartao_id"]}:
            if cartao_id in transportes_pendentes:
                transportes_pendentes[cartao_id] += 1

    comprometidos = {cartao_id: ZERO for cartao_id in cartoes_por_id}
    for fatura in faturas:
        titulo = titulos.get(fatura.pk)
        titulo_pendente = (
            titulo
            and titulo.ativo
            and titulo.status != Titulo.Status.LIQUIDADO
            and titulo.saldo_aberto > ZERO
        )
        if fatura.status not in {Fatura.Status.ABERTA, Fatura.Status.PAGA} and titulo_pendente:
            comprometidos[fatura.cartao_id] += titulo.saldo_aberto
            continue
        if fatura.status == Fatura.Status.ABERTA:
            comprometidos[fatura.cartao_id] += sum(
                (_valor_parcela_comprometida(parcela) for parcela in parcelas_por_fatura[fatura.pk]),
                ZERO,
            )
            comprometidos[fatura.cartao_id] += transportes_por_destino[fatura.pk]

    estimativas_por_cartao = {cartao_id: ZERO for cartao_id in cartoes_por_id}
    for contribuicao in contribuicoes_futuras(
        usuario,
        cartoes,
        ciclo_base,
        incluir_tarifas=True,
    ):
        if contribuicao["origem"] != "estimado":
            continue
        if (contribuicao["compra_id"], contribuicao["numero"]) in parcelas_materializadas:
            continue
        estimativas_por_cartao[contribuicao["cartao_id"]] += contribuicao["valor"]
        comprometidos[contribuicao["cartao_id"]] += contribuicao["valor"]

    itens = []
    for cartao in cartoes:
        limite_informado = cartao.limite > ZERO
        comprometido = comprometidos[cartao.pk]
        percentual = _percentual(comprometido, cartao.limite) if limite_informado else None
        disponivel = cartao.limite - comprometido if limite_informado else None
        itens.append({
            "cartao": cartao,
            "comprometido": comprometido,
            "limite": cartao.limite,
            "limite_informado": limite_informado,
            "percentual": percentual,
            "percentual_css": _percentual_css(min(percentual, HUNDRED)) if percentual is not None else "0.00",
            "faixa_percentual": _faixa_comprometimento(percentual),
            "disponivel": disponivel,
            "excedente": max(comprometido - cartao.limite, ZERO) if limite_informado else ZERO,
            "inclui_estimativas": estimativas_por_cartao[cartao.pk] > ZERO,
            "transportes_pendentes": transportes_pendentes[cartao.pk],
        })
    return itens


def _dashboard_configurado(usuario) -> bool:
    return all([
        Conta.objects.filter(usuario=usuario).exists(),
        Cartao.objects.filter(usuario=usuario).exists(),
        Categoria.objects.filter(usuario=usuario).exists(),
        EventoFinanceiro.objects.filter(usuario=usuario).exists(),
    ])


def _metricas_legadas(analise: dict, fluxo: dict, saldo: Decimal, fatura: Decimal) -> dict:
    despesas = _despesas_liquidas(analise)
    return {
        "entradas": fluxo["entradas"],
        "saidas": fluxo["saidas"],
        "saldo": saldo,
        "fatura": fatura,
        "receitas": analise["receitas"],
        "despesas": despesas,
        "resultado": analise["receitas"] - despesas,
    }


def carregar_contribuicoes_dashboard(
    usuario,
    periodo=None,
    conta_id=None,
    cartao_id=None,
    origem="all",
    natureza="all",
    computacao="COMPUTA",
    date_from=None,
    date_to=None,
    aplicar_composicao_dashboard=False,
) -> dict:
    data_inicial, data_final = _intervalo_dashboard(date_from, date_to, periodo)
    fim_exclusivo = data_final + timedelta(days=1)
    periodo_referencia = data_final.strftime("%Y-%m")
    filtros = resolver_filtros_financeiros(
        usuario,
        periodo_referencia,
        conta_id,
        cartao_id,
        aplicar_composicao_dashboard=aplicar_composicao_dashboard,
    )
    contas_filtradas = [filtros["conta_selecionada"]] if filtros["conta_selecionada"] else filtros["contas"]
    origem = _normalizar_filtro(origem, ORIGENS)
    natureza = _normalizar_filtro(natureza, NATUREZAS_ANALITICAS)
    categorias = _mapa_categorias(usuario)
    parcelas_carregadas = _carregar_parcelas_cartao(usuario, filtros["cartoes_filtrados"])
    movimentos = _movimentos_conta(usuario, contas_filtradas, data_inicial, fim_exclusivo)
    parcelas = _parcelas_cartao(parcelas_carregadas, data_inicial, fim_exclusivo)

    return {
        "data_inicial": data_inicial,
        "data_final": data_final,
        "fim_exclusivo": fim_exclusivo,
        "periodo_referencia": periodo_referencia,
        "filtros": filtros,
        "contas_filtradas": contas_filtradas,
        "origem": origem,
        "natureza": natureza,
        "categorias": categorias,
        "parcelas_carregadas": parcelas_carregadas,
        "movimentos": movimentos,
        "parcelas": parcelas,
        "aplicar_composicao_dashboard": aplicar_composicao_dashboard,
        "contribuicoes": gerar_contribuicoes_analiticas(
            movimentos,
            parcelas,
            origem,
            natureza,
            computacao,
        ),
    }


def consultar_dashboard(
    usuario,
    periodo=None,
    conta_id=None,
    cartao_id=None,
    origem="all",
    natureza="all",
    date_from=None,
    date_to=None,
) -> dict:
    dados_analiticos = carregar_contribuicoes_dashboard(
        usuario,
        periodo=periodo,
        conta_id=conta_id,
        cartao_id=cartao_id,
        origem=origem,
        natureza=natureza,
        date_from=date_from,
        date_to=date_to,
        aplicar_composicao_dashboard=True,
    )
    data_inicial = dados_analiticos["data_inicial"]
    data_final = dados_analiticos["data_final"]
    fim_exclusivo = dados_analiticos["fim_exclusivo"]
    periodo_referencia = dados_analiticos["periodo_referencia"]
    filtros = dados_analiticos["filtros"]
    contas_filtradas = dados_analiticos["contas_filtradas"]
    origem = dados_analiticos["origem"]
    natureza = dados_analiticos["natureza"]
    categorias = dados_analiticos["categorias"]
    parcelas_carregadas = dados_analiticos["parcelas_carregadas"]
    movimentos_atual = dados_analiticos["movimentos"]
    parcelas_atual = dados_analiticos["parcelas"]
    analise_atual = _analisar_contribuicoes(dados_analiticos["contribuicoes"], categorias)
    analise_nao_computavel = _analisar_contribuicoes(
        gerar_contribuicoes_analiticas(
            movimentos_atual,
            parcelas_atual,
            origem,
            natureza,
            "NAO_COMPUTA",
        ),
        categorias,
    )

    inicio_anterior, fim_anterior = _intervalo_anterior(data_inicial, data_final)
    analise_anterior = _analisar_dados(
        _movimentos_conta(usuario, contas_filtradas, inicio_anterior, fim_anterior),
        _parcelas_cartao(parcelas_carregadas, inicio_anterior, fim_anterior),
        origem,
        natureza,
        categorias,
    )

    fluxo = _fluxo_movimentos(movimentos_atual)
    saldo = _saldo_contas(usuario, contas_filtradas, fim_exclusivo)
    previstos_legados = _debitos_previstos(usuario, contas_filtradas, data_inicial, fim_exclusivo)
    previsao_fluxo = _previsao_fluxo(
        usuario,
        contas_filtradas,
        data_inicial,
        fim_exclusivo,
        previstos_legados,
    )
    saldo_projetado = saldo + previsao_fluxo["receitas_previstas"] - previsao_fluxo["saidas_previstas"]
    totais_fatura = calcular_totais_parcelas(parcelas_atual)
    fatura = totais_fatura["projetado"]
    fatura_confirmada = totais_fatura["confirmado"]
    categorias_formatadas = _categorias_formatadas(analise_atual)
    categorias_nao_computaveis = _categorias_formatadas(analise_nao_computavel)
    composicao = _composicao_formatada(analise_atual)
    distribuicao = _natureza_formatada(analise_atual)
    total_composicao = sum((item["valor"] for item in composicao), ZERO)
    total_categorias = sum((item["valor"] for item in categorias_formatadas), ZERO)
    total_categorias_nao_computaveis = sum(
        (item["valor"] for item in categorias_nao_computaveis),
        ZERO,
    )
    total_distribuicao = sum((item["valor"] for item in distribuicao), ZERO)
    gastos_controlaveis = sum(
        (valor for tipo, valor in analise_atual["essencialidade"].items() if tipo in {
            Categoria.Essencialidade.ESTILO_VIDA,
            Categoria.Essencialidade.SUPERFLUO,
        }),
        ZERO,
    )
    despesas_atuais = _despesas_liquidas(analise_atual)
    despesas_anteriores = _despesas_liquidas(analise_anterior)
    resultado = analise_atual["receitas"] - despesas_atuais
    compromissos = _compromissos_futuros(usuario, contas_filtradas)
    fatura_projetada_compromissos = _faturas_fechadas_sem_baixa(
        usuario,
        filtros["cartoes_filtrados"],
        periodo_referencia,
    )
    hoje = timezone.localdate()
    posicao_contas = calcular_posicao_contas(usuario, contas_filtradas, hoje, fim_exclusivo)
    limites_cartoes = calcular_limites_cartoes(usuario, filtros["cartoes_filtrados"], hoje.strftime("%Y-%m"))

    simulador = _simulador_formatado(categorias_formatadas)
    base_simulador = next(
        (item["valor"] for item in categorias_formatadas if item["codigo"] == "alimentacao-fora"),
        ZERO,
    )

    resumo = {
        "receitas": analise_atual["receitas"],
        "despesas": despesas_atuais,
        "resultado": resultado,
        "saldoAtual": saldo,
        "faturaAtual": fatura,
        "faturaConfirmada": fatura_confirmada,
        "faturaProjetada": fatura,
    }
    comparacoes_resumo = {
        "receitas": _comparacao_resumo(analise_atual["receitas"], analise_anterior["receitas"]),
        "despesas": _comparacao_resumo(despesas_atuais, despesas_anteriores),
        "resultado": _comparacao_resumo(resultado, analise_anterior["receitas"] - despesas_anteriores),
    }

    percentual_gastos_controlaveis = _percentual(max(gastos_controlaveis, ZERO), despesas_atuais)
    conta_query = filtros["conta_selecionada"].pk if filtros["conta_selecionada"] else "all"
    cartao_query = filtros["cartao_selecionado"].pk if filtros["cartao_selecionado"] else "all"
    detalhe_base_query = urlencode({
        "date_from": data_inicial.strftime("%d/%m/%Y"),
        "date_to": data_final.strftime("%d/%m/%Y"),
        "account": conta_query,
        "card": cartao_query,
        "origin": origem,
        "nature": natureza,
        "dashboard_context": "1",
    })
    lancamentos_base_query = urlencode({
        "date_from": data_inicial.strftime("%d/%m/%Y"),
        "date_to": data_final.strftime("%d/%m/%Y"),
        "account": conta_query,
        "card": cartao_query,
        "origin": {"account": "CONTA", "card": "CARTAO"}.get(origem, "all"),
        "analysis_nature": natureza,
        "dashboard_context": "1",
    })

    return {
        **{chave: valor for chave, valor in filtros.items() if chave != "cartoes_filtrados"},
        "data_inicial": data_inicial,
        "data_final": data_final,
        "date_from": data_inicial.strftime("%d/%m/%Y"),
        "date_to": data_final.strftime("%d/%m/%Y"),
        "periodo_referencia": periodo_referencia,
        "origem_filtro": origem,
        "natureza_filtro": natureza,
        "origens_analiticas": [("all", "Todos"), ("account", "Conta"), ("card", "Cartão")],
        "naturezas_analiticas": [("all", "Todas"), ("personal", "Pessoal"), ("business", "Empresarial")],
        "detalhe_base_query": detalhe_base_query,
        "lancamentos_base_query": lancamentos_base_query,
        "resumo": resumo,
        "comparacoes_resumo": comparacoes_resumo,
        "fluxo_caixa": {
            **fluxo,
            "saldo": saldo,
            **previsao_fluxo,
            "saldo_projetado": saldo_projetado,
        },
        "metricas": _metricas_legadas(analise_atual, fluxo, saldo, fatura),
        "composicao_despesas": composicao,
        "total_composicao_despesas": total_composicao,
        "gastos_controlaveis": {
            "valor": gastos_controlaveis,
            "percentual": percentual_gastos_controlaveis,
            "percentual_css": _percentual_css(percentual_gastos_controlaveis),
        },
        "gastos_por_categoria": categorias_formatadas,
        "total_gastos_por_categoria": total_categorias,
        "gastos_por_categoria_nao_computa": categorias_nao_computaveis,
        "total_gastos_por_categoria_nao_computa": total_categorias_nao_computaveis,
        "categorias": categorias_formatadas,
        "distribuicao_natureza": distribuicao,
        "total_distribuicao_natureza": total_distribuicao,
        "simulador_economia": simulador,
        "base_simulador_centavos": int(base_simulador * 100),
        "economia_inicial": (base_simulador * Decimal("0.20")).quantize(Decimal("0.01")),
        "compromissos": {
            **compromissos,
            "fatura_atual": fatura,
            "fatura_confirmada": fatura_confirmada,
            "fatura_projetada": fatura_projetada_compromissos,
        },
        "debitos_previstos": previstos_legados["itens"],
        "total_debitos_previstos": previstos_legados["total"],
        "saldo_apos_previsoes": saldo - previstos_legados["total"],
        "posicao_data": hoje,
        "contas_financeiras": posicao_contas,
        "limites_cartoes": limites_cartoes,
        "pontos_atencao": _pontos_atencao(analise_atual, analise_anterior),
        "evolucao": _evolucao(
            usuario,
            contas_filtradas,
            parcelas_carregadas,
            periodo_referencia,
            origem,
            natureza,
            categorias,
        ),
        "dashboard_configurado": _dashboard_configurado(usuario),
    }
