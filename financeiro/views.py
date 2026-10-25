from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Max, Prefetch, ProtectedError, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django.views.generic import CreateView, ListView, UpdateView
from decimal import Decimal
from urllib.parse import urlencode
from uuid import uuid4

from .forms import (
    AplicacaoLoteForm,
    CartaoForm,
    CategoriaForm,
    CompraCartaoForm,
    ContaForm,
    ClonagemPeriodoOrcamentoForm,
    ImportacaoDespesasOrcamentoForm,
    ImportacaoOFXForm,
    ImportacaoPlanilhaForm,
    ItemImportacaoForm,
    LancamentoForm,
    OrcamentoForm,
    FechamentoFaturaForm,
    ProgramacaoPagamentoForm,
    ConciliacaoPagamentoForm,
    PagamentoManualFaturaForm,
    TransporteSaldoFaturaForm,
    ReaberturaFaturaForm,
    PessoaForm,
    RegraCategoriaCondicaoFormSet,
    RegraCategoriaForm,
    SugestaoRegraLancamentoForm,
    TokenAcessoApiForm,
)
from .models import Ambito, Cartao, Categoria, Conta, EventoFinanceiro, Fatura, Importacao, ItemImportacao, Liquidacao, NaturezaEvento, NotificacaoCentral, Orcamento, ParcelaCartao, Pessoa, RegraCategoria, RegraCategoriaCondicao, Situacao, Titulo, TokenAcessoApi, VinculoTituloImportacao
from .ofx import OFXError, OFXParser
from .services.cartoes_faturas import (
    aviso_ciclo_cartao,
    consultar_cartoes_faturas,
    criar_compra_cartao,
    editar_parcela_cartao,
    sincronizar_vencimentos_faturas_abertas_cartao,
)
from .services.dashboard import (
    calcular_limites_cartoes,
    calcular_posicao_contas,
    consultar_dashboard,
    limites_periodo,
)
from .services.importacao_ofx import (
    aplicar_alteracoes_lote,
    aplicar_classificacao_primeira_revisao,
    atualizar_status_revisao,
    buscar_candidatos_vinculo,
    confirmar_importacao,
    criar_itens,
    desfazer_importacao,
    resumo_importacao,
    selecionar_vinculo_item,
    sincronizar_titulos_multiplos,
)
from .services.importacao_planilha import criar_itens_planilha, ler_compras_planilha
from .services.parcelas_futuras import consultar_parcelas_futuras
from .services.orcamentos import (
    CLONAGEM_COMPLETA,
    CLONAGEM_PARCIAL,
    IMPORTACAO_COMPLETA,
    IMPORTACAO_PARCIAL,
    ClonagemPeriodoOrcamentoInvalida,
    ImportacaoDespesasOrcamentoInvalida,
    alertas_orcamento_dashboard,
    clonar_periodo_orcamentos,
    enriquecer_orcamentos_com_realizado,
    importar_despesas_para_orcamento,
    previsualizar_clonagem_periodo,
    previsualizar_importacao_despesas,
    resumir_orcamento_dashboard,
)
from .services.regras_categoria import DadosClassificacao, carregar_regras_categoria, melhor_regra_manual
from .services.saldo_contas import reprocessar_saldo_conta
from .services.lancamentos import (
    consultar_lancamentos,
    criar_lancamento,
    dados_edicao_lancamento,
    editar_lancamento_conta,
    excluir_lancamento_conta,
    excluir_parcela_lancamento,
    lancamento_possui_dados_protegidos,
    replicar_edicao_recorrencia,
)
from .services.detalhamento_dashboard import (
    DetalhamentoDashboardInvalido,
    consultar_detalhamento_dashboard,
)
from .services.fechamento_faturas import (
    fechar_fatura,
    programar_pagamento,
    conciliar_pagamento,
    registrar_pagamento_manual,
    criar_transporte,
    reabrir_fatura,
)
from .services.tokens_api import criar_token_acesso


class UsuarioMixin(LoginRequiredMixin):
    def get_queryset(self):
        return super().get_queryset().filter(usuario=self.request.user)

    def form_valid(self, form):
        form.instance.usuario = self.request.user
        return super().form_valid(form)


def _total_notificacoes_nao_lidas(usuario):
    return NotificacaoCentral.objects.filter(
        usuario=usuario,
        lida_em__isnull=True,
        arquivada_em__isnull=True,
    ).count()


def _notificacao_json(notificacao):
    return {
        "id": notificacao.pk,
        "tipo": notificacao.tipo,
        "titulo": notificacao.titulo,
        "mensagem": notificacao.mensagem,
        "dataReferencia": notificacao.data_referencia.isoformat(),
        "criadaEm": timezone.localtime(notificacao.criado_em).isoformat(),
        "atualizadaEm": timezone.localtime(notificacao.atualizado_em).isoformat(),
        "lida": notificacao.lida_em is not None,
        "arquivada": notificacao.arquivada_em is not None,
        "conteudo": notificacao.conteudo,
        "urls": {
            "ler": reverse("notificacao_marcar_lida", args=[notificacao.pk]),
            "arquivar": reverse("notificacao_arquivar", args=[notificacao.pk]),
            "restaurar": reverse("notificacao_restaurar", args=[notificacao.pk]),
        },
    }


@login_required
@never_cache
@require_GET
def notificacoes_central(request):
    filtro = request.GET.get("filtro", "nao_lidas")
    if filtro not in {"nao_lidas", "todas", "arquivadas"}:
        return JsonResponse({"ok": False, "erro": "Filtro invalido."}, status=400)
    try:
        pagina = max(int(request.GET.get("pagina", "1")), 1)
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "erro": "Pagina invalida."}, status=400)

    notificacoes = NotificacaoCentral.objects.filter(usuario=request.user)
    if filtro == "nao_lidas":
        notificacoes = notificacoes.filter(lida_em__isnull=True, arquivada_em__isnull=True)
    elif filtro == "todas":
        notificacoes = notificacoes.filter(arquivada_em__isnull=True)
    else:
        notificacoes = notificacoes.filter(arquivada_em__isnull=False)

    tamanho_pagina = 20
    inicio = (pagina - 1) * tamanho_pagina
    itens = list(notificacoes[inicio : inicio + tamanho_pagina + 1])
    return JsonResponse(
        {
            "ok": True,
            "filtro": filtro,
            "naoLidas": _total_notificacoes_nao_lidas(request.user),
            "notificacoes": [_notificacao_json(item) for item in itens[:tamanho_pagina]],
            "pagina": pagina,
            "temMais": len(itens) > tamanho_pagina,
        }
    )


@login_required
@require_POST
def notificacao_marcar_lida(request, pk):
    notificacao = get_object_or_404(NotificacaoCentral, pk=pk, usuario=request.user)
    if notificacao.lida_em is None:
        notificacao.lida_em = timezone.now()
        notificacao.save(update_fields=["lida_em", "atualizado_em"])
    return JsonResponse({"ok": True, "naoLidas": _total_notificacoes_nao_lidas(request.user)})


@login_required
@require_POST
def notificacao_arquivar(request, pk):
    notificacao = get_object_or_404(NotificacaoCentral, pk=pk, usuario=request.user)
    agora = timezone.now()
    notificacao.lida_em = notificacao.lida_em or agora
    notificacao.arquivada_em = agora
    notificacao.arquivada_automaticamente = False
    notificacao.save(
        update_fields=["lida_em", "arquivada_em", "arquivada_automaticamente", "atualizado_em"]
    )
    return JsonResponse({"ok": True, "naoLidas": _total_notificacoes_nao_lidas(request.user)})


@login_required
@require_POST
def notificacao_restaurar(request, pk):
    notificacao = get_object_or_404(NotificacaoCentral, pk=pk, usuario=request.user)
    notificacao.arquivada_em = None
    notificacao.arquivada_automaticamente = False
    notificacao.save(update_fields=["arquivada_em", "arquivada_automaticamente", "atualizado_em"])
    return JsonResponse({"ok": True, "naoLidas": _total_notificacoes_nao_lidas(request.user)})


@login_required
@require_POST
def notificacoes_marcar_todas_lidas(request):
    atualizadas = NotificacaoCentral.objects.filter(
        usuario=request.user,
        lida_em__isnull=True,
        arquivada_em__isnull=True,
    ).update(lida_em=timezone.now(), atualizado_em=timezone.now())
    return JsonResponse({"ok": True, "atualizadas": atualizadas, "naoLidas": 0})


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def integracoes(request):
    token_exibido = ""
    form = TokenAcessoApiForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _, token_exibido = criar_token_acesso(request.user, form.cleaned_data["nome"])
        form = TokenAcessoApiForm()
        messages.success(request, "Token criado. Copie o segredo agora; ele não será exibido novamente.")
    tokens = TokenAcessoApi.objects.filter(usuario=request.user)
    return render(
        request,
        "financeiro/integracoes.html",
        {"form": form, "tokens": tokens, "token_exibido": token_exibido},
    )


@login_required
@require_POST
def token_api_revogar(request, pk):
    token = get_object_or_404(TokenAcessoApi, pk=pk, usuario=request.user)
    if token.ativo:
        token.ativo = False
        token.revogado_em = timezone.now()
        token.save(update_fields=["ativo", "revogado_em", "atualizado_em"])
        messages.success(request, f'Token "{token.nome}" revogado.')
    return redirect("integracoes")


