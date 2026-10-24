from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils.text import slugify
from decimal import Decimal, InvalidOperation

from .vencimentos_cartao import VencimentoCartaoInvalido, resolver_vencimento_fatura


class TimestampedModel(models.Model):
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Ambito(models.TextChoices):
    PESSOAL = "PESSOAL", "Pessoal"
    PROFISSIONAL = "PROFISSIONAL", "Profissional"
    NAO_DEFINIDO = "NAO_DEFINIDO", "Nao definido"


class Situacao(models.TextChoices):
    REALIZADO = "REALIZADO", "Realizado"
    PREVISTO = "PREVISTO", "Previsto"
    PENDENTE = "PENDENTE", "Pendente"
    CANCELADO = "CANCELADO", "Cancelado"


class NaturezaEvento(models.TextChoices):
    DESPESA = "DESPESA", "Despesa"
    RECEITA = "RECEITA", "Receita"
    ESTORNO = "ESTORNO", "Estorno"
    PAGAMENTO_FATURA = "PAGAMENTO_FATURA", "Pagamento de fatura"
    TRANSFERENCIA = "TRANSFERENCIA", "Transferencia"


class Conta(TimestampedModel):
    class Tipo(models.TextChoices):
        CORRENTE = "CORRENTE", "Conta corrente"
        POUPANCA = "POUPANCA", "Poupanca"
        CARTEIRA = "CARTEIRA", "Carteira"
        PAGAMENTO = "PAGAMENTO", "Conta de pagamento"

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    nome = models.CharField(max_length=120)
    banco = models.CharField(max_length=120, blank=True)
    agencia = models.CharField(max_length=30, blank=True)
    numero = models.CharField(max_length=60, blank=True)
    titular = models.CharField(max_length=160, blank=True)
    tipo = models.CharField(max_length=20, choices=Tipo.choices, default=Tipo.CORRENTE)
    ambito = models.CharField(max_length=20, choices=Ambito.choices, default=Ambito.NAO_DEFINIDO)
    saldo_inicial = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    data_saldo_inicial = models.DateField(null=True, blank=True)
    limite_credito = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    nao_compoe_dashboard = models.BooleanField(default=False)
    ativa = models.BooleanField(default=True)

    class Meta:
        ordering = ["nome"]
        indexes = [
            models.Index(fields=["usuario", "ativa"]),
            models.Index(fields=["usuario", "banco", "numero"]),
        ]

    def __str__(self):
        return self.nome


class Pessoa(TimestampedModel):
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    nome = models.CharField(max_length=160)
    documento = models.CharField(max_length=40, blank=True)
    observacao = models.TextField(blank=True)
    ativa = models.BooleanField(default=True)

    class Meta:
        ordering = ["nome"]
        constraints = [
            models.UniqueConstraint(fields=["usuario", "nome"], name="uniq_pessoa_usuario_nome"),
        ]

    def __str__(self):
        return self.nome


