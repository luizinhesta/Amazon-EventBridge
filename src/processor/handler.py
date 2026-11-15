"""
EventBridge - Lambda processamento.

Consome mensagens da fila SQS que e target de uma rule (ex.: order.created).
Representa o processamento assincrono desacoplado. Registra evidencia de
processamento na tabela DynamoDB.

Esta funcao e um CONSUMIDOR da fila SQS (event source mapping), nao um target
direto do EventBridge. Isso ilustra a diferenca entre:
- DLQ de target do EventBridge (falha de ENTREGA a fila), e
- falhas no PROCESSAMENTO interno desta Lambda (redrive da propria fila SQS).

Falha controlada e reversivel: se o detail contiver {"forceFail": true}, a funcao
levanta excecao para exercitar o fluxo de erro (retry/visibilidade da SQS).

Variaveis de ambiente:
- RUNS_TABLE : tabela DynamoDB de logs de processo
- LOG_LEVEL  : nivel de log (default INFO)
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

RUNS_TABLE = os.environ.get("RUNS_TABLE", "integracao-lab01-runs")
dynamodb = boto3.resource("dynamodb")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _process_detail(detail):
    correlation_id = detail.get("correlationId", "desconhecido")
    event_type = detail.get("eventType", "desconhecido")

    if detail.get("data", {}).get("forceFail") is True:
        raise RuntimeError(f"Falha controlada solicitada para {correlation_id}")

    table = dynamodb.Table(RUNS_TABLE)
    item = {
        "runId": correlation_id,
        "sk": f"PROCESS#{event_type}",
        "target": "processor",
        "eventType": event_type,
        "state": "SUCCEEDED",
        "observedAt": _now_iso(),
    }
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(runId) AND attribute_not_exists(sk)",
        )
        logger.info(json.dumps({"msg": "processamento registrado", "correlationId": correlation_id, "eventType": event_type}))
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info(json.dumps({"msg": "ja processado (idempotencia)", "correlationId": correlation_id}))
        else:
            raise


def handler(event, context):
    """Consumidor SQS: event['Records'] contem as mensagens da fila."""
    records = event.get("Records", []) if isinstance(event, dict) else []
    logger.info(json.dumps({"msg": "lote recebido da SQS", "count": len(records)}))

    for record in records:
        body = json.loads(record["body"])
        # Mensagem entregue pelo EventBridge a SQS mantem o envelope do evento.
        detail = body.get("detail", body)
        _process_detail(detail)

    return {"status": "ok", "processed": len(records)}