@login_required
def dashboard(request):
    contexto = consultar_dashboard(
        request.user,
        periodo=request.GET.get("period"),
        date_from=request.GET.get("date_from"),
        date_to=request.GET.get("date_to"),
        conta_id=request.GET.get("account"),
        cartao_id=request.GET.get("card"),
        origem=request.GET.get("origin", "all"),
        natureza=request.GET.get("nature", "all"),
    )
    orcamento_dashboard = resumir_orcamento_dashboard(
        request.user,
        contexto["data_final"].strftime("%Y-%m"),
        dashboard_context=True,
    )
    contexto["orcamento_dashboard"] = orcamento_dashboard
    contexto["pontos_atencao"] = (
        alertas_orcamento_dashboard(orcamento_dashboard)
        + contexto["pontos_atencao"]
    )[:3]
    return render(request, "financeiro/dashboard.html", contexto)


@login_required
@require_GET
def dashboard_detalhamento(request):
    try:
        contexto = consultar_detalhamento_dashboard(
            request.user,
            tipo=request.GET.get("type"),
            valor=request.GET.get("value", ""),
            periodo=request.GET.get("period"),
            date_from=request.GET.get("date_from"),
            date_to=request.GET.get("date_to"),
            conta_id=request.GET.get("account"),
            cartao_id=request.GET.get("card"),
            origem=request.GET.get("origin", "all"),
            natureza=request.GET.get("nature", "all"),
            computacao=request.GET.get("computes", "COMPUTA"),
            scope=request.GET.get("scope", "all"),
            dashboard_context=request.GET.get("dashboard_context") == "1",
        )
    except DetalhamentoDashboardInvalido as exc:
        raise Http404("Detalhamento não encontrado.") from exc
    return render(request, "financeiro/_dashboard_detalhamento.html", contexto)


def _url_retorno_lancamento(request, periodo: str, destino_padrao="eventos"):
    destino = request.GET.get("return_to", destino_padrao)
    if destino not in {"dashboard", "eventos"}:
        destino = destino_padrao
    parametros = {"period": periodo}
    for origem, nome in (("account", "account"), ("card", "card")):
        valor = request.GET.get(origem)
        if valor and valor.isdigit():
            parametros[nome] = valor
    if destino == "dashboard":
        for nome in ("date_from", "date_to", "origin", "nature"):
            valor = request.GET.get(nome)
            if valor not in (None, ""):
                parametros[nome] = valor
    else:
        for nome in (
            "date_from",
            "date_to",
            "q",
            "origin",
            "category",
            "parent_category",
            "nature",
            "category_nature",
            "essentiality",
            "computes",
            "person",
            "status",
            "analysis_type",
            "analysis_value",
            "analysis_nature",
            "analysis_scope",
            "group_card_invoices",
            "dashboard_context",
        ):
            valor = request.GET.get(nome)
            if valor not in (None, ""):
                parametros[nome] = valor
    return f"{reverse(destino)}?{urlencode(parametros)}"


@login_required
def lancamentos(request):
    try:
        contexto = consultar_lancamentos(
            request.user,
            periodo=request.GET.get("period"),
            conta_id=request.GET.get("account"),
            cartao_id=request.GET.get("card"),
            busca=request.GET.get("q", ""),
            origem=request.GET.get("origin", "all"),
            categoria=request.GET.get("category", "all"),
            situacao=request.GET.get("status", "all"),
            data_inicial=request.GET.get("date_from"),
            data_final=request.GET.get("date_to"),
            categoria_superior=request.GET.get("parent_category", "all"),
            natureza=request.GET.get("nature", "all"),
            natureza_categoria=request.GET.get("category_nature", "all"),
            essencialidade=request.GET.get("essentiality", "all"),
            computa_resultado=request.GET.get("computes", "all"),
            pessoa=request.GET.get("person", "all"),
            analysis_type=request.GET.get("analysis_type", ""),
            analysis_value=request.GET.get("analysis_value", ""),
            analysis_nature=request.GET.get("analysis_nature", "all"),
            analysis_scope=request.GET.get("analysis_scope", "all"),
            group_card_invoices=request.GET.get("group_card_invoices"),
            dashboard_context=request.GET.get("dashboard_context") == "1",
        )
    except DetalhamentoDashboardInvalido as exc:
        raise Http404("Detalhamento não encontrado.") from exc
    return render(request, "financeiro/eventos.html", contexto)


@login_required
def lancamento_novo(request):
    periodo = request.GET.get("period")
    conta_id = request.GET.get("account")
    cartao_id = request.GET.get("card")
    clone_evento = None
    clone_initial = {}
    clone_id = request.GET.get("clone_evento")
    if clone_id and clone_id.isdigit():
        clone_evento = get_object_or_404(
            EventoFinanceiro.objects.prefetch_related("compras_cartao__parcelas", "rateios"),
            pk=int(clone_id),
            usuario=request.user,
        )
        clone_initial = dados_clonagem_lancamento(clone_evento)
        # A clonagem exige que a nova data seja informada manualmente.
        periodo = None
        if clone_initial.get("conta"):
            conta_id = str(clone_initial["conta"].pk)
        if clone_initial.get("cartao"):
            cartao_id = str(clone_initial["cartao"].pk)
    conta_preferida = Conta.objects.filter(
        usuario=request.user,
        ativa=True,
        pk=int(conta_id),
    ).first() if conta_id and conta_id.isdigit() else None
    cartao_preferido = Cartao.objects.filter(
        usuario=request.user,
        ativo=True,
        pk=int(cartao_id),
    ).first() if cartao_id and cartao_id.isdigit() else None
    kwargs = {
        "usuario": request.user,
        "periodo": periodo,
        "conta_preferida": conta_preferida,
        "cartao_preferido": cartao_preferido,
    }
    if request.method == "POST":
        form = LancamentoForm(request.POST, **kwargs)
        if form.is_valid():
            try:
                criar_lancamento(request.user, form.cleaned_data)
            except ValidationError as exc:
                form.add_error(None, exc.messages[0])
            else:
                if form.cleaned_data["origem"] == "CARTAO":
                    periodo_destino = form.cleaned_data["ciclo"]
                elif form.cleaned_data["situacao"] == Situacao.PREVISTO:
                    periodo_destino = form.cleaned_data["vencimento"].strftime("%Y-%m")
                else:
                    periodo_destino = form.cleaned_data["data"].strftime("%Y-%m")
                if form.ciclo_aviso:
                    messages.warning(request, form.ciclo_aviso)
                messages.success(request, "Lançamento salvo e indicadores atualizados.")
                return redirect(_url_retorno_lancamento(request, periodo_destino, "dashboard"))
    else:
        form = LancamentoForm(initial=clone_initial, **kwargs)
    titulo = "Clonar lançamento" if clone_evento else "Novo lançamento"
    return render(request, "financeiro/form.html", {
        "form": form,
        "titulo": titulo,
        "formulario_lancamento": True,
        "ciclo_aviso": form.ciclo_aviso,
        "sugestao_regra_url": reverse("regra_categoria_sugerir_lancamento") if clone_evento is None else "",
        "sugestao_resultados_protegidos": form.is_bound,
    })


def dados_clonagem_lancamento(evento):
    compra = evento.compras_cartao.select_related("cartao").prefetch_related(
        "parcelas__fatura", "parcelas__categoria", "parcelas__pessoa"
    ).first()
    if compra:
        parcela = compra.parcelas.order_by("numero", "id").first()
        rateio = evento.rateios.first()
        return {
            "descricao": parcela.descricao or evento.descricao,
            "natureza": parcela.natureza or evento.natureza,
            "data": None,
            "valor": parcela.valor,
            "origem": "CARTAO",
            "conta": None,
            "cartao": compra.cartao,
            "categoria": parcela.categoria or (rateio.categoria if rateio else None),
            "pessoa": parcela.pessoa or evento.pessoa,
            "pessoa_nome_livre": parcela.pessoa_nome_livre or evento.pessoa_nome_livre,
            "ambito": parcela.ambito or evento.ambito,
            "situacao": parcela.situacao,
            "vencimento": None,
            "ciclo": parcela.fatura.ciclo,
            "quantidade_parcelas": 1,
            "observacao": evento.observacao,
        }
    iniciais = dados_edicao_lancamento(evento.usuario, evento)
    iniciais["data"] = None
    iniciais["vencimento"] = None
    return iniciais


@login_required
def lancamento_clonar(request, pk):
    evento = get_object_or_404(EventoFinanceiro, pk=pk, usuario=request.user)
    parametros = {
        "clone_evento": evento.pk,
        "period": request.GET.get("period", ""),
        "return_to": request.GET.get("return_to", "eventos"),
    }
    for nome in ("account", "card"):
        valor = request.GET.get(nome)
        if valor and valor.isdigit():
            parametros[nome] = valor
    if request.GET.get("group_card_invoices") == "1":
        parametros["group_card_invoices"] = "1"
    return redirect(f"{reverse('evento_novo')}?{urlencode(parametros)}")