class Categoria(TimestampedModel):
    class Tipo(models.TextChoices):
        DESPESA = "DESPESA", "Despesa"
        RECEITA = "RECEITA", "Receita"
        TRANSFERENCIA = "TRANSFERENCIA", "Transferência"

    class Natureza(models.TextChoices):
        PESSOAL = "PESSOAL", "Pessoal"
        EMPRESARIAL = "EMPRESARIAL", "Empresarial"
        AMBOS = "AMBOS", "Pessoal / Empresarial"

    class Essencialidade(models.TextChoices):
        ESSENCIAL = "ESSENCIAL", "Essencial"
        NECESSARIO_VARIAVEL = "NECESSARIO_VARIAVEL", "Necessário variável"
        PROFISSIONAL = "PROFISSIONAL", "Profissional"
        ESTILO_VIDA = "ESTILO_VIDA", "Estilo de vida"
        SUPERFLUO = "SUPERFLUO", "Supérfluo"
        FINANCEIRO = "FINANCEIRO", "Financeiro"
        NEUTRO = "NEUTRO", "Neutro"

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    nome = models.CharField(max_length=120)
    codigo = models.SlugField(max_length=140, blank=True)
    tipo = models.CharField(max_length=20, choices=Tipo.choices, default=Tipo.DESPESA)
    categoria_pai = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    natureza = models.CharField(max_length=20, choices=Natureza.choices, default=Natureza.PESSOAL)
    essencialidade = models.CharField(
        max_length=30,
        choices=Essencialidade.choices,
        default=Essencialidade.NECESSARIO_VARIAVEL,
    )
    computa_resultado = models.BooleanField(default=True)
    ativa = models.BooleanField(default=True)

    class Meta:
        ordering = ["nome"]
        constraints = [
            models.UniqueConstraint(fields=["usuario", "nome"], name="uniq_categoria_usuario_nome"),
            models.UniqueConstraint(
                fields=["usuario", "codigo"],
                condition=~Q(codigo=""),
                name="uniq_categoria_usuario_codigo",
            ),
        ]

    def __str__(self):
        return self.nome

    @property
    def rotulo_com_pai(self):
        if self.categoria_pai_id:
            return f"{self.nome} ({self.categoria_pai.nome})"
        return self.nome

    def clean(self):
        super().clean()
        if not self.categoria_pai_id:
            return
        if self.pk and self.categoria_pai_id == self.pk:
            raise ValidationError({"categoria_pai": "Uma categoria não pode ser superior a ela própria."})
        if self.usuario_id and self.categoria_pai.usuario_id != self.usuario_id:
            raise ValidationError({"categoria_pai": "A categoria superior deve pertencer ao mesmo usuário."})

        visitados = {self.pk} if self.pk else set()
        ancestral = self.categoria_pai
        while ancestral:
            if ancestral.pk in visitados:
                raise ValidationError({"categoria_pai": "A categoria superior criaria um ciclo na hierarquia."})
            visitados.add(ancestral.pk)
            ancestral = ancestral.categoria_pai

    def save(self, *args, **kwargs):
        codigo_criado = False
        if not self.codigo and self.usuario_id:
            base = slugify(self.nome)[:120] or "categoria"
            codigo = base
            sufixo = 2
            while Categoria.objects.filter(usuario_id=self.usuario_id, codigo=codigo).exclude(pk=self.pk).exists():
                codigo = f"{base[:130 - len(str(sufixo))]}-{sufixo}"
                sufixo += 1
            self.codigo = codigo
            codigo_criado = True
        if codigo_criado and kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"codigo"}
        super().save(*args, **kwargs)


class Cartao(TimestampedModel):
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    nome = models.CharField(max_length=120)
    emissor = models.CharField(max_length=120, blank=True)
    identificador_externo = models.CharField(max_length=80, blank=True)
    final = models.CharField(max_length=4, blank=True)
    dia_fechamento = models.PositiveSmallIntegerField(null=True, blank=True)
    dia_vencimento = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(31)],
    )
    conta_pagamento_padrao = models.ForeignKey(Conta, null=True, blank=True, on_delete=models.PROTECT)
    limite = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ["nome"]
        indexes = [
            models.Index(fields=["usuario", "ativo"]),
            models.Index(fields=["usuario", "identificador_externo"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(dia_vencimento__gte=1, dia_vencimento__lte=31),
                name="cartao_dia_vencimento_valido",
            ),
        ]

    def __str__(self):
        return self.nome


class Fatura(TimestampedModel):
    class Status(models.TextChoices):
        ABERTA = "ABERTA", "Aberta"
        FECHADA = "FECHADA", "Fechada"
        PAGA = "PAGA", "Paga"
        PARCIAL = "PARCIAL", "Parcial"
        EM_CORRECAO = "EM_CORRECAO", "Em correção"

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    cartao = models.ForeignKey(Cartao, on_delete=models.PROTECT)
    ciclo = models.CharField(max_length=7)
    fechamento = models.DateField(null=True, blank=True)
    vencimento = models.DateField(null=True, blank=True)
    total_bruto = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_creditos = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    # ``total`` continua sendo a composição própria do ciclo. O saldo trazido
    # de outro ciclo é uma obrigação, e não uma nova despesa.
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_transportado = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_exigido = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ABERTA)

    class Meta:
        ordering = ["-ciclo", "cartao__nome"]
        constraints = [
            models.UniqueConstraint(fields=["cartao", "ciclo"], name="uniq_fatura_cartao_ciclo"),
        ]

    def __str__(self):
        return f"{self.cartao} {self.ciclo}"


class SerieLancamentoRecorrente(TimestampedModel):
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    quantidade_ocorrencias = models.PositiveSmallIntegerField()
    intervalo_meses = models.PositiveSmallIntegerField()

    class Meta:
        indexes = [
            models.Index(fields=["usuario", "criado_em"]),
        ]


