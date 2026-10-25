import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from financeiro.services.notificacoes import executar_ciclo_notificacoes


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Executa o agendador da central de notificacoes financeiras."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Verifica o agendamento apenas uma vez.")
        parser.add_argument(
            "--forcar",
            action="store_true",
            help="Reprocessa imediatamente a data atual, mesmo que ja tenha sido concluida.",
        )

    def handle(self, *args, **options):
        if options["forcar"] and not options["once"]:
            raise CommandError("Use --forcar junto com --once para evitar reprocessamento continuo.")

        while True:
            try:
                execucao = executar_ciclo_notificacoes(forcar=options["forcar"])
                if execucao:
                    self.stdout.write(
                        self.style.SUCCESS(
                            "Central processada: "
                            f"status={execucao.status}, usuarios={execucao.usuarios_processados}, "
                            f"criadas={execucao.notificacoes_criadas}, "
                            f"atualizadas={execucao.notificacoes_atualizadas}, "
                            f"arquivadas={execucao.notificacoes_arquivadas}, "
                            f"erros={len(execucao.erros)}."
                        )
                    )
            except DatabaseError as exc:
                logger.error("Falha de banco na central de notificacoes: %s", type(exc).__name__)
                if options["once"]:
                    raise CommandError("Nao foi possivel acessar o banco da central de notificacoes.") from exc
            except Exception as exc:
                logger.exception("Falha tecnica na central de notificacoes: %s", type(exc).__name__)
                if options["once"]:
                    raise CommandError("A central de notificacoes falhou.") from exc

            if options["once"]:
                return
            time.sleep(settings.CENTRAL_NOTIFICACOES_INTERVALO_VERIFICACAO)
