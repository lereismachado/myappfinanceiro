from datetime import date
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from financeiro.models import (
    Ambito,
    Cartao,
    Categoria,
    CompraCartao,
    EventoFinanceiro,
    Fatura,
    Importacao,
    ItemImportacao,
    NaturezaEvento,
    ParcelaCartao,
    Pessoa,
    RegraCategoria,
    RegraCategoriaCondicao,
    Situacao,
)
from financeiro.ofx import OFXParser
from financeiro.services.importacao_ofx import (
    buscar_candidatos_vinculo,
    confirmar_importacao,
    criar_itens,
    selecionar_vinculo_item,
)
from financeiro.services.importacao_planilha import ler_compras_planilha
from financeiro.services.cartoes_faturas import calcular_totais_parcelas


def csv_upload(nome="compras.csv", conteudo=None):
    conteudo = conteudo or (
        "descricao;data_compra;valor_total;quantidade_parcelas;categoria;pessoa;ambito;observacao\n"
        "Assinatura Streaming;15/10/2026;49,90;1;;;PESSOAL;mensal\n"
    )
    return SimpleUploadedFile(nome, conteudo.encode("utf-8"), content_type="text/csv")


def xlsx_upload(valor=120.00):
    from openpyxl import Workbook

    arquivo = BytesIO()
    workbook = Workbook()
    planilha = workbook.active
    planilha.append(["descricao", "data_compra", "valor_total", "quantidade_parcelas", "categoria", "pessoa", "ambito", "observacao"])
    planilha.append(["Compra parcelada", date(2026, 10, 20), valor, 3, "", "", "PESSOAL", "planilha"])
    workbook.save(arquivo)
    return SimpleUploadedFile("compras.xlsx", arquivo.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


OFX_ASSINATURA = b"""OFXHEADER:100
ENCODING:USASCII
<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><CCACCTFROM><ACCTID>9999</ACCTID></CCACCTFROM>
<BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261020<TRNAMT>-49.90<FITID>ASS1<MEMO>ASSINATURA STREAMING</STMTTRN></BANKTRANLIST>
</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>"""


class ImportacaoPlanilhaTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(username="planilha", password="senha")
        self.cartao = Cartao.objects.create(
            usuario=self.usuario,
            nome="Cartão principal",
            identificador_externo="9999",
            final="9999",
            dia_vencimento=10,
        )
        self.categoria = Categoria.objects.create(
            usuario=self.usuario,
            nome="Assinaturas",
            tipo=Categoria.Tipo.DESPESA,
        )

    def criar_lote_planilha(self, arquivo=None, ciclo="11/2026"):
        self.client.force_login(self.usuario)
        resposta = self.client.post(
            "/importar/planilha/",
            {"cartao": self.cartao.pk, "ciclo": ciclo, "arquivo_planilha": arquivo or csv_upload()},
        )
        self.assertEqual(resposta.status_code, 302)
        return Importacao.objects.latest("id")

    def confirmar_planejamento(self, lote):
        confirmar_importacao(lote.pk, self.usuario)
        lote.refresh_from_db()
        return lote

    def criar_parcela_existente(
        self,
        *,
        descricao="Compra já lançada",
        data_compra=date(2026, 9, 17),
        valor="537.50",
        numero=2,
        total=2,
        ciclo="2026-11",
        situacao=Situacao.PREVISTO,
    ):
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao=descricao,
            data=data_compra,
            valor=Decimal(valor) * total,
            situacao=situacao,
        )
        compra = CompraCartao.objects.create(
            usuario=self.usuario,
            cartao=self.cartao,
            evento=evento,
            estabelecimento=descricao,
            data_compra=data_compra,
            valor_total=evento.valor,
            total_parcelas=total,
        )
        fatura, _ = Fatura.objects.get_or_create(
            usuario=self.usuario,
            cartao=self.cartao,
            ciclo=ciclo,
            defaults={"status": Fatura.Status.ABERTA},
        )
        return ParcelaCartao.objects.create(
            compra=compra,
            fatura=fatura,
            natureza=NaturezaEvento.DESPESA,
            descricao=descricao,
            data_original=data_compra,
            numero=numero,
            total=total,
            valor=Decimal(valor),
            situacao=situacao,
        )

    def test_csv_cria_compra_e_parcelas_futuras(self):
        lote = self.criar_lote_planilha(
            csv_upload(
                conteudo=(
                    "descricao;data_compra;valor_total;quantidade_parcelas;categoria;pessoa;ambito;observacao\n"
                    "Compra anual;20/10/2026;100,00;3;Assinaturas;;PESSOAL;futuro\n"
                )
            )
        )
        item = lote.itens.get()
        self.assertEqual(item.status, ItemImportacao.Status.NOVO)
        self.assertEqual(item.acao, ItemImportacao.Acao.CRIAR)
        self.assertIsNone(item.evento_id)
        self.assertIsNone(item.movimento_conta_id)
        self.assertIsNone(item.titulo_alvo_id)
        self.assertIsNone(item.parcela_id)
        self.assertIn("Criar lançamento foi pré-selecionado", item.motivo_status)

        resposta = self.client.get(f"/importar/{lote.pk}/revisao/")
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, '<option value="CRIAR" selected>')
        self.assertContains(resposta, '<section class="link-picker" data-link-picker hidden>', html=False)

        self.confirmar_planejamento(lote)

        compra = CompraCartao.objects.get()
        self.assertEqual(compra.valor_total, Decimal("100.00"))
        self.assertEqual(compra.parcelas.count(), 3)
        self.assertEqual(list(compra.parcelas.values_list("fatura__ciclo", flat=True)), ["2026-11", "2026-12", "2027-01"])
        self.assertEqual(
            list(compra.parcelas.values_list("vencimento", flat=True)),
            [date(2026, 11, 10), date(2026, 12, 10), date(2027, 1, 10)],
        )
        self.assertEqual(compra.parcelas.filter(situacao=Situacao.PREVISTO).count(), 3)
        self.assertEqual(compra.evento.situacao, Situacao.PREVISTO)
        self.assertEqual(compra.evento.observacao, "futuro")

    def test_planilha_sem_observacao_nao_cria_texto_automatico(self):
        lote = self.criar_lote_planilha(
            csv_upload(
                conteudo=(
                    "descricao;data_compra;valor_total;quantidade_parcelas;categoria;pessoa;ambito;observacao\n"
                    "Compra sem observação;20/10/2026;25,00;1;Assinaturas;;PESSOAL;\n"
                )
            )
        )

        self.confirmar_planejamento(lote)

        self.assertEqual(CompraCartao.objects.get().evento.observacao, "")
        self.assertFalse(CompraCartao.objects.get().evento.observacao.startswith("Compra futura importada"))

    def test_planilha_sem_ambito_classificado_usa_pessoal_como_padrao(self):
        lote = self.criar_lote_planilha(
            csv_upload(
                conteudo=(
                    "descricao;data_compra;valor_total;quantidade_parcelas;categoria;pessoa;ambito;observacao\n"
                    "Compra sem âmbito;20/10/2026;25,00;1;;;;\n"
                    "Compra com âmbito não definido;21/10/2026;30,00;1;;;NAO_DEFINIDO;\n"
                )
            )
        )

        itens = list(lote.itens.order_by("posicao"))
        self.assertEqual([item.ambito for item in itens], [Ambito.PESSOAL, Ambito.PESSOAL])
        for item in itens:
            self.assertIn("Âmbito definido como Pessoal por padrão", item.motivo_status)

    def test_planilha_cartao_sugere_historico_do_ciclo_anterior(self):
        categoria = Categoria.objects.create(
            usuario=self.usuario,
            nome="Streaming",
            tipo=Categoria.Tipo.DESPESA,
        )
        parcela = self.criar_parcela_existente(
            descricao="Assinatura Streaming",
            data_compra=date(2026, 9, 15),
            valor="49.90",
            ciclo="2026-09",
            situacao=Situacao.REALIZADO,
        )
        parcela.categoria = categoria
        parcela.ambito = Ambito.PESSOAL
        parcela.save(update_fields=["categoria", "ambito"])

        lote = self.criar_lote_planilha(
            csv_upload(
                conteudo=(
                    "descricao;data_compra;valor_total;quantidade_parcelas;categoria;pessoa;ambito;observacao\n"
                    "Assinatura Streaming;15/10/2026;49,90;1;;;;\n"
                )
            ),
            ciclo="10/2026",
        )
        item = lote.itens.get()

        self.assertEqual(item.categoria_id, categoria.pk)
        self.assertEqual(item.ambito, Ambito.PESSOAL)
        self.assertIn("Categoria sugerida pelo histórico recente", item.motivo_status)

    def test_planilha_preserva_classificacao_explicitamente_informada(self):
        categoria_historica = Categoria.objects.create(
            usuario=self.usuario,
            nome="Streaming",
            tipo=Categoria.Tipo.DESPESA,
        )
        categoria_informada = Categoria.objects.create(
            usuario=self.usuario,
            nome="Serviços",
            tipo=Categoria.Tipo.DESPESA,
        )
        parcela = self.criar_parcela_existente(
            descricao="Assinatura Streaming",
            data_compra=date(2026, 9, 15),
            valor="49.90",
            ciclo="2026-09",
            situacao=Situacao.REALIZADO,
        )
        parcela.categoria = categoria_historica
        parcela.ambito = Ambito.PESSOAL
        parcela.save(update_fields=["categoria", "ambito"])

        lote = self.criar_lote_planilha(
            csv_upload(
                conteudo=(
                    "descricao;data_compra;valor_total;quantidade_parcelas;categoria;pessoa;ambito;observacao\n"
                    "Assinatura Streaming;15/10/2026;49,90;1;Serviços;;PROFISSIONAL;\n"
                )
            ),
            ciclo="10/2026",
        )
        item = lote.itens.get()

        self.assertEqual(item.categoria_id, categoria_informada.pk)
        self.assertEqual(item.ambito, Ambito.PROFISSIONAL)

    def test_planilha_preserva_campos_explicitos_e_regra_pode_sugerir_tipo(self):
        categoria_informada = Categoria.objects.create(
            usuario=self.usuario,
            nome="Serviços informados",
            tipo=Categoria.Tipo.DESPESA,
        )
        categoria_regra = Categoria.objects.create(
            usuario=self.usuario,
            nome="Categoria da regra",
            tipo=Categoria.Tipo.DESPESA,
        )
        pessoa_informada = Pessoa.objects.create(usuario=self.usuario, nome="Pessoa informada")
        pessoa_regra = Pessoa.objects.create(usuario=self.usuario, nome="Pessoa da regra")
        regra = RegraCategoria.objects.create(
            usuario=self.usuario,
            nome="Streaming com tipo",
            prioridade=1,
            categoria=categoria_regra,
            pessoa=pessoa_regra,
            ambito=Ambito.PESSOAL,
            natureza_sugerida=NaturezaEvento.ESTORNO,
        )
        RegraCategoriaCondicao.objects.create(
            regra=regra,
            ordem=1,
            campo=RegraCategoriaCondicao.Campo.DESCRICAO,
            operador=RegraCategoriaCondicao.Operador.CONTEM,
            valor="STREAMING",
        )

        lote = self.criar_lote_planilha(
            csv_upload(
                conteudo=(
                    "descricao;data_compra;valor_total;quantidade_parcelas;categoria;pessoa;ambito;observacao\n"
                    f"Assinatura Streaming;15/10/2026;49,90;1;{categoria_informada.nome};{pessoa_informada.nome};PROFISSIONAL;mensal\n"
                )
            )
        )
        item = lote.itens.get()

        self.assertEqual(item.categoria, categoria_informada)
        self.assertEqual(item.pessoa, pessoa_informada)
        self.assertEqual(item.ambito, Ambito.PROFISSIONAL)
        self.assertEqual(item.natureza, NaturezaEvento.ESTORNO)
        self.assertIn("Streaming com tipo", item.motivo_status)

    def test_xlsx_e_valor_brasileiro_sao_lidos(self):
        compras = ler_compras_planilha(xlsx_upload(), self.usuario)
        self.assertEqual(compras[0]["valor_total"], Decimal("120.00"))
        self.assertEqual(compras[0]["quantidade_parcelas"], 3)
        self.assertEqual(compras[0]["data_compra"], date(2026, 10, 20))

    def test_xlsx_aceita_valor_negativo(self):
        compras = ler_compras_planilha(xlsx_upload(-120.00), self.usuario)

        self.assertEqual(compras[0]["valor_total"], Decimal("-120.00"))

    def test_planilha_aceita_estorno_e_pagamento_de_fatura_negativos(self):
        regra = RegraCategoria.objects.create(
            usuario=self.usuario,
            nome="Não aplicar em pagamento",
            prioridade=1,
            categoria=self.categoria,
            natureza_sugerida=NaturezaEvento.RECEITA,
        )
        RegraCategoriaCondicao.objects.create(
            regra=regra,
            ordem=1,
            campo=RegraCategoriaCondicao.Campo.DESCRICAO,
            operador=RegraCategoriaCondicao.Operador.CONTEM,
            valor="PAGAMENTO",
        )
        lote = self.criar_lote_planilha(
            csv_upload(
                conteudo=(
                    "descricao;data_compra;valor_total;quantidade_parcelas;categoria;pessoa;ambito;observacao\n"
                    "PAGAMENTO DEBITO EM CONTA*;13/10/2026;-8538,19;1;;;PESSOAL;pagamento\n"
                    "Estorno da compra;17/10/2026;-132,82;1;;;PESSOAL;credito\n"
                    "Compra normal;18/10/2026;100,00;1;;;PESSOAL;compra\n"
                )
            )
        )
        pagamento = lote.itens.get(descricao_original__startswith="PAGAMENTO")
        estorno = lote.itens.get(descricao_original="Estorno da compra")
        despesa = lote.itens.get(descricao_original="Compra normal")

        self.assertEqual(pagamento.valor, Decimal("-8538.19"))
        self.assertEqual(pagamento.natureza, NaturezaEvento.PAGAMENTO_FATURA)
        self.assertIsNone(pagamento.categoria)
        self.assertNotIn("Não aplicar em pagamento", pagamento.motivo_status)
        self.assertEqual(estorno.valor, Decimal("-132.82"))
        self.assertEqual(estorno.natureza, NaturezaEvento.ESTORNO)
        self.assertEqual(despesa.natureza, NaturezaEvento.DESPESA)
        self.assertEqual(lote.total_debitos, Decimal("100.00"))
        self.assertEqual(lote.total_creditos, Decimal("8671.01"))
        self.assertEqual(lote.total_liquido, Decimal("8571.01"))
        self.assertEqual(
            buscar_candidatos_vinculo(pagamento, self.usuario, tipo="PARCELA", escopo="TODOS")["total"],
            0,
        )

        self.confirmar_planejamento(lote)

        pagamento.refresh_from_db()
        self.assertIsNotNone(pagamento.evento_id)
        self.assertIsNone(pagamento.parcela_id)
        self.assertEqual(pagamento.evento.natureza, NaturezaEvento.PAGAMENTO_FATURA)
        self.assertEqual(pagamento.evento.situacao, Situacao.REALIZADO)
        self.assertEqual(CompraCartao.objects.count(), 2)

        parcela_estorno = ParcelaCartao.objects.get(compra__evento__natureza=NaturezaEvento.ESTORNO)
        self.assertEqual(parcela_estorno.valor, Decimal("132.82"))
        self.assertEqual(parcela_estorno.natureza, NaturezaEvento.ESTORNO)
        totais = calcular_totais_parcelas(parcela_estorno.fatura.parcelas.all())
        self.assertEqual(totais["projetado"], Decimal("-32.82"))

    def test_planilha_rejeita_valor_zero(self):
        self.client.force_login(self.usuario)
        resposta = self.client.post(
            "/importar/planilha/",
            {
                "cartao": self.cartao.pk,
                "ciclo": "11/2026",
                "arquivo_planilha": csv_upload(
                    conteudo=(
                        "descricao;data_compra;valor_total;quantidade_parcelas\n"
                        "Compra sem valor;15/10/2026;0,00;1\n"
                    )
                ),
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "valor_total deve ser diferente de zero", html=False)
        self.assertFalse(Importacao.objects.exists())

    def test_categoria_inexistente_rejeita_planilha(self):
        self.client.force_login(self.usuario)
        resposta = self.client.post(
            "/importar/planilha/",
            {
                "cartao": self.cartao.pk,
                "ciclo": "11/2026",
                "arquivo_planilha": csv_upload(
                    conteudo=(
                        "descricao;data_compra;valor_total;quantidade_parcelas;categoria\n"
                        "Compra;15/10/2026;10,00;1;Categoria inexistente\n"
                    )
                ),
            },
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Categoria inexistente", html=False)
        self.assertFalse(Importacao.objects.exists())

    def test_compra_duplicada_fica_pendente_e_nao_cria_silenciosamente(self):
        primeiro = self.criar_lote_planilha()
        self.confirmar_planejamento(primeiro)
        segundo = self.criar_lote_planilha()
        item = segundo.itens.get()
        self.assertEqual(item.status, ItemImportacao.Status.PENDENTE)
        self.assertEqual(item.acao, ItemImportacao.Acao.IGNORAR)

        confirmar_importacao(segundo.pk, self.usuario)
        self.assertEqual(CompraCartao.objects.count(), 1)

    def test_planilha_sugere_parcela_existente_por_data_e_valor(self):
        parcela = self.criar_parcela_existente(
            descricao="SPLENDORE FARMACI",
        )
        lote = self.criar_lote_planilha(
            csv_upload(
                conteudo=(
                    "descricao;data_compra;valor_total;quantidade_parcelas;categoria;pessoa;ambito;observacao\n"
                    "SPLENDORE FARMACI - 2/2;17/09/2026;537,50;2;Assinaturas;;PESSOAL;\n"
                )
            )
        )
        item = lote.itens.get()

        resposta = self.client.get(
            f"/importar/{lote.pk}/itens/{item.pk}/candidatos/",
            {"tipo": "PARCELA", "escopo": "SUGESTOES"},
        )
        self.assertEqual(resposta.status_code, 200)
        resultado = resposta.json()
        self.assertTrue(resultado["ok"])

        self.assertEqual(item.numero_parcela, 1)
        self.assertEqual(parcela.numero, 2)
        self.assertEqual(item.status, ItemImportacao.Status.PENDENTE)
        self.assertEqual(item.acao, ItemImportacao.Acao.VINCULAR)
        self.assertEqual(item.parcela_id, parcela.pk)
        self.assertIn("Sugestão automática", item.motivo_status)
        self.assertIn(parcela.pk, [candidato["id"] for candidato in resultado["resultados"]])
        candidato = next(candidato for candidato in resultado["resultados"] if candidato["id"] == parcela.pk)
        self.assertIn("valor exato", candidato["motivos"])
        self.assertIn("mesma data", candidato["motivos"])

        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, parcela_id=parcela.pk)
        item.save()
        item.refresh_from_db()
        self.assertEqual(item.parcela_id, parcela.pk)
        self.assertEqual(item.status, ItemImportacao.Status.CONCILIADO)
        confirmar_importacao(lote.pk, self.usuario)
        self.assertEqual(CompraCartao.objects.count(), 1)

    def test_planilha_com_parcelas_exatas_multiplas_exige_selecao_manual(self):
        parcelas = [
            self.criar_parcela_existente(descricao=f"Compra equivalente {indice}")
            for indice in range(2)
        ]
        lote = self.criar_lote_planilha(
            csv_upload(
                conteudo=(
                    "descricao;data_compra;valor_total;quantidade_parcelas;categoria;pessoa;ambito;observacao\n"
                    "Compra importada;17/09/2026;537,50;2;Assinaturas;;PESSOAL;\n"
                )
            )
        )
        item = lote.itens.get()

        self.assertEqual(item.status, ItemImportacao.Status.PENDENTE)
        self.assertEqual(item.acao, ItemImportacao.Acao.VINCULAR)
        self.assertIsNone(item.parcela_id)
        self.assertIn("Há 2 destinos", item.motivo_status)

        resposta = self.client.get(
            f"/importar/{lote.pk}/itens/{item.pk}/candidatos/",
            {"tipo": "PARCELA", "escopo": "SUGESTOES"},
        )
        self.assertEqual(resposta.status_code, 200)
        resultado = resposta.json()
        self.assertEqual(resultado["total"], 2)
        self.assertCountEqual(
            [candidato["id"] for candidato in resultado["resultados"]],
            [parcela.pk for parcela in parcelas],
        )

    def test_planilha_revalida_parcela_sugerida_na_confirmacao(self):
        parcela = self.criar_parcela_existente(descricao="Compra para revalidar")
        lote = self.criar_lote_planilha(
            csv_upload(
                conteudo=(
                    "descricao;data_compra;valor_total;quantidade_parcelas;categoria;pessoa;ambito;observacao\n"
                    "Compra para revalidar;17/09/2026;537,50;2;Assinaturas;;PESSOAL;\n"
                )
            )
        )
        item = lote.itens.get()
        self.assertEqual(item.parcela_id, parcela.pk)

        selecionar_vinculo_item(item, self.usuario, parcela_id=parcela.pk)
        item.save()
        parcela.fatura.status = Fatura.Status.FECHADA
        parcela.fatura.save(update_fields=["status"])

        with self.assertRaisesMessage(ValidationError, "não está disponível"):
            confirmar_importacao(lote.pk, self.usuario)

    def test_buscar_em_todos_planilha_retorna_apenas_previstas_de_fatura_aberta(self):
        elegivel = self.criar_parcela_existente(
            descricao="Compra com data e valor diferentes",
            data_compra=date(2026, 8, 1),
            valor="100.00",
            numero=1,
            total=1,
        )
        realizada = self.criar_parcela_existente(
            descricao="Compra já realizada",
            data_compra=date(2026, 8, 2),
            valor="200.00",
            numero=1,
            total=1,
            situacao=Situacao.REALIZADO,
        )
        lote = self.criar_lote_planilha(
            csv_upload(
                conteudo=(
                    "descricao;data_compra;valor_total;quantidade_parcelas;categoria;pessoa;ambito;observacao\n"
                    "Compra importada;17/09/2026;537,50;2;Assinaturas;;PESSOAL;\n"
                )
            )
        )
        item = lote.itens.get()

        sugestoes = buscar_candidatos_vinculo(item, self.usuario, tipo="PARCELA")
        todos = buscar_candidatos_vinculo(
            item,
            self.usuario,
            tipo="PARCELA",
            escopo="TODOS",
        )

        self.assertNotIn(elegivel.pk, [candidato["id"] for candidato in sugestoes["resultados"]])
        self.assertIn(elegivel.pk, [candidato["id"] for candidato in todos["resultados"]])
        self.assertNotIn(realizada.pk, [candidato["id"] for candidato in todos["resultados"]])

        elegivel.fatura.status = Fatura.Status.FECHADA
        elegivel.fatura.save(update_fields=["status"])
        depois_fechamento = buscar_candidatos_vinculo(
            item,
            self.usuario,
            tipo="PARCELA",
            escopo="TODOS",
        )
        self.assertEqual(depois_fechamento["total"], 0)

    def test_ofx_sem_data_exata_permite_vinculo_manual_de_parcela(self):
        lote = self.criar_lote_planilha()
        self.confirmar_planejamento(lote)
        parcela = ParcelaCartao.objects.get()

        documento = OFXParser.parse(OFX_ASSINATURA)
        ofx = Importacao.objects.create(
            usuario=self.usuario,
            arquivo="importacoes/assinatura.ofx",
            nome_arquivo="assinatura.ofx",
            hash_arquivo="b" * 64,
            origem=Importacao.Origem.CARTAO,
            cartao=self.cartao,
            ciclo="2026-11",
            status=Importacao.Status.EM_REVISAO,
        )
        criar_itens(ofx, documento)
        item = ofx.itens.get()

        self.assertEqual(item.status, ItemImportacao.Status.NOVO)
        self.assertEqual(item.acao, ItemImportacao.Acao.CRIAR)
        self.assertIsNone(item.parcela_id)
        parcela.refresh_from_db()
        self.assertEqual(parcela.situacao, Situacao.PREVISTO)

        item.acao = ItemImportacao.Acao.VINCULAR
        selecionar_vinculo_item(item, self.usuario, parcela_id=parcela.pk)
        item.save()
        confirmar_importacao(ofx.pk, self.usuario)
        parcela.refresh_from_db()
        self.assertEqual(parcela.situacao, Situacao.REALIZADO)

    def test_ofx_a_oito_dias_nao_sugere_parcela(self):
        lote = self.criar_lote_planilha()
        self.confirmar_planejamento(lote)
        documento = OFXParser.parse(OFX_ASSINATURA.replace(b"20261020", b"20261023"))
        ofx = Importacao.objects.create(
            usuario=self.usuario,
            arquivo="importacoes/assinatura-longe.ofx",
            nome_arquivo="assinatura-longe.ofx",
            hash_arquivo="c" * 64,
            origem=Importacao.Origem.CARTAO,
            cartao=self.cartao,
            ciclo="2026-11",
            status=Importacao.Status.EM_REVISAO,
        )
        criar_itens(ofx, documento)
        item = ofx.itens.get()
        self.assertEqual(item.status, ItemImportacao.Status.NOVO)
        self.assertEqual(item.acao, ItemImportacao.Acao.CRIAR)
        self.assertIsNone(item.evento_id)
        self.assertIsNone(item.movimento_conta_id)
        self.assertIsNone(item.titulo_alvo_id)
        self.assertIsNone(item.parcela_id)
