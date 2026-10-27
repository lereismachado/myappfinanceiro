import hashlib
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from uuid import uuid4

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .models import (
    Ambito,
    Cartao,
    Categoria,
    Conta,
    EventoFinanceiro,
    Importacao,
    ItemImportacao,
    NaturezaEvento,
    Orcamento,
    ParcelaCartao,
    Pessoa,
    RegraCategoria,
    RegraCategoriaCondicao,
    MovimentoConta,
    Titulo,
    Situacao,
)
from .services.cartoes_faturas import aviso_ciclo_cartao


class BRLDecimalField(forms.DecimalField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", 14)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("widget", forms.TextInput(attrs={"inputmode": "decimal", "placeholder": "Ex.: 250,00"}))
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if value in (None, ""):
            return ""
        try:
            return f"{Decimal(value):.2f}".replace(".", ",")
        except (InvalidOperation, TypeError, ValueError):
            return value

    def to_python(self, value):
        if isinstance(value, str):
            value = value.strip().replace(".", "").replace(",", ".")
        return super().to_python(value)


class DataBRField(forms.DateField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("input_formats", ["%d/%m/%Y", "%Y-%m-%d"])
        kwargs.setdefault(
            "widget",
            forms.TextInput(
                attrs={
                    "placeholder": "DD/MM/AAAA",
                    "inputmode": "numeric",
                    "data-date-input": "true",
                }
            ),
        )
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if isinstance(value, date):
            return value.strftime("%d/%m/%Y")
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                return date.fromisoformat(value).strftime("%d/%m/%Y")
            except ValueError:
                pass
        return value


class CicloField(forms.CharField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_length", 7)
        kwargs.setdefault(
            "widget",
            forms.TextInput(
                attrs={
                    "placeholder": "MM/AAAA",
                    "inputmode": "numeric",
                    "data-month-input": "true",
                }
            ),
        )
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if isinstance(value, str) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
            ano, mes = value.split("-")
            return f"{mes}/{ano}"
        return value

    def to_python(self, value):
        value = super().to_python(value)
        if value:
            match = re.fullmatch(r"(0[1-9]|1[0-2])/(\d{4})", value.strip())
            if match:
                mes, ano = match.groups()
                return f"{ano}-{mes}"
        return value


class CategoriaModelChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.rotulo_com_pai


class BRLModelForm(forms.ModelForm):
    money_fields = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.money_fields:
            if name in self.fields:
                old = self.fields[name]
                self.fields[name] = BRLDecimalField(
                    label=old.label,
                    required=old.required,
                    help_text=old.help_text,
                    initial=old.initial,
                )


class ContaForm(forms.ModelForm):
    money_fields = ("saldo_inicial", "limite_credito")

    saldo_inicial = BRLDecimalField(label="Saldo inicial (R$)")
    limite_credito = BRLDecimalField(
        label="Limite de cheque especial (R$)",
        min_value=Decimal("0.00"),
    )

    class Meta:
        model = Conta
        fields = [
            "nome",
            "banco",
            "agencia",
            "numero",
            "titular",
            "tipo",
            "ambito",
            "saldo_inicial",
            "data_saldo_inicial",
            "limite_credito",
            "nao_compoe_dashboard",
            "ativa",
        ]
        labels = {"nao_compoe_dashboard": "Não compõe a Visão Geral"}
        help_texts = {
            "nao_compoe_dashboard": (
                "Quando marcada, apenas os indicadores da Visão Geral deixam de considerar o saldo e as "
                "movimentações desta conta. Lançamentos, importações, cálculo de saldos e demais "
                "funcionalidades permanecem inalterados."
            ),
        }
        widgets = {"data_saldo_inicial": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["data_saldo_inicial"].input_formats = ["%Y-%m-%d"]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("saldo_inicial") and not cleaned.get("data_saldo_inicial"):
            self.add_error("data_saldo_inicial", "Informe a data de abertura do saldo inicial.")
        return cleaned


class CartaoForm(forms.ModelForm):
    limite = BRLDecimalField(label="Limite do cartão (R$)", required=False)

    class Meta:
        model = Cartao
        fields = [
            "nome",
            "emissor",
            "identificador_externo",
            "final",
            "dia_fechamento",
            "dia_vencimento",
            "conta_pagamento_padrao",
            "limite",
            "ativo",
        ]

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        if usuario:
            self.fields["conta_pagamento_padrao"].queryset = Conta.objects.filter(usuario=usuario)


class CategoriaForm(forms.ModelForm):
    class Meta:
        model = Categoria
        fields = [
            "nome",
            "tipo",
            "categoria_pai",
            "natureza",
            "essencialidade",
            "computa_resultado",
            "ativa",
        ]
        labels = {
            "categoria_pai": "Categoria superior",
            "computa_resultado": "Compõe no resultado",
            "ativa": "Situação ativa",
        }
        help_texts = {
            "natureza": "Identifica se a categoria pertence ao contexto pessoal, empresarial ou ambos.",
            "essencialidade": "Classifica o comportamento do gasto para análises de orçamento e potencial de economia.",
            "computa_resultado": "Define se os lançamentos desta categoria participam dos totais de receitas, despesas e resultado.",
        }
        widgets = {
            "computa_resultado": forms.CheckboxInput(attrs={"data-computa-resultado": ""}),
        }

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario
        if usuario:
            self.instance.usuario = usuario
            queryset = Categoria.objects.filter(usuario=usuario)
            if self.instance.pk:
                descendentes = {self.instance.pk}
                fronteira = {self.instance.pk}
                while fronteira:
                    encontrados = set(
                        Categoria.objects.filter(usuario=usuario, categoria_pai_id__in=fronteira)
                        .values_list("pk", flat=True)
                    ) - descendentes
                    descendentes.update(encontrados)
                    fronteira = encontrados
                queryset = queryset.exclude(pk__in=descendentes)
            self.fields["categoria_pai"].queryset = queryset
        self.fields["tipo"].widget.attrs["data-categoria-tipo"] = ""
        self.fields["essencialidade"].widget.attrs["data-categoria-essencialidade"] = ""

    def clean(self):
        cleaned = super().clean()
        categoria_pai = cleaned.get("categoria_pai")
        if categoria_pai and self.usuario and categoria_pai.usuario_id != self.usuario.pk:
            self.add_error("categoria_pai", "A categoria superior deve pertencer ao mesmo usuário.")
        return cleaned


class OrcamentoForm(forms.ModelForm):
    categoria = CategoriaModelChoiceField(queryset=Categoria.objects.none(), label="Categoria")
    mes = CicloField(label="Mês")
    limite = BRLDecimalField(label="Limite (R$)")

    class Meta:
        model = Orcamento
        fields = ["categoria", "mes", "ambito", "limite"]
        labels = {"ambito": "Âmbito"}

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario
        if usuario:
            self.instance.usuario = usuario
            categorias = Categoria.objects.filter(
                usuario=usuario,
                tipo=Categoria.Tipo.DESPESA,
                ativa=True,
                categoria_pai__isnull=True,
            )
            if self.instance.pk and self.instance.categoria_id:
                categorias = Categoria.objects.filter(
                    Q(
                        usuario=usuario,
                        tipo=Categoria.Tipo.DESPESA,
                        ativa=True,
                        categoria_pai__isnull=True,
                    )
                    | Q(pk=self.instance.categoria_id, usuario=usuario, categoria_pai__isnull=True)
                )
            self.fields["categoria"].queryset = categorias.select_related("categoria_pai").distinct()

    def clean_mes(self):
        mes = self.cleaned_data["mes"]
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", mes or ""):
            raise forms.ValidationError("Informe o mês no formato MM/AAAA.")
        return mes

    def clean(self):
        cleaned = super().clean()
        categoria = cleaned.get("categoria")
        mes = cleaned.get("mes")
        ambito = cleaned.get("ambito")
        if categoria and self.usuario and categoria.usuario_id != self.usuario.pk:
            self.add_error("categoria", "A categoria deve pertencer ao usuário atual.")
        if categoria and categoria.tipo != Categoria.Tipo.DESPESA:
            self.add_error("categoria", "Selecione uma categoria de despesa.")
        if categoria and categoria.categoria_pai_id:
            self.add_error("categoria", "Selecione uma categoria superior, sem categoria superior informada.")
        if self.usuario and categoria and mes and ambito:
            duplicado = Orcamento.objects.filter(
                usuario=self.usuario,
                categoria=categoria,
                mes=mes,
                ambito=ambito,
            ).exclude(pk=self.instance.pk)
            if duplicado.exists():
                raise forms.ValidationError("Já existe um orçamento para esta categoria, mês e âmbito.")
        return cleaned


class ClonagemPeriodoOrcamentoForm(forms.Form):
    mes_origem = CicloField(label="Período de origem")
    mes_destino = CicloField(label="Período de destino")

    def _validar_mes(self, nome_campo):
        mes = self.cleaned_data[nome_campo]
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", mes or ""):
            raise forms.ValidationError("Informe o mês no formato MM/AAAA.")
        return mes

    def clean_mes_origem(self):
        return self._validar_mes("mes_origem")

    def clean_mes_destino(self):
        return self._validar_mes("mes_destino")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("mes_origem") and cleaned.get("mes_origem") == cleaned.get("mes_destino"):
            self.add_error("mes_destino", "O período de destino deve ser diferente do período de origem.")
        return cleaned


class ImportacaoDespesasOrcamentoForm(forms.Form):
    mes_despesas = CicloField(label="Período das despesas")
    mes_destino = CicloField(label="Período de destino")

    def __init__(self, *args, mes_atual=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.mes_atual = mes_atual

    def _validar_mes(self, nome_campo):
        mes = self.cleaned_data[nome_campo]
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", mes or ""):
            raise forms.ValidationError("Informe o mês no formato MM/AAAA.")
        return mes

    def clean_mes_despesas(self):
        mes = self._validar_mes("mes_despesas")
        if self.mes_atual and mes > self.mes_atual:
            raise forms.ValidationError("O período das despesas não pode ser posterior ao mês atual.")
        return mes

    def clean_mes_destino(self):
        return self._validar_mes("mes_destino")

    def clean(self):
        cleaned = super().clean()
        mes_despesas = cleaned.get("mes_despesas")
        mes_destino = cleaned.get("mes_destino")
        if mes_despesas and mes_destino and mes_destino <= mes_despesas:
            self.add_error(
                "mes_destino",
                "O período de destino deve ser posterior ao período das despesas.",
            )
        return cleaned


class PessoaForm(forms.ModelForm):
    class Meta:
        model = Pessoa
        fields = ["nome", "documento", "observacao", "ativa"]


class RegraCategoriaForm(forms.ModelForm):
    class Meta:
        model = RegraCategoria
        field_classes = {"categoria": CategoriaModelChoiceField}
        fields = [
            "nome",
            "prioridade",
            "combinador",
            "categoria",
            "ambito",
            "pessoa",
            "natureza_sugerida",
            "ativa",
        ]
        labels = {
            "prioridade": "Prioridade",
            "combinador": "Combinação das condições",
            "categoria": "Sugerir categoria",
            "ambito": "Sugerir âmbito",
            "pessoa": "Sugerir pessoa",
            "natureza_sugerida": "Sugerir tipo",
            "ativa": "Regra ativa",
        }
        help_texts = {
            "prioridade": "Números menores são avaliados primeiro.",
        }

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario
        if usuario:
            self.instance.usuario = usuario
            self.fields["categoria"].queryset = Categoria.objects.filter(usuario=usuario).select_related("categoria_pai")
            self.fields["pessoa"].queryset = Pessoa.objects.filter(usuario=usuario)
        ambitos = [("", "Não sugerir âmbito"), (Ambito.PESSOAL, "Pessoal"), (Ambito.PROFISSIONAL, "Profissional")]
        if self.instance.pk and self.instance.ambito == Ambito.NAO_DEFINIDO:
            ambitos.append((Ambito.NAO_DEFINIDO, "Não definido (legado)"))
        self.fields["ambito"].choices = ambitos

    def clean(self):
        cleaned = super().clean()
        ambito = cleaned.get("ambito")
        if not any(
            (
                cleaned.get("categoria"),
                cleaned.get("pessoa"),
                cleaned.get("natureza_sugerida"),
                ambito in {Ambito.PESSOAL, Ambito.PROFISSIONAL},
            )
        ):
            raise forms.ValidationError("Informe ao menos um resultado sugerido para a regra.")
        return cleaned


class RegraCategoriaCondicaoForm(forms.ModelForm):
    valor_texto = forms.CharField(
        label="Valor",
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={"placeholder": "Informe o valor da condição"}),
    )
    valor_natureza = forms.ChoiceField(
        label="Tipo",
        required=False,
        choices=(("", "Selecione"), *RegraCategoria.NATUREZAS_SUGERIDAS),
    )
    valor_origem = forms.ChoiceField(
        label="Origem",
        required=False,
        choices=(("", "Selecione"), ("CONTA", "Conta financeira"), ("CARTAO", "Cartão de crédito")),
    )
    valor_conta = forms.ModelChoiceField(label="Conta", required=False, queryset=Conta.objects.none())
    valor_cartao = forms.ModelChoiceField(label="Cartão", required=False, queryset=Cartao.objects.none())

    class Meta:
        model = RegraCategoriaCondicao
        fields = ["ordem", "campo", "operador", "valor"]
        widgets = {
            "ordem": forms.HiddenInput(),
            "valor": forms.HiddenInput(),
        }

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario
        self.fields["valor"].required = False
        if usuario:
            self.fields["valor_conta"].queryset = Conta.objects.filter(usuario=usuario).order_by("nome", "id")
            self.fields["valor_cartao"].queryset = Cartao.objects.filter(usuario=usuario).order_by("nome", "id")
        if self.instance.pk and not self.is_bound:
            valor = self.instance.valor
            if self.instance.campo in {
                RegraCategoriaCondicao.Campo.DESCRICAO,
                RegraCategoriaCondicao.Campo.OBSERVACAO,
                RegraCategoriaCondicao.Campo.VALOR,
            }:
                self.fields["valor_texto"].initial = valor.replace(".", ",") if self.instance.campo == RegraCategoriaCondicao.Campo.VALOR else valor
            elif self.instance.campo == RegraCategoriaCondicao.Campo.NATUREZA:
                self.fields["valor_natureza"].initial = valor
            elif self.instance.campo == RegraCategoriaCondicao.Campo.ORIGEM:
                self.fields["valor_origem"].initial = valor
            elif self.instance.campo == RegraCategoriaCondicao.Campo.CONTA and valor.isdigit():
                self.fields["valor_conta"].initial = int(valor)
            elif self.instance.campo == RegraCategoriaCondicao.Campo.CARTAO and valor.isdigit():
                self.fields["valor_cartao"].initial = int(valor)

    def _get_validation_exclusions(self):
        exclusions = super()._get_validation_exclusions()
        # A ordem é normalizada transacionalmente ao salvar o formset. Excluí-la
        # aqui evita falsos conflitos da constraint durante uma reordenação.
        exclusions.add("ordem")
        return exclusions

    def clean(self):
        cleaned = super().clean()
        campo = cleaned.get("campo")
        operador = cleaned.get("operador")
        if not campo or not operador:
            return cleaned
        if operador not in RegraCategoriaCondicao.operadores_permitidos(campo):
            self.add_error("operador", "O operador não é compatível com o campo selecionado.")

        valor = ""
        if campo in {RegraCategoriaCondicao.Campo.DESCRICAO, RegraCategoriaCondicao.Campo.OBSERVACAO}:
            valor = (cleaned.get("valor_texto") or "").strip()
        elif campo == RegraCategoriaCondicao.Campo.VALOR:
            bruto = (cleaned.get("valor_texto") or "").strip()
            normalizado = bruto.replace(".", "").replace(",", ".") if "," in bruto else bruto
            try:
                valor = format(abs(Decimal(normalizado)), "f")
            except (InvalidOperation, ValueError):
                self.add_error("valor_texto", "Informe um valor monetário válido.")
        elif campo == RegraCategoriaCondicao.Campo.NATUREZA:
            valor = cleaned.get("valor_natureza") or ""
        elif campo == RegraCategoriaCondicao.Campo.ORIGEM:
            valor = cleaned.get("valor_origem") or ""
        elif campo == RegraCategoriaCondicao.Campo.CONTA:
            conta = cleaned.get("valor_conta")
            valor = str(conta.pk) if conta else ""
        elif campo == RegraCategoriaCondicao.Campo.CARTAO:
            cartao = cleaned.get("valor_cartao")
            valor = str(cartao.pk) if cartao else ""
        if not valor:
            self.add_error("valor", "Informe o valor da condição.")
        cleaned["valor"] = valor
        return cleaned


class BaseRegraCategoriaCondicaoFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        condicoes = [
            form for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE")
        ]
        if not condicoes:
            raise forms.ValidationError("Adicione ao menos uma condição à regra.")
        if any(self.errors):
            return


RegraCategoriaCondicaoFormSet = inlineformset_factory(
    RegraCategoria,
    RegraCategoriaCondicao,
    form=RegraCategoriaCondicaoForm,
    formset=BaseRegraCategoriaCondicaoFormSet,
    extra=0,
    min_num=1,
    max_num=20,
    validate_min=True,
    validate_max=True,
    can_delete=True,
)


class EventoFinanceiroForm(forms.ModelForm):
    valor = BRLDecimalField(label="Valor (R$)")

    class Meta:
        model = EventoFinanceiro
        fields = ["natureza", "descricao", "data", "vencimento", "valor", "pessoa", "ambito", "situacao", "observacao"]
        widgets = {
            "data": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "vencimento": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        }

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        if usuario:
            self.fields["pessoa"].queryset = Pessoa.objects.filter(usuario=usuario)
        self.fields["data"].input_formats = ["%Y-%m-%d"]
        self.fields["vencimento"].input_formats = ["%Y-%m-%d"]

    def clean(self):
        cleaned = super().clean()
        natureza = cleaned.get("natureza")
        valor = cleaned.get("valor")
        if natureza in {NaturezaEvento.DESPESA, NaturezaEvento.PAGAMENTO_FATURA} and valor and valor < 0:
            self.add_error("valor", "Informe despesas e pagamentos com valor positivo; o sinal fica no movimento de conta.")
        return cleaned


class SugestaoRegraLancamentoForm(forms.Form):
    descricao = forms.CharField(required=False, max_length=180)
    observacao = forms.CharField(required=False, max_length=2000)
    natureza = forms.ChoiceField(required=False, choices=(("", ""), *NaturezaEvento.choices))
    valor = forms.CharField(required=False, max_length=64)
    origem = forms.ChoiceField(required=False, choices=(("", ""), ("CONTA", "Conta"), ("CARTAO", "Cartão")))
    conta = forms.ModelChoiceField(required=False, queryset=Conta.objects.none())
    cartao = forms.ModelChoiceField(required=False, queryset=Cartao.objects.none())

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["conta"].queryset = Conta.objects.filter(usuario=usuario, ativa=True) if usuario else Conta.objects.none()
        self.fields["cartao"].queryset = Cartao.objects.filter(usuario=usuario, ativo=True) if usuario else Cartao.objects.none()

    def clean_valor(self):
        bruto = (self.cleaned_data.get("valor") or "").strip()
        if not bruto:
            return None
        normalizado = bruto.replace(".", "").replace(",", ".") if "," in bruto else bruto
        try:
            return abs(Decimal(normalizado))
        except (InvalidOperation, TypeError, ValueError):
            return None


class LancamentoForm(forms.Form):
    class ModoLancamento(models.TextChoices):
        UNICO = "UNICO", "Lançamento único"
        PARCELADO = "PARCELADO", "Compra parcelada"
        RECORRENTE = "RECORRENTE", "Lançamento recorrente"

    descricao = forms.CharField(label="Descrição", max_length=220)
    natureza = forms.ChoiceField(
        label="Tipo",
        choices=(
            (NaturezaEvento.DESPESA, "Despesa"),
            (NaturezaEvento.RECEITA, "Receita"),
            (NaturezaEvento.ESTORNO, "Estorno / crédito"),
            (NaturezaEvento.PAGAMENTO_FATURA, "Pagamento de fatura"),
        ),
        initial=NaturezaEvento.DESPESA,
    )
    data = DataBRField(label="Data do movimento / compra")
    valor = BRLDecimalField(label="Valor (R$)", min_value=Decimal("0.01"))
    origem = forms.ChoiceField(
        label="Origem",
        choices=(("CONTA", "Conta financeira"), ("CARTAO", "Cartão de crédito")),
        initial="CONTA",
    )
    conta = forms.ModelChoiceField(queryset=Conta.objects.none(), required=False, label="Conta financeira")
    cartao = forms.ModelChoiceField(queryset=Cartao.objects.none(), required=False, label="Cartão de crédito")
    categoria = CategoriaModelChoiceField(queryset=Categoria.objects.none(), required=False, label="Categoria")
    pessoa = forms.ModelChoiceField(
        queryset=Pessoa.objects.none(),
        required=False,
        widget=forms.HiddenInput(),
    )
    pessoa_nome_livre = forms.CharField(
        label="Pessoa / pagador / beneficiário",
        max_length=160,
        required=False,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "role": "combobox",
                "aria-autocomplete": "list",
                "aria-expanded": "false",
                "aria-controls": "id_pessoa_nome_livre_suggestions",
            }
        ),
    )
    ambito = forms.ChoiceField(
        label="Âmbito",
        choices=((Ambito.NAO_DEFINIDO, "Não definido"), (Ambito.PESSOAL, "Pessoal"), (Ambito.PROFISSIONAL, "Profissional")),
        initial=Ambito.NAO_DEFINIDO,
    )
    situacao = forms.ChoiceField(
        label="Situação",
        choices=((Situacao.REALIZADO, "Realizado / cobrado em fatura"), (Situacao.PREVISTO, "Previsto")),
        initial=Situacao.PREVISTO,
    )
    vencimento = DataBRField(label="Vencimento (para previsão)", required=False)
    ciclo = CicloField(
        label="Fatura / primeiro ciclo",
        required=False,
    )
    modo_lancamento = forms.ChoiceField(
        label="Forma de lançamento",
        choices=ModoLancamento.choices,
        initial=ModoLancamento.UNICO,
        required=False,
    )
    quantidade_parcelas = forms.IntegerField(label="Quantidade de parcelas", min_value=1, max_value=120, initial=1, required=False)
    quantidade_repeticoes = forms.IntegerField(label="Repetir por", min_value=1, max_value=120, initial=1, required=False)
    intervalo_meses = forms.IntegerField(label="Intervalo (meses)", min_value=1, max_value=120, initial=1, required=False)
    observacao = forms.CharField(
        label="Observação",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Observação do lançamento"}),
    )

    def __init__(
        self,
        *args,
        usuario=None,
        periodo=None,
        conta_preferida=None,
        cartao_preferido=None,
        evento=None,
        bloquear_financeiro=False,
        **kwargs,
    ):
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)
        self.usuario = usuario
        self.evento = evento
        self.bloquear_financeiro = bloquear_financeiro
        self.ciclo_aviso = ""
        valores_iniciais = dict(self.initial)
        if usuario:
            conta_atual = valores_iniciais.get("conta")
            cartao_atual = valores_iniciais.get("cartao")
            categoria_atual = valores_iniciais.get("categoria")
            pessoa_atual = valores_iniciais.get("pessoa")
            conta_id = getattr(conta_atual, "pk", conta_atual)
            cartao_id = getattr(cartao_atual, "pk", cartao_atual)
            categoria_id = getattr(categoria_atual, "pk", categoria_atual)
            pessoa_id = getattr(pessoa_atual, "pk", pessoa_atual)
            self.fields["conta"].queryset = Conta.objects.filter(usuario=usuario).filter(Q(ativa=True) | Q(pk=conta_id))
            self.fields["cartao"].queryset = Cartao.objects.filter(usuario=usuario).filter(Q(ativo=True) | Q(pk=cartao_id))
            self.fields["categoria"].queryset = (
                Categoria.objects.filter(usuario=usuario)
                .filter(Q(ativa=True) | Q(pk=categoria_id))
                .select_related("categoria_pai")
            )
            pessoas = Pessoa.objects.filter(usuario=usuario).filter(Q(ativa=True) | Q(pk=pessoa_id)).order_by("nome", "id")
            self.fields["pessoa"].queryset = pessoas
            self.pessoas_sugestoes = pessoas
        else:
            self.pessoas_sugestoes = Pessoa.objects.none()
        self.fields["conta"].empty_label = "Selecione a conta"
        self.fields["cartao"].empty_label = "Selecione o cartão"
        self.fields["categoria"].empty_label = "A classificar"
        if not self.is_bound:
            self.initial.update(
                {
                    "origem": "CONTA",
                    "ciclo": periodo,
                    "conta": conta_preferida,
                    "cartao": cartao_preferido,
                }
            )
            self.initial.update({chave: valor for chave, valor in valores_iniciais.items() if valor is not None})
            if evento is None:
                self.initial["data"] = timezone.localdate()
                self.initial["situacao"] = Situacao.PREVISTO
            pessoa_inicial = self.initial.get("pessoa")
            if pessoa_inicial:
                self.initial["pessoa_nome_livre"] = pessoa_inicial.nome

        if evento:
            naturezas = list(self.fields["natureza"].choices)
            if evento.natureza not in {valor for valor, _ in naturezas}:
                naturezas.append((evento.natureza, evento.get_natureza_display()))
            self.fields["natureza"].choices = naturezas
            situacoes = list(self.fields["situacao"].choices)
            if evento.situacao not in {valor for valor, _ in situacoes}:
                situacoes.append((evento.situacao, evento.get_situacao_display()))
                bloquear_financeiro = True
                self.bloquear_financeiro = True
            self.fields["origem"].disabled = True
            self.fields["quantidade_parcelas"].disabled = True
            self.fields["modo_lancamento"].disabled = True
            self.fields["quantidade_repeticoes"].disabled = True
            self.fields["intervalo_meses"].disabled = True
            if evento.serie_recorrente_id:
                self.fields["aplicar_recorrencia"] = forms.ChoiceField(
                    label="Aplicar alterações",
                    choices=(
                        ("SOMENTE", "Alterar somente este lançamento"),
                        ("FUTUROS", "Replicar para os lançamentos futuros previstos"),
                    ),
                    initial="SOMENTE",
                )

        limite_descricao = 220 if evento and len(evento.descricao) > 180 else 180
        self.fields["descricao"].widget.attrs["maxlength"] = limite_descricao
        if bloquear_financeiro:
            for nome in ("natureza", "data", "valor", "origem", "conta", "cartao", "situacao", "vencimento", "ciclo", "modo_lancamento", "quantidade_parcelas", "quantidade_repeticoes", "intervalo_meses"):
                self.fields[nome].disabled = True

    def clean_descricao(self):
        descricao = self.cleaned_data["descricao"].strip()
        if len(descricao) > 180 and (not self.evento or descricao != self.evento.descricao):
            raise forms.ValidationError("A descrição deve ter no máximo 180 caracteres.")
        return descricao

    def clean_pessoa_nome_livre(self):
        return self.cleaned_data["pessoa_nome_livre"].strip()

    def clean(self):
        cleaned = super().clean()
        origem = cleaned.get("origem")
        natureza = cleaned.get("natureza")
        situacao = cleaned.get("situacao")
        ciclo = (cleaned.get("ciclo") or "").strip()
        if origem == "CONTA" and not cleaned.get("conta"):
            self.add_error("conta", "Selecione a conta financeira.")
        if origem == "CARTAO":
            if natureza not in {NaturezaEvento.DESPESA, NaturezaEvento.ESTORNO}:
                self.add_error("natureza", "No cartão, use despesa ou estorno/crédito.")
            if not cleaned.get("cartao"):
                self.add_error("cartao", "Selecione o cartão de crédito.")
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", ciclo):
                self.add_error("ciclo", "Informe um ciclo válido.")
            elif cleaned.get("cartao") and cleaned.get("data"):
                self.ciclo_aviso = aviso_ciclo_cartao(
                    cleaned["cartao"],
                    cleaned["data"],
                    ciclo,
                )
            cleaned["vencimento"] = None
        if origem == "CONTA" and situacao == Situacao.PREVISTO and not cleaned.get("vencimento"):
            self.add_error("vencimento", "Informe o vencimento da previsão.")
        if natureza == NaturezaEvento.PAGAMENTO_FATURA:
            if origem != "CONTA":
                self.add_error("origem", "O pagamento da fatura deve sair de uma conta financeira.")
            if situacao != Situacao.REALIZADO:
                self.add_error("situacao", "O pagamento da fatura deve ser realizado.")
            if not cleaned.get("cartao"):
                self.add_error("cartao", "Selecione o cartão da fatura.")
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", ciclo):
                self.add_error("ciclo", "Informe o ciclo da fatura.")
        quantidade = cleaned.get("quantidade_parcelas") or 1
        modo = cleaned.get("modo_lancamento") or (
            self.ModoLancamento.PARCELADO if quantidade > 1 else self.ModoLancamento.UNICO
        )
        repeticoes = cleaned.get("quantidade_repeticoes") or 1
        intervalo = cleaned.get("intervalo_meses") or 1
        valor = cleaned.get("valor")
        if modo == self.ModoLancamento.PARCELADO and origem != "CARTAO":
            self.add_error("modo_lancamento", "O parcelamento está disponível somente para cartão de crédito.")
        if modo == self.ModoLancamento.PARCELADO and natureza != NaturezaEvento.DESPESA:
            self.add_error("quantidade_parcelas", "Parcelamento está disponível somente para despesas.")
        if modo == self.ModoLancamento.PARCELADO and valor and quantidade > int(valor * 100):
            self.add_error("quantidade_parcelas", "A quantidade de parcelas não pode superar o valor em centavos.")
        if modo != self.ModoLancamento.PARCELADO:
            quantidade = 1
        if modo != self.ModoLancamento.RECORRENTE:
            repeticoes = 1
            intervalo = 1
        elif origem == "CONTA" and repeticoes > 1 and not cleaned.get("vencimento"):
            self.add_error("vencimento", "Informe o vencimento da recorrência para gerar as previsões futuras.")
        pessoa = cleaned.get("pessoa")
        pessoa_nome_livre = cleaned.get("pessoa_nome_livre", "")
        if pessoa and pessoa_nome_livre == pessoa.nome:
            cleaned["pessoa_nome_livre"] = ""
        elif pessoa:
            cleaned["pessoa"] = None
        cleaned["ciclo"] = ciclo
        cleaned["modo_lancamento"] = modo
        cleaned["quantidade_parcelas"] = quantidade
        cleaned["quantidade_repeticoes"] = repeticoes
        cleaned["intervalo_meses"] = intervalo
        return cleaned


class CompraCartaoForm(forms.Form):
    class ModoLancamento(models.TextChoices):
        UNICO = "UNICO", "Compra única"
        PARCELADO = "PARCELADO", "Compra parcelada"
        RECORRENTE = "RECORRENTE", "Lançamento recorrente"

    descricao = forms.CharField(label="Descrição", max_length=220)
    natureza = forms.ChoiceField(
        label="Tipo",
        choices=(
            (NaturezaEvento.DESPESA, "Despesa"),
            (NaturezaEvento.ESTORNO, "Estorno / crédito"),
        ),
        initial=NaturezaEvento.DESPESA,
    )
    data_original = DataBRField(label="Data do movimento / compra")
    valor = BRLDecimalField(label="Valor (R$)", min_value=Decimal("0.01"))
    origem = forms.ChoiceField(
        label="Origem",
        choices=(("CARTAO", "Cartão de crédito"),),
        initial="CARTAO",
        disabled=True,
    )
    cartao = forms.ModelChoiceField(queryset=Cartao.objects.none(), label="Cartão")
    categoria = CategoriaModelChoiceField(queryset=Categoria.objects.none(), required=False, label="Categoria")
    pessoa = forms.ModelChoiceField(
        queryset=Pessoa.objects.none(),
        required=False,
        widget=forms.HiddenInput(),
    )
    pessoa_nome_livre = forms.CharField(
        label="Pessoa / pagador / beneficiário",
        max_length=160,
        required=False,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "role": "combobox",
                "aria-autocomplete": "list",
                "aria-expanded": "false",
                "aria-controls": "id_pessoa_nome_livre_suggestions",
            }
        ),
    )
    ambito = forms.ChoiceField(
        label="Âmbito",
        choices=((Ambito.NAO_DEFINIDO, "Não definido"), (Ambito.PESSOAL, "Pessoal"), (Ambito.PROFISSIONAL, "Profissional")),
        initial=Ambito.NAO_DEFINIDO,
    )
    situacao = forms.ChoiceField(
        label="Situação",
        choices=(
            (Situacao.REALIZADO, "Realizado / cobrado em fatura"),
            (Situacao.PREVISTO, "Previsto"),
            (Situacao.CANCELADO, "Cancelado"),
        ),
        initial=Situacao.REALIZADO,
    )
    ciclo = CicloField(
        label="Fatura / primeiro ciclo",
    )
    quantidade_parcelas = forms.IntegerField(
        label="Quantidade de parcelas",
        min_value=1,
        max_value=120,
        initial=1,
    )
    modo_lancamento = forms.ChoiceField(
        label="Forma de lançamento",
        choices=ModoLancamento.choices,
        initial=ModoLancamento.UNICO,
        required=False,
    )
    quantidade_repeticoes = forms.IntegerField(label="Repetir por", min_value=1, max_value=120, initial=1, required=False)
    intervalo_meses = forms.IntegerField(label="Intervalo (meses)", min_value=1, max_value=120, initial=1, required=False)
    observacao = forms.CharField(label="Observação", required=False, widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Observação do lançamento"}))
    numero_parcela = forms.IntegerField(label="Parcela atual", min_value=1, max_value=120, required=False)
    total_parcelas = forms.IntegerField(label="Total de parcelas", min_value=1, max_value=120, required=False)

    def __init__(self, *args, usuario=None, parcela: ParcelaCartao | None = None, cartao_preferido=None, ciclo=None, **kwargs):
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)
        self.usuario = usuario
        self.parcela = parcela
        self.ciclo_aviso = ""
        cartoes = Cartao.objects.none()
        categorias = Categoria.objects.none()
        pessoas = Pessoa.objects.none()
        if usuario:
            cartoes = Cartao.objects.filter(usuario=usuario, ativo=True)
            categorias = Categoria.objects.filter(usuario=usuario, ativa=True).select_related("categoria_pai")
            pessoas = Pessoa.objects.filter(usuario=usuario, ativa=True)
        if parcela:
            cartao_atual = parcela.fatura.cartao
            evento = parcela.compra.evento
            pessoa_atual = parcela.pessoa or (evento.pessoa if not parcela.pessoa_nome_livre else None)
            pessoa_nome_atual = (
                pessoa_atual.nome
                if pessoa_atual
                else parcela.pessoa_nome_livre or evento.pessoa_nome_livre
            )
            cartoes = Cartao.objects.filter(usuario=usuario).filter(Q(ativo=True) | Q(pk=cartao_atual.pk))
            if parcela.categoria_id:
                categorias = (
                    Categoria.objects.filter(usuario=usuario)
                    .filter(Q(ativa=True) | Q(pk=parcela.categoria_id))
                    .select_related("categoria_pai")
                )
            if pessoa_atual:
                pessoas = Pessoa.objects.filter(usuario=usuario).filter(Q(ativa=True) | Q(pk=pessoa_atual.pk))
            self.initial.update(
                {
                    "descricao": parcela.descricao_exibicao,
                    "natureza": parcela.natureza_exibicao,
                    "data_original": parcela.data_original_exibicao,
                    "valor": parcela.valor,
                    "cartao": cartao_atual,
                    "categoria": parcela.categoria,
                    "pessoa": pessoa_atual,
                    "pessoa_nome_livre": pessoa_nome_atual,
                    "ambito": parcela.ambito_exibicao,
                    "situacao": parcela.situacao,
                    "ciclo": parcela.fatura.ciclo,
                    "numero_parcela": parcela.numero,
                    "total_parcelas": parcela.total,
                    "observacao": evento.observacao,
                }
            )
            self.fields["cartao"].disabled = True
            del self.fields["quantidade_parcelas"]
            del self.fields["modo_lancamento"]
            del self.fields["quantidade_repeticoes"]
            del self.fields["intervalo_meses"]
            if evento.serie_recorrente_id:
                self.fields["aplicar_recorrencia"] = forms.ChoiceField(
                    label="Aplicar alterações",
                    choices=(
                        ("SOMENTE", "Alterar somente este lançamento"),
                        ("FUTUROS", "Replicar para os lançamentos futuros previstos"),
                    ),
                    initial="SOMENTE",
                )
        else:
            self.initial["ciclo"] = ciclo
            if cartao_preferido and cartao_preferido.ativo:
                self.initial["cartao"] = cartao_preferido
            del self.fields["numero_parcela"]
            del self.fields["total_parcelas"]
        self.fields["cartao"].queryset = cartoes.select_related("conta_pagamento_padrao")
        self.fields["categoria"].queryset = categorias
        pessoas = pessoas.order_by("nome", "id")
        self.fields["pessoa"].queryset = pessoas
        self.pessoas_sugestoes = pessoas
        self.fields["cartao"].empty_label = "Selecione o cartão"
        self.fields["categoria"].empty_label = "A classificar"
        self.fields["descricao"].widget.attrs["maxlength"] = 220 if parcela and len(parcela.descricao_exibicao) > 180 else 180

    def clean_descricao(self):
        descricao = self.cleaned_data["descricao"].strip()
        if len(descricao) > 180 and (not self.parcela or descricao != self.parcela.descricao_exibicao):
            raise forms.ValidationError("A descrição deve ter no máximo 180 caracteres.")
        return descricao

    def clean_pessoa_nome_livre(self):
        return self.cleaned_data["pessoa_nome_livre"].strip()

    def clean(self):
        cleaned = super().clean()
        ciclo = (cleaned.get("ciclo") or "").strip()
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", ciclo):
            self.add_error("ciclo", "Informe um ciclo válido.")
        elif cleaned.get("cartao") and cleaned.get("data_original"):
            self.ciclo_aviso = aviso_ciclo_cartao(
                cleaned["cartao"],
                cleaned["data_original"],
                ciclo,
                numero_parcela=cleaned.get("numero_parcela"),
                total_parcelas=cleaned.get("total_parcelas"),
            )
        valor = cleaned.get("valor")
        if self.parcela:
            numero = cleaned.get("numero_parcela")
            total = cleaned.get("total_parcelas")
            if numero and total and numero > total:
                self.add_error("numero_parcela", "A parcela atual não pode ultrapassar o total.")
            if numero and ParcelaCartao.objects.filter(compra=self.parcela.compra, numero=numero).exclude(pk=self.parcela.pk).exists():
                self.add_error("numero_parcela", "Já existe uma parcela com este número na compra.")
        else:
            quantidade = cleaned.get("quantidade_parcelas") or 1
            modo = cleaned.get("modo_lancamento") or (
                self.ModoLancamento.PARCELADO if quantidade > 1 else self.ModoLancamento.UNICO
            )
            repeticoes = cleaned.get("quantidade_repeticoes") or 1
            intervalo = cleaned.get("intervalo_meses") or 1
            if modo == self.ModoLancamento.PARCELADO and quantidade and cleaned.get("natureza") != NaturezaEvento.DESPESA:
                self.add_error("quantidade_parcelas", "Parcelamento está disponível somente para despesas.")
            if modo == self.ModoLancamento.PARCELADO and valor and quantidade > int(valor * 100):
                self.add_error("quantidade_parcelas", "A quantidade de parcelas não pode superar o valor em centavos.")
            cleaned["modo_lancamento"] = modo
            cleaned["quantidade_parcelas"] = quantidade if modo == self.ModoLancamento.PARCELADO else 1
            cleaned["quantidade_repeticoes"] = repeticoes if modo == self.ModoLancamento.RECORRENTE else 1
            cleaned["intervalo_meses"] = intervalo if modo == self.ModoLancamento.RECORRENTE else 1
        pessoa = cleaned.get("pessoa")
        pessoa_nome_livre = cleaned.get("pessoa_nome_livre", "")
        if pessoa and pessoa_nome_livre == pessoa.nome:
            cleaned["pessoa_nome_livre"] = ""
        elif pessoa:
            cleaned["pessoa"] = None
        return cleaned


