from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from financeiro.forms import CompraCartaoForm, LancamentoForm
from financeiro.models import (
    Ambito,
    Cartao,
    Categoria,
    Conta,
    EventoFinanceiro,
    Fatura,
    MovimentoConta,
    NaturezaEvento,
    ParcelaCartao,
    Pessoa,
    RateioCategoria,
    SerieLancamentoRecorrente,
    Situacao,
    Titulo,
)
from financeiro.services.lancamentos import (
    _totalizar_linhas,
    consultar_lancamentos,
    criar_lancamento,
    editar_lancamento_conta,
    excluir_parcela_lancamento,
    excluir_lancamento_conta,
    replicar_edicao_recorrencia,
)


class LancamentosServiceTests(TestCase):
    def setUp(self):
        localdate_patcher = patch(
            "financeiro.services.lancamentos.timezone.localdate",
            return_value=date(2026, 10, 14),
        )
        localdate_patcher.start()
        self.addCleanup(localdate_patcher.stop)
        self.usuario = get_user_model().objects.create_user(username="titular", password="senha")
        self.outro = get_user_model().objects.create_user(username="outro", password="senha")
        self.conta = Conta.objects.create(usuario=self.usuario, nome="Conta principal")
        self.conta_inativa = Conta.objects.create(usuario=self.usuario, nome="Conta histórica", ativa=False)
        self.cartao = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão principal",
            conta_pagamento_padrao=self.conta,
            dia_vencimento=10,
        )
        self.cartao_inativo = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão histórico",
            conta_pagamento_padrao=self.conta_inativa,
            dia_vencimento=10,
            ativo=False,
        )
        self.categoria = Categoria.objects.create(usuario=self.usuario, nome="Mercado")
        self.pessoa = Pessoa.objects.create(usuario=self.usuario, nome="Fornecedor")

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
            "pessoa": self.pessoa,
            "pessoa_nome_livre": "",
            "ambito": Ambito.PROFISSIONAL,
            "situacao": Situacao.REALIZADO,
            "vencimento": None,
            "ciclo": "",
            "quantidade_parcelas": 1,
        }
        dados.update(alteracoes)
        return dados

    def test_recorrencia_de_conta_cria_ocorrencias_vinculadas_e_previstas(self):
        primeiro = criar_lancamento(
            self.usuario,
            self.dados(
                data=date(2027, 1, 31),
                situacao=Situacao.REALIZADO,
                vencimento=date(2027, 2, 28),
                modo_lancamento="RECORRENTE",
                quantidade_repeticoes=3,
                intervalo_meses=1,
            ),
        )

        serie = primeiro.serie_recorrente
        ocorrencias = list(serie.eventos.order_by("numero_recorrencia"))
        self.assertEqual(serie.quantidade_ocorrencias, 3)
        self.assertEqual([evento.numero_recorrencia for evento in ocorrencias], [1, 2, 3])
        self.assertEqual([evento.data for evento in ocorrencias], [date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31)])
        self.assertEqual([evento.vencimento for evento in ocorrencias], [date(2027, 2, 28), date(2027, 3, 28), date(2027, 4, 28)])
        self.assertEqual([evento.situacao for evento in ocorrencias], [Situacao.REALIZADO, Situacao.PREVISTO, Situacao.PREVISTO])
        self.assertEqual([evento.valor for evento in ocorrencias], [Decimal("100.00")] * 3)
        self.assertEqual(Titulo.objects.filter(evento__serie_recorrente=serie).count(), 2)

    def test_recorrencia_de_cartao_mantem_valor_integral_em_cada_ciclo(self):
        primeiro = criar_lancamento(
            self.usuario,
            self.dados(
                origem="CARTAO",
                conta=None,
                cartao=self.cartao,
                data=date(2027, 1, 1),
                ciclo="2027-01",
                valor=Decimal("120.00"),
                modo_lancamento="RECORRENTE",
                quantidade_repeticoes=3,
                intervalo_meses=1,
            ),
        )

        ocorrencias = list(primeiro.serie_recorrente.eventos.order_by("numero_recorrencia"))
        parcelas = [evento.compras_cartao.get().parcelas.get() for evento in ocorrencias]
        self.assertEqual([parcela.valor for parcela in parcelas], [Decimal("120.00")] * 3)
        self.assertEqual([parcela.fatura.ciclo for parcela in parcelas], ["2027-01", "2027-02", "2027-03"])
        self.assertEqual([parcela.situacao for parcela in parcelas], [Situacao.REALIZADO, Situacao.PREVISTO, Situacao.PREVISTO])

    def test_replica_edicao_somente_para_futuros_previsto_sem_mudar_agenda(self):
        primeiro = criar_lancamento(
            self.usuario,
            self.dados(
                data=date(2027, 1, 10),
                situacao=Situacao.REALIZADO,
                vencimento=date(2027, 1, 15),
                modo_lancamento="RECORRENTE",
                quantidade_repeticoes=3,
                intervalo_meses=1,
            ),
        )
        futuros = list(primeiro.serie_recorrente.eventos.order_by("numero_recorrencia"))[1:]
        agenda = [(evento.data, evento.vencimento, evento.situacao) for evento in futuros]
        dados = self.dados(
            descricao="Assinatura atualizada",
            valor=Decimal("125.00"),
            data=primeiro.data,
            vencimento=primeiro.vencimento,
        )

        editar_lancamento_conta(self.usuario, primeiro.pk, dados)
        self.assertEqual(replicar_edicao_recorrencia(self.usuario, primeiro, dados), 2)

        for evento, esperado in zip(futuros, agenda):
            evento.refresh_from_db()
            self.assertEqual(evento.descricao, "Assinatura atualizada")
            self.assertEqual(evento.valor, Decimal("125.00"))
            self.assertEqual((evento.data, evento.vencimento, evento.situacao), esperado)

    def test_exclusao_de_recorrencia_preserva_futuro_realizado(self):
        primeiro = criar_lancamento(
            self.usuario,
            self.dados(
                modo_lancamento="RECORRENTE",
                quantidade_repeticoes=3,
                intervalo_meses=1,
                situacao=Situacao.REALIZADO,
                vencimento=date(2026, 10, 15),
            ),
        )
        segundo, terceiro = list(primeiro.serie_recorrente.eventos.order_by("numero_recorrencia"))[1:]
        terceiro.situacao = Situacao.REALIZADO
        terceiro.save(update_fields=["situacao"])

        self.assertEqual(excluir_lancamento_conta(self.usuario, primeiro.pk, excluir_futuros=True), 1)
        self.assertFalse(EventoFinanceiro.objects.filter(pk=segundo.pk).exists())
        self.assertTrue(EventoFinanceiro.objects.filter(pk=terceiro.pk).exists())

    def test_periodo_usa_ciclo_e_intervalo_usa_vencimento_do_cartao(self):
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Movimento efetivado em outubro",
            data=date(2026, 9, 28),
            valor=Decimal("20.00"),
            situacao=Situacao.REALIZADO,
        )
        MovimentoConta.objects.create(
            conta=self.conta,
            evento=evento,
            data_efetiva=date(2026, 10, 2),
            valor=Decimal("-20.00"),
        )
        criar_lancamento(
            self.usuario,
            self.dados(
                descricao="Previsão de outubro",
                data=date(2026, 9, 15),
                situacao=Situacao.PREVISTO,
                vencimento=date(2026, 10, 20),
            ),
        )
        evento_cartao = criar_lancamento(
            self.usuario,
            self.dados(
                descricao="Compra feita em setembro",
                data=date(2026, 9, 10),
                origem="CARTAO",
                conta=None,
                cartao=self.cartao,
                ciclo="2026-10",
            ),
        )

        consulta = consultar_lancamentos(self.usuario, periodo="2026-10")

        self.assertEqual([linha.descricao for linha in consulta["linhas"]], ["Compra feita em setembro"])

        consulta_setembro = consultar_lancamentos(
            self.usuario,
            periodo="2026-10",
            data_inicial="01/09/2026",
            data_final="30/09/2026",
        )
        self.assertEqual(consulta_setembro["linhas"], [])

        consulta_setembro_por_compra = consultar_lancamentos(
            self.usuario,
            periodo="2026-10",
            data_inicial="01/09/2026",
            data_final="30/09/2026",
            card_purchase_date="1",
        )
        self.assertEqual(
            [linha.descricao for linha in consulta_setembro_por_compra["linhas"]],
            ["Compra feita em setembro"],
        )

        evento_cartao.compras_cartao.get().parcelas.update(data_original=None)
        consulta_setembro_com_fallback = consultar_lancamentos(
            self.usuario,
            periodo="2026-10",
            data_inicial="01/09/2026",
            data_final="30/09/2026",
            card_purchase_date="1",
        )
        self.assertEqual(
            [linha.descricao for linha in consulta_setembro_com_fallback["linhas"]],
            ["Compra feita em setembro"],
        )

        consulta_sem_ciclo = consultar_lancamentos(
            self.usuario,
            periodo="",
            data_inicial="01/09/2026",
            data_final="30/09/2026",
        )
        self.assertEqual(
            {linha.descricao for linha in consulta_sem_ciclo["linhas"]},
            {"Movimento efetivado em outubro", "Previsão de outubro"},
        )

        consulta_outubro = consultar_lancamentos(
            self.usuario,
            periodo="",
            data_inicial="01/10/2026",
            data_final="31/10/2026",
        )
        self.assertEqual([linha.descricao for linha in consulta_outubro["linhas"]], ["Compra feita em setembro"])

    def test_ordenacao_usa_data_original_crescente_em_conta_e_cartao(self):
        criar_lancamento(
            self.usuario,
            self.dados(
                descricao="Lançamento mais antigo da conta",
                data=date(2026, 10, 20),
            ),
        )
        criar_lancamento(
            self.usuario,
            self.dados(
                descricao="Compra mais recente com vencimento posterior",
                data=date(2026, 11, 5),
                origem="CARTAO",
                conta=None,
                cartao=self.cartao,
                ciclo="2026-12",
            ),
        )

        consulta = consultar_lancamentos(
            self.usuario,
            data_inicial="01/10/2026",
            data_final="31/12/2026",
        )

        self.assertEqual(
            [linha.descricao for linha in consulta["linhas"]],
            [
                "Lançamento mais antigo da conta",
                "Compra mais recente com vencimento posterior",
            ],
        )

    def test_ordenacao_desempata_mesma_data_pelo_id_do_lancamento_crescente(self):
        primeiro = criar_lancamento(
            self.usuario,
            self.dados(descricao="Primeiro ID", data=date(2026, 10, 10)),
        )
        segundo = criar_lancamento(
            self.usuario,
            self.dados(descricao="Segundo ID", data=date(2026, 10, 10)),
        )

        consulta = consultar_lancamentos(self.usuario, periodo="2026-10", origem="CONTA")

        self.assertLess(primeiro.pk, segundo.pk)
        self.assertEqual(
            [(linha.evento_id, linha.descricao) for linha in consulta["linhas"]],
            [
                (primeiro.pk, "Primeiro ID"),
                (segundo.pk, "Segundo ID"),
            ],
        )

    def test_referencia_da_fatura_respeita_precedencia_e_calculo_do_ciclo(self):
        self.cartao.dia_vencimento = 11
        self.cartao.save(update_fields=["dia_vencimento"])
        evento = criar_lancamento(
            self.usuario,
            self.dados(
                origem="CARTAO",
                conta=None,
                cartao=self.cartao,
                ciclo="2026-10",
                quantidade_parcelas=2,
            ),
        )
        parcelas = list(evento.compras_cartao.get().parcelas.select_related("fatura__cartao").order_by("numero"))

        primeira, segunda = parcelas
        segunda.fatura.vencimento = date(2026, 11, 10)
        segunda.fatura.save(update_fields=["vencimento"])
        segunda.vencimento = None
        segunda.save(update_fields=["vencimento"])

        primeira.vencimento = date(2026, 10, 9)
        with self.assertRaises(ValidationError):
            primeira.save(update_fields=["vencimento"])
        primeira.refresh_from_db()

        linhas = consultar_lancamentos(
            self.usuario,
            origem="CARTAO",
            data_inicial="01/10/2026",
            data_final="30/11/2026",
        )["linhas"]
        referencias = {linha.parcela_numero: linha.vencimento_fatura for linha in linhas}
        self.assertEqual(referencias, {1: date(2026, 10, 11), 2: date(2026, 11, 10)})

        segunda.fatura.vencimento = None
        segunda.fatura.save(update_fields=["vencimento"])
        segunda.vencimento = None
        segunda.save(update_fields=["vencimento"])

        linhas = consultar_lancamentos(
            self.usuario,
            origem="CARTAO",
            data_inicial="01/10/2026",
            data_final="30/11/2026",
        )["linhas"]
        referencias = {linha.parcela_numero: linha.vencimento_fatura for linha in linhas}
        self.assertEqual(referencias, {1: date(2026, 10, 11), 2: date(2026, 11, 11)})

    def test_referencia_da_fatura_ajusta_fim_do_mes(self):
        self.cartao.dia_vencimento = 31
        self.cartao.save(update_fields=["dia_vencimento"])
        criar_lancamento(
            self.usuario,
            self.dados(origem="CARTAO", conta=None, cartao=self.cartao, ciclo="2027-02"),
        )

        linha = consultar_lancamentos(
            self.usuario,
            periodo="2027-02",
            origem="CARTAO",
            data_inicial="01/02/2027",
            data_final="28/02/2027",
        )["linhas"][0]
        self.assertEqual(linha.vencimento_fatura, date(2027, 2, 28))
        self.assertEqual(linha.ciclo_fatura, "02/2027")

        criar_lancamento(self.usuario, self.dados(descricao="Movimento sem fatura"))
        linha_conta = consultar_lancamentos(self.usuario, periodo="2026-10", origem="CONTA")["linhas"][0]
        self.assertIsNone(linha_conta.vencimento_fatura)
        self.assertEqual(linha_conta.ciclo_fatura, "")

    def test_intervalo_e_inclusivo(self):
        criar_lancamento(self.usuario, self.dados(descricao="Primeiro dia", data=date(2026, 10, 1)))
        criar_lancamento(self.usuario, self.dados(descricao="Último dia", data=date(2026, 10, 31)))
        criar_lancamento(self.usuario, self.dados(descricao="Fora do período", data=date(2026, 11, 1)))

        consulta = consultar_lancamentos(
            self.usuario,
            periodo="2026-10",
            data_inicial="2026-10-01",
            data_final="2026-10-31",
        )

        self.assertEqual({linha.descricao for linha in consulta["linhas"]}, {"Primeiro dia", "Último dia"})

    def test_intervalo_padrao_usa_mes_atual_e_preserva_periodo_informado(self):
        criar_lancamento(self.usuario, self.dados(descricao="Outubro", data=date(2026, 10, 20)))
        criar_lancamento(self.usuario, self.dados(descricao="Novembro", data=date(2026, 11, 20)))

        consulta_padrao = consultar_lancamentos(self.usuario)
        consulta_novembro = consultar_lancamentos(
            self.usuario,
            data_inicial="01/11/2026",
            data_final="30/11/2026",
        )

        self.assertEqual(consulta_padrao["data_inicial"], date(2026, 10, 1))
        self.assertEqual(consulta_padrao["data_final"], date(2026, 10, 31))
        self.assertEqual([linha.descricao for linha in consulta_padrao["linhas"]], ["Outubro"])
        self.assertEqual(consulta_novembro["data_inicial"], date(2026, 11, 1))
        self.assertEqual(consulta_novembro["data_final"], date(2026, 11, 30))
        self.assertEqual([linha.descricao for linha in consulta_novembro["linhas"]], ["Novembro"])

    def test_filtros_de_categoria_hierarquia_classificacao_natureza_e_pessoa(self):
        categoria_pai = Categoria.objects.create(usuario=self.usuario, nome="Receitas")
        categoria_filha = Categoria.objects.create(
            usuario=self.usuario,
            nome="Serviços",
            categoria_pai=categoria_pai,
            natureza=Categoria.Natureza.EMPRESARIAL,
            essencialidade=Categoria.Essencialidade.PROFISSIONAL,
            computa_resultado=False,
        )
        categoria_neta = Categoria.objects.create(
            usuario=self.usuario,
            nome="Consultoria",
            categoria_pai=categoria_filha,
            natureza=Categoria.Natureza.EMPRESARIAL,
            essencialidade=Categoria.Essencialidade.PROFISSIONAL,
            computa_resultado=False,
        )
        evento = criar_lancamento(
            self.usuario,
            self.dados(
                descricao="Consultoria empresarial",
                natureza=NaturezaEvento.RECEITA,
                categoria=categoria_neta,
                pessoa=self.pessoa,
            ),
        )
        RateioCategoria.objects.create(evento=evento, categoria=self.categoria, valor=Decimal("1.00"))
        criar_lancamento(self.usuario, self.dados(descricao="Despesa sem correspondência"))

        consulta = consultar_lancamentos(
            self.usuario,
            periodo="2026-10",
            categoria_superior=categoria_pai.pk,
            natureza=NaturezaEvento.RECEITA,
            natureza_categoria=Categoria.Natureza.EMPRESARIAL,
            essencialidade=Categoria.Essencialidade.PROFISSIONAL,
            computa_resultado="NAO_COMPUTA",
            pessoa=self.pessoa.pk,
        )

        self.assertEqual([linha.descricao for linha in consulta["linhas"]], ["Consultoria empresarial"])

    def test_listas_dos_filtros_de_categoria_separam_raizes_e_descendentes(self):
        categoria_pai = Categoria.objects.create(usuario=self.usuario, nome="Receitas")
        categoria_filha = Categoria.objects.create(
            usuario=self.usuario,
            nome="Serviços",
            categoria_pai=categoria_pai,
        )
        categoria_neta = Categoria.objects.create(
            usuario=self.usuario,
            nome="Consultoria",
            categoria_pai=categoria_filha,
        )
        categoria_outro_usuario = Categoria.objects.create(
            usuario=self.outro,
            nome="Categoria externa",
        )

        consulta = consultar_lancamentos(self.usuario, periodo="2026-10")

        self.assertEqual(
            {categoria.pk for categoria in consulta["categorias_superiores"]},
            {self.categoria.pk, categoria_pai.pk},
        )
        self.assertEqual(
            {categoria.pk for categoria in consulta["categorias"]},
            {categoria_filha.pk, categoria_neta.pk},
        )
        self.assertNotIn(
            categoria_outro_usuario.pk,
            {
                categoria.pk
                for categoria in consulta["categorias_superiores"] + consulta["categorias"]
            },
        )

    def test_filtros_de_categoria_devem_coincidir_na_mesma_categoria(self):
        categoria_empresarial = Categoria.objects.create(
            usuario=self.usuario,
            nome="Empresarial computável",
            natureza=Categoria.Natureza.EMPRESARIAL,
            computa_resultado=True,
        )
        categoria_pessoal = Categoria.objects.create(
            usuario=self.usuario,
            nome="Pessoal não computável",
            natureza=Categoria.Natureza.PESSOAL,
            computa_resultado=False,
        )
        evento = criar_lancamento(
            self.usuario,
            self.dados(descricao="Rateio misto", categoria=categoria_empresarial),
        )
        RateioCategoria.objects.create(evento=evento, categoria=categoria_pessoal, valor=Decimal("1.00"))

        consulta = consultar_lancamentos(
            self.usuario,
            periodo="2026-10",
            natureza_categoria=Categoria.Natureza.EMPRESARIAL,
            computa_resultado="NAO_COMPUTA",
        )

        self.assertEqual(consulta["linhas"], [])

    def test_cartao_mostra_itens_e_somente_pagamentos_realmente_vinculados(self):
        criar_lancamento(
            self.usuario,
            self.dados(descricao="Despesa direta da conta"),
        )
        criar_lancamento(
            self.usuario,
            self.dados(
                descricao="Previsão direta da conta",
                situacao=Situacao.PREVISTO,
                vencimento=date(2026, 10, 25),
            ),
        )
        compra = criar_lancamento(
            self.usuario,
            self.dados(origem="CARTAO", conta=None, cartao=self.cartao, ciclo="2026-10"),
        )
        fatura = Fatura.objects.get(cartao=self.cartao, ciclo="2026-10")
        fatura.status = Fatura.Status.FECHADA
        fatura.total = Decimal("100.00")
        fatura.total_exigido = Decimal("100.00")
        fatura.vencimento = date(2026, 10, 20)
        fatura.save()
        Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao="Fatura outubro",
            vencimento=fatura.vencimento,
            valor_original=Decimal("100.00"),
            saldo_aberto=Decimal("100.00"),
            conta=self.conta,
            fatura=fatura,
        )
        criar_lancamento(
            self.usuario,
            self.dados(
                descricao="Pagamento confirmado",
                natureza=NaturezaEvento.PAGAMENTO_FATURA,
                cartao=self.cartao,
                ciclo="2026-10",
                categoria=None,
            ),
        )
        criar_lancamento(
            self.usuario,
            self.dados(descricao="Pagamento da fatura apenas no texto", categoria=None),
        )

        consulta = consultar_lancamentos(self.usuario, periodo="2026-10", cartao_id=self.cartao.pk)
        descricoes = {linha.descricao for linha in consulta["linhas"]}

        self.assertIn(compra.descricao, descricoes)
        self.assertIn("Pagamento confirmado", descricoes)
        self.assertNotIn("Pagamento da fatura apenas no texto", descricoes)

        consulta_conta = consultar_lancamentos(
            self.usuario,
            periodo="2026-10",
            conta_id=self.conta.pk,
        )
        descricoes_conta = {linha.descricao for linha in consulta_conta["linhas"]}
        self.assertIn("Despesa direta da conta", descricoes_conta)
        self.assertIn("Previsão direta da conta", descricoes_conta)
        self.assertIn("Pagamento da fatura apenas no texto", descricoes_conta)
        self.assertNotIn(compra.descricao, descricoes_conta)
        self.assertNotIn("Pagamento confirmado", descricoes_conta)

        consulta_combinada = consultar_lancamentos(
            self.usuario,
            periodo="2026-10",
            conta_id=self.conta.pk,
            cartao_id=self.cartao.pk,
        )
        descricoes_combinadas = {linha.descricao for linha in consulta_combinada["linhas"]}
        self.assertIn(compra.descricao, descricoes_combinadas)
        self.assertIn("Pagamento confirmado", descricoes_combinadas)
        self.assertNotIn("Despesa direta da conta", descricoes_combinadas)

    def test_busca_e_filtros_sao_combinados_e_isolam_usuario(self):
        criar_lancamento(self.usuario, self.dados(descricao="Mercado mensal"))
        conta_alheia = Conta.objects.create(usuario=self.outro, nome="Conta alheia")
        evento_alheio = EventoFinanceiro.objects.create(
            usuario=self.outro,
            natureza=NaturezaEvento.DESPESA,
            descricao="Mercado mensal alheio",
            data=date(2026, 10, 10),
            valor=Decimal("99.00"),
        )
        MovimentoConta.objects.create(
            conta=conta_alheia,
            evento=evento_alheio,
            data_efetiva=evento_alheio.data,
            valor=Decimal("-99.00"),
        )

        consulta = consultar_lancamentos(
            self.usuario,
            periodo="2026-10",
            busca="fornecedor",
            origem="CONTA",
            categoria=self.categoria.pk,
            situacao=Situacao.REALIZADO,
        )

        self.assertEqual([linha.descricao for linha in consulta["linhas"]], ["Mercado mensal"])

    def test_totalizadores_usam_as_linhas_finais_e_respeitam_origem_e_natureza(self):
        criar_lancamento(
            self.usuario,
            self.dados(descricao="Despesa da conta", valor=Decimal("100.00")),
        )
        criar_lancamento(
            self.usuario,
            self.dados(
                descricao="Receita da conta",
                natureza=NaturezaEvento.RECEITA,
                valor=Decimal("250.00"),
            ),
        )
        for descricao, valor in (("Transferência recebida", "60.00"), ("Transferência enviada", "-30.00")):
            evento = EventoFinanceiro.objects.create(
                usuario=self.usuario,
                natureza=NaturezaEvento.TRANSFERENCIA,
                descricao=descricao,
                data=date(2026, 10, 10),
                valor=abs(Decimal(valor)),
                situacao=Situacao.REALIZADO,
            )
            MovimentoConta.objects.create(
                conta=self.conta,
                evento=evento,
                data_efetiva=evento.data,
                valor=Decimal(valor),
            )
        criar_lancamento(
            self.usuario,
            self.dados(
                descricao="Despesa do cartão",
                origem="CARTAO",
                conta=None,
                cartao=self.cartao,
                ciclo="2026-10",
                valor=Decimal("40.00"),
            ),
        )

        consulta = consultar_lancamentos(self.usuario, periodo="2026-10")
        somente_conta = consultar_lancamentos(self.usuario, periodo="2026-10", origem="CONTA")
        somente_despesas = consultar_lancamentos(
            self.usuario,
            periodo="2026-10",
            natureza=NaturezaEvento.DESPESA,
        )

        self.assertEqual(consulta["total_creditos"], Decimal("310.00"))
        self.assertEqual(consulta["total_debitos"], Decimal("170.00"))
        self.assertEqual(somente_conta["total_creditos"], Decimal("310.00"))
        self.assertEqual(somente_conta["total_debitos"], Decimal("130.00"))
        self.assertEqual(somente_despesas["total_creditos"], Decimal("0.00"))
        self.assertEqual(somente_despesas["total_debitos"], Decimal("140.00"))

    def test_totalizador_classifica_valor_alocado_de_despesa_como_debito(self):
        criar_lancamento(
            self.usuario,
            self.dados(descricao="Despesa com rateio", valor=Decimal("100.00")),
        )
        linha = consultar_lancamentos(self.usuario, periodo="2026-10")["linhas"][0]
        linha_alocada = replace(linha, valor=Decimal("35.00"))

        total_creditos, total_debitos = _totalizar_linhas([linha_alocada])

        self.assertEqual(total_creditos, Decimal("0.00"))
        self.assertEqual(total_debitos, Decimal("35.00"))

    def test_nome_livre_persiste_sem_criar_pessoa_e_participa_da_busca(self):
        quantidade_pessoas = Pessoa.objects.filter(usuario=self.usuario).count()

        evento = criar_lancamento(
            self.usuario,
            self.dados(pessoa=None, pessoa_nome_livre="Fornecedor avulso"),
        )

        evento.refresh_from_db()
        self.assertIsNone(evento.pessoa)
        self.assertEqual(evento.pessoa_nome_livre, "Fornecedor avulso")
        self.assertEqual(Pessoa.objects.filter(usuario=self.usuario).count(), quantidade_pessoas)
        consulta = consultar_lancamentos(self.usuario, periodo="2026-10", busca="AVULSO")
        self.assertEqual([linha.pessoa for linha in consulta["linhas"]], ["Fornecedor avulso"])

    def test_nome_livre_e_propagado_para_todas_as_parcelas(self):
        evento = criar_lancamento(
            self.usuario,
            self.dados(
                origem="CARTAO",
                conta=None,
                cartao=self.cartao,
                ciclo="2026-10",
                quantidade_parcelas=3,
                pessoa=None,
                pessoa_nome_livre="Loja sem cadastro",
            ),
        )

        parcelas = evento.compras_cartao.get().parcelas.order_by("numero")
        self.assertEqual(list(parcelas.values_list("pessoa_nome_livre", flat=True)), ["Loja sem cadastro"] * 3)
        self.assertEqual([parcela.valor for parcela in parcelas], [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")])

    def test_exclusao_de_parcela_em_fatura_fechada_e_bloqueada(self):
        evento = criar_lancamento(
            self.usuario,
            self.dados(origem="CARTAO", conta=None, cartao=self.cartao, ciclo="2026-10"),
        )
        parcela = evento.compras_cartao.get().parcelas.get()
        parcela.fatura.status = Fatura.Status.FECHADA
        parcela.fatura.save(update_fields=["status", "atualizado_em"])

        with self.assertRaisesMessage(ValidationError, "deve ser reaberta"):
            excluir_parcela_lancamento(self.usuario, parcela.pk)

    def test_agrupa_cartao_por_fatura_depois_dos_filtros_sem_alterar_totais(self):
        outra_categoria = Categoria.objects.create(usuario=self.usuario, nome="Transporte")
        criar_lancamento(
            self.usuario,
            self.dados(
                descricao="Mercado da fatura",
                origem="CARTAO",
                conta=None,
                cartao=self.cartao,
                ciclo="2026-10",
                valor=Decimal("40.00"),
            ),
        )
        criar_lancamento(
            self.usuario,
            self.dados(
                descricao="Transporte da fatura",
                origem="CARTAO",
                conta=None,
                cartao=self.cartao,
                ciclo="2026-10",
                categoria=outra_categoria,
                valor=Decimal("60.00"),
            ),
        )
        criar_lancamento(self.usuario, self.dados(descricao="Despesa da conta", valor=Decimal("15.00")))
        fatura = Fatura.objects.get(cartao=self.cartao, ciclo="2026-10")
        fatura.status = Fatura.Status.FECHADA
        fatura.vencimento = date(2026, 10, 22)
        fatura.save(update_fields=["status", "vencimento", "atualizado_em"])

        consulta = consultar_lancamentos(
            self.usuario,
            periodo="2026-10",
            group_card_invoices="1",
        )

        agrupada = next(linha for linha in consulta["linhas"] if linha.agrupada_fatura)
        conta = next(linha for linha in consulta["linhas"] if linha.origem == "CONTA")
        self.assertEqual(consulta["total_registros"], 2)
        self.assertEqual(agrupada.descricao, "Fatura Cartão principal • 10/2026")
        self.assertEqual(agrupada.valor, Decimal("-100.00"))
        self.assertEqual(agrupada.quantidade_lancamentos_agrupados, 2)
        self.assertEqual(agrupada.data_referencia, date(2026, 10, 22))
        self.assertEqual(agrupada.categoria, "Lançamentos de cartão")
        self.assertEqual(agrupada.editar_url, "")
        self.assertFalse(conta.agrupada_fatura)
        self.assertEqual(consulta["total_debitos"], Decimal("115.00"))

        filtrada = consultar_lancamentos(
            self.usuario,
            periodo="2026-10",
            categoria=self.categoria.pk,
            busca="MERCADO",
            origem="CARTAO",
            group_card_invoices="1",
        )
        self.assertEqual(filtrada["total_registros"], 1)
        self.assertEqual(filtrada["linhas"][0].valor, Decimal("-40.00"))
        self.assertEqual(filtrada["linhas"][0].quantidade_lancamentos_agrupados, 1)

    def test_agrupamento_separa_ciclos_e_calcula_vencimento_previsto_aberto(self):
        criar_lancamento(
            self.usuario,
            self.dados(
                descricao="Fatura de outubro",
                origem="CARTAO",
                conta=None,
                cartao=self.cartao,
                ciclo="2026-10",
                valor=Decimal("20.00"),
            ),
        )
        criar_lancamento(
            self.usuario,
            self.dados(
                descricao="Fatura de novembro",
                origem="CARTAO",
                conta=None,
                cartao=self.cartao,
                ciclo="2026-11",
                valor=Decimal("30.00"),
            ),
        )

        consulta = consultar_lancamentos(
            self.usuario,
            origem="CARTAO",
            data_inicial="01/10/2026",
            data_final="30/11/2026",
            group_card_invoices="1",
        )

        self.assertEqual(consulta["total_registros"], 2)
        grupos = {linha.ciclo_fatura: linha for linha in consulta["linhas"]}
        self.assertEqual(set(grupos), {"10/2026", "11/2026"})
        self.assertEqual(grupos["11/2026"].data_referencia, date(2026, 11, 10))
        self.assertEqual(grupos["11/2026"].fatura_status, Fatura.Status.ABERTA)


class LancamentosViewTests(TestCase):
    def setUp(self):
        localdate_patcher = patch(
            "financeiro.services.lancamentos.timezone.localdate",
            return_value=date(2026, 10, 14),
        )
        localdate_patcher.start()
        self.addCleanup(localdate_patcher.stop)
        self.usuario = get_user_model().objects.create_user(username="titular", password="senha")
        self.conta = Conta.objects.create(usuario=self.usuario, nome="Conta")
        self.categoria = Categoria.objects.create(usuario=self.usuario, nome="Mercado")
        self.pessoa = Pessoa.objects.create(usuario=self.usuario, nome="Fornecedor cadastrado")
        self.client.force_login(self.usuario)

    def dados_post(self, **alteracoes):
        dados = {
            "descricao": "Despesa manual",
            "natureza": NaturezaEvento.DESPESA,
            "data": "2026-10-05",
            "valor": "50,00",
            "origem": "CONTA",
            "conta": self.conta.pk,
            "cartao": "",
            "categoria": "",
            "pessoa": "",
            "pessoa_nome_livre": "",
            "ambito": Ambito.NAO_DEFINIDO,
            "situacao": Situacao.REALIZADO,
            "vencimento": "",
            "ciclo": "",
            "quantidade_parcelas": "1",
        }
        dados.update(alteracoes)
        return dados

    def assert_redirecionamento_com_filtros(self, resposta, destino, filtros):
        self.assertEqual(resposta.status_code, 302)
        url = urlsplit(resposta.url)
        self.assertEqual(url.path, reverse(destino))
        parametros = parse_qs(url.query, keep_blank_values=True)
        for nome, valor in filtros.items():
            self.assertEqual(parametros.get(nome), [str(valor)], nome)

    def test_novo_lancamento_preserva_periodo_e_filtros_informados(self):
        filtros = {
            "period": "2026-11",
            "date_from": "01/11/2026",
            "date_to": "30/11/2026",
            "account": self.conta.pk,
            "q": "mercado",
            "origin": "CONTA",
            "category": self.categoria.pk,
            "nature": NaturezaEvento.DESPESA,
            "category_nature": Categoria.Natureza.PESSOAL,
            "essentiality": Categoria.Essencialidade.NECESSARIO_VARIAVEL,
            "computes": "COMPUTA",
            "person": self.pessoa.pk,
            "status": Situacao.REALIZADO,
            "group_card_invoices": "1",
            "card_purchase_date": "1",
        }
        url = f"{reverse('evento_novo')}?{urlencode({'return_to': 'eventos', **filtros})}"

        resposta = self.client.post(url, self.dados_post(data="2026-10-05"))

        self.assert_redirecionamento_com_filtros(resposta, "eventos", filtros)

    def test_novo_e_edicao_preservam_intervalo_explicitamente_vazio(self):
        filtros = {"period": "", "date_from": "", "date_to": ""}
        url_novo = f"{reverse('evento_novo')}?{urlencode({'return_to': 'eventos', **filtros})}"

        resposta_novo = self.client.post(
            url_novo,
            self.dados_post(descricao="Novo sem intervalo", data="2026-10-05"),
        )

        self.assert_redirecionamento_com_filtros(resposta_novo, "eventos", filtros)

        evento = EventoFinanceiro.objects.get(descricao="Novo sem intervalo")
        url_edicao = (
            f"{reverse('evento_editar', args=[evento.pk])}"
            f"?{urlencode({'return_to': 'eventos', **filtros})}"
        )
        resposta_edicao = self.client.post(
            url_edicao,
            self.dados_post(descricao="Editado sem intervalo", data="2026-10-06"),
        )

        self.assert_redirecionamento_com_filtros(resposta_edicao, "eventos", filtros)

    def test_novo_lancamento_preserva_todos_os_ciclos_e_mantem_fallback_sem_periodo(self):
        resposta_todos = self.client.post(
            f"{reverse('evento_novo')}?return_to=eventos&period=",
            self.dados_post(descricao="Todos os ciclos", data="2026-10-05"),
        )
        resposta_fallback = self.client.post(
            f"{reverse('evento_novo')}?return_to=eventos",
            self.dados_post(descricao="Fallback", data="2026-10-05"),
        )

        self.assert_redirecionamento_com_filtros(resposta_todos, "eventos", {"period": ""})
        self.assert_redirecionamento_com_filtros(resposta_fallback, "eventos", {"period": "2026-10"})

    def test_clonagem_encaminha_e_preserva_filtros_ate_o_retorno(self):
        evento = criar_lancamento(
            self.usuario,
            {
                "descricao": "Despesa original",
                "natureza": NaturezaEvento.DESPESA,
                "data": date(2026, 10, 5),
                "valor": Decimal("50.00"),
                "origem": "CONTA",
                "conta": self.conta,
                "cartao": None,
                "categoria": self.categoria,
                "pessoa": self.pessoa,
                "pessoa_nome_livre": "",
                "ambito": Ambito.PESSOAL,
                "situacao": Situacao.REALIZADO,
                "vencimento": None,
                "ciclo": "",
                "quantidade_parcelas": 1,
            },
        )
        filtros = {
            "period": "2026-11",
            "date_from": "01/11/2026",
            "date_to": "30/11/2026",
            "q": "original",
            "origin": "CONTA",
            "category": self.categoria.pk,
            "person": self.pessoa.pk,
            "status": Situacao.REALIZADO,
            "group_card_invoices": "1",
            "card_purchase_date": "1",
        }
        url_clonagem = (
            f"{reverse('evento_clonar', args=[evento.pk])}"
            f"?{urlencode({'return_to': 'eventos', **filtros})}"
        )

        redirecionamento_formulario = self.client.get(url_clonagem)

        self.assertEqual(redirecionamento_formulario.status_code, 302)
        parametros_formulario = parse_qs(
            urlsplit(redirecionamento_formulario.url).query,
            keep_blank_values=True,
        )
        for nome, valor in filtros.items():
            self.assertEqual(parametros_formulario.get(nome), [str(valor)], nome)

        resposta = self.client.post(
            redirecionamento_formulario.url,
            self.dados_post(descricao="Despesa clonada", data="2026-10-20"),
        )

        self.assert_redirecionamento_com_filtros(resposta, "eventos", filtros)

    def test_edicao_de_conta_preserva_periodo_e_datas_filtradas(self):
        evento = criar_lancamento(
            self.usuario,
            {
                "descricao": "Despesa para editar",
                "natureza": NaturezaEvento.DESPESA,
                "data": date(2026, 10, 5),
                "valor": Decimal("50.00"),
                "origem": "CONTA",
                "conta": self.conta,
                "cartao": None,
                "categoria": self.categoria,
                "pessoa": None,
                "pessoa_nome_livre": "",
                "ambito": Ambito.PESSOAL,
                "situacao": Situacao.REALIZADO,
                "vencimento": None,
                "ciclo": "",
                "quantidade_parcelas": 1,
            },
        )
        filtros = {
            "period": "2026-11",
            "date_from": "01/11/2026",
            "date_to": "30/11/2026",
            "origin": "CONTA",
            "status": Situacao.REALIZADO,
            "card_purchase_date": "1",
        }
        url = (
            f"{reverse('evento_editar', args=[evento.pk])}"
            f"?{urlencode({'return_to': 'eventos', **filtros})}"
        )

        resposta = self.client.post(
            url,
            self.dados_post(
                descricao="Despesa editada",
                data="2026-10-20",
                categoria=self.categoria.pk,
            ),
        )

        self.assert_redirecionamento_com_filtros(resposta, "eventos", filtros)

    def test_edicao_de_parcela_preserva_periodo_e_datas_filtradas(self):
        cartao = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão",
            conta_pagamento_padrao=self.conta,
            dia_vencimento=10,
        )
        evento = criar_lancamento(
            self.usuario,
            {
                "descricao": "Compra para editar",
                "natureza": NaturezaEvento.DESPESA,
                "data": date(2026, 10, 5),
                "valor": Decimal("50.00"),
                "origem": "CARTAO",
                "conta": None,
                "cartao": cartao,
                "categoria": self.categoria,
                "pessoa": None,
                "pessoa_nome_livre": "",
                "ambito": Ambito.PESSOAL,
                "situacao": Situacao.REALIZADO,
                "vencimento": None,
                "ciclo": "2026-10",
                "quantidade_parcelas": 1,
            },
        )
        parcela = ParcelaCartao.objects.get(compra__evento=evento)
        filtros = {
            "period": "2026-11",
            "date_from": "01/11/2026",
            "date_to": "30/11/2026",
            "origin": "CARTAO",
            "card": cartao.pk,
            "status": Situacao.REALIZADO,
            "card_purchase_date": "1",
        }
        url = (
            f"{reverse('parcela_cartao_editar', args=[parcela.pk])}"
            f"?{urlencode({'return_to': 'eventos', **filtros})}"
        )

        resposta = self.client.post(
            url,
            {
                "descricao": "Compra editada",
                "natureza": NaturezaEvento.DESPESA,
                "data_original": "2026-10-20",
                "valor": "50,00",
                "cartao": cartao.pk,
                "categoria": self.categoria.pk,
                "pessoa": "",
                "pessoa_nome_livre": "",
                "ambito": Ambito.PESSOAL,
                "situacao": Situacao.REALIZADO,
                "ciclo": "2026-10",
                "numero_parcela": "1",
                "total_parcelas": "1",
                "observacao": "",
            },
        )

        self.assert_redirecionamento_com_filtros(resposta, "eventos", filtros)

    def test_formulario_manual_de_cartao_ignora_vencimento_informado(self):
        cartao = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão",
            conta_pagamento_padrao=self.conta,
            dia_fechamento=25,
            dia_vencimento=11,
        )
        dados = self.dados_post(
            origem="CARTAO",
            conta="",
            cartao=cartao.pk,
            data="2026-10-08",
            vencimento="2026-11-11",
            ciclo="2026-11",
        )

        form = LancamentoForm(data=dados, usuario=self.usuario)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.ciclo_aviso, "")
        self.assertIsNone(form.cleaned_data["vencimento"])

        dados["ciclo"] = "2026-10"
        form_divergente = LancamentoForm(data=dados, usuario=self.usuario)

        self.assertTrue(form_divergente.is_valid(), form_divergente.errors)
        self.assertIn("data da compra (08/10/2026)", form_divergente.ciclo_aviso)
        self.assertIn("ciclo esperado é 11/2026", form_divergente.ciclo_aviso)

    def test_novo_lancamento_recorrente_de_conta_cria_previsoes_vinculadas(self):
        resposta = self.client.post(
            reverse("evento_novo"),
            self.dados_post(
                data="2027-01-01",
                vencimento="2027-01-15",
                modo_lancamento="RECORRENTE",
                quantidade_repeticoes="3",
                intervalo_meses="1",
            ),
        )

        self.assertEqual(resposta.status_code, 302)
        eventos = list(EventoFinanceiro.objects.filter(usuario=self.usuario).order_by("numero_recorrencia"))
        self.assertEqual([evento.numero_recorrencia for evento in eventos], [1, 2, 3])
        self.assertEqual([evento.situacao for evento in eventos], [Situacao.REALIZADO, Situacao.PREVISTO, Situacao.PREVISTO])
        self.assertEqual([evento.vencimento for evento in eventos], [date(2027, 1, 15), date(2027, 2, 15), date(2027, 3, 15)])

    def test_pagina_renderiza_seis_colunas_e_estado_vazio(self):
        resposta = self.client.get(reverse("eventos"), {"period": "2026-10"})

        self.assertContains(resposta, "Data original")
        self.assertContains(resposta, "Descrição / pessoa")
        self.assertContains(resposta, "Conta / cartão")
        self.assertContains(resposta, "Categoria / situação")
        self.assertContains(resposta, "Nenhum lançamento com esses filtros.")
        self.assertContains(resposta, 'colspan="6"')
        self.assertNotContains(resposta, "Natureza</th>")

    def test_pagina_preserva_agrupamento_de_faturas_na_url_de_retorno(self):
        resposta = self.client.get(reverse("eventos"), {"period": "2026-10", "group_card_invoices": "1"})

        self.assertContains(resposta, 'name="group_card_invoices"')
        self.assertContains(resposta, 'id="groupCardInvoices"')
        self.assertContains(resposta, 'checked')
        self.assertContains(resposta, "group_card_invoices=1")

    def test_pagina_exibe_fatura_agrupada_sem_acoes_individuais(self):
        cartao = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão agrupado",
            conta_pagamento_padrao=self.conta,
            dia_vencimento=11,
        )
        criar_lancamento(
            self.usuario,
            {
                "descricao": "Compra para agrupar",
                "natureza": NaturezaEvento.DESPESA,
                "data": date(2026, 10, 7),
                "valor": Decimal("75.00"),
                "origem": "CARTAO",
                "conta": None,
                "cartao": cartao,
                "categoria": self.categoria,
                "pessoa": None,
                "pessoa_nome_livre": "",
                "ambito": Ambito.PESSOAL,
                "situacao": Situacao.REALIZADO,
                "vencimento": None,
                "ciclo": "2026-10",
                "quantidade_parcelas": 1,
            },
        )

        resposta = self.client.get(
            reverse("eventos"),
            {"period": "2026-10", "group_card_invoices": "1"},
        )

        self.assertContains(resposta, "Fatura Cartão agrupado • 10/2026")
        self.assertContains(resposta, "1 lançamento")
        self.assertContains(resposta, "Lançamentos de cartão")
        self.assertNotContains(resposta, 'class="icon-button icon-edit"')
        self.assertNotContains(resposta, 'class="icon-button icon-clone"')
        self.assertNotContains(resposta, 'data-confirm-delete=')

    def test_pagina_preenche_mes_atual_e_preserva_intervalo_filtrado(self):
        resposta_inicial = self.client.get(reverse("eventos"))
        resposta_novembro = self.client.get(
            reverse("eventos"),
            {"date_from": "01/11/2026", "date_to": "30/11/2026"},
        )
        resposta_sem_intervalo = self.client.get(
            reverse("eventos"),
            {"date_from": "", "date_to": ""},
        )
        conteudo = resposta_inicial.content.decode()

        self.assertContains(resposta_inicial, 'name="date_from" value="01/10/2026"')
        self.assertContains(resposta_inicial, 'name="date_to" value="31/10/2026"')
        self.assertContains(resposta_novembro, 'name="date_from" value="01/11/2026"')
        self.assertContains(resposta_novembro, 'name="date_to" value="30/11/2026"')
        self.assertContains(resposta_sem_intervalo, 'name="date_from" value=""')
        self.assertContains(resposta_sem_intervalo, 'name="date_to" value=""')
        self.assertContains(
            resposta_inicial,
            f'href="{reverse("eventos")}?date_from=&amp;date_to="',
        )
        self.assertContains(
            resposta_inicial,
            'class="temporal-control transactions-date-control" data-transactions-date-control',
            count=2,
        )
        self.assertContains(
            resposta_inicial,
            'class="temporal-native" type="date" tabindex="-1" aria-hidden="true" data-transactions-date-native',
            count=2,
        )
        self.assertContains(resposta_inicial, 'data-date-role="from"')
        self.assertContains(resposta_inicial, 'data-date-role="to"')
        self.assertContains(resposta_inicial, 'aria-label="Selecionar data inicial no calendário"')
        self.assertContains(resposta_inicial, 'aria-label="Selecionar data final no calendário"')
        self.assertIn("const syncFinalDateFromInitial = () =>", conteudo)
        self.assertIn("new Date(parsed.year, parsed.month, 0).getDate()", conteudo)
        self.assertIn("if (typeof native.showPicker === 'function') native.showPicker()", conteudo)

    def test_pagina_exibe_vencimento_da_fatura_abaixo_da_data_original(self):
        cartao = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão",
            conta_pagamento_padrao=self.conta,
            dia_vencimento=11,
        )
        criar_lancamento(
            self.usuario,
            {
                "descricao": "Compra parcelada",
                "natureza": NaturezaEvento.DESPESA,
                "data": date(2026, 9, 17),
                "valor": Decimal("100.00"),
                "origem": "CARTAO",
                "conta": None,
                "cartao": cartao,
                "categoria": self.categoria,
                "pessoa": None,
                "pessoa_nome_livre": "",
                "ambito": Ambito.PESSOAL,
                "situacao": Situacao.REALIZADO,
                "vencimento": None,
                "ciclo": "2026-11",
                "quantidade_parcelas": 1,
            },
        )

        resposta = self.client.get(
            reverse("eventos"),
            {"period": "2026-11", "date_from": "01/11/2026", "date_to": "30/11/2026"},
        )

        self.assertContains(resposta, "17/09/2026")
        self.assertContains(resposta, "Vcto: 11/11/2026")
        self.assertContains(resposta, "Ciclo: 11/2026")

    def test_pagina_exibe_observacao_de_conta_e_cartao_em_linha_adicional(self):
        evento_conta = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Despesa observada",
            data=date(2026, 10, 5),
            valor=Decimal("50.00"),
            situacao=Situacao.REALIZADO,
            observacao="Primeira linha\nSegunda linha",
        )
        MovimentoConta.objects.create(
            conta=self.conta,
            evento=evento_conta,
            data_efetiva=evento_conta.data,
            valor=Decimal("-50.00"),
        )
        evento_sem_observacao = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Despesa sem observação",
            data=date(2026, 10, 6),
            valor=Decimal("25.00"),
            situacao=Situacao.REALIZADO,
            observacao="   ",
        )
        MovimentoConta.objects.create(
            conta=self.conta,
            evento=evento_sem_observacao,
            data_efetiva=evento_sem_observacao.data,
            valor=Decimal("-25.00"),
        )
        cartao = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão observado",
            conta_pagamento_padrao=self.conta,
            dia_vencimento=11,
        )
        criar_lancamento(
            self.usuario,
            {
                "descricao": "Compra observada",
                "natureza": NaturezaEvento.DESPESA,
                "data": date(2026, 10, 7),
                "valor": Decimal("75.00"),
                "origem": "CARTAO",
                "conta": None,
                "cartao": cartao,
                "categoria": self.categoria,
                "pessoa": None,
                "pessoa_nome_livre": "",
                "ambito": Ambito.PESSOAL,
                "situacao": Situacao.REALIZADO,
                "vencimento": None,
                "ciclo": "2026-10",
                "quantidade_parcelas": 1,
                "observacao": "Observação da compra no cartão.",
            },
        )

        resposta = self.client.get(reverse("eventos"), {"period": "2026-10"})
        conteudo = resposta.content.decode()

        self.assertContains(resposta, 'class="transaction-observation-row"', count=2)
        self.assertContains(resposta, '<td colspan="4">', count=2)
        self.assertContains(resposta, "Observação:", count=2)
        self.assertIn("Primeira linha\nSegunda linha", conteudo)
        self.assertContains(resposta, "Observação da compra no cartão.")

    def test_pagina_concentra_todos_os_filtros_e_preserva_parametros_nas_acoes(self):
        criar_lancamento(
            self.usuario,
            {
                "descricao": "Lançamento filtrável",
                "natureza": NaturezaEvento.DESPESA,
                "data": date(2026, 10, 5),
                "valor": Decimal("50.00"),
                "origem": "CONTA",
                "conta": self.conta,
                "cartao": None,
                "categoria": self.categoria,
                "pessoa": self.pessoa,
                "pessoa_nome_livre": "",
                "ambito": Ambito.PESSOAL,
                "situacao": Situacao.REALIZADO,
                "vencimento": None,
                "ciclo": "",
                "quantidade_parcelas": 1,
            },
        )
        parametros = {
            "period": "2026-10",
            "date_from": "01/10/2026",
            "date_to": "31/10/2026",
            "category": self.categoria.pk,
            "nature": NaturezaEvento.DESPESA,
            "person": self.pessoa.pk,
            "group_card_invoices": "1",
            "card_purchase_date": "1",
        }

        resposta = self.client.get(reverse("eventos"), parametros)
        conteudo = resposta.content.decode()

        self.assertContains(resposta, 'id="transactionsFilters"')
        self.assertContains(
            resposta,
            '<details class="panel full collapsible-panel transactions-filters-panel" open>',
        )
        self.assertContains(resposta, 'title="Abrir ou fechar os filtros dos lançamentos"')
        self.assertNotContains(resposta, 'id="transactionsGlobalFilters"')
        self.assertNotContains(resposta, 'id="transactionsLocalFilters"')
        for nome in (
            "period",
            "date_from",
            "date_to",
            "account",
            "card",
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
            "card_purchase_date",
            "group_card_invoices",
        ):
            self.assertIn(f'name="{nome}"', conteudo)
        cabecalho_inicio = conteudo.index('<header class="top">')
        cabecalho_fim = conteudo.index("</header>", cabecalho_inicio)
        cabecalho = conteudo[cabecalho_inicio:cabecalho_fim]
        self.assertIn("Lançamentos", cabecalho)
        self.assertIn('name="period"', cabecalho)
        self.assertIn("Novo lançamento", cabecalho)
        self.assertLess(cabecalho.index('name="period"'), cabecalho.index("Novo lançamento"))
        self.assertNotIn('name="date_from"', cabecalho)
        self.assertIn("date_from=01%2F10%2F2026", conteudo)
        self.assertIn("person=", conteudo)
        self.assertContains(resposta, "Todos os ciclos")
        self.assertContains(resposta, "Compõe e não compõe")
        self.assertContains(resposta, ">Compõe</option>")
        self.assertContains(resposta, ">Não compõe</option>")
        self.assertContains(resposta, 'placeholder="DD/MM/AAAA"', count=2)
        self.assertTrue(resposta.context["card_purchase_date"])
        self.assertContains(resposta, 'name="card_purchase_date" value="1" checked data-card-purchase-date')
        self.assertIn("card_purchase_date=1", conteudo)

    def test_checkbox_data_compra_inicia_desmarcado_e_submete_automaticamente(self):
        resposta = self.client.get(reverse("eventos"))

        self.assertFalse(resposta.context["card_purchase_date"])
        self.assertContains(resposta, 'name="card_purchase_date" value="1" data-card-purchase-date')
        self.assertNotContains(resposta, 'name="card_purchase_date" value="1" checked')
        self.assertContains(
            resposta,
            "[data-card-invoice-grouping], [data-card-purchase-date]",
        )

    def test_lista_colore_entradas_e_saidas(self):
        for descricao, natureza, valor in (
            ("Despesa colorida", NaturezaEvento.DESPESA, Decimal("-50.00")),
            ("Receita colorida", NaturezaEvento.RECEITA, Decimal("80.00")),
        ):
            evento = EventoFinanceiro.objects.create(
                usuario=self.usuario,
                natureza=natureza,
                descricao=descricao,
                data=date(2026, 10, 5),
                valor=abs(valor),
                situacao=Situacao.REALIZADO,
            )
            MovimentoConta.objects.create(
                conta=self.conta,
                evento=evento,
                data_efetiva=evento.data,
                valor=valor,
            )

        resposta = self.client.get(reverse("eventos"), {"period": "2026-10"})

        self.assertContains(resposta, 'class="money amount-negative">-R$ 50,00')
        self.assertContains(resposta, 'class="money amount-positive">R$ 80,00')

    def test_pagina_exibe_totalizadores_e_lista_recolhivel_entre_filtros_e_tabela(self):
        for descricao, natureza, valor in (
            ("Despesa totalizada", NaturezaEvento.DESPESA, Decimal("-50.00")),
            ("Receita totalizada", NaturezaEvento.RECEITA, Decimal("80.00")),
        ):
            evento = EventoFinanceiro.objects.create(
                usuario=self.usuario,
                natureza=natureza,
                descricao=descricao,
                data=date(2026, 10, 5),
                valor=abs(valor),
                situacao=Situacao.REALIZADO,
            )
            MovimentoConta.objects.create(
                conta=self.conta,
                evento=evento,
                data_efetiva=evento.data,
                valor=valor,
            )

        resposta = self.client.get(reverse("eventos"), {"period": "2026-10"})
        conteudo = resposta.content.decode()

        self.assertContains(resposta, 'class="transactions-totals"')
        self.assertContains(resposta, 'aria-label="Totais dos lançamentos filtrados"')
        self.assertContains(resposta, "Total de créditos")
        self.assertContains(resposta, 'class="amount-positive">R$ 80,00')
        self.assertContains(resposta, "Total de débitos")
        self.assertContains(resposta, 'class="amount-negative">R$ 50,00')
        self.assertContains(
            resposta,
            '<details class="panel full collapsible-panel transactions-list-panel" aria-labelledby="transactionsListTitle" open>',
        )
        self.assertContains(resposta, 'title="Abrir ou fechar a lista de lançamentos"')
        self.assertContains(resposta, "2 registros")
        self.assertLess(conteudo.index("transactions-filters-panel"), conteudo.index("transactions-totals"))
        self.assertLess(conteudo.index("transactions-totals"), conteudo.index("transactions-list-panel"))

    def test_modal_renderiza_ordem_defaults_e_controles_da_referencia(self):
        resposta = self.client.get(reverse("evento_novo"), {"period": "2026-10", "return_to": "eventos"})
        conteudo = resposta.content.decode()

        self.assertEqual(resposta.status_code, 200)
        campos = [
            'name="descricao"',
            'name="natureza"',
            'name="data"',
            'name="valor"',
            'name="origem"',
            'name="conta"',
            'name="cartao"',
            'name="categoria"',
            'name="pessoa_nome_livre"',
            'name="ambito"',
            'name="situacao"',
            'name="vencimento"',
            'name="ciclo"',
            'name="quantidade_parcelas"',
            'name="observacao"',
        ]
        posicoes = [conteudo.index(campo) for campo in campos]
        self.assertEqual(posicoes, sorted(posicoes))
        self.assertContains(resposta, 'data-temporal-control="date"', count=2)
        self.assertContains(resposta, 'data-temporal-control="month"', count=1)
        self.assertContains(resposta, 'placeholder="DD/MM/AAAA"')
        self.assertContains(resposta, 'placeholder="MM/AAAA"')
        self.assertContains(resposta, "Selecione a conta")
        self.assertContains(resposta, "Selecione o cartão")
        self.assertContains(resposta, "A classificar")
        self.assertContains(resposta, 'placeholder="Ex.: 250,00"')
        self.assertNotContains(resposta, "---------")
        self.assertNotContains(resposta, "Descrição:</label>")
        self.assertContains(resposta, "Vencimento (para previsão)")
        self.assertContains(resposta, "Escolha lançamento único, compra parcelada ou recorrência.")

    @patch("financeiro.forms.timezone.localdate", return_value=date(2026, 10, 26))
    def test_novo_e_clonagem_usam_hoje_previsto_sem_alterar_edicao_ou_nova_compra(self, _localdate):
        cartao = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão para clonar",
            conta_pagamento_padrao=self.conta,
            dia_vencimento=10,
        )
        original_cartao = criar_lancamento(
            self.usuario,
            {
                "descricao": "Compra original para clonar",
                "natureza": NaturezaEvento.DESPESA,
                "data": date(2026, 10, 5),
                "valor": Decimal("50.00"),
                "origem": "CARTAO",
                "conta": None,
                "cartao": cartao,
                "categoria": self.categoria,
                "pessoa": None,
                "pessoa_nome_livre": "",
                "ambito": Ambito.PESSOAL,
                "situacao": Situacao.REALIZADO,
                "vencimento": None,
                "ciclo": "2026-11",
                "quantidade_parcelas": 1,
            },
        )
        original_conta = criar_lancamento(
            self.usuario,
            {
                "descricao": "Conta para editar",
                "natureza": NaturezaEvento.DESPESA,
                "data": date(2026, 10, 6),
                "valor": Decimal("40.00"),
                "origem": "CONTA",
                "conta": self.conta,
                "cartao": None,
                "categoria": self.categoria,
                "pessoa": None,
                "pessoa_nome_livre": "",
                "ambito": Ambito.PESSOAL,
                "situacao": Situacao.REALIZADO,
                "vencimento": None,
                "ciclo": "",
                "quantidade_parcelas": 1,
            },
        )

        novo = self.client.get(reverse("evento_novo"), {"period": "2026-11"})
        form_novo = novo.context["form"]
        self.assertEqual(form_novo.initial["data"], date(2026, 10, 26))
        self.assertEqual(form_novo.initial["situacao"], Situacao.PREVISTO)
        self.assertEqual(form_novo.initial["ciclo"], "2026-11")

        redirecionamento = self.client.get(reverse("evento_clonar", args=[original_cartao.pk]))
        clonagem = self.client.get(redirecionamento.url)
        form_clonagem = clonagem.context["form"]
        self.assertEqual(form_clonagem.initial["data"], date(2026, 10, 26))
        self.assertEqual(form_clonagem.initial["situacao"], Situacao.PREVISTO)
        self.assertEqual(form_clonagem.initial["cartao"], cartao)
        self.assertEqual(form_clonagem.initial["ciclo"], "2026-11")

        edicao = self.client.get(reverse("evento_editar", args=[original_conta.pk]))
        form_edicao = edicao.context["form"]
        self.assertEqual(form_edicao.initial["data"], date(2026, 10, 6))
        self.assertEqual(form_edicao.initial["situacao"], Situacao.REALIZADO)

        nova_compra = self.client.get(reverse("compra_cartao_nova"), {"card": cartao.pk, "cycle": "2026-11"})
        form_nova_compra = nova_compra.context["form"]
        self.assertEqual(form_nova_compra["situacao"].value(), Situacao.REALIZADO)
        self.assertIsNone(form_nova_compra["data_original"].value())

    @patch("financeiro.forms.timezone.localdate", return_value=date(2026, 10, 26))
    def test_erro_no_novo_lancamento_preserva_data_e_situacao_enviadas(self, _localdate):
        resposta = self.client.post(
            reverse("evento_novo"),
            self.dados_post(
                data="2026-09-03",
                situacao=Situacao.REALIZADO,
                valor="",
            ),
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.context["form"]["data"].value(), "03/09/2026")
        self.assertEqual(resposta.context["form"]["situacao"].value(), Situacao.REALIZADO)

    def test_comboboxes_exibem_categoria_filha_com_pai_e_preservam_valores(self):
        pai = Categoria.objects.create(usuario=self.usuario, nome="Receitas")
        filha = Categoria.objects.create(
            usuario=self.usuario,
            nome="Prestação de serviços",
            categoria_pai=pai,
        )
        inativa = Categoria.objects.create(
            usuario=self.usuario,
            nome="Serviço antigo",
            categoria_pai=pai,
            ativa=False,
        )

        formulario = LancamentoForm(usuario=self.usuario, initial={"categoria": filha})
        formulario_cartao = CompraCartaoForm(usuario=self.usuario, initial={"categoria": filha})
        formulario_enviado = LancamentoForm(
            data=self.dados_post(categoria=filha.pk),
            usuario=self.usuario,
        )

        self.assertContains(
            self.client.get(reverse("evento_novo")),
            f'<option value="{filha.pk}">Prestação de serviços (Receitas)</option>',
            html=True,
        )
        self.assertIn(
            f'<option value="{filha.pk}" selected>Prestação de serviços (Receitas)</option>',
            str(formulario["categoria"]),
        )
        self.assertIn("Prestação de serviços (Receitas)", str(formulario_cartao["categoria"]))
        self.assertTrue(formulario_enviado.is_valid(), formulario_enviado.errors)
        self.assertEqual(formulario_enviado.cleaned_data["categoria"], filha)

        resposta_filtro = self.client.get(
            reverse("eventos"),
            {"period": "2026-10", "category": inativa.pk},
        )
        self.assertContains(
            resposta_filtro,
            f'<option value="{inativa.pk}" selected>Serviço antigo (Receitas) (inativa)</option>',
            html=True,
        )

    def test_novo_lancamento_exibe_e_persiste_observacao(self):
        resposta = self.client.post(
            reverse("evento_novo") + "?return_to=eventos&period=2026-10",
            self.dados_post(observacao="Pagamento referente ao contrato de outubro."),
        )

        self.assertEqual(resposta.status_code, 302)
        evento = EventoFinanceiro.objects.get(descricao="Despesa manual")
        self.assertEqual(evento.observacao, "Pagamento referente ao contrato de outubro.")

    def test_nome_livre_e_pessoa_selecionada_persistem_sem_associacao_silenciosa(self):
        quantidade_pessoas = Pessoa.objects.count()
        resposta_livre = self.client.post(
            reverse("evento_novo") + "?return_to=eventos&period=2026-10",
            self.dados_post(descricao="Nome livre", pessoa_nome_livre="Prestador ocasional"),
        )

        self.assertEqual(resposta_livre.status_code, 302)
        evento_livre = EventoFinanceiro.objects.get(descricao="Nome livre")
        self.assertIsNone(evento_livre.pessoa)
        self.assertEqual(evento_livre.pessoa_nome_livre, "Prestador ocasional")
        self.assertEqual(Pessoa.objects.count(), quantidade_pessoas)

        resposta_cadastrada = self.client.post(
            reverse("evento_novo") + "?return_to=eventos&period=2026-10",
            self.dados_post(
                descricao="Pessoa cadastrada",
                pessoa=self.pessoa.pk,
                pessoa_nome_livre=self.pessoa.nome,
            ),
        )

        self.assertEqual(resposta_cadastrada.status_code, 302)
        evento_cadastrado = EventoFinanceiro.objects.get(descricao="Pessoa cadastrada")
        self.assertEqual(evento_cadastrado.pessoa, self.pessoa)
        self.assertEqual(evento_cadastrado.pessoa_nome_livre, "")

        resposta_alterada = self.client.post(
            reverse("evento_novo") + "?return_to=eventos&period=2026-10",
            self.dados_post(
                descricao="Pessoa digitada após sugestão",
                pessoa=self.pessoa.pk,
                pessoa_nome_livre="Nome alterado manualmente",
            ),
        )
        self.assertEqual(resposta_alterada.status_code, 302)
        evento_alterado = EventoFinanceiro.objects.get(descricao="Pessoa digitada após sugestão")
        self.assertIsNone(evento_alterado.pessoa)
        self.assertEqual(evento_alterado.pessoa_nome_livre, "Nome alterado manualmente")

    def test_formulario_rejeita_pessoa_de_outro_usuario_e_preserva_texto(self):
        outro = get_user_model().objects.create_user(username="outro-form", password="senha")
        pessoa_alheia = Pessoa.objects.create(usuario=outro, nome="Pessoa alheia")
        form = LancamentoForm(
            self.dados_post(pessoa=pessoa_alheia.pk, pessoa_nome_livre="Nome informado"),
            usuario=self.usuario,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("pessoa", form.errors)
        self.assertEqual(form.data["pessoa_nome_livre"], "Nome informado")

    def test_edicao_manual_pode_transformar_realizado_em_previsto(self):
        evento = criar_lancamento(
            self.usuario,
            {
                "descricao": "Despesa manual",
                "natureza": NaturezaEvento.DESPESA,
                "data": date(2026, 10, 5),
                "valor": Decimal("50.00"),
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
            },
        )

        resposta = self.client.post(
            reverse("evento_editar", args=[evento.pk]) + "?return_to=eventos&period=2026-10",
            {
                "descricao": "Despesa prevista",
                "natureza": NaturezaEvento.DESPESA,
                "data": "05/10/2026",
                "valor": "50,00",
                "origem": "CONTA",
                "conta": self.conta.pk,
                "cartao": "",
                "categoria": self.categoria.pk,
                "pessoa": "",
                "ambito": Ambito.PESSOAL,
                "situacao": Situacao.PREVISTO,
                "vencimento": "20/10/2026",
                "ciclo": "",
                "quantidade_parcelas": "1",
            },
        )

        self.assertRedirects(resposta, reverse("eventos") + "?period=2026-10")
        evento.refresh_from_db()
        self.assertEqual(evento.situacao, Situacao.PREVISTO)
        self.assertFalse(evento.movimentos_conta.exists())
        self.assertEqual(evento.titulo_previsto.vencimento, date(2026, 10, 20))