@login_required
def lancamento_editar(request, pk):
    evento = get_object_or_404(EventoFinanceiro, pk=pk, usuario=request.user)
    if evento.compras_cartao.exists():
        parcela = evento.compras_cartao.first().parcelas.order_by("numero", "id").first()
        if parcela:
            return redirect("parcela_cartao_editar", pk=parcela.pk)
    iniciais = dados_edicao_lancamento(request.user, evento)
    possui_liquidacao = Liquidacao.objects.filter(
        movimento_conta__evento=evento,
        status=Liquidacao.Status.ATIVA,
    ).exists()
    protegido = lancamento_possui_dados_protegidos(evento) or possui_liquidacao
    kwargs = {
        "usuario": request.user,
        "evento": evento,
        "bloquear_financeiro": protegido,
        "initial": iniciais,
    }
    if request.method == "POST":
        form = LancamentoForm(request.POST, **kwargs)
        if form.is_valid():
            try:
                evento = editar_lancamento_conta(request.user, evento.pk, form.cleaned_data)
                if form.cleaned_data.get("aplicar_recorrencia") == "FUTUROS":
                    replicar_edicao_recorrencia(request.user, evento, form.cleaned_data)
            except ValidationError as exc:
                form.add_error(None, exc.messages[0])
            else:
                periodo_destino = (
                    form.cleaned_data["vencimento"].strftime("%Y-%m")
                    if form.cleaned_data["situacao"] == Situacao.PREVISTO
                    else form.cleaned_data["data"].strftime("%Y-%m")
                )
                if form.ciclo_aviso:
                    messages.warning(request, form.ciclo_aviso)
                messages.success(request, "Lançamento atualizado; vínculos financeiros preservados.")
                return redirect(_url_retorno_lancamento(request, periodo_destino))
    else:
        form = LancamentoForm(**kwargs)
    return render(request, "financeiro/form.html", {
        "form": form,
        "titulo": "Editar lançamento",
        "formulario_lancamento": True,
        "edicao_lancamento": True,
        "edicao_protegida": protegido,
        "ciclo_aviso": form.ciclo_aviso,
    })


@login_required
def cartoes_faturas(request):
    contexto = consultar_cartoes_faturas(
        request.user,
        ciclo=request.GET.get("cycle"),
        conta_id=request.GET.get("account"),
        cartao_id=request.GET.get("card"),
        fatura_id=request.GET.get("invoice"),
    )
    return render(request, "financeiro/cartoes_faturas.html", contexto)


def _retorno_fatura(fatura):
    return redirect(f"{reverse('cartoes_faturas')}?cycle={fatura.ciclo}&card={fatura.cartao_id}&invoice={fatura.pk}")


def _formulario_fatura(request, form, titulo):
    return render(request, "financeiro/form.html", {"form": form, "titulo": titulo})


@login_required
def fatura_fechar(request, pk):
    fatura = get_object_or_404(Fatura.objects.select_related("cartao__conta_pagamento_padrao"), pk=pk, usuario=request.user)
    form = FechamentoFaturaForm(request.POST or None, usuario=request.user, fatura=fatura)
    if request.method == "POST" and form.is_valid():
        try:
            fechar_fatura(request.user, fatura.pk, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc.messages[0])
        else:
            messages.success(request, "Fatura fechada; o título a pagar foi atualizado.")
            return _retorno_fatura(fatura)
    return _formulario_fatura(request, form, "Conferir e fechar fatura")


@login_required
def fatura_programar_pagamento(request, pk):
    titulo = get_object_or_404(Titulo.objects.select_related("fatura"), pk=pk, usuario=request.user, fatura__isnull=False)
    form = ProgramacaoPagamentoForm(request.POST or None, usuario=request.user, titulo=titulo)
    if request.method == "POST" and form.is_valid():
        try:
            programar_pagamento(
                request.user, titulo.pk, form.cleaned_data["conta"], form.cleaned_data["data_programada"],
                form.cleaned_data["valor"], chave=request.POST.get("idempotency_key") or str(uuid4()),
                observacao=form.cleaned_data["observacao"],
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages[0])
        else:
            messages.success(request, "Pagamento programado sem movimentar a conta.")
            return _retorno_fatura(titulo.fatura)
    return _formulario_fatura(request, form, "Programar pagamento")


@login_required
def fatura_conciliar_pagamento(request, pk):
    titulo = get_object_or_404(Titulo.objects.select_related("fatura"), pk=pk, usuario=request.user, fatura__isnull=False)
    form = ConciliacaoPagamentoForm(request.POST or None, usuario=request.user, titulo=titulo)
    if request.method == "POST" and form.is_valid():
        try:
            conciliar_pagamento(request.user, titulo.pk, form.cleaned_data["movimento"].pk, form.cleaned_data.get("valor"))
        except ValidationError as exc:
            form.add_error(None, exc.messages[0])
        else:
            messages.success(request, "Débito existente conciliado com a fatura.")
            return _retorno_fatura(titulo.fatura)
    return _formulario_fatura(request, form, "Conciliar pagamento")


@login_required
def fatura_pagamento_manual(request, pk):
    titulo = get_object_or_404(Titulo.objects.select_related("fatura"), pk=pk, usuario=request.user, fatura__isnull=False)
    form = PagamentoManualFaturaForm(request.POST or None, usuario=request.user, titulo=titulo)
    if request.method == "POST" and form.is_valid():
        try:
            registrar_pagamento_manual(
                request.user, titulo.pk, form.cleaned_data["conta"], form.cleaned_data["data_pagamento"],
                form.cleaned_data["valor"], request.POST.get("idempotency_key") or str(uuid4()), form.cleaned_data["observacao"],
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages[0])
        else:
            messages.success(request, "Pagamento manual registrado e liquidado.")
            return _retorno_fatura(titulo.fatura)
    return _formulario_fatura(request, form, "Registrar pagamento manual")


@login_required
def fatura_transportar_saldo(request, pk):
    titulo = get_object_or_404(Titulo.objects.select_related("fatura__cartao"), pk=pk, usuario=request.user, fatura__isnull=False)
    form = TransporteSaldoFaturaForm(request.POST or None, usuario=request.user, titulo=titulo)
    if request.method == "POST" and form.is_valid():
        try:
            criar_transporte(
                request.user, titulo.pk, form.cleaned_data["fatura_destino"].pk, form.cleaned_data["valor"],
                form.cleaned_data["data_identificacao"], chave=request.POST.get("idempotency_key") or str(uuid4()),
                observacao=form.cleaned_data["observacao"],
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages[0])
        else:
            messages.success(request, "Saldo transportado e registrado no histórico das duas faturas.")
            return _retorno_fatura(titulo.fatura)
    return _formulario_fatura(request, form, "Transportar saldo")


@login_required
def fatura_reabrir(request, pk):
    fatura = get_object_or_404(Fatura, pk=pk, usuario=request.user)
    form = ReaberturaFaturaForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            reabrir_fatura(request.user, fatura.pk, form.cleaned_data["motivo"])
        except ValidationError as exc:
            form.add_error(None, exc.messages[0])
        else:
            messages.success(request, "Fatura reaberta ou encaminhada para correção.")
            return _retorno_fatura(fatura)
    return _formulario_fatura(request, form, "Reabrir ou corrigir fatura")


@login_required
def parcelas_futuras(request):
    contexto = consultar_parcelas_futuras(
        request.user,
        ciclo=request.GET.get("cycle"),
        conta_id=request.GET.get("account"),
        cartao_id=request.GET.get("card"),
    )
    return render(request, "financeiro/parcelas_futuras.html", contexto)


@login_required
def compra_cartao_nova(request):
    ciclo = request.GET.get("cycle")
    cartao_preferido = Cartao.objects.filter(
        pk=request.GET.get("card"),
        usuario=request.user,
        ativo=True,
    ).first()
    if request.method == "POST":
        form = CompraCartaoForm(request.POST, usuario=request.user)
        if form.is_valid():
            try:
                if form.cleaned_data.get("modo_lancamento") == "RECORRENTE":
                    dados = dict(form.cleaned_data)
                    dados.update({
                        "origem": "CARTAO",
                        "data": dados.pop("data_original"),
                        "vencimento": None,
                    })
                    evento = criar_lancamento(request.user, dados)
                    compra = evento.compras_cartao.get()
                else:
                    compra = criar_compra_cartao(request.user, form.cleaned_data)
            except (ValidationError, Cartao.DoesNotExist) as exc:
                mensagem = exc.messages[0] if isinstance(exc, ValidationError) else "O cartão selecionado não está disponível."
                form.add_error(None, mensagem)
            else:
                if form.ciclo_aviso:
                    messages.warning(request, form.ciclo_aviso)
                messages.success(request, "Compra registrada e parcelas projetadas com sucesso.")
                return redirect(
                    f"{reverse('cartoes_faturas')}?cycle={form.cleaned_data['ciclo']}&card={compra.cartao_id}"
                )
    else:
        form = CompraCartaoForm(
            usuario=request.user,
            cartao_preferido=cartao_preferido,
            ciclo=ciclo,
        )
    return render(request, "financeiro/form.html", {
        "form": form,
        "titulo": "Nova compra",
        "formulario_lancamento": True,
        "ciclo_aviso": form.ciclo_aviso,
        "sugestao_regra_url": reverse("regra_categoria_sugerir_lancamento"),
        "sugestao_resultados_protegidos": form.is_bound,
    })


@login_required
def parcela_cartao_editar(request, pk):
    parcela = get_object_or_404(
        ParcelaCartao.objects.select_related(
            "compra__evento",
            "fatura__cartao",
            "categoria",
            "pessoa",
        ),
        pk=pk,
        compra__usuario=request.user,
    )
    if request.method == "POST":
        form = CompraCartaoForm(request.POST, usuario=request.user, parcela=parcela)
        if form.is_valid():
            try:
                parcela = editar_parcela_cartao(request.user, parcela.pk, form.cleaned_data)
                if form.cleaned_data.get("aplicar_recorrencia") == "FUTUROS":
                    replicar_edicao_recorrencia(request.user, parcela.compra.evento, form.cleaned_data)
            except ValidationError as exc:
                form.add_error(None, exc.messages[0])
            else:
                if form.ciclo_aviso:
                    messages.warning(request, form.ciclo_aviso)
                messages.success(request, "Item da fatura atualizado; as demais parcelas foram preservadas.")
                if request.GET.get("return_to") == "eventos":
                    return redirect(_url_retorno_lancamento(request, parcela.fatura.ciclo))
                return redirect(
                    f"{reverse('cartoes_faturas')}?cycle={parcela.fatura.ciclo}&card={parcela.fatura.cartao_id}"
                )
    else:
        form = CompraCartaoForm(usuario=request.user, parcela=parcela)
    return render(request, "financeiro/form.html", {
        "form": form,
        "titulo": "Editar lançamento",
        "formulario_lancamento": True,
        "edicao_lancamento": True,
        "ciclo_aviso": form.ciclo_aviso,
    })


