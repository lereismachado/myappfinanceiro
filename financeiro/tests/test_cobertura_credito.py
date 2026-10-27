from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from financeiro.models import (
    Cartao,
    Categoria,
    CompraCartao,
    Conta,
    EventoFinanceiro,
    Fatura,
    Liquidacao,
    MovimentoConta,
    NaturezaEvento,
    ParcelaCartao,
    RateioCategoria,
    Situacao,
    Titulo,
)
from financeiro.services.dashboard import consultar_dashboard


class CoberturaCreditoTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(username="cobertura", password="senha")
        self.outro_usuario = get_user_model().objects.create_user(username="cobertura-outro", password="senha")
        self.conta = Conta.objects.create(
            usuario=self.usuario,
            nome="Conta principal",
            saldo_inicial=Decimal("0.00"),
            data_saldo_inicial=date(2026, 9, 30),
        )
        self.cartao = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão principal",
            conta_pagamento_padrao=self.conta,
            dia_vencimento=10,
        )
        self.categoria_pessoal = Categoria.objects.create(
            usuario=self.usuario,
            nome="Pessoal",
            natureza=Categoria.Natureza.PESSOAL,
        )
        self.client.force_login(self.usuario)

    def movimento(
        self,
        valor,
        data_movimento,
        *,
        natureza=None,
        categoria=None,
        conta=None,
        descricao="Movimento",
    ):
        valor = Decimal(valor)
        natureza = natureza or (NaturezaEvento.RECEITA if valor > 0 else NaturezaEvento.DESPESA)
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=natureza,
            descricao=descricao,
            data=data_movimento,
            valor=abs(valor),
            situacao=Situacao.REALIZADO,
        )
        if categoria:
            RateioCategoria.objects.create(evento=evento, categoria=categoria, valor=abs(valor))
        return MovimentoConta.objects.create(
            conta=conta or self.conta,
            evento=evento,
            data_efetiva=data_movimento,
            valor=valor,
        )

    def compra(
        self,
        valor_total,
        data_compra,
        *,
        parcelas=1,
        natureza=NaturezaEvento.DESPESA,
        situacao=Situacao.REALIZADO,
        categoria=None,
        cartao=None,
        usuario=None,
        valor_total_estimado=False,
        quantidade_materializada=None,
    ):
        usuario = usuario or self.usuario
        cartao = cartao or self.cartao
        valor_total = Decimal(valor_total)
        evento = EventoFinanceiro.objects.create(
            usuario=usuario,
            natureza=natureza,
            descricao="Compra no cartão",
            data=data_compra,
            valor=abs(valor_total),
            situacao=situacao,
        )
        if categoria:
            RateioCategoria.objects.create(evento=evento, categoria=categoria, valor=abs(valor_total))
        compra = CompraCartao.objects.create(
            usuario=usuario,
            cartao=cartao,
            evento=evento,
            estabelecimento="Compra no cartão",
            data_compra=data_compra,
            valor_total=abs(valor_total),
            valor_total_estimado=valor_total_estimado,
            total_parcelas=parcelas,
        )
        quantidade = parcelas if quantidade_materializada is None else quantidade_materializada
        valor_parcela = (abs(valor_total) / Decimal(parcelas)).quantize(Decimal("0.01"))
        for numero in range(1, quantidade + 1):
            indice_mes = data_compra.month - 1 + numero - 1
            ano = data_compra.year + indice_mes // 12
            mes = indice_mes % 12 + 1
            ciclo = f"{ano:04d}-{mes:02d}"
            fatura, _ = Fatura.objects.get_or_create(usuario=usuario, cartao=cartao, ciclo=ciclo)
            ParcelaCartao.objects.create(
                compra=compra,
                fatura=fatura,
                natureza=natureza,
                descricao="Compra no cartão",
                data_original=data_compra,
                categoria=categoria,
                numero=numero,
                total=parcelas,
                valor=valor_parcela,
                situacao=situacao if numero == 1 else Situacao.PREVISTO,
            )
        return compra

    def test_calcula_cobertura_pela_renda_e_com_saldo_inicial(self):
        self.conta.saldo_inicial = Decimal("2000.00")
        self.conta.save(update_fields=["saldo_inicial"])
        self.movimento("10000.00", date(2026, 10, 1), categoria=self.categoria_pessoal)
        self.movimento("-7000.00", date(2026, 10, 10), categoria=self.categoria_pessoal)
        self.compra("4000.00", date(2026, 10, 20), categoria=self.categoria_pessoal)

        cobertura = consultar_dashboard(self.usuario, periodo="2026-10")["cobertura_credito"]

        self.assertEqual(cobertura["receitas"]["total"], Decimal("10000.00"))
        self.assertEqual(cobertura["saidas"]["total"], Decimal("7000.00"))
        self.assertEqual(cobertura["compras_cartao"]["total"], Decimal("4000.00"))
        self.assertEqual(cobertura["cobertura_renda"], Decimal("-1000.00"))
        self.assertEqual(cobertura["cobertura_com_saldo"], Decimal("1000.00"))
        self.assertEqual(cobertura["estado_renda"]["codigo"], "DEPENDENCIA_FECHAMENTO")
        self.assertEqual(cobertura["estado_com_saldo"]["codigo"], "COBERTO")
        self.assertEqual(cobertura["primeira_data_dependencia_renda"], date(2026, 10, 20))

    def test_compra_parcelada_entra_integralmente_no_mes_da_compra(self):
        self.compra("1000.00", date(2026, 10, 18), parcelas=4, categoria=self.categoria_pessoal)

        outubro = consultar_dashboard(self.usuario, periodo="2026-10")
        novembro = consultar_dashboard(self.usuario, periodo="2026-11")

        self.assertEqual(outubro["cobertura_credito"]["compras_cartao"]["total"], Decimal("1000.00"))
        self.assertEqual(novembro["cobertura_credito"]["compras_cartao"]["total"], Decimal("0.00"))
        self.assertEqual(outubro["resumo"]["despesas"], Decimal("250.00"))
        self.assertEqual(novembro["resumo"]["despesas"], Decimal("250.00"))

    def test_compra_incompleta_usa_total_explicito_e_marca_projecao_estimada(self):
        self.compra(
            "900.00",
            date(2026, 10, 12),
            parcelas=3,
            categoria=self.categoria_pessoal,
            quantidade_materializada=1,
        )
        self.compra(
            "600.00",
            date(2026, 10, 13),
            parcelas=3,
            categoria=self.categoria_pessoal,
            quantidade_materializada=1,
            valor_total_estimado=True,
        )

        compras = consultar_dashboard(self.usuario, periodo="2026-10")["cobertura_credito"]["compras_cartao"]

        self.assertEqual(compras["total"], Decimal("1500.00"))
        self.assertFalse(compras["itens"][0]["estimado"])
        self.assertTrue(compras["itens"][1]["estimado"])

    def test_faturas_paga_parcial_e_pendente_nao_duplicam_obrigacoes(self):
        def criar_titulo(ciclo, vencimento, original, saldo, status, pago):
            fatura = Fatura.objects.create(
                usuario=self.usuario,
                cartao=self.cartao,
                ciclo=ciclo,
            )
            titulo = Titulo.objects.create(
                usuario=self.usuario,
                tipo=Titulo.Tipo.PAGAR,
                descricao=f"Fatura {ciclo}",
                vencimento=vencimento,
                valor_original=Decimal(original),
                saldo_aberto=Decimal(saldo),
                status=status,
                conta=self.conta,
                fatura=fatura,
            )
            if Decimal(pago) > 0:
                movimento = self.movimento(
                    f"-{pago}",
                    vencimento,
                    natureza=NaturezaEvento.PAGAMENTO_FATURA,
                    descricao=f"Pagamento {ciclo}",
                )
                Liquidacao.objects.create(
                    titulo=titulo,
                    movimento_conta=movimento,
                    valor=Decimal(pago),
                    data=vencimento,
                )

        criar_titulo("2026-10", date(2026, 10, 5), "3000.00", "0.00", Titulo.Status.LIQUIDADO, "3000.00")
        criar_titulo("2026-11", date(2026, 10, 10), "3000.00", "1000.00", Titulo.Status.ABERTO, "2000.00")
        criar_titulo("2026-12", date(2026, 10, 15), "3000.00", "3000.00", Titulo.Status.ABERTO, "0.00")

        saidas = consultar_dashboard(self.usuario, periodo="2026-10")["cobertura_credito"]["saidas"]

        self.assertEqual(saidas["realizados"], Decimal("5000.00"))
        self.assertEqual(saidas["previstos"], Decimal("4000.00"))
        self.assertEqual(saidas["total"], Decimal("9000.00"))

    def test_estorno_reduz_cancelamento_nao_entra_e_previsto_fica_separado(self):
        self.compra("1000.00", date(2026, 10, 2), categoria=self.categoria_pessoal)
        self.compra(
            "200.00",
            date(2026, 10, 3),
            categoria=self.categoria_pessoal,
            natureza=NaturezaEvento.ESTORNO,
        )
        self.compra(
            "500.00",
            date(2026, 10, 4),
            categoria=self.categoria_pessoal,
            situacao=Situacao.CANCELADO,
        )
        self.compra(
            "300.00",
            date(2026, 10, 5),
            categoria=self.categoria_pessoal,
            situacao=Situacao.PREVISTO,
        )

        compras = consultar_dashboard(self.usuario, periodo="2026-10")["cobertura_credito"]["compras_cartao"]

        self.assertEqual(compras["realizados"], Decimal("800.00"))
        self.assertEqual(compras["previstos"], Decimal("300.00"))
        self.assertEqual(compras["total"], Decimal("1100.00"))

    def test_parcela_cancelada_nao_faz_compra_voltar_ao_total_original(self):
        compra = self.compra(
            "900.00",
            date(2026, 10, 4),
            parcelas=3,
            categoria=self.categoria_pessoal,
        )
        compra.parcelas.get(numero=2).delete()
        fatura = Fatura.objects.get(usuario=self.usuario, cartao=self.cartao, ciclo="2026-11")
        ParcelaCartao.objects.create(
            compra=compra,
            fatura=fatura,
            natureza=NaturezaEvento.DESPESA,
            descricao="Parcela cancelada",
            data_original=date(2026, 10, 4),
            categoria=self.categoria_pessoal,
            numero=2,
            total=3,
            valor=Decimal("300.00"),
            situacao=Situacao.CANCELADO,
        )

        compras = consultar_dashboard(self.usuario, periodo="2026-10")["cobertura_credito"]["compras_cartao"]

        self.assertEqual(compras["total"], Decimal("600.00"))

    def test_identifica_dependencia_temporaria_apos_agregar_o_dia(self):
        self.compra("1000.00", date(2026, 10, 5), categoria=self.categoria_pessoal)
        self.movimento("2000.00", date(2026, 10, 10), categoria=self.categoria_pessoal)

        cobertura = consultar_dashboard(self.usuario, periodo="2026-10")["cobertura_credito"]

        self.assertEqual(cobertura["cobertura_renda"], Decimal("1000.00"))
        self.assertEqual(cobertura["primeira_data_dependencia_renda"], date(2026, 10, 5))
        self.assertEqual(cobertura["maior_deficit_renda"], Decimal("1000.00"))
        self.assertEqual(cobertura["estado_renda"]["codigo"], "DEPENDENCIA_TEMPORARIA")

    def test_agrega_movimentos_do_mesmo_dia_e_exclui_transferencias(self):
        self.compra("1000.00", date(2026, 10, 5), categoria=self.categoria_pessoal)
        self.movimento("1000.00", date(2026, 10, 5), categoria=self.categoria_pessoal)
        self.movimento(
            "-500.00",
            date(2026, 10, 6),
            natureza=NaturezaEvento.TRANSFERENCIA,
            descricao="Transferência própria",
        )

        cobertura = consultar_dashboard(self.usuario, periodo="2026-10")["cobertura_credito"]

        self.assertEqual(cobertura["cobertura_renda"], Decimal("0.00"))
        self.assertIsNone(cobertura["primeira_data_dependencia_renda"])
        self.assertEqual(cobertura["saidas"]["total"], Decimal("0.00"))
        self.assertEqual(len(cobertura["evolucao_diaria"]), 1)

    def test_ignora_origem_e_respeita_cartao_natureza_e_usuario(self):
        conta_secundaria = Conta.objects.create(usuario=self.usuario, nome="Outra conta")
        cartao_secundario = Cartao.objects.create(
            usuario=self.usuario,
            nome="Outro cartão",
            conta_pagamento_padrao=conta_secundaria,
            dia_vencimento=12,
        )
        categoria_empresarial = Categoria.objects.create(
            usuario=self.usuario,
            nome="Empresarial",
            natureza=Categoria.Natureza.EMPRESARIAL,
        )
        self.compra("200.00", date(2026, 10, 6), categoria=self.categoria_pessoal)
        self.compra(
            "500.00",
            date(2026, 10, 7),
            categoria=categoria_empresarial,
            cartao=cartao_secundario,
        )
        conta_alheia = Conta.objects.create(usuario=self.outro_usuario, nome="Conta alheia")
        cartao_alheio = Cartao.objects.create(
            usuario=self.outro_usuario,
            nome="Cartão alheio",
            conta_pagamento_padrao=conta_alheia,
            dia_vencimento=9,
        )
        self.compra(
            "900.00",
            date(2026, 10, 8),
            cartao=cartao_alheio,
            usuario=self.outro_usuario,
        )

        conta = consultar_dashboard(self.usuario, periodo="2026-10", origem="account")
        cartao = consultar_dashboard(self.usuario, periodo="2026-10", origem="card")
        pessoal = consultar_dashboard(self.usuario, periodo="2026-10", natureza="personal")
        filtrado = consultar_dashboard(self.usuario, periodo="2026-10", cartao_id=self.cartao.pk)

        self.assertEqual(conta["cobertura_credito"]["compras_cartao"]["total"], Decimal("700.00"))
        self.assertEqual(cartao["cobertura_credito"]["compras_cartao"]["total"], Decimal("700.00"))
        self.assertEqual(pessoal["cobertura_credito"]["compras_cartao"]["total"], Decimal("200.00"))
        self.assertEqual(filtrado["cobertura_credito"]["compras_cartao"]["total"], Decimal("200.00"))

    def test_dashboard_renderiza_painel_recolhido_tooltip_e_detalhamento(self):
        self.movimento("1000.00", date(2026, 10, 1), categoria=self.categoria_pessoal)
        self.compra("200.00", date(2026, 10, 6), categoria=self.categoria_pessoal)

        resposta = self.client.get(reverse("dashboard"), {"period": "2026-10"})
        conteudo = resposta.content.decode()

        inicio_comparativo = conteudo.index('id="comparisonTitle"')
        inicio_cobertura = conteudo.index('id="coverageTitle"')
        inicio_simulador = conteudo.index('id="simulatorTitle"')
        self.assertLess(inicio_comparativo, inicio_cobertura)
        self.assertLess(inicio_cobertura, inicio_simulador)
        self.assertContains(
            resposta,
            '<details class="panel full overview-section collapsible-panel comparison-panel"',
        )
        self.assertNotContains(
            resposta,
            'class="panel full overview-section collapsible-panel comparison-panel" open',
        )
        self.assertContains(
            resposta,
            '<details class="panel full overview-section collapsible-panel coverage-panel"',
        )
        self.assertNotContains(
            resposta,
            'class="panel full overview-section collapsible-panel coverage-panel" open',
        )
        self.assertContains(resposta, 'aria-label="Ver regras financeiras do indicador"')
        self.assertContains(resposta, 'aria-controls="coverageRulesTooltip"')
        self.assertContains(resposta, 'aria-expanded="false"')
        self.assertContains(resposta, 'id="coverageRulesTooltip" role="tooltip"')
        self.assertContains(resposta, "Pagamentos de faturas representam compromissos anteriores")
        self.assertContains(resposta, "event.preventDefault()")
        self.assertContains(resposta, "event.stopPropagation()")
        self.assertContains(resposta, "event.key !== 'Escape'")
        self.assertContains(resposta, 'type=credit_coverage')
        self.assertIn(".coverage-panel{margin-top:28px}", conteudo)

        detalhe = self.client.get(
            reverse("dashboard_detalhamento"),
            {"type": "credit_coverage", "period": "2026-10", "dashboard_context": "1"},
        )
        self.assertEqual(detalhe.status_code, 200)
        self.assertContains(detalhe, "Composição e evolução diária")
        self.assertContains(detalhe, "Saídas e obrigações da conta")
        self.assertContains(detalhe, "Compras no cartão pela data da compra")
        self.assertContains(detalhe, "Evolução diária contrafactual")
        self.assertContains(detalhe, "Compra no cartão")

    def test_detalhamento_exige_autenticacao_e_isola_usuario(self):
        self.compra("200.00", date(2026, 10, 6), categoria=self.categoria_pessoal)
        conta_alheia = Conta.objects.create(usuario=self.outro_usuario, nome="Conta alheia")
        cartao_alheio = Cartao.objects.create(
            usuario=self.outro_usuario,
            nome="Cartão alheio",
            conta_pagamento_padrao=conta_alheia,
            dia_vencimento=9,
        )
        self.compra(
            "900.00",
            date(2026, 10, 8),
            cartao=cartao_alheio,
            usuario=self.outro_usuario,
        )

        resposta = self.client.get(
            reverse("dashboard_detalhamento"),
            {"type": "credit_coverage", "period": "2026-10", "dashboard_context": "1"},
        )
        self.assertContains(resposta, "R$ 200,00")
        self.assertNotContains(resposta, "R$ 900,00")

        self.client.logout()
        anonima = self.client.get(
            reverse("dashboard_detalhamento"),
            {"type": "credit_coverage", "period": "2026-10"},
        )
        self.assertEqual(anonima.status_code, 302)
