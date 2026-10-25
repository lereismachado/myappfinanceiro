# myAppFinanceiro

Aplicação web de gestão financeira pessoal e profissional, construída com Django, templates renderizados no servidor e PostgreSQL. O sistema reúne contas, cartões, faturas, lançamentos, previsões, importações e uma Visão Geral consolidada por usuário.

As regras financeiras detalhadas e suas evidências estão em [`DOSSIER_REGRAS_FINANCEIRAS.md`](DOSSIER_REGRAS_FINANCEIRAS.md). O arquivo [`diagnostico-e-projeto-financeiro.md`](diagnostico-e-projeto-financeiro.md) e o [`PR.md`](PR.md) registram o diagnóstico e o escopo inicial do projeto; devem ser lidos como documentos históricos.

## Funcionalidades atuais

### Visão Geral

- Intervalo inclusivo por data inicial e final, com mês corrente como padrão e compatibilidade com o parâmetro mensal legado.
- Receitas, despesas líquidas de estornos elegíveis, resultado, fluxo de caixa, saldo atual e saldo projetado.
- Fatura confirmada e projetada, compromissos, limite comprometido dos cartões, categorias, essencialidade, natureza, evolução e simulador de economia.
- Detalhamento consultivo dos indicadores em modal, com ligação para a listagem de lançamentos filtrada.
- Controle para ocultar valores da página inteira. A preferência fica somente no `localStorage` do navegador e também cobre modal e simulador.

### Lançamentos, cartões e faturas

- Lançamentos de conta e cartão, realizados ou previstos, com categoria, pessoa, âmbito e observação.
- Compras parceladas e recorrências manuais em meses inteiros.
- Na recorrência, a primeira ocorrência mantém a situação escolhida e as futuras são materializadas como previstas. A propagação de edição preserva a agenda original.
- Exclusão opcional de ocorrências futuras apenas quando ainda são previstas e editáveis; realizados, importados, liquidados e faturas bloqueadas são preservados.
- Filtros combináveis, categorias hierárquicas, período inicial do mês corrente e agrupamento visual de cartão por fatura.
- Clonagem de lançamentos e pessoa em texto livre, sem criar cadastro silenciosamente.
- Fechamento, reabertura, pagamento parcial e transporte de saldo de fatura.

### Importação e conciliação

- OFX de conta corrente e cartão em duas fases: revisão e confirmação transacional.
- Planilhas XLSX/CSV para planejar compras futuras de cartão.
- Reimportação idempotente por chave/FITID, sem duplicar eventos, movimentos, parcelas ou liquidações.
- Pré-vínculo automático somente quando existe um único destino elegível com data e valor exatos. A sugestão continua editável e é revalidada na confirmação.
- Busca manual ampla por “Buscar em todos”, sem remover as restrições de usuário, cartão, ciclo, fatura aberta e destino ainda disponível.
- Em OFX de conta, um crédito ou débito pode conciliar manualmente vários títulos previstos quando a soma dos saldos selecionados é exatamente igual ao valor do extrato. A confirmação gera um movimento e uma liquidação por título, sem alterar o evento previsto de origem.
- Sugestão de categoria e âmbito pelo histórico recente, com precedência para regras e valores informados explicitamente.
- Alterações em massa preservam itens já revisados; a edição individual permanece disponível.
- Observações do usuário são preservadas. Os importadores não acrescentam textos técnicos automáticos à observação financeira.

### PWA

- Manifesto instalável, ícones, modo `standalone` e atalhos para Novo lançamento, Lançamentos e Cartões e faturas.
- Botão de instalação no layout autenticado e na tela de login, quando suportado pelo navegador.
- Service worker com escopo raiz e página de contingência sem conexão.
- Somente o shell público da PWA é armazenado: página offline, manifesto e ícones. Páginas autenticadas e dados financeiros não são disponibilizados para edição ou consulta offline.

## Regras importantes

- Pagamento de fatura afeta o caixa e o saldo da conta, mas não volta a entrar como despesa no resultado.
- `Fatura.ciclo` representa o mês esperado de vencimento/pagamento. O ciclo informado pelo usuário é preservado; divergências de data geram aviso, não realocação automática.
- Sem vencimento explícito, uma compra feita até o fechamento, inclusive, sugere pagamento no mês seguinte; depois do fechamento, sugere dois meses à frente.
- OFX e planilha permanecem fora da recorrência manual.
- Valores previstos não movimentam o saldo atual. Parcelas previstas participam da fatura projetada, não da confirmada.

## Requisitos

- Python compatível com as dependências de `requirements.txt`.
- PostgreSQL para o ambiente principal.
- Variáveis de ambiente configuradas a partir de `.env.example`.
- Para instalação da PWA e funcionamento do service worker: HTTPS ou `localhost`.

## Configuração local

Copie o arquivo de exemplo e preencha as credenciais somente no `.env` local:

```powershell
Copy-Item .env.example .env
```

As principais variáveis são:

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `DJANGO_SECURE_PROXY_SSL_HEADER`
- `DJANGO_USE_SQLITE`
- `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` e `POSTGRES_TEST_TEMPLATE`

O `.env.example` contém hosts e endereços próprios desta instalação. Ajuste-os ao ambiente de destino; `ALLOWED_HOSTS`, origem CSRF e protocolo precisam corresponder ao endereço realmente usado.

```text
POSTGRES_HOST=100.127.41.45
POSTGRES_PORT=5432
POSTGRES_DB=myappfinanceiro
POSTGRES_USER=postgres
POSTGRES_PASSWORD=preencher-no-arquivo-local
```