class EventoFinanceiro(TimestampedModel):
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    natureza = models.CharField(max_length=30, choices=NaturezaEvento.choices)
    descricao = models.CharField(max_length=220)
    data = models.DateField()
    vencimento = models.DateField(null=True, blank=True)
    valor = models.DecimalField(max_digits=14, decimal_places=2)
    pessoa = models.ForeignKey(Pessoa, null=True, blank=True, on_delete=models.PROTECT)
    pessoa_nome_livre = models.CharField(max_length=160, blank=True)
    ambito = models.CharField(max_length=20, choices=Ambito.choices, default=Ambito.NAO_DEFINIDO)
    situacao = models.CharField(max_length=20, choices=Situacao.choices, default=Situacao.REALIZADO)
    observacao = models.TextField(blank=True)
    serie_recorrente = models.ForeignKey(
        SerieLancamentoRecorrente,
        null=True,
        blank=True,
        related_name="eventos",
        on_delete=models.PROTECT,
    )
    numero_recorrencia = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-data", "-id"]
        indexes = [
            models.Index(fields=["usuario", "data"]),
            models.Index(fields=["usuario", "natureza", "situacao"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["serie_recorrente", "numero_recorrencia"],
                name="uniq_evento_serie_recorrencia_numero",
            ),
        ]

    def __str__(self):
        return self.descricao


class RateioCategoria(TimestampedModel):
    evento = models.ForeignKey(EventoFinanceiro, related_name="rateios", on_delete=models.CASCADE)
    categoria = models.ForeignKey(Categoria, on_delete=models.PROTECT)
    valor = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["evento", "categoria"], name="uniq_rateio_evento_categoria"),
        ]


class MovimentoConta(TimestampedModel):
    conta = models.ForeignKey(Conta, on_delete=models.PROTECT)
    evento = models.ForeignKey(EventoFinanceiro, related_name="movimentos_conta", on_delete=models.CASCADE)
    data_efetiva = models.DateField()
    valor = models.DecimalField(max_digits=14, decimal_places=2)
    documento = models.CharField(max_length=80, blank=True)
    historico_original = models.TextField(blank=True)
    chave_idempotencia = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["-data_efetiva", "-id"]
        indexes = [
            models.Index(fields=["conta", "data_efetiva"]),
            models.Index(fields=["documento"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["conta", "chave_idempotencia"],
                condition=~Q(chave_idempotencia=""),
                name="uniq_movimento_conta_idempotencia",
            ),
        ]


class SaldoContaPeriodo(TimestampedModel):
    conta = models.ForeignKey(Conta, related_name="saldos_periodo", on_delete=models.CASCADE)
    periodo = models.DateField()
    entradas = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    saidas = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    saldo_fechamento = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    quantidade_movimentos = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["conta_id", "periodo"]
        constraints = [
            models.UniqueConstraint(fields=["conta", "periodo"], name="uniq_saldo_conta_periodo"),
        ]
        indexes = [
            models.Index(fields=["conta", "periodo"]),
        ]


class CompraCartao(TimestampedModel):
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    cartao = models.ForeignKey(Cartao, on_delete=models.PROTECT)
    evento = models.ForeignKey(EventoFinanceiro, related_name="compras_cartao", on_delete=models.CASCADE)
    estabelecimento = models.CharField(max_length=180)
    data_compra = models.DateField()
    valor_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    valor_total_estimado = models.BooleanField(default=False)
    total_parcelas = models.PositiveSmallIntegerField(default=1)
    chave_compra = models.CharField(max_length=120, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["usuario", "cartao", "data_compra"]),
            models.Index(fields=["usuario", "chave_compra"]),
        ]


class ParcelaCartao(TimestampedModel):
    compra = models.ForeignKey(CompraCartao, related_name="parcelas", on_delete=models.CASCADE)
    fatura = models.ForeignKey(Fatura, related_name="parcelas", on_delete=models.PROTECT)
    natureza = models.CharField(max_length=30, choices=NaturezaEvento.choices, blank=True)
    descricao = models.CharField(max_length=220, blank=True)
    data_original = models.DateField(null=True, blank=True)
    vencimento = models.DateField(db_index=True)
    categoria = models.ForeignKey(Categoria, null=True, blank=True, on_delete=models.PROTECT)
    pessoa = models.ForeignKey(Pessoa, null=True, blank=True, on_delete=models.PROTECT)
    pessoa_nome_livre = models.CharField(max_length=160, blank=True)
    ambito = models.CharField(max_length=20, choices=Ambito.choices, blank=True)
    numero = models.PositiveSmallIntegerField(default=1)
    total = models.PositiveSmallIntegerField(default=1)
    valor = models.DecimalField(max_digits=14, decimal_places=2)
    situacao = models.CharField(max_length=20, choices=Situacao.choices, default=Situacao.REALIZADO)

    class Meta:
        ordering = ["fatura__ciclo", "numero"]
        constraints = [
            models.UniqueConstraint(fields=["compra", "numero"], name="uniq_parcela_compra_numero"),
        ]

    @property
    def natureza_exibicao(self):
        return self.natureza or self.compra.evento.natureza

    @property
    def descricao_exibicao(self):
        return self.descricao or self.compra.evento.descricao

    @property
    def data_original_exibicao(self):
        return self.data_original or self.compra.data_compra

    def _vencimento_esperado(self):
        try:
            return resolver_vencimento_fatura(self.fatura)
        except VencimentoCartaoInvalido as exc:
            raise ValidationError({"vencimento": str(exc)}) from exc

    def clean(self):
        super().clean()
        esperado = self._vencimento_esperado()
        if self.vencimento and self.vencimento != esperado:
            raise ValidationError({"vencimento": "O vencimento da parcela deve acompanhar o vencimento da fatura."})
        self.vencimento = esperado

    def save(self, *args, **kwargs):
        esperado = self._vencimento_esperado()
        if self.vencimento and self.vencimento != esperado:
            raise ValidationError({"vencimento": "O vencimento da parcela deve acompanhar o vencimento da fatura."})
        self.vencimento = esperado
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"vencimento"}
        super().save(*args, **kwargs)

    @property
    def ambito_exibicao(self):
        return self.ambito or self.compra.evento.ambito