class ContaListView(UsuarioMixin, ListView):
    model = Conta
    template_name = "financeiro/lista.html"
    context_object_name = "objetos"

    def get_queryset(self):
        queryset = super().get_queryset()
        return filtrar_cadastro(queryset, self.request)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        contas = list(context["objetos"])
        hoje = timezone.localdate()
        _, fim_mes_exclusivo = limites_periodo(hoje.strftime("%Y-%m"))
        for indicador in calcular_posicao_contas(self.request.user, contas, hoje, fim_mes_exclusivo):
            indicador["conta"].indicador_saldo = indicador
        context["objetos"] = contas
        return cadastro_context(context, self.request, "Contas financeiras", "conta_nova", "conta")


class ContaCreateView(UsuarioMixin, CreateView):
    model = Conta
    form_class = ContaForm
    template_name = "financeiro/form.html"
    success_url = reverse_lazy("contas")
    extra_context = {"titulo": "Nova conta financeira"}


class ContaUpdateView(UsuarioMixin, UpdateView):
    model = Conta
    form_class = ContaForm
    template_name = "financeiro/form.html"
    success_url = reverse_lazy("contas")
    extra_context = {"titulo": "Editar conta financeira"}

    def form_valid(self, form):
        conta_anterior = self.get_object()
        saldo_anterior = (conta_anterior.saldo_inicial, conta_anterior.data_saldo_inicial)
        with transaction.atomic():
            resposta = super().form_valid(form)
            saldo_atual = (self.object.saldo_inicial, self.object.data_saldo_inicial)
            if saldo_anterior != saldo_atual:
                reprocessar_saldo_conta(self.object.pk)
        return resposta


class CartaoListView(UsuarioMixin, ListView):
    model = Cartao
    template_name = "financeiro/lista.html"
    context_object_name = "objetos"

    def get_queryset(self):
        queryset = super().get_queryset().select_related("conta_pagamento_padrao")
        return filtrar_cadastro(queryset, self.request, active_field="ativo")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cartoes = list(context["objetos"])
        ciclo_atual = timezone.localdate().strftime("%Y-%m")
        for indicador in calcular_limites_cartoes(self.request.user, cartoes, ciclo_atual):
            indicador["cartao"].indicador_limite = indicador
        context["objetos"] = cartoes
        return cadastro_context(context, self.request, "Cadastro de cartões", "cartao_novo", "cartao")


class CartaoCreateView(UsuarioMixin, CreateView):
    model = Cartao
    form_class = CartaoForm
    template_name = "financeiro/form.html"
    success_url = reverse_lazy("cartoes")
    extra_context = {"titulo": "Novo cartao"}

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["usuario"] = self.request.user
        return kwargs


class CartaoUpdateView(UsuarioMixin, UpdateView):
    model = Cartao
    form_class = CartaoForm
    template_name = "financeiro/form.html"
    success_url = reverse_lazy("cartoes")
    extra_context = {"titulo": "Editar cartao"}

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["usuario"] = self.request.user
        return kwargs

    def form_valid(self, form):
        dia_anterior = self.get_object().dia_vencimento
        with transaction.atomic():
            resposta = super().form_valid(form)
            if dia_anterior != self.object.dia_vencimento:
                sincronizar_vencimentos_faturas_abertas_cartao(self.object)
        return resposta


class CategoriaListView(UsuarioMixin, ListView):
    model = Categoria
    template_name = "financeiro/lista.html"
    context_object_name = "objetos"
    def get_queryset(self):
        queryset = super().get_queryset().select_related("categoria_pai")
        return filtrar_cadastro(queryset, self.request)

    def get_context_data(self, **kwargs):
        return cadastro_context(super().get_context_data(**kwargs), self.request, "Categorias", "categoria_nova", "categoria")


class CategoriaCreateView(UsuarioMixin, CreateView):
    model = Categoria
    form_class = CategoriaForm
    template_name = "financeiro/form.html"
    success_url = reverse_lazy("categorias")
    extra_context = {"titulo": "Nova categoria"}

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["usuario"] = self.request.user
        return kwargs


class CategoriaUpdateView(UsuarioMixin, UpdateView):
    model = Categoria
    form_class = CategoriaForm
    template_name = "financeiro/form.html"
    success_url = reverse_lazy("categorias")
    extra_context = {"titulo": "Editar categoria"}

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["usuario"] = self.request.user
        return kwargs


def _mes_orcamento(valor):
    if not valor:
        return ""
    valor = valor.strip()
    if len(valor) == 7 and valor[2] == "/" and valor[:2].isdigit() and valor[3:].isdigit():
        mes, ano = valor.split("/")
        if mes in {f"{numero:02d}" for numero in range(1, 13)}:
            return f"{ano}-{mes}"
    return valor if len(valor) == 7 and valor[4] == "-" else ""


def _proximo_mes_orcamento(valor):
    ano, mes = map(int, valor.split("-"))
    return f"{ano + (mes == 12):04d}-{1 if mes == 12 else mes + 1:02d}"


def _mes_anterior_orcamento(valor):
    ano, mes = map(int, valor.split("-"))
    return f"{ano - (mes == 1):04d}-{12 if mes == 1 else mes - 1:02d}"


def _rotulo_mes_orcamento(valor):
    return f"{valor[5:7]}/{valor[:4]}"


def _url_retorno_orcamentos(request):
    filtros = {
        nome: request.GET.get(nome, "")
        for nome in ("categoria", "mes", "ambito")
        if nome in request.GET
    }
    query = urlencode(filtros)
    return f"{reverse('orcamentos')}?{query}" if query else reverse("orcamentos")


def _url_periodo_orcamentos(mes):
    query = urlencode({"categoria": "", "mes": _rotulo_mes_orcamento(mes), "ambito": ""})
    return f"{reverse('orcamentos')}?{query}"


@login_required
def clonar_periodo_orcamentos_view(request):
    mes_origem = _mes_orcamento(request.GET.get("mes", ""))
    if not mes_origem:
        hoje = timezone.localdate()
        mes_origem = f"{hoje.year:04d}-{hoje.month:02d}"

    resumo = None
    if request.method == "POST":
        form = ClonagemPeriodoOrcamentoForm(request.POST)
        if form.is_valid():
            origem = form.cleaned_data["mes_origem"]
            destino = form.cleaned_data["mes_destino"]
            acao = request.POST.get("acao", "")
            if acao == "alterar":
                form = ClonagemPeriodoOrcamentoForm(
                    initial={"mes_origem": origem, "mes_destino": destino}
                )
            elif acao == "revisar":
                resumo = previsualizar_clonagem_periodo(request.user, origem, destino)
            elif acao in {CLONAGEM_COMPLETA, CLONAGEM_PARCIAL}:
                try:
                    resultado = clonar_periodo_orcamentos(request.user, origem, destino, acao)
                except ClonagemPeriodoOrcamentoInvalida as exc:
                    form.add_error(None, str(exc))
                    resumo = previsualizar_clonagem_periodo(request.user, origem, destino)
                except IntegrityError:
                    form.add_error(
                        None,
                        "Os orçamentos foram alterados durante a confirmação. Revise os períodos e tente novamente.",
                    )
                    resumo = previsualizar_clonagem_periodo(request.user, origem, destino)
                else:
                    if resultado["modo"] == CLONAGEM_COMPLETA:
                        messages.success(
                            request,
                            "Período clonado: "
                            f"{resultado['copiados']} orçamento(s) copiado(s) e "
                            f"{resultado['substituidos']} registro(s) do destino substituído(s).",
                        )
                    else:
                        messages.success(
                            request,
                            "Clonagem parcial concluída: "
                            f"{resultado['copiados']} orçamento(s) incluído(s), "
                            f"{resultado['preservados']} registro(s) do destino preservado(s) e "
                            f"{resultado['ignorados']} combinação(ões) já existente(s) ignorada(s).",
                        )
                    return redirect(_url_periodo_orcamentos(destino))
            else:
                form.add_error(None, "Escolha uma ação válida para continuar.")
    else:
        form = ClonagemPeriodoOrcamentoForm(
            initial={
                "mes_origem": mes_origem,
                "mes_destino": _proximo_mes_orcamento(mes_origem),
            }
        )

    if resumo is not None:
        resumo["mes_origem_rotulo"] = _rotulo_mes_orcamento(resumo["mes_origem"])
        resumo["mes_destino_rotulo"] = _rotulo_mes_orcamento(resumo["mes_destino"])
    return render(
        request,
        "financeiro/orcamento_clonar_periodo.html",
        {
            "form": form,
            "titulo": "Clonar período de orçamento",
            "resumo_clonagem": resumo,
        },
    )


