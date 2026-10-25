from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from financeiro.models import (
    Conta,
    EventoFinanceiro,
    ExecucaoCentralNotificacao,
    NaturezaEvento,
    NotificacaoCentral,
    Situacao,
    Titulo,
)
from financeiro.services.notificacoes import (
    executar_ciclo_notificacoes,
    processar_notificacao_usuario,
    processar_previsoes_diarias,
)


DATA_REFERENCIA = date(2026, 10, 25)
FUSO_CUIABA = ZoneInfo("America/Cuiaba")


def conteudo_previsao(valor="100.00"):
    return {
        "ok": True,
        "periodo": {
            "dataConsulta": DATA_REFERENCIA.isoformat(),
            "dataFinal": DATA_REFERENCIA.isoformat(),
            "diasConsulta": 0,
        },
        "totais": {"receitas": "0.00", "despesas": valor, "saldoPrevisto": f"-{valor}"},
        "contas": [
            {
                "conta": {"id": 1, "nome": "Conta principal"},
                "totais": {"receitas": "0.00", "despesas": valor, "saldoPrevisto": f"-{valor}"},
                "itens": [
                    {
                        "tituloId": 1,
                        "programacaoId": None,
                        "tipo": "DESPESA",
                        "natureza": NaturezaEvento.DESPESA,
                        "origem": "CONTA",
                        "descricao": "Energia",
                        "data": DATA_REFERENCIA.isoformat(),
                        "categorias": ["Moradia"],
                        "valor": valor,
                        "fatura": None,
                    }
                ],
            }
        ],
    }


class NotificacoesServiceTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(username="notificado", password="senha")
        self.conta = Conta.objects.create(usuario=self.usuario, nome="Conta principal")
        self.evento = EventoFinanceiro.objects.create(
            usuario=self.usuario,
            natureza=NaturezaEvento.DESPESA,
            descricao="Energia",
            data=DATA_REFERENCIA,
            vencimento=DATA_REFERENCIA,
            valor=Decimal("100.00"),
            situacao=Situacao.PREVISTO,
        )
        self.titulo = Titulo.objects.create(
            usuario=self.usuario,
            tipo=Titulo.Tipo.PAGAR,
            descricao="Energia",
            vencimento=DATA_REFERENCIA,
            valor_original=Decimal("100.00"),
            saldo_aberto=Decimal("100.00"),
            conta=self.conta,
            evento=self.evento,
        )

    def test_usa_consulta_do_dia_e_armazena_retorno_completo(self):
        acao = processar_notificacao_usuario(self.usuario, DATA_REFERENCIA)

        notificacao = NotificacaoCentral.objects.get(usuario=self.usuario)
        self.assertEqual(acao, "criada")
        self.assertEqual(notificacao.conteudo["periodo"]["diasConsulta"], 0)
        self.assertEqual(notificacao.conteudo["periodo"]["dataConsulta"], "2026-10-25")
        self.assertEqual(notificacao.conteudo["totais"]["despesas"], "100.00")
        self.assertEqual(notificacao.conteudo["contas"][0]["itens"][0]["descricao"], "Energia")

    def test_resultado_igual_preserva_leitura_e_arquivamento_manual(self):
        processar_notificacao_usuario(self.usuario, DATA_REFERENCIA)
        notificacao = NotificacaoCentral.objects.get(usuario=self.usuario)
        agora = timezone.now()
        notificacao.lida_em = agora
        notificacao.arquivada_em = agora
        notificacao.save(update_fields=["lida_em", "arquivada_em", "atualizado_em"])

        acao = processar_notificacao_usuario(self.usuario, DATA_REFERENCIA)

        notificacao.refresh_from_db()
        self.assertEqual(acao, "inalterada")
        self.assertEqual(notificacao.lida_em, agora)
        self.assertEqual(notificacao.arquivada_em, agora)

    def test_resultado_alterado_atualiza_e_reabre(self):
        processar_notificacao_usuario(self.usuario, DATA_REFERENCIA)
        notificacao = NotificacaoCentral.objects.get(usuario=self.usuario)
        agora = timezone.now()
        notificacao.lida_em = agora
        notificacao.arquivada_em = agora
        notificacao.save(update_fields=["lida_em", "arquivada_em", "atualizado_em"])
        self.titulo.saldo_aberto = Decimal("80.00")
        self.titulo.save(update_fields=["saldo_aberto", "atualizado_em"])

        acao = processar_notificacao_usuario(self.usuario, DATA_REFERENCIA)

        notificacao.refresh_from_db()
        self.assertEqual(acao, "atualizada")
        self.assertEqual(notificacao.conteudo["totais"]["despesas"], "80.00")
        self.assertIsNone(notificacao.lida_em)
        self.assertIsNone(notificacao.arquivada_em)

    def test_resultado_vazio_arquiva_e_reaparecimento_reabre(self):
        processar_notificacao_usuario(self.usuario, DATA_REFERENCIA)
        self.evento.situacao = Situacao.REALIZADO
        self.evento.save(update_fields=["situacao", "atualizado_em"])

        acao_vazia = processar_notificacao_usuario(self.usuario, DATA_REFERENCIA)
        notificacao = NotificacaoCentral.objects.get(usuario=self.usuario)
        self.assertEqual(acao_vazia, "arquivada")
        self.assertTrue(notificacao.arquivada_automaticamente)
        self.assertIsNotNone(notificacao.arquivada_em)

        self.evento.situacao = Situacao.PREVISTO
        self.evento.save(update_fields=["situacao", "atualizado_em"])
        acao_retorno = processar_notificacao_usuario(self.usuario, DATA_REFERENCIA)

        notificacao.refresh_from_db()
        self.assertEqual(acao_retorno, "atualizada")
        self.assertFalse(notificacao.arquivada_automaticamente)
        self.assertIsNone(notificacao.lida_em)
        self.assertIsNone(notificacao.arquivada_em)

    def test_processa_usuarios_ativos_e_ignora_inativos(self):
        inativo = get_user_model().objects.create_user(username="inativo", password="senha", is_active=False)
        Conta.objects.create(usuario=inativo, nome="Conta inativa")

        resultado = processar_previsoes_diarias(DATA_REFERENCIA)

        self.assertEqual(resultado["usuarios_processados"], 1)
        self.assertEqual(resultado["notificacoes_criadas"], 1)
        self.assertFalse(NotificacaoCentral.objects.filter(usuario=inativo).exists())