class Titulo(TimestampedModel):
    class Tipo(models.TextChoices):
        RECEBER = "RECEBER", "Receber"
        PAGAR = "PAGAR", "Pagar"

    class Status(models.TextChoices):
        ABERTO = "ABERTO", "Aberto"
        SUSPENSO = "SUSPENSO", "Suspenso"
        LIQUIDADO = "LIQUIDADO", "Liquidado"

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    descricao = models.CharField(max_length=220)
    vencimento = models.DateField()
    valor_original = models.DecimalField(max_digits=14, decimal_places=2)
    saldo_aberto = models.DecimalField(max_digits=14, decimal_places=2)
    valor_transportado = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ABERTO)
    conta = models.ForeignKey(Conta, null=True, blank=True, on_delete=models.PROTECT)
    fatura = models.ForeignKey(Fatura, null=True, blank=True, on_delete=models.PROTECT)
    evento = models.OneToOneField(
        EventoFinanceiro,
        null=True,
        blank=True,
        related_name="titulo_previsto",
        on_delete=models.PROTECT,
    )
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ["vencimento"]
        indexes = [
            models.Index(
                fields=["usuario", "tipo", "vencimento"],
                condition=Q(ativo=True),
                name="idx_titulo_pendente",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["fatura"],
                condition=Q(fatura__isnull=False),
                name="uniq_titulo_por_fatura",
            ),
        ]


class Liquidacao(TimestampedModel):
    class Status(models.TextChoices):
        ATIVA = "ATIVA", "Ativa"
        ESTORNADA = "ESTORNADA", "Estornada"

    class Origem(models.TextChoices):
        CONCILIACAO = "CONCILIACAO", "Conciliação"
        MANUAL = "MANUAL", "Manual"
        IMPORTACAO = "IMPORTACAO", "Importação"

    titulo = models.ForeignKey(Titulo, related_name="liquidacoes", on_delete=models.PROTECT)
    movimento_conta = models.ForeignKey(MovimentoConta, null=True, blank=True, on_delete=models.PROTECT)
    valor = models.DecimalField(max_digits=14, decimal_places=2)
    data = models.DateField()
    observacao = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ATIVA)
    origem = models.CharField(max_length=20, choices=Origem.choices, default=Origem.CONCILIACAO)
    chave_idempotencia = models.CharField(max_length=80, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["titulo", "movimento_conta"],
                condition=Q(movimento_conta__isnull=False, status="ATIVA"),
                name="uniq_liquidacao_ativa_titulo_movimento",
            ),
            models.UniqueConstraint(
                fields=["titulo", "chave_idempotencia"],
                condition=~Q(chave_idempotencia=""),
                name="uniq_liquidacao_titulo_idempotencia",
            ),
        ]


class FechamentoFatura(TimestampedModel):
    class Status(models.TextChoices):
        ATIVO = "ATIVO", "Ativo"
        REABERTO = "REABERTO", "Reaberto"
        SUBSTITUIDO = "SUBSTITUIDO", "Substituído"

    fatura = models.ForeignKey(Fatura, related_name="fechamentos", on_delete=models.PROTECT)
    versao = models.PositiveIntegerField()
    total_bruto = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_creditos = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_proprio = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_transportado = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_exigido = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    vencimento = models.DateField()
    conta_pagamento = models.ForeignKey(Conta, null=True, blank=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ATIVO)
    observacao = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["fatura", "versao"], name="uniq_fechamento_fatura_versao")]
        ordering = ["fatura_id", "-versao"]


