# Atividade 2 — serviços para experimentos de provisionamento

Este repositório contém três backends independentes em FastAPI. Cada serviço
tem uma carga sintética configurável e foi desenhado para ter um recurso
dominante diferente:

- `cpu_service`: recomendação de produtos por similaridade entre vetores;
- `memory_service`: catálogo/cache residente em um buffer grande na memória;
- `io_service`: escrita, leitura e remoção de documentos no disco local.

As aplicações não possuem frontend, autenticação ou banco de dados externo. A
intenção é manter o comportamento controlável e repetível para os experimentos
da disciplina.

## Instalação

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Execução

É possível iniciar cada serviço em um terminal separado:

```bash
uvicorn services.cpu_service.app:app --host 0.0.0.0 --port 8001
uvicorn services.memory_service.app:app --host 0.0.0.0 --port 8002
uvicorn services.io_service.app:app --host 0.0.0.0 --port 8003
```

Também é possível executar diretamente os módulos:

```bash
python services/cpu_service/app.py
python services/memory_service/app.py
python services/io_service/app.py
```

Cada serviço disponibiliza documentação interativa em `/docs` e um endpoint
`/health`.

## Exemplos de uso

### CPU-bound — recomendação

O catálogo é gerado deterministicamente pelo `seed` e fica em um cache limitado
para que as requisições seguintes meçam principalmente o cálculo de
similaridade.

```bash
curl -X POST http://localhost:8001/recommend \
  -H 'content-type: application/json' \
  -d '{"items": 10000, "dimensions": 64, "top_k": 10, "iterations": 2, "seed": 42}'
```

Os principais controles são `items`, `dimensions` e `iterations`. O número de
operações aproximado é `items * dimensions * iterations`.

### Memory-bound — catálogo em memória

O serviço aloca um único conjunto de trabalho residente. Alterar `dataset_mb`
substitui o conjunto anterior; repetir a mesma configuração aproveita o buffer
já carregado.

```bash
curl 'http://localhost:8002/cache/scan?dataset_mb=128&passes=3&mode=read&seed=42'
curl 'http://localhost:8002/cache/status'
```

`mode=read_write` também grava em cada passagem:

```bash
curl 'http://localhost:8002/cache/scan?dataset_mb=256&passes=2&mode=read_write'
```

### IO-bound — documentos

Para um teste que grava, lê e remove arquivos temporários:

```bash
curl -X POST \
  'http://localhost:8003/documents/workload?size_mb=32&repetitions=2&sync=false'
```

Para manter um documento no diretório de dados e depois consultá-lo:

```bash
created=$(curl -s -X POST 'http://localhost:8003/documents?size_mb=8&sync=true')
echo "$created"
curl http://localhost:8003/documents/<id-retornado>
curl -X DELETE http://localhost:8003/documents/<id-retornado>
```

O diretório pode ser alterado sem modificar o código:

```bash
IO_DATA_DIR=/local/uasc/poi-io-data \
  uvicorn services.io_service.app:app --host 0.0.0.0 --port 8003
```

`sync=true` chama `fsync` depois da escrita e é útil para comparar uma escrita
confirmada no dispositivo com uma escrita que pode permanecer no cache do
sistema operacional.

## Testes

```bash
pytest -q
```

Os testes são smoke tests: validam o contrato básico dos três endpoints sem
executar cargas grandes.

## Decisões de design

### Interfaces independentes

Cada perfil é um serviço e uma porta separados. Isso deixa claro qual processo
está sendo medido na VM e permite selecionar apenas um deles para o deploy
exigido pela atividade.

### Dados sintéticos determinísticos

Os serviços de CPU e memória aceitam `seed`; o serviço de IO usa um padrão
fixo de bytes. Assim, dois cenários com a mesma configuração geram o mesmo tipo
de carga. Os dados são gerados localmente, sem depender de rede, APIs externas
ou datasets que precisem ser distribuídos.

### Parâmetros com limites

Os parâmetros são validados pelo FastAPI/Pydantic. Há limites máximos para
evitar que uma chamada experimental acidental aloque vários gigabytes ou faça
um cálculo inviável. Esses limites podem ser ajustados por variáveis de
ambiente, por exemplo `MEMORY_MAX_MB=768`.

### CPU service

O catálogo usa NumPy apenas para armazenar e gerar os vetores. O cálculo de
similaridade é um loop explícito em Python, com complexidade proporcional a
`items * dimensions * iterations`. Isso torna o custo computacional visível em
uma VM pequena e evita que uma chamada vetorizada esconda o trabalho em uma
operação muito rápida de biblioteca.

### Memory service

O conjunto de trabalho é um `numpy.ndarray` de bytes, pois seu tamanho é
previsível e não sofre a grande sobrecarga de milhares de objetos Python. O
buffer é preenchido para que as páginas sejam efetivamente comprometidas pelo
sistema operacional. A varredura usa operações nativas do NumPy e retorna
apenas um checksum, sem transferir o buffer pela rede.

### IO service

Os arquivos são processados em blocos de 1 MiB. Isso evita carregar um arquivo
inteiro na RAM e mantém o experimento focado no disco. O endpoint de workload
remove os arquivos ao final, evitando que vários testes encham a VM. O
`fsync` é opcional porque muda significativamente o comportamento observado em
sistemas com cache de escrita.

### Aquecimento dos testes

Antes de registrar resultados, faça uma requisição de aquecimento para cada
configuração. Isso separa custos de inicialização, geração de dataset e cache
do custo representativo das requisições repetidas. A resposta de cada serviço
informa tempos internos e se o dataset foi reutilizado.
