from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from financeiro.models import (
    Ambito,
    Cartao,
    CompraCartao,
    Conta,
    EventoFinanceiro,
    EvidenciaLiquidacao,
    Fatura,
    Importacao,
    ItemImportacao,
    Liquidacao,
    MovimentoConta,
    NaturezaEvento,
    ParcelaCartao,
    RateioCategoria,
    RegraCategoria,
    SaldoTransportadoFatura,
    Situacao,
    Titulo,
    VinculoTituloImportacao,
)
from financeiro.ofx import CartaoOFXAdapter, OFXDocument, normalize_text, transaction_as_dict
from financeiro.services.titulos import liquidar_titulo
from financeiro.services.fechamento_faturas import criar_transporte, recalcular_titulo
from financeiro.services.saldo_contas import reprocessar_saldo_conta
from financeiro.services.regras_categoria import (
    ConjuntoRegrasCategoria,
    NATUREZAS_FORA_DO_MOTOR,
    avaliar_e_aplicar_regra,
    carregar_regras_categoria,
)
from financeiro.vencimentos_cartao import resolver_vencimento_fatura


ZERO = Decimal("0.00")
CENTAVO = Decimal("0.01")
MAX_HISTORICO_CLASSIFICACAO = 200
MOTIVO_SEM_CANDIDATO_CONFIAVEL = (
    "Nenhum destino compatível por data e valor; selecione um vínculo ou crie o lançamento."
)
MOTIVO_CRIAR_SEM_CANDIDATO = (
    "Nenhum destino compatível por data e valor; Criar lançamento foi pré-selecionado."
)


