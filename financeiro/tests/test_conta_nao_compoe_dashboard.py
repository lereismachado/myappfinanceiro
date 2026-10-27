from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from financeiro.forms import ContaForm, ImportacaoOFXForm
from financeiro.models import (
    Ambito,
    Cartao,
    Categoria,
    CompraCartao,
    Conta,
    EventoFinanceiro,
    Fatura,
    MovimentoConta,
    NaturezaEvento,
    Orcamento,
    ParcelaCartao,
    RateioCategoria,
    Situacao,
    Titulo,
)
from financeiro.services.dashboard import consultar_dashboard
from financeiro.services.detalhamento_dashboard import consultar_detalhamento_dashboard
from financeiro.services.lancamentos import consultar_lancamentos
from financeiro.services.orcamentos import (
    enriquecer_orcamentos_com_realizado,
    resumir_orcamento_dashboard,
)


class ContaNaoCompoeDashboardTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(
            username="composicao-dashboard",
            password="senha",
        )
        self.outro_usuario = get_user_model().objects.create_user(
            username="composicao-dashboard-outro",
            password="senha",
        )
        self.conta_incluida = Conta.objects.create(
            usuario=self.usuario,
            nome="Conta incluída",
            saldo_inicial=Decimal("100.00"),
            data_saldo_inicial=date(2026, 10, 1),
        )
        self.conta_excluida = Conta.objects.create(
            usuario=self.usuario,
            nome="Conta fora da Visão Geral",
            saldo_inicial=Decimal("1000.00"),
            data_saldo_inicial=date(2026, 10, 1),
            nao_compoe_dashboard=True,
        )
        self.categoria = Categoria.objects.create(
            usuario=self.usuario,
            nome="Alimentação",
        )
        self.client.force_login(self.usuario)

    def movimento(self, conta, valor, *, categoria=None, data_evento=date(2026, 10, 5)):
        valor = Decimal(valor)
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.RECEITA if valor > 0 else NaturezaEvento.DESPESA,
            descricao=f"Movimento {conta.nome}",
            data=data_evento,
            valor=abs(valor),
            situacao=Situacao.REALIZADO,
        )
        if categoria:
            RateioCategoria.objects.create(
                evento=evento,
                categoria=categoria,
                valor=abs(valor),
            )
        return MovimentoConta.objects.create(
            conta=conta,
            evento=evento,
            data_efetiva=data_evento,
            valor=valor,
        )

    def titulo(self, conta, valor, *, vencimento=date(2026, 10, 20)):
        valor = Decimal(valor)
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao=f"Título {conta.nome}",
            data=vencimento,
            vencimento=vencimento,
            valor=valor,
            situacao=Situacao.PREVISTO,
        )
        RateioCategoria.objects.create(
            evento=evento,
            categoria=self.categoria,
            valor=valor,
        )
        return Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao=evento.descricao,
            vencimento=vencimento,
            valor_original=valor,
            saldo_aberto=valor,
            conta=conta,
            evento=evento,
        )

    def parcela_cartao(self, cartao, valor):
        valor = Decimal(valor)
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Compra no cartão",
            data=date(2026, 10, 8),
            valor=valor,
            situacao=Situacao.REALIZADO,
        )
        compra = CompraCartao.objects.create(
            usuario=self.usuario,
            cartao=cartao,
            evento=evento,
            estabelecimento="Mercado",
            data_compra=evento.data,
            valor_total=valor,
            total_parcelas=1,
        )
        fatura = Fatura.objects.create(
            usuario=self.usuario,
            cartao=cartao,
            ciclo="2026-10",
            vencimento=date(2026, 10, 10),
        )
        return ParcelaCartao.objects.create(
            compra=compra,
            fatura=fatura,
            natureza=NaturezaEvento.DESPESA,
            descricao=evento.descricao,
            data_original=evento.data,
            categoria=self.categoria,
            valor=valor,
            situacao=Situacao.REALIZADO,
        )

    def test_formulario_persiste_parametro_e_lista_continua_calculando_saldo(self):
        conta_padrao = Conta(usuario=self.usuario, nome="Conta padrão")
        self.assertFalse(conta_padrao.nao_compoe_dashboard)

        dados = {
            "nome": "Conta reservada",
            "banco": "",
            "agencia": "",
            "numero": "",
            "titular": "",
            "tipo": Conta.Tipo.CORRENTE,
            "ambito": Ambito.NAO_DEFINIDO,
            "saldo_inicial": "50,00",
            "data_saldo_inicial": "2026-10-01",
            "limite_credito": "0,00",
            "nao_compoe_dashboard": "on",
            "ativa": "on",
        }
        formulario = ContaForm(data=dados)
        self.assertTrue(formulario.is_valid(), formulario.errors)
        conta = formulario.save(commit=False)
        conta.usuario = self.usuario
        conta.save()
        conta.refresh_from_db()
        self.assertTrue(conta.nao_compoe_dashboard)

        dados.pop("nao_compoe_dashboard")
        formulario_edicao = ContaForm(data=dados, instance=conta)
        self.assertTrue(formulario_edicao.is_valid(), formulario_edicao.errors)
        formulario_edicao.save()
        conta.refresh_from_db()
        self.assertFalse(conta.nao_compoe_dashboard)

        conta.nao_compoe_dashboard = True
        conta.save(update_fields=["nao_compoe_dashboard"])
        self.movimento(conta, "-10.00")
        with patch("financeiro.views.timezone.localdate", return_value=date(2026, 10, 15)):
            resposta = self.client.get(reverse("contas"))
        conta_listada = next(item for item in resposta.context["objetos"] if item.pk == conta.pk)
        self.assertEqual(conta_listada.indicador_saldo["saldo_atual"], Decimal("40.00"))
        resposta_formulario = self.client.get(reverse("conta_editar", args=[conta.pk]))
        self.assertContains(resposta_formulario, "Não compõe a Visão Geral")
        self.assertContains(
            resposta_formulario,
            "Lançamentos, importações, cálculo de saldos e demais funcionalidades permanecem inalterados.",
        )
        formulario_importacao = ImportacaoOFXForm(usuario=self.usuario)
        self.assertIn(conta, formulario_importacao.fields["conta"].queryset)

    def test_limite_cheque_especial_e_obrigatorio_e_aceita_zero(self):
        dados = {
            "nome": "Conta com limite obrigatório",
            "banco": "",
            "agencia": "",
            "numero": "",
            "titular": "",
            "tipo": Conta.Tipo.CORRENTE,
            "ambito": Ambito.NAO_DEFINIDO,
            "saldo_inicial": "0,00",
            "data_saldo_inicial": "",
            "limite_credito": "",
            "ativa": "on",
        }

        resposta_invalida = self.client.post(reverse("conta_nova"), dados)

        self.assertEqual(resposta_invalida.status_code, 200)
        self.assertIn("limite_credito", resposta_invalida.context["form"].errors)
        self.assertFalse(Conta.objects.filter(nome=dados["nome"]).exists())

        dados["limite_credito"] = "0,00"
        resposta_valida = self.client.post(reverse("conta_nova"), dados)

        self.assertRedirects(resposta_valida, reverse("contas"))
        conta = Conta.objects.get(nome=dados["nome"])
        self.assertEqual(conta.limite_credito, Decimal("0.00"))

    @patch("financeiro.services.dashboard.timezone.localdate", return_value=date(2026, 10, 15))
    def test_visao_geral_exclui_conta_mas_preserva_cartao_vinculado(self, _localdate):
        self.movimento(self.conta_incluida, "-10.00", categoria=self.categoria)
        self.movimento(self.conta_excluida, "-200.00", categoria=self.categoria)
        self.movimento(self.conta_excluida, "50.00", categoria=self.categoria)
        self.titulo(self.conta_incluida, "30.00")
        self.titulo(self.conta_excluida, "300.00")
        cartao = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão da conta excluída",
            dia_vencimento=10,
            conta_pagamento_padrao=self.conta_excluida,
            limite=Decimal("500.00"),
        )
        self.parcela_cartao(cartao, "100.00")

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual([conta.pk for conta in consulta["contas"]], [self.conta_incluida.pk])
        self.assertEqual(
            [item["conta"].pk for item in consulta["contas_financeiras"]],
            [self.conta_incluida.pk],
        )
        self.assertEqual(consulta["resumo"]["saldoAtual"], Decimal("90.00"))
        self.assertEqual(consulta["resumo"]["receitas"], Decimal("0.00"))
        self.assertEqual(consulta["resumo"]["despesas"], Decimal("110.00"))
        self.assertEqual(consulta["resumo"]["resultado"], Decimal("-110.00"))
        self.assertEqual(consulta["fluxo_caixa"]["saidas"], Decimal("10.00"))
        self.assertEqual(consulta["fluxo_caixa"]["saldo_projetado"], Decimal("60.00"))
        self.assertEqual(consulta["compromissos"]["total_30_dias"], Decimal("30.00"))
        self.assertEqual(consulta["total_gastos_por_categoria"], Decimal("110.00"))
        self.assertIn("dashboard_context=1", consulta["detalhe_base_query"])
        self.assertIn("dashboard_context=1", consulta["lancamentos_base_query"])
        limite = next(item for item in consulta["limites_cartoes"] if item["cartao"] == cartao)
        self.assertEqual(limite["comprometido"], Decimal("100.00"))
        self.assertEqual(consulta["resumo"]["faturaAtual"], Decimal("100.00"))

    def test_detalhamentos_e_lancamentos_respeitam_apenas_o_contexto_do_dashboard(self):
        self.movimento(self.conta_incluida, "-10.00", categoria=self.categoria)
        self.movimento(self.conta_excluida, "-200.00", categoria=self.categoria)

        detalhe_dashboard = consultar_detalhamento_dashboard(
            self.usuario,
            tipo="category",
            valor=str(self.categoria.pk),
            date_from=date(2026, 10, 1),
            date_to=date(2026, 10, 31),
            dashboard_context=True,
        )
        detalhe_completo = consultar_detalhamento_dashboard(
            self.usuario,
            tipo="category",
            valor=str(self.categoria.pk),
            date_from=date(2026, 10, 1),
            date_to=date(2026, 10, 31),
        )
        self.assertEqual(detalhe_dashboard["total_indicador"], Decimal("10.00"))
        self.assertEqual(detalhe_completo["total_indicador"], Decimal("210.00"))
        self.assertIn("dashboard_context=1", detalhe_dashboard["lancamentos_url"])
        self.assertNotIn("dashboard_context=1", detalhe_completo["lancamentos_url"])

        lancamentos_dashboard = consultar_lancamentos(
            self.usuario,
            data_inicial=date(2026, 10, 1),
            data_final=date(2026, 10, 31),
            analysis_type="category",
            analysis_value=str(self.categoria.pk),
            dashboard_context=True,
        )
        lancamentos_completos = consultar_lancamentos(
            self.usuario,
            data_inicial=date(2026, 10, 1),
            data_final=date(2026, 10, 31),
            analysis_type="category",
            analysis_value=str(self.categoria.pk),
        )
        self.assertEqual(lancamentos_dashboard["total_debitos"], Decimal("10.00"))
        self.assertEqual(lancamentos_completos["total_debitos"], Decimal("210.00"))
        self.assertNotIn(self.conta_excluida, lancamentos_dashboard["contas"])
        self.assertIn(self.conta_excluida, lancamentos_completos["contas"])
        self.assertIn("dashboard_context=1", lancamentos_dashboard["filtros_query"])

        parametros_detalhe = {
            "type": "category",
            "value": str(self.categoria.pk),
            "date_from": "01/10/2026",
            "date_to": "31/10/2026",
            "dashboard_context": "1",
        }
        resposta_detalhe = self.client.get(reverse("dashboard_detalhamento"), parametros_detalhe)
        self.assertEqual(resposta_detalhe.context["total_indicador"], Decimal("10.00"))

        parametros_lancamentos = {
            "analysis_type": "category",
            "analysis_value": str(self.categoria.pk),
            "date_from": "01/10/2026",
            "date_to": "31/10/2026",
            "dashboard_context": "1",
        }
        resposta_lancamentos = self.client.get(reverse("eventos"), parametros_lancamentos)
        self.assertEqual(resposta_lancamentos.context["total_debitos"], Decimal("10.00"))
        self.assertContains(
            resposta_lancamentos,
            '<input type="hidden" name="dashboard_context" value="1">',
            html=False,
        )

    def test_quadro_orcamento_do_dashboard_exclui_conta_e_tela_propria_preserva(self):
        self.movimento(self.conta_incluida, "-10.00", categoria=self.categoria)
        self.movimento(self.conta_excluida, "-200.00", categoria=self.categoria)
        cartao = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão preservado no orçamento",
            dia_vencimento=10,
            conta_pagamento_padrao=self.conta_excluida,
        )
        self.parcela_cartao(cartao, "100.00")
        orcamento = Orcamento.objects.create(
            usuario=self.usuario,
            categoria=self.categoria,
            mes="2026-10",
            ambito=Ambito.NAO_DEFINIDO,
            limite=Decimal("500.00"),
        )

        resumo_dashboard = resumir_orcamento_dashboard(
            self.usuario,
            "2026-10",
            dashboard_context=True,
        )
        resumo_completo = resumir_orcamento_dashboard(self.usuario, "2026-10")
        [orcamento_tela] = enriquecer_orcamentos_com_realizado(self.usuario, [orcamento])

        self.assertEqual(resumo_dashboard["categorias"][0]["realizado"], Decimal("110.00"))
        self.assertEqual(resumo_completo["categorias"][0]["realizado"], Decimal("310.00"))
        self.assertEqual(orcamento_tela.realizado, Decimal("310.00"))
        self.assertIn("dashboard_context=1", resumo_dashboard["categorias"][0]["detalhe_url"])
        self.assertIn("dashboard_context=1", resumo_dashboard["categorias"][0]["lancamentos_url"])
        self.assertNotIn("dashboard_context=1", orcamento_tela.detalhe_url)

        resposta_dashboard = self.client.get(reverse("dashboard"), {"period": "2026-10"})
        self.assertEqual(
            resposta_dashboard.context["orcamento_dashboard"]["categorias"][0]["realizado"],
            Decimal("110.00"),
        )

    @patch("financeiro.services.dashboard.timezone.localdate", return_value=date(2026, 10, 15))
    def test_usuario_sem_conta_participante_recebe_indicadores_zerados_e_isolados(self, _localdate):
        self.conta_incluida.nao_compoe_dashboard = True
        self.conta_incluida.save(update_fields=["nao_compoe_dashboard"])
        self.movimento(self.conta_incluida, "-10.00", categoria=self.categoria)
        self.movimento(self.conta_excluida, "-20.00", categoria=self.categoria)
        conta_alheia = Conta.objects.create(
            usuario=self.outro_usuario,
            nome="Conta de outro usuário",
            saldo_inicial=Decimal("900.00"),
            data_saldo_inicial=date(2026, 10, 1),
        )
        evento_alheio = EventoFinanceiro.objects.create(
            usuario=self.outro_usuario,
            natureza=NaturezaEvento.RECEITA,
            descricao="Receita alheia",
            data=date(2026, 10, 5),
            valor=Decimal("100.00"),
            situacao=Situacao.REALIZADO,
        )
        MovimentoConta.objects.create(
            conta=conta_alheia,
            evento=evento_alheio,
            data_efetiva=evento_alheio.data,
            valor=Decimal("100.00"),
        )

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["contas"], [])
        self.assertEqual(consulta["contas_financeiras"], [])
        self.assertEqual(consulta["resumo"]["saldoAtual"], Decimal("0.00"))
        self.assertEqual(consulta["resumo"]["receitas"], Decimal("0.00"))
        self.assertEqual(consulta["resumo"]["despesas"], Decimal("0.00"))
        self.assertEqual(consulta["fluxo_caixa"]["saldo_projetado"], Decimal("0.00"))
