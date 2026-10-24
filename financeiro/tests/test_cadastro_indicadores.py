from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from financeiro.models import (
    Cartao,
    CompraCartao,
    Conta,
    EventoFinanceiro,
    Fatura,
    MovimentoConta,
    NaturezaEvento,
    ParcelaCartao,
    Situacao,
    Titulo,
)


class CadastroIndicadoresTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(username="cadastros", password="senha")
        self.outro_usuario = get_user_model().objects.create_user(username="outro-cadastros", password="senha")
        self.client.force_login(self.usuario)

    def criar_movimento(self, conta, valor, data_movimento=date(2026, 10, 10), usuario=None):
        usuario = usuario or self.usuario
        valor = Decimal(valor)
        evento = EventoFinanceiro.objects.create(
            usuario=usuario,
            natureza=NaturezaEvento.RECEITA if valor >= 0 else NaturezaEvento.DESPESA,
            descricao="Movimento realizado",
            data=data_movimento,
            valor=abs(valor),
            situacao=Situacao.REALIZADO,
        )
        return MovimentoConta.objects.create(
            conta=conta,
            evento=evento,
            data_efetiva=data_movimento,
            valor=valor,
        )

    def criar_parcela(self, cartao, valor, usuario=None, ciclo="2026-10"):
        usuario = usuario or self.usuario
        valor = Decimal(valor)
        evento = EventoFinanceiro.objects.create(
            usuario=usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao=f"Compra {cartao.nome}",
            data=date(2026, 10, 5),
            valor=valor,
            situacao=Situacao.REALIZADO,
        )
        compra = CompraCartao.objects.create(
            usuario=usuario,
            cartao=cartao,
            evento=evento,
            estabelecimento="Estabelecimento",
            data_compra=date(2026, 10, 5),
            valor_total=valor,
            total_parcelas=1,
        )
        fatura = Fatura.objects.create(usuario=usuario, cartao=cartao, ciclo=ciclo)
        return ParcelaCartao.objects.create(
            compra=compra,
            fatura=fatura,
            natureza=NaturezaEvento.DESPESA,
            descricao=evento.descricao,
            data_original=evento.data,
            numero=1,
            total=1,
            valor=valor,
            situacao=Situacao.REALIZADO,
        )

    @patch("financeiro.views.timezone.localdate", return_value=date(2026, 10, 15))
    def test_lista_contas_exibe_saldo_atual_e_projetado_ate_fim_do_mes(self, _localdate):
        conta_positiva = Conta.objects.create(
            usuario=self.usuario,
            nome="Conta corrente positiva",
            tipo=Conta.Tipo.CORRENTE,
            saldo_inicial=Decimal("100.00"),
            data_saldo_inicial=date(2026, 10, 1),
        )
        Conta.objects.create(
            usuario=self.usuario,
            nome="Conta poupança zerada",
            tipo=Conta.Tipo.POUPANCA,
            saldo_inicial=Decimal("0.00"),
            data_saldo_inicial=date(2026, 10, 1),
        )
        Conta.objects.create(
            usuario=self.usuario,
            nome="Carteira negativa",
            tipo=Conta.Tipo.CARTEIRA,
            saldo_inicial=Decimal("-10.00"),
            data_saldo_inicial=date(2026, 10, 1),
            ativa=False,
        )
        conta_alheia = Conta.objects.create(
            usuario=self.outro_usuario,
            nome="Conta alheia",
            saldo_inicial=Decimal("999.00"),
            data_saldo_inicial=date(2026, 10, 1),
        )
        self.criar_movimento(conta_positiva, "-20.00")
        self.criar_movimento(conta_alheia, "50.00", usuario=self.outro_usuario)
        Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao="Despesa deste mês",
            vencimento=date(2026, 10, 20),
            valor_original=Decimal("30.00"),
            saldo_aberto=Decimal("30.00"),
            conta=conta_positiva,
        )
        Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao="Despesa do próximo mês",
            vencimento=date(2026, 11, 1),
            valor_original=Decimal("200.00"),
            saldo_aberto=Decimal("200.00"),
            conta=conta_positiva,
        )

        resposta = self.client.get(reverse("contas"))

        self.assertEqual(resposta.status_code, 200)
        contas = {conta.nome: conta for conta in resposta.context["objetos"]}
        self.assertEqual(contas["Conta corrente positiva"].indicador_saldo["saldo_atual"], Decimal("80.00"))
        self.assertEqual(contas["Conta corrente positiva"].indicador_saldo["saldo_projetado"], Decimal("50.00"))
        self.assertEqual(contas["Conta poupança zerada"].indicador_saldo["saldo_atual"], Decimal("0.00"))
        self.assertEqual(contas["Carteira negativa"].indicador_saldo["saldo_projetado"], Decimal("-10.00"))
        self.assertContains(resposta, "<th>Saldo inicial</th><th>Saldo</th><th>Situação</th>", html=False)
        self.assertContains(resposta, 'Atual: <span class="amount-positive">R$ 0,00</span>', html=False)
        self.assertContains(resposta, 'Atual: <span class="amount-negative">-R$ 10,00</span>', html=False)
        self.assertContains(resposta, 'Projetado: <span class="amount-positive">R$ 50,00</span>', html=False)
        self.assertNotContains(resposta, "Conta alheia")

    @patch("financeiro.views.timezone.localdate", return_value=date(2026, 10, 15))
    def test_lista_contas_preserva_busca_e_filtro_de_situacao(self, _localdate):
        Conta.objects.create(usuario=self.usuario, nome="Conta ativa")
        Conta.objects.create(
            usuario=self.usuario,
            nome="Carteira negativa filtrada",
            tipo=Conta.Tipo.CARTEIRA,
            saldo_inicial=Decimal("-5.00"),
            data_saldo_inicial=date(2026, 10, 1),
            ativa=False,
        )

        resposta = self.client.get(reverse("contas"), {"q": "negativa", "status": "inactive"})

        self.assertEqual([conta.nome for conta in resposta.context["objetos"]], ["Carteira negativa filtrada"])
        self.assertContains(resposta, "-R$ 5,00")
        self.assertNotContains(resposta, "Conta ativa")

    @patch("financeiro.views.timezone.localdate", return_value=date(2026, 10, 15))
    def test_lista_cartoes_exibe_disponivel_comprometido_e_faixas(self, _localdate):
        conta = Conta.objects.create(usuario=self.usuario, nome="Conta de pagamento")
        cartoes = {
            "Azul": Cartao.objects.create(
                usuario=self.usuario, nome="Cartão Azul", dia_vencimento=10, limite=Decimal("100.00")
            ),
            "Laranja": Cartao.objects.create(
                usuario=self.usuario, nome="Cartão Laranja", dia_vencimento=10, limite=Decimal("100.00")
            ),
            "Vermelho": Cartao.objects.create(
                usuario=self.usuario, nome="Cartão Vermelho", dia_vencimento=10, limite=Decimal("100.00"), ativo=False
            ),
            "Sem limite": Cartao.objects.create(
                usuario=self.usuario, nome="Cartão sem limite", dia_vencimento=10, limite=Decimal("0.00")
            ),
        }
        for cartao in cartoes.values():
            cartao.conta_pagamento_padrao = conta
            cartao.save(update_fields=["conta_pagamento_padrao"])
        self.criar_parcela(cartoes["Azul"], "80.00")
        self.criar_parcela(cartoes["Laranja"], "85.00")
        self.criar_parcela(cartoes["Vermelho"], "125.00")
        cartao_alheio = Cartao.objects.create(
            usuario=self.outro_usuario,
            nome="Cartão alheio",
            dia_vencimento=10,
            limite=Decimal("100.00"),
        )
        self.criar_parcela(cartao_alheio, "50.00", usuario=self.outro_usuario)

        resposta = self.client.get(reverse("cartoes"))

        self.assertEqual(resposta.status_code, 200)
        indicadores = {cartao.nome: cartao.indicador_limite for cartao in resposta.context["objetos"]}
        self.assertEqual(indicadores["Cartão Azul"]["disponivel"], Decimal("20.00"))
        self.assertEqual(indicadores["Cartão Azul"]["faixa_percentual"], "blue")
        self.assertEqual(indicadores["Cartão Laranja"]["percentual"], Decimal("85.00"))
        self.assertEqual(indicadores["Cartão Laranja"]["faixa_percentual"], "orange")
        self.assertEqual(indicadores["Cartão Vermelho"]["disponivel"], Decimal("-25.00"))
        self.assertEqual(indicadores["Cartão Vermelho"]["percentual_css"], "100.00")
        self.assertEqual(indicadores["Cartão Vermelho"]["faixa_percentual"], "red")
        self.assertFalse(indicadores["Cartão sem limite"]["limite_informado"])
        self.assertContains(
            resposta,
            "<th>Saldo disponível</th><th>Comprometido</th><th>Situação</th>",
            html=False,
        )
        self.assertContains(resposta, 'class="credit-progress-cell credit-progress-blue"', count=1, html=False)
        self.assertContains(resposta, 'class="credit-progress-cell credit-progress-orange"', count=1, html=False)
        self.assertContains(resposta, 'class="credit-progress-cell credit-progress-red"', count=1, html=False)
        self.assertContains(resposta, 'style="width:100.00%"', count=1, html=False)
        self.assertContains(resposta, 'aria-valuetext="125,00% comprometido, no valor de R$ 125,00"', html=False)
        self.assertContains(resposta, '<span class="amount-negative">-R$ 25,00</span>', html=False)
        self.assertContains(resposta, "Limite não informado", count=1)
        self.assertNotContains(resposta, "Cartão alheio")

    def test_estados_vazios_usam_quantidade_correta_de_colunas(self):
        resposta_contas = self.client.get(reverse("contas"))
        resposta_cartoes = self.client.get(reverse("cartoes"))

        self.assertContains(resposta_contas, '<td colspan="6" class="muted">Nenhum cadastro encontrado.</td>', html=False)
        self.assertContains(resposta_cartoes, '<td colspan="7" class="muted">Nenhum cadastro encontrado.</td>', html=False)
