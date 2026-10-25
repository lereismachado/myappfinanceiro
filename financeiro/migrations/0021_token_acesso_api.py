from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("financeiro", "0020_conta_nao_compoe_dashboard"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TokenAcessoApi",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("nome", models.CharField(max_length=120)),
                ("prefixo", models.CharField(max_length=24, unique=True)),
                ("hash_token", models.CharField(max_length=64, unique=True)),
                ("ativo", models.BooleanField(default=True)),
                ("ultimo_uso_em", models.DateTimeField(blank=True, null=True)),
                ("revogado_em", models.DateTimeField(blank=True, null=True)),
                (
                    "usuario",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tokens_acesso_api",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-criado_em", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="tokenacessoapi",
            index=models.Index(fields=["usuario", "ativo"], name="fin_token_usuario_ativo_idx"),
        ),
    ]