class ItemFechamentoFatura(TimestampedModel):
    fechamento = models.ForeignKey(FechamentoFatura, related_name="itens", on_delete=models.CASCADE)
    parcela = models.ForeignKey(ParcelaCartao, null=True, blank=True, on_delete=models.PROTECT)
    descricao = models.CharField(max_length=220)
    natureza = models.CharField(max_length=30, choices=NaturezaEvento.choices)
    valor = models.DecimalField(max_digits=14, decimal_places=2)


class HistoricoFatura(TimestampedModel):
    class Evento(models.TextChoices):
        FECHAMENTO = "FECHAMENTO", "Fechamento"
        REABERTURA = "REABERTURA", "Reabertura"
        CORRECAO = "CORRECAO", "Correção"
        PROGRAMACAO = "PROGRAMACAO", "Programação"
        LIQUIDACAO = "LIQUIDACAO", "Liquidação"
        TRANSPORTE_SAIDA = "TRANSPORTE_SAIDA", "Saldo transportado"
        TRANSPORTE_ENTRADA = "TRANSPORTE_ENTRADA", "Saldo recebido"

    fatura = models.ForeignKey(Fatura, related_name="historico", on_delete=models.PROTECT)
    evento = models.CharField(max_length=30, choices=Evento.choices)
    valor = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    descricao = models.CharField(max_length=255)
    dados = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-criado_em", "-id"]


class ProgramacaoPagamento(TimestampedModel):
    class Status(models.TextChoices):
        ATIVA = "ATIVA", "Ativa"
        PARCIAL = "PARCIAL", "Parcial"
        CONCLUIDA = "CONCLUIDA", "Concluída"
        CANCELADA = "CANCELADA", "Cancelada"
        SUBSTITUIDA = "SUBSTITUIDA", "Substituída"

    titulo = models.ForeignKey(Titulo, related_name="programacoes", on_delete=models.PROTECT)
    conta = models.ForeignKey(Conta, on_delete=models.PROTECT)
    data_programada = models.DateField()
    valor_programado = models.DecimalField(max_digits=14, decimal_places=2)
    valor_realizado = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ATIVA)
    chave_idempotencia = models.CharField(max_length=80, blank=True)
    observacao = models.TextField(blank=True)

    class Meta:
        ordering = ["data_programada", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["titulo", "chave_idempotencia"],
                condition=~Q(chave_idempotencia=""),
                name="uniq_programacao_titulo_idempotencia",
            ),
        ]


class AplicacaoLiquidacaoProgramacao(TimestampedModel):
    liquidacao = models.ForeignKey(Liquidacao, related_name="aplicacoes_programacao", on_delete=models.PROTECT)
    programacao = models.ForeignKey(ProgramacaoPagamento, related_name="aplicacoes", on_delete=models.PROTECT)
    valor = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["liquidacao", "programacao"], name="uniq_aplicacao_liquidacao_programacao")]


class EvidenciaLiquidacao(TimestampedModel):
    class Tipo(models.TextChoices):
        OFX_CONTA = "OFX_CONTA", "OFX conta"
        OFX_CARTAO = "OFX_CARTAO", "OFX cartão"
        MANUAL = "MANUAL", "Manual"

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    liquidacao = models.ForeignKey(Liquidacao, related_name="evidencias", on_delete=models.PROTECT)
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    chave = models.CharField(max_length=100)
    dados = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["usuario", "tipo", "chave"], name="uniq_evidencia_liquidacao")]


class SaldoTransportadoFatura(TimestampedModel):
    class Status(models.TextChoices):
        IDENTIFICADO = "IDENTIFICADO", "Identificado"
        CONFIRMADO = "CONFIRMADO", "Confirmado"
        INCORPORADO = "INCORPORADO", "Incorporado"
        CANCELADO = "CANCELADO", "Cancelado"

    fatura_origem = models.ForeignKey(Fatura, related_name="transportes_saida", on_delete=models.PROTECT)
    titulo_origem = models.ForeignKey(Titulo, related_name="transportes_saida", on_delete=models.PROTECT)
    fatura_destino = models.ForeignKey(Fatura, related_name="transportes_entrada", on_delete=models.PROTECT)
    titulo_destino = models.ForeignKey(Titulo, related_name="transportes_entrada", null=True, blank=True, on_delete=models.PROTECT)
    valor = models.DecimalField(max_digits=14, decimal_places=2)
    data_identificacao = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IDENTIFICADO)
    evidencia = models.ForeignKey(EvidenciaLiquidacao, null=True, blank=True, on_delete=models.PROTECT)
    chave_idempotencia = models.CharField(max_length=100, blank=True)
    observacao = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["fatura_origem", "status"], name="financeiro__fatura__dca5c3_idx"),
            models.Index(fields=["fatura_destino", "status"], name="financeiro__fatura__0d46db_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["titulo_origem", "chave_idempotencia"],
                condition=~Q(chave_idempotencia=""),
                name="uniq_transporte_origem_idempotencia",
            ),
        ]