class ImportacaoOFXForm(forms.ModelForm):
    arquivo_ofx = forms.FileField(label="Arquivo OFX")
    ciclo = forms.CharField(
        label="Ciclo (MM/AAAA)",
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Ex.: 10/2026", "inputmode": "numeric"}),
    )

    class Meta:
        model = Importacao
        fields = ["origem", "conta", "cartao", "ciclo"]

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario
        if usuario:
            self.fields["conta"].queryset = Conta.objects.filter(usuario=usuario)
            self.fields["cartao"].queryset = Cartao.objects.filter(usuario=usuario)

    def clean_arquivo_ofx(self):
        arquivo = self.cleaned_data["arquivo_ofx"]
        if not arquivo.name.lower().endswith(".ofx"):
            raise forms.ValidationError("Envie um arquivo OFX.")
        if arquivo.size > 10 * 1024 * 1024:
            raise forms.ValidationError("O arquivo OFX deve ter no máximo 10 MB.")
        return arquivo

    def clean(self):
        cleaned = super().clean()
        origem = cleaned.get("origem")
        conta = cleaned.get("conta")
        cartao = cleaned.get("cartao")
        ciclo = (cleaned.get("ciclo") or "").strip()
        if origem == Importacao.Origem.CONTA:
            if not conta:
                self.add_error("conta", "Confirme a conta financeira deste extrato.")
            cleaned["cartao"] = None
            cleaned["ciclo"] = ""
        elif origem == Importacao.Origem.CARTAO:
            if not cartao:
                self.add_error("cartao", "Confirme o cartão desta fatura.")
            match = re.fullmatch(r"(0[1-9]|1[0-2])/(\d{4})", ciclo)
            if not match:
                self.add_error("ciclo", "Informe o ciclo no formato MM/AAAA.")
            else:
                mes, ano = match.groups()
                cleaned["ciclo"] = f"{ano}-{mes}"
            cleaned["conta"] = None
        return cleaned

    def save(self, commit=True):
        arquivo = self.cleaned_data["arquivo_ofx"]
        posicao = arquivo.tell()
        arquivo.seek(0)
        digest = hashlib.sha256(arquivo.read()).hexdigest()
        arquivo.seek(posicao)
        instance = super().save(commit=False)
        instance.usuario = self.usuario
        instance.arquivo = arquivo
        instance.nome_arquivo = arquivo.name
        instance.hash_arquivo = digest
        instance.status = Importacao.Status.EM_REVISAO
        if commit:
            instance.save()
        return instance


