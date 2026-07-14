# PDF2MD — Conversor de PDF Jurídico para Markdown

## O que é este projeto

Aplicação web Django que converte PDFs jurídicos em Markdown fiel usando a API Gemini (Google). O fluxo é: upload de um ou mais PDFs → divisão em blocos de páginas → conversão por IA → validação por IA → correção automática → merge final → download.

Implantado no Render (free tier). Arquivos de mídia são efêmeros (somem ao reiniciar o serviço), o que é intencional.

## Arquitetura

```
pdf2md_project/       # Configurações Django (settings, urls, wsgi, asgi)
converter/
  models.py           # ConversionJob — única tabela do projeto
  views.py            # upload_view, queue_view, download_all, progress_status
  urls.py             # Rotas sob o app "converter"
  forms.py            # PdfUploadForm com suporte a múltiplos arquivos
  pipeline/
    splitter.py       # Corta PDF em blocos de N páginas (pypdf)
    gemini_client.py  # Wrappers para a API Gemini (convert, validate, reconvert)
    runner.py         # Orquestra splitter → convert → validate/fix loop → merge
    merger.py         # Junta blocos de Markdown em documento final
    queue_worker.py   # Fila em memória (threading.Queue) + daemon thread único
  prompts/
    converter_prompt.txt   # System prompt do transcritor jurídico
    validator_prompt.txt   # System prompt do validador
    reconvert_prompt.txt   # Template do prompt de reconversão com feedback
  templates/converter/     # HTML das páginas (upload, queue, base)
  migrations/              # Migrações Django
```

## Modelo de dados

`ConversionJob` (única model):

| Campo | Tipo | Descrição |
|---|---|---|
| `original_pdf` | FileField | PDF original (upload_to="uploads/") |
| `original_filename` | CharField | Nome original para exibição e nomeação do .md |
| `status` | CharField | queued / running / done / failed |
| `current_step` | CharField | splitting / converting / validating / merging / rate_limited / fixing |
| `total_blocks` / `current_block` | PositiveIntegerField | Progresso por bloco |
| `retry_attempt/max/wait_seconds` | PositiveIntegerField | Estado de rate-limit retry |
| `fix_attempt/max` | PositiveIntegerField | Estado de correção automática por bloco |
| `error_message` | TextField | Stack trace ou msg de erro em caso de falha |
| `result_file` | FileField | .md gerado (upload_to="results/") |
| `needs_review` | BooleanField | True se algum bloco não foi aprovado nem após MAX_FIX_ATTEMPTS |
| `review_notes` | TextField | Detalhes dos blocos que precisam de revisão manual |

## Pipeline de conversão

1. **Split** — `split_pdf()` usa `pypdf` para fatiar o PDF em blocos de `PDF_BLOCK_SIZE` páginas (padrão: 5).
2. **Convert** — cada bloco é enviado ao Gemini (`gemini-3.1-flash-lite` por padrão) com o `converter_prompt.txt`.
3. **Validate** — o mesmo bloco + o Markdown gerado são enviados ao validador Gemini, que retorna JSON estruturado `{aprovado, motivo, trechos_problematicos}`. Carimbos/rubricas são filtrados pelo `_sanitize_validation()`.
4. **Fix loop** — se reprovado, o bloco é reenviado com feedback via `reconvert_prompt.txt` (até `MAX_FIX_ATTEMPTS = 3` vezes). Se ainda reprovado, aceita o melhor esforço e marca `needs_review = True`.
5. **Merge** — todos os blocos são unidos por `merge_blocks()` separados por `\n\n`.
6. **Rate limit retry** — qualquer chamada ao Gemini em erro 429 é reexecutada com espera crescente (30s, 60s, 90s, 120s) via `tenacity`.

## Fila de processamento

`queue_worker.py` usa `threading.Queue` + um daemon thread único. Jobs são processados **um de cada vez** para não estourar o rate limit da API. A fila é **em memória** — jobs pendentes são perdidos se o servidor reiniciar.

## Variáveis de ambiente

| Variável | Padrão | Descrição |
|---|---|---|
| `GEMINI_API_KEY` | — | Chave genérica — usada como fallback se as específicas abaixo não forem definidas |
| `GEMINI_API_KEY_CONVERTER` | `GEMINI_API_KEY` | Chave exclusiva para a etapa de conversão |
| `GEMINI_API_KEY_VALIDATOR` | `GEMINI_API_KEY` | Chave exclusiva para a etapa de validação |
| `GEMINI_API_KEY_RECONVERTER` | `GEMINI_API_KEY` | Chave exclusiva para a etapa de reconversão |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Modelo Gemini correto — gratuito e com maior cota de uso. **Não alterar sem pedido explícito.** |
| `PDF_BLOCK_SIZE` | `5` | Páginas por bloco |
| `DJANGO_SECRET_KEY` | insecure default | Obrigatória em produção |
| `DJANGO_DEBUG` | `True` | Setar `False` em produção |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Hosts separados por vírgula |
| `RENDER_EXTERNAL_HOSTNAME` | — | Setado automaticamente pelo Render |
| `DATABASE_URL` | — | Opcional; SQLite é o padrão local |

## Como rodar localmente

```bash
python -m venv venv
venv\Scripts\activate       # Windows
pip install -r requirements.txt
cp .env.example .env        # preencher GEMINI_API_KEY
python manage.py migrate
python manage.py runserver
```

## Deploy (Render)

- Runtime: `python-3.13` (ver `runtime.txt`)
- Comando de build: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate`
- Comando de start: `gunicorn pdf2md_project.wsgi`
- `WhiteNoise` serve os arquivos estáticos diretamente do gunicorn
- Banco: SQLite local (efêmero no Render free tier) ou PostgreSQL via `DATABASE_URL`

## Decisões de design importantes

- **Sem Celery/Redis**: a fila em `threading.Queue` foi escolhida para simplicidade e zero dependências externas. Funciona bem para um único worker. Não escala para múltiplos processos/réplicas.
- **Mídia efêmera**: PDFs e markdowns ficam em `MEDIA_ROOT` local — somem ao reiniciar o Render. O usuário deve baixar antes de fechar o browser.
- **Validação estruturada**: o validador usa `response_schema` do Gemini para garantir JSON confiável, sem parsing frágil.
- **Best-effort em vez de falha**: quando o fix loop se esgota, o job conclui com `needs_review = True` em vez de falhar, preservando o trabalho já feito.

## Limitações conhecidas

- **Fila em memória**: jobs pendentes são perdidos se o servidor reiniciar (intencional no Render free tier).
- **Gunicorn deve rodar com `--workers 1`**: a fila usa `threading.Queue` por processo. Com múltiplos workers do Gunicorn, cada processo tem fila própria — jobs enviados a um processo não são visíveis para outro.
- **Sem autenticação**: qualquer pessoa com a URL pode fazer upload. Não há controle de acesso.
- **Upload sem verificação de MIME type**: a validação só checa a extensão `.pdf`. Um arquivo arbitrário renomeado para `.pdf` é aceito e salvo em disco antes de falhar na conversão. Para mitigar: adicionar verificação dos primeiros bytes (`%PDF-`) em `forms.py:clean_pdf_files`.
- **Sem limite explícito de tamanho de arquivo**: depende dos defaults do Django/gunicorn.
- **Mídia efêmera**: PDFs e markdowns ficam em `MEDIA_ROOT` local e somem ao reiniciar o Render. O usuário deve baixar antes de fechar o browser.