class Importacao(TimestampedModel):
    class Origem(models.TextChoices):
        CONTA = "CONTA", "Conta corrente"
        CARTAO = "CARTAO", "Cartao"

    class Status(models.TextChoices):
        RECEBIDA = "RECEBIDA", "Recebida"
        EM_REVISAO = "EM_REVISAO", "Em revisao"
        CONFIRMADA = "CONFIRMADA", "Confirmada"
        DESFEITA = "DESFEITA", "Desfeita"

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    arquivo = models.FileField(upload_to="importacoes/%Y/%m/")
    nome_arquivo = models.CharField(max_length=220)
    hash_arquivo = models.CharField(max_length=64)
    origem = models.CharField(max_length=20, choices=Origem.choices)
    instituicao = models.CharField(max_length=120, blank=True)
    conta = models.ForeignKey(Conta, null=True, blank=True, on_delete=models.PROTECT)
    cartao = models.ForeignKey(Cartao, null=True, blank=True, on_delete=models.PROTECT)
    ciclo = models.CharField(max_length=7, blank=True)
    data_inicio = models.DateField(null=True, blank=True)
    data_fim = models.DateField(null=True, blank=True)
    data_posicao = models.DateField(null=True, blank=True)
    identificador_conta = models.CharField(max_length=120, blank=True)
    saldo_informado = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    total_debitos = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_creditos = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_liquido = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    adaptador = models.CharField(max_length=80, default="ofx")
    versao_adaptador = models.CharField(max_length=20, default="0.1")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RECEBIDA)

    class Meta:
        ordering = ["-criado_em"]
        indexes = [
            models.Index(fields=["usuario", "hash_arquivo"]),
            models.Index(fields=["usuario", "origem", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(origem="CONTA", conta__isnull=False, cartao__isnull=True, ciclo="")
                    | (Q(origem="CARTAO", conta__isnull=True, cartao__isnull=False) & ~Q(ciclo=""))
                ),
                name="check_importacao_origem_destino",
            ),
        ]

    def __str__(self):
        return self.nome_arquivo