@login_required
def importar_despesas_orcamento_view(request):
    hoje = timezone.localdate()
    mes_atual = f"{hoje.year:04d}-{hoje.month:02d}"
    mes_destino = _mes_orcamento(request.GET.get("mes", "")) or mes_atual
    mes_despesas = min(_mes_anterior_orcamento(mes_destino), mes_atual)

    resumo = None
    if request.method == "POST":
        form = ImportacaoDespesasOrcamentoForm(request.POST, mes_atual=mes_atual)
        if form.is_valid():
            despesas = form.cleaned_data["mes_despesas"]
            destino = form.cleaned_data["mes_destino"]
            acao = request.POST.get("acao", "")
            if acao == "alterar":
                form = ImportacaoDespesasOrcamentoForm(
                    initial={"mes_despesas": despesas, "mes_destino": destino},
                    mes_atual=mes_atual,
                )
            elif acao == "revisar":
                resumo = previsualizar_importacao_despesas(request.user, despesas, destino)
            elif acao in {IMPORTACAO_COMPLETA, IMPORTACAO_PARCIAL}:
                try:
                    resultado = importar_despesas_para_orcamento(
                        request.user,
                        despesas,
                        destino,
                        acao,
                    )
                except ImportacaoDespesasOrcamentoInvalida as exc:
                    form.add_error(None, str(exc))
                    resumo = previsualizar_importacao_despesas(request.user, despesas, destino)
                except IntegrityError:
                    form.add_error(
                        None,
                        "Os orçamentos foram alterados durante a confirmação. "
                        "Revise os períodos e tente novamente.",
                    )
                    resumo = previsualizar_importacao_despesas(request.user, despesas, destino)
                else:
                    messages.success(
                        request,
                        "Importação concluída: "
                        f"{resultado['incluidos']} orçamento(s) incluído(s), "
                        f"{resultado['preservados']} registro(s) preservado(s), "
                        f"{resultado['substituidos']} registro(s) substituído(s) e "
                        f"{resultado['ignorados']} item(ns) ignorado(s).",
                    )
                    return redirect(_url_periodo_orcamentos(destino))
            else:
                form.add_error(None, "Escolha uma ação válida para continuar.")
    else:
        form = ImportacaoDespesasOrcamentoForm(
            initial={"mes_despesas": mes_despesas, "mes_destino": mes_destino},
            mes_atual=mes_atual,
        )

    if resumo is not None:
        resumo["mes_despesas_rotulo"] = _rotulo_mes_orcamento(resumo["mes_despesas"])
        resumo["mes_destino_rotulo"] = _rotulo_mes_orcamento(resumo["mes_destino"])
    return render(
        request,
        "financeiro/orcamento_importar_despesas.html",
        {
            "form": form,
            "titulo": "Importar valores para o orçamento",
            "resumo_importacao": resumo,
        },
    )


class OrcamentoListView(UsuarioMixin, ListView):
    model = Orcamento
    template_name = "financeiro/orcamentos.html"
    context_object_name = "orcamentos"

    def get_queryset(self):
        queryset = super().get_queryset().select_related("categoria", "categoria__categoria_pai")
        categoria = self.request.GET.get("categoria", "")
        ambito = self.request.GET.get("ambito", "")
        mes = self.request.GET.get("mes")
        if mes is None:
            hoje = timezone.localdate()
            mes = f"{hoje.year:04d}-{hoje.month:02d}"
        else:
            mes = _mes_orcamento(mes)
        if categoria.isdigit():
            queryset = queryset.filter(categoria_id=int(categoria))
        if mes:
            queryset = queryset.filter(mes=mes)
        if ambito in Ambito.values:
            queryset = queryset.filter(ambito=ambito)
        return queryset.order_by("-mes", "categoria__nome", "ambito", "id")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["orcamentos"] = enriquecer_orcamentos_com_realizado(
            self.request.user,
            list(context["orcamentos"]),
        )
        mes_enviado = self.request.GET.get("mes")
        if mes_enviado is None:
            hoje = timezone.localdate()
            mes_filtro = f"{hoje.month:02d}/{hoje.year:04d}"
        else:
            normalizado = _mes_orcamento(mes_enviado)
            mes_filtro = f"{normalizado[5:7]}/{normalizado[:4]}" if normalizado else ""
        context.update(
            {
                "categorias": Categoria.objects.filter(
                    usuario=self.request.user,
                    tipo=Categoria.Tipo.DESPESA,
                    categoria_pai__isnull=True,
                    orcamento__isnull=False,
                ).select_related("categoria_pai").distinct().order_by("nome"),
                "ambitos": Ambito.choices,
                "categoria_filtro": self.request.GET.get("categoria", ""),
                "mes_filtro": mes_filtro,
                "ambito_filtro": self.request.GET.get("ambito", ""),
                "filtros_query": urlencode(
                    {
                        "categoria": self.request.GET.get("categoria", ""),
                        "mes": mes_filtro,
                        "ambito": self.request.GET.get("ambito", ""),
                    }
                ),
            }
        )
        return context


class OrcamentoCreateView(UsuarioMixin, CreateView):
    model = Orcamento
    form_class = OrcamentoForm
    template_name = "financeiro/form.html"
    extra_context = {"titulo": "Novo orçamento"}

    def get_initial(self):
        initial = super().get_initial()
        mes = _mes_orcamento(self.request.GET.get("mes", ""))
        if mes:
            initial["mes"] = mes
        return initial

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["usuario"] = self.request.user
        return kwargs

    def get_success_url(self):
        return _url_retorno_orcamentos(self.request)


class OrcamentoCloneView(OrcamentoCreateView):
    extra_context = {"titulo": "Clonar orçamento"}

    def get_orcamento_origem(self):
        return get_object_or_404(Orcamento, pk=self.kwargs["pk"], usuario=self.request.user)

    def get_initial(self):
        origem = self.get_orcamento_origem()
        return {
            "categoria": origem.categoria,
            "mes": _proximo_mes_orcamento(origem.mes),
            "ambito": origem.ambito,
            "limite": origem.limite,
        }


class OrcamentoUpdateView(UsuarioMixin, UpdateView):
    model = Orcamento
    form_class = OrcamentoForm
    template_name = "financeiro/form.html"
    extra_context = {"titulo": "Editar orçamento"}

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["usuario"] = self.request.user
        return kwargs

    def get_success_url(self):
        return _url_retorno_orcamentos(self.request)


@login_required
@require_POST
def excluir_orcamento(request, pk):
    orcamento = get_object_or_404(Orcamento, pk=pk, usuario=request.user)
    orcamento.delete()
    messages.success(request, "Orçamento excluído.")
    return redirect(_url_retorno_orcamentos(request))


class PessoaListView(UsuarioMixin, ListView):
    model = Pessoa
    template_name = "financeiro/lista.html"
    context_object_name = "objetos"
    def get_queryset(self):
        queryset = super().get_queryset()
        return filtrar_cadastro(queryset, self.request)

    def get_context_data(self, **kwargs):
        return cadastro_context(super().get_context_data(**kwargs), self.request, "Pessoas", "pessoa_nova", "pessoa")


class PessoaCreateView(UsuarioMixin, CreateView):
    model = Pessoa
    form_class = PessoaForm
    template_name = "financeiro/form.html"
    success_url = reverse_lazy("pessoas")
    extra_context = {"titulo": "Nova pessoa"}


class PessoaUpdateView(UsuarioMixin, UpdateView):
    model = Pessoa
    form_class = PessoaForm
    template_name = "financeiro/form.html"
    success_url = reverse_lazy("pessoas")
    extra_context = {"titulo": "Editar pessoa"}


def _condicoes_ordenadas_formset(formset):
    condicoes = [
        form for form in formset.forms
        if form.cleaned_data and not form.cleaned_data.get("DELETE") and form.cleaned_data.get("campo")
    ]
    return sorted(
        condicoes,
        key=lambda form: (form.cleaned_data.get("ordem") or 9999, form.prefix),
    )


def _regra_permanece_legada(regra, condicoes):
    if regra.versao_motor != RegraCategoria.VersaoMotor.LEGADO or len(condicoes) != 1:
        return False
    condicao = condicoes[0].cleaned_data
    return (
        regra.combinador == RegraCategoria.Combinador.TODAS
        and regra.categoria_id is not None
        and not regra.natureza_sugerida
        and condicao.get("campo") == RegraCategoriaCondicao.Campo.DESCRICAO
        and condicao.get("operador") == RegraCategoriaCondicao.Operador.CONTEM
        and len(condicao.get("valor") or "") <= 160
    )


def _salvar_condicoes_regra(regra, formset, condicoes):
    # Libera temporariamente as posições atuais para permitir trocar a ordem
    # sem violar a constraint única durante os saves intermediários.
    regra.condicoes.update(ordem=F("ordem") + 10000)
    formset.save(commit=False)
    for condicao in formset.deleted_objects:
        condicao.delete()
    for ordem, form_condicao in enumerate(condicoes, start=1):
        condicao = form_condicao.save(commit=False)
        condicao.regra = regra
        condicao.ordem = ordem
        condicao.save()