Não versione senhas ou outras credenciais reais.

Para preparar o ambiente virtual e instalar as dependências:

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Para aplicar migrations e iniciar o servidor:

```powershell
& .\.venv\Scripts\python.exe manage.py migrate
& .\.venv\Scripts\python.exe manage.py runserver
```

O modo SQLite existe para verificações locais que não devem tocar no PostgreSQL configurado:

```powershell
$env:DJANGO_USE_SQLITE='True'
& .\.venv\Scripts\python.exe manage.py migrate
& .\.venv\Scripts\python.exe manage.py runserver
```

## Fluxo de importação

1. Acesse **Importação**.
2. Escolha OFX de conta, OFX de cartão ou planilha de compras futuras.
3. Para cartão, informe o cartão e o ciclo `AAAA-MM`.
4. Revise classificação, ação e eventual destino sugerido.
5. Use a busca manual quando a sugestão estrita não for suficiente. Em OFX de conta, podem ser marcados vários títulos previstos; a soma precisa fechar exatamente com o valor do item.
6. Confirme o lote. A gravação é transacional; uma falha reverte a confirmação e mantém o lote disponível para revisão.

Uma sugestão de vínculo não significa conciliação concluída. Somente uma reimportação já confirmada pode reaproveitar automaticamente um destino; vínculos novos permanecem sob revisão humana.

## Execução com Docker

O Compose contém o serviço web e o agendador da central de notificações. O PostgreSQL é externo e continua configurado pelas variáveis `POSTGRES_*` do `.env`.

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f web
docker compose logs -f notificacoes
```

O container web aplica migrations antes de iniciar o Gunicorn e publicar a aplicação na porta `8099`. O serviço `notificacoes` executa separadamente, consulta as previsões do dia às `06:00` no fuso configurado pelo Django e recupera a execução quando o ambiente volta depois desse horário.

Para conferir o agendamento uma vez ou reprocessar deliberadamente a data atual:

```bash
python manage.py executar_central_notificacoes --once
python manage.py executar_central_notificacoes --once --forcar
```

Os endereços configurados nesta instalação são:

```text
http://100.127.41.45:8099
http://192.168.100.212:8099
```

Esses endereços não foram validados em runtime nesta revisão documental. As origens HTTP podem servir a aplicação, mas normalmente não constituem contexto seguro para instalar a PWA. Para instalação e service worker, use `localhost` ou a origem HTTPS configurada no proxy/Tailscale, por exemplo:

```text
https://srv-bd.tail638df2.ts.net
```

Para reconstruir e atualizar somente o container da aplicação:

```bash
docker compose build --no-cache
docker compose up -d
```

Para encerrar o container:

```bash
docker compose down
```

O banco PostgreSQL externo não é removido por esses comandos. Os diretórios `media/` e `staticfiles/` permanecem no host.

## Verificação automatizada

Use o Python do ambiente virtual e execute as suítes sequencialmente, pois elas compartilham o banco de testes preservado. O exemplo abaixo ativa SQLite e aplica as migrations locais para não acessar o PostgreSQL configurado; remova essa variável somente quando quiser validar deliberadamente o banco de testes PostgreSQL do ambiente.

```powershell
$env:DJANGO_USE_SQLITE='True'
& .\.venv\Scripts\python.exe manage.py migrate
& .\.venv\Scripts\python.exe manage.py check
& .\.venv\Scripts\python.exe manage.py test financeiro.tests.test_dashboard --keepdb
& .\.venv\Scripts\python.exe manage.py test financeiro.tests.test_importacao_service financeiro.tests.test_importacao_planilha --keepdb
& .\.venv\Scripts\python.exe manage.py test financeiro.tests.test_lancamentos --keepdb
& .\.venv\Scripts\python.exe manage.py test financeiro.tests.test_pwa --keepdb
& .\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
& .\.venv\Scripts\python.exe manage.py showmigrations financeiro
```

### Estado verificado em 16/10/2026

- `manage.py check`: aprovado, sem problemas.
- Dashboard: 63 testes aprovados em execução isolada.
- Importação OFX e planilha: 71 testes aprovados.
- PWA: 6 testes aprovados.
- Lançamentos: 39 aprovados e 1 falha; a divergência observada é textual, pois o teste ainda espera a mensagem anterior do formulário.
- Migrations `financeiro.0001` a `financeiro.0015`: aplicadas no ambiente consultado em 16/10/2026.
- Atualização de 19/10/2026: a migration `financeiro.0016_vinculo_titulo_importacao` deve ser aplicada para habilitar a conciliação de vários títulos por item OFX de conta.
- `makemigrations --check --dry-run`: continua identificando a renomeação preexistente do índice de `SaldoContaPeriodo`, agora como possível `0017`; ela não integra esta evolução.
- `git diff --check`: sem erros de whitespace nos arquivos rastreados; foram emitidos apenas avisos de conversão LF/CRLF.

Esses resultados não comprovam instalação PWA, funcionamento offline, interação completa em navegador, build Docker, proxy HTTPS, PostgreSQL de produção ou ambiente publicado.

## Documentação

- `DOSSIER_REGRAS_FINANCEIRAS.md`: regras financeiras atuais, evidências, riscos e limites de validação.
- `diagnostico-e-projeto-financeiro.md`: diagnóstico financeiro e desenho inicial, mantido como histórico.
- `PR.md`: resumo da primeira entrega do MVP, mantido como histórico.