class ImportacaoPlanilhaForm(forms.ModelForm):
    arquivo_planilha = forms.FileField(label="Planilha de compras futuras")
    ciclo = CicloField(label="Ciclo inicial (MM/AAAA)")

    class Meta:
        model = Importacao
        fields = ["cartao", "ciclo"]

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario
        self.fields["cartao"].queryset = Cartao.objects.filter(usuario=usuario) if usuario else Cartao.objects.none()
        self.fields["cartao"].widget.attrs["id"] = "id_planilha_cartao"
        self.fields["ciclo"].widget.attrs["id"] = "id_planilha_ciclo"

    def clean_arquivo_planilha(self):
        arquivo = self.cleaned_data["arquivo_planilha"]
        nome = arquivo.name.lower()
        if not (nome.endswith(".xlsx") or nome.endswith(".csv")):
            raise forms.ValidationError("Envie uma planilha XLSX ou CSV.")
        if arquivo.size > 10 * 1024 * 1024:
            raise forms.ValidationError("A planilha deve ter no máximo 10 MB.")
        return arquivo

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("cartao"):
            self.add_error("cartao", "Confirme o cartão das compras futuras.")
        if not cleaned.get("ciclo"):
            self.add_error("ciclo", "Informe o ciclo inicial da fatura.")
        return cleaned

    def save(self, commit=True):
        arquivo = self.cleaned_data["arquivo_planilha"]
        posicao = arquivo.tell()
        arquivo.seek(0)
        digest = hashlib.sha256(arquivo.read()).hexdigest()
        arquivo.seek(posicao)
        instance = super().save(commit=False)
        instance.usuario = self.usuario
        instance.origem = Importacao.Origem.CARTAO
        instance.conta = None
        instance.cartao = self.cleaned_data["cartao"]
        instance.ciclo = self.cleaned_data["ciclo"]
        instance.arquivo = arquivo
        instance.nome_arquivo = arquivo.name
        instance.hash_arquivo = digest
        instance.adaptador = "cartao_planilha"
        instance.versao_adaptador = "1.0"
        instance.status = Importacao.Status.EM_REVISAO
        if commit:
            instance.save()
        return instance


