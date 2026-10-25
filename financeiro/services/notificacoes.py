from __future__ import annotations

import hashlib
import json
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from financeiro.models import ExecucaoCentralNotificacao, NotificacaoCentral
from financeiro.services.previsoes import consultar_previsoes


TIPO_PREVISAO_DIARIA = NotificacaoCentral.Tipo.PREVISAO_DIARIA
TEMPO_BLOQUEIO = timedelta(minutes=30)


def _hash_conteudo(conteudo: dict) -> str:
    serializado = json.dumps(conteudo, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serializado.encode("utf-8")).hexdigest()


def _moeda_br(valor: str) -> str:
    numero = Decimal(valor)
    formatado = f"{abs(numero):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    sinal = "-" if numero < 0 else ""
    return f"{sinal}R$ {formatado}"


def _titulo(data_referencia) -> str:
    return f"Previsoes de hoje - {data_referencia:%d/%m/%Y}"


def _mensagem(conteudo: dict) -> str:
    totais = conteudo["totais"]
    return (
        f"Receitas previstas: {_moeda_br(totais['receitas'])}. "
        f"Despesas previstas: {_moeda_br(totais['despesas'])}. "
        f"Saldo previsto: {_moeda_br(totais['saldoPrevisto'])}."
    )


@transaction.atomic
def processar_notificacao_usuario(usuario, data_referencia) -> str:
    conteudo = consultar_previsoes(usuario, data_referencia, data_referencia)
    existente = (
        NotificacaoCentral.objects.select_for_update()
        .filter(
            usuario=usuario,
            tipo=TIPO_PREVISAO_DIARIA,
            data_referencia=data_referencia,
        )
        .first()
    )

    if not conteudo["contas"]:
        if not existente or existente.arquivada_em:
            return "sem_resultado"
        agora = timezone.now()
        existente.lida_em = existente.lida_em or agora
        existente.arquivada_em = agora
        existente.arquivada_automaticamente = True
        existente.save(
            update_fields=["lida_em", "arquivada_em", "arquivada_automaticamente", "atualizado_em"]
        )
        return "arquivada"

    hash_atual = _hash_conteudo(conteudo)
    if not existente:
        NotificacaoCentral.objects.create(
            usuario=usuario,
            tipo=TIPO_PREVISAO_DIARIA,
            data_referencia=data_referencia,
            titulo=_titulo(data_referencia),
            mensagem=_mensagem(conteudo),
            conteudo=conteudo,
            hash_conteudo=hash_atual,
        )
        return "criada"

    if existente.hash_conteudo == hash_atual and not existente.arquivada_automaticamente:
        return "inalterada"

    existente.titulo = _titulo(data_referencia)
    existente.mensagem = _mensagem(conteudo)
    existente.conteudo = conteudo
    existente.hash_conteudo = hash_atual
    existente.lida_em = None
    existente.arquivada_em = None
    existente.arquivada_automaticamente = False
    existente.save(
        update_fields=[
            "titulo",
            "mensagem",
            "conteudo",
            "hash_conteudo",
            "lida_em",
            "arquivada_em",
            "arquivada_automaticamente",
            "atualizado_em",
        ]
    )
    return "atualizada"


def processar_previsoes_diarias(data_referencia) -> dict:
    resultado = {
        "usuarios_processados": 0,
        "notificacoes_criadas": 0,
        "notificacoes_atualizadas": 0,
        "notificacoes_arquivadas": 0,
        "erros": [],
    }
    usuarios = get_user_model().objects.filter(is_active=True).only("id").order_by("id")
    for usuario in usuarios.iterator(chunk_size=100):
        try:
            acao = processar_notificacao_usuario(usuario, data_referencia)
            resultado["usuarios_processados"] += 1
            if acao == "criada":
                resultado["notificacoes_criadas"] += 1
            elif acao == "atualizada":
                resultado["notificacoes_atualizadas"] += 1
            elif acao == "arquivada":
                resultado["notificacoes_arquivadas"] += 1
        except Exception as exc:  # Uma falha individual nao interrompe os demais usuarios.
            resultado["erros"].append({"usuarioId": usuario.pk, "erro": type(exc).__name__})
    return resultado


def _horario_agendado() -> time:
    return time.fromisoformat(settings.CENTRAL_NOTIFICACOES_HORARIO)


@transaction.atomic
def _reivindicar_execucao(data_referencia, agora: datetime, *, forcar: bool):
    execucao, _ = ExecucaoCentralNotificacao.objects.select_for_update().get_or_create(
        tipo=TIPO_PREVISAO_DIARIA,
        data_referencia=data_referencia,
    )
    if execucao.bloqueado_ate and execucao.bloqueado_ate > agora:
        return None
    if not forcar:
        if execucao.status == ExecucaoCentralNotificacao.Status.SUCESSO:
            return None
        if execucao.proxima_tentativa_em and execucao.proxima_tentativa_em > agora:
            return None
        if execucao.tentativas >= settings.CENTRAL_NOTIFICACOES_MAX_TENTATIVAS:
            return None

    execucao.status = ExecucaoCentralNotificacao.Status.PROCESSANDO
    execucao.tentativas += 1
    execucao.iniciado_em = agora
    execucao.concluido_em = None
    execucao.bloqueado_ate = agora + TEMPO_BLOQUEIO
    execucao.proxima_tentativa_em = None
    execucao.save(
        update_fields=[
            "status",
            "tentativas",
            "iniciado_em",
            "concluido_em",
            "bloqueado_ate",
            "proxima_tentativa_em",
            "atualizado_em",
        ]
    )
    return execucao


def executar_ciclo_notificacoes(*, agora: datetime | None = None, forcar: bool = False):
    agora = agora or timezone.now()
    data_referencia = timezone.localdate(agora)
    if not forcar and timezone.localtime(agora).time() < _horario_agendado():
        return None

    execucao = _reivindicar_execucao(data_referencia, agora, forcar=forcar)
    if not execucao:
        return None

    try:
        resultado = processar_previsoes_diarias(data_referencia)
    except Exception:
        ExecucaoCentralNotificacao.objects.filter(pk=execucao.pk).update(
            status=ExecucaoCentralNotificacao.Status.ERRO,
            concluido_em=agora,
            bloqueado_ate=None,
            proxima_tentativa_em=(
                agora + timedelta(seconds=settings.CENTRAL_NOTIFICACOES_INTERVALO_RETRY)
                if execucao.tentativas < settings.CENTRAL_NOTIFICACOES_MAX_TENTATIVAS
                else None
            ),
        )
        raise

    concluido_em = agora
    possui_erros = bool(resultado["erros"])
    status = (
        ExecucaoCentralNotificacao.Status.PARCIAL
        if possui_erros and resultado["usuarios_processados"]
        else ExecucaoCentralNotificacao.Status.ERRO
        if possui_erros
        else ExecucaoCentralNotificacao.Status.SUCESSO
    )
    proxima_tentativa = None
    if possui_erros and execucao.tentativas < settings.CENTRAL_NOTIFICACOES_MAX_TENTATIVAS:
        proxima_tentativa = concluido_em + timedelta(seconds=settings.CENTRAL_NOTIFICACOES_INTERVALO_RETRY)

    ExecucaoCentralNotificacao.objects.filter(pk=execucao.pk).update(
        status=status,
        concluido_em=concluido_em,
        bloqueado_ate=None,
        proxima_tentativa_em=proxima_tentativa,
        **resultado,
    )
    execucao.refresh_from_db()
    return execucao
