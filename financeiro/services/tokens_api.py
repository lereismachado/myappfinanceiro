from __future__ import annotations

from hashlib import sha256
from secrets import compare_digest, token_hex, token_urlsafe

from django.utils import timezone

from financeiro.models import TokenAcessoApi


PREFIXO_TOKEN = "mfin_"


def _hash_token(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def criar_token_acesso(usuario, nome: str) -> tuple[TokenAcessoApi, str]:
    prefixo = f"{PREFIXO_TOKEN}{token_hex(6)}"
    token = f"{prefixo}.{token_urlsafe(32)}"
    registro = TokenAcessoApi.objects.create(
        usuario=usuario,
        nome=nome.strip(),
        prefixo=prefixo,
        hash_token=_hash_token(token),
    )
    return registro, token


def autenticar_token_bearer(cabecalho: str) -> TokenAcessoApi | None:
    esquema, separador, token = (cabecalho or "").partition(" ")
    if not separador or esquema.casefold() != "bearer" or not token or len(token) > 256:
        return None

    prefixo, separador, segredo = token.partition(".")
    if not separador or not segredo or not prefixo.startswith(PREFIXO_TOKEN):
        return None

    registro = (
        TokenAcessoApi.objects.select_related("usuario")
        .filter(prefixo=prefixo, ativo=True, revogado_em__isnull=True)
        .first()
    )
    if not registro or not compare_digest(registro.hash_token, _hash_token(token)):
        return None

    agora = timezone.now()
    TokenAcessoApi.objects.filter(pk=registro.pk).update(ultimo_uso_em=agora)
    registro.ultimo_uso_em = agora
    return registro