def _formulario_regra_categoria(request, regra=None):
    criando = regra is None
    if criando:
        ultima_prioridade = (
            RegraCategoria.objects.filter(usuario=request.user).aggregate(maior=Max("prioridade"))["maior"]
            or 0
        )
        regra = RegraCategoria(usuario=request.user, prioridade=ultima_prioridade + 1)
    versao_anterior = regra.versao_motor
    form = RegraCategoriaForm(request.POST or None, instance=regra, usuario=request.user)
    formset = RegraCategoriaCondicaoFormSet(
        request.POST or None,
        instance=regra,
        prefix="condicoes",
        form_kwargs={"usuario": request.user},
    )
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        condicoes = _condicoes_ordenadas_formset(formset)
        with transaction.atomic():
            regra = form.save(commit=False)
            regra.usuario = request.user
            if criando:
                regra.versao_motor = RegraCategoria.VersaoMotor.NOVO
                regra.texto = ""
            elif _regra_permanece_legada(regra, condicoes):
                regra.versao_motor = RegraCategoria.VersaoMotor.LEGADO
                regra.texto = condicoes[0].cleaned_data["valor"]
            else:
                regra.versao_motor = RegraCategoria.VersaoMotor.NOVO
            regra.save()
            _salvar_condicoes_regra(regra, formset, condicoes)
        messages.success(request, "Regra salva com sucesso.")
        if versao_anterior == RegraCategoria.VersaoMotor.LEGADO and regra.versao_motor == RegraCategoria.VersaoMotor.NOVO:
            messages.warning(
                request,
                "A regra foi promovida para o novo formato e poderá sugerir dados nas próximas importações e nos novos lançamentos manuais.",
            )
        return redirect("regras_categoria")
    return render(
        request,
        "financeiro/regra_categoria_form.html",
        {
            "form": form,
            "formset": formset,
            "regra": regra,
            "titulo": "Nova regra" if criando else "Editar regra",
        },
    )


@login_required
def regras_categoria(request):
    termo = request.GET.get("q", "").strip()
    status = request.GET.get("status", "all")
    queryset = (
        RegraCategoria.objects.filter(usuario=request.user)
        .select_related("categoria__categoria_pai", "pessoa")
        .prefetch_related("condicoes")
    )
    if termo:
        queryset = queryset.filter(Q(nome__icontains=termo) | Q(condicoes__valor__icontains=termo)).distinct()
    if status == "active":
        queryset = queryset.filter(ativa=True)
    elif status == "inactive":
        queryset = queryset.filter(ativa=False)
    regras = list(queryset.order_by("prioridade", "id"))
    contas = {str(item.pk): item.nome for item in Conta.objects.filter(usuario=request.user)}
    cartoes = {str(item.pk): item.nome for item in Cartao.objects.filter(usuario=request.user)}
    naturezas = dict(RegraCategoria.NATUREZAS_SUGERIDAS)
    origens = {"CONTA": "Conta financeira", "CARTAO": "Cartão de crédito"}
    for regra in regras:
        regra.condicoes_resumo = []
        for condicao in regra.condicoes.all():
            valor = condicao.valor
            if condicao.campo == RegraCategoriaCondicao.Campo.CONTA:
                valor = contas.get(valor, f"Conta #{valor}")
            elif condicao.campo == RegraCategoriaCondicao.Campo.CARTAO:
                valor = cartoes.get(valor, f"Cartão #{valor}")
            elif condicao.campo == RegraCategoriaCondicao.Campo.NATUREZA:
                valor = naturezas.get(valor, valor)
            elif condicao.campo == RegraCategoriaCondicao.Campo.ORIGEM:
                valor = origens.get(valor, valor)
            regra.condicoes_resumo.append(
                f"{condicao.get_campo_display()} {condicao.get_operador_display()} {valor}"
            )
        resultados = []
        if regra.categoria_id:
            resultados.append(f"Categoria: {regra.categoria.rotulo_com_pai}")
        if regra.ambito and regra.ambito != "NAO_DEFINIDO":
            resultados.append(f"Âmbito: {regra.get_ambito_display()}")
        if regra.pessoa_id:
            resultados.append(f"Pessoa: {regra.pessoa.nome}")
        if regra.natureza_sugerida:
            resultados.append(f"Tipo: {regra.get_natureza_sugerida_display()}")
        regra.resultados_resumo = resultados
    return render(
        request,
        "financeiro/regras_categoria.html",
        {
            "regras": regras,
            "busca": termo,
            "status_filtro": status,
        },
    )


@login_required
def regra_categoria_nova(request):
    return _formulario_regra_categoria(request)


@login_required
def regra_categoria_editar(request, pk):
    regra = get_object_or_404(RegraCategoria, pk=pk, usuario=request.user)
    return _formulario_regra_categoria(request, regra)


@login_required
@require_POST
def regra_categoria_duplicar(request, pk):
    original = get_object_or_404(
        RegraCategoria.objects.prefetch_related("condicoes"),
        pk=pk,
        usuario=request.user,
    )
    with transaction.atomic():
        copia = RegraCategoria.objects.create(
            usuario=request.user,
            texto="",
            nome=f"Cópia de {original.nome}"[:160],
            prioridade=original.prioridade,
            combinador=original.combinador,
            versao_motor=RegraCategoria.VersaoMotor.NOVO,
            pessoa=original.pessoa,
            categoria=original.categoria,
            ambito=original.ambito,
            natureza_sugerida=original.natureza_sugerida,
            ativa=original.ativa,
        )
        RegraCategoriaCondicao.objects.bulk_create(
            [
                RegraCategoriaCondicao(
                    regra=copia,
                    ordem=condicao.ordem,
                    campo=condicao.campo,
                    operador=condicao.operador,
                    valor=condicao.valor,
                )
                for condicao in original.condicoes.all()
            ]
        )
    messages.success(
        request,
        "Regra duplicada no novo formato; se ativa, ela poderá sugerir dados nas próximas importações e nos novos lançamentos manuais.",
    )
    return redirect("regras_categoria")


@login_required
@require_POST
def regra_categoria_alternar(request, pk):
    regra = get_object_or_404(RegraCategoria, pk=pk, usuario=request.user)
    regra.ativa = not regra.ativa
    regra.save(update_fields=["ativa", "atualizado_em"])
    messages.success(request, "Regra ativada." if regra.ativa else "Regra inativada; seu cadastro foi preservado.")
    return redirect("regras_categoria")


@login_required
@require_POST
def regra_categoria_sugerir_lancamento(request):
    form = SugestaoRegraLancamentoForm(request.POST, usuario=request.user)
    if not form.is_valid():
        erros = {
            campo: [erro["message"] for erro in lista]
            for campo, lista in form.errors.get_json_data().items()
        }
        return JsonResponse({"encontrada": False, "erros": erros}, status=400)

    dados = DadosClassificacao(
        descricao=form.cleaned_data.get("descricao") or "",
        observacao=form.cleaned_data.get("observacao") or "",
        valor=form.cleaned_data.get("valor"),
        natureza=form.cleaned_data.get("natureza") or "",
        origem=form.cleaned_data.get("origem") or "",
        conta_id=getattr(form.cleaned_data.get("conta"), "pk", None),
        cartao_id=getattr(form.cleaned_data.get("cartao"), "pk", None),
    )
    resultado = melhor_regra_manual(dados, carregar_regras_categoria(request.user))
    regra = resultado.regra
    sugestoes_vazias = {
        "categoria": None,
        "pessoa": None,
        "ambito": None,
        "natureza": None,
    }
    if not regra:
        return JsonResponse({"encontrada": False, "regra": None, "sugestoes": sugestoes_vazias})

    categoria = regra.categoria
    pessoa = regra.pessoa
    categoria_disponivel = categoria and categoria.usuario_id == request.user.pk and categoria.ativa
    pessoa_disponivel = pessoa and pessoa.usuario_id == request.user.pk and pessoa.ativa
    natureza = None
    if regra.versao_motor == RegraCategoria.VersaoMotor.NOVO and regra.natureza_sugerida:
        naturezas_permitidas = {NaturezaEvento.DESPESA, NaturezaEvento.ESTORNO}
        if dados.origem != Importacao.Origem.CARTAO:
            naturezas_permitidas.add(NaturezaEvento.RECEITA)
        if regra.natureza_sugerida in naturezas_permitidas:
            natureza = regra.natureza_sugerida

    sugestoes = {
        "categoria": (
            {"id": categoria.pk, "rotulo": categoria.rotulo_com_pai}
            if categoria_disponivel
            else None
        ),
        "pessoa": (
            {"id": pessoa.pk, "rotulo": pessoa.nome}
            if pessoa_disponivel
            else None
        ),
        "ambito": regra.ambito if regra.ambito in {Ambito.PESSOAL, Ambito.PROFISSIONAL} else None,
        "natureza": natureza,
    }
    return JsonResponse(
        {
            "encontrada": True,
            "regra": {
                "id": regra.pk,
                "nome": regra.nome,
                "versao": regra.versao_motor,
            },
            "sugestoes": sugestoes,
        }
    )