class ItemImportacaoForm(forms.ModelForm):
    valor_revisado = BRLDecimalField(label="Valor revisado (R$)", required=False)
    titulo_alvo_id = forms.IntegerField(required=False, widget=forms.HiddenInput())
    titulos_alvo_ids = forms.CharField(required=False, widget=forms.HiddenInput())
    movimento_alvo_id = forms.IntegerField(required=False, widget=forms.HiddenInput())
    parcela_alvo_id = forms.IntegerField(required=False, widget=forms.HiddenInput())
    remover_vinculo = forms.BooleanField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = ItemImportacao
        field_classes = {"categoria": CategoriaModelChoiceField}
        fields = [
            "data_revisada",
            "valor_revisado",
            "descricao_revisada",
            "natureza",
            "categoria",
            "pessoa",
            "ambito",
            "acao",
            "observacao",
        ]
        widgets = {
            "data_revisada": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "observacao": forms.TextInput(attrs={"placeholder": "Observação do lançamento"}),
        }

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["data_revisada"].input_formats = ["%Y-%m-%d"]
        if usuario:
            self.fields["categoria"].queryset = Categoria.objects.filter(
                usuario=usuario, ativa=True
            ).select_related("categoria_pai")
            self.fields["pessoa"].queryset = Pessoa.objects.filter(usuario=usuario, ativa=True)

    def clean(self):
        cleaned = super().clean()
        acao = cleaned.get("acao")
        titulo_id = cleaned.get("titulo_alvo_id")
        titulos_ids_brutos = cleaned.get("titulos_alvo_ids") or ""
        movimento_id = cleaned.get("movimento_alvo_id")
        parcela_id = cleaned.get("parcela_alvo_id")
        remover_vinculo = cleaned.get("remover_vinculo", False)
        try:
            titulos_ids = [int(valor) for valor in titulos_ids_brutos.split(",") if valor.strip()]
        except ValueError:
            self.add_error("acao", "A seleção de títulos contém um identificador inválido.")
            titulos_ids = []
        if len(titulos_ids) != len(set(titulos_ids)):
            self.add_error("acao", "O mesmo título não pode ser selecionado mais de uma vez.")
        cleaned["titulos_alvo_ids"] = titulos_ids
        if (
            (self.instance.evento_id or self.instance.movimento_conta_id or self.instance.parcela_id)
            and self.instance.status == ItemImportacao.Status.CONCILIADO
            and not self.instance.vinculo_manual_pendente
            and acao == ItemImportacao.Acao.CRIAR
        ):
            self.add_error("acao", "Um registro já conciliado não pode ser recriado. Vincule-o ou ignore a evidência.")
        if acao == ItemImportacao.Acao.VINCULAR:
            possui_titulos_multiplos = self.instance.titulos_multiplos.exists()
            possui_destino_existente = bool(
                self.instance.evento_id
                or self.instance.movimento_conta_id
                or self.instance.parcela_id
                or possui_titulos_multiplos
            )
            destinos = int(bool(titulo_id)) + int(bool(titulos_ids)) + int(bool(movimento_id)) + int(bool(parcela_id))
            if destinos > 1:
                self.add_error("acao", "Selecione títulos ou somente um destino para o vínculo.")
            elif remover_vinculo and destinos:
                self.add_error("acao", "Remova o destino selecionado antes de confirmar a remoção do vínculo.")
            elif not titulo_id and not titulos_ids and not movimento_id and not parcela_id and not remover_vinculo and not possui_destino_existente:
                mensagem = (
                    "Selecione uma parcela prevista para vincular."
                    if self.instance.importacao.origem == Importacao.Origem.CARTAO
                    else "Selecione um título ou movimento para vincular."
                )
                self.add_error("acao", mensagem)
            elif titulos_ids and self.instance.importacao.origem != Importacao.Origem.CONTA:
                self.add_error("acao", "A seleção de vários títulos está disponível somente para importações de conta.")
            if (
                self.instance.status == ItemImportacao.Status.CONCILIADO
                and self.instance.evento_id
                and not self.instance.vinculo_manual_pendente
            ):
                titulo_alterado = bool(titulo_id) and titulo_id != self.instance.titulo_alvo_id
                movimento_alterado = bool(movimento_id) and movimento_id != self.instance.movimento_conta_id
                parcela_alterada = bool(parcela_id) and parcela_id != self.instance.parcela_id
                if titulo_alterado or movimento_alterado or parcela_alterada:
                    self.add_error("acao", "O vínculo já conciliado não pode ser substituído nesta revisão.")
        return cleaned


