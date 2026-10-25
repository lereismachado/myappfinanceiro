import json
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from financeiro.models import (
    Cartao,
    Categoria,
    Conta,
    EventoFinanceiro,
    Fatura,
    NaturezaEvento,
    ProgramacaoPagamento,
    RateioCategoria,
    Situacao,
    Titulo,
    TokenAcessoApi,
)
from financeiro.services.previsoes import consultar_previsoes
from financeiro.services.tokens_api import criar_token_acesso


class PrevisoesServiceTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(username="previsoes", password="senha")
        self.outro = get_user_model().objects.create_user(username="outro-previsoes", password="senha")
        self.conta = Conta.objects.create(usuario=self.usuario, nome="Conta B")
        self.outra_conta = Conta.objects.create(usuario=self.usuario, nome="Conta A")

    def titulo_direto(
        self,
        natureza,
        valor,
        vencimento,
        *,
        usuario=None,
        conta=None,
        situacao=Situacao.PREVISTO,
        descricao=None,
        categorias=(),
    ):
        usuario = usuario or self.usuario
        if conta is None and usuario == self.usuario:
            conta = self.conta
        evento = EventoFinanceiro.objects.create(
            usuario=usuario,
            natureza=natureza,
            descricao=descricao or natureza,
            data=vencimento,
            vencimento=vencimento,
            valor=valor,
            situacao=situacao,
        )
        for categoria in categorias:
            RateioCategoria.objects.create(evento=evento, categoria=categoria, valor=valor)
        tipo = Titulo.Tipo.RECEBER if natureza in {NaturezaEvento.RECEITA, NaturezaEvento.ESTORNO} else Titulo.Tipo.PAGAR
        return Titulo.objects.create(
            usuario=usuario,
            tipo=tipo,
            descricao=evento.descricao,
            vencimento=vencimento,
            valor_original=valor,
            saldo_aberto=valor,
            conta=conta,
            evento=evento,
        )

    def titulo_fatura(self, status, valor, vencimento, *, ciclo, conta=None, cartao=None):
        cartao = cartao or Cartao.objects.create(
            usuario=self.usuario,
            nome=f"Cartão {ciclo}",
            dia_vencimento=10,
        )
        fatura = Fatura.objects.create(
            usuario=self.usuario,
            cartao=cartao,
            ciclo=ciclo,
            vencimento=vencimento,
            status=status,
        )
        return Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao=f"Fatura {cartao.nome} {ciclo}",
            vencimento=vencimento,
            valor_original=valor,
            saldo_aberto=valor,
            conta=conta,
            fatura=fatura,
        )

    def test_inclui_fluxo_direto_com_categorias_e_exclui_naturezas_indevidas(self):
        categoria_pai = Categoria.objects.create(usuario=self.usuario, nome="Moradia")
        categoria = Categoria.objects.create(
            usuario=self.usuario,
            nome="Energia",
            categoria_pai=categoria_pai,
            computa_resultado=False,
        )
        self.titulo_direto(
            NaturezaEvento.DESPESA,
            Decimal("100.00"),
            date(2026, 10, 24),
            descricao="Energia elétrica",
            categorias=(categoria,),
        )
        self.titulo_direto(NaturezaEvento.RECEITA, Decimal("50.00"), date(2026, 10, 29))
        self.titulo_direto(NaturezaEvento.ESTORNO, Decimal("10.00"), date(2026, 10, 26))
        self.titulo_direto(NaturezaEvento.TRANSFERENCIA, Decimal("90.00"), date(2026, 10, 25))
        self.titulo_direto(NaturezaEvento.PAGAMENTO_FATURA, Decimal("80.00"), date(2026, 10, 25))
        self.titulo_direto(
            NaturezaEvento.DESPESA,
            Decimal("70.00"),
            date(2026, 10, 25),
            situacao=Situacao.REALIZADO,
        )

        resultado = consultar_previsoes(self.usuario, date(2026, 10, 24), date(2026, 10, 29))

        self.assertEqual(resultado["totais"], {
            "receitas": "60.00",
            "despesas": "100.00",
            "saldoPrevisto": "-40.00",
        })
        itens = resultado["contas"][0]["itens"]
        self.assertEqual(len(itens), 3)
        despesa = next(item for item in itens if item["descricao"] == "Energia elétrica")
        self.assertEqual(despesa["categorias"], ["Energia (Moradia)"])
        estorno = next(item for item in itens if item["natureza"] == NaturezaEvento.ESTORNO)
        self.assertEqual(estorno["tipo"], "RECEITA")

    def test_programacoes_dividem_conta_data_e_saldo_residual_sem_duplicar(self):
        titulo = self.titulo_direto(
            NaturezaEvento.DESPESA,
            Decimal("300.00"),
            date(2026, 10, 29),
        )
        programacao = ProgramacaoPagamento.objects.create(
            titulo=titulo,
            conta=self.outra_conta,
            data_programada=date(2026, 10, 25),
            valor_programado=Decimal("100.00"),
            valor_realizado=Decimal("20.00"),
            status=ProgramacaoPagamento.Status.PARCIAL,
        )
        ProgramacaoPagamento.objects.create(
            titulo=titulo,
            conta=self.outra_conta,
            data_programada=date(2026, 10, 30),
            valor_programado=Decimal("50.00"),
        )

        resultado = consultar_previsoes(self.usuario, date(2026, 10, 24), date(2026, 10, 29))

        self.assertEqual(resultado["totais"]["despesas"], "250.00")
        grupos = {grupo["conta"]["nome"]: grupo for grupo in resultado["contas"]}
        self.assertEqual(grupos["Conta A"]["itens"][0]["programacaoId"], programacao.pk)
        self.assertEqual(grupos["Conta A"]["itens"][0]["valor"], "80.00")
        self.assertEqual(grupos["Conta B"]["itens"][0]["programacaoId"], None)
        self.assertEqual(grupos["Conta B"]["itens"][0]["valor"], "170.00")

    def test_inclui_faturas_fechada_e_parcial_e_exclui_demais_status(self):
        status_esperados = {
            Fatura.Status.FECHADA: "100.00",
            Fatura.Status.PARCIAL: "80.00",
        }
        for indice, (status, valor) in enumerate(status_esperados.items(), start=1):
            self.titulo_fatura(status, Decimal(valor), date(2026, 10, 26), ciclo=f"2026-{indice:02d}")
        for indice, status in enumerate(
            (Fatura.Status.ABERTA, Fatura.Status.PAGA, Fatura.Status.EM_CORRECAO),
            start=3,
        ):
            self.titulo_fatura(status, Decimal("900.00"), date(2026, 10, 26), ciclo=f"2026-{indice:02d}")

        resultado = consultar_previsoes(self.usuario, date(2026, 10, 24), date(2026, 10, 29))

        self.assertEqual(resultado["totais"]["despesas"], "180.00")
        grupo = resultado["contas"][0]
        self.assertEqual(grupo["conta"], {"id": None, "nome": "Sem conta definida"})
        self.assertTrue(all(item["origem"] == "FATURA" for item in grupo["itens"]))
        self.assertTrue(all(item["categorias"] == [] for item in grupo["itens"]))
        self.assertTrue(all(item["natureza"] == NaturezaEvento.PAGAMENTO_FATURA for item in grupo["itens"]))

    def test_periodo_e_isolamento_por_usuario_sao_inclusivos(self):
        self.titulo_direto(NaturezaEvento.DESPESA, Decimal("10.00"), date(2026, 12, 31))
        self.titulo_direto(NaturezaEvento.RECEITA, Decimal("20.00"), date(2027, 1, 1))
        conta_alheia = Conta.objects.create(usuario=self.outro, nome="Conta alheia")
        self.titulo_direto(
            NaturezaEvento.RECEITA,
            Decimal("999.00"),
            date(2027, 1, 1),
            usuario=self.outro,
            conta=conta_alheia,
        )

        resultado = consultar_previsoes(self.usuario, date(2026, 12, 31), date(2027, 1, 1))

        self.assertEqual(resultado["periodo"]["diasConsulta"], 1)
        self.assertEqual(resultado["totais"]["despesas"], "10.00")
        self.assertEqual(resultado["totais"]["receitas"], "20.00")

    def test_nao_expoe_relacionamentos_inconsistentes_de_outro_usuario(self):
        conta_alheia = Conta.objects.create(usuario=self.outro, nome="Conta sigilosa")
        categoria_alheia = Categoria.objects.create(usuario=self.outro, nome="Categoria sigilosa")
        titulo = self.titulo_direto(
            NaturezaEvento.DESPESA,
            Decimal("25.00"),
            date(2026, 10, 25),
            conta=conta_alheia,
        )
        RateioCategoria.objects.create(evento=titulo.evento, categoria=categoria_alheia, valor=Decimal("25.00"))

        resultado = consultar_previsoes(self.usuario, date(2026, 10, 24), date(2026, 10, 29))

        grupo = resultado["contas"][0]
        self.assertEqual(grupo["conta"], {"id": None, "nome": "Sem conta definida"})
        self.assertEqual(grupo["itens"][0]["categorias"], [])
        self.assertNotIn("sigilosa", json.dumps(resultado).casefold())


class PrevisoesApiTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(username="api-previsoes", password="senha")
        self.registro, self.token = criar_token_acesso(self.usuario, "n8n")
        self.url = reverse("api_previsoes_consulta")

    def post(self, dados, token=None, content_type="application/json"):
        conteudo = json.dumps(dados) if content_type == "application/json" else dados
        return self.client.post(
            self.url,
            data=conteudo,
            content_type=content_type,
            HTTP_AUTHORIZATION=f"Bearer {token or self.token}",
        )

    def test_consulta_vazia_retorna_contrato_e_atualiza_ultimo_uso(self):
        resposta = self.post({"dataConsulta": "2026-10-24", "diasConsulta": 5})

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("no-store", resposta["Cache-Control"])
        self.assertEqual(resposta.json(), {
            "ok": True,
            "periodo": {
                "dataConsulta": "2026-10-24",
                "dataFinal": "2026-10-29",
                "diasConsulta": 5,
            },
            "totais": {"receitas": "0.00", "despesas": "0.00", "saldoPrevisto": "0.00"},
            "contas": [],
        })
        self.registro.refresh_from_db()
        self.assertIsNotNone(self.registro.ultimo_uso_em)

    def test_endpoint_integra_autenticacao_consulta_e_isolamento(self):
        conta = Conta.objects.create(usuario=self.usuario, nome="Conta API")
        evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Aluguel",
            data=date(2026, 10, 24),
            vencimento=date(2026, 10, 24),
            valor=Decimal("700.00"),
            situacao=Situacao.PREVISTO,
        )
        Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao=evento.descricao,
            vencimento=evento.vencimento,
            valor_original=evento.valor,
            saldo_aberto=evento.valor,
            conta=conta,
            evento=evento,
        )
        outro = get_user_model().objects.create_user(username="api-isolado", password="senha")
        conta_alheia = Conta.objects.create(usuario=outro, nome="Conta alheia")
        evento_alheio = EventoFinanceiro.objects.create(
            usuario=outro,
            natureza=NaturezaEvento.RECEITA,
            descricao="Receita alheia",
            data=date(2026, 10, 24),
            vencimento=date(2026, 10, 24),
            valor=Decimal("999.00"),
            situacao=Situacao.PREVISTO,
        )
        Titulo.objects.create(
            usuario=outro,
            tipo=Titulo.Tipo.RECEBER,
            descricao=evento_alheio.descricao,
            vencimento=evento_alheio.vencimento,
            valor_original=evento_alheio.valor,
            saldo_aberto=evento_alheio.valor,
            conta=conta_alheia,
            evento=evento_alheio,
        )

        resposta = self.post({"dataConsulta": "2026-10-24", "diasConsulta": 0})

        dados = resposta.json()
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(dados["totais"]["despesas"], "700.00")
        self.assertEqual(dados["totais"]["receitas"], "0.00")
        self.assertEqual(dados["contas"][0]["itens"][0]["descricao"], "Aluguel")
        self.assertNotIn("alheia", json.dumps(dados).casefold())

    def test_exige_bearer_mesmo_com_sessao_e_rejeita_token_revogado(self):
        self.client.force_login(self.usuario)
        sem_token = self.client.post(
            self.url,
            data=json.dumps({"dataConsulta": "2026-10-24", "diasConsulta": 5}),
            content_type="application/json",
        )
        invalido = self.post({"dataConsulta": "2026-10-24", "diasConsulta": 5}, token="invalido")
        self.registro.ativo = False
        self.registro.save(update_fields=["ativo", "atualizado_em"])
        revogado = self.post({"dataConsulta": "2026-10-24", "diasConsulta": 5})

        self.assertEqual(sem_token.status_code, 401)
        self.assertEqual(invalido.status_code, 401)
        self.assertEqual(revogado.status_code, 401)

    def test_valida_metodo_content_type_json_data_e_dias(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(self.post({}, content_type="text/plain").status_code, 400)

        casos = (
            ({"dataConsulta": "24/10/2026", "diasConsulta": 5}, "dataConsulta"),
            ({"dataConsulta": "2026-10-24", "diasConsulta": True}, "diasConsulta"),
            ({"dataConsulta": "2026-10-24", "diasConsulta": -1}, "diasConsulta"),
            ({"dataConsulta": "2026-10-24", "diasConsulta": 366}, "diasConsulta"),
        )
        for dados, campo in casos:
            with self.subTest(campo=campo, dados=dados):
                resposta = self.post(dados)
                self.assertEqual(resposta.status_code, 400)
                self.assertIn(campo, resposta.json()["erro"]["mensagem"])

        json_invalido = self.client.post(
            self.url,
            data="{",
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(json_invalido.status_code, 400)

    def test_bearer_nao_exige_csrf(self):
        cliente = Client(enforce_csrf_checks=True)
        resposta = cliente.post(
            self.url,
            data=json.dumps({"dataConsulta": "2026-10-24", "diasConsulta": 0}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()["periodo"]["dataFinal"], "2026-10-24")


class IntegracoesViewTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(username="integracoes", password="senha")
        self.outro = get_user_model().objects.create_user(username="outras-integracoes", password="senha")
        self.url = reverse("integracoes")

    def test_exige_login(self):
        resposta = self.client.get(self.url)

        self.assertEqual(resposta.status_code, 302)
        self.assertIn(reverse("login"), resposta.url)

    def test_gera_exibe_uma_vez_e_nao_armazena_segredo(self):
        self.client.force_login(self.usuario)
        resposta = self.client.post(self.url, {"nome": "n8n WhatsApp"})

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("no-store", resposta["Cache-Control"])
        token_exibido = resposta.context["token_exibido"]
        registro = TokenAcessoApi.objects.get(usuario=self.usuario)
        self.assertTrue(token_exibido.startswith(f"{registro.prefixo}."))
        self.assertNotEqual(registro.hash_token, token_exibido)
        self.assertNotContains(self.client.get(self.url), token_exibido)

    def test_lista_somente_tokens_do_usuario_e_revoga_sem_excluir(self):
        token, _ = criar_token_acesso(self.usuario, "Meu n8n")
        criar_token_acesso(self.outro, "Token alheio")
        self.client.force_login(self.usuario)

        pagina = self.client.get(self.url)
        resposta = self.client.post(reverse("token_api_revogar", args=[token.pk]))

        self.assertContains(pagina, "Meu n8n")
        self.assertNotContains(pagina, "Token alheio")
        self.assertRedirects(resposta, self.url)
        token.refresh_from_db()
        self.assertFalse(token.ativo)
        self.assertIsNotNone(token.revogado_em)
        self.assertTrue(TokenAcessoApi.objects.filter(pk=token.pk).exists())

    def test_nao_revoga_token_de_outro_usuario(self):
        token, _ = criar_token_acesso(self.outro, "Token alheio")
        self.client.force_login(self.usuario)

        resposta = self.client.post(reverse("token_api_revogar", args=[token.pk]))

        self.assertEqual(resposta.status_code, 404)
        token.refresh_from_db()
        self.assertTrue(token.ativo)