@login_required
def importar_ofx(request):
    if request.method == "POST":
        form = ImportacaoOFXForm(request.POST, request.FILES, usuario=request.user)
        if form.is_valid():
            arquivo = form.cleaned_data["arquivo_ofx"]
            conteudo = arquivo.read()
            arquivo.seek(0)
            try:
                documento = OFXParser.parse(conteudo)
            except OFXError as exc:
                form.add_error("arquivo_ofx", str(exc))
            else:
                if documento.origem != form.cleaned_data["origem"]:
                    detectada = "cartão" if documento.origem == Importacao.Origem.CARTAO else "conta corrente"
                    form.add_error("origem", f"O conteúdo foi identificado como OFX de {detectada}.")
                else:
                    with transaction.atomic():
                        importacao = form.save(commit=False)
                        importacao.instituicao = documento.instituicao or documento.banco
                        importacao.identificador_conta = documento.identificador_conta
                        importacao.data_inicio = documento.data_inicio
                        importacao.data_fim = documento.data_fim
                        importacao.data_posicao = documento.data_posicao
                        importacao.saldo_informado = documento.saldo
                        importacao.total_debitos = sum((-item.valor for item in documento.transacoes if item.valor < 0), 0)
                        importacao.total_creditos = sum((item.valor for item in documento.transacoes if item.valor > 0), 0)
                        importacao.total_liquido = sum((item.valor for item in documento.transacoes), 0)
                        importacao.adaptador = "conta_ofx" if documento.origem == Importacao.Origem.CONTA else "cartao_ofx"
                        importacao.versao_adaptador = "1.0"
                        importacao.save()
                        criar_itens(importacao, documento)
                    messages.success(request, "Arquivo processado. Revise a conciliação antes de confirmar.")
                    return redirect("importacao_revisao", pk=importacao.pk)
    else:
        form = ImportacaoOFXForm(usuario=request.user)
    if request.method == "POST" and form.errors:
        mensagens_exibidas = set()
        for erros in form.errors.values():
            for erro in erros:
                texto = str(erro)
                if texto not in mensagens_exibidas:
                    messages.error(request, texto)
                    mensagens_exibidas.add(texto)
    importacoes = Importacao.objects.filter(usuario=request.user).select_related("conta", "cartao")[:7]
    return render(
        request,
        "financeiro/importar.html",
        {
            "form": form,
            "form_planilha": ImportacaoPlanilhaForm(usuario=request.user),
            "importacoes": importacoes,
        },
    )


