import json
from datetime import date, timedelta

from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from .services.previsoes import consultar_previsoes
from .services.tokens_api import autenticar_token_bearer


def _erro(codigo: str, mensagem: str, status: int) -> JsonResponse:
    return JsonResponse(
        {"ok": False, "erro": {"codigo": codigo, "mensagem": mensagem}},
        status=status,
    )


@csrf_exempt
@never_cache
def consultar_previsoes_view(request):
    if request.method != "POST":
        resposta = _erro("metodo_nao_permitido", "Utilize o método POST.", 405)
        resposta["Allow"] = "POST"
        return resposta

    token = autenticar_token_bearer(request.headers.get("Authorization", ""))
    if not token:
        return _erro("nao_autorizado", "Token ausente, inválido ou revogado.", 401)
    if request.content_type != "application/json":
        return _erro("conteudo_invalido", "Envie o corpo como application/json.", 400)

    try:
        dados = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _erro("json_invalido", "O corpo da requisição não contém um JSON válido.", 400)
    if not isinstance(dados, dict):
        return _erro("json_invalido", "O corpo da requisição deve ser um objeto JSON.", 400)

    data_consulta = dados.get("dataConsulta")
    dias_consulta = dados.get("diasConsulta")
    try:
        inicio = date.fromisoformat(data_consulta) if isinstance(data_consulta, str) else None
    except ValueError:
        inicio = None
    if not inicio:
        return _erro("parametros_invalidos", "dataConsulta deve usar o formato AAAA-MM-DD.", 400)
    if isinstance(dias_consulta, bool) or not isinstance(dias_consulta, int) or not 0 <= dias_consulta <= 365:
        return _erro("parametros_invalidos", "diasConsulta deve ser um número inteiro entre 0 e 365.", 400)
    try:
        fim = inicio + timedelta(days=dias_consulta)
    except OverflowError:
        return _erro("parametros_invalidos", "O período informado ultrapassa o limite de datas suportado.", 400)

    return JsonResponse(consultar_previsoes(token.usuario, inicio, fim))
