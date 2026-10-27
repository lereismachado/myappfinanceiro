from decimal import Decimal
from datetime import date
import tempfile

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from financeiro.forms import AplicacaoLoteForm, ItemImportacaoForm
from financeiro.models import (
    Ambito,
    Cartao,
    Categoria,
    CompraCartao,
    Conta,
    EventoFinanceiro,
    Fatura,
    Importacao,
    ItemImportacao,
    Liquidacao,
    MovimentoConta,
    NaturezaEvento,
    ParcelaCartao,
    RateioCategoria,
    RegraCategoria,
    RegraCategoriaCondicao,
    Situacao,
    Titulo,
    VinculoTituloImportacao,
)
from financeiro.ofx import OFXParser
from financeiro.services.importacao_ofx import (
    buscar_candidatos_vinculo,
    confirmar_importacao,
    criar_itens,
    desfazer_importacao,
    selecionar_vinculo_item,
    sincronizar_titulos_multiplos,
)
from financeiro.tests.test_ofx import OFX_CARTAO, OFX_CONTA


class ImportacaoServiceTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(username="leandro", password="senha")
        self.conta = Conta.objects.create(usuario=self.usuario, nome="Conta Sicoob", banco="756", numero="12345")
        self.cartao = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão principal",
            identificador_externo="9999",
            final="9999",
            dia_vencimento=10,
        )

    def criar_lote(self, documento, nome="arquivo.ofx", ciclo=""):
        lote = Importacao.objects.create(
            usuario=self.usuario,
            arquivo=f"importacoes/testes/{nome}",
            nome_arquivo=nome,
            hash_arquivo="a" * 64,
            origem=documento.origem,
            conta=self.conta if documento.origem == "CONTA" else None,
            cartao=self.cartao if documento.origem == "CARTAO" else None,
            ciclo=ciclo,
            status=Importacao.Status.EM_REVISAO,
        )
        criar_itens(lote, documento)
        return lote

    def criar_titulo_previsto(self, valor, vencimento, descricao="Receita prevista", categoria=None, tipo=Titulo.Tipo.RECEBER):
        natureza = NaturezaEvento.RECEITA if tipo == Titulo.Tipo.RECEBER else NaturezaEvento.DESPESA
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=natureza,
            descricao=descricao,
            data=vencimento,
            vencimento=vencimento,
            valor=Decimal(valor),
            ambito=Ambito.PROFISSIONAL,
            situacao=Situacao.PREVISTO,
        )
        if categoria:
            RateioCategoria.objects.create(evento=evento, categoria=categoria, valor=Decimal(valor))
        titulo = Titulo.objects.create(
            usuario=self.usuario,
            tipo=tipo,
            descricao=descricao,
            vencimento=vencimento,
            valor_original=Decimal(valor),
            saldo_aberto=Decimal(valor),
            conta=self.conta,
            evento=evento,
        )
        return titulo

    def criar_parcela_prevista_cartao(
        self,
        ciclo="2026-10",
        numero=2,
        total=12,
        valor="100.00",
        data_compra=date(2026, 9, 10),
        status_fatura=Fatura.Status.ABERTA,
        situacao=Situacao.PREVISTO,
    ):
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Compra prevista para conferência",
            data=data_compra,
            valor=Decimal(valor) * total,
            situacao=situacao,
        )
        compra = CompraCartao.objects.create(
            usuario=self.usuario,
            cartao=self.cartao,
            evento=evento,
            estabelecimento="Compra prevista para conferência",
            data_compra=evento.data,
            valor_total=evento.valor,
            total_parcelas=total,
        )
        fatura = Fatura.objects.create(
            usuario=self.usuario,
            cartao=self.cartao,
            ciclo=ciclo,
            status=status_fatura,
        )
        parcela = ParcelaCartao.objects.create(
            compra=compra,
            fatura=fatura,
            natureza=NaturezaEvento.DESPESA,
            descricao="Compra prevista para conferência",
            data_original=evento.data,
            numero=numero,
            total=total,
            valor=Decimal(valor),
            situacao=situacao,
        )
        return parcela

    def test_conta_confirma_e_reimporta_sem_duplicar(self):
        documento = OFXParser.parse(OFX_CONTA)
        primeiro = self.criar_lote(documento)
        self.assertEqual(
            primeiro.itens.filter(
                status=ItemImportacao.Status.NOVO,
                acao=ItemImportacao.Acao.CRIAR,
                evento__isnull=True,
                movimento_conta__isnull=True,
                titulo_alvo__isnull=True,
                parcela__isnull=True,
            ).count(),
            2,
        )
        item_com_observacao = primeiro.itens.order_by("posicao").first()
        item_com_observacao.observacao = "Conferido no extrato semanal."
        item_com_observacao.save(update_fields=["observacao"])
        confirmar_importacao(primeiro.pk, self.usuario)
        self.assertEqual(EventoFinanceiro.objects.count(), 2)
        self.assertEqual(MovimentoConta.objects.count(), 2)

        segundo = self.criar_lote(documento, nome="mesmo-nome.ofx")
        self.assertEqual(segundo.itens.filter(status=ItemImportacao.Status.CONCILIADO).count(), 2)
        self.assertEqual(segundo.itens.get(posicao=item_com_observacao.posicao).observacao, "Conferido no extrato semanal.")
        confirmar_importacao(segundo.pk, self.usuario)
        terceiro = self.criar_lote(documento, nome="terceiro.ofx")
        self.assertEqual(terceiro.itens.filter(status=ItemImportacao.Status.CONCILIADO).count(), 2)
        confirmar_importacao(terceiro.pk, self.usuario)
        self.assertEqual(EventoFinanceiro.objects.count(), 2)
        self.assertEqual(MovimentoConta.objects.count(), 2)
        item_com_observacao.refresh_from_db()
        self.assertEqual(item_com_observacao.evento.observacao, "Conferido no extrato semanal.")
        self.assertNotIn("Importado do lote #", item_com_observacao.evento.observacao)
        self.assertFalse(EventoFinanceiro.objects.filter(observacao__icontains="Importado do lote #").exists())
        self.assertTrue(EventoFinanceiro.objects.filter(observacao="").exists())

    def _criar_historico_conta(self, *, descricao="Mercado Central", data=date(2026, 9, 5), valor="-125.50", categoria=None, ambito=Ambito.PESSOAL, natureza=NaturezaEvento.DESPESA, conta=None):
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=natureza,
            descricao=descricao,
            data=data,
            valor=abs(Decimal(valor)),
            ambito=ambito,
            situacao=Situacao.REALIZADO,
        )
        if categoria:
            RateioCategoria.objects.create(evento=evento, categoria=categoria, valor=abs(Decimal(valor)))
        return MovimentoConta.objects.create(
            conta=conta or self.conta,
            evento=evento,
            data_efetiva=data,
            valor=Decimal(valor),
            historico_original=descricao,
        )

    def test_conta_sugere_categoria_e_ambito_por_historico_exato(self):
        categoria = Categoria.objects.create(usuario=self.usuario, nome="Mercado", tipo=Categoria.Tipo.DESPESA)
        self._criar_historico_conta(categoria=categoria)

        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(posicao=1)

        self.assertEqual(item.status, ItemImportacao.Status.NOVO)
        self.assertEqual(item.acao, ItemImportacao.Acao.CRIAR)
        self.assertEqual(item.categoria_id, categoria.pk)
        self.assertEqual(item.ambito, Ambito.PESSOAL)
        self.assertIn("Categoria sugerida pelo histórico recente", item.motivo_status)
        self.assertIn("Âmbito sugerido pelo histórico recente", item.motivo_status)

    def test_conta_historico_dividido_nao_sugere_classificacao(self):
        categoria_a = Categoria.objects.create(usuario=self.usuario, nome="Mercado", tipo=Categoria.Tipo.DESPESA)
        categoria_b = Categoria.objects.create(usuario=self.usuario, nome="Casa", tipo=Categoria.Tipo.DESPESA)
        self._criar_historico_conta(categoria=categoria_a, data=date(2026, 9, 5), ambito=Ambito.PESSOAL)
        self._criar_historico_conta(categoria=categoria_b, data=date(2026, 9, 6), ambito=Ambito.PROFISSIONAL)

        documento = OFXParser.parse(OFX_CONTA.replace(b"Mercado Central", b"Compra recorrente"))
        lote = self.criar_lote(documento)
        item = lote.itens.get(posicao=1)

        self.assertIsNone(item.categoria_id)
        self.assertEqual(item.ambito, Ambito.PESSOAL)
        self.assertIn("Âmbito definido como Pessoal por padrão", item.motivo_status)

    def test_conta_historico_fora_do_mes_anterior_e_natureza_incompativel_nao_sugere(self):
        categoria = Categoria.objects.create(usuario=self.usuario, nome="Mercado", tipo=Categoria.Tipo.DESPESA)
        self._criar_historico_conta(categoria=categoria, data=date(2026, 8, 5))
        self._criar_historico_conta(
            categoria=categoria,
            data=date(2026, 9, 7),
            valor="125.50",
            natureza=NaturezaEvento.RECEITA,
        )

        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(posicao=1)

        self.assertIsNone(item.categoria_id)
        self.assertEqual(item.ambito, Ambito.PESSOAL)

    def test_conta_historico_exclui_pagamento_transferencia_e_estorno_por_descricao(self):
        categoria = Categoria.objects.create(usuario=self.usuario, nome="Mercado", tipo=Categoria.Tipo.DESPESA)
        for descricao in ("Pagamento de fatura", "Transferência interna", "Estorno Mercado"):
            self._criar_historico_conta(categoria=categoria, descricao=descricao)

        documento = OFXParser.parse(OFX_CONTA.replace(b"Mercado Central", b"Pagamento de fatura"))
        lote = self.criar_lote(documento)
        item = lote.itens.get(posicao=1)

        self.assertIsNone(item.categoria_id)
        self.assertEqual(item.ambito, Ambito.PESSOAL)

    def test_regra_categoria_prevalece_sobre_historico(self):
        categoria_historica = Categoria.objects.create(usuario=self.usuario, nome="Mercado", tipo=Categoria.Tipo.DESPESA)
        categoria_regra = Categoria.objects.create(usuario=self.usuario, nome="Alimentação", tipo=Categoria.Tipo.DESPESA)
        self._criar_historico_conta(categoria=categoria_historica)
        RegraCategoria.objects.create(
            usuario=self.usuario,
            texto="Mercado Central",
            nome="Mercado Central (legada)",
            versao_motor=RegraCategoria.VersaoMotor.LEGADO,
            categoria=categoria_regra,
            ambito=Ambito.PROFISSIONAL,
        )

        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(posicao=1)

        self.assertEqual(item.categoria_id, categoria_regra.pk)
        self.assertEqual(item.ambito, Ambito.PROFISSIONAL)

    def test_regra_nova_e_aplicada_somente_depois_da_conciliacao(self):
        categoria = Categoria.objects.create(usuario=self.usuario, nome="Compras categorizadas")
        regra = RegraCategoria.objects.create(
            usuario=self.usuario,
            nome="Mercado como receita",
            prioridade=1,
            categoria=categoria,
            ambito=Ambito.PROFISSIONAL,
            natureza_sugerida=NaturezaEvento.RECEITA,
        )
        RegraCategoriaCondicao.objects.create(
            regra=regra,
            ordem=1,
            campo=RegraCategoriaCondicao.Campo.DESCRICAO,
            operador=RegraCategoriaCondicao.Operador.CONTEM,
            valor="MERCADO CENTRAL",
        )

        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(posicao=1)

        self.assertEqual(item.status, ItemImportacao.Status.NOVO)
        self.assertEqual(item.acao, ItemImportacao.Acao.CRIAR)
        self.assertEqual(item.categoria, categoria)
        self.assertEqual(item.ambito, Ambito.PROFISSIONAL)
        self.assertEqual(item.natureza, NaturezaEvento.RECEITA)
        self.assertIn("Mercado como receita", item.motivo_status)

        confirmar_importacao(lote.pk, self.usuario)
        item.refresh_from_db()
        self.assertIn("Mercado como receita", item.motivo_status)

    def test_regra_nova_nao_altera_item_pre_vinculado(self):
        documento = OFXParser.parse(OFX_CONTA)
        registro = documento.transacoes[0]
        categoria = Categoria.objects.create(usuario=self.usuario, nome="Não aplicar no vínculo")
        regra = RegraCategoria.objects.create(
            usuario=self.usuario,
            nome="Regra que seria aplicável",
            prioridade=1,
            categoria=categoria,
        )
        RegraCategoriaCondicao.objects.create(
            regra=regra,
            ordem=1,
            campo=RegraCategoriaCondicao.Campo.DESCRICAO,
            operador=RegraCategoriaCondicao.Operador.CONTEM,
            valor="MERCADO",
        )
        tipo = Titulo.Tipo.RECEBER if registro.valor > 0 else Titulo.Tipo.PAGAR
        self.criar_titulo_previsto(
            abs(registro.valor),
            registro.data,
            tipo=tipo,
        )

        lote = self.criar_lote(documento)
        item = lote.itens.get(posicao=1)

        self.assertEqual(item.acao, ItemImportacao.Acao.VINCULAR)
        self.assertEqual(item.status, ItemImportacao.Status.PENDENTE)
        self.assertIsNone(item.categoria)
        self.assertNotIn("Regra que seria aplicável", item.motivo_status)

    def test_conta_preseleciona_movimento_exato_e_permite_criar(self):
        documento = OFXParser.parse(OFX_CONTA)
        registro = documento.transacoes[0]
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.RECEITA if registro.valor > 0 else NaturezaEvento.DESPESA,
            descricao="Movimento pré-existente",
            data=registro.data,
            valor=abs(registro.valor),
            situacao=Situacao.REALIZADO,
        )
        movimento = MovimentoConta.objects.create(
            conta=self.conta,
            evento=evento,
            data_efetiva=registro.data,
            valor=registro.valor,
        )

        lote = self.criar_lote(documento)
        item = lote.itens.get(posicao=1)
        self.assertEqual(item.status, ItemImportacao.Status.PENDENTE)
        self.assertEqual(item.acao, ItemImportacao.Acao.VINCULAR)
        self.assertEqual(item.movimento_conta_id, movimento.pk)
        self.assertIn("Sugestão automática", item.motivo_status)

        self.client.force_login(self.usuario)
        resposta = self.client.post(
            f"/importar/{lote.pk}/revisao/",
            {
                "item_id": item.pk,
                "data_revisada": "",
                "valor_revisado": "",
                "descricao_revisada": "",
                "natureza": item.natureza,
                "categoria": "",
                "pessoa": "",
                "ambito": item.ambito,
                "acao": ItemImportacao.Acao.CRIAR,
                "observacao": "",
                "titulo_alvo_id": "",
                "movimento_alvo_id": "",
                "parcela_alvo_id": "",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(resposta.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.status, ItemImportacao.Status.NOVO)
        self.assertIsNone(item.movimento_conta_id)
        self.assertIsNone(item.evento_id)

    def test_conta_preseleciona_titulo_exato(self):
        documento = OFXParser.parse(OFX_CONTA)
        registro = documento.transacoes[0]
        titulo = self.criar_titulo_previsto(
            str(abs(registro.valor)),
            registro.data,
            tipo=Titulo.Tipo.RECEBER if registro.valor > 0 else Titulo.Tipo.PAGAR,
        )

        item = self.criar_lote(documento).itens.get(posicao=1)

        self.assertEqual(item.status, ItemImportacao.Status.PENDENTE)
        self.assertEqual(item.acao, ItemImportacao.Acao.VINCULAR)
        self.assertEqual(item.titulo_alvo_id, titulo.pk)
        self.assertIsNone(item.evento_id)

    def test_conta_com_destinos_exatos_multiplos_exige_selecao_manual(self):
        documento = OFXParser.parse(OFX_CONTA)
        registro = documento.transacoes[0]
        movimentos = []
        for indice in range(2):
            evento = EventoFinanceiro.objects.create(
                usuario=self.usuario,
                natureza=NaturezaEvento.RECEITA if registro.valor > 0 else NaturezaEvento.DESPESA,
                descricao=f"Movimento duplicado {indice}",
                data=registro.data,
                valor=abs(registro.valor),
                situacao=Situacao.REALIZADO,
            )
            movimentos.append(MovimentoConta.objects.create(
                conta=self.conta,
                evento=evento,
                data_efetiva=registro.data,
                valor=registro.valor,
            ))

        item = self.criar_lote(documento).itens.get(posicao=1)
        self.assertEqual(item.status, ItemImportacao.Status.PENDENTE)
        self.assertEqual(item.acao, ItemImportacao.Acao.VINCULAR)
        self.assertIsNone(item.movimento_conta_id)
        self.assertIn("Há 2 destinos", item.motivo_status)
        candidatos = buscar_candidatos_vinculo(item, self.usuario, tipo="MOVIMENTO")
        self.assertEqual(candidatos["total"], 2)
        self.assertCountEqual(
            [candidato["id"] for candidato in candidatos["resultados"]],
            [movimento.pk for movimento in movimentos],
        )

    def test_cartao_projeta_parcelas_e_permite_conciliar_proxima_manualmente(self):
        documento = OFXParser.parse(OFX_CARTAO)
        primeiro = self.criar_lote(documento, ciclo="2026-10")
        confirmar_importacao(primeiro.pk, self.usuario)
        compra = CompraCartao.objects.get()
        self.assertTrue(compra.valor_total_estimado)
        self.assertEqual(compra.valor_total, Decimal("1200.00"))
        self.assertEqual(compra.parcelas.count(), 11)
        self.assertEqual(compra.parcelas.filter(situacao=Situacao.PREVISTO).count(), 10)
        self.assertFalse(compra.parcelas.filter(vencimento__isnull=True).exists())
        self.assertEqual(compra.parcelas.order_by("numero").first().vencimento, date(2026, 10, 10))
        self.assertEqual(compra.parcelas.order_by("numero").last().vencimento, date(2027, 8, 10))
        self.assertEqual(Fatura.objects.exclude(status=Fatura.Status.ABERTA).count(), 0)

        proximo_ofx = OFX_CARTAO.replace("20261010", "20261110").replace("02/12", "03/12").replace("<FITID>C1", "<FITID>C2")
        segundo = self.criar_lote(OFXParser.parse(proximo_ofx), ciclo="2026-11")
        item = segundo.itens.get()
        self.assertEqual(item.status, ItemImportacao.Status.NOVO)
        self.assertEqual(item.acao, ItemImportacao.Acao.CRIAR)
        self.assertIsNone(item.parcela_id)
        parcela = ParcelaCartao.objects.get(compra=compra, numero=3)
        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, parcela_id=parcela.pk)
        item.save()
        confirmar_importacao(segundo.pk, self.usuario)
        self.assertEqual(CompraCartao.objects.count(), 1)
        self.assertEqual(ParcelaCartao.objects.get(compra=compra, numero=3).situacao, Situacao.REALIZADO)

    def test_revisao_cartao_exibe_seletor_de_evento_previsto(self):
        self.cartao.dia_fechamento = 25
        self.cartao.save(update_fields=["dia_fechamento"])
        lote = self.criar_lote(OFXParser.parse(OFX_CARTAO), ciclo="2026-10")
        self.client.force_login(self.usuario)

        resposta = self.client.get(f"/importar/{lote.pk}/revisao/")

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Buscar evento ou parcela prevista")
        self.assertContains(resposta, 'name="parcela_alvo_id"', html=False)

    def test_revisao_cartao_exibe_aviso_de_ciclo_sem_bloquear(self):
        self.cartao.dia_fechamento = 25
        self.cartao.save(update_fields=["dia_fechamento"])
        lote = self.criar_lote(OFXParser.parse(OFX_CARTAO), ciclo="2026-09")
        self.client.force_login(self.usuario)

        resposta = self.client.get(f"/importar/{lote.pk}/revisao/")

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "ciclo esperado é 12/2026")
        self.assertContains(resposta, "parcela 2/12")
        self.assertContains(resposta, "ciclo informado será mantido")
        self.assertEqual(lote.ciclo, "2026-09")

    def test_revisao_cartao_calcula_ciclo_pelo_fechamento_e_vencimento(self):
        self.cartao.dia_fechamento = 1
        self.cartao.dia_vencimento = 9
        self.cartao.save(update_fields=["dia_fechamento", "dia_vencimento"])
        ofx = OFX_CARTAO.replace("20261010", "20261022").replace("02/12", "01/02")
        lote = self.criar_lote(OFXParser.parse(ofx), ciclo="2026-10")
        self.client.force_login(self.usuario)

        resposta = self.client.get(f"/importar/{lote.pk}/revisao/")

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "ciclo esperado é 11/2026")
        self.assertNotContains(resposta, "ciclo esperado é 12/2026")
        self.assertContains(resposta, "Parcela 1/2")
        self.assertContains(resposta, "ciclo informado será mantido")
        self.assertEqual(lote.ciclo, "2026-10")

    def test_revisao_cartao_nao_avisa_quando_ciclo_da_parcela_corresponde(self):
        self.cartao.dia_fechamento = 25
        self.cartao.save(update_fields=["dia_fechamento"])
        lote = self.criar_lote(OFXParser.parse(OFX_CARTAO), ciclo="2026-12")
        self.client.force_login(self.usuario)

        resposta = self.client.get(f"/importar/{lote.pk}/revisao/")

        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, "ciclo esperado é")

        item = lote.itens.get()
        resposta_ajax = self.client.post(
            f"/importar/{lote.pk}/revisao/",
            {
                "item_id": item.pk,
                "data_revisada": "",
                "valor_revisado": "",
                "descricao_revisada": "",
                "natureza": NaturezaEvento.DESPESA,
                "categoria": "",
                "pessoa": "",
                "ambito": Ambito.NAO_DEFINIDO,
                "acao": ItemImportacao.Acao.CRIAR,
                "observacao": "",
                "titulo_alvo_id": "",
                "movimento_alvo_id": "",
                "parcela_alvo_id": "",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(resposta_ajax.status_code, 200)
        self.assertEqual(resposta_ajax.json()["item"]["aviso_ciclo"], "")

    def test_salvar_vinculo_cartao_por_ajax_aceita_parcela_prevista(self):
        parcela = self.criar_parcela_prevista_cartao()
        lote = self.criar_lote(OFXParser.parse(OFX_CARTAO), ciclo="2026-10")
        item = lote.itens.get()
        self.client.force_login(self.usuario)

        resposta = self.client.post(
            f"/importar/{lote.pk}/revisao/",
            {
                "item_id": item.pk,
                "data_revisada": "",
                "valor_revisado": "",
                "descricao_revisada": "",
                "natureza": NaturezaEvento.DESPESA,
                "categoria": "",
                "pessoa": "",
                "ambito": Ambito.NAO_DEFINIDO,
                "acao": ItemImportacao.Acao.VINCULAR,
                "observacao": "",
                "titulo_alvo_id": "",
                "movimento_alvo_id": "",
                "parcela_alvo_id": parcela.pk,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["ok"])
        item.refresh_from_db()
        self.assertEqual(item.parcela_id, parcela.pk)
        self.assertEqual(item.evento_id, parcela.compra.evento_id)
        self.assertEqual(item.status, ItemImportacao.Status.CONCILIADO)

    def test_vinculo_manual_cartao_realiza_parcela_sem_duplicar_evento(self):
        parcela = self.criar_parcela_prevista_cartao()
        lote = self.criar_lote(OFXParser.parse(OFX_CARTAO), ciclo="2026-10")
        item = lote.itens.get()
        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, parcela_id=parcela.pk)
        item.save()

        confirmar_importacao(lote.pk, self.usuario)

        parcela.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(parcela.situacao, Situacao.REALIZADO)
        self.assertEqual(parcela.valor, Decimal("100.00"))
        self.assertEqual(item.evento_id, parcela.compra.evento_id)
        self.assertEqual(EventoFinanceiro.objects.count(), 1)
        self.assertEqual(CompraCartao.objects.count(), 1)

    def test_busca_cartao_retorna_parcela_prevista_e_reimporta_sem_duplicar(self):
        parcela = self.criar_parcela_prevista_cartao(data_compra=date(2026, 10, 10))
        documento = OFXParser.parse(OFX_CARTAO)
        primeiro = self.criar_lote(documento, ciclo="2026-10")
        item = primeiro.itens.get()
        self.assertEqual(item.status, ItemImportacao.Status.PENDENTE)
        self.assertEqual(item.parcela_id, parcela.pk)
        self.assertIn("Sugestão automática", item.motivo_status)
        candidatos = buscar_candidatos_vinculo(item, self.usuario)
        self.assertEqual(candidatos["resultados"][0]["tipo"], "PARCELA")
        self.assertEqual(candidatos["resultados"][0]["id"], parcela.pk)

        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, parcela_id=parcela.pk)
        item.save()
        confirmar_importacao(primeiro.pk, self.usuario)

        segundo = self.criar_lote(documento, nome="cartao-reimportado.ofx", ciclo="2026-10")
        item_reimportado = segundo.itens.get()
        self.assertEqual(item_reimportado.parcela_id, parcela.pk)
        confirmar_importacao(segundo.pk, self.usuario)
        self.assertEqual(EventoFinanceiro.objects.count(), 1)
        self.assertEqual(CompraCartao.objects.count(), 1)

    def test_buscar_em_todos_cartao_amplia_resultado_sem_sair_do_ciclo_aberto(self):
        parcela = self.criar_parcela_prevista_cartao(
            data_compra=date(2026, 9, 1),
            valor="275.00",
        )
        lote = self.criar_lote(OFXParser.parse(OFX_CARTAO), ciclo="2026-10")
        item = lote.itens.get()

        sugestoes = buscar_candidatos_vinculo(item, self.usuario, tipo="PARCELA")
        todos = buscar_candidatos_vinculo(
            item,
            self.usuario,
            tipo="PARCELA",
            escopo="TODOS",
        )

        self.assertNotIn(parcela.pk, [candidato["id"] for candidato in sugestoes["resultados"]])
        self.assertIn(parcela.pk, [candidato["id"] for candidato in todos["resultados"]])

    def test_busca_e_vinculo_cartao_rejeitam_fatura_nao_aberta(self):
        parcela = self.criar_parcela_prevista_cartao(
            data_compra=date(2026, 10, 10),
            status_fatura=Fatura.Status.FECHADA,
        )
        lote = self.criar_lote(OFXParser.parse(OFX_CARTAO), ciclo="2026-10")
        item = lote.itens.get()

        resultado = buscar_candidatos_vinculo(
            item,
            self.usuario,
            tipo="PARCELA",
            escopo="TODOS",
        )

        self.assertNotIn(parcela.pk, [candidato["id"] for candidato in resultado["resultados"]])
        item.acao = ItemImportacao.Acao.VINCULAR
        with self.assertRaisesMessage(
            ValidationError,
            "A parcela prevista selecionada não está disponível para este item da importação.",
        ):
            selecionar_vinculo_item(item, self.usuario, parcela_id=parcela.pk)

    def test_desfazer_remove_apenas_registros_criados(self):
        documento = OFXParser.parse(OFX_CONTA)
        lote = self.criar_lote(documento)
        confirmar_importacao(lote.pk, self.usuario)
        desfazer_importacao(lote.pk, self.usuario)
        lote.refresh_from_db()
        self.assertEqual(lote.status, Importacao.Status.DESFEITA)
        self.assertEqual(EventoFinanceiro.objects.count(), 0)
        self.assertEqual(lote.itens.count(), 2)

    def test_desfazer_cartao_remove_parcelas_e_faturas_futuras(self):
        documento = OFXParser.parse(OFX_CARTAO)
        lote = self.criar_lote(documento, ciclo="2026-10")
        confirmar_importacao(lote.pk, self.usuario)

        self.assertEqual(CompraCartao.objects.count(), 1)
        self.assertEqual(ParcelaCartao.objects.count(), 11)
        self.assertEqual(Fatura.objects.count(), 11)

        desfazer_importacao(lote.pk, self.usuario)

        lote.refresh_from_db()
        self.assertEqual(lote.status, Importacao.Status.DESFEITA)
        self.assertEqual(EventoFinanceiro.objects.count(), 0)
        self.assertEqual(CompraCartao.objects.count(), 0)
        self.assertEqual(ParcelaCartao.objects.count(), 0)
        self.assertEqual(Fatura.objects.count(), 0)

    def test_pagamento_debito_em_conta_no_cartao_e_debito_conta_usam_o_mesmo_evento(self):
        ofx_cartao = OFX_CARTAO.replace("LOJA TESTE 02/12", "PAGAMENTO DEBITO EM CONTA").replace("-100.00", "500.00")
        lote_cartao = self.criar_lote(OFXParser.parse(ofx_cartao), ciclo="2026-10")
        self.assertEqual(lote_cartao.itens.get().natureza, NaturezaEvento.PAGAMENTO_FATURA)
        confirmar_importacao(lote_cartao.pk, self.usuario)
        self.assertEqual(EventoFinanceiro.objects.count(), 1)
        self.assertEqual(CompraCartao.objects.count(), 0)

        ofx_conta = b"""OFXHEADER:100
ENCODING:USASCII
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKACCTFROM><BANKID>756<ACCTID>12345</BANKACCTFROM>
<BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261010<TRNAMT>-500.00<FITID>PAG1<MEMO>PAGAMENTO FATURA</STMTTRN></BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""
        lote_conta = self.criar_lote(OFXParser.parse(ofx_conta))
        self.assertEqual(lote_conta.itens.get().status, ItemImportacao.Status.CONCILIADO)
        confirmar_importacao(lote_conta.pk, self.usuario)
        self.assertEqual(EventoFinanceiro.objects.count(), 1)
        self.assertEqual(MovimentoConta.objects.count(), 1)

    def test_vinculo_manual_quita_titulo_previsto_sem_duplicar_previsao(self):
        titulo = self.criar_titulo_previsto("2500.00", date(2026, 10, 6))
        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(valor=Decimal("2500.00"))
        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, titulo_id=titulo.pk)
        item.save()

        confirmar_importacao(lote.pk, self.usuario)

        titulo.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(titulo.saldo_aberto, Decimal("0.00"))
        self.assertEqual(titulo.status, Titulo.Status.LIQUIDADO)
        self.assertFalse(titulo.ativo)
        self.assertNotEqual(item.evento_id, titulo.evento_id)
        self.assertEqual(item.movimento_conta.valor, Decimal("2500.00"))
        self.assertEqual(item.movimento_conta.evento.situacao, Situacao.REALIZADO)
        self.assertEqual(Liquidacao.objects.get().titulo, titulo)

    def _selecionar_titulos_multiplos_9042(self):
        documento = OFXParser.parse(OFX_CONTA.replace(b"2500.00", b"9042.00"))
        lote = self.criar_lote(documento, nome="credito-multiplo.ofx")
        titulos = [
            self.criar_titulo_previsto("7000.00", date(2026, 10, 16), descricao="Boleto cliente A"),
            self.criar_titulo_previsto("1500.00", date(2026, 10, 16), descricao="Boleto cliente B"),
            self.criar_titulo_previsto("542.00", date(2026, 10, 16), descricao="Boleto cliente C"),
        ]
        item = lote.itens.get(valor=Decimal("9042.00"))
        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, titulos_ids=[titulo.pk for titulo in titulos])
        item.save()
        sincronizar_titulos_multiplos(item, [titulo.pk for titulo in titulos])
        return lote, item, titulos, documento

    def test_vinculo_multiplo_quita_tres_titulos_com_um_movimento(self):
        lote, item, titulos, _ = self._selecionar_titulos_multiplos_9042()

        self.assertEqual(VinculoTituloImportacao.objects.filter(item=item).count(), 3)
        candidatos = buscar_candidatos_vinculo(item, self.usuario, tipo="TITULO", escopo="TODOS")
        self.assertTrue({titulo.pk for titulo in titulos}.issubset({candidato["id"] for candidato in candidatos["resultados"]}))
        confirmar_importacao(lote.pk, self.usuario)

        item.refresh_from_db()
        self.assertEqual(item.movimento_conta.valor, Decimal("9042.00"))
        self.assertEqual(Liquidacao.objects.filter(movimento_conta=item.movimento_conta).count(), 3)
        self.assertEqual(VinculoTituloImportacao.objects.filter(item=item, liquidacao__isnull=False).count(), 3)
        for titulo in titulos:
            titulo.refresh_from_db()
            self.assertEqual(titulo.saldo_aberto, Decimal("0.00"))
            self.assertEqual(titulo.status, Titulo.Status.LIQUIDADO)
            self.assertEqual(titulo.evento.situacao, Situacao.PREVISTO)

    def test_vinculo_multiplo_revalida_saldo_alterado_antes_da_confirmacao(self):
        lote, _, titulos, _ = self._selecionar_titulos_multiplos_9042()
        titulos[0].saldo_aberto = Decimal("6999.00")
        titulos[0].save(update_fields=["saldo_aberto"])

        with self.assertRaisesMessage(ValidationError, "exatamente igual"):
            confirmar_importacao(lote.pk, self.usuario)

        self.assertEqual(MovimentoConta.objects.filter(valor=Decimal("9042.00")).count(), 0)
        self.assertEqual(Liquidacao.objects.filter(titulo__in=titulos).count(), 0)

    def test_vinculo_multiplo_rejeita_soma_inexata_e_desfazer_restaura_titulos(self):
        lote, item, titulos, _ = self._selecionar_titulos_multiplos_9042()
        with self.assertRaisesMessage(ValidationError, "exatamente igual"):
            selecionar_vinculo_item(item, self.usuario, titulos_ids=[titulos[0].pk, titulos[1].pk])

        confirmar_importacao(lote.pk, self.usuario)
        desfazer_importacao(lote.pk, self.usuario)

        self.assertEqual(Liquidacao.objects.filter(titulo__in=titulos).count(), 0)
        self.assertEqual(VinculoTituloImportacao.objects.filter(item=item, liquidacao__isnull=False).count(), 0)
        for titulo, valor in zip(titulos, ("7000.00", "1500.00", "542.00")):
            titulo.refresh_from_db()
            self.assertEqual(titulo.saldo_aberto, Decimal(valor))
            self.assertEqual(titulo.status, Titulo.Status.ABERTO)

    def test_reimportacao_de_vinculo_multiplo_nao_duplica_baixas(self):
        lote, item, titulos, documento = self._selecionar_titulos_multiplos_9042()
        confirmar_importacao(lote.pk, self.usuario)
        movimento_id = ItemImportacao.objects.get(pk=item.pk).movimento_conta_id

        reimportado = self.criar_lote(documento, nome="credito-multiplo-reimportado.ofx")
        item_reimportado = reimportado.itens.get(valor=Decimal("9042.00"))
        self.assertEqual(VinculoTituloImportacao.objects.filter(item=item_reimportado).count(), 3)
        confirmar_importacao(reimportado.pk, self.usuario)

        item_reimportado.refresh_from_db()
        self.assertEqual(item_reimportado.movimento_conta_id, movimento_id)
        self.assertEqual(Liquidacao.objects.filter(titulo__in=titulos).count(), 3)

    def test_reimportacao_preserva_baixa_do_titulo_sem_duplicar(self):
        documento = OFXParser.parse(OFX_CONTA)
        titulo = self.criar_titulo_previsto("2500.00", date(2026, 10, 6))
        primeiro = self.criar_lote(documento)
        item = primeiro.itens.get(valor=Decimal("2500.00"))
        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, titulo_id=titulo.pk)
        item.save()
        confirmar_importacao(primeiro.pk, self.usuario)
        item.refresh_from_db()
        movimento_id = item.movimento_conta_id

        segundo = self.criar_lote(documento, nome="reimportacao.ofx")
        item_reimportado = segundo.itens.get(valor=Decimal("2500.00"))
        self.assertEqual(item_reimportado.titulo_alvo_id, titulo.pk)
        self.assertIsNotNone(item_reimportado.evidencia_liquidacao_id)
        confirmar_importacao(segundo.pk, self.usuario)

        item_reimportado.refresh_from_db()
        self.assertEqual(item_reimportado.movimento_conta_id, movimento_id)
        self.assertEqual(Liquidacao.objects.filter(titulo=titulo).count(), 1)
        self.assertEqual(MovimentoConta.objects.count(), 2)

    def test_vinculo_manual_realiza_baixa_parcial_e_desfazer_restaura_titulo(self):
        titulo = self.criar_titulo_previsto("3000.00", date(2026, 10, 6))
        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(valor=Decimal("2500.00"))
        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, titulo_id=titulo.pk)
        item.save()
        confirmar_importacao(lote.pk, self.usuario)

        titulo.refresh_from_db()
        self.assertEqual(titulo.saldo_aberto, Decimal("500.00"))
        self.assertEqual(titulo.status, Titulo.Status.ABERTO)
        self.assertTrue(titulo.ativo)

        desfazer_importacao(lote.pk, self.usuario)
        titulo.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(titulo.saldo_aberto, Decimal("3000.00"))
        self.assertEqual(titulo.status, Titulo.Status.ABERTO)
        self.assertTrue(titulo.ativo)
        self.assertIsNone(item.titulo_alvo_id)
        self.assertIsNone(item.movimento_conta_id)
        self.assertEqual(Liquidacao.objects.count(), 0)

    def test_vinculo_manual_rejeita_valor_maior_que_saldo(self):
        titulo = self.criar_titulo_previsto("2000.00", date(2026, 10, 6))
        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(valor=Decimal("2500.00"))
        item.acao = ItemImportacao.Acao.VINCULAR

        with self.assertRaisesMessage(ValidationError, "não está disponível"):
            selecionar_vinculo_item(item, self.usuario, titulo_id=titulo.pk)

    def test_alteracao_do_saldo_antes_da_confirmacao_cancela_o_lote(self):
        titulo = self.criar_titulo_previsto("2500.00", date(2026, 10, 6))
        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(valor=Decimal("2500.00"))
        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, titulo_id=titulo.pk)
        item.save()
        titulo.saldo_aberto = Decimal("2000.00")
        titulo.save(update_fields=["saldo_aberto"])

        with self.assertRaisesMessage(ValidationError, "não está disponível"):
            confirmar_importacao(lote.pk, self.usuario)

        lote.refresh_from_db()
        self.assertEqual(lote.status, Importacao.Status.EM_REVISAO)
        self.assertEqual(MovimentoConta.objects.count(), 0)
        self.assertEqual(Liquidacao.objects.count(), 0)

    def test_vinculo_manual_debito_cria_movimento_negativo(self):
        titulo = self.criar_titulo_previsto(
            "125.50",
            date(2026, 10, 5),
            descricao="Despesa prevista",
            tipo=Titulo.Tipo.PAGAR,
        )
        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(valor=Decimal("-125.50"))
        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, titulo_id=titulo.pk)
        item.save()

        confirmar_importacao(lote.pk, self.usuario)

        item.refresh_from_db()
        titulo.refresh_from_db()
        self.assertEqual(item.movimento_conta.valor, Decimal("-125.50"))
        self.assertEqual(titulo.saldo_aberto, Decimal("0.00"))

    def test_baixa_parcial_copia_rateios_preservando_centavos(self):
        titulo = self.criar_titulo_previsto("3000.00", date(2026, 10, 6))
        categoria_a = Categoria.objects.create(usuario=self.usuario, nome="Serviço A", tipo=Categoria.Tipo.RECEITA)
        categoria_b = Categoria.objects.create(usuario=self.usuario, nome="Serviço B", tipo=Categoria.Tipo.RECEITA)
        RateioCategoria.objects.create(evento=titulo.evento, categoria=categoria_a, valor=Decimal("1000.01"))
        RateioCategoria.objects.create(evento=titulo.evento, categoria=categoria_b, valor=Decimal("1999.99"))
        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(valor=Decimal("2500.00"))
        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, titulo_id=titulo.pk)
        item.save()

        confirmar_importacao(lote.pk, self.usuario)

        item.refresh_from_db()
        rateios = list(item.evento.rateios.order_by("id").values_list("valor", flat=True))
        self.assertEqual(len(rateios), 2)
        self.assertEqual(sum(rateios, Decimal("0.00")), Decimal("2500.00"))

    def test_vinculo_manual_com_movimento_existente_nao_cria_duplicado(self):
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Compra já registrada",
            data=date(2026, 10, 5),
            valor=Decimal("125.50"),
            situacao=Situacao.REALIZADO,
        )
        movimento = MovimentoConta.objects.create(
            conta=self.conta,
            evento=evento,
            data_efetiva=evento.data,
            valor=Decimal("-125.50"),
            historico_original="Histórico manual diferente",
        )
        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(valor=Decimal("-125.50"))
        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, movimento_id=movimento.pk)
        item.save()

        confirmar_importacao(lote.pk, self.usuario)

        item.refresh_from_db()
        self.assertEqual(item.movimento_conta, movimento)
        self.assertEqual(MovimentoConta.objects.filter(valor=Decimal("-125.50")).count(), 1)

    def test_movimento_alterado_antes_da_confirmacao_e_revalidado(self):
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Compra registrada",
            data=date(2026, 10, 5),
            valor=Decimal("125.50"),
            situacao=Situacao.REALIZADO,
        )
        movimento = MovimentoConta.objects.create(
            conta=self.conta,
            evento=evento,
            data_efetiva=evento.data,
            valor=Decimal("-125.50"),
        )
        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(valor=Decimal("-125.50"))
        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, movimento_id=movimento.pk)
        item.save()
        outra_conta = Conta.objects.create(usuario=self.usuario, nome="Outra conta")
        movimento.conta = outra_conta
        movimento.save(update_fields=["conta"])

        with self.assertRaisesMessage(ValidationError, "não está disponível"):
            confirmar_importacao(lote.pk, self.usuario)

        lote.refresh_from_db()
        self.assertEqual(lote.status, Importacao.Status.EM_REVISAO)

    def test_trocar_vinculo_manual_por_criar_limpa_destino(self):
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Compra registrada",
            data=date(2026, 10, 5),
            valor=Decimal("125.50"),
            situacao=Situacao.REALIZADO,
        )
        movimento = MovimentoConta.objects.create(
            conta=self.conta,
            evento=evento,
            data_efetiva=evento.data,
            valor=Decimal("-125.50"),
        )
        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(valor=Decimal("-125.50"))
        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, movimento_id=movimento.pk)
        item.save()

        item.acao = ItemImportacao.Acao.CRIAR
        selecionar_vinculo_item(item, self.usuario)

        self.assertIsNone(item.evento_id)
        self.assertIsNone(item.movimento_conta_id)

    def test_busca_prioriza_titulo_por_valor_data_e_categoria(self):
        categoria = Categoria.objects.create(usuario=self.usuario, nome="Serviços", tipo=Categoria.Tipo.RECEITA)
        principal = self.criar_titulo_previsto(
            "2500.00",
            date(2026, 10, 6),
            descricao="Cliente principal",
            categoria=categoria,
        )
        self.criar_titulo_previsto("3000.00", date(2026, 11, 20), descricao="Outro cliente")
        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(valor=Decimal("2500.00"))
        item.categoria = categoria
        resultado = buscar_candidatos_vinculo(item, self.usuario)

        self.assertGreaterEqual(resultado["total"], 1)
        self.assertEqual(resultado["resultados"][0]["tipo"], "TITULO")
        self.assertEqual(resultado["resultados"][0]["id"], principal.pk)
        self.assertIn("valor exato", resultado["resultados"][0]["motivos"])

    def test_buscar_em_todos_inclui_movimento_de_mesmo_sinal_com_outro_valor(self):
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.RECEITA,
            descricao="Crédito sem correspondência forte",
            data=date(2026, 1, 1),
            valor=Decimal("100.00"),
            situacao=Situacao.REALIZADO,
        )
        movimento = MovimentoConta.objects.create(
            conta=self.conta,
            evento=evento,
            data_efetiva=evento.data,
            valor=Decimal("100.00"),
        )
        lote = self.criar_lote(OFXParser.parse(OFX_CONTA))
        item = lote.itens.get(valor=Decimal("2500.00"))

        resultado = buscar_candidatos_vinculo(item, self.usuario, tipo="MOVIMENTO", escopo="TODOS")

        self.assertIn(movimento.pk, [candidato["id"] for candidato in resultado["resultados"]])


class ImportacaoViewSecurityTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(self.media_dir.cleanup)
        user_model = get_user_model()
        self.dono = user_model.objects.create_user(username="dono", password="senha")
        self.outro = user_model.objects.create_user(username="outro", password="senha")
        conta = Conta.objects.create(usuario=self.dono, nome="Conta")
        self.cartao = Cartao.objects.create(
            usuario=self.dono, nome="Cartão", identificador_externo="9999", dia_vencimento=10
        )
        self.lote = Importacao.objects.create(
            usuario=self.dono,
            arquivo="importacoes/testes/teste.ofx",
            nome_arquivo="teste.ofx",
            hash_arquivo="b" * 64,
            origem=Importacao.Origem.CONTA,
            conta=conta,
            status=Importacao.Status.EM_REVISAO,
        )

    def test_usuario_nao_acessa_lote_de_outro(self):
        self.client.force_login(self.outro)
        resposta = self.client.get(f"/importar/{self.lote.pk}/revisao/")
        self.assertEqual(resposta.status_code, 404)

    def test_importacao_colore_debitos_creditos_e_totais(self):
        ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            data=date(2026, 10, 5),
            valor=Decimal("-50.00"),
            descricao_original="Débito colorido",
            status=ItemImportacao.Status.NOVO,
        )
        ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=2,
            data=date(2026, 10, 6),
            valor=Decimal("80.00"),
            descricao_original="Crédito colorido",
            status=ItemImportacao.Status.NOVO,
        )
        self.lote.total_debitos = Decimal("50.00")
        self.lote.total_creditos = Decimal("80.00")
        self.lote.total_liquido = Decimal("30.00")
        self.lote.save(update_fields=["total_debitos", "total_creditos", "total_liquido"])
        self.client.force_login(self.dono)

        revisao = self.client.get(f"/importar/{self.lote.pk}/revisao/")

        self.assertContains(revisao, 'class="amount-negative">R$ -50,00')
        self.assertContains(revisao, 'class="amount-positive">R$ 80,00')
        self.assertContains(revisao, 'class="amount-negative">R$ 50,00')

        self.lote.status = Importacao.Status.CONFIRMADA
        self.lote.save(update_fields=["status"])
        resultado = self.client.get(f"/importar/{self.lote.pk}/resultado/")

        self.assertContains(resultado, 'class="money amount-negative">R$ -50,00')
        self.assertContains(resultado, 'class="money amount-positive">R$ 80,00')

    def test_revisao_e_aplicacao_em_lote_exibem_categoria_filha_com_pai(self):
        pai = Categoria.objects.create(usuario=self.dono, nome="Receitas")
        filha = Categoria.objects.create(
            usuario=self.dono,
            nome="Prestação de serviços",
            categoria_pai=pai,
        )
        item = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            valor=Decimal("100.00"),
            descricao_original="Serviço prestado",
            categoria=filha,
            status=ItemImportacao.Status.NOVO,
        )
        self.client.force_login(self.dono)

        resposta = self.client.get(reverse("importacao_revisao", args=[self.lote.pk]))
        formulario_item = ItemImportacaoForm(instance=item, usuario=self.dono)
        formulario_lote = AplicacaoLoteForm(usuario=self.dono)

        self.assertContains(resposta, "Prestação de serviços (Receitas)", count=2)
        self.assertContains(
            resposta,
            f'<option value="{filha.pk}" selected>Prestação de serviços (Receitas)</option>',
            html=True,
        )
        self.assertIn("Prestação de serviços (Receitas)", str(formulario_item["categoria"]))
        self.assertIn("Prestação de serviços (Receitas)", str(formulario_lote["categoria"]))

    def test_busca_de_candidatos_exige_proprietario_do_lote(self):
        item = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            data=date(2026, 10, 6),
            valor=Decimal("10.00"),
            descricao_original="Receita",
            status=ItemImportacao.Status.NOVO,
        )
        url = f"/importar/{self.lote.pk}/itens/{item.pk}/candidatos/"
        self.client.force_login(self.outro)
        self.assertEqual(self.client.get(url).status_code, 404)

        self.client.force_login(self.dono)
        resposta = self.client.get(url)
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["ok"])

    def test_confirmacao_exige_post(self):
        self.client.force_login(self.dono)
        resposta = self.client.get(f"/importar/{self.lote.pk}/confirmar/")
        self.assertEqual(resposta.status_code, 405)

    def test_exclusao_de_lote_em_revisao_exige_post_e_proprietario(self):
        self.client.force_login(self.dono)
        resposta_get = self.client.get(f"/importar/{self.lote.pk}/excluir/")
        self.assertEqual(resposta_get.status_code, 405)

        self.client.force_login(self.outro)
        resposta_outro = self.client.post(f"/importar/{self.lote.pk}/excluir/")
        self.assertEqual(resposta_outro.status_code, 404)
        self.assertTrue(Importacao.objects.filter(pk=self.lote.pk).exists())

    def test_exclusao_remove_lote_em_revisao_e_seus_itens(self):
        ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            valor=Decimal("-10.00"),
            descricao_original="Item temporário",
        )
        self.client.force_login(self.dono)

        resposta = self.client.post(f"/importar/{self.lote.pk}/excluir/")

        self.assertRedirects(resposta, "/importar/ofx/")
        self.assertFalse(Importacao.objects.filter(pk=self.lote.pk).exists())
        self.assertEqual(ItemImportacao.objects.count(), 0)

    def test_exclusao_nao_remove_lote_confirmado(self):
        self.lote.status = Importacao.Status.CONFIRMADA
        self.lote.save(update_fields=["status"])
        self.client.force_login(self.dono)

        resposta = self.client.post(f"/importar/{self.lote.pk}/excluir/")

        self.assertEqual(resposta.status_code, 404)
        self.assertTrue(Importacao.objects.filter(pk=self.lote.pk).exists())

    def test_salvar_item_por_ajax_retorna_json_sem_redirecionar(self):
        item = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            valor=Decimal("-10.00"),
            descricao_original="Compra original",
            natureza=NaturezaEvento.DESPESA,
            ambito=Ambito.NAO_DEFINIDO,
            acao=ItemImportacao.Acao.CRIAR,
            status=ItemImportacao.Status.NOVO,
        )
        self.client.force_login(self.dono)

        resposta = self.client.post(
            f"/importar/{self.lote.pk}/revisao/",
            {
                "item_id": item.pk,
                "data_revisada": "",
                "valor_revisado": "",
                "descricao_revisada": "Compra revisada",
                "natureza": NaturezaEvento.DESPESA,
                "categoria": "",
                "pessoa": "",
                "ambito": Ambito.NAO_DEFINIDO,
                "acao": ItemImportacao.Acao.CRIAR,
                "observacao": "Revisado manualmente",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["ok"])
        self.assertEqual(resposta.json()["item"]["id"], item.pk)
        self.assertEqual(resposta.json()["contagens"]["NOVO"], 1)
        item.refresh_from_db()
        self.assertEqual(item.descricao_revisada, "Compra revisada")
        self.assertEqual(item.observacao, "Revisado manualmente")
        self.assertIsNotNone(item.revisado_em)
        self.assertTrue(resposta.json()["item"]["revisado"])

    def test_troca_para_criar_aplica_regra_sem_substituir_natureza_revisada(self):
        categoria = Categoria.objects.create(usuario=self.dono, nome="Transporte")
        regra = RegraCategoria.objects.create(
            usuario=self.dono,
            nome="Aplicativos de transporte",
            prioridade=1,
            categoria=categoria,
            ambito=Ambito.PROFISSIONAL,
            natureza_sugerida=NaturezaEvento.RECEITA,
        )
        RegraCategoriaCondicao.objects.create(
            regra=regra,
            ordem=1,
            campo=RegraCategoriaCondicao.Campo.DESCRICAO,
            operador=RegraCategoriaCondicao.Operador.CONTEM,
            valor="UBER",
        )
        item = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            data=date(2026, 10, 20),
            valor=Decimal("-25.00"),
            descricao_original="UBER viagem",
            natureza=NaturezaEvento.DESPESA,
            ambito=Ambito.NAO_DEFINIDO,
            acao=ItemImportacao.Acao.VINCULAR,
            status=ItemImportacao.Status.PENDENTE,
            motivo_status="Aguardando definição do usuário.",
        )
        self.client.force_login(self.dono)

        resposta = self.client.post(
            reverse("importacao_revisao", args=[self.lote.pk]),
            {
                "item_id": item.pk,
                "data_revisada": "",
                "valor_revisado": "",
                "descricao_revisada": "",
                "natureza": NaturezaEvento.DESPESA,
                "categoria": "",
                "pessoa": "",
                "ambito": Ambito.NAO_DEFINIDO,
                "acao": ItemImportacao.Acao.CRIAR,
                "observacao": "",
                "titulo_alvo_id": "",
                "titulos_alvo_ids": "",
                "movimento_alvo_id": "",
                "parcela_alvo_id": "",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(resposta.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.categoria, categoria)
        self.assertEqual(item.ambito, Ambito.PROFISSIONAL)
        self.assertEqual(item.natureza, NaturezaEvento.DESPESA)
        self.assertIn("Aplicativos de transporte", item.motivo_status)
        self.assertIn("Revisado pelo usuário", item.motivo_status)
        self.assertEqual(resposta.json()["item"]["categoria"], categoria.pk)

    def test_aplicacao_em_lote_para_criar_executa_regra(self):
        categoria = Categoria.objects.create(usuario=self.dono, nome="Assinaturas")
        regra = RegraCategoria.objects.create(
            usuario=self.dono,
            nome="Streaming em lote",
            prioridade=1,
            categoria=categoria,
        )
        RegraCategoriaCondicao.objects.create(
            regra=regra,
            ordem=1,
            campo=RegraCategoriaCondicao.Campo.DESCRICAO,
            operador=RegraCategoriaCondicao.Operador.CONTEM,
            valor="STREAMING",
        )
        item = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            data=date(2026, 10, 20),
            valor=Decimal("-49.90"),
            descricao_original="STREAMING mensal",
            natureza=NaturezaEvento.DESPESA,
            ambito=Ambito.NAO_DEFINIDO,
            acao=ItemImportacao.Acao.VINCULAR,
            status=ItemImportacao.Status.PENDENTE,
        )
        self.client.force_login(self.dono)

        resposta = self.client.post(
            reverse("importacao_aplicar_lote", args=[self.lote.pk]),
            {
                "categoria": "",
                "pessoa": "",
                "ambito": "",
                "acao": ItemImportacao.Acao.CRIAR,
                "observacao": "",
                "somente_status": "SELECIONADOS",
                "itens_selecionados": [item.pk],
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(resposta.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.categoria, categoria)
        self.assertEqual(item.ambito, Ambito.PESSOAL)
        self.assertEqual(item.status, ItemImportacao.Status.NOVO)
        self.assertIn("Streaming em lote", item.motivo_status)
        self.assertIn("Âmbito definido como Pessoal por padrão", item.motivo_status)
        self.assertEqual(resposta.json()["items"][0]["categoria"], categoria.pk)

    def test_revisao_ajax_persiste_e_exibe_selecao_de_titulos_multiplos(self):
        item = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            data=date(2026, 10, 16),
            valor=Decimal("9042.00"),
            descricao_original="Crédito de boletos",
            natureza=NaturezaEvento.RECEITA,
            ambito=Ambito.NAO_DEFINIDO,
            acao=ItemImportacao.Acao.CRIAR,
            status=ItemImportacao.Status.NOVO,
        )
        titulos = []
        for indice, valor in enumerate(("7000.00", "1500.00", "542.00"), start=1):
            evento = EventoFinanceiro.objects.create(
                usuario=self.dono,
                natureza=NaturezaEvento.RECEITA,
                descricao=f"Título {indice}",
                data=date(2026, 10, 16),
                valor=Decimal(valor),
                situacao=Situacao.PREVISTO,
            )
            titulos.append(Titulo.objects.create(
                usuario=self.dono,
                tipo=Titulo.Tipo.RECEBER,
                descricao=evento.descricao,
                vencimento=evento.data,
                valor_original=evento.valor,
                saldo_aberto=evento.valor,
                conta=self.lote.conta,
                evento=evento,
            ))
        self.client.force_login(self.dono)

        resposta = self.client.post(
            reverse("importacao_revisao", args=[self.lote.pk]),
            {
                "item_id": item.pk,
                "data_revisada": "",
                "valor_revisado": "",
                "descricao_revisada": "",
                "natureza": NaturezaEvento.RECEITA,
                "categoria": "",
                "pessoa": "",
                "ambito": Ambito.NAO_DEFINIDO,
                "acao": ItemImportacao.Acao.VINCULAR,
                "observacao": "",
                "titulo_alvo_id": "",
                "titulos_alvo_ids": ",".join(str(titulo.pk) for titulo in titulos),
                "movimento_alvo_id": "",
                "parcela_alvo_id": "",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()["titulos_multiplos"], {"quantidade": 3, "total": "9042.00"})
        self.assertEqual(VinculoTituloImportacao.objects.filter(item=item).count(), 3)
        revisao = self.client.get(reverse("importacao_revisao", args=[self.lote.pk]))
        self.assertContains(revisao, "3 títulos selecionados")
        self.assertContains(revisao, 'name="titulos_alvo_ids"', html=False)
        self.assertContains(revisao, 'data-item-value="9042.00"', html=False)

        remocao = self.client.post(
            reverse("importacao_revisao", args=[self.lote.pk]),
            {
                "item_id": item.pk,
                "data_revisada": "",
                "valor_revisado": "",
                "descricao_revisada": "",
                "natureza": NaturezaEvento.RECEITA,
                "categoria": "",
                "pessoa": "",
                "ambito": Ambito.NAO_DEFINIDO,
                "acao": ItemImportacao.Acao.VINCULAR,
                "observacao": "",
                "titulo_alvo_id": "",
                "titulos_alvo_ids": "",
                "movimento_alvo_id": "",
                "parcela_alvo_id": "",
                "remover_vinculo": "1",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(remocao.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.status, ItemImportacao.Status.PENDENTE)
        self.assertEqual(VinculoTituloImportacao.objects.filter(item=item).count(), 0)
        self.assertIsNone(item.titulo_alvo_id)

    def test_salvar_individual_permite_alterar_item_ja_revisado(self):
        item = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            valor=Decimal("-10.00"),
            descricao_original="Compra já revisada",
            natureza=NaturezaEvento.DESPESA,
            ambito=Ambito.NAO_DEFINIDO,
            acao=ItemImportacao.Acao.CRIAR,
            status=ItemImportacao.Status.NOVO,
            observacao="Observação anterior",
            revisado_em=timezone.now(),
        )
        self.client.force_login(self.dono)

        resposta = self.client.post(
            f"/importar/{self.lote.pk}/revisao/",
            {
                "item_id": item.pk,
                "data_revisada": "",
                "valor_revisado": "",
                "descricao_revisada": "Compra corrigida individualmente",
                "natureza": NaturezaEvento.DESPESA,
                "categoria": "",
                "pessoa": "",
                "ambito": Ambito.NAO_DEFINIDO,
                "acao": ItemImportacao.Acao.CRIAR,
                "observacao": "Alterada individualmente",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(resposta.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.descricao_revisada, "Compra corrigida individualmente")
        self.assertEqual(item.observacao, "Alterada individualmente")
        self.assertIsNotNone(item.revisado_em)

    def test_salvar_vinculo_sem_destino_retorna_erro(self):
        item = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            data=date(2026, 10, 6),
            valor=Decimal("10.00"),
            descricao_original="Receita sem destino",
            natureza=NaturezaEvento.RECEITA,
            ambito=Ambito.NAO_DEFINIDO,
            acao=ItemImportacao.Acao.CRIAR,
            status=ItemImportacao.Status.NOVO,
        )
        self.client.force_login(self.dono)

        resposta = self.client.post(
            f"/importar/{self.lote.pk}/revisao/",
            {
                "item_id": item.pk,
                "data_revisada": "",
                "valor_revisado": "",
                "descricao_revisada": "",
                "natureza": NaturezaEvento.RECEITA,
                "categoria": "",
                "pessoa": "",
                "ambito": Ambito.NAO_DEFINIDO,
                "acao": ItemImportacao.Acao.VINCULAR,
                "observacao": "",
                "titulo_alvo_id": "",
                "movimento_alvo_id": "",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(resposta.status_code, 422)
        self.assertIn("Selecione um título ou movimento", resposta.json()["message"])

    def test_aplicacao_ajax_altera_somente_itens_selecionados(self):
        categoria = Categoria.objects.create(usuario=self.dono, nome="Mercado")
        revisado_em = timezone.now()
        selecionado = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            valor=Decimal("-10.00"),
            descricao_original="Selecionado",
            status=ItemImportacao.Status.NOVO,
        )
        preservado = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=2,
            valor=Decimal("-20.00"),
            descricao_original="Preservado",
            status=ItemImportacao.Status.NOVO,
            observacao="Conferido individualmente",
            revisado_em=revisado_em,
        )
        self.client.force_login(self.dono)

        resposta = self.client.post(
            f"/importar/{self.lote.pk}/aplicar-em-lote/",
            {
                "categoria": categoria.pk,
                "pessoa": "",
                "ambito": "",
                "acao": ItemImportacao.Acao.IGNORAR,
                "observacao": "Compra da viagem",
                "somente_status": "SELECIONADOS",
                "itens_selecionados": [selecionado.pk, preservado.pk],
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual([item["id"] for item in resposta.json()["items"]], [selecionado.pk])
        self.assertEqual(resposta.json()["quantidade_preservada"], 1)
        self.assertIn("1 item já revisado foi preservado", resposta.json()["message"])
        selecionado.refresh_from_db()
        preservado.refresh_from_db()
        self.assertEqual(selecionado.categoria, categoria)
        self.assertEqual(selecionado.acao, ItemImportacao.Acao.IGNORAR)
        self.assertEqual(selecionado.observacao, "Compra da viagem")
        self.assertIsNotNone(selecionado.revisado_em)
        self.assertIsNone(preservado.categoria)
        self.assertEqual(preservado.acao, ItemImportacao.Acao.CRIAR)
        self.assertEqual(preservado.observacao, "Conferido individualmente")
        self.assertEqual(preservado.revisado_em, revisado_em)

    def test_aplicacao_global_preserva_revisados(self):
        categoria = Categoria.objects.create(usuario=self.dono, nome="Alimentação")
        revisado_em = timezone.now()
        revisado = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            valor=Decimal("-10.00"),
            descricao_original="Já revisado",
            status=ItemImportacao.Status.NOVO,
            observacao="Manter",
            revisado_em=revisado_em,
        )
        pendente = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=2,
            valor=Decimal("-20.00"),
            descricao_original="Ainda não revisado",
            status=ItemImportacao.Status.NOVO,
        )
        self.client.force_login(self.dono)

        resposta = self.client.post(
            f"/importar/{self.lote.pk}/aplicar-em-lote/",
            {"categoria": categoria.pk, "observacao": "Aplicação global", "somente_status": ""},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual([item["id"] for item in resposta.json()["items"]], [pendente.pk])
        self.assertEqual(resposta.json()["quantidade_preservada"], 1)
        revisado.refresh_from_db()
        pendente.refresh_from_db()
        self.assertIsNone(revisado.categoria)
        self.assertEqual(revisado.observacao, "Manter")
        self.assertEqual(revisado.revisado_em, revisado_em)
        self.assertEqual(pendente.categoria, categoria)
        self.assertEqual(pendente.observacao, "Aplicação global")

    def test_aplicacao_por_status_preserva_revisados(self):
        revisado_em = timezone.now()
        revisado = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            valor=Decimal("-10.00"),
            descricao_original="Pendente revisado",
            status=ItemImportacao.Status.PENDENTE,
            observacao="Manter",
            revisado_em=revisado_em,
        )
        pendente = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=2,
            valor=Decimal("-20.00"),
            descricao_original="Pendente não revisado",
            status=ItemImportacao.Status.PENDENTE,
        )
        self.client.force_login(self.dono)

        resposta = self.client.post(
            f"/importar/{self.lote.pk}/aplicar-em-lote/",
            {"observacao": "Revisão dos pendentes", "somente_status": ItemImportacao.Status.PENDENTE},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual([item["id"] for item in resposta.json()["items"]], [pendente.pk])
        revisado.refresh_from_db()
        pendente.refresh_from_db()
        self.assertEqual(revisado.observacao, "Manter")
        self.assertEqual(revisado.revisado_em, revisado_em)
        self.assertEqual(pendente.observacao, "Revisão dos pendentes")

    def test_aplicacao_somente_em_revisados_nao_altera_dados(self):
        revisado_em = timezone.now()
        revisado = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            valor=Decimal("-10.00"),
            descricao_original="Somente revisado",
            status=ItemImportacao.Status.NOVO,
            observacao="Valor original",
            revisado_em=revisado_em,
        )
        self.client.force_login(self.dono)

        resposta = self.client.post(
            f"/importar/{self.lote.pk}/aplicar-em-lote/",
            {
                "observacao": "Não gravar",
                "somente_status": "SELECIONADOS",
                "itens_selecionados": [revisado.pk],
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(resposta.status_code, 422)
        self.assertIn("Nenhum item não revisado", resposta.json()["message"])
        self.assertIn("1 item já revisado foi preservado", resposta.json()["message"])
        revisado.refresh_from_db()
        self.assertEqual(revisado.observacao, "Valor original")
        self.assertEqual(revisado.revisado_em, revisado_em)

    def test_aplicacao_em_selecionados_exige_selecao(self):
        self.client.force_login(self.dono)

        resposta = self.client.post(
            f"/importar/{self.lote.pk}/aplicar-em-lote/",
            {"acao": ItemImportacao.Acao.IGNORAR, "somente_status": "SELECIONADOS"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(resposta.status_code, 422)
        self.assertIn("Selecione ao menos um item", resposta.json()["message"])

    def test_vinculo_em_lote_rejeita_item_sem_evento(self):
        item = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            valor=Decimal("-10.00"),
            descricao_original="Sem vínculo",
            status=ItemImportacao.Status.NOVO,
        )
        self.client.force_login(self.dono)

        resposta = self.client.post(
            f"/importar/{self.lote.pk}/aplicar-em-lote/",
            {
                "acao": ItemImportacao.Acao.VINCULAR,
                "observacao": "Não deve gravar",
                "somente_status": "SELECIONADOS",
                "itens_selecionados": [item.pk],
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(resposta.status_code, 422)
        item.refresh_from_db()
        self.assertEqual(item.acao, ItemImportacao.Acao.CRIAR)
        self.assertEqual(item.observacao, "")

    def test_resultado_renderiza_para_o_dono(self):
        self.lote.status = Importacao.Status.CONFIRMADA
        self.lote.save(update_fields=["status"])
        self.client.force_login(self.dono)
        resposta = self.client.get(f"/importar/{self.lote.pk}/resultado/")
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Rastreabilidade")
        self.assertContains(resposta, '<details class="panel full collapsible-panel import-result-items">')
        self.assertNotContains(resposta, 'class="panel full collapsible-panel import-result-items" open')
        self.assertContains(resposta, 'title="Abrir ou fechar os itens da importação finalizada"')
        self.assertContains(resposta, "Desfazer importação")

    def test_revisao_renderiza_selecao_observacao_e_novo_layout(self):
        ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            valor=Decimal("-10.00"),
            descricao_original="Item para revisão",
        )
        ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=2,
            valor=Decimal("-20.00"),
            descricao_original="Item já revisado",
            revisado_em=timezone.now(),
        )
        self.client.force_login(self.dono)

        resposta = self.client.get(f"/importar/{self.lote.pk}/revisao/")

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Selecionar todos os itens não revisados visíveis")
        self.assertContains(resposta, '<option value="SELECIONADOS">Selecionados</option>', html=False)
        self.assertContains(resposta, 'class="item-editor-grid"')
        self.assertContains(resposta, 'name="observacao"')
        self.assertContains(resposta, 'name="revisao"')
        self.assertContains(resposta, "Não revisado")
        self.assertContains(resposta, 'aria-label="Selecionar Item já revisado" disabled', html=False)
        self.assertContains(resposta, 'id="batchConfirmation"')
        self.assertContains(resposta, "Confirmar classificação em massa")
        self.assertContains(resposta, "Confirmar e aplicar")
        self.assertContains(resposta, '<details class="panel full collapsible-panel import-review-items">')
        self.assertNotContains(resposta, 'class="panel full collapsible-panel import-review-items" open')
        self.assertContains(resposta, 'title="Abrir ou fechar a lista de itens da importação"')
        self.assertContains(resposta, "Confirmar importação")

    def test_filtro_de_revisao_separa_revisados_e_nao_revisados(self):
        revisado = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=1,
            valor=Decimal("-10.00"),
            descricao_original="Item já conferido",
            status=ItemImportacao.Status.NOVO,
            revisado_em=timezone.now(),
        )
        nao_revisado = ItemImportacao.objects.create(
            importacao=self.lote,
            posicao=2,
            valor=Decimal("-20.00"),
            descricao_original="Item aguardando conferência",
            status=ItemImportacao.Status.NOVO,
        )
        self.client.force_login(self.dono)

        resposta_revisados = self.client.get(
            f"/importar/{self.lote.pk}/revisao/",
            {"revisao": "REVISADOS", "status": ItemImportacao.Status.NOVO},
        )
        resposta_pendentes = self.client.get(
            f"/importar/{self.lote.pk}/revisao/",
            {"revisao": "NAO_REVISADOS", "status": ItemImportacao.Status.NOVO},
        )

        self.assertContains(resposta_revisados, revisado.descricao_original)
        self.assertNotContains(resposta_revisados, nao_revisado.descricao_original)
        self.assertContains(resposta_pendentes, nao_revisado.descricao_original)
        self.assertNotContains(resposta_pendentes, revisado.descricao_original)
        self.assertContains(resposta_pendentes, "const reviewFilter = 'NAO_REVISADOS';")

    def test_upload_cria_lote_em_revisao(self):
        self.client.force_login(self.dono)
        resposta = self.client.post(
            "/importar/ofx/",
            {
                "origem": Importacao.Origem.CONTA,
                "conta": self.lote.conta_id,
                "cartao": "",
                "ciclo": "",
                "arquivo_ofx": SimpleUploadedFile("extrato.ofx", OFX_CONTA, content_type="application/x-ofx"),
            },
        )
        novo_lote = Importacao.objects.exclude(pk=self.lote.pk).get()
        self.assertRedirects(resposta, f"/importar/{novo_lote.pk}/revisao/")
        self.assertEqual(novo_lote.itens.count(), 2)

    def test_tela_de_upload_exibe_estado_de_analise(self):
        self.client.force_login(self.dono)

        resposta = self.client.get("/importar/ofx/")

        self.assertContains(resposta, 'data-loading-label="Analisando arquivo..."')
        self.assertContains(resposta, 'id="loadingOverlay"')
        self.assertContains(resposta, 'data-loading-link')
        self.assertContains(resposta, 'data-loading-message="Abrindo importação..."')
        self.assertContains(resposta, 'class="pill pill-review"')
        self.assertContains(resposta, 'class="icon-button icon-open"')
        self.assertContains(resposta, '<details class="panel collapsible-panel">', html=False)
        self.assertContains(resposta, '.import-planilha-panel{margin-top:28px}')
        self.assertContains(
            resposta,
            '<details class="panel collapsible-panel import-planilha-panel">',
            html=False,
        )
        self.assertContains(resposta, '<details class="panel full collapsible-panel">', html=False)
        self.assertNotContains(resposta, '<details class="panel collapsible-panel" open', html=False)
        self.assertNotContains(resposta, '<details class="panel full collapsible-panel" open', html=False)

    def test_tela_de_upload_exibe_no_maximo_sete_importacoes_recentes(self):
        for indice in range(8):
            Importacao.objects.create(
                usuario=self.dono,
                arquivo=f"importacoes/testes/recente-{indice}.ofx",
                nome_arquivo=f"recente-{indice}.ofx",
                hash_arquivo=f"{indice + 10:064d}",
                origem=Importacao.Origem.CONTA,
                conta=self.lote.conta,
                status=Importacao.Status.CONFIRMADA,
            )

        self.client.force_login(self.dono)
        resposta = self.client.get("/importar/ofx/")

        importacoes = list(resposta.context["importacoes"])
        esperadas = list(
            Importacao.objects.filter(usuario=self.dono)
            .order_by("-criado_em")[:7]
            .values_list("pk", flat=True)
        )
        self.assertEqual(len(importacoes), 7)
        self.assertEqual([importacao.pk for importacao in importacoes], esperadas)
        self.assertContains(resposta, "Últimos 7 registros")

    def test_importacao_confirmada_usa_status_azul(self):
        self.lote.status = Importacao.Status.CONFIRMADA
        self.lote.save(update_fields=["status"])
        self.client.force_login(self.dono)

        resposta = self.client.get("/importar/ofx/")

        self.assertContains(resposta, 'class="pill pill-completed"')
        self.assertNotContains(resposta, 'class="pill pill-review"')

    def test_upload_cartao_aceita_ciclo_mes_ano_e_normaliza(self):
        self.client.force_login(self.dono)
        resposta = self.client.post(
            "/importar/ofx/",
            {
                "origem": Importacao.Origem.CARTAO,
                "conta": "",
                "cartao": self.cartao.pk,
                "ciclo": "10/2026",
                "arquivo_ofx": SimpleUploadedFile("fatura.ofx", OFX_CARTAO.encode(), content_type="application/x-ofx"),
            },
        )
        novo_lote = Importacao.objects.exclude(pk=self.lote.pk).get()
        self.assertRedirects(resposta, f"/importar/{novo_lote.pk}/revisao/")
        self.assertEqual(novo_lote.ciclo, "2026-10")

    def test_erro_de_ciclo_aparece_em_toast(self):
        self.client.force_login(self.dono)
        resposta = self.client.post(
            "/importar/ofx/",
            {
                "origem": Importacao.Origem.CARTAO,
                "conta": "",
                "cartao": self.cartao.pk,
                "ciclo": "2026-10",
                "arquivo_ofx": SimpleUploadedFile("fatura.ofx", OFX_CARTAO.encode(), content_type="application/x-ofx"),
            },
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Informe o ciclo no formato MM/AAAA.")
        self.assertContains(resposta, 'class="toast error"', html=False)
        self.assertNotContains(resposta, '<ul class="errorlist">', html=False)