@login_required
def importar_planilha(request):
    form = ImportacaoPlanilhaForm(request.POST or None, request.FILES or None, usuario=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            compras = ler_compras_planilha(form.cleaned_data["arquivo_planilha"], request.user)
            with transaction.atomic():
                importacao = form.save(commit=False)
                importacao.data_inicio = min(compra["data_compra"] for compra in compras)
                importacao.data_fim = max(compra["data_compra"] for compra in compras)
                importacao.total_debitos = sum(
                    (compra["valor_total"] for compra in compras if compra["valor_total"] > 0),
                    Decimal("0.00"),
                )
                importacao.total_creditos = sum(
                    (-compra["valor_total"] for compra in compras if compra["valor_total"] < 0),
                    Decimal("0.00"),
                )
                importacao.total_liquido = importacao.total_creditos - importacao.total_debitos
                importacao.save()
                criar_itens_planilha(importacao, compras)
        except ValidationError as exc:
            mensagem = exc.messages[0] if getattr(exc, "messages", None) else str(exc)
            form.add_error("arquivo_planilha", mensagem)
        else:
            messages.success(request, "Planilha processada. Revise as compras futuras antes de confirmar.")
            return redirect("importacao_revisao", pk=importacao.pk)
    if request.method == "POST" and form.errors:
        mensagens_exibidas = set()
        for erros in form.errors.values():
            for erro in erros:
                texto = str(erro)
                if texto not in mensagens_exibidas:
                    messages.error(request, texto)
                    mensagens_exibidas.add(texto)
    importacoes = Importacao.objects.filter(usuario=request.user).select_related("conta", "cartao")[:20]
    return render(
        request,
        "financeiro/importar.html",
        {
            "form": ImportacaoOFXForm(usuario=request.user),
            "form_planilha": form,
            "importacoes": importacoes,
        },
    )


@login_required
@require_GET
def baixar_modelo_planilha(request):
    resposta = HttpResponse(content_type="text/csv; charset=utf-8")
    resposta["Content-Disposition"] = 'attachment; filename="modelo_compras_cartao.csv"'
    resposta.write("\ufeffdescricao;data_compra;valor_total;quantidade_parcelas;categoria;pessoa;ambito;observacao\n")
    resposta.write("Exemplo de assinatura;15/10/2026;49,90;1;;;PESSOAL;\n")
    return resposta


@login_required
def importacao_revisao(request, pk):
    importacao = get_object_or_404(Importacao.objects.select_related("conta", "cartao"), usuario=request.user, pk=pk)
    if request.method == "POST":
        if importacao.status != Importacao.Status.EM_REVISAO:
            messages.error(request, "Este lote não está mais disponível para edição.")
            return redirect("importacao_resultado", pk=importacao.pk)
        item = get_object_or_404(ItemImportacao, pk=request.POST.get("item_id"), importacao=importacao)
        acao_anterior = item.acao
        status_anterior = item.status
        revisado_em_anterior = item.revisado_em
        form_item = ItemImportacaoForm(request.POST, instance=item, usuario=request.user)
        if form_item.is_valid():
            try:
                with transaction.atomic():
                    item = form_item.save(commit=False)
                    selecionar_vinculo_item(
                        item,
                        request.user,
                        titulo_id=form_item.cleaned_data.get("titulo_alvo_id"),
                        titulos_ids=form_item.cleaned_data.get("titulos_alvo_ids"),
                        movimento_id=form_item.cleaned_data.get("movimento_alvo_id"),
                        parcela_id=form_item.cleaned_data.get("parcela_alvo_id"),
                        remover_vinculo=form_item.cleaned_data.get("remover_vinculo", False),
                    )
                    if (
                        revisado_em_anterior is None
                        and item.acao == ItemImportacao.Acao.CRIAR
                        and (
                            acao_anterior != ItemImportacao.Acao.CRIAR
                            or status_anterior != ItemImportacao.Status.NOVO
                        )
                    ):
                        protegidos = {"natureza"}
                        if form_item.cleaned_data.get("categoria"):
                            protegidos.add("categoria")
                        if form_item.cleaned_data.get("pessoa"):
                            protegidos.add("pessoa")
                        if form_item.cleaned_data.get("ambito") in {"PESSOAL", "PROFISSIONAL"}:
                            protegidos.add("ambito")
                        aplicar_classificacao_primeira_revisao(
                            item,
                            campos_protegidos=protegidos,
                        )
                    atualizar_status_revisao(item)
                    item.save()
                    sincronizar_titulos_multiplos(item, getattr(item, "_titulos_multiplos_ids", []))
            except ValidationError as exc:
                form_item.add_error("acao", exc.messages[0])
            else:
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    resumo_atualizado = resumo_importacao(importacao)
                    return JsonResponse(
                        {
                            "ok": True,
                            "message": "Item da importação atualizado.",
                            "item": {
                                "id": item.pk,
                                "status": item.status,
                                "status_display": item.get_status_display(),
                                "motivo": item.motivo_status or "Novo registro identificado.",
                                "natureza": item.natureza,
                                "categoria": item.categoria_id or "",
                                "pessoa": item.pessoa_id or "",
                                "ambito": item.ambito,
                                "acao": item.acao,
                                "revisado": item.revisado_em is not None,
                                "aviso_ciclo": aviso_ciclo_cartao(
                                    importacao.cartao,
                                    item.data_final,
                                    importacao.ciclo,
                                    numero_parcela=item.numero_parcela,
                                    total_parcelas=item.total_parcelas,
                                ),
                            },
                            "contagens": resumo_atualizado["contagens"],
                            "titulos_multiplos": {
                                "quantidade": item.titulos_multiplos.count(),
                                "total": str(sum((vinculo.valor for vinculo in item.titulos_multiplos.all()), Decimal("0.00"))),
                            },
                        }
                    )
                messages.success(request, "Item da importação atualizado.")
                return redirect("importacao_revisao", pk=importacao.pk)
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            erros = [str(erro) for lista in form_item.errors.values() for erro in lista]
            return JsonResponse(
                {"ok": False, "message": erros[0] if erros else "Revise os campos informados no item.", "errors": erros},
                status=422,
            )
        messages.error(request, "Revise os campos informados no item.")
    status = request.GET.get("status", "")
    revisao = request.GET.get("revisao", "")
    busca = request.GET.get("q", "").strip()
    itens = importacao.itens.select_related(
        "categoria",
        "pessoa",
        "evento",
        "movimento_conta__evento",
        "titulo_alvo__conta",
        "parcela__compra__evento",
        "parcela__fatura__cartao",
    ).prefetch_related(
        Prefetch("titulos_multiplos", queryset=VinculoTituloImportacao.objects.select_related("titulo__conta"))
    )
    if status:
        itens = itens.filter(status=status)
    if revisao == "REVISADOS":
        itens = itens.filter(revisado_em__isnull=False)
    elif revisao == "NAO_REVISADOS":
        itens = itens.filter(revisado_em__isnull=True)
    if busca:
        itens = itens.filter(Q(descricao_original__icontains=busca) | Q(identificador_externo__icontains=busca))
    itens = list(itens)
    for item in itens:
        item.titulos_multiplos_lista = list(item.titulos_multiplos.all())
        item.aviso_ciclo = aviso_ciclo_cartao(
            importacao.cartao,
            item.data_final,
            importacao.ciclo,
            numero_parcela=item.numero_parcela,
            total_parcelas=item.total_parcelas,
        )
    contexto = {
        "importacao": importacao,
        "itens": itens,
        "resumo": resumo_importacao(importacao),
        "busca": busca,
        "status_filtro": status,
        "revisao_filtro": revisao,
        "categorias": Categoria.objects.filter(usuario=request.user, ativa=True).select_related("categoria_pai"),
        "pessoas": Pessoa.objects.filter(usuario=request.user, ativa=True),
        "naturezas": EventoFinanceiro._meta.get_field("natureza").choices,
        "ambitos": EventoFinanceiro._meta.get_field("ambito").choices,
        "acoes": ItemImportacao.Acao.choices,
        "form_lote": AplicacaoLoteForm(usuario=request.user),
    }
    return render(request, "financeiro/importacao_revisao.html", contexto)


@login_required
@require_GET
def importacao_candidatos_vinculo(request, pk, item_id):
    importacao = get_object_or_404(
        Importacao.objects.select_related("conta", "cartao"),
        usuario=request.user,
        pk=pk,
        status=Importacao.Status.EM_REVISAO,
    )
    item = get_object_or_404(
        ItemImportacao.objects.select_related("importacao", "categoria"),
        pk=item_id,
        importacao=importacao,
    )
    try:
        resultado = buscar_candidatos_vinculo(
            item,
            request.user,
            tipo=request.GET.get("tipo", "TODOS"),
            busca=request.GET.get("q", ""),
            escopo=request.GET.get("escopo", "SUGESTOES"),
            pagina=request.GET.get("pagina", 1),
        )
    except (TypeError, ValueError, ValidationError) as exc:
        mensagem = exc.messages[0] if isinstance(exc, ValidationError) else "Parâmetros de busca inválidos."
        return JsonResponse({"ok": False, "message": mensagem}, status=422)
    return JsonResponse({"ok": True, **resultado})


@login_required
@require_POST
def importacao_aplicar_lote(request, pk):
    importacao = get_object_or_404(Importacao, usuario=request.user, pk=pk, status=Importacao.Status.EM_REVISAO)
    itens_selecionados = request.POST.getlist("itens_selecionados")
    form = AplicacaoLoteForm(request.POST, usuario=request.user, itens_selecionados=itens_selecionados)
    if form.is_valid():
        try:
            resultado = aplicar_alteracoes_lote(importacao.pk, request.user, form.cleaned_data, itens_selecionados)
        except ValidationError as exc:
            erros = exc.messages
        else:
            itens = resultado["itens"]
            quantidade_preservada = resultado["quantidade_preservada"]
            mensagem = f"Classificação aplicada a {len(itens)} item(ns)."
            if quantidade_preservada:
                mensagem += (
                    " 1 item já revisado foi preservado."
                    if quantidade_preservada == 1
                    else f" {quantidade_preservada} itens já revisados foram preservados."
                )
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse(
                    {
                        "ok": True,
                        "message": mensagem,
                        "items": [
                            {
                                "id": item.pk,
                                "status": item.status,
                                "status_display": item.get_status_display(),
                                "motivo": item.motivo_status or "Novo registro identificado.",
                                "categoria": item.categoria_id or "",
                                "pessoa": item.pessoa_id or "",
                                "ambito": item.ambito,
                                "natureza": item.natureza,
                                "acao": item.acao,
                                "observacao": item.observacao,
                                "revisado": item.revisado_em is not None,
                            }
                            for item in itens
                        ],
                        "quantidade_preservada": quantidade_preservada,
                        "contagens": resumo_importacao(importacao)["contagens"],
                    }
                )
            messages.success(request, mensagem)
            return redirect("importacao_revisao", pk=importacao.pk)
    else:
        erros = [str(erro) for lista in form.errors.values() for erro in lista]
    mensagem = erros[0] if erros else "Revise os dados da classificação em lote."
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": False, "message": mensagem, "errors": erros}, status=422)
    messages.error(request, mensagem)
    return redirect("importacao_revisao", pk=importacao.pk)


@login_required
@require_POST
def importacao_confirmar(request, pk):
    try:
        importacao = confirmar_importacao(pk, request.user)
    except (Importacao.DoesNotExist, ValidationError) as exc:
        messages.error(request, "; ".join(exc.messages) if isinstance(exc, ValidationError) else "Importação não encontrada.")
        return redirect("importacao_revisao", pk=pk)
    messages.success(request, "Importação confirmada sem duplicar lançamentos conciliados.")
    return redirect("importacao_resultado", pk=importacao.pk)


@login_required
@require_POST
def importacao_excluir(request, pk):
    importacao = get_object_or_404(
        Importacao,
        usuario=request.user,
        pk=pk,
        status=Importacao.Status.EM_REVISAO,
    )
    nome_arquivo = importacao.arquivo.name
    armazenamento = importacao.arquivo.storage
    with transaction.atomic():
        importacao.delete()
        if nome_arquivo:
            transaction.on_commit(lambda: armazenamento.delete(nome_arquivo))
    messages.success(request, "Lote em revisão excluído com sucesso.")
    return redirect("importar_ofx")


@login_required
@require_POST
def importacao_desfazer(request, pk):
    try:
        importacao = desfazer_importacao(pk, request.user)
    except (Importacao.DoesNotExist, ValidationError) as exc:
        messages.error(request, "; ".join(exc.messages) if isinstance(exc, ValidationError) else "Importação não encontrada.")
        return redirect("importacao_resultado", pk=pk)
    messages.success(request, "Importação desfeita; o histórico do lote foi preservado.")
    return redirect("importacao_resultado", pk=importacao.pk)


@login_required
def importacao_resultado(request, pk):
    importacao = get_object_or_404(Importacao.objects.select_related("conta", "cartao"), usuario=request.user, pk=pk)
    return render(
        request,
        "financeiro/importacao_resultado.html",
        {"importacao": importacao, "resumo": resumo_importacao(importacao), "itens": importacao.itens.select_related("evento", "parcela")},
    )


@login_required
def placeholder(request, titulo):
    return render(request, "financeiro/placeholder.html", {"titulo": titulo})


def filtrar_cadastro(queryset, request, active_field="ativa"):
    termo = request.GET.get("q", "").strip()
    status = request.GET.get("status", "all")
    if termo:
        queryset = queryset.filter(Q(nome__icontains=termo))
    if status == "active":
        queryset = queryset.filter(**{active_field: True})
    elif status == "inactive":
        queryset = queryset.filter(**{active_field: False})
    return queryset


def cadastro_context(context, request, titulo, novo_url, tipo):
    context.update(
        {
            "titulo": titulo,
            "novo_url": novo_url,
            "tipo_cadastro": tipo,
            "busca": request.GET.get("q", ""),
            "status_filtro": request.GET.get("status", "all"),
        }
    )
    return context


def cadastro_modelo(tipo):
    return {
        "conta": (Conta, "ativa", "contas"),
        "cartao": (Cartao, "ativo", "cartoes"),
        "categoria": (Categoria, "ativa", "categorias"),
        "pessoa": (Pessoa, "ativa", "pessoas"),
    }[tipo]


@login_required
@require_POST
def alternar_cadastro(request, tipo, pk):
    model, active_field, redirect_name = cadastro_modelo(tipo)
    objeto = get_object_or_404(model, pk=pk, usuario=request.user)
    atual = getattr(objeto, active_field)
    setattr(objeto, active_field, not atual)
    objeto.save(update_fields=[active_field, "atualizado_em"])
    messages.success(request, "Cadastro ativado." if atual is False else "Cadastro inativado; histórico preservado.")
    return redirect(redirect_name)


@login_required
@require_POST
def reprocessar_saldo_conta_view(request, pk):
    conta = get_object_or_404(Conta, pk=pk, usuario=request.user)
    with transaction.atomic():
        resultado = reprocessar_saldo_conta(conta.pk)
    messages.success(
        request,
        f"Saldo da conta reprocessado: {resultado['quantidade_periodos']} ciclo(s) e "
        f"{resultado['quantidade_movimentos']} movimento(s) considerados.",
    )
    return redirect("contas")


@login_required
@require_POST
def excluir_cadastro(request, tipo, pk):
    model, _active_field, redirect_name = cadastro_modelo(tipo)
    objeto = get_object_or_404(model, pk=pk, usuario=request.user)
    try:
        objeto.delete()
        messages.success(request, "Registro excluído.")
    except ProtectedError:
        if tipo == "categoria":
            messages.error(request, "A categoria possui movimentações ou categorias vinculadas e deve ser inativada.")
        else:
            messages.error(request, "Há vínculos com este cadastro. Use Inativar para preservar o histórico.")
    return redirect(redirect_name)


@login_required
@require_POST
def excluir_evento(request, pk):
    try:
        excluidos = excluir_lancamento_conta(
            request.user,
            pk,
            excluir_futuros=request.POST.get("excluir_futuros") == "1",
        )
        messages.success(request, f"Lançamento excluído.{f' {excluidos} lançamento(s) futuro(s) previsto(s) também foram excluídos.' if excluidos else ''}")
    except (ValidationError, ProtectedError, EventoFinanceiro.DoesNotExist) as exc:
        mensagem = exc.messages[0] if isinstance(exc, ValidationError) else "Há vínculos com este lançamento. Revise os vínculos antes de excluir."
        messages.error(request, mensagem)
    periodo = request.GET.get("period") or request.POST.get("period")
    return redirect(_url_retorno_lancamento(request, periodo or "", "eventos") if periodo else "eventos")


@login_required
@require_POST
def excluir_parcela_evento(request, pk):
    try:
        excluidos = excluir_parcela_lancamento(
            request.user,
            pk,
            excluir_futuros=request.POST.get("excluir_futuros") == "1",
        )
        messages.success(request, f"Parcela excluída e fatura recalculada.{f' {excluidos} lançamento(s) futuro(s) previsto(s) também foram excluídos.' if excluidos else ''}")
    except (ValidationError, ProtectedError, ParcelaCartao.DoesNotExist) as exc:
        mensagem = exc.messages[0] if isinstance(exc, ValidationError) else "Há vínculos com esta parcela. Revise a fatura antes de excluir."
        messages.error(request, mensagem)
    periodo = request.GET.get("period") or request.POST.get("period")
    return redirect(_url_retorno_lancamento(request, periodo or "", "eventos") if periodo else "eventos")