@override_settings(
    CENTRAL_NOTIFICACOES_HORARIO="06:00",
    CENTRAL_NOTIFICACOES_INTERVALO_RETRY=900,
    CENTRAL_NOTIFICACOES_MAX_TENTATIVAS=3,
)
class AgendadorNotificacoesTests(TestCase):
    def resultado(self, *, erros=None):
        return {
            "usuarios_processados": 1,
            "notificacoes_criadas": 1,
            "notificacoes_atualizadas": 0,
            "notificacoes_arquivadas": 0,
            "erros": erros or [],
        }

    @patch("financeiro.services.notificacoes.processar_previsoes_diarias")
    def test_executa_as_seis_e_nao_duplica_execucao_concluida(self, processar):
        processar.return_value = self.resultado()
        antes = datetime(2026, 10, 25, 5, 59, tzinfo=FUSO_CUIABA)
        horario = datetime(2026, 10, 25, 6, 0, tzinfo=FUSO_CUIABA)

        self.assertIsNone(executar_ciclo_notificacoes(agora=antes))
        execucao = executar_ciclo_notificacoes(agora=horario)
        repetida = executar_ciclo_notificacoes(agora=horario + timedelta(hours=1))

        self.assertEqual(execucao.status, ExecucaoCentralNotificacao.Status.SUCESSO)
        self.assertIsNone(repetida)
        self.assertEqual(processar.call_count, 1)
        self.assertEqual(execucao.tentativas, 1)

    @patch("financeiro.services.notificacoes.processar_previsoes_diarias")
    def test_recupera_execucao_ausente_quando_servidor_retorna(self, processar):
        processar.return_value = self.resultado()
        retorno = datetime(2026, 10, 25, 10, 30, tzinfo=FUSO_CUIABA)

        execucao = executar_ciclo_notificacoes(agora=retorno)

        self.assertEqual(execucao.data_referencia, DATA_REFERENCIA)
        self.assertEqual(execucao.status, ExecucaoCentralNotificacao.Status.SUCESSO)

    @patch("financeiro.services.notificacoes.processar_previsoes_diarias")
    def test_respeita_bloqueio_e_retry_de_quinze_minutos(self, processar):
        processar.side_effect = [self.resultado(erros=[{"usuarioId": 1, "erro": "ErroTeste"}]), self.resultado()]
        horario = datetime(2026, 10, 25, 6, 0, tzinfo=FUSO_CUIABA)

        parcial = executar_ciclo_notificacoes(agora=horario)
        antes_retry = executar_ciclo_notificacoes(agora=horario + timedelta(minutes=10))
        sucesso = executar_ciclo_notificacoes(agora=horario + timedelta(minutes=16))

        self.assertEqual(parcial.status, ExecucaoCentralNotificacao.Status.PARCIAL)
        self.assertIsNone(antes_retry)
        self.assertEqual(sucesso.status, ExecucaoCentralNotificacao.Status.SUCESSO)
        self.assertEqual(sucesso.tentativas, 2)
        self.assertEqual(processar.call_count, 2)

    @patch("financeiro.services.notificacoes.processar_previsoes_diarias")
    def test_bloqueio_impede_execucao_concorrente_ate_mesmo_forcada(self, processar):
        agora = datetime(2026, 10, 25, 6, 0, tzinfo=FUSO_CUIABA)
        ExecucaoCentralNotificacao.objects.create(
            tipo=NotificacaoCentral.Tipo.PREVISAO_DIARIA,
            data_referencia=DATA_REFERENCIA,
            bloqueado_ate=agora + timedelta(minutes=20),
        )

        execucao = executar_ciclo_notificacoes(agora=agora, forcar=True)

        self.assertIsNone(execucao)
        processar.assert_not_called()


class NotificacoesViewsTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(username="central", password="senha")
        self.outro = get_user_model().objects.create_user(username="central-outro", password="senha")
        self.notificacao = self.criar_notificacao(self.usuario, DATA_REFERENCIA)

    def criar_notificacao(self, usuario, data_referencia, *, lida=False, arquivada=False):
        agora = timezone.now()
        return NotificacaoCentral.objects.create(
            usuario=usuario,
            tipo=NotificacaoCentral.Tipo.PREVISAO_DIARIA,
            data_referencia=data_referencia,
            titulo=f"Previsões de {data_referencia:%d/%m/%Y}",
            mensagem="Mensagem financeira",
            conteudo=conteudo_previsao(),
            hash_conteudo=f"hash-{usuario.pk}-{data_referencia}",
            lida_em=agora if lida else None,
            arquivada_em=agora if arquivada else None,
        )

    def test_central_exige_login_e_lista_somente_dados_do_usuario(self):
        url = reverse("notificacoes_central")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.criar_notificacao(self.outro, DATA_REFERENCIA)
        self.client.force_login(self.usuario)

        resposta = self.client.get(url)

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("no-store", resposta["Cache-Control"])
        dados = resposta.json()
        self.assertEqual(dados["naoLidas"], 1)
        self.assertEqual([item["id"] for item in dados["notificacoes"]], [self.notificacao.pk])

    def test_ler_arquivar_e_restaurar_preservam_regras_de_estado(self):
        self.client.force_login(self.usuario)

        lida = self.client.post(reverse("notificacao_marcar_lida", args=[self.notificacao.pk]))
        self.notificacao.refresh_from_db()
        self.assertEqual(lida.json()["naoLidas"], 0)
        self.assertIsNotNone(self.notificacao.lida_em)

        arquivada = self.client.post(reverse("notificacao_arquivar", args=[self.notificacao.pk]))
        self.notificacao.refresh_from_db()
        self.assertEqual(arquivada.status_code, 200)
        self.assertIsNotNone(self.notificacao.arquivada_em)
        self.assertFalse(self.notificacao.arquivada_automaticamente)

        restaurada = self.client.post(reverse("notificacao_restaurar", args=[self.notificacao.pk]))
        self.notificacao.refresh_from_db()
        self.assertEqual(restaurada.status_code, 200)
        self.assertIsNone(self.notificacao.arquivada_em)
        self.assertIsNotNone(self.notificacao.lida_em)

    def test_marcar_todas_como_lidas_nao_altera_outro_usuario(self):
        outra = self.criar_notificacao(self.outro, DATA_REFERENCIA)
        self.client.force_login(self.usuario)

        resposta = self.client.post(reverse("notificacoes_marcar_todas_lidas"))

        self.notificacao.refresh_from_db()
        outra.refresh_from_db()
        self.assertEqual(resposta.json(), {"ok": True, "atualizadas": 1, "naoLidas": 0})
        self.assertIsNotNone(self.notificacao.lida_em)
        self.assertIsNone(outra.lida_em)

    def test_mutacoes_rejeitam_notificacao_de_outro_usuario(self):
        alheia = self.criar_notificacao(self.outro, DATA_REFERENCIA)
        self.client.force_login(self.usuario)

        for nome in ("notificacao_marcar_lida", "notificacao_arquivar", "notificacao_restaurar"):
            with self.subTest(nome=nome):
                self.assertEqual(self.client.post(reverse(nome, args=[alheia.pk])).status_code, 404)

    def test_post_exige_csrf(self):
        cliente = Client(enforce_csrf_checks=True)
        cliente.force_login(self.usuario)
        cliente.get(reverse("dashboard"))

        resposta = cliente.post(reverse("notificacao_arquivar", args=[self.notificacao.pk]))

        self.assertEqual(resposta.status_code, 403)

    def test_dashboard_renderiza_sino_painel_e_rotas(self):
        self.client.force_login(self.usuario)

        resposta = self.client.get(reverse("dashboard"))

        self.assertContains(resposta, "data-notifications-toggle")
        self.assertContains(resposta, 'id="notificationCenter"')
        self.assertContains(resposta, reverse("notificacoes_central"))
        self.assertContains(resposta, reverse("notificacoes_marcar_todas_lidas"))