class FechamentoFaturaForm(forms.Form):
    vencimento = forms.DateField(
        label="Vencimento",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        input_formats=["%Y-%m-%d"],
    )
    conta = forms.ModelChoiceField(queryset=Conta.objects.none(), required=False, label="Conta de pagamento")
    observacao = forms.CharField(label="Observação", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, usuario=None, fatura=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["conta"].queryset = Conta.objects.filter(usuario=usuario, ativa=True).order_by("nome")
        if fatura:
            self.initial.update({"vencimento": fatura.vencimento, "conta": fatura.cartao.conta_pagamento_padrao})


class ProgramacaoPagamentoForm(forms.Form):
    idempotency_key = forms.CharField(required=False, widget=forms.HiddenInput())
    conta = forms.ModelChoiceField(queryset=Conta.objects.none(), label="Conta")
    data_programada = forms.DateField(
        label="Data programada", widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}), input_formats=["%Y-%m-%d"]
    )
    valor = BRLDecimalField(label="Valor (R$)", min_value=Decimal("0.01"))
    observacao = forms.CharField(label="Observação", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, usuario=None, titulo=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["conta"].queryset = Conta.objects.filter(usuario=usuario, ativa=True).order_by("nome")
        if titulo:
            self.initial.update({"conta": titulo.conta, "data_programada": titulo.vencimento})
        self.initial.setdefault("idempotency_key", str(uuid4()))


class ConciliacaoPagamentoForm(forms.Form):
    movimento = forms.ModelChoiceField(queryset=MovimentoConta.objects.none(), label="Débito existente em conta")
    valor = BRLDecimalField(label="Valor a conciliar (R$)", min_value=Decimal("0.01"), required=False)

    def __init__(self, *args, usuario=None, titulo=None, **kwargs):
        super().__init__(*args, **kwargs)
        movimentos = MovimentoConta.objects.none()
        if usuario and titulo:
            movimentos = MovimentoConta.objects.filter(
                conta__usuario=usuario, valor__lt=0
            ).select_related("conta", "evento").order_by("-data_efetiva", "-id")
            if titulo.conta_id:
                movimentos = movimentos.filter(conta=titulo.conta)
        self.fields["movimento"].queryset = movimentos


class PagamentoManualFaturaForm(forms.Form):
    idempotency_key = forms.CharField(required=False, widget=forms.HiddenInput())
    conta = forms.ModelChoiceField(queryset=Conta.objects.none(), label="Conta")
    data_pagamento = forms.DateField(
        label="Data do pagamento", widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}), input_formats=["%Y-%m-%d"]
    )
    valor = BRLDecimalField(label="Valor (R$)", min_value=Decimal("0.01"))
    observacao = forms.CharField(label="Observação", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, usuario=None, titulo=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["conta"].queryset = Conta.objects.filter(usuario=usuario, ativa=True).order_by("nome")
        if titulo:
            self.initial["conta"] = titulo.conta
        self.initial.setdefault("idempotency_key", str(uuid4()))


class TransporteSaldoFaturaForm(forms.Form):
    idempotency_key = forms.CharField(required=False, widget=forms.HiddenInput())
    fatura_destino = forms.ModelChoiceField(queryset=Titulo.objects.none(), label="Fatura de destino")
    valor = BRLDecimalField(label="Valor transportado (R$)", min_value=Decimal("0.01"))
    data_identificacao = forms.DateField(
        label="Data de identificação", widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}), input_formats=["%Y-%m-%d"]
    )
    observacao = forms.CharField(label="Observação", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, usuario=None, titulo=None, **kwargs):
        super().__init__(*args, **kwargs)
        # A escolha é feita por fatura, mas o queryset usa o modelo correto na view.
        from .models import Fatura
        self.fields["fatura_destino"] = forms.ModelChoiceField(
            queryset=Fatura.objects.filter(usuario=usuario, cartao=titulo.fatura.cartao, status=Fatura.Status.ABERTA).exclude(pk=titulo.fatura_id),
            label="Fatura de destino",
        )
        self.initial.setdefault("idempotency_key", str(uuid4()))


