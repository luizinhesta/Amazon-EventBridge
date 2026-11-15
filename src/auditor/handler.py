"""
EventBridge - Lambda auditoria.

E um TARGET direto de uma rule do EventBridge (ex.: order.paid).
Recebe o evento ja roteado pelo barramento e registra evidencia de
processamento na tabela DynamoDB, avancando o estado da execucao.

Idempotencia: usa (runId, sk) deterministico para tolerar replay/reentrega
sem duplicar registros. Replays do Archive reexecutam este target; o
ConditionExpression evita sobrescrever um evento ja auditado.

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


def handler(event, context):
    """Target de rule: recebe o evento completo do EventBridge."""
    logger.info(json.dumps({"msg": "evento recebido para auditoria", "event": event}))

    detail = event.get("detail", {}) if isinstance(event, dict) else {}
    correlation_id = detail.get("correlationId", "desconhecido")
    event_type = detail.get("eventType") or event.get("detail-type", "desconhecido")

    table = dynamodb.Table(RUNS_TABLE)
    sort_key = f"AUDIT#{event_type}"

    item = {
        "runId": correlation_id,
        "sk": sort_key,
        "target": "auditor",
        "eventType": event_type,
        "state": "SUCCEEDED",
        "observedAt": _now_iso(),
        "source": event.get("source", "desconhecido"),
    }

    try:
        # Idempotente: so grava se ainda nao existir esta combinacao (evita duplicata em replay).
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(runId) AND attribute_not_exists(sk)",
        )
        logger.info(json.dumps({"msg": "auditoria registrada", "correlationId": correlation_id, "eventType": event_type}))
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info(json.dumps({"msg": "evento ja auditado (idempotencia)", "correlationId": correlation_id, "eventType": event_type}))
        else:
            logger.exception("Falha ao gravar auditoria")
            raise

    return {"status": "ok", "correlationId": correlation_id, "eventType": event_type}