class ItemImportacao(TimestampedModel):
    class Status(models.TextChoices):
        NOVO = "NOVO", "Novo"
        CONCILIADO = "CONCILIADO", "Conciliado"
        DIVERGENTE = "DIVERGENTE", "Divergente"
        PENDENTE = "PENDENTE", "Pendente"

    STATUS_PLANEJADO = "PLANEJADO"

    class Acao(models.TextChoices):
        CRIAR = "CRIAR", "Criar lançamento"
        VINCULAR = "VINCULAR", "Vincular existente"
        IGNORAR = "IGNORAR", "Ignorar"

    importacao = models.ForeignKey(Importacao, related_name="itens", on_delete=models.CASCADE)
    posicao = models.PositiveIntegerField(default=1)
    identificador_externo = models.CharField(max_length=160, blank=True)
    ocorrencia = models.PositiveIntegerField(default=1)
    chave_conciliacao = models.CharField(max_length=64, blank=True)
    tipo_normalizado = models.CharField(max_length=40, blank=True)
    documento = models.CharField(max_length=80, blank=True)
    data = models.DateField(null=True, blank=True)
    valor = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    descricao_original = models.TextField(blank=True)
    data_revisada = models.DateField(null=True, blank=True)
    valor_revisado = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    descricao_revisada = models.CharField(max_length=220, blank=True)
    natureza = models.CharField(max_length=30, choices=NaturezaEvento.choices, blank=True)
    ambito = models.CharField(max_length=20, choices=Ambito.choices, default=Ambito.NAO_DEFINIDO)
    numero_parcela = models.PositiveSmallIntegerField(null=True, blank=True)
    total_parcelas = models.PositiveSmallIntegerField(null=True, blank=True)
    acao = models.CharField(max_length=20, choices=Acao.choices, default=Acao.CRIAR)
    observacao = models.TextField(blank=True)
    revisado_em = models.DateTimeField(null=True, blank=True)
    motivo_status = models.TextField(blank=True)
    registro_original = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDENTE)
    categoria = models.ForeignKey(Categoria, null=True, blank=True, on_delete=models.PROTECT)
    pessoa = models.ForeignKey(Pessoa, null=True, blank=True, on_delete=models.PROTECT)
    evento = models.ForeignKey(EventoFinanceiro, null=True, blank=True, on_delete=models.PROTECT)
    movimento_conta = models.ForeignKey(MovimentoConta, null=True, blank=True, on_delete=models.PROTECT)
    titulo_alvo = models.ForeignKey(
        Titulo,
        null=True,
        blank=True,
        related_name="itens_importacao_vinculados",
        on_delete=models.PROTECT,
    )
    parcela = models.ForeignKey(ParcelaCartao, null=True, blank=True, on_delete=models.PROTECT)
    evidencia_liquidacao = models.ForeignKey(EvidenciaLiquidacao, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ["posicao", "id"]
        indexes = [
            models.Index(fields=["importacao", "status"]),
            models.Index(fields=["chave_conciliacao"]),
            models.Index(fields=["identificador_externo"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["importacao", "posicao"], name="uniq_item_importacao_posicao"),
        ]

    @property
    def data_final(self):
        return self.data_revisada or self.data

    @property
    def valor_final(self):
        return self.valor_revisado if self.valor_revisado is not None else self.valor

    @property
    def descricao_final(self):
        return self.descricao_revisada or self.descricao_original

    @property
    def vinculo_manual_pendente(self):
        return (
            self.importacao.status == Importacao.Status.EM_REVISAO
            and self.motivo_status in {
                "Título selecionado manualmente; a baixa será realizada na confirmação.",
                "Títulos selecionados manualmente; as baixas serão realizadas na confirmação.",
                "Movimento existente selecionado manualmente.",
                "Parcela prevista selecionada manualmente; a realização ocorrerá na confirmação.",
            }
        )


class VinculoTituloImportacao(TimestampedModel):
    """Reserva títulos para a conciliação integral de um único item OFX."""

    item = models.ForeignKey(ItemImportacao, related_name="titulos_multiplos", on_delete=models.CASCADE)
    titulo = models.ForeignKey(Titulo, related_name="vinculos_importacao_multipla", on_delete=models.PROTECT)
    valor = models.DecimalField(max_digits=14, decimal_places=2)
    liquidacao = models.ForeignKey(Liquidacao, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["item", "titulo"], name="uniq_vinculo_importacao_titulo"),
        ]
        indexes = [
            models.Index(fields=["item", "titulo"], name="financeiro__item_id_9d1265_idx"),
            models.Index(fields=["titulo", "liquidacao"], name="financeiro__titulo__fe15c5_idx"),
        ]


class RegraCategoria(TimestampedModel):
    class Combinador(models.TextChoices):
        TODAS = "TODAS", "Todas as condições (E)"
        QUALQUER = "QUALQUER", "Qualquer condição (OU)"

    class VersaoMotor(models.TextChoices):
        LEGADO = "LEGADO", "Legada — compatibilidade"
        NOVO = "NOVO", "Nova — em uso"

    NATUREZAS_SUGERIDAS = (
        (NaturezaEvento.DESPESA, "Despesa"),
        (NaturezaEvento.RECEITA, "Receita"),
        (NaturezaEvento.ESTORNO, "Estorno"),
    )

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    texto = models.CharField(max_length=160, blank=True, default="")
    nome = models.CharField(max_length=160)
    prioridade = models.PositiveIntegerField(default=100)
    combinador = models.CharField(max_length=20, choices=Combinador.choices, default=Combinador.TODAS)
    versao_motor = models.CharField(max_length=20, choices=VersaoMotor.choices, default=VersaoMotor.NOVO)
    pessoa = models.ForeignKey(Pessoa, null=True, blank=True, on_delete=models.PROTECT)
    categoria = models.ForeignKey(Categoria, null=True, blank=True, on_delete=models.PROTECT)
    ambito = models.CharField(max_length=20, choices=Ambito.choices, blank=True, default="")
    natureza_sugerida = models.CharField(max_length=30, choices=NATUREZAS_SUGERIDAS, blank=True, default="")
    ativa = models.BooleanField(default=True)

    class Meta:
        ordering = ["prioridade", "id"]
        indexes = [
            models.Index(
                fields=["usuario", "versao_motor", "ativa", "prioridade"],
                name="fin_regra_usr_ver_at_pr_idx",
            ),
        ]

    def __str__(self):
        return self.nome

    def clean(self):
        super().clean()
        for campo in ("categoria", "pessoa"):
            relacionado = getattr(self, campo, None)
            if relacionado and self.usuario_id and relacionado.usuario_id != self.usuario_id:
                raise ValidationError({campo: "O cadastro selecionado deve pertencer ao mesmo usuário da regra."})


class RegraCategoriaCondicao(TimestampedModel):
    class Campo(models.TextChoices):
        DESCRICAO = "DESCRICAO", "Descrição"
        OBSERVACAO = "OBSERVACAO", "Observação"
        VALOR = "VALOR", "Valor absoluto"
        NATUREZA = "NATUREZA", "Tipo atual"
        ORIGEM = "ORIGEM", "Origem"
        CONTA = "CONTA", "Conta específica"
        CARTAO = "CARTAO", "Cartão específico"

    class Operador(models.TextChoices):
        CONTEM = "CONTEM", "contém"
        NAO_CONTEM = "NAO_CONTEM", "não contém"
        IGUAL = "IGUAL", "é igual a"
        DIFERENTE = "DIFERENTE", "é diferente de"
        COMECA_COM = "COMECA_COM", "começa com"
        TERMINA_COM = "TERMINA_COM", "termina com"
        MAIOR = "MAIOR", "é maior que"
        MAIOR_IGUAL = "MAIOR_IGUAL", "é maior ou igual a"
        MENOR = "MENOR", "é menor que"
        MENOR_IGUAL = "MENOR_IGUAL", "é menor ou igual a"

    OPERADORES_TEXTO = {
        Operador.CONTEM,
        Operador.NAO_CONTEM,
        Operador.IGUAL,
        Operador.DIFERENTE,
        Operador.COMECA_COM,
        Operador.TERMINA_COM,
    }
    OPERADORES_VALOR = {
        Operador.IGUAL,
        Operador.DIFERENTE,
        Operador.MAIOR,
        Operador.MAIOR_IGUAL,
        Operador.MENOR,
        Operador.MENOR_IGUAL,
    }
    OPERADORES_ESCOLHA = {Operador.IGUAL, Operador.DIFERENTE}

    regra = models.ForeignKey(RegraCategoria, related_name="condicoes", on_delete=models.CASCADE)
    ordem = models.PositiveSmallIntegerField(default=1)
    campo = models.CharField(max_length=20, choices=Campo.choices)
    operador = models.CharField(max_length=20, choices=Operador.choices)
    valor = models.CharField(max_length=255)

    class Meta:
        ordering = ["ordem", "id"]
        constraints = [
            models.UniqueConstraint(fields=["regra", "ordem"], name="uniq_regra_categoria_condicao_ordem"),
        ]
        indexes = [
            models.Index(fields=["regra", "campo"], name="fin_regra_cond_reg_campo_idx"),
        ]

    def __str__(self):
        return f"{self.get_campo_display()} {self.get_operador_display()} {self.valor}"

    @classmethod
    def operadores_permitidos(cls, campo):
        if campo in {cls.Campo.DESCRICAO, cls.Campo.OBSERVACAO}:
            return cls.OPERADORES_TEXTO
        if campo == cls.Campo.VALOR:
            return cls.OPERADORES_VALOR
        return cls.OPERADORES_ESCOLHA

    def clean(self):
        super().clean()
        if self.operador and self.campo and self.operador not in self.operadores_permitidos(self.campo):
            raise ValidationError({"operador": "O operador não é compatível com o campo selecionado."})
        valor = str(self.valor or "").strip()
        if not valor:
            raise ValidationError({"valor": "Informe o valor da condição."})
        if self.campo == self.Campo.VALOR:
            try:
                Decimal(valor)
            except InvalidOperation as exc:
                raise ValidationError({"valor": "Informe um valor monetário válido."}) from exc
        if self.campo == self.Campo.NATUREZA and valor not in dict(RegraCategoria.NATUREZAS_SUGERIDAS):
            raise ValidationError({"valor": "Selecione um tipo de lançamento válido."})
        if self.campo == self.Campo.ORIGEM and valor not in {"CONTA", "CARTAO"}:
            raise ValidationError({"valor": "Selecione uma origem válida."})
        if self.campo in {self.Campo.CONTA, self.Campo.CARTAO} and self.regra_id:
            modelo = Conta if self.campo == self.Campo.CONTA else Cartao
            if not valor.isdigit() or not modelo.objects.filter(pk=int(valor), usuario_id=self.regra.usuario_id).exists():
                raise ValidationError({"valor": "O cadastro selecionado não pertence ao usuário da regra."})


class Orcamento(TimestampedModel):
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    categoria = models.ForeignKey(Categoria, on_delete=models.PROTECT)
    mes = models.CharField(max_length=7)
    ambito = models.CharField(max_length=20, choices=Ambito.choices, default=Ambito.NAO_DEFINIDO)
    limite = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["usuario", "categoria", "mes", "ambito"], name="uniq_orcamento_categoria_mes_ambito"),
        ]