def fingerprint(*parts) -> str:
    material = "|".join(str(part or "").strip() for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def proximo_ciclo(ciclo: str, quantidade: int = 1) -> str:
    ano, mes = (int(value) for value in ciclo.split("-"))
    indice = ano * 12 + mes - 1 + quantidade
    return f"{indice // 12:04d}-{indice % 12 + 1:02d}"


def estabelecimento_normalizado(descricao: str) -> str:
    sem_parcela = CartaoOFXAdapter.parcela_pattern.sub(" ", descricao or "")
    return normalize_text(sem_parcela)


def _mes_anterior(data_referencia: date) -> tuple[date, date]:
    ciclo = proximo_ciclo(f"{data_referencia.year:04d}-{data_referencia.month:02d}", -1)
    inicio = date.fromisoformat(f"{ciclo}-01")
    proximo = proximo_ciclo(ciclo)
    fim = date.fromisoformat(f"{proximo}-01") - timedelta(days=1)
    return inicio, fim


def _tokens_descricao(descricao: str) -> set[str]:
    return {token for token in estabelecimento_normalizado(descricao).split() if len(token) > 2}


def _historico_excluido(descricao: str) -> bool:
    texto = normalize_text(descricao)
    return (
        ("pagamento" in texto and "fatura" in texto)
        or "transfer" in texto
        or "estorno" in texto
    )


def _similaridade_descricao(principal: str, historico: str) -> tuple[int, int]:
    principal_normalizado = estabelecimento_normalizado(principal)
    historico_normalizado = estabelecimento_normalizado(historico)
    if not principal_normalizado or not historico_normalizado:
        return 0, 0
    if principal_normalizado == historico_normalizado:
        return 3, 3
    if (
        len(principal_normalizado) >= 5
        and len(historico_normalizado) >= 5
        and (principal_normalizado in historico_normalizado or historico_normalizado in principal_normalizado)
    ):
        return 2, 2
    tokens_principais = _tokens_descricao(principal)
    tokens_historicos = _tokens_descricao(historico)
    if not tokens_principais or not tokens_historicos:
        return 0, 0
    intersecao = tokens_principais & tokens_historicos
    proporcao = len(intersecao) / min(len(tokens_principais), len(tokens_historicos))
    if len(intersecao) >= 2 and proporcao >= 0.5:
        return 1, len(intersecao)
    return 0, 0


def _adicionar_motivo(item: ItemImportacao, motivo: str) -> None:
    if motivo not in (item.motivo_status or ""):
        item.motivo_status = f"{item.motivo_status} {motivo}".strip()


def _categorias_evento(evento: EventoFinanceiro | None) -> list[dict]:
    if not evento:
        return []
    return [
        {"id": rateio.categoria_id, "nome": rateio.categoria.nome, "valor": str(rateio.valor)}
        for rateio in evento.rateios.select_related("categoria").all()
    ]


def _pontuacao_candidato(item: ItemImportacao, data_candidato, valor: Decimal, evento=None) -> tuple[int, list[str]]:
    pontos = 0
    motivos = []
    if abs(valor) == abs(item.valor_final or ZERO):
        pontos += 100
        motivos.append("valor exato")
    if item.data_final and data_candidato:
        distancia = abs((data_candidato - item.data_final).days)
        if distancia <= 7:
            pontos += 30 - (distancia * 3)
            motivos.append("data próxima" if distancia else "mesma data")
    if item.categoria_id and evento and evento.rateios.filter(categoria_id=item.categoria_id).exists():
        pontos += 40
        motivos.append("mesma categoria")
    if evento:
        descricao_item = normalize_text(item.descricao_final)
        descricao_evento = normalize_text(evento.descricao)
        if descricao_item and descricao_evento and (
            descricao_evento in descricao_item or descricao_item in descricao_evento
        ):
            pontos += 20
            motivos.append("descrição semelhante")
        pessoa = normalize_text(
            evento.pessoa.nome if evento.pessoa_id else evento.pessoa_nome_livre
        )
        if pessoa and pessoa in descricao_item:
            pontos += 15
            motivos.append("pessoa semelhante")
    return pontos, motivos


def _natureza_esperada_parcela(item: ItemImportacao) -> str:
    if item.natureza in {NaturezaEvento.DESPESA, NaturezaEvento.ESTORNO}:
        return item.natureza
    return NaturezaEvento.ESTORNO if (item.valor_final or ZERO) > ZERO else NaturezaEvento.DESPESA


def _parcelas_elegiveis_vinculo(item: ItemImportacao, usuario):
    valor = item.valor_final or ZERO
    if (
        item.importacao.origem != Importacao.Origem.CARTAO
        or item.importacao.usuario_id != usuario.id
        or not item.importacao.cartao_id
        or not item.importacao.ciclo
        or not valor
        or item.natureza not in {NaturezaEvento.DESPESA, NaturezaEvento.ESTORNO}
    ):
        return ParcelaCartao.objects.none()

    ocupadas = (
        ItemImportacao.objects.filter(
            importacao__status=Importacao.Status.CONFIRMADA,
            parcela__isnull=False,
        )
        .exclude(importacao__adaptador="cartao_planilha")
        .exclude(chave_conciliacao=item.chave_conciliacao)
        .values_list("parcela_id", flat=True)
    )
    natureza_esperada = _natureza_esperada_parcela(item)
    return (
        ParcelaCartao.objects.filter(
            compra__usuario=usuario,
            compra__cartao=item.importacao.cartao,
            fatura__ciclo=item.importacao.ciclo,
            fatura__status=Fatura.Status.ABERTA,
            situacao=Situacao.PREVISTO,
        )
        .filter(
            Q(natureza=natureza_esperada)
            | Q(natureza="", compra__evento__natureza=natureza_esperada)
        )
        .exclude(pk__in=ocupadas)
    )


def _parcela_corresponde_data_valor(item: ItemImportacao, parcela: ParcelaCartao) -> bool:
    return bool(
        item.data_final
        and parcela.data_original_exibicao
        and parcela.data_original_exibicao == item.data_final
        and abs(parcela.valor - abs(item.valor_final or ZERO)) <= CENTAVO
    )


def buscar_candidatos_vinculo(
    item: ItemImportacao,
    usuario,
    tipo="TODOS",
    busca="",
    escopo="SUGESTOES",
    pagina=1,
    por_pagina=20,
) -> dict:
    if item.importacao.usuario_id != usuario.id:
        raise ValidationError("O item da importação não pertence ao usuário informado.")
    tipo = tipo if tipo in {"TODOS", "TITULO", "MOVIMENTO", "PARCELA"} else "TODOS"
    escopo = escopo if escopo in {"SUGESTOES", "TODOS"} else "SUGESTOES"
    termo = normalize_text(busca)
    valor = item.valor_final or ZERO
    candidatos = []

    if item.importacao.origem == Importacao.Origem.CONTA and tipo in {"TODOS", "TITULO"} and valor:
        tipo_titulo = Titulo.Tipo.RECEBER if valor > ZERO else Titulo.Tipo.PAGAR
        saldo_lookup = "saldo_aberto__lte" if escopo == "TODOS" else "saldo_aberto__gte"
        titulos = (
            Titulo.objects.filter(
                usuario=usuario,
                tipo=tipo_titulo,
                ativo=True,
                status=Titulo.Status.ABERTO,
                **{saldo_lookup: abs(valor)},
            )
            .filter(Q(conta=item.importacao.conta) | Q(conta__isnull=True))
            .select_related("conta", "evento__pessoa", "fatura__cartao")
            .prefetch_related("evento__rateios__categoria")
        )
        for titulo in titulos:
            evento = titulo.evento
            categorias = _categorias_evento(evento)
            pessoa = evento.pessoa.nome if evento and evento.pessoa_id else evento.pessoa_nome_livre if evento else ""
            texto = normalize_text(" ".join([
                titulo.descricao,
                titulo.conta.nome if titulo.conta_id else "",
                pessoa,
                " ".join(categoria["nome"] for categoria in categorias),
            ]))
            if termo and termo not in texto:
                continue
            pontos, motivos = _pontuacao_candidato(item, titulo.vencimento, titulo.saldo_aberto, evento)
            candidatos.append({
                "tipo": "TITULO",
                "id": titulo.pk,
                "descricao": titulo.descricao,
                "data": titulo.vencimento.isoformat(),
                "valor": str(titulo.valor_original),
                "saldo": str(titulo.saldo_aberto),
                "conta": titulo.conta.nome if titulo.conta_id else "Conta não definida",
                "natureza": evento.get_natureza_display() if evento else titulo.get_tipo_display(),
                "categoria": ", ".join(categoria["nome"] for categoria in categorias) or "A classificar",
                "pessoa": pessoa or "Pessoa não informada",
                "pontuacao": pontos,
                "motivos": motivos,
            })

    if item.importacao.origem == Importacao.Origem.CONTA and tipo in {"TODOS", "MOVIMENTO"} and valor:
        ocupados = set(
            ItemImportacao.objects.filter(
                importacao__status=Importacao.Status.CONFIRMADA,
                movimento_conta__isnull=False,
            )
            .exclude(chave_conciliacao=item.chave_conciliacao)
            .values_list("movimento_conta_id", flat=True)
        )
        movimentos = MovimentoConta.objects.filter(
            conta=item.importacao.conta,
            evento__usuario=usuario,
            valor__gt=ZERO,
        ) if valor > ZERO else MovimentoConta.objects.filter(
            conta=item.importacao.conta,
            evento__usuario=usuario,
            valor__lt=ZERO,
        )
        movimentos = movimentos.exclude(pk__in=ocupados).select_related("conta", "evento__pessoa").prefetch_related("evento__rateios__categoria")
        for movimento in movimentos:
            evento = movimento.evento
            categorias = _categorias_evento(evento)
            pessoa = evento.pessoa.nome if evento.pessoa_id else evento.pessoa_nome_livre
            texto = normalize_text(" ".join([
                evento.descricao,
                movimento.historico_original,
                movimento.documento,
                movimento.conta.nome,
                pessoa,
                " ".join(categoria["nome"] for categoria in categorias),
            ]))
            if termo and termo not in texto:
                continue
            pontos, motivos = _pontuacao_candidato(item, movimento.data_efetiva, movimento.valor, evento)
            candidatos.append({
                "tipo": "MOVIMENTO",
                "id": movimento.pk,
                "descricao": evento.descricao,
                "data": movimento.data_efetiva.isoformat(),
                "valor": str(movimento.valor),
                "saldo": None,
                "conta": movimento.conta.nome,
                "natureza": evento.get_natureza_display(),
                "categoria": ", ".join(categoria["nome"] for categoria in categorias) or "A classificar",
                "pessoa": pessoa or "Pessoa não informada",
                "pontuacao": pontos,
                "motivos": motivos,
            })

    if item.importacao.origem == Importacao.Origem.CARTAO and tipo in {"TODOS", "PARCELA"} and valor:
        parcelas = (
            _parcelas_elegiveis_vinculo(item, usuario)
            .select_related(
                "compra__evento__pessoa",
                "fatura__cartao",
                "categoria",
                "pessoa",
            )
            .prefetch_related("compra__evento__rateios__categoria")
        )
        for parcela in parcelas:
            if escopo == "SUGESTOES" and not _parcela_corresponde_data_valor(item, parcela):
                continue
            evento = parcela.compra.evento
            categoria = parcela.categoria.nome if parcela.categoria_id else ""
            if not categoria:
                categorias_evento = _categorias_evento(evento)
                categoria = ", ".join(item_categoria["nome"] for item_categoria in categorias_evento)
            pessoa = (
                parcela.pessoa.nome
                if parcela.pessoa_id
                else parcela.pessoa_nome_livre
                or (evento.pessoa.nome if evento.pessoa_id else evento.pessoa_nome_livre)
            )
            descricao = parcela.descricao_exibicao
            texto = normalize_text(" ".join([
                descricao,
                parcela.compra.estabelecimento,
                parcela.fatura.ciclo,
                pessoa,
                categoria,
            ]))
            if termo and termo not in texto:
                continue
            data_candidata = parcela.data_original_exibicao
            pontos, motivos = _pontuacao_candidato(item, data_candidata, parcela.valor, evento)
            if item.categoria_id and parcela.categoria_id == item.categoria_id and "mesma categoria" not in motivos:
                pontos += 40
                motivos.append("mesma categoria")
            if item.numero_parcela and item.total_parcelas:
                if parcela.numero == item.numero_parcela and parcela.total == item.total_parcelas:
                    pontos += 35
                    motivos.append("mesma parcela")
            candidatos.append({
                "tipo": "PARCELA",
                "id": parcela.pk,
                "descricao": f"{descricao} · parcela {parcela.numero}/{parcela.total}",
                "data": data_candidata.isoformat(),
                "valor": str(parcela.valor),
                "saldo": None,
                "conta": f"{parcela.fatura.cartao.nome} · {parcela.fatura.ciclo}",
                "natureza": parcela.get_natureza_display() if parcela.natureza else evento.get_natureza_display(),
                "categoria": categoria or "A classificar",
                "pessoa": pessoa or "Pessoa não informada",
                "pontuacao": pontos,
                "motivos": motivos,
            })

    if escopo == "SUGESTOES" and not termo:
        candidatos = [candidato for candidato in candidatos if candidato["pontuacao"] > 0]
    candidatos.sort(key=lambda candidato: (
        -candidato["pontuacao"],
        -date.fromisoformat(candidato["data"]).toordinal(),
        candidato["tipo"],
        candidato["id"],
    ))
    total = len(candidatos)
    pagina = max(int(pagina or 1), 1)
    por_pagina = min(max(int(por_pagina or 20), 1), 50)
    inicio = (pagina - 1) * por_pagina
    return {
        "resultados": candidatos[inicio:inicio + por_pagina],
        "pagina": pagina,
        "por_pagina": por_pagina,
        "total": total,
        "tem_mais": inicio + por_pagina < total,
    }


def natureza_sugerida(origem: str, tipo: str, valor: Decimal, descricao: str) -> str:
    tipo = (tipo or "").upper()
    descricao_normalizada = normalize_text(descricao)
    pagamento_fatura = "PAGAMENTO" in descricao_normalizada and "FATURA" in descricao_normalizada
    pagamento_debito_em_conta = "PAGAMENTO DEBITO EM CONTA" in descricao_normalizada
    if pagamento_fatura or pagamento_debito_em_conta:
        return NaturezaEvento.PAGAMENTO_FATURA
    if origem == Importacao.Origem.CARTAO:
        if tipo in {"CREDIT", "CREDITO"} or valor > 0:
            return NaturezaEvento.ESTORNO
        return NaturezaEvento.DESPESA
    if tipo in {"CREDIT", "DEP", "DIRECTDEP", "INT", "CREDITO"} or valor > 0:
        return NaturezaEvento.RECEITA
    return NaturezaEvento.DESPESA


def criar_itens(importacao: Importacao, documento: OFXDocument) -> list[ItemImportacao]:
    repeticoes = Counter()
    itens = []
    adaptador_cartao = CartaoOFXAdapter()
    for posicao, registro in enumerate(documento.transacoes, start=1):
        identidade = registro.identificador or fingerprint(
            registro.data,
            registro.valor,
            registro.documento,
            normalize_text(registro.descricao),
        )
        repeticoes[identidade] += 1
        ocorrencia = repeticoes[identidade]
        numero_parcela, total_parcelas = (None, None)
        if importacao.origem == Importacao.Origem.CARTAO:
            numero_parcela, total_parcelas = adaptador_cartao.installment(registro.descricao)

        chave = _chave_item(importacao, registro, ocorrencia)
        natureza = natureza_sugerida(importacao.origem, registro.tipo, registro.valor, registro.descricao)
        item = ItemImportacao(
            importacao=importacao,
            posicao=posicao,
            identificador_externo=registro.identificador,
            ocorrencia=ocorrencia,
            chave_conciliacao=chave,
            tipo_normalizado=(registro.tipo or "").upper(),
            documento=registro.documento,
            data=registro.data,
            valor=registro.valor,
            descricao_original=registro.descricao,
            natureza=natureza,
            numero_parcela=numero_parcela,
            total_parcelas=total_parcelas,
            registro_original=transaction_as_dict(registro),
            status=ItemImportacao.Status.NOVO,
            acao=ItemImportacao.Acao.CRIAR,
        )
        itens.append(item)
    for item in itens:
        _conciliar_item(item)
        if not item.data or item.valor is None:
            item.status = ItemImportacao.Status.PENDENTE
            item.motivo_status = "Data ou valor ausente no registro OFX."
    contexto = preparar_contexto_classificacao(importacao, itens)
    for item in itens:
        aplicar_sugestao_classificacao(item, contexto)
    ItemImportacao.objects.bulk_create(itens)
    _copiar_titulos_multiplos_reimportados(itens)
    return itens


def _chave_item(importacao: Importacao, registro, ocorrencia: int) -> str:
    alvo = importacao.conta_id if importacao.origem == Importacao.Origem.CONTA else importacao.cartao_id
    if registro.identificador:
        return fingerprint(importacao.origem, alvo, importacao.ciclo, registro.identificador, ocorrencia)
    return fingerprint(
        importacao.origem,
        alvo,
        importacao.ciclo,
        registro.data,
        registro.valor,
        registro.documento,
        normalize_text(registro.descricao),
        ocorrencia,
    )


def _sugerir_categoria(item: ItemImportacao, regras=None):
    """Compatibilidade interna para chamadas anteriores ao motor da Fase 2."""
    if isinstance(regras, ConjuntoRegrasCategoria):
        conjunto = regras
    elif regras is None:
        conjunto = carregar_regras_categoria(item.importacao.usuario)
    else:
        regras = tuple(regras)
        conjunto = ConjuntoRegrasCategoria(
            novas=tuple(regra for regra in regras if regra.versao_motor == RegraCategoria.VersaoMotor.NOVO),
            legadas=tuple(regra for regra in regras if regra.versao_motor == RegraCategoria.VersaoMotor.LEGADO),
        )
    return avaliar_e_aplicar_regra(item, conjunto)


def _historico_conta(importacao: Importacao, itens: list[ItemImportacao]) -> list[dict]:
    datas = [item.data_final for item in itens if item.data_final]
    if not datas or not importacao.conta_id:
        return []
    janelas = [_mes_anterior(data) for data in datas]
    inicio = min(janela[0] for janela in janelas)
    fim = max(janela[1] for janela in janelas)
    movimentos = (
        MovimentoConta.objects.filter(
            conta=importacao.conta,
            evento__usuario=importacao.usuario,
            evento__situacao=Situacao.REALIZADO,
            evento__natureza__in=(NaturezaEvento.DESPESA, NaturezaEvento.RECEITA),
            data_efetiva__range=(inicio, fim),
        )
        .select_related("evento")
        .prefetch_related("evento__rateios__categoria")
        .order_by("-data_efetiva", "-id")[:MAX_HISTORICO_CLASSIFICACAO]
    )
    historico = []
    for movimento in movimentos:
        descricao_alternativa = movimento.historico_original or ""
        if _historico_excluido(" ".join(filter(None, [movimento.evento.descricao, descricao_alternativa]))):
            continue
        categorias = list(movimento.evento.rateios.all())
        historico.append({
            "data": movimento.data_efetiva,
            "valor": abs(movimento.valor),
            "natureza": movimento.evento.natureza,
            "descricao": movimento.evento.descricao,
            "descricao_alternativa": movimento.historico_original,
            "categorias": [categorias[0].categoria_id] if len(categorias) == 1 else [],
            "ambito": movimento.evento.ambito,
        })
    return historico


def _historico_cartao(importacao: Importacao) -> list[dict]:
    if not importacao.cartao_id or not importacao.ciclo:
        return []
    ciclo_anterior = proximo_ciclo(importacao.ciclo, -1)
    parcelas = (
        ParcelaCartao.objects.filter(
            compra__usuario=importacao.usuario,
            compra__cartao=importacao.cartao,
            fatura__ciclo=ciclo_anterior,
            situacao=Situacao.REALIZADO,
        )
        .filter(
            Q(natureza__in=(NaturezaEvento.DESPESA, NaturezaEvento.RECEITA))
            | Q(natureza="", compra__evento__natureza__in=(NaturezaEvento.DESPESA, NaturezaEvento.RECEITA))
        )
        .select_related("compra__evento")
        .prefetch_related("compra__evento__rateios__categoria")
        .order_by("-data_original", "-id")[:MAX_HISTORICO_CLASSIFICACAO]
    )
    historico = []
    for parcela in parcelas:
        evento = parcela.compra.evento
        if _historico_excluido(" ".join(filter(None, [parcela.descricao_exibicao, parcela.compra.estabelecimento]))):
            continue
        categorias = [parcela.categoria_id] if parcela.categoria_id else [
            rateio.categoria_id for rateio in evento.rateios.all()
        ]
        historico.append({
            "data": parcela.data_original_exibicao,
            "valor": abs(parcela.valor),
            "natureza": parcela.natureza or evento.natureza,
            "descricao": parcela.descricao_exibicao,
            "descricao_alternativa": parcela.compra.estabelecimento,
            "categorias": categorias if len(categorias) == 1 else [],
            "ambito": parcela.ambito or evento.ambito,
        })
    return historico


def preparar_contexto_classificacao(importacao: Importacao, itens: list[ItemImportacao]) -> dict:
    regras = carregar_regras_categoria(importacao.usuario)
    historico = (
        _historico_conta(importacao, itens)
        if importacao.origem == Importacao.Origem.CONTA
        else _historico_cartao(importacao)
    )
    return {
        "regras_categoria": regras,
        "regras": list(regras.legadas),
        "historico": historico,
    }


def _sugerir_valor_historico(item: ItemImportacao, historico: list[dict], chave: str):
    def valores(registro):
        bruto = registro[chave]
        if chave == "categorias":
            return [valor for valor in bruto if valor]
        return [bruto] if bruto not in (None, "", Ambito.NAO_DEFINIDO) else []

    evidencias = []
    for registro in historico:
        if registro["natureza"] != item.natureza:
            continue
        similaridade = max(
            _similaridade_descricao(item.descricao_final, registro[descricao])[0]
            for descricao in ("descricao", "descricao_alternativa")
            if registro.get(descricao)
        )
        if not similaridade:
            continue
        valor_exato = abs(registro["valor"]) == abs(item.valor_final or ZERO)
        evidencias.append((registro, similaridade, valor_exato))
    if not evidencias:
        return None

    exatas = [registro for registro, similaridade, valor_exato in evidencias if similaridade == 3 and valor_exato]
    valores_exatos = [valor for registro in exatas for valor in valores(registro)]
    if exatas and len(set(valores_exatos)) == 1 and valores_exatos:
        return valores_exatos[0]

    contagem = Counter(
        valor
        for registro, _, _ in evidencias
        for valor in valores(registro)
    )
    if not contagem:
        return None
    vencedores = contagem.most_common()
    vencedor, quantidade = vencedores[0]
    segundo = vencedores[1][1] if len(vencedores) > 1 else 0
    if quantidade >= 2 and quantidade > segundo:
        return vencedor
    return None


def aplicar_sugestao_classificacao(
    item: ItemImportacao,
    contexto: dict,
    *,
    campos_protegidos=None,
) -> None:
    if (
        item.status != ItemImportacao.Status.NOVO
        or item.acao != ItemImportacao.Acao.CRIAR
        or item.revisado_em is not None
    ):
        return
    protegidos = set(campos_protegidos or ())
    if getattr(item, "_categoria_informada", False):
        protegidos.add("categoria")
    if getattr(item, "_pessoa_informada", False):
        protegidos.add("pessoa")
    if getattr(item, "_ambito_informado", False):
        protegidos.add("ambito")

    if item.natureza not in NATUREZAS_FORA_DO_MOTOR:
        resultado = avaliar_e_aplicar_regra(
            item,
            contexto["regras_categoria"],
            campos_protegidos=protegidos,
        )
        if resultado.regra and resultado.campos_aplicados:
            if resultado.regra.versao_motor == RegraCategoria.VersaoMotor.LEGADO:
                motivo = f'Classificação sugerida pela regra legada “{resultado.regra.nome}”.'
            else:
                motivo = f'Classificação sugerida pela regra “{resultado.regra.nome}”.'
            _adicionar_motivo(item, motivo)

        historico = contexto.get("historico", [])
        if item.categoria_id is None:
            categoria_id = _sugerir_valor_historico(item, historico, "categorias")
            if categoria_id:
                item.categoria_id = categoria_id
                _adicionar_motivo(item, "Categoria sugerida pelo histórico recente.")
        if "ambito" not in protegidos and item.ambito in {None, "", Ambito.NAO_DEFINIDO}:
            ambito = _sugerir_valor_historico(item, historico, "ambito")
            if ambito:
                item.ambito = ambito
                _adicionar_motivo(item, "Âmbito sugerido pelo histórico recente.")

    if "ambito" not in protegidos and item.ambito in {None, "", Ambito.NAO_DEFINIDO}:
        item.ambito = Ambito.PESSOAL
        _adicionar_motivo(item, "Âmbito definido como Pessoal por padrão.")


def aplicar_classificacao_primeira_revisao(
    item: ItemImportacao,
    *,
    campos_protegidos=None,
    contexto=None,
) -> None:
    if (
        item.revisado_em is not None
        or item.acao != ItemImportacao.Acao.CRIAR
        or not item.data_final
        or item.valor_final is None
    ):
        return
    item.status = ItemImportacao.Status.NOVO
    contexto = contexto or preparar_contexto_classificacao(item.importacao, [item])
    aplicar_sugestao_classificacao(
        item,
        contexto,
        campos_protegidos=campos_protegidos,
    )


def _conciliar_item(item: ItemImportacao) -> None:
    anteriores = list(ItemImportacao.objects.filter(
        importacao__usuario=item.importacao.usuario,
        importacao__status=Importacao.Status.CONFIRMADA,
        chave_conciliacao=item.chave_conciliacao,
    ).select_related("evento", "movimento_conta", "parcela").order_by("-importacao__criado_em", "-id"))
    if anteriores:
        alvos = {(anterior.evento_id, anterior.movimento_conta_id, anterior.parcela_id) for anterior in anteriores}
        exatos = all(anterior.data == item.data and anterior.valor == item.valor for anterior in anteriores)
        if len(alvos) == 1 and exatos:
            _vincular_existente(item, anteriores[0])
            return
    if len(anteriores) == 1:
        anterior = anteriores[0]
        if anterior.data == item.data and anterior.valor == item.valor:
            _vincular_existente(item, anterior)
        else:
            item.status = ItemImportacao.Status.DIVERGENTE
            item.acao = ItemImportacao.Acao.VINCULAR
            item.evento = anterior.evento
            item.movimento_conta = anterior.movimento_conta
            item.parcela = anterior.parcela
            item.motivo_status = "O identificador já existe, mas data ou valor foi alterado."
        return
    if len(anteriores) > 1:
        item.status = ItemImportacao.Status.PENDENTE
        item.motivo_status = "A chave foi vinculada a destinos diferentes em importações anteriores."
        return

    if item.natureza == NaturezaEvento.PAGAMENTO_FATURA:
        if _conciliar_evento_pagamento(item):
            return
    sugerir_vinculo_confiavel(item)


def _vincular_existente(item: ItemImportacao, anterior: ItemImportacao) -> None:
    item.status = ItemImportacao.Status.CONCILIADO
    item.acao = ItemImportacao.Acao.VINCULAR
    item.evento = anterior.evento
    item.movimento_conta = anterior.movimento_conta
    item.titulo_alvo = anterior.titulo_alvo
    item.parcela = anterior.parcela
    item.evidencia_liquidacao = anterior.evidencia_liquidacao
    item.categoria = anterior.categoria
    item.pessoa = anterior.pessoa
    item.ambito = anterior.ambito
    item.observacao = anterior.observacao
    if anterior.titulos_multiplos.exists():
        item._copiar_titulos_multiplos_de = anterior.pk
    item.motivo_status = "Registro já confirmado em outra importação."


def _copiar_titulos_multiplos_reimportados(itens: list[ItemImportacao]) -> None:
    pares = {
        item.pk: item._copiar_titulos_multiplos_de
        for item in itens
        if getattr(item, "_copiar_titulos_multiplos_de", None)
    }
    if not pares:
        return
    vinculos_por_origem = {}
    for vinculo in VinculoTituloImportacao.objects.filter(item_id__in=pares.values()):
        vinculos_por_origem.setdefault(vinculo.item_id, []).append(vinculo)
    VinculoTituloImportacao.objects.bulk_create([
        VinculoTituloImportacao(
            item_id=item_id,
            titulo_id=vinculo.titulo_id,
            valor=vinculo.valor,
            liquidacao_id=vinculo.liquidacao_id,
        )
        for item_id, origem_id in pares.items()
        for vinculo in vinculos_por_origem.get(origem_id, [])
    ])


def _candidatos_vinculo_confiavel(item: ItemImportacao) -> list[tuple[str, object]]:
    """Retorna somente destinos de correspondência estrita e ainda utilizáveis."""
    valor = item.valor_final or ZERO
    if not item.data_final or not valor:
        return []

    usuario = item.importacao.usuario
    candidatos = []
    if item.importacao.origem == Importacao.Origem.CONTA:
        tipo_titulo = Titulo.Tipo.RECEBER if valor > ZERO else Titulo.Tipo.PAGAR
        titulos = Titulo.objects.filter(
            usuario=usuario,
            tipo=tipo_titulo,
            ativo=True,
            status=Titulo.Status.ABERTO,
            vencimento=item.data_final,
            saldo_aberto=abs(valor),
        ).filter(Q(conta=item.importacao.conta) | Q(conta__isnull=True))
        candidatos.extend(("TITULO", titulo) for titulo in titulos)

        ocupados = ItemImportacao.objects.filter(
            importacao__status=Importacao.Status.CONFIRMADA,
            movimento_conta__isnull=False,
        ).exclude(chave_conciliacao=item.chave_conciliacao).values_list("movimento_conta_id", flat=True)
        movimentos = MovimentoConta.objects.filter(
            conta=item.importacao.conta,
            evento__usuario=usuario,
            data_efetiva=item.data_final,
            valor=valor,
        ).exclude(pk__in=ocupados).select_related("evento")
        candidatos.extend(("MOVIMENTO", movimento) for movimento in movimentos)
        return candidatos

    parcelas = _parcelas_elegiveis_vinculo(item, usuario).filter(
        valor=abs(valor),
    ).filter(
        Q(data_original=item.data_final)
        | Q(data_original__isnull=True, compra__data_compra=item.data_final)
    ).select_related("compra__evento")
    if item.importacao.adaptador != "cartao_planilha" and item.numero_parcela and item.total_parcelas:
        parcelas = parcelas.filter(numero=item.numero_parcela, total=item.total_parcelas)
    candidatos.extend(("PARCELA", parcela) for parcela in parcelas)
    return candidatos


def sugerir_vinculo_confiavel(item: ItemImportacao) -> None:
    """Define a ação inicial a partir de destinos exatos, sem confirmar a conciliação."""
    candidatos = _candidatos_vinculo_confiavel(item)
    item.titulo_alvo = None
    item.movimento_conta = None
    item.parcela = None
    item.evento = None

    if len(candidatos) == 1:
        item.acao = ItemImportacao.Acao.VINCULAR
        item.status = ItemImportacao.Status.PENDENTE
        tipo, destino = candidatos[0]
        if tipo == "TITULO":
            item.titulo_alvo = destino
            descricao = "título"
        elif tipo == "MOVIMENTO":
            item.movimento_conta = destino
            item.evento = destino.evento
            descricao = "movimento"
        else:
            item.parcela = destino
            item.evento = destino.compra.evento
            descricao = "parcela"
        _adicionar_motivo(
            item,
            f"Sugestão automática: único {descricao} compatível por data e valor; confirme o vínculo na revisão.",
        )
    elif candidatos:
        item.acao = ItemImportacao.Acao.VINCULAR
        item.status = ItemImportacao.Status.PENDENTE
        _adicionar_motivo(
            item,
            f"Há {len(candidatos)} destinos compatíveis por data e valor; selecione o vínculo correto na revisão.",
        )
    else:
        item.acao = ItemImportacao.Acao.CRIAR
        item.status = ItemImportacao.Status.NOVO
        _adicionar_motivo(item, MOTIVO_CRIAR_SEM_CANDIDATO)


def _conciliar_evento_pagamento(item: ItemImportacao) -> bool:
    candidatos = EventoFinanceiro.objects.filter(
        usuario=item.importacao.usuario,
        natureza=NaturezaEvento.PAGAMENTO_FATURA,
        data=item.data,
        valor=abs(item.valor or ZERO),
    )
    if item.importacao.origem == Importacao.Origem.CONTA:
        candidatos = candidatos.filter(movimentos_conta__isnull=True)
    if candidatos.count() == 1:
        evento = candidatos.get()
        item.status = ItemImportacao.Status.CONCILIADO
        item.acao = ItemImportacao.Acao.VINCULAR
        item.evento = evento
        item.movimento_conta = evento.movimentos_conta.first()
        item.motivo_status = "Pagamento de fatura conciliado com a evidência já existente."
        return True
    if candidatos.count() > 1:
        item.status = ItemImportacao.Status.PENDENTE
        item.motivo_status = "Há mais de um pagamento de fatura compatível."
        return True
    return False


def _validar_titulo_alvo(item: ItemImportacao, titulo: Titulo) -> None:
    valor = item.valor_final or ZERO
    if (
        not _titulo_elegivel_para_item(item, titulo)
        or titulo.saldo_aberto < abs(valor)
    ):
        raise ValidationError("O título selecionado não está disponível para este item da importação.")


def _titulo_elegivel_para_item(item: ItemImportacao, titulo: Titulo) -> bool:
    valor = item.valor_final or ZERO
    tipo_esperado = Titulo.Tipo.RECEBER if valor > ZERO else Titulo.Tipo.PAGAR
    return bool(
        item.importacao.origem == Importacao.Origem.CONTA
        and valor
        and titulo.usuario_id == item.importacao.usuario_id
        and titulo.tipo == tipo_esperado
        and titulo.ativo
        and titulo.status == Titulo.Status.ABERTO
        and titulo.saldo_aberto > ZERO
        and (not titulo.conta_id or titulo.conta_id == item.importacao.conta_id)
    )


def _validar_titulos_multiplos(item: ItemImportacao, titulos: list[Titulo]) -> None:
    if len(titulos) < 2 or not all(_titulo_elegivel_para_item(item, titulo) for titulo in titulos):
        raise ValidationError("Os títulos selecionados não estão disponíveis para este item da importação.")
    total = sum((titulo.saldo_aberto for titulo in titulos), ZERO)
    if total != abs(item.valor_final or ZERO):
        raise ValidationError("A soma dos títulos selecionados deve ser exatamente igual ao valor do extrato.")


def sincronizar_titulos_multiplos(item: ItemImportacao, titulos_ids: list[int]) -> None:
    """Persiste a seleção manual já validada após o item possuir chave primária."""
    if len(titulos_ids) <= 1:
        item.titulos_multiplos.all().delete()
        return
    titulos = list(Titulo.objects.filter(pk__in=titulos_ids).order_by("id"))
    _validar_titulos_multiplos(item, titulos)
    item.titulos_multiplos.all().delete()
    VinculoTituloImportacao.objects.bulk_create([
        VinculoTituloImportacao(item=item, titulo=titulo, valor=titulo.saldo_aberto)
        for titulo in titulos
    ])


def _validar_movimento_alvo(item: ItemImportacao, movimento: MovimentoConta) -> None:
    valor = item.valor_final or ZERO
    ocupado = ItemImportacao.objects.filter(
        importacao__status=Importacao.Status.CONFIRMADA,
        movimento_conta=movimento,
    ).exclude(chave_conciliacao=item.chave_conciliacao).exists()
    if (
        item.importacao.origem != Importacao.Origem.CONTA
        or movimento.conta_id != item.importacao.conta_id
        or movimento.evento.usuario_id != item.importacao.usuario_id
        or not valor
        or (movimento.valor > ZERO) != (valor > ZERO)
        or ocupado
    ):
        raise ValidationError("O movimento selecionado não está disponível para este item da importação.")


def _validar_parcela_alvo(item: ItemImportacao, parcela: ParcelaCartao) -> None:
    if not _parcelas_elegiveis_vinculo(
        item,
        item.importacao.usuario,
    ).filter(pk=parcela.pk).exists():
        raise ValidationError("A parcela prevista selecionada não está disponível para este item da importação.")


def selecionar_vinculo_item(
    item: ItemImportacao,
    usuario,
    titulo_id=None,
    titulos_ids=None,
    movimento_id=None,
    parcela_id=None,
    remover_vinculo=False,
) -> ItemImportacao:
    titulos_ids = [int(titulo_id) for titulo_id in (titulos_ids or [])]
    if item.importacao.usuario_id != usuario.id:
        raise ValidationError("O item não pertence ao usuário informado.")
    if item.status == ItemImportacao.Status.CONCILIADO and item.evento_id and not item.vinculo_manual_pendente:
        titulo_informado = int(titulo_id) if titulo_id else None
        movimento_informado = int(movimento_id) if movimento_id else None
        parcela_informada = int(parcela_id) if parcela_id else None
        destinos_atuais = (item.titulo_alvo_id, item.movimento_conta_id, item.parcela_id)
        destinos_informados = (titulo_informado, movimento_informado, parcela_informada)
        titulos_atuais = list(item.titulos_multiplos.values_list("titulo_id", flat=True))
        if (destinos_informados == destinos_atuais and titulos_ids == titulos_atuais) or (
            not any(destinos_informados) and not titulos_ids and not any(destinos_atuais) and not titulos_atuais
        ):
            return item
        raise ValidationError("O item já está conciliado e seu destino não pode ser alterado.")

    if remover_vinculo:
        if any((titulo_id, titulos_ids, movimento_id, parcela_id)):
            raise ValidationError("Remova o destino selecionado antes de confirmar a remoção do vínculo.")
        item.titulo_alvo = None
        item.movimento_conta = None
        item.evento = None
        item.parcela = None
        item._titulos_multiplos_ids = []
        item._remover_vinculo = True
        item.status = ItemImportacao.Status.PENDENTE
        item.motivo_status = "Vínculo removido; selecione outro destino ou altere a ação."
        return item

    if item.acao != ItemImportacao.Acao.VINCULAR:
        item.titulo_alvo = None
        item.movimento_conta = None
        item.evento = None
        item.parcela = None
        item._titulos_multiplos_ids = []
        return item

    destinos = int(bool(titulo_id)) + int(bool(titulos_ids)) + int(bool(movimento_id)) + int(bool(parcela_id))
    if destinos != 1:
        raise ValidationError("Selecione títulos ou exatamente um destino para vincular.")
    if titulos_ids:
        if len(titulos_ids) != len(set(titulos_ids)):
            raise ValidationError("O mesmo título não pode ser selecionado mais de uma vez.")
        titulos = list(Titulo.objects.select_related("conta", "evento").filter(pk__in=titulos_ids, usuario=usuario).order_by("id"))
        if len(titulos) != len(titulos_ids):
            raise ValidationError("Um ou mais títulos selecionados não foram encontrados.")
        if len(titulos) == 1:
            titulo_id = titulos[0].pk
        else:
            _validar_titulos_multiplos(item, titulos)
            item.titulo_alvo = None
            item.movimento_conta = None
            item.evento = None
            item.parcela = None
            item.status = ItemImportacao.Status.CONCILIADO
            item.motivo_status = "Títulos selecionados manualmente; as baixas serão realizadas na confirmação."
            item._titulos_multiplos_ids = [titulo.pk for titulo in titulos]
            return item
    if titulo_id:
        titulo = Titulo.objects.select_related("conta", "evento").filter(pk=titulo_id, usuario=usuario).first()
        if not titulo:
            raise ValidationError("O título selecionado não foi encontrado.")
        _validar_titulo_alvo(item, titulo)
        item.titulo_alvo = titulo
        item.movimento_conta = None
        item.evento = None
        item.parcela = None
        item._titulos_multiplos_ids = []
        item.status = ItemImportacao.Status.CONCILIADO
        item.motivo_status = "Título selecionado manualmente; a baixa será realizada na confirmação."
        return item

    movimento = MovimentoConta.objects.select_related("conta", "evento").filter(pk=movimento_id).first()
    if movimento_id:
        if not movimento:
            raise ValidationError("O movimento selecionado não foi encontrado.")
        _validar_movimento_alvo(item, movimento)
        item.titulo_alvo = None
        item.movimento_conta = movimento
        item.evento = movimento.evento
        item.parcela = None
        item._titulos_multiplos_ids = []
        item.status = ItemImportacao.Status.CONCILIADO
        item.motivo_status = "Movimento existente selecionado manualmente."
        return item

    parcela = (
        ParcelaCartao.objects.select_related("compra__evento", "compra__cartao", "fatura")
        .filter(pk=parcela_id, compra__usuario=usuario)
        .first()
    )
    if not parcela:
        raise ValidationError("A parcela prevista selecionada não foi encontrada.")
    _validar_parcela_alvo(item, parcela)
    item.titulo_alvo = None
    item.movimento_conta = None
    item.parcela = parcela
    item.evento = parcela.compra.evento
    item._titulos_multiplos_ids = []
    item.status = ItemImportacao.Status.CONCILIADO
    item.motivo_status = "Parcela prevista selecionada manualmente; a realização ocorrerá na confirmação."
    return item


def atualizar_status_revisao(item: ItemImportacao) -> None:
    if getattr(item, "_remover_vinculo", False):
        item.status = ItemImportacao.Status.PENDENTE
        item.revisado_em = timezone.now()
        return
    possui_titulos_multiplos = bool(getattr(item, "_titulos_multiplos_ids", [])) or item.titulos_multiplos.exists()
    if item.acao == ItemImportacao.Acao.VINCULAR and (
        item.evento_id or item.titulo_alvo_id or item.parcela_id or possui_titulos_multiplos
    ):
        item.status = ItemImportacao.Status.CONCILIADO
        if not item.motivo_status:
            item.motivo_status = "Divergência aceita e vinculada pelo usuário."
    elif item.acao == ItemImportacao.Acao.CRIAR and item.data_final and item.valor_final is not None:
        item.status = ItemImportacao.Status.NOVO
        _adicionar_motivo(item, "Revisado pelo usuário.")
    item.revisado_em = timezone.now()


@transaction.atomic
def aplicar_alteracoes_lote(importacao_id: int, usuario, dados: dict, itens_selecionados: list[str]) -> dict:
    importacao = Importacao.objects.select_for_update().get(
        pk=importacao_id,
        usuario=usuario,
        status=Importacao.Status.EM_REVISAO,
    )
    itens = importacao.itens.select_for_update().order_by("posicao", "id")
    escopo = dados.get("somente_status")
    if escopo == "SELECIONADOS":
        try:
            ids = {int(item_id) for item_id in itens_selecionados}
        except (TypeError, ValueError):
            raise ValidationError("A seleção contém um item inválido.")
        itens = itens.filter(pk__in=ids)
        if itens.count() != len(ids):
            raise ValidationError("Um ou mais itens selecionados não pertencem a esta importação.")
    elif escopo:
        itens = itens.filter(status=escopo)

    quantidade_preservada = itens.filter(revisado_em__isnull=False).count()
    itens = list(itens.filter(revisado_em__isnull=True))
    if not itens:
        if quantidade_preservada:
            preservados = (
                "1 item já revisado foi preservado."
                if quantidade_preservada == 1
                else f"{quantidade_preservada} itens já revisados foram preservados."
            )
            raise ValidationError(
                "Nenhum item não revisado corresponde ao escopo informado; "
                f"{preservados}"
            )
        raise ValidationError("Nenhum item corresponde ao escopo informado.")

    acao = dados.get("acao")
    if acao == ItemImportacao.Acao.VINCULAR:
        raise ValidationError("Escolha o destino de cada vínculo diretamente no item da importação.")
    if acao == ItemImportacao.Acao.CRIAR and any(item.evento_id or item.titulo_alvo_id for item in itens):
        raise ValidationError("Itens já conciliados não podem ser alterados para Criar lançamento.")

    campos = ("categoria", "pessoa", "ambito", "acao", "observacao")
    contexto_classificacao = preparar_contexto_classificacao(importacao, itens)
    for item in itens:
        acao_anterior = item.acao
        status_anterior = item.status
        for campo in campos:
            valor = dados.get(campo)
            if valor not in (None, ""):
                setattr(item, campo, valor)
        if (
            item.acao == ItemImportacao.Acao.CRIAR
            and (acao_anterior != ItemImportacao.Acao.CRIAR or status_anterior != ItemImportacao.Status.NOVO)
        ):
            protegidos = {
                campo
                for campo in ("categoria", "pessoa", "ambito")
                if dados.get(campo) not in (None, "", Ambito.NAO_DEFINIDO)
            }
            aplicar_classificacao_primeira_revisao(
                item,
                campos_protegidos=protegidos,
                contexto=contexto_classificacao,
            )
        atualizar_status_revisao(item)
        item.save(
            update_fields=[
                "categoria",
                "pessoa",
                "ambito",
                "acao",
                "observacao",
                "revisado_em",
                "status",
                "motivo_status",
                "atualizado_em",
            ]
        )
    return {"itens": itens, "quantidade_preservada": quantidade_preservada}


@transaction.atomic
def confirmar_importacao(importacao_id: int, usuario) -> Importacao:
    importacao = Importacao.objects.select_for_update().get(pk=importacao_id, usuario=usuario)
    if importacao.status != Importacao.Status.EM_REVISAO:
        raise ValidationError("Somente lotes em revisão podem ser confirmados.")
    item_ids = list(importacao.itens.select_for_update().values_list("id", flat=True))
    itens = list(
        ItemImportacao.objects.filter(id__in=item_ids).select_related(
            "evento", "movimento_conta", "titulo_alvo__evento", "parcela", "categoria", "pessoa"
        ).prefetch_related("titulos_multiplos__titulo__evento")
    )
    itens_com_titulos_multiplos = set(
        VinculoTituloImportacao.objects.filter(item_id__in=item_ids).values_list("item_id", flat=True)
    )
    if importacao.origem == Importacao.Origem.CONTA:
        Conta.objects.select_for_update().get(pk=importacao.conta_id, usuario=usuario)
    else:
        Cartao.objects.select_for_update().get(pk=importacao.cartao_id, usuario=usuario)
    for item in itens:
        if item.status == ItemImportacao.Status.NOVO and item.acao != ItemImportacao.Acao.IGNORAR:
            if importacao.adaptador != "cartao_planilha":
                _conciliar_item(item)
            item.save(
                update_fields=[
                    "status",
                    "acao",
                    "evento",
                    "movimento_conta",
                    "titulo_alvo",
                    "parcela",
                    "evidencia_liquidacao",
                    "categoria",
                    "pessoa",
                    "ambito",
                    "motivo_status",
                    "atualizado_em",
                ]
            )
    for item in itens:
        if (
            item.status == ItemImportacao.Status.PENDENTE
            and item.acao == ItemImportacao.Acao.VINCULAR
            and not any((item.titulo_alvo_id, item.movimento_conta_id, item.parcela_id, item.evento_id))
            and item.motivo_status == MOTIVO_SEM_CANDIDATO_CONFIAVEL
        ):
            item.acao = ItemImportacao.Acao.CRIAR
            item.status = ItemImportacao.Status.NOVO
            item.motivo_status = "Sem destino selecionado; lançamento novo criado na confirmação."
            item.save(update_fields=["acao", "status", "motivo_status", "atualizado_em"])
    bloqueios = [
        item for item in itens
        if item.acao != ItemImportacao.Acao.IGNORAR
        and (
            item.status in {ItemImportacao.Status.PENDENTE, ItemImportacao.Status.DIVERGENTE}
            or not item.data_final
            or item.valor_final is None
            or (
                item.acao == ItemImportacao.Acao.VINCULAR
                and not (item.titulo_alvo_id or item.movimento_conta_id or item.evento_id or item.parcela_id)
                and item.pk not in itens_com_titulos_multiplos
            )
        )
    ]
    if bloqueios:
        raise ValidationError(f"Existem {len(bloqueios)} item(ns) com pendência estrutural ou divergência.")

    faturas_afetadas = set()
    for item in itens:
        if item.acao == ItemImportacao.Acao.IGNORAR:
            continue
        if importacao.adaptador == "cartao_planilha":
            if item.status == ItemImportacao.Status.CONCILIADO and item.parcela_id:
                parcela = ParcelaCartao.objects.select_for_update().get(pk=item.parcela_id)
                _validar_parcela_alvo(item, parcela)
            if item.status != ItemImportacao.Status.NOVO:
                continue
            if item.natureza == NaturezaEvento.PAGAMENTO_FATURA:
                _confirmar_item_cartao(item)
                item.status = ItemImportacao.STATUS_PLANEJADO
                _adicionar_motivo(item, "Pagamento de fatura registrado a partir da planilha.")
                item.save(update_fields=["status", "evento", "evidencia_liquidacao", "motivo_status", "atualizado_em"])
                continue
            from financeiro.services.importacao_planilha import criar_compra_planejada

            compra, parcela = criar_compra_planejada(item)
            item.evento = compra.evento
            item.parcela = parcela
            item.status = ItemImportacao.STATUS_PLANEJADO
            _adicionar_motivo(item, "Compra futura criada a partir da planilha.")
            faturas_afetadas.update(compra.parcelas.values_list("fatura_id", flat=True))
            item.save(update_fields=["status", "evento", "parcela", "motivo_status", "atualizado_em"])
            continue
        if item.status == ItemImportacao.Status.CONCILIADO:
            if importacao.origem == Importacao.Origem.CONTA and item.pk in itens_com_titulos_multiplos:
                _confirmar_vinculos_multiplos_titulo(item)
                item.save(update_fields=[
                    "evento",
                    "movimento_conta",
                    "evidencia_liquidacao",
                    "motivo_status",
                    "atualizado_em",
                ])
                continue
            if importacao.origem == Importacao.Origem.CONTA and item.titulo_alvo_id:
                _confirmar_vinculo_titulo(item)
                item.save(update_fields=[
                    "evento",
                    "movimento_conta",
                    "evidencia_liquidacao",
                    "motivo_status",
                    "atualizado_em",
                ])
                continue
            if importacao.origem == Importacao.Origem.CONTA and item.movimento_conta_id:
                _confirmar_vinculo_movimento(item)
                item.save(update_fields=["evento", "movimento_conta", "motivo_status", "atualizado_em"])
                continue
            if importacao.origem == Importacao.Origem.CONTA and item.evento_id and not item.movimento_conta_id:
                movimento = _criar_movimento_conta(item, item.evento)
                item.movimento_conta = movimento
                item.save(update_fields=["movimento_conta", "atualizado_em"])
                if item.evento.natureza == NaturezaEvento.PAGAMENTO_FATURA:
                    liquidacao = _tentar_liquidar_fatura(movimento)
                    if liquidacao:
                        _vincular_evidencia_pagamento(item, liquidacao, EvidenciaLiquidacao.Tipo.OFX_CONTA)
                        item.save(update_fields=["evidencia_liquidacao", "atualizado_em"])
            if importacao.origem == Importacao.Origem.CARTAO and item.parcela_id:
                faturas_afetadas.add(_confirmar_vinculo_parcela(item))
                item.save(update_fields=["evento", "parcela", "motivo_status", "atualizado_em"])
            continue
        if importacao.origem == Importacao.Origem.CONTA:
            _confirmar_item_conta(item)
        else:
            faturas_afetadas.update(_confirmar_item_cartao(item))
        item.save(update_fields=["evento", "movimento_conta", "parcela", "evidencia_liquidacao", "atualizado_em"])

    for fatura_id in faturas_afetadas:
        recalcular_fatura(fatura_id)
    if importacao.origem == Importacao.Origem.CONTA and importacao.conta_id:
        reprocessar_saldo_conta(importacao.conta_id)
    importacao.status = Importacao.Status.CONFIRMADA
    importacao.save(update_fields=["status", "atualizado_em"])
    return importacao


def _novo_evento(item: ItemImportacao, valor: Decimal | None = None) -> EventoFinanceiro:
    evento = EventoFinanceiro.objects.create(
        usuario=item.importacao.usuario,
        natureza=item.natureza or natureza_sugerida(item.importacao.origem, item.tipo_normalizado, item.valor_final, item.descricao_final),
        descricao=item.descricao_final[:220],
        data=item.data_final,
        valor=abs(valor if valor is not None else item.valor_final),
        pessoa=item.pessoa,
        ambito=item.ambito or Ambito.NAO_DEFINIDO,
        situacao=Situacao.REALIZADO,
        observacao=item.observacao.strip(),
    )
    if item.categoria_id:
        RateioCategoria.objects.create(evento=evento, categoria=item.categoria, valor=evento.valor)
    return evento


def _copiar_rateios_proporcionais(evento_origem: EventoFinanceiro, evento_destino: EventoFinanceiro) -> None:
    rateios = list(evento_origem.rateios.select_related("categoria").order_by("id"))
    total_origem = sum((rateio.valor for rateio in rateios), ZERO)
    if not rateios or total_origem <= ZERO:
        return
    restante = evento_destino.valor
    novos = []
    for indice, rateio in enumerate(rateios):
        if indice == len(rateios) - 1:
            valor = restante
        else:
            valor = (evento_destino.valor * rateio.valor / total_origem).quantize(CENTAVO, rounding=ROUND_HALF_UP)
            valor = min(valor, restante)
        restante -= valor
        novos.append(RateioCategoria(evento=evento_destino, categoria=rateio.categoria, valor=valor))
    RateioCategoria.objects.bulk_create(novos)


def _movimento_importado_para_titulos(
    item: ItemImportacao,
    titulo_referencia: Titulo,
    usar_dados_item=False,
) -> tuple[EventoFinanceiro, MovimentoConta]:
    chave_movimento = f"ofx:{item.chave_conciliacao}"
    movimento = MovimentoConta.objects.select_related("evento").filter(
        conta=item.importacao.conta,
        chave_idempotencia=chave_movimento,
    ).first()
    if movimento:
        if movimento.valor != item.valor_final or movimento.data_efetiva != item.data_final:
            raise ValidationError("A chave OFX já pertence a um movimento com data ou valor diferente.")
        return movimento.evento, movimento

    previsto = titulo_referencia.evento
    natureza = (
        previsto.natureza
        if previsto
        else NaturezaEvento.PAGAMENTO_FATURA
        if titulo_referencia.fatura_id
        else NaturezaEvento.RECEITA
        if titulo_referencia.tipo == Titulo.Tipo.RECEBER
        else NaturezaEvento.DESPESA
    )
    observacao = "\n\n".join(
        parte
        for parte in (
            previsto.observacao.strip() if previsto else "",
            item.observacao.strip(),
        )
        if parte
    )
    evento = EventoFinanceiro.objects.create(
        usuario=item.importacao.usuario,
        natureza=natureza,
        descricao=item.descricao_final[:220] if usar_dados_item else titulo_referencia.descricao[:220],
        data=item.data_final,
        valor=abs(item.valor_final or ZERO),
        pessoa=(item.pessoa or (previsto.pessoa if previsto else None)) if usar_dados_item else previsto.pessoa if previsto else None,
        pessoa_nome_livre=(previsto.pessoa_nome_livre if previsto and not item.pessoa_id else "") if usar_dados_item else previsto.pessoa_nome_livre if previsto else "",
        ambito=(item.ambito if item.ambito != Ambito.NAO_DEFINIDO else previsto.ambito if previsto else Ambito.NAO_DEFINIDO) if usar_dados_item else previsto.ambito if previsto else Ambito.NAO_DEFINIDO,
        situacao=Situacao.REALIZADO,
        observacao=observacao,
    )
    if usar_dados_item and item.categoria_id:
        RateioCategoria.objects.create(evento=evento, categoria=item.categoria, valor=evento.valor)
    elif previsto:
        _copiar_rateios_proporcionais(previsto, evento)
    movimento = _criar_movimento_conta(item, evento, chave_idempotencia=chave_movimento)
    return evento, movimento


def _confirmar_vinculos_multiplos_titulo(item: ItemImportacao) -> None:
    vinculos = list(
        VinculoTituloImportacao.objects.select_for_update()
        .filter(item=item)
        .order_by("id")
    )
    titulos_por_id = {
        titulo.pk: titulo
        for titulo in Titulo.objects.select_for_update().filter(
            pk__in=[vinculo.titulo_id for vinculo in vinculos]
        )
    }
    if len(titulos_por_id) != len(vinculos):
        raise ValidationError("Um dos títulos selecionados não está mais disponível.")
    for vinculo in vinculos:
        vinculo.titulo = titulos_por_id[vinculo.titulo_id]
    if vinculos and all(vinculo.liquidacao_id for vinculo in vinculos):
        liquidacoes = [
            Liquidacao.objects.select_for_update().get(pk=vinculo.liquidacao_id)
            for vinculo in vinculos
        ]
        movimento_ids = {liquidacao.movimento_conta_id for liquidacao in liquidacoes}
        if (
            len(movimento_ids) != 1
            or any(
                liquidacao.status != Liquidacao.Status.ATIVA
                or liquidacao.titulo_id != vinculo.titulo_id
                or not liquidacao.movimento_conta_id
                for vinculo, liquidacao in zip(vinculos, liquidacoes)
            )
        ):
            raise ValidationError("Os vínculos anteriores dos títulos não correspondem à conciliação informada.")
        movimento = MovimentoConta.objects.select_related("evento").get(pk=liquidacoes[0].movimento_conta_id)
        item.evento = movimento.evento
        item.movimento_conta = movimento
        item.evidencia_liquidacao = None
        item.motivo_status = "Registro já confirmado em outra importação."
        return
    if any(vinculo.liquidacao_id for vinculo in vinculos):
        raise ValidationError("A conciliação múltipla possui vínculos incompletos e não pode ser confirmada.")
    titulos = [vinculo.titulo for vinculo in vinculos]
    _validar_titulos_multiplos(item, titulos)
    if any(vinculo.valor != vinculo.titulo.saldo_aberto for vinculo in vinculos):
        raise ValidationError("O saldo de um dos títulos selecionados foi alterado; revise o vínculo antes de confirmar.")

    evento, movimento = _movimento_importado_para_titulos(item, titulos[0], usar_dados_item=True)
    for vinculo in vinculos:
        liquidacao = liquidar_titulo(
            vinculo.titulo,
            movimento,
            vinculo.valor,
            chave=f"ofx:{item.chave_conciliacao}:titulo:{vinculo.titulo_id}",
        )
        vinculo.liquidacao = liquidacao
        vinculo.save(update_fields=["liquidacao", "atualizado_em"])
        _vincular_evidencia_pagamento(
            item,
            liquidacao,
            EvidenciaLiquidacao.Tipo.OFX_CONTA,
            chave=f"{item.chave_conciliacao}:titulo:{vinculo.titulo_id}",
            vincular_item=False,
        )
    item.evento = evento
    item.movimento_conta = movimento
    item.evidencia_liquidacao = None
    item.motivo_status = f"{len(vinculos)} títulos conciliados com o movimento importado."


def _confirmar_vinculo_titulo(item: ItemImportacao) -> None:
    # O PostgreSQL não permite FOR UPDATE no lado anulável de um OUTER JOIN.
    # Bloqueamos somente o título; conta e item já foram bloqueados pelo fluxo
    # chamador, e as relações opcionais podem ser carregadas sob a transação.
    titulo = Titulo.objects.select_for_update().get(
        pk=item.titulo_alvo_id,
        usuario=item.importacao.usuario,
    )
    if item.evidencia_liquidacao_id:
        evidencia = EvidenciaLiquidacao.objects.select_for_update().get(
            pk=item.evidencia_liquidacao_id
        )
        liquidacao = Liquidacao.objects.select_for_update().get(pk=evidencia.liquidacao_id)
        if (
            evidencia.usuario_id != item.importacao.usuario_id
            or evidencia.tipo != EvidenciaLiquidacao.Tipo.OFX_CONTA
            or evidencia.chave != item.chave_conciliacao
            or liquidacao.titulo_id != titulo.pk
            or liquidacao.status != Liquidacao.Status.ATIVA
            or not liquidacao.movimento_conta_id
        ):
            raise ValidationError("A evidência anterior não corresponde ao título selecionado.")
        movimento = MovimentoConta.objects.select_related("evento").get(pk=liquidacao.movimento_conta_id)
        item.movimento_conta = movimento
        item.evento = movimento.evento
        item.motivo_status = "Registro já confirmado em outra importação."
        return
    _validar_titulo_alvo(item, titulo)
    valor = abs(item.valor_final or ZERO)
    evento, movimento = _movimento_importado_para_titulos(item, titulo)
    liquidacao = liquidar_titulo(
        titulo,
        movimento,
        valor,
        chave=f"ofx:{item.chave_conciliacao}",
    )
    item.evento = evento
    item.movimento_conta = movimento
    _vincular_evidencia_pagamento(item, liquidacao, EvidenciaLiquidacao.Tipo.OFX_CONTA)
    item.motivo_status = "Título conciliado com o movimento importado."


def _confirmar_vinculo_movimento(item: ItemImportacao) -> None:
    movimento = (
        MovimentoConta.objects.select_for_update()
        .select_related("evento")
        .get(pk=item.movimento_conta_id)
    )
    _validar_movimento_alvo(item, movimento)
    item.movimento_conta = movimento
    item.evento = movimento.evento
    if item.motivo_status != "Registro já confirmado em outra importação.":
        item.motivo_status = "Movimento existente conciliado com a evidência OFX."


def _confirmar_vinculo_parcela(item: ItemImportacao) -> int:
    parcela = (
        ParcelaCartao.objects.select_for_update()
        .select_related("compra__evento", "compra__cartao", "fatura")
        .get(pk=item.parcela_id)
    )
    mesma_evidencia_confirmada = ItemImportacao.objects.filter(
        importacao__status=Importacao.Status.CONFIRMADA,
        chave_conciliacao=item.chave_conciliacao,
        parcela=parcela,
    ).exists()
    if parcela.situacao != Situacao.REALIZADO or not mesma_evidencia_confirmada:
        _validar_parcela_alvo(item, parcela)
        parcela.situacao = Situacao.REALIZADO
        parcela.valor = abs(item.valor_final)
        parcela.save(update_fields=["situacao", "valor", "atualizado_em"])
    item.parcela = parcela
    item.evento = parcela.compra.evento
    if item.motivo_status != "Registro já confirmado em outra importação.":
        item.motivo_status = "Parcela prevista conciliada com a evidência OFX."
    return parcela.fatura_id


def _confirmar_item_conta(item: ItemImportacao) -> None:
    evento = None
    if item.natureza == NaturezaEvento.PAGAMENTO_FATURA:
        candidatos = EventoFinanceiro.objects.filter(
            usuario=item.importacao.usuario,
            natureza=NaturezaEvento.PAGAMENTO_FATURA,
            data=item.data_final,
            valor=abs(item.valor_final),
            movimentos_conta__isnull=True,
        )
        if candidatos.count() == 1:
            evento = candidatos.get()
    evento = evento or _novo_evento(item)
    movimento = _criar_movimento_conta(item, evento)
    item.evento = evento
    item.movimento_conta = movimento
    if evento.natureza == NaturezaEvento.PAGAMENTO_FATURA:
        liquidacao = _tentar_liquidar_fatura(movimento)
        if liquidacao:
            _vincular_evidencia_pagamento(item, liquidacao, EvidenciaLiquidacao.Tipo.OFX_CONTA)
            for evidencia_cartao in ItemImportacao.objects.filter(
                importacao__origem=Importacao.Origem.CARTAO,
                importacao__usuario=item.importacao.usuario,
                evento=evento,
                evidencia_liquidacao__isnull=True,
            ):
                _vincular_evidencia_pagamento(evidencia_cartao, liquidacao, EvidenciaLiquidacao.Tipo.OFX_CARTAO)
                evidencia_cartao.save(update_fields=["evidencia_liquidacao", "atualizado_em"])


def _criar_movimento_conta(item: ItemImportacao, evento: EventoFinanceiro, chave_idempotencia="") -> MovimentoConta:
    return MovimentoConta.objects.create(
        conta=item.importacao.conta,
        evento=evento,
        data_efetiva=item.data_final,
        valor=item.valor_final,
        documento=item.documento,
        historico_original=item.descricao_original,
        chave_idempotencia=chave_idempotencia,
    )


def _vincular_evidencia_pagamento(
    item: ItemImportacao,
    liquidacao: Liquidacao,
    tipo: str,
    chave: str | None = None,
    vincular_item=True,
) -> None:
    evidencia, _ = EvidenciaLiquidacao.objects.get_or_create(
        usuario=item.importacao.usuario,
        tipo=tipo,
        chave=chave or item.chave_conciliacao,
        defaults={
            "liquidacao": liquidacao,
            "dados": {"importacao": item.importacao_id, "item": item.pk, "descricao": item.descricao_final},
        },
    )
    if evidencia.liquidacao_id != liquidacao.pk:
        raise ValidationError("A evidência OFX já está vinculada a outra liquidação.")
    if vincular_item:
        item.evidencia_liquidacao = evidencia


def _tentar_liquidar_fatura(movimento: MovimentoConta):
    valor = abs(movimento.valor)
    candidatos = Titulo.objects.select_for_update().filter(
        usuario=movimento.conta.usuario,
        tipo=Titulo.Tipo.PAGAR,
        ativo=True,
        fatura__isnull=False,
        saldo_aberto__gte=valor,
    ).filter(Q(conta=movimento.conta) | Q(conta__isnull=True))
    exatos = candidatos.filter(saldo_aberto=valor)
    if exatos.count() == 1:
        titulo = exatos.get()
    elif candidatos.count() == 1:
        titulo = candidatos.get()
    else:
        return None
    return liquidar_titulo(titulo, movimento, valor)


def _confirmar_item_cartao(item: ItemImportacao) -> set[int]:
    descricao_normalizada = normalize_text(item.descricao_final)
    if "SALDO" in descricao_normalizada and ("ANTERIOR" in descricao_normalizada or "FATURA ANTERIOR" in descricao_normalizada):
        destino, _ = Fatura.objects.get_or_create(
            usuario=item.importacao.usuario,
            cartao=item.importacao.cartao,
            ciclo=item.importacao.ciclo,
            defaults={"status": Fatura.Status.ABERTA},
        )
        valor = abs(item.valor_final)
        candidatos = list(
            Titulo.objects.select_for_update().filter(
                usuario=item.importacao.usuario,
                fatura__cartao=item.importacao.cartao,
                fatura__ciclo__lt=destino.ciclo,
                ativo=True,
                saldo_aberto=valor,
            ).order_by("-fatura__ciclo", "-id")
        )
        if len(candidatos) == 1:
            criar_transporte(
                item.importacao.usuario,
                candidatos[0].pk,
                destino.pk,
                valor,
                item.data_final,
                chave=f"ofx:{item.chave_conciliacao}",
                observacao="Saldo anterior identificado na importação OFX do cartão.",
            )
            item.observacao = "Saldo anterior conciliado como transporte de obrigação."
            return {destino.pk}
        item.status = ItemImportacao.Status.PENDENTE
        item.motivo_status = "Saldo anterior sem origem inequívoca; concilie manualmente."
        return set()
    if item.natureza == NaturezaEvento.PAGAMENTO_FATURA:
        candidatos = EventoFinanceiro.objects.filter(
            usuario=item.importacao.usuario,
            natureza=NaturezaEvento.PAGAMENTO_FATURA,
            data=item.data_final,
            valor=abs(item.valor_final),
        )
        item.evento = candidatos.get() if candidatos.count() == 1 else _novo_evento(item)
        liquidacao = Liquidacao.objects.filter(
            movimento_conta__evento=item.evento, status=Liquidacao.Status.ATIVA
        ).first()
        if liquidacao:
            _vincular_evidencia_pagamento(item, liquidacao, EvidenciaLiquidacao.Tipo.OFX_CARTAO)
        return set()
    numero = item.numero_parcela or 1
    total = item.total_parcelas or 1
    valor_parcela = abs(item.valor_final)
    valor_total = valor_parcela * total
    evento = _novo_evento(item, valor_total)
    chave_compra = fingerprint(item.importacao.cartao_id, estabelecimento_normalizado(item.descricao_final), total, item.data_final)
    compra = CompraCartao.objects.create(
        usuario=item.importacao.usuario,
        cartao=item.importacao.cartao,
        evento=evento,
        estabelecimento=item.descricao_final[:180],
        data_compra=item.data_final,
        valor_total=valor_total,
        valor_total_estimado=total > 1,
        total_parcelas=total,
        chave_compra=chave_compra,
    )
    faturas = set()
    parcela_atual = None
    for parcela_numero in range(numero, total + 1):
        ciclo = proximo_ciclo(item.importacao.ciclo, parcela_numero - numero)
        fatura, _ = Fatura.objects.get_or_create(
            usuario=item.importacao.usuario,
            cartao=item.importacao.cartao,
            ciclo=ciclo,
            defaults={"status": Fatura.Status.ABERTA},
        )
        parcela = ParcelaCartao.objects.create(
            compra=compra,
            fatura=fatura,
            natureza=evento.natureza,
            descricao=item.descricao_final[:220],
            data_original=item.data_final,
            vencimento=resolver_vencimento_fatura(fatura),
            categoria=item.categoria,
            pessoa=item.pessoa,
            ambito=item.ambito or Ambito.NAO_DEFINIDO,
            numero=parcela_numero,
            total=total,
            valor=valor_parcela,
            situacao=Situacao.REALIZADO if parcela_numero == numero else Situacao.PREVISTO,
        )
        faturas.add(fatura.id)
        if parcela_numero == numero:
            parcela_atual = parcela
    item.evento = evento
    item.parcela = parcela_atual
    return faturas


def recalcular_fatura(fatura_id: int) -> None:
    fatura = Fatura.objects.select_for_update().get(pk=fatura_id)
    parcelas = fatura.parcelas.filter(situacao=Situacao.REALIZADO).select_related("compra__evento")
    bruto = sum((parcela.valor for parcela in parcelas if parcela.natureza_exibicao == NaturezaEvento.DESPESA), ZERO)
    creditos = sum((parcela.valor for parcela in parcelas if parcela.natureza_exibicao == NaturezaEvento.ESTORNO), ZERO)
    fatura.total_bruto = bruto
    fatura.total_creditos = creditos
    fatura.total = bruto - creditos
    fatura.total_transportado = SaldoTransportadoFatura.objects.filter(
        fatura_destino=fatura,
        status__in=[SaldoTransportadoFatura.Status.CONFIRMADO, SaldoTransportadoFatura.Status.INCORPORADO],
    ).aggregate(valor=Sum("valor", default=ZERO))["valor"] or ZERO
    fatura.total_exigido = fatura.total + fatura.total_transportado
    fatura.save(update_fields=["total_bruto", "total_creditos", "total", "total_transportado", "total_exigido", "atualizado_em"])


@transaction.atomic
def desfazer_importacao(importacao_id: int, usuario) -> Importacao:
    importacao = Importacao.objects.select_for_update().get(pk=importacao_id, usuario=usuario)
    if importacao.status != Importacao.Status.CONFIRMADA:
        raise ValidationError("Somente lotes confirmados podem ser desfeitos.")
    item_ids = list(importacao.itens.select_for_update().values_list("id", flat=True))
    itens = list(ItemImportacao.objects.filter(id__in=item_ids).select_related(
        "evento", "movimento_conta", "titulo_alvo", "parcela", "evidencia_liquidacao"
    ))
    vinculos_multiplos = list(
        VinculoTituloImportacao.objects.filter(item_id__in=item_ids).select_related("liquidacao")
    )
    eventos_novos = {
        item.evento_id
        for item in itens
        if item.evento_id
        and (
            item.status == ItemImportacao.Status.NOVO
            or (importacao.adaptador == "cartao_planilha" and item.status == ItemImportacao.STATUS_PLANEJADO)
        )
    }
    eventos_titulo_criados = {
        item.evento_id
        for item in itens
        if item.titulo_alvo_id
        and item.evento_id
        and item.evidencia_liquidacao_id
        and item.evidencia_liquidacao.dados.get("importacao") == importacao.pk
    }
    liquidacoes_multiplas_criadas = {
        vinculo.liquidacao_id
        for vinculo in vinculos_multiplos
        if vinculo.liquidacao_id
        and EvidenciaLiquidacao.objects.filter(
            liquidacao_id=vinculo.liquidacao_id,
            dados__importacao=importacao.pk,
        ).exists()
    }
    eventos_titulos_multiplos_criados = {
        item.evento_id
        for item in itens
        if item.evento_id
        and any(
            vinculo.item_id == item.pk and vinculo.liquidacao_id in liquidacoes_multiplas_criadas
            for vinculo in vinculos_multiplos
        )
    }
    eventos_criados = eventos_novos | eventos_titulo_criados | eventos_titulos_multiplos_criados
    for evento_id in eventos_criados:
        if ItemImportacao.objects.filter(evento_id=evento_id, importacao__status=Importacao.Status.CONFIRMADA).exclude(importacao=importacao).exists():
            raise ValidationError("O lote possui lançamentos usados por importações posteriores e não pode ser desfeito.")
        if EventoFinanceiro.objects.filter(pk=evento_id, atualizado_em__gt=importacao.atualizado_em).exists():
            raise ValidationError("O lote possui lançamentos alterados após a confirmação e não pode ser desfeito automaticamente.")
    faturas = set(
        ParcelaCartao.objects.filter(compra__evento_id__in=eventos_criados).values_list("fatura_id", flat=True)
    )
    faturas.update(item.parcela.fatura_id for item in itens if item.parcela_id)
    for item in itens:
        item.evento = None
        item.movimento_conta = None
        item.titulo_alvo = None
        item.parcela = None
        item.evidencia_liquidacao = None
        item.save(update_fields=[
            "evento",
            "movimento_conta",
            "titulo_alvo",
            "parcela",
            "evidencia_liquidacao",
            "atualizado_em",
        ])
    for evento_id in eventos_criados:
        liquidacoes = list(
            Liquidacao.objects.filter(movimento_conta__evento_id=evento_id).select_related("titulo__fatura")
        )
        titulos_ids = {liquidacao.titulo_id for liquidacao in liquidacoes}
        VinculoTituloImportacao.objects.filter(
            liquidacao_id__in=[liquidacao.pk for liquidacao in liquidacoes]
        ).update(liquidacao=None)
        EvidenciaLiquidacao.objects.filter(liquidacao_id__in=[liquidacao.pk for liquidacao in liquidacoes]).delete()
        Liquidacao.objects.filter(pk__in=[liquidacao.pk for liquidacao in liquidacoes]).delete()
        for titulo_id in titulos_ids:
            titulo = Titulo.objects.select_for_update().get(pk=titulo_id)
            recalcular_titulo(titulo)
        EventoFinanceiro.objects.filter(pk=evento_id).delete()
    for fatura_id in faturas:
        if not ParcelaCartao.objects.filter(fatura_id=fatura_id).exists() and not Titulo.objects.filter(fatura_id=fatura_id).exists():
            Fatura.objects.filter(pk=fatura_id).delete()
        else:
            recalcular_fatura(fatura_id)
    if importacao.origem == Importacao.Origem.CONTA and importacao.conta_id:
        reprocessar_saldo_conta(importacao.conta_id)
    importacao.status = Importacao.Status.DESFEITA
    importacao.save(update_fields=["status", "atualizado_em"])
    return importacao


def resumo_importacao(importacao: Importacao) -> dict:
    itens = importacao.itens.all()
    contagens = {status: itens.filter(status=status).count() for status, _ in ItemImportacao.Status.choices}
    contagens[ItemImportacao.STATUS_PLANEJADO] = itens.filter(status=ItemImportacao.STATUS_PLANEJADO).count()
    valores = itens.exclude(acao=ItemImportacao.Acao.IGNORAR).aggregate(total=Sum("valor"))
    return {"contagens": contagens, "total": valores["total"] or ZERO, "quantidade": itens.count()}
