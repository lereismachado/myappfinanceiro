from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from financeiro.models import (
    Ambito,
    Cartao,
    Categoria,
    CompraCartao,
    Conta,
    EventoFinanceiro,
    Fatura,
    Liquidacao,
    MovimentoConta,
    NaturezaEvento,
    Orcamento,
    ParcelaCartao,
    ProgramacaoPagamento,
    RateioCategoria,
    SaldoContaPeriodo,
    SaldoTransportadoFatura,
    Situacao,
    Titulo,
)
from financeiro.services.dashboard import consultar_dashboard
from financeiro.services.detalhamento_dashboard import (
    DetalhamentoDashboardInvalido,
    consultar_detalhamento_dashboard,
)
from financeiro.services.lancamentos import criar_lancamento, editar_lancamento_conta, excluir_lancamento_conta
from financeiro.services.saldo_contas import reprocessar_saldo_conta


class DashboardServiceTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(username="titular", password="senha")
        self.outro = get_user_model().objects.create_user(username="outro", password="senha")
        self.conta = Conta.objects.create(
            usuario=self.usuario,
            nome="Conta principal",
            saldo_inicial=Decimal("1000.00"),
            data_saldo_inicial=date(2026, 10, 1),
        )
        self.outra_conta = Conta.objects.create(usuario=self.usuario, nome="Conta secundária", ativa=False)
        self.cartao = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão principal",
            conta_pagamento_padrao=self.conta,
            dia_vencimento=10,
        )
        self.outro_cartao = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão secundário",
            conta_pagamento_padrao=self.outra_conta,
            dia_vencimento=10,
            ativo=False,
        )
        self.alimentacao = Categoria.objects.create(usuario=self.usuario, nome="Alimentação fora")

    def movimento(
        self,
        valor,
        data_movimento=date(2026, 10, 5),
        natureza=None,
        conta=None,
        usuario=None,
        categoria=None,
        data_original=None,
        data_efetiva=None,
    ):
        usuario = usuario or self.usuario
        conta = conta or self.conta
        valor = Decimal(valor)
        natureza = natureza or (NaturezaEvento.RECEITA if valor > 0 else NaturezaEvento.DESPESA)
        data_original = data_original or data_movimento
        data_efetiva = data_efetiva or data_movimento
        evento = EventoFinanceiro.objects.create(
            usuario=usuario,
            natureza=natureza,
            descricao="Movimento",
            data=data_original,
            valor=abs(valor),
            situacao=Situacao.REALIZADO,
        )
        movimento = MovimentoConta.objects.create(
            conta=conta,
            evento=evento,
            data_efetiva=data_efetiva,
            valor=valor,
        )
        if categoria:
            RateioCategoria.objects.create(evento=evento, categoria=categoria, valor=abs(valor))
        return movimento

    def parcela(
        self,
        valor,
        natureza=NaturezaEvento.DESPESA,
        categoria=None,
        cartao=None,
        ciclo="2026-10",
        data_original=None,
        data_compra=None,
        numero=1,
        total=1,
    ):
        cartao = cartao or self.cartao
        ano, mes = (int(parte) for parte in ciclo.split("-"))
        data_original = data_original or date(ano, mes, 20)
        data_compra = data_compra or data_original
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=natureza,
            descricao="Compra",
            data=data_compra,
            valor=Decimal(valor),
            situacao=Situacao.REALIZADO,
        )
        compra = CompraCartao.objects.create(
            usuario=self.usuario,
            cartao=cartao,
            evento=evento,
            estabelecimento="Compra",
            data_compra=data_compra,
            valor_total=Decimal(valor),
            total_parcelas=total,
        )
        fatura, _ = Fatura.objects.get_or_create(usuario=self.usuario, cartao=cartao, ciclo=ciclo)
        return ParcelaCartao.objects.create(
            compra=compra,
            fatura=fatura,
            natureza=natureza,
            descricao="Compra",
            data_original=data_original,
            categoria=categoria,
            numero=numero,
            total=total,
            valor=Decimal(valor),
            situacao=Situacao.REALIZADO,
        )

    def test_intervalo_padrao_usa_mes_corrente_e_limites_inclusivos(self):
        self.movimento("-900.00", date(2026, 9, 30))
        self.movimento("-20.00", date(2026, 10, 1))
        self.movimento("-30.00", date(2026, 10, 31))
        self.movimento("-800.00", date(2026, 11, 1))

        with patch("financeiro.services.dashboard.timezone.localdate", return_value=date(2026, 10, 7)):
            consulta = consultar_dashboard(self.usuario)

        self.assertEqual(consulta["data_inicial"], date(2026, 10, 1))
        self.assertEqual(consulta["data_final"], date(2026, 10, 31))
        self.assertEqual(consulta["periodo_referencia"], "2026-10")
        self.assertEqual(consulta["resumo"]["despesas"], Decimal("50.00"))

    def test_intervalo_usa_data_original_do_evento_e_saldo_usa_data_efetiva(self):
        self.movimento(
            "-100.00",
            data_original=date(2026, 10, 10),
            data_efetiva=date(2026, 11, 10),
        )
        self.movimento(
            "-300.00",
            data_original=date(2026, 11, 10),
            data_efetiva=date(2026, 10, 10),
        )

        consulta = consultar_dashboard(
            self.usuario,
            date_from="01/10/2026",
            date_to="31/10/2026",
        )

        self.assertEqual(consulta["resumo"]["despesas"], Decimal("100.00"))
        self.assertEqual(consulta["metricas"]["saldo"], Decimal("700.00"))

    def test_compra_em_doze_vezes_distribui_cem_reais_por_vencimento_mensal(self):
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Compra anual",
            data=date(2026, 1, 31),
            valor=Decimal("1200.00"),
            situacao=Situacao.REALIZADO,
        )
        compra = CompraCartao.objects.create(
            usuario=self.usuario,
            cartao=self.cartao,
            evento=evento,
            estabelecimento="Compra anual",
            data_compra=date(2026, 1, 31),
            valor_total=Decimal("1200.00"),
            total_parcelas=12,
        )
        for numero in range(1, 13):
            fatura = Fatura.objects.create(
                usuario=self.usuario,
                cartao=self.cartao,
                ciclo=f"2026-{numero:02d}",
            )
            ParcelaCartao.objects.create(
                compra=compra,
                fatura=fatura,
                natureza=NaturezaEvento.DESPESA,
                descricao=f"Compra anual {numero}/12",
                data_original=date(2026, 1, 31) if numero == 1 else None,
                categoria=self.alimentacao,
                numero=numero,
                total=12,
                valor=Decimal("100.00"),
                situacao=Situacao.REALIZADO,
            )

        for mes in range(1, 13):
            inicio = date(2026, mes, 1)
            proximo_mes = date(2027, 1, 1) if mes == 12 else date(2026, mes + 1, 1)
            fim = proximo_mes - timedelta(days=1)
            with self.subTest(mes=mes):
                consulta = consultar_dashboard(
                    self.usuario,
                    date_from=inicio.strftime("%d/%m/%Y"),
                    date_to=fim.strftime("%d/%m/%Y"),
                )
                self.assertEqual(consulta["resumo"]["despesas"], Decimal("100.00"))
                self.assertEqual(consulta["resumo"]["faturaAtual"], Decimal("100.00"))

    def test_dashboard_usa_vencimento_das_faturas_intermediarias(self):
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Importação parcial",
            data=date(2026, 5, 3),
            valor=Decimal("300.00"),
            situacao=Situacao.REALIZADO,
        )
        compra = CompraCartao.objects.create(
            usuario=self.usuario,
            cartao=self.cartao,
            evento=evento,
            estabelecimento="Importação parcial",
            data_compra=date(2026, 5, 3),
            valor_total=Decimal("1200.00"),
            total_parcelas=12,
        )
        for numero, ciclo, data_original in (
            (4, "2026-08", date(2026, 8, 15)),
            (5, "2026-09", None),
            (6, "2026-10", None),
        ):
            fatura = Fatura.objects.create(usuario=self.usuario, cartao=self.cartao, ciclo=ciclo)
            ParcelaCartao.objects.create(
                compra=compra,
                fatura=fatura,
                natureza=NaturezaEvento.DESPESA,
                descricao=f"Importação parcial {numero}/12",
                data_original=data_original,
                categoria=self.alimentacao,
                numero=numero,
                total=12,
                valor=Decimal("100.00"),
                situacao=Situacao.REALIZADO,
            )

        setembro = consultar_dashboard(
            self.usuario,
            date_from="01/09/2026",
            date_to="30/09/2026",
        )

        self.assertEqual(setembro["resumo"]["despesas"], Decimal("100.00"))
        self.assertEqual(setembro["gastos_por_categoria"][0]["nome"], "Alimentação fora")

    def test_parcela_sem_data_original_continua_usando_vencimento(self):
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Compra sem data na parcela",
            data=date(2026, 10, 12),
            valor=Decimal("80.00"),
            situacao=Situacao.REALIZADO,
        )
        compra = CompraCartao.objects.create(
            usuario=self.usuario,
            cartao=self.cartao,
            evento=evento,
            estabelecimento="Compra sem data na parcela",
            data_compra=date(2026, 10, 12),
            valor_total=Decimal("80.00"),
        )
        fatura = Fatura.objects.create(usuario=self.usuario, cartao=self.cartao, ciclo="2027-01")
        ParcelaCartao.objects.create(
            compra=compra,
            fatura=fatura,
            natureza=NaturezaEvento.DESPESA,
            descricao="Compra sem data na parcela",
            data_original=None,
            categoria=self.alimentacao,
            valor=Decimal("80.00"),
            situacao=Situacao.REALIZADO,
        )

        outubro = consultar_dashboard(
            self.usuario,
            date_from="01/10/2026",
            date_to="31/10/2026",
        )
        janeiro = consultar_dashboard(
            self.usuario,
            date_from="01/01/2027",
            date_to="31/01/2027",
        )

        self.assertEqual(outubro["resumo"]["despesas"], Decimal("0.00"))
        self.assertEqual(janeiro["resumo"]["despesas"], Decimal("80.00"))
        self.assertEqual(janeiro["resumo"]["faturaAtual"], Decimal("80.00"))

    def test_comparacao_usa_intervalo_anterior_e_evolucao_termina_no_mes_final(self):
        self.movimento("-100.00", date(2026, 9, 30))
        self.movimento("-150.00", date(2026, 10, 10))

        consulta = consultar_dashboard(
            self.usuario,
            date_from="10/10/2026",
            date_to="20/10/2026",
        )

        self.assertEqual(consulta["comparacoes_resumo"]["despesas"]["percentual"], Decimal("50.00"))
        self.assertEqual(
            [item["ciclo"] for item in consulta["evolucao"]],
            ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"],
        )
        evolucao = {item["ciclo"]: item["despesas"] for item in consulta["evolucao"]}
        self.assertEqual(evolucao["2026-09"], Decimal("100.00"))
        self.assertEqual(evolucao["2026-10"], Decimal("150.00"))

    def test_previsoes_usam_programacao_e_residual_por_vencimento(self):
        programado = Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao="Despesa parcialmente programada",
            vencimento=date(2026, 11, 5),
            valor_original=Decimal("300.00"),
            saldo_aberto=Decimal("300.00"),
            conta=self.conta,
        )
        ProgramacaoPagamento.objects.create(
            titulo=programado,
            conta=self.conta,
            data_programada=date(2026, 10, 15),
            valor_programado=Decimal("100.00"),
        )
        Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao="Despesa pelo vencimento",
            vencimento=date(2026, 10, 31),
            valor_original=Decimal("50.00"),
            saldo_aberto=Decimal("50.00"),
            conta=self.conta,
        )

        consulta = consultar_dashboard(
            self.usuario,
            date_from="01/10/2026",
            date_to="31/10/2026",
        )

        self.assertEqual(consulta["total_debitos_previstos"], Decimal("150.00"))
        self.assertEqual(consulta["fluxo_caixa"]["saidas_previstas"], Decimal("150.00"))

    def test_saldo_respeita_data_final_no_meio_do_mes(self):
        self.movimento("-100.00", data_original=date(2026, 10, 15), data_efetiva=date(2026, 10, 15))
        self.movimento("-200.00", data_original=date(2026, 10, 15), data_efetiva=date(2026, 10, 16))

        consulta = consultar_dashboard(
            self.usuario,
            date_from="01/10/2026",
            date_to="15/10/2026",
        )

        self.assertEqual(consulta["metricas"]["saldo"], Decimal("900.00"))

    def test_periodo_legado_e_precedencia_do_intervalo_explicito(self):
        self.movimento("-40.00", date(2026, 9, 10))
        self.movimento("-60.00", date(2026, 10, 10))

        legado = consultar_dashboard(self.usuario, periodo="2026-09")
        explicito = consultar_dashboard(
            self.usuario,
            periodo="2026-09",
            date_from="05/10/2026",
            date_to="15/10/2026",
        )

        self.assertEqual(legado["data_inicial"], date(2026, 9, 1))
        self.assertEqual(legado["data_final"], date(2026, 9, 30))
        self.assertEqual(legado["resumo"]["despesas"], Decimal("40.00"))
        self.assertEqual(explicito["data_inicial"], date(2026, 10, 5))
        self.assertEqual(explicito["data_final"], date(2026, 10, 15))
        self.assertEqual(explicito["resumo"]["despesas"], Decimal("60.00"))

    def test_intervalo_invalido_retorna_ao_mes_corrente(self):
        with patch("financeiro.services.dashboard.timezone.localdate", return_value=date(2026, 10, 7)):
            consulta = consultar_dashboard(
                self.usuario,
                date_from="data inválida",
                date_to="31/10/2026",
            )

        self.assertEqual(consulta["data_inicial"], date(2026, 10, 1))
        self.assertEqual(consulta["data_final"], date(2026, 10, 31))


    def test_calcula_caixa_saldo_fatura_previsoes_e_simulador(self):
        self.movimento("200.00")
        self.movimento("-100.00")
        self.movimento("-50.00", natureza=NaturezaEvento.PAGAMENTO_FATURA)
        self.parcela("823.68", categoria=self.alimentacao)
        self.parcela("23.68", natureza=NaturezaEvento.ESTORNO, categoria=self.alimentacao)
        Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao="Conta prevista",
            vencimento=date(2026, 10, 15),
            valor_original=Decimal("300.00"),
            saldo_aberto=Decimal("300.00"),
            conta=self.conta,
        )

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["metricas"]["entradas"], Decimal("200.00"))
        self.assertEqual(consulta["metricas"]["saidas"], Decimal("150.00"))
        self.assertEqual(consulta["metricas"]["saldo"], Decimal("1050.00"))
        self.assertEqual(consulta["metricas"]["fatura"], Decimal("800.00"))
        self.assertEqual(consulta["total_debitos_previstos"], Decimal("300.00"))
        self.assertEqual(consulta["saldo_apos_previsoes"], Decimal("750.00"))
        self.assertEqual(consulta["base_simulador_centavos"], 80000)
        self.assertEqual(consulta["economia_inicial"], Decimal("160.00"))

    def test_fatura_separa_confirmado_de_projetado_e_exclui_cancelados_e_pagamentos(self):
        self.parcela("100.00")
        previsto = self.parcela("50.00")
        previsto.situacao = Situacao.PREVISTO
        previsto.save(update_fields=["situacao"])
        pendente = self.parcela("25.00")
        pendente.situacao = Situacao.PENDENTE
        pendente.save(update_fields=["situacao"])
        cancelado = self.parcela("900.00")
        cancelado.situacao = Situacao.CANCELADO
        cancelado.save(update_fields=["situacao"])
        credito = self.parcela("10.00", natureza=NaturezaEvento.ESTORNO)
        credito.situacao = Situacao.PREVISTO
        credito.save(update_fields=["situacao"])
        self.parcela("300.00", natureza=NaturezaEvento.PAGAMENTO_FATURA)

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["resumo"]["faturaConfirmada"], Decimal("100.00"))
        self.assertEqual(consulta["resumo"]["faturaProjetada"], Decimal("165.00"))
        self.assertEqual(consulta["resumo"]["faturaAtual"], Decimal("165.00"))
        self.assertEqual(consulta["compromissos"]["fatura_projetada"], Decimal("0.00"))

    def test_fatura_projetada_dos_compromissos_usa_fechadas_sem_baixa_e_respeita_filtros(self):
        def criar_cartao(nome, conta=None):
            return Cartao.objects.create(
                usuario=self.usuario,
                nome=nome,
                conta_pagamento_padrao=conta or self.conta,
                dia_vencimento=10,
            )

        def criar_titulo(cartao, ciclo, valor, *, status_fatura=Fatura.Status.FECHADA, conta=None):
            fatura = Fatura.objects.create(
                usuario=self.usuario,
                cartao=cartao,
                ciclo=ciclo,
                status=status_fatura,
            )
            return Titulo.objects.create(
                usuario=self.usuario,
                tipo=Titulo.Tipo.PAGAR,
                descricao=f"Fatura {ciclo}",
                vencimento=date(int(ciclo[:4]), int(ciclo[5:]), 10),
                valor_original=Decimal(valor),
                saldo_aberto=Decimal(valor),
                conta=conta or cartao.conta_pagamento_padrao,
                fatura=fatura,
            )

        criar_titulo(self.cartao, "2026-10", "100.00")
        cartao_compativel = criar_cartao("Cartão compatível")
        criar_titulo(cartao_compativel, "2026-10", "50.00")
        criar_titulo(self.cartao, "2026-09", "900.00")
        criar_titulo(criar_cartao("Cartão aberto"), "2026-10", "800.00", status_fatura=Fatura.Status.ABERTA)
        criar_titulo(criar_cartao("Cartão parcial"), "2026-10", "700.00", status_fatura=Fatura.Status.PARCIAL)
        criar_titulo(self.outro_cartao, "2026-10", "600.00", conta=self.outra_conta)

        titulo_com_baixa = criar_titulo(criar_cartao("Cartão com baixa"), "2026-10", "400.00")
        movimento = self.movimento(
            "-100.00",
            natureza=NaturezaEvento.PAGAMENTO_FATURA,
        )
        Liquidacao.objects.create(
            titulo=titulo_com_baixa,
            movimento_conta=movimento,
            valor=Decimal("100.00"),
            data=date(2026, 10, 5),
        )

        titulo_liquidado = criar_titulo(criar_cartao("Cartão liquidado"), "2026-10", "300.00")
        titulo_liquidado.status = Titulo.Status.LIQUIDADO
        titulo_liquidado.ativo = False
        titulo_liquidado.saldo_aberto = Decimal("0.00")
        titulo_liquidado.save(update_fields=["status", "ativo", "saldo_aberto"])

        consulta = consultar_dashboard(
            self.usuario,
            periodo="2026-10",
            conta_id=self.conta.pk,
        )
        somente_cartao = consultar_dashboard(
            self.usuario,
            periodo="2026-10",
            cartao_id=self.cartao.pk,
        )
        setembro = consultar_dashboard(
            self.usuario,
            periodo="2026-09",
            conta_id=self.conta.pk,
            cartao_id=self.cartao.pk,
        )

        self.assertEqual(consulta["compromissos"]["fatura_projetada"], Decimal("150.00"))
        self.assertEqual(somente_cartao["compromissos"]["fatura_projetada"], Decimal("100.00"))
        self.assertEqual(setembro["compromissos"]["fatura_projetada"], Decimal("900.00"))
        self.assertEqual(consulta["resumo"]["faturaProjetada"], Decimal("0.00"))

    def test_fluxo_projetado_soma_realizados_e_previsoes(self):
        self.movimento("200.00")
        self.movimento("-50.00")
        Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.RECEBER,
            descricao="Receita prevista",
            vencimento=date(2026, 10, 20),
            valor_original=Decimal("400.00"),
            saldo_aberto=Decimal("400.00"),
            conta=self.conta,
        )
        Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao="Saída prevista",
            vencimento=date(2026, 10, 25),
            valor_original=Decimal("150.00"),
            saldo_aberto=Decimal("150.00"),
            conta=self.conta,
        )

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["fluxo_caixa"]["entradas"], Decimal("200.00"))
        self.assertEqual(consulta["fluxo_caixa"]["saidas"], Decimal("50.00"))
        self.assertEqual(consulta["fluxo_caixa"]["saldo"], Decimal("1150.00"))
        self.assertEqual(consulta["fluxo_caixa"]["receitas_previstas"], Decimal("400.00"))
        self.assertEqual(consulta["fluxo_caixa"]["saidas_previstas"], Decimal("150.00"))
        self.assertEqual(consulta["fluxo_caixa"]["saldo_projetado"], Decimal("1400.00"))

    def test_saldo_usa_abertura_e_exclui_previsoes(self):
        self.movimento("999.00", data_movimento=date(2026, 9, 30))
        self.movimento("-25.00", data_movimento=date(2026, 10, 1))
        evento_previsto = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Ainda não realizado",
            data=date(2026, 10, 2),
            valor=Decimal("500.00"),
            situacao=Situacao.PREVISTO,
        )
        MovimentoConta.objects.create(conta=self.conta, evento=evento_previsto, data_efetiva=evento_previsto.data, valor=Decimal("-500.00"))

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["metricas"]["saldo"], Decimal("975.00"))
        self.assertEqual(consulta["metricas"]["saidas"], Decimal("25.00"))

    def test_conta_restringe_cartoes_e_descarta_cartao_incompativel(self):
        consulta = consultar_dashboard(
            self.usuario,
            periodo="2026-10",
            conta_id=self.conta.pk,
            cartao_id=self.outro_cartao.pk,
        )
        self.assertEqual(consulta["cartoes_compativeis"], [self.cartao])
        self.assertIsNone(consulta["cartao_selecionado"])

    def test_cartao_nao_altera_metricas_de_caixa(self):
        self.movimento("-80.00")
        self.parcela("40.00", cartao=self.cartao)
        self.parcela("70.00", cartao=self.outro_cartao)

        todos = consultar_dashboard(self.usuario, periodo="2026-10")
        filtrado = consultar_dashboard(self.usuario, periodo="2026-10", cartao_id=self.cartao.pk)

        self.assertEqual(todos["metricas"]["saidas"], filtrado["metricas"]["saidas"])
        self.assertEqual(filtrado["metricas"]["fatura"], Decimal("40.00"))

    def test_categoria_neutra_nao_compoe_saida_mas_preserva_saldo(self):
        neutra = Categoria.objects.create(
            usuario=self.usuario,
            nome="Pagamento de fatura",
            tipo=Categoria.Tipo.TRANSFERENCIA,
            essencialidade=Categoria.Essencialidade.NEUTRO,
            computa_resultado=False,
        )
        movimento = self.movimento("-500.00", natureza=NaturezaEvento.PAGAMENTO_FATURA)
        RateioCategoria.objects.create(evento=movimento.evento, categoria=neutra, valor=Decimal("500.00"))

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["metricas"]["saidas"], Decimal("500.00"))
        self.assertEqual(consulta["metricas"]["saldo"], Decimal("500.00"))

    def test_rateio_misto_considera_somente_parte_computavel(self):
        computavel = Categoria.objects.create(usuario=self.usuario, nome="Computável")
        neutra = Categoria.objects.create(usuario=self.usuario, nome="Neutra", computa_resultado=False)
        movimento = self.movimento("-100.00")
        RateioCategoria.objects.create(evento=movimento.evento, categoria=computavel, valor=Decimal("40.00"))
        RateioCategoria.objects.create(evento=movimento.evento, categoria=neutra, valor=Decimal("60.00"))

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["metricas"]["saidas"], Decimal("100.00"))
        self.assertEqual(consulta["metricas"]["saldo"], Decimal("900.00"))
        self.assertEqual(consulta["resumo"]["despesas"], Decimal("40.00"))
        self.assertEqual(consulta["resumo"]["resultado"], Decimal("-40.00"))

    def test_categoria_nao_computavel_preserva_entrada_e_saldo(self):
        neutra = Categoria.objects.create(
            usuario=self.usuario,
            nome="Estorno de débito",
            tipo=Categoria.Tipo.RECEITA,
            computa_resultado=False,
        )
        self.movimento("125.00", natureza=NaturezaEvento.ESTORNO, categoria=neutra)

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["resumo"]["receitas"], Decimal("0.00"))
        self.assertEqual(consulta["resumo"]["resultado"], Decimal("0.00"))
        self.assertEqual(consulta["fluxo_caixa"]["entradas"], Decimal("125.00"))
        self.assertEqual(consulta["metricas"]["saldo"], Decimal("1125.00"))

    def test_categoria_neutra_sai_do_agrupamento_sem_alterar_total_da_fatura(self):
        neutra = Categoria.objects.create(usuario=self.usuario, nome="Neutra cartão", computa_resultado=False)
        self.cartao.limite = Decimal("200.00")
        self.cartao.save(update_fields=["limite"])
        self.parcela("100.00", categoria=neutra)

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["metricas"]["fatura"], Decimal("100.00"))
        self.assertEqual(consulta["categorias"], [])
        self.assertEqual(consulta["limites_cartoes"][0]["comprometido"], Decimal("100.00"))

    def test_isola_dados_por_usuario(self):
        conta_alheia = Conta.objects.create(usuario=self.outro, nome="Conta alheia")
        self.movimento("5000.00", conta=conta_alheia, usuario=self.outro)

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["metricas"]["entradas"], Decimal("0.00"))
        self.assertNotIn(conta_alheia, consulta["contas"])

    def test_resultado_separa_receita_despesa_e_fluxo_de_caixa(self):
        receita = Categoria.objects.create(
            usuario=self.usuario,
            nome="Receita recorrente",
            tipo=Categoria.Tipo.RECEITA,
        )
        self.movimento("100.00", natureza=NaturezaEvento.RECEITA, categoria=receita)
        self.movimento("-200.00", categoria=self.alimentacao)
        self.movimento("-50.00", natureza=NaturezaEvento.PAGAMENTO_FATURA)

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["resumo"]["receitas"], Decimal("100.00"))
        self.assertEqual(consulta["resumo"]["despesas"], Decimal("200.00"))
        self.assertEqual(consulta["resumo"]["resultado"], Decimal("-100.00"))
        self.assertEqual(consulta["fluxo_caixa"]["saidas"], Decimal("250.00"))

    def test_composicao_controlaveis_natureza_e_simulador(self):
        restaurante = Categoria.objects.create(
            usuario=self.usuario,
            nome="Restaurante",
            natureza=Categoria.Natureza.PESSOAL,
            essencialidade=Categoria.Essencialidade.ESTILO_VIDA,
        )
        aws = Categoria.objects.create(
            usuario=self.usuario,
            nome="AWS",
            natureza=Categoria.Natureza.EMPRESARIAL,
            essencialidade=Categoria.Essencialidade.PROFISSIONAL,
        )
        self.movimento("-300.00", categoria=restaurante)
        self.movimento("-800.00", categoria=aws)

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        composicao = {item["tipo"]: item["valor"] for item in consulta["composicao_despesas"]}
        natureza = {item["tipo"]: item["valor"] for item in consulta["distribuicao_natureza"]}
        self.assertEqual(composicao[Categoria.Essencialidade.ESTILO_VIDA], Decimal("300.00"))
        self.assertEqual(composicao[Categoria.Essencialidade.PROFISSIONAL], Decimal("800.00"))
        self.assertEqual(consulta["total_composicao_despesas"], Decimal("1100.00"))
        self.assertEqual(consulta["gastos_controlaveis"]["valor"], Decimal("300.00"))
        self.assertEqual(natureza[Categoria.Natureza.PESSOAL], Decimal("300.00"))
        self.assertEqual(natureza[Categoria.Natureza.EMPRESARIAL], Decimal("800.00"))
        self.assertEqual(consulta["total_distribuicao_natureza"], Decimal("1100.00"))
        self.assertEqual(consulta["total_gastos_por_categoria"], Decimal("1100.00"))
        self.assertEqual(consulta["simulador_economia"][0]["nome"], "AWS")

    def test_ponto_atencao_controlaveis_explica_composicao_e_percentual(self):
        estilo_vida = Categoria.objects.create(
            usuario=self.usuario,
            nome="Lazer",
            essencialidade=Categoria.Essencialidade.ESTILO_VIDA,
        )
        essencial = Categoria.objects.create(
            usuario=self.usuario,
            nome="Moradia",
            essencialidade=Categoria.Essencialidade.ESSENCIAL,
        )
        self.movimento("-300.00", categoria=estilo_vida)
        self.movimento("-700.00", categoria=essencial)

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(
            consulta["pontos_atencao"],
            [
                {
                    "tipo": "controlavel",
                    "titulo": "Gastos controláveis",
                    "mensagem": (
                        "Soma das despesas classificadas como Estilo de vida e Supérfluo. "
                        "Representa 30,00% das despesas do período e indica uma possibilidade "
                        "de economia."
                    ),
                    "valor": Decimal("300.00"),
                    "variacao": Decimal("0.00"),
                }
            ],
        )

    def test_estorno_de_categoria_de_despesa_reduz_resumo_e_total_por_categoria(self):
        tarifas = Categoria.objects.create(
            usuario=self.usuario,
            nome="Tarifas",
            tipo=Categoria.Tipo.DESPESA,
            natureza=Categoria.Natureza.PESSOAL,
            essencialidade=Categoria.Essencialidade.FINANCEIRO,
        )
        self.movimento("-4735.79", categoria=tarifas)
        self.movimento("15.50", natureza=NaturezaEvento.ESTORNO, categoria=tarifas)

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["resumo"]["despesas"], Decimal("4720.29"))
        self.assertEqual(consulta["resumo"]["resultado"], Decimal("-4720.29"))
        self.assertEqual(consulta["total_gastos_por_categoria"], Decimal("4720.29"))
        self.assertEqual(consulta["gastos_por_categoria"][0]["valor"], Decimal("4720.29"))
        self.assertEqual(consulta["gastos_por_categoria"][0]["percentual"], Decimal("100.00"))

    def test_estorno_de_cartao_reduz_despesas_e_detalhamento(self):
        self.parcela("100.00", categoria=self.alimentacao)
        self.parcela("15.50", natureza=NaturezaEvento.ESTORNO, categoria=self.alimentacao)

        consulta = consultar_dashboard(self.usuario, periodo="2026-10", origem="card")
        detalhe = consultar_detalhamento_dashboard(
            self.usuario,
            "category",
            str(self.alimentacao.pk),
            periodo="2026-10",
            origem="card",
        )

        self.assertEqual(consulta["resumo"]["despesas"], Decimal("84.50"))
        self.assertEqual(consulta["total_gastos_por_categoria"], Decimal("84.50"))
        self.assertEqual(detalhe["total_indicador"], Decimal("84.50"))

    def test_somente_estorno_computavel_de_categoria_de_despesa_reduz_total(self):
        receita = Categoria.objects.create(
            usuario=self.usuario,
            nome="Receita",
            tipo=Categoria.Tipo.RECEITA,
        )
        nao_computavel = Categoria.objects.create(
            usuario=self.usuario,
            nome="Despesa não computável",
            tipo=Categoria.Tipo.DESPESA,
            computa_resultado=False,
        )
        self.movimento("-100.00", categoria=self.alimentacao)
        self.movimento("10.00", natureza=NaturezaEvento.ESTORNO)
        self.movimento("10.00", natureza=NaturezaEvento.ESTORNO, categoria=receita)
        self.movimento("10.00", natureza=NaturezaEvento.ESTORNO, categoria=nao_computavel)

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["resumo"]["despesas"], Decimal("100.00"))
        self.assertEqual(consulta["total_gastos_por_categoria"], Decimal("100.00"))

    def test_categorias_nao_computaveis_tem_aba_propria_sem_alterar_resultado(self):
        investimentos = Categoria.objects.create(
            usuario=self.usuario,
            nome="Investimentos",
            tipo=Categoria.Tipo.DESPESA,
        )
        aplicacoes = Categoria.objects.create(
            usuario=self.usuario,
            nome="Aplicações",
            tipo=Categoria.Tipo.DESPESA,
            categoria_pai=investimentos,
            computa_resultado=False,
        )
        cursos = Categoria.objects.create(
            usuario=self.usuario,
            nome="Cursos",
            tipo=Categoria.Tipo.DESPESA,
            computa_resultado=False,
        )
        transferencia = Categoria.objects.create(
            usuario=self.usuario,
            nome="Entre contas",
            tipo=Categoria.Tipo.TRANSFERENCIA,
            computa_resultado=False,
        )

        rateado = self.movimento("-100.00")
        RateioCategoria.objects.create(evento=rateado.evento, categoria=self.alimentacao, valor=Decimal("40.00"))
        RateioCategoria.objects.create(evento=rateado.evento, categoria=aplicacoes, valor=Decimal("60.00"))
        self.parcela("50.00", categoria=aplicacoes)
        self.movimento("10.00", natureza=NaturezaEvento.ESTORNO, categoria=aplicacoes)
        self.movimento("-50.00", categoria=cursos)
        self.movimento("-900.00", natureza=NaturezaEvento.TRANSFERENCIA, categoria=transferencia)
        self.movimento("-800.00", natureza=NaturezaEvento.PAGAMENTO_FATURA, categoria=transferencia)

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")
        detalhe = consultar_detalhamento_dashboard(
            self.usuario,
            "category",
            str(investimentos.pk),
            periodo="2026-10",
            computacao="NAO_COMPUTA",
        )

        computaveis = {item["nome"]: item for item in consulta["gastos_por_categoria"]}
        nao_computaveis = {item["nome"]: item for item in consulta["gastos_por_categoria_nao_computa"]}
        self.assertEqual(consulta["resumo"]["despesas"], Decimal("40.00"))
        self.assertEqual(consulta["total_gastos_por_categoria"], Decimal("40.00"))
        self.assertEqual(computaveis["Alimentação fora"]["valor"], Decimal("40.00"))
        self.assertEqual(consulta["total_gastos_por_categoria_nao_computa"], Decimal("150.00"))
        self.assertEqual(nao_computaveis["Investimentos"]["valor"], Decimal("100.00"))
        self.assertEqual(nao_computaveis["Investimentos"]["percentual"], Decimal("66.67"))
        self.assertEqual(nao_computaveis["Cursos"]["valor"], Decimal("50.00"))
        self.assertEqual(nao_computaveis["Cursos"]["percentual"], Decimal("33.33"))
        self.assertNotIn("Entre contas", nao_computaveis)
        self.assertEqual(detalhe["total_indicador"], Decimal("100.00"))
        self.assertEqual(detalhe["computes"], "NAO_COMPUTA")
        self.assertEqual(parse_qs(urlsplit(detalhe["lancamentos_url"]).query)["computes"], ["NAO_COMPUTA"])

    def test_estorno_rateado_usa_apenas_categoria_de_despesa_e_pode_gerar_total_negativo(self):
        categoria_pai = Categoria.objects.create(usuario=self.usuario, nome="Tarifas")
        categoria_filha = Categoria.objects.create(
            usuario=self.usuario,
            nome="Tarifas bancárias",
            categoria_pai=categoria_pai,
        )
        categoria_receita = Categoria.objects.create(
            usuario=self.usuario,
            nome="Créditos",
            tipo=Categoria.Tipo.RECEITA,
        )
        self.movimento("-10.00", categoria=categoria_filha)
        estorno = self.movimento("30.00", natureza=NaturezaEvento.ESTORNO)
        RateioCategoria.objects.create(evento=estorno.evento, categoria=categoria_filha, valor=Decimal("25.00"))
        RateioCategoria.objects.create(evento=estorno.evento, categoria=categoria_receita, valor=Decimal("5.00"))

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["resumo"]["despesas"], Decimal("-15.00"))
        self.assertEqual(consulta["resumo"]["resultado"], Decimal("15.00"))
        self.assertEqual(consulta["total_gastos_por_categoria"], Decimal("-15.00"))
        self.assertEqual(consulta["gastos_por_categoria"][0]["nome"], "Tarifas")
        self.assertEqual(consulta["gastos_por_categoria"][0]["valor"], Decimal("-15.00"))

    def test_estorno_atualiza_comparacao_e_evolucao_com_despesa_liquida(self):
        self.movimento("-100.00", data_movimento=date(2026, 9, 5), categoria=self.alimentacao)
        self.movimento("-100.00", data_movimento=date(2026, 10, 5), categoria=self.alimentacao)
        self.movimento(
            "20.00",
            data_movimento=date(2026, 10, 6),
            natureza=NaturezaEvento.ESTORNO,
            categoria=self.alimentacao,
        )

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")
        evolucao = {item["ciclo"]: item for item in consulta["evolucao"]}

        self.assertEqual(consulta["resumo"]["despesas"], Decimal("80.00"))
        self.assertEqual(consulta["comparacoes_resumo"]["despesas"]["percentual"], Decimal("-20.00"))
        self.assertEqual(evolucao["2026-10"]["despesas"], Decimal("80.00"))
        self.assertEqual(evolucao["2026-10"]["resultado"], Decimal("-80.00"))

    def test_gastos_consolidam_conta_cartao_e_aplicam_filtros(self):
        empresarial = Categoria.objects.create(
            usuario=self.usuario,
            nome="Serviço empresarial",
            natureza=Categoria.Natureza.EMPRESARIAL,
            essencialidade=Categoria.Essencialidade.PROFISSIONAL,
        )
        self.movimento("-100.00", categoria=self.alimentacao)
        self.parcela("50.00", categoria=self.alimentacao)
        self.movimento("-800.00", categoria=empresarial)

        todos = consultar_dashboard(self.usuario, periodo="2026-10")
        pessoal = consultar_dashboard(self.usuario, periodo="2026-10", natureza="personal")
        empresarial_consulta = consultar_dashboard(self.usuario, periodo="2026-10", natureza="business")
        cartao = consultar_dashboard(self.usuario, periodo="2026-10", origem="card")

        por_categoria = {item["nome"]: item["valor"] for item in todos["gastos_por_categoria"]}
        self.assertEqual(por_categoria["Alimentação fora"], Decimal("150.00"))
        self.assertEqual(pessoal["resumo"]["despesas"], Decimal("150.00"))
        self.assertEqual(empresarial_consulta["resumo"]["despesas"], Decimal("800.00"))
        self.assertEqual(cartao["resumo"]["despesas"], Decimal("50.00"))

    def test_categoria_superior_agrupa_gastos(self):
        superior = Categoria.objects.create(usuario=self.usuario, nome="Casa")
        subcategoria = Categoria.objects.create(usuario=self.usuario, nome="Mercado", categoria_pai=superior)
        self.parcela("125.00", categoria=subcategoria)

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["gastos_por_categoria"][0]["nome"], "Casa")
        self.assertEqual(consulta["gastos_por_categoria"][0]["valor"], Decimal("125.00"))

    def test_detalhamento_consolida_rateios_e_estorno_com_o_mesmo_total_do_dashboard(self):
        estilo = Categoria.objects.create(
            usuario=self.usuario,
            nome="Restaurante",
            essencialidade=Categoria.Essencialidade.ESTILO_VIDA,
        )
        superfluo = Categoria.objects.create(
            usuario=self.usuario,
            nome="Lazer",
            essencialidade=Categoria.Essencialidade.SUPERFLUO,
        )
        profissional = Categoria.objects.create(
            usuario=self.usuario,
            nome="Ferramentas",
            essencialidade=Categoria.Essencialidade.PROFISSIONAL,
        )
        movimento = self.movimento("-100.00")
        RateioCategoria.objects.create(evento=movimento.evento, categoria=estilo, valor=Decimal("30.00"))
        RateioCategoria.objects.create(evento=movimento.evento, categoria=superfluo, valor=Decimal("20.00"))
        RateioCategoria.objects.create(evento=movimento.evento, categoria=profissional, valor=Decimal("50.00"))
        self.movimento("10.00", natureza=NaturezaEvento.ESTORNO, categoria=estilo)
        self.movimento("-500.00", natureza=NaturezaEvento.PAGAMENTO_FATURA, categoria=estilo)

        dashboard = consultar_dashboard(self.usuario, periodo="2026-10")
        essencialidade = consultar_detalhamento_dashboard(
            self.usuario,
            "essentiality",
            Categoria.Essencialidade.ESTILO_VIDA,
            periodo="2026-10",
        )
        controlaveis = consultar_detalhamento_dashboard(
            self.usuario,
            "controllable",
            periodo="2026-10",
        )

        composicao = {item["tipo"]: item["valor"] for item in dashboard["composicao_despesas"]}
        self.assertEqual(essencialidade["total_indicador"], composicao[Categoria.Essencialidade.ESTILO_VIDA])
        self.assertEqual(essencialidade["total_indicador"], Decimal("20.00"))
        self.assertEqual(controlaveis["total_indicador"], dashboard["gastos_controlaveis"]["valor"])
        self.assertEqual(controlaveis["total_indicador"], Decimal("40.00"))
        self.assertEqual(len(controlaveis["linhas"]), 2)
        linha_rateada = next(linha for linha in controlaveis["linhas"] if linha.valor == Decimal("50.00"))
        self.assertEqual(linha_rateada.categoria, "Lazer, Restaurante")

    def test_detalhamento_respeita_hierarquia_natureza_e_isolamento_por_usuario(self):
        superior = Categoria.objects.create(usuario=self.usuario, nome="Casa")
        filha = Categoria.objects.create(
            usuario=self.usuario,
            nome="Mercado",
            categoria_pai=superior,
            natureza=Categoria.Natureza.EMPRESARIAL,
        )
        self.movimento("-75.00", categoria=filha)
        categoria = consultar_detalhamento_dashboard(
            self.usuario,
            "category",
            str(superior.pk),
            periodo="2026-10",
        )
        natureza = consultar_detalhamento_dashboard(
            self.usuario,
            "category_nature",
            Categoria.Natureza.EMPRESARIAL,
            periodo="2026-10",
        )
        categoria_alheia = Categoria.objects.create(usuario=self.outro, nome="Alheia")

        self.assertEqual(categoria["total_indicador"], Decimal("75.00"))
        self.assertEqual(natureza["total_indicador"], Decimal("75.00"))
        with self.assertRaises(DetalhamentoDashboardInvalido):
            consultar_detalhamento_dashboard(
                self.usuario,
                "category",
                str(categoria_alheia.pk),
                periodo="2026-10",
            )

    def test_detalhamento_do_cartao_usa_competencia_analitica_e_exibe_data_original(self):
        categoria = Categoria.objects.create(
            usuario=self.usuario,
            nome="Assinatura",
            essencialidade=Categoria.Essencialidade.ESTILO_VIDA,
        )
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Compra parcelada",
            data=date(2026, 9, 15),
            valor=Decimal("100.00"),
            situacao=Situacao.REALIZADO,
        )
        compra = CompraCartao.objects.create(
            usuario=self.usuario,
            cartao=self.cartao,
            evento=evento,
            estabelecimento="Assinatura",
            data_compra=date(2026, 9, 15),
            valor_total=Decimal("100.00"),
            total_parcelas=2,
        )
        fatura_setembro = Fatura.objects.create(usuario=self.usuario, cartao=self.cartao, ciclo="2026-09")
        fatura_outubro = Fatura.objects.create(usuario=self.usuario, cartao=self.cartao, ciclo="2026-10")
        ParcelaCartao.objects.create(
            compra=compra,
            fatura=fatura_setembro,
            natureza=NaturezaEvento.DESPESA,
            data_original=date(2026, 9, 15),
            categoria=categoria,
            numero=1,
            total=2,
            valor=Decimal("50.00"),
        )
        parcela_outubro = ParcelaCartao.objects.create(
            compra=compra,
            fatura=fatura_outubro,
            natureza=NaturezaEvento.DESPESA,
            data_original=date(2026, 9, 15),
            categoria=categoria,
            numero=2,
            total=2,
            valor=Decimal("50.00"),
        )

        detalhe = consultar_detalhamento_dashboard(
            self.usuario,
            "essentiality",
            Categoria.Essencialidade.ESTILO_VIDA,
            date_from="01/10/2026",
            date_to="31/10/2026",
        )

        self.assertEqual([linha.registro_id for linha in detalhe["linhas"]], [parcela_outubro.pk])
        self.assertEqual(detalhe["linhas"][0].data_original, date(2026, 9, 15))
        self.assertEqual(detalhe["total_indicador"], Decimal("50.00"))

    def test_detalhamento_ordena_compras_por_data_original_e_id_do_lancamento(self):
        primeira = self.parcela(
            "10.00",
            categoria=self.alimentacao,
            ciclo="2026-10",
            data_original=date(2026, 9, 10),
        )
        segunda = self.parcela(
            "20.00",
            categoria=self.alimentacao,
            ciclo="2026-10",
            data_original=date(2026, 9, 10),
        )
        posterior = self.parcela(
            "30.00",
            categoria=self.alimentacao,
            ciclo="2026-10",
            data_original=date(2026, 9, 20),
        )

        detalhe = consultar_detalhamento_dashboard(
            self.usuario,
            "category",
            str(self.alimentacao.pk),
            periodo="2026-10",
            origem="card",
        )

        self.assertEqual(
            [linha.registro_id for linha in detalhe["linhas"]],
            [primeira.pk, segunda.pk, posterior.pk],
        )

    def test_compromissos_usam_data_atual_e_janela_de_trinta_dias(self):
        Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao="Vence em sete dias",
            vencimento=date(2026, 10, 10),
            valor_original=Decimal("100.00"),
            saldo_aberto=Decimal("100.00"),
            conta=self.conta,
        )
        Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao="Vence em trinta dias",
            vencimento=date(2026, 10, 25),
            valor_original=Decimal("200.00"),
            saldo_aberto=Decimal("200.00"),
            conta=self.conta,
        )
        Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao="Fora da janela",
            vencimento=date(2026, 11, 20),
            valor_original=Decimal("300.00"),
            saldo_aberto=Decimal("300.00"),
            conta=self.conta,
        )

        with patch("financeiro.services.dashboard.timezone.localdate", return_value=date(2026, 10, 7)):
            consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["compromissos"]["total_7_dias"], Decimal("100.00"))
        self.assertEqual(consulta["compromissos"]["total_30_dias"], Decimal("300.00"))

    def test_posicao_por_conta_usa_realizados_e_previsoes_ate_o_ciclo(self):
        self.movimento("100.00", data_movimento=date(2026, 10, 7))
        self.movimento("-20.00", data_movimento=date(2026, 10, 7))
        receber = Titulo.objects.create(
            usuario=self.usuario, tipo=Titulo.Tipo.RECEBER, descricao="Receita",
            vencimento=date(2026, 10, 12), valor_original=Decimal("60.00"),
            saldo_aberto=Decimal("60.00"), conta=self.conta,
        )
        pagar = Titulo.objects.create(
            usuario=self.usuario, tipo=Titulo.Tipo.PAGAR, descricao="Conta",
            vencimento=date(2026, 10, 15), valor_original=Decimal("100.00"),
            saldo_aberto=Decimal("100.00"), conta=self.conta,
        )
        ProgramacaoPagamento.objects.create(
            titulo=pagar, conta=self.outra_conta, data_programada=date(2026, 10, 20),
            valor_programado=Decimal("25.00"), status=ProgramacaoPagamento.Status.ATIVA,
        )
        Titulo.objects.create(
            usuario=self.usuario, tipo=Titulo.Tipo.RECEBER, descricao="Futuro",
            vencimento=date(2026, 11, 2), valor_original=Decimal("500.00"),
            saldo_aberto=Decimal("500.00"), conta=self.conta,
        )

        with patch("financeiro.services.dashboard.timezone.localdate", return_value=date(2026, 10, 7)):
            consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        contas = {item["conta"].pk: item for item in consulta["contas_financeiras"]}
        self.assertEqual(contas[self.conta.pk]["saldo_atual"], Decimal("1080.00"))
        self.assertEqual(contas[self.conta.pk]["saldo_projetado"], Decimal("1065.00"))
        self.assertEqual(contas[self.outra_conta.pk]["saldo_projetado"], Decimal("-25.00"))
        self.assertEqual(receber.saldo_aberto, Decimal("60.00"))

    def test_titulos_nao_computaveis_e_rateios_mistos_respeitam_saldo_projetado(self):
        neutra = Categoria.objects.create(usuario=self.usuario, nome="Não computável", computa_resultado=False)
        computavel = Categoria.objects.create(usuario=self.usuario, nome="Computável futura")

        evento_neutro = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Título não computável",
            data=date(2026, 10, 8),
            vencimento=date(2026, 10, 10),
            valor=Decimal("100.00"),
            situacao=Situacao.PREVISTO,
        )
        RateioCategoria.objects.create(evento=evento_neutro, categoria=neutra, valor=Decimal("100.00"))
        Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao=evento_neutro.descricao,
            vencimento=evento_neutro.vencimento,
            valor_original=Decimal("100.00"),
            saldo_aberto=Decimal("100.00"),
            conta=self.conta,
            evento=evento_neutro,
        )

        evento_misto = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Título misto",
            data=date(2026, 10, 8),
            vencimento=date(2026, 10, 15),
            valor=Decimal("100.00"),
            situacao=Situacao.PREVISTO,
        )
        RateioCategoria.objects.create(evento=evento_misto, categoria=computavel, valor=Decimal("40.00"))
        RateioCategoria.objects.create(evento=evento_misto, categoria=neutra, valor=Decimal("60.00"))
        Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao=evento_misto.descricao,
            vencimento=evento_misto.vencimento,
            valor_original=Decimal("100.00"),
            saldo_aberto=Decimal("100.00"),
            conta=self.conta,
            evento=evento_misto,
        )

        Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.RECEBER,
            descricao="Receita sem categoria",
            vencimento=date(2026, 10, 20),
            valor_original=Decimal("50.00"),
            saldo_aberto=Decimal("50.00"),
            conta=self.conta,
        )

        with patch("financeiro.services.dashboard.timezone.localdate", return_value=date(2026, 10, 7)):
            consulta = consultar_dashboard(self.usuario, periodo="2026-10")

        self.assertEqual(consulta["fluxo_caixa"]["saidas_previstas"], Decimal("40.00"))
        self.assertEqual(consulta["fluxo_caixa"]["receitas_previstas"], Decimal("50.00"))
        self.assertEqual(consulta["fluxo_caixa"]["saldo_projetado"], Decimal("1010.00"))
        contas = {item["conta"].pk: item for item in consulta["contas_financeiras"]}
        self.assertEqual(contas[self.conta.pk]["saidas_previstas"], Decimal("40.00"))
        self.assertEqual(contas[self.conta.pk]["entradas_previstas"], Decimal("50.00"))
        self.assertEqual(contas[self.conta.pk]["saldo_projetado"], Decimal("1010.00"))
        self.assertEqual(consulta["compromissos"]["total_30_dias"], Decimal("40.00"))

    def test_limite_usa_titulo_de_fatura_fechada_sem_duplicar_parcelas(self):
        self.cartao.limite = Decimal("500.00")
        self.cartao.save(update_fields=["limite"])
        parcela_fechada = self.parcela("300.00", ciclo="2026-10")
        fatura_fechada = parcela_fechada.fatura
        fatura_fechada.status = Fatura.Status.PARCIAL
        fatura_fechada.save(update_fields=["status"])
        Titulo.objects.create(
            usuario=self.usuario, tipo=Titulo.Tipo.PAGAR, descricao="Fatura fechada",
            vencimento=date(2026, 10, 20), valor_original=Decimal("300.00"),
            saldo_aberto=Decimal("200.00"), conta=self.conta, fatura=fatura_fechada,
        )
        self.parcela("100.00", ciclo="2026-11")
        self.parcela("20.00", natureza=NaturezaEvento.ESTORNO, ciclo="2026-11")

        consulta = consultar_dashboard(self.usuario, periodo="2025-01")
        limite = next(item for item in consulta["limites_cartoes"] if item["cartao"] == self.cartao)

        self.assertEqual(limite["comprometido"], Decimal("280.00"))
        self.assertEqual(limite["disponivel"], Decimal("220.00"))
        self.assertEqual(limite["percentual"], Decimal("56.00"))

    def test_limite_conta_saldo_transportado_apenas_no_destino_aberto(self):
        self.cartao.limite = Decimal("400.00")
        self.cartao.save(update_fields=["limite"])
        origem = Fatura.objects.create(usuario=self.usuario, cartao=self.cartao, ciclo="2026-09", status=Fatura.Status.PARCIAL)
        destino = Fatura.objects.create(usuario=self.usuario, cartao=self.cartao, ciclo="2026-10")
        titulo = Titulo.objects.create(
            usuario=self.usuario, tipo=Titulo.Tipo.PAGAR, descricao="Fatura origem",
            vencimento=date(2026, 9, 20), valor_original=Decimal("90.00"),
            saldo_aberto=Decimal("0.00"), ativo=False, conta=self.conta, fatura=origem,
        )
        SaldoTransportadoFatura.objects.create(
            fatura_origem=origem, titulo_origem=titulo, fatura_destino=destino,
            valor=Decimal("90.00"), data_identificacao=date(2026, 10, 1),
            status=SaldoTransportadoFatura.Status.CONFIRMADO,
        )
        SaldoTransportadoFatura.objects.create(
            fatura_origem=origem, titulo_origem=titulo, fatura_destino=destino,
            valor=Decimal("10.00"), data_identificacao=date(2026, 10, 2),
            status=SaldoTransportadoFatura.Status.IDENTIFICADO,
        )

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")
        limite = next(item for item in consulta["limites_cartoes"] if item["cartao"] == self.cartao)

        self.assertEqual(limite["comprometido"], Decimal("90.00"))
        self.assertEqual(limite["transportes_pendentes"], 1)

    def test_limite_excedido_e_sem_limite_nao_dividem_por_zero(self):
        self.cartao.limite = Decimal("100.00")
        self.cartao.save(update_fields=["limite"])
        self.parcela("125.00", ciclo="2026-10")

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")
        limite = next(item for item in consulta["limites_cartoes"] if item["cartao"] == self.cartao)
        sem_limite = next(item for item in consulta["limites_cartoes"] if item["cartao"] == self.outro_cartao)

        self.assertEqual(limite["percentual"], Decimal("125.00"))
        self.assertEqual(limite["percentual_css"], "100.00")
        self.assertEqual(limite["disponivel"], Decimal("-25.00"))
        self.assertEqual(limite["excedente"], Decimal("25.00"))
        self.assertFalse(sem_limite["limite_informado"])
        self.assertIsNone(sem_limite["percentual"])

    def test_limite_inclui_estimativa_de_parcela_e_tarifa(self):
        self.cartao.limite = Decimal("200.00")
        self.cartao.save(update_fields=["limite"])
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario, natureza=NaturezaEvento.DESPESA, descricao="Anuidade",
            data=date(2026, 10, 1), valor=Decimal("30.00"), situacao=Situacao.REALIZADO,
        )
        compra = CompraCartao.objects.create(
            usuario=self.usuario, cartao=self.cartao, evento=evento, estabelecimento="Anuidade",
            data_compra=date(2026, 10, 1), valor_total=Decimal("60.00"),
            valor_total_estimado=True, total_parcelas=2,
        )
        fatura = Fatura.objects.create(usuario=self.usuario, cartao=self.cartao, ciclo="2026-10")
        ParcelaCartao.objects.create(
            compra=compra, fatura=fatura, natureza=NaturezaEvento.DESPESA, descricao="Anuidade",
            numero=1, total=2, valor=Decimal("30.00"), situacao=Situacao.REALIZADO,
        )

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")
        limite = next(item for item in consulta["limites_cartoes"] if item["cartao"] == self.cartao)

        self.assertEqual(limite["comprometido"], Decimal("60.00"))
        self.assertTrue(limite["inclui_estimativas"])

    def test_limite_nao_duplica_parcela_futura_estimada_ja_materializada(self):
        self.cartao.limite = Decimal("200.00")
        self.cartao.save(update_fields=["limite"])
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario, natureza=NaturezaEvento.DESPESA, descricao="Compra parcelada",
            data=date(2026, 10, 1), valor=Decimal("60.00"), situacao=Situacao.REALIZADO,
        )
        compra = CompraCartao.objects.create(
            usuario=self.usuario, cartao=self.cartao, evento=evento, estabelecimento="Compra parcelada",
            data_compra=date(2026, 10, 1), valor_total=Decimal("60.00"),
            valor_total_estimado=True, total_parcelas=2,
        )
        fatura_atual = Fatura.objects.create(usuario=self.usuario, cartao=self.cartao, ciclo="2026-10")
        fatura_futura = Fatura.objects.create(usuario=self.usuario, cartao=self.cartao, ciclo="2026-11")
        ParcelaCartao.objects.create(
            compra=compra, fatura=fatura_atual, natureza=NaturezaEvento.DESPESA,
            descricao="Compra parcelada", numero=1, total=2, valor=Decimal("30.00"),
            situacao=Situacao.REALIZADO,
        )
        ParcelaCartao.objects.create(
            compra=compra, fatura=fatura_futura, natureza=NaturezaEvento.DESPESA,
            descricao="Compra parcelada", numero=2, total=2, valor=Decimal("30.00"),
            situacao=Situacao.PREVISTO,
        )

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")
        limite = next(item for item in consulta["limites_cartoes"] if item["cartao"] == self.cartao)

        self.assertEqual(limite["comprometido"], Decimal("60.00"))
        self.assertEqual(limite["disponivel"], Decimal("140.00"))
        self.assertFalse(limite["inclui_estimativas"])

    def test_limite_ignora_fatura_liquidada(self):
        self.cartao.limite = Decimal("200.00")
        self.cartao.save(update_fields=["limite"])
        parcela = self.parcela("125.00", ciclo="2026-10")
        parcela.fatura.status = Fatura.Status.PAGA
        parcela.fatura.save(update_fields=["status"])
        Titulo.objects.create(
            usuario=self.usuario, tipo=Titulo.Tipo.PAGAR, descricao="Fatura liquidada",
            vencimento=date(2026, 10, 20), valor_original=Decimal("125.00"),
            saldo_aberto=Decimal("125.00"), status=Titulo.Status.LIQUIDADO,
            ativo=False, conta=self.conta, fatura=parcela.fatura,
        )

        consulta = consultar_dashboard(self.usuario, periodo="2026-10")
        limite = next(item for item in consulta["limites_cartoes"] if item["cartao"] == self.cartao)

        self.assertEqual(limite["comprometido"], Decimal("0.00"))
        self.assertEqual(limite["disponivel"], Decimal("200.00"))
        self.assertFalse(limite["inclui_estimativas"])

    def test_paineis_respeitam_filtros_e_sao_independentes_do_periodo(self):
        self.cartao.limite = Decimal("200.00")
        self.cartao.save(update_fields=["limite"])
        self.parcela("50.00", cartao=self.cartao, ciclo="2026-11")
        self.outro_cartao.limite = Decimal("200.00")
        self.outro_cartao.save(update_fields=["limite"])
        self.parcela("70.00", cartao=self.outro_cartao, ciclo="2026-12")

        outubro = consultar_dashboard(self.usuario, periodo="2026-10", conta_id=self.conta.pk)
        janeiro = consultar_dashboard(self.usuario, periodo="2027-01", conta_id=self.conta.pk)
        por_cartao_outubro = {item["cartao"].pk: item["comprometido"] for item in outubro["limites_cartoes"]}
        por_cartao_janeiro = {item["cartao"].pk: item["comprometido"] for item in janeiro["limites_cartoes"]}

        self.assertEqual([item["conta"] for item in outubro["contas_financeiras"]], [self.conta])
        self.assertEqual(list(por_cartao_outubro), [self.cartao.pk])
        self.assertEqual(por_cartao_outubro, por_cartao_janeiro)


class SaldoContaPeriodoTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(username="saldo", password="senha")
        self.conta = Conta.objects.create(
            usuario=self.usuario,
            nome="Conta resumida",
            saldo_inicial=Decimal("1000.00"),
            data_saldo_inicial=date(2026, 10, 1),
        )
        self.client.force_login(self.usuario)

    def criar_movimento(self, valor, data_movimento=date(2026, 10, 5)):
        valor = Decimal(valor)
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.RECEITA if valor > 0 else NaturezaEvento.DESPESA,
            descricao="Movimento resumido",
            data=data_movimento,
            valor=abs(valor),
            situacao=Situacao.REALIZADO,
        )
        return MovimentoConta.objects.create(
            conta=self.conta,
            evento=evento,
            data_efetiva=data_movimento,
            valor=valor,
        )

    def test_reprocessa_saldo_mensal_com_saldo_inicial(self):
        self.criar_movimento("200.00")
        self.criar_movimento("-75.00", date(2026, 10, 6))

        resultado = reprocessar_saldo_conta(self.conta.pk)

        resumo = SaldoContaPeriodo.objects.get(conta=self.conta, periodo=date(2026, 10, 1))
        self.assertEqual(resumo.entradas, Decimal("200.00"))
        self.assertEqual(resumo.saidas, Decimal("75.00"))
        self.assertEqual(resumo.saldo_fechamento, Decimal("1125.00"))
        self.assertEqual(resumo.quantidade_movimentos, 2)
        self.assertEqual(resultado["saldo_fechamento"], Decimal("1125.00"))

    def test_reprocessamento_repetido_nao_duplica_valores(self):
        self.criar_movimento("50.00")

        reprocessar_saldo_conta(self.conta.pk)
        reprocessar_saldo_conta(self.conta.pk)

        self.assertEqual(SaldoContaPeriodo.objects.filter(conta=self.conta).count(), 1)
        self.assertEqual(
            SaldoContaPeriodo.objects.get(conta=self.conta).saldo_fechamento,
            Decimal("1050.00"),
        )

    def test_dashboard_prefere_resumo_e_exclusao_reprocessa_saldo(self):
        movimento = self.criar_movimento("125.00")
        reprocessar_saldo_conta(self.conta.pk)

        consulta = consultar_dashboard(self.usuario, periodo="2026-10", conta_id=self.conta.pk)
        self.assertEqual(consulta["metricas"]["saldo"], Decimal("1125.00"))

        excluir_lancamento_conta(self.usuario, movimento.evento_id)

        consulta = consultar_dashboard(self.usuario, periodo="2026-10", conta_id=self.conta.pk)
        self.assertEqual(consulta["metricas"]["saldo"], Decimal("1000.00"))

    def test_edicao_de_movimento_reprocessa_data_e_valor(self):
        movimento = self.criar_movimento("100.00")
        dados = {
            "descricao": "Movimento alterado",
            "natureza": NaturezaEvento.DESPESA,
            "data": date(2026, 10, 6),
            "valor": Decimal("40.00"),
            "conta": self.conta,
            "categoria": None,
            "pessoa": None,
            "pessoa_nome_livre": "",
            "ambito": Ambito.NAO_DEFINIDO,
            "situacao": Situacao.REALIZADO,
            "vencimento": None,
            "observacao": "",
        }

        editar_lancamento_conta(self.usuario, movimento.evento_id, dados)

        resumo = SaldoContaPeriodo.objects.get(conta=self.conta, periodo=date(2026, 10, 1))
        self.assertEqual(resumo.saldo_fechamento, Decimal("960.00"))

    def test_botao_reprocessar_saldo_exige_post_e_isola_usuario(self):
        outro = get_user_model().objects.create_user(username="outro-saldo", password="senha")
        outra_conta = Conta.objects.create(usuario=outro, nome="Conta alheia")

        resposta_get = self.client.get(reverse("conta_reprocessar_saldo", args=[self.conta.pk]))
        resposta_alheia = self.client.post(reverse("conta_reprocessar_saldo", args=[outra_conta.pk]))
        resposta_post = self.client.post(reverse("conta_reprocessar_saldo", args=[self.conta.pk]))

        self.assertEqual(resposta_get.status_code, 405)
        self.assertEqual(resposta_alheia.status_code, 404)
        self.assertRedirects(resposta_post, reverse("contas"))


class LancamentoDashboardTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(username="titular", password="senha")
        self.conta = Conta.objects.create(usuario=self.usuario, nome="Conta")
        self.cartao = Cartao.objects.create(
            usuario=self.usuario, nome="Cartão", conta_pagamento_padrao=self.conta, dia_vencimento=10
        )
        self.categoria = Categoria.objects.create(usuario=self.usuario, nome="Mercado")
        self.client.force_login(self.usuario)

    def dados(self, **alteracoes):
        dados = {
            "descricao": "Lançamento manual",
            "natureza": NaturezaEvento.DESPESA,
            "data": date(2026, 10, 10),
            "valor": Decimal("100.00"),
            "origem": "CONTA",
            "conta": self.conta,
            "cartao": None,
            "categoria": self.categoria,
            "pessoa": None,
            "ambito": Ambito.PESSOAL,
            "situacao": Situacao.REALIZADO,
            "vencimento": None,
            "ciclo": "",
            "quantidade_parcelas": 1,
            "observacao": "",
        }
        dados.update(alteracoes)
        return dados

    def test_previsto_cria_titulo_sem_movimentar_saldo(self):
        evento = criar_lancamento(
            self.usuario,
            self.dados(situacao=Situacao.PREVISTO, vencimento=date(2026, 10, 20)),
        )

        self.assertEqual(MovimentoConta.objects.count(), 0)
        self.assertEqual(Titulo.objects.get(evento=evento).saldo_aberto, Decimal("100.00"))

    def test_realizado_cria_movimento_assinado(self):
        criar_lancamento(self.usuario, self.dados())
        self.assertEqual(MovimentoConta.objects.get().valor, Decimal("-100.00"))

    def test_pagamento_parcial_mantem_saldo_restante_da_fatura(self):
        fatura = Fatura.objects.create(
            usuario=self.usuario,
            cartao=self.cartao,
            ciclo="2026-10",
            vencimento=date(2026, 10, 20),
            total=Decimal("200.00"),
            status=Fatura.Status.FECHADA,
        )
        titulo = Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao="Fatura outubro",
            vencimento=fatura.vencimento,
            valor_original=fatura.total,
            saldo_aberto=fatura.total,
            conta=self.conta,
            fatura=fatura,
        )

        criar_lancamento(
            self.usuario,
            self.dados(
                natureza=NaturezaEvento.PAGAMENTO_FATURA,
                categoria=None,
                cartao=self.cartao,
                ciclo="2026-10",
            ),
        )

        titulo.refresh_from_db()
        fatura.refresh_from_db()
        self.assertEqual(titulo.saldo_aberto, Decimal("100.00"))
        self.assertTrue(titulo.ativo)
        self.assertEqual(fatura.status, Fatura.Status.PARCIAL)

    def test_post_do_formulario_persiste_e_redireciona_para_o_periodo(self):
        resposta = self.client.post(
            reverse("evento_novo") + "?period=2026-10",
            {
                "descricao": "Despesa pelo dashboard",
                "natureza": NaturezaEvento.DESPESA,
                "data": "2026-10-12",
                "valor": "75,50",
                "origem": "CONTA",
                "conta": self.conta.pk,
                "cartao": "",
                "categoria": self.categoria.pk,
                "pessoa": "",
                "ambito": Ambito.PESSOAL,
                "situacao": Situacao.REALIZADO,
                "vencimento": "",
                "ciclo": "",
                "quantidade_parcelas": "1",
                "observacao": "",
            },
        )

        self.assertRedirects(resposta, "/?period=2026-10")
        self.assertEqual(MovimentoConta.objects.get().valor, Decimal("-75.50"))

    def test_post_preserva_intervalo_e_filtros_ao_retornar_para_dashboard(self):
        resposta = self.client.post(
            reverse("evento_novo")
            + "?return_to=dashboard&date_from=05%2F10%2F2026&date_to=20%2F10%2F2026"
            + f"&account={self.conta.pk}&origin=account&nature=personal",
            {
                "descricao": "Despesa com filtros preservados",
                "natureza": NaturezaEvento.DESPESA,
                "data": "2026-10-12",
                "valor": "75,50",
                "origem": "CONTA",
                "conta": self.conta.pk,
                "cartao": "",
                "categoria": self.categoria.pk,
                "pessoa": "",
                "ambito": Ambito.PESSOAL,
                "situacao": Situacao.REALIZADO,
                "vencimento": "",
                "ciclo": "",
                "quantidade_parcelas": "1",
                "observacao": "",
            },
        )

        self.assertEqual(resposta.status_code, 302)
        parametros = parse_qs(urlsplit(resposta["Location"]).query)
        self.assertEqual(parametros["date_from"], ["05/10/2026"])
        self.assertEqual(parametros["date_to"], ["20/10/2026"])
        self.assertEqual(parametros["account"], [str(self.conta.pk)])
        self.assertEqual(parametros["origin"], ["account"])
        self.assertEqual(parametros["nature"], ["personal"])

    @patch("financeiro.services.dashboard.timezone.localdate", return_value=date(2026, 10, 7))
    def test_view_renderiza_datas_do_mes_corrente_sem_seletor_de_periodo(self, _localdate):
        resposta = self.client.get(reverse("dashboard"))
        conteudo = resposta.content.decode()

        self.assertContains(resposta, 'name="date_from"')
        self.assertContains(resposta, 'value="01/10/2026"')
        self.assertContains(resposta, 'name="date_to"')
        self.assertContains(resposta, 'value="31/10/2026"')
        self.assertContains(
            resposta,
            'class="temporal-control overview-date-control" data-dashboard-date-control',
            count=2,
        )
        self.assertContains(
            resposta,
            'class="temporal-native" type="date" tabindex="-1" aria-hidden="true" data-dashboard-date-native',
            count=2,
        )
        self.assertContains(resposta, 'data-date-role="from"')
        self.assertContains(resposta, 'data-date-role="to"')
        self.assertContains(resposta, 'aria-label="Selecionar data inicial no calendário"')
        self.assertContains(resposta, 'aria-label="Selecionar data final no calendário"')
        self.assertIn("const syncFinalDateFromInitial = () =>", conteudo)
        self.assertIn("new Date(parsed.year, parsed.month, 0).getDate()", conteudo)
        self.assertIn("if (typeof native.showPicker === 'function') native.showPicker()", conteudo)
        self.assertNotContains(resposta, '<select id="periodFilter"')

    def test_view_renderiza_dashboard_e_formulario_compartilhado(self):
        resposta = self.client.get(reverse("dashboard"), {"period": "2026-10"})
        formulario = self.client.get(reverse("evento_novo"), {"period": "2026-10"})
        conteudo = resposta.content.decode()

        self.assertContains(resposta, "Gastos por categoria")
        self.assertContains(resposta, "Simulador de economia")
        self.assertContains(resposta, 'aria-label="Gastos por categoria e distribuição por natureza"')
        self.assertContains(
            resposta,
            'class="panel full overview-section collapsible-panel simulator-panel"',
        )
        self.assertContains(resposta, 'title="Abrir ou fechar o simulador de economia"')
        self.assertNotContains(
            resposta,
            'class="panel full overview-section collapsible-panel simulator-panel" open',
        )
        self.assertLess(conteudo.index('id="categoryTitle"'), conteudo.index("Distribuição por natureza"))
        self.assertLess(conteudo.index(">Fluxo de caixa</h2>"), conteudo.index('id="simulatorTitle"'))
        self.assertContains(resposta, "Receitas previstas")
        self.assertContains(resposta, "Saídas previstas")
        self.assertContains(resposta, "Saldo projetado")
        self.assertContains(resposta, "Contas financeiras")
        self.assertContains(resposta, "Limite dos cartões")
        self.assertContains(resposta, "Limite disponível calculado")
        self.assertContains(resposta, "grid-template-columns:repeat(4,minmax(0,1fr))")
        self.assertContains(resposta, ".commitment-cards{grid-template-columns:repeat(2,minmax(0,1fr))}")
        self.assertContains(resposta, ".commitment-cards,.cash-grid,.budget-list{grid-template-columns:1fr}")
        self.assertContains(formulario, 'data-lancamento-form')
        self.assertContains(formulario, 'name="origem"')

    def test_dashboard_renderiza_abas_acessiveis_de_categorias_e_escopo_nao_computavel(self):
        superior = Categoria.objects.create(usuario=self.usuario, nome="Investimentos")
        aplicacoes = Categoria.objects.create(
            usuario=self.usuario,
            nome="Aplicações",
            categoria_pai=superior,
            computa_resultado=False,
        )
        criar_lancamento(self.usuario, self.dados(categoria=aplicacoes))

        resposta = self.client.get(reverse("dashboard"), {"period": "2026-10"})

        self.assertContains(resposta, 'role="tablist"')
        self.assertContains(resposta, 'id="categoryTabComputa"')
        self.assertContains(resposta, 'aria-selected="true"')
        self.assertContains(resposta, 'id="categoryPanelNaoComputa"')
        self.assertContains(resposta, 'data-category-panel="NAO_COMPUTA" hidden')
        self.assertContains(resposta, "Compõe no resultado")
        self.assertContains(resposta, "Não compõe no resultado")
        self.assertContains(resposta, "Ver lançamentos que não compõem a categoria Investimentos")
        self.assertContains(resposta, "Investimentos")
        self.assertContains(resposta, "computes=NAO_COMPUTA", count=2)
        self.assertContains(resposta, "ArrowRight")
        self.assertContains(resposta, "ArrowLeft")

    def test_endpoint_e_listagem_detalham_categoria_nao_computavel(self):
        superior = Categoria.objects.create(usuario=self.usuario, nome="Investimentos")
        aplicacoes = Categoria.objects.create(
            usuario=self.usuario,
            nome="Aplicações",
            categoria_pai=superior,
            computa_resultado=False,
        )
        criar_lancamento(self.usuario, self.dados(categoria=aplicacoes, valor=Decimal("125.00")))

        detalhe = self.client.get(
            reverse("dashboard_detalhamento"),
            {
                "type": "category",
                "value": superior.pk,
                "period": "2026-10",
                "computes": "NAO_COMPUTA",
            },
        )
        listagem = self.client.get(
            reverse("eventos"),
            {
                "period": "2026-10",
                "analysis_type": "category",
                "analysis_value": superior.pk,
                "parent_category": superior.pk,
                "computes": "NAO_COMPUTA",
            },
        )
        invalido = self.client.get(
            reverse("dashboard_detalhamento"),
            {"type": "category", "value": superior.pk, "computes": "INVALIDO"},
        )

        self.assertEqual(detalhe.status_code, 200)
        self.assertContains(detalhe, "R$ 125,00")
        self.assertContains(detalhe, "computes=NAO_COMPUTA")
        self.assertContains(listagem, "Aplicações")
        self.assertContains(listagem, "R$ 125,00")
        self.assertEqual(invalido.status_code, 404)

    def test_dashboard_renderiza_controle_de_ocultacao_de_valores(self):
        resposta = self.client.get(reverse("dashboard"), {"period": "2026-10"})

        self.assertContains(resposta, 'data-dashboard-values-toggle')
        self.assertContains(resposta, 'aria-label="Ocultar valores"')
        self.assertContains(resposta, "my-financeiro.dashboard.values-hidden")
        self.assertContains(resposta, "data-sensitive-value")
        self.assertContains(resposta, "Valor oculto")

    def test_dashboard_renderiza_orcamento_fixo_ao_lado_do_fluxo(self):
        Orcamento.objects.create(
            usuario=self.usuario,
            categoria=self.categoria,
            mes="2026-10",
            ambito=Ambito.PESSOAL,
            limite=Decimal("100.00"),
        )
        criar_lancamento(self.usuario, self.dados(valor=Decimal("95.00")))

        resposta = self.client.get(reverse("dashboard"), {"period": "2026-10"})
        conteudo = resposta.content.decode()
        inicio_orcamento = conteudo.index('<article class="panel budget-panel"')
        fim_orcamento = conteudo.index("</article>", inicio_orcamento)
        trecho_orcamento = conteudo[inicio_orcamento:fim_orcamento]

        self.assertContains(resposta, 'aria-label="Fluxo de caixa e orçamento"')
        self.assertContains(resposta, 'class="panel budget-panel"')
        self.assertContains(resposta, "data-cash-flow-panel")
        self.assertContains(resposta, "data-budget-overview-panel")
        self.assertContains(resposta, "Orçamento <span class=\"collapsible-summary-meta\">Total ·")
        self.assertContains(resposta, "10/2026")
        self.assertContains(resposta, "R$ 100,00")
        self.assertContains(resposta, "95,00% comprometido")
        self.assertContains(resposta, 'style="width:95.00%"')
        self.assertContains(resposta, 'role="progressbar"')
        self.assertContains(resposta, 'class="budget-item-budget"')
        self.assertContains(resposta, 'class="budget-expense-row"')
        self.assertContains(resposta, 'class="budget-expense-values"')
        self.assertNotContains(resposta, 'class="budget-bar-fill share"')
        self.assertNotContains(resposta, "Participação no total orçado")
        self.assertNotContains(resposta, ">Orçado ")
        self.assertNotIn("<strong", trecho_orcamento)
        self.assertEqual(trecho_orcamento.count('role="progressbar"'), 1)
        self.assertContains(resposta, ".budget-panel h2,.budget-panel h2 *,.budget-item,.budget-item *{font-weight:400}")
        self.assertContains(resposta, ".budget-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))")
        self.assertContains(resposta, ".commitment-cards,.cash-grid,.budget-list{grid-template-columns:1fr}")
        self.assertContains(resposta, "overflow-y:auto")
        self.assertContains(resposta, "new ResizeObserver(syncBudgetPanelHeight).observe(cashFlowPanel)")
        self.assertContains(resposta, ".overview-section.simulator-panel{margin-top:22px}")
        self.assertNotContains(resposta, "data-budget-panel")
        self.assertNotContains(resposta, "my-financeiro.dashboard.budget-open")
        self.assertNotContains(resposta, 'title="Abrir ou fechar o indicador de orçamento"')
        self.assertContains(resposta, "Orçamento próximo do limite")
        self.assertLess(conteudo.index(">Fluxo de caixa</h2>"), conteudo.index('id="budgetTitle"'))
        self.assertEqual(resposta.context["pontos_atencao"][0]["tipo"], "orcamento")

        resposta_sem_orcamento = self.client.get(reverse("dashboard"), {"period": "2026-11"})
        self.assertContains(resposta_sem_orcamento, "Nenhum orçamento cadastrado para 11/2026.")
        self.assertNotContains(resposta_sem_orcamento, "Cadastrar orçamento")

    def test_dashboard_orcamento_usa_mes_completo_da_data_final_e_ignora_filtros(self):
        Orcamento.objects.create(
            usuario=self.usuario,
            categoria=self.categoria,
            mes="2026-11",
            ambito=Ambito.PESSOAL,
            limite=Decimal("200.00"),
        )
        criar_lancamento(
            self.usuario,
            self.dados(data=date(2026, 11, 20), valor=Decimal("50.00")),
        )

        resposta = self.client.get(
            reverse("dashboard"),
            {
                "date_from": "15/10/2026",
                "date_to": "05/11/2026",
                "origin": "card",
            },
        )

        orcamento = resposta.context["orcamento_dashboard"]
        self.assertEqual(orcamento["mes"], "2026-11")
        self.assertEqual(orcamento["total_orcado"], Decimal("200.00"))
        self.assertEqual(orcamento["categorias"][0]["realizado"], Decimal("50.00"))
        self.assertEqual(orcamento["categorias"][0]["percentual_comprometido"], Decimal("25.00"))

    def test_barras_usam_decimal_com_ponto_no_atributo_css(self):
        outra_categoria = Categoria.objects.create(usuario=self.usuario, nome="Outra categoria")
        criar_lancamento(self.usuario, self.dados(valor=Decimal("25.00")))
        criar_lancamento(self.usuario, self.dados(valor=Decimal("75.00"), categoria=outra_categoria))

        resposta = self.client.get(reverse("dashboard"), {"period": "2026-10"})

        self.assertContains(resposta, 'style="width:25.00%"')
        self.assertContains(resposta, 'style="width:75.00%"')
        self.assertNotContains(resposta, 'style="width:25,00%"')
        self.assertContains(resposta, 'amount-negative')
        self.assertContains(resposta, '.evolution-bar.expense{background:var(--danger)}')

    def test_dashboard_exibe_totalizadores_nos_tres_indicadores_solicitados(self):
        self.categoria.essencialidade = Categoria.Essencialidade.ESSENCIAL
        self.categoria.natureza = Categoria.Natureza.PESSOAL
        self.categoria.save(update_fields=["essencialidade", "natureza"])
        criar_lancamento(self.usuario, self.dados(valor=Decimal("125.00")))

        resposta = self.client.get(reverse("dashboard"), {"period": "2026-10"})

        self.assertContains(
            resposta,
            'Composição das despesas <span class="collapsible-summary-meta">Total ·',
        )
        self.assertContains(
            resposta,
            'Gastos por categoria <span class="collapsible-summary-meta">Total ·',
        )
        self.assertContains(
            resposta,
            'Distribuição por natureza <span class="collapsible-summary-meta">Total ·',
        )
        self.assertContains(resposta, "R$ 125,00")
        self.assertNotContains(resposta, "Gastos controláveis <span class=\"collapsible-summary-meta\"")

    def test_dashboard_renderiza_gatilhos_acessiveis_e_modal_de_detalhamento(self):
        criar_lancamento(self.usuario, self.dados())

        resposta = self.client.get(reverse("dashboard"), {"period": "2026-10"})

        self.assertContains(resposta, 'data-dashboard-detail-url=')
        self.assertContains(resposta, 'id="dashboardDetailDialog"')
        self.assertContains(resposta, 'aria-label="Ver lançamentos que compõem')
        self.assertContains(resposta, 'aria-disabled="true"')
        self.assertContains(resposta, 'data-close-dashboard-detail')

    def test_endpoint_de_detalhamento_e_autenticado_consultivo_e_filtrado(self):
        self.categoria.essencialidade = Categoria.Essencialidade.ESSENCIAL
        self.categoria.save(update_fields=["essencialidade"])
        criar_lancamento(self.usuario, self.dados())

        resposta = self.client.get(
            reverse("dashboard_detalhamento"),
            {
                "type": "essentiality",
                "value": Categoria.Essencialidade.ESSENCIAL,
                "date_from": "01/10/2026",
                "date_to": "31/10/2026",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Lançamentos que compõem o indicador")
        self.assertContains(resposta, "Essencial")
        self.assertContains(resposta, "Valor no indicador")
        self.assertContains(resposta, "Ver em lançamentos")
        self.assertNotContains(resposta, "Ações</th>")

        resposta_tipo_invalido = self.client.get(
            reverse("dashboard_detalhamento"),
            {"type": "desconhecido", "value": "x"},
        )
        resposta_valor_invalido = self.client.get(
            reverse("dashboard_detalhamento"),
            {"type": "essentiality", "value": "INVALIDA"},
        )
        self.assertEqual(resposta_tipo_invalido.status_code, 404)
        self.assertEqual(resposta_valor_invalido.status_code, 404)

        resposta_vazia = self.client.get(
            reverse("dashboard_detalhamento"),
            {
                "type": "essentiality",
                "value": Categoria.Essencialidade.PROFISSIONAL,
                "date_from": "01/10/2026",
                "date_to": "31/10/2026",
            },
        )
        self.assertContains(resposta_vazia, "Nenhum lançamento compõe este indicador no período.")

        self.client.logout()
        resposta_anonima = self.client.get(
            reverse("dashboard_detalhamento"),
            {"type": "essentiality", "value": Categoria.Essencialidade.ESSENCIAL},
        )
        self.assertEqual(resposta_anonima.status_code, 302)

    def test_modal_de_detalhamento_exibe_vencimento_da_fatura_do_cartao(self):
        self.categoria.essencialidade = Categoria.Essencialidade.ESSENCIAL
        self.categoria.save(update_fields=["essencialidade"])
        self.cartao.dia_vencimento = 11
        self.cartao.save(update_fields=["dia_vencimento"])
        criar_lancamento(
            self.usuario,
            self.dados(
                origem="CARTAO",
                conta=None,
                cartao=self.cartao,
                data=date(2026, 9, 17),
                ciclo="2026-11",
            ),
        )

        resposta = self.client.get(
            reverse("dashboard_detalhamento"),
            {
                "type": "essentiality",
                "value": Categoria.Essencialidade.ESSENCIAL,
                "date_from": "01/11/2026",
                "date_to": "30/11/2026",
                "origin": "card",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "17/09/2026")
        self.assertContains(resposta, "Vcto: 11/11/2026")
        self.assertContains(resposta, "Ciclo: 11/2026")

    def test_modal_de_detalhamento_exibe_observacao_com_quebras_da_compra_cartao(self):
        self.categoria.essencialidade = Categoria.Essencialidade.ESSENCIAL
        self.categoria.save(update_fields=["essencialidade"])
        criar_lancamento(
            self.usuario,
            self.dados(
                origem="CARTAO",
                conta=None,
                cartao=self.cartao,
                ciclo="2026-10",
                observacao="Conferido na fatura.\nComprovante arquivado.",
            ),
        )

        resposta = self.client.get(
            reverse("dashboard_detalhamento"),
            {
                "type": "essentiality",
                "value": Categoria.Essencialidade.ESSENCIAL,
                "date_from": "01/10/2026",
                "date_to": "31/10/2026",
                "origin": "card",
            },
        )
        conteudo = resposta.content.decode()

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'class="transaction-observation-row"', count=1)
        self.assertContains(resposta, '<td colspan="4">', count=1)
        self.assertContains(resposta, "Observação:")
        self.assertIn("Conferido na fatura.\nComprovante arquivado.", conteudo)
        self.assertNotContains(resposta, "Ações</th>")

    def test_endpoint_rejeita_categoria_alheia_e_listagem_preserva_contexto_analitico(self):
        outro = get_user_model().objects.create_user(username="outro-dashboard", password="senha")
        categoria_alheia = Categoria.objects.create(usuario=outro, nome="Categoria alheia")
        resposta_alheia = self.client.get(
            reverse("dashboard_detalhamento"),
            {"type": "category", "value": categoria_alheia.pk, "period": "2026-10"},
        )

        outra_categoria = Categoria.objects.create(
            usuario=self.usuario,
            nome="Profissional",
            essencialidade=Categoria.Essencialidade.PROFISSIONAL,
        )
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Despesa rateada",
            data=date(2026, 10, 10),
            valor=Decimal("100.00"),
            situacao=Situacao.REALIZADO,
        )
        MovimentoConta.objects.create(
            conta=self.conta,
            evento=evento,
            data_efetiva=date(2026, 10, 10),
            valor=Decimal("-100.00"),
        )
        RateioCategoria.objects.create(evento=evento, categoria=self.categoria, valor=Decimal("40.00"))
        RateioCategoria.objects.create(evento=evento, categoria=outra_categoria, valor=Decimal("60.00"))

        resposta = self.client.get(
            reverse("eventos"),
            {
                "date_from": "01/10/2026",
                "date_to": "31/10/2026",
                "analysis_type": "essentiality",
                "analysis_value": self.categoria.essencialidade,
                "analysis_nature": "all",
                "essentiality": self.categoria.essencialidade,
                "computes": "COMPUTA",
            },
        )

        self.assertEqual(resposta_alheia.status_code, 404)
        self.assertContains(resposta, "Exibindo lançamentos que compõem")
        self.assertContains(resposta, "Valor no indicador")
        self.assertContains(resposta, "R$ 40,00")
        self.assertContains(resposta, 'name="analysis_type" value="essentiality"')
        self.assertContains(resposta, "Ações</th>")