class ReaberturaFaturaForm(forms.Form):
    motivo = forms.CharField(
        label="Motivo da reabertura ou correção",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="O motivo ficará registrado no histórico da fatura.",
    )


class AplicacaoLoteForm(forms.Form):
    categoria = CategoriaModelChoiceField(queryset=Categoria.objects.none(), required=False)
    pessoa = forms.ModelChoiceField(queryset=Pessoa.objects.none(), required=False)
    ambito = forms.ChoiceField(choices=[("", "Manter âmbito atual"), *Ambito.choices], required=False)
    acao = forms.ChoiceField(
        choices=[
            ("", "Manter ação atual"),
            (ItemImportacao.Acao.CRIAR, ItemImportacao.Acao.CRIAR.label),
            (ItemImportacao.Acao.IGNORAR, ItemImportacao.Acao.IGNORAR.label),
        ],
        required=False,
        label="Ação",
    )
    observacao = forms.CharField(
        required=False,
        label="Observação",
        widget=forms.TextInput(attrs={"placeholder": "Manter observação atual"}),
    )
    somente_status = forms.ChoiceField(
        choices=[("", "Todos os itens"), *ItemImportacao.Status.choices, ("SELECIONADOS", "Selecionados")],
        required=False,
        label="Aplicar somente em",
    )

    def __init__(self, *args, usuario=None, itens_selecionados=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.itens_selecionados = itens_selecionados or []
        if usuario:
            self.fields["categoria"].queryset = Categoria.objects.filter(
                usuario=usuario, ativa=True
            ).select_related("categoria_pai")
            self.fields["pessoa"].queryset = Pessoa.objects.filter(usuario=usuario, ativa=True)

    def clean(self):
        cleaned = super().clean()
        if not any(cleaned.get(field) for field in ("categoria", "pessoa", "ambito", "acao", "observacao")):
            raise forms.ValidationError("Selecione ao menos uma informação para aplicar ao lote.")
        if cleaned.get("somente_status") == "SELECIONADOS" and not self.itens_selecionados:
            raise forms.ValidationError("Selecione ao menos um item da importação.")
        return cleaned


class TokenAcessoApiForm(forms.Form):
    nome = forms.CharField(
        label="Nome do token",
        max_length=120,
        help_text="Use um nome que identifique a automação, por exemplo: n8n WhatsApp.",
    )
